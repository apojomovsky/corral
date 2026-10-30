"""Board-variant wiring: sync, mirror and drive through one factory.

Fakes only: ``FakeBoardTracker`` (a ``FakeTracker`` with a scripted
board side) stands in for board repos, and one leg runs the real
``BoardBackedTracker`` over canned GraphQL/REST payloads. No network,
no subprocess, no real state dir.
"""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from corral import board, drive, mirror, mirrorcli, sync, syncli, tracker
from corral import trackers
from corral.github.board import BoardSchema
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    rest_issue,
    schema_payload,
    text_value,
)
from tests.test_board_fields import FakePacer
from tests.test_tracker import FakeTracker, item_payload, reading

APP = "acme/app"
DEMO = "example/demo"
T0 = 1_800_000_100.0

TWO_REPO_TOML = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"
tracker.operators = ["alice"]

[[repo]]
name = "demo"
tracker.repo = "example/demo"
tracker.variant = "board"
tracker.operators = ["alice"]

[repo.board]
project_owner = "example"
project_number = 3
"""


class FakeBoardTracker(FakeTracker):
    """A ``FakeTracker`` with a scripted board side: schema plus poll.

    ``board_items`` are returned by ``poll``; ``poll_error`` fails the
    poll, ``schema_error`` fails ``board_schema``. Every call is recorded
    in ``board_calls``.
    """

    def __init__(self, items=(), board_items=(), poll_error=None, schema_error=None):
        super().__init__(items)
        self.board_items = list(board_items)
        self.poll_error = poll_error
        self.schema_error = schema_error
        self.board_calls = []

    def board_schema(self):
        self.board_calls.append("schema")
        if self.schema_error is not None:
            raise self.schema_error
        return BoardSchema(project_id="PVT_1", fields={})

    @property
    def board_poll_pages(self):
        return 5

    def poll(self, schema, *, after=None, max_pages):
        self.board_calls.append(("poll", after, max_pages))
        if self.poll_error is not None:
            raise self.poll_error
        return list(self.board_items), None


class Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class FakeGit:
    def __init__(self):
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        return Proc(0, "")


class WiringCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-wiring-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO_REPO_TOML)
        self.store = board.open_store(os.path.join(self.root, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.git = FakeGit()

    def factory_for(self, mapping):
        def factory(tracker_repo):
            return mapping[tracker_repo]

        return factory

    def run_sync(self, factory, names=(), **over):
        params = {
            "resync": False,
            "audit": None,
            "as_json": False,
            "tracker": FakeTracker(),
            "store": self.store,
            "git": self.git,
            "now": T0,
            "out": self.out,
            "err": self.err,
            "tracker_factory": factory,
        }
        params.update(over)
        return syncli.sync_root(self.root, list(names), **params)

    def run_mirror(self, factory, names=(), **over):
        params = {
            "limit": None,
            "as_json": False,
            "tracker": FakeTracker(),
            "store": self.store,
            "git": self.git,
            "now": T0,
            "out": self.out,
            "err": self.err,
            "tracker_factory": factory,
        }
        params.update(over)
        return mirrorcli.mirror_root(self.root, list(names), **params)


class SyncFanOutTest(WiringCase):
    def test_board_step_runs_for_board_repos_only(self):
        app = FakeTracker([reading(repo=APP, number=1, updated_at=T0)])
        demo = FakeBoardTracker(
            items=[reading(repo=DEMO, number=7, updated_at=T0)],
            board_items=[reading(repo=DEMO, number=7, updated_at=T0, extra={"area": "Backend"})],
        )
        code = self.run_sync(self.factory_for({APP: app, DEMO: demo}))
        self.assertEqual(code, 0)
        row, _ = self.store.item(APP, 1, now=T0)
        self.assertEqual(row.title, "Title")
        row, _ = self.store.item(DEMO, 7, now=T0)
        self.assertEqual(row.extra.get("area"), "Backend")
        self.assertEqual(
            self.store.get_cursor(DEMO, sync.BOARD_CURSOR_NAME), tracker.format_cursor(T0)
        )
        self.assertEqual(demo.board_calls[0], "schema")
        self.assertEqual(demo.board_calls[1], ("poll", None, 5))
        self.assertEqual(app.calls, [("changes", APP, None)])

    def test_board_detail_lands_in_json_but_label_repos_do_not_carry_it(self):
        app = FakeTracker([reading(repo=APP, number=1, updated_at=T0)])
        demo = FakeBoardTracker(
            items=[reading(repo=DEMO, number=7, updated_at=T0)],
            board_items=[reading(repo=DEMO, number=7, updated_at=T0)],
        )
        self.assertEqual(self.run_sync(self.factory_for({APP: app, DEMO: demo}), as_json=True), 0)
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["repos"]["demo"]["board_seen"], 1)
        self.assertEqual(doc["repos"]["demo"]["board_cursor"], tracker.format_cursor(T0))
        self.assertNotIn("board_seen", doc["repos"]["app"])
        self.assertNotIn("board_cursor", doc["repos"]["app"])
        self.assertIn("board", self.out.getvalue())

    def test_board_variant_without_a_reader_is_exit_four_naming_the_repo(self):
        app = FakeTracker()
        code = self.run_sync(self.factory_for({APP: app, DEMO: FakeTracker()}))
        self.assertEqual(code, 4)
        self.assertIn("demo", self.err.getvalue())

    def test_board_poll_failure_degrades_to_store_data(self):
        app = FakeTracker([reading(repo=APP, number=1, updated_at=T0)])
        demo = FakeBoardTracker(
            items=[reading(repo=DEMO, number=7, updated_at=T0)],
            poll_error=tracker.TrackerError("board unreachable: gone"),
        )
        code = self.run_sync(self.factory_for({APP: app, DEMO: demo}))
        self.assertEqual(code, 0)
        row, _ = self.store.item(DEMO, 7, now=T0)
        self.assertEqual(row.title, "Title")
        self.assertIsNone(self.store.get_cursor(DEMO, sync.BOARD_CURSOR_NAME))
        self.assertIn("board unreachable", self.err.getvalue())

    def test_board_budget_exhaustion_keeps_store_data(self):
        app = FakeTracker()
        demo = FakeBoardTracker(
            items=[reading(repo=DEMO, number=7, updated_at=T0)],
            schema_error=tracker.TrackerRateLimited("rate limit", 60.0),
        )
        code = self.run_sync(self.factory_for({APP: app, DEMO: demo}))
        self.assertEqual(code, 0)
        row, _ = self.store.item(DEMO, 7, now=T0)
        self.assertEqual(row.title, "Title")
        self.assertIn("rate limit", self.err.getvalue())

    def test_resync_runs_the_board_step(self):
        demo = FakeBoardTracker(
            items=[reading(repo=DEMO, number=7, updated_at=T0)],
            board_items=[reading(repo=DEMO, number=7, updated_at=T0)],
        )
        code = self.run_sync(self.factory_for({APP: FakeTracker(), DEMO: demo}), resync=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            self.store.get_cursor(DEMO, sync.BOARD_CURSOR_NAME), tracker.format_cursor(T0)
        )

    def test_audit_rereads_through_the_board_item(self):
        self.store.upsert_item(
            board.Item(repo=DEMO, number=7, tracker_updated_at=T0 - 200, author="alice"),
            operators=["alice"],
            now=T0 - 200,
        )
        demo = FakeBoardTracker(
            board_items=[reading(repo=DEMO, number=7, updated_at=T0)],
        )
        demo.by_key[(DEMO, 7)] = reading(repo=DEMO, number=7, updated_at=T0)
        code = self.run_sync(self.factory_for({APP: FakeTracker(), DEMO: demo}), audit=1)
        self.assertEqual(code, 0)
        self.assertIn(("item", DEMO, 7), demo.calls)


class MirrorSplitTest(WiringCase):
    def seed(self, repo, number, author="alice"):
        self.store.upsert_item(
            board.Item(
                repo=repo,
                number=number,
                tracker_updated_at=T0 - 100,
                author=author,
            ),
            operators=[author],
            now=T0 - 100,
        )
        self.store.enqueue(repo, number, "agent", "agent-1", now=T0 - 50)

    def test_each_repo_writes_through_its_own_tracker(self):
        self.seed(APP, 1)
        self.seed(DEMO, 7)
        app = FakeTracker()
        demo = FakeBoardTracker()
        code = self.run_mirror(self.factory_for({APP: app, DEMO: demo}))
        self.assertEqual(code, 0)
        self.assertIn(("write", APP, 1, "agent", "agent-1"), app.calls)
        self.assertIn(("write", DEMO, 7, "agent", "agent-1"), demo.calls)

    def test_limit_decrements_across_repos(self):
        self.seed(APP, 1)
        self.seed(DEMO, 7)
        app = FakeTracker()
        demo = FakeBoardTracker()
        code = self.run_mirror(self.factory_for({APP: app, DEMO: demo}), limit=1)
        self.assertEqual(code, 0)
        self.assertIn(("write", APP, 1, "agent", "agent-1"), app.calls)
        self.assertNotIn(("write", DEMO, 7, "agent", "agent-1"), demo.calls)
        queued = [row for row in self.store.queued_writes()]
        self.assertEqual([(row.repo, row.number) for row in queued], [(DEMO, 7)])

    def test_unknown_repo_is_exit_four_naming_the_repo(self):
        self.seed(DEMO, 7)
        code = self.run_mirror(self.factory_for({APP: FakeTracker()}))
        self.assertEqual(code, 4)
        self.assertIn("demo", self.err.getvalue())


class PushReposTest(unittest.TestCase):
    def test_merged_outcome_sums_and_stops_once(self):
        import os
        import tempfile

        from tests.test_mirror import MirrorTracker

        tmp = tempfile.TemporaryDirectory(prefix="corral-push-repos-")
        self.addCleanup(tmp.cleanup)
        store = board.open_store(os.path.join(tmp.name, "board.sqlite"))
        self.addCleanup(store.close)
        for repo, number in ((APP, 1), (DEMO, 7)):
            store.upsert_item(
                board.Item(repo=repo, number=number, tracker_updated_at=1000.0, author="alice"),
                operators=["alice"],
                now=1000.0,
            )
            store.enqueue(repo, number, "agent", "agent-1", now=1500.0)
        first, second = MirrorTracker(), MirrorTracker()
        result = mirror.push_repos(
            store,
            [(APP, first), (DEMO, second)],
            operators={APP: ["alice"], DEMO: ["alice"]},
            now=1600.0,
        )
        self.assertEqual(result.sent, 2)
        self.assertEqual(result.errors, ())
        self.assertEqual(first.writes, [(APP, 1, "agent", "agent-1")])
        self.assertEqual(second.writes, [(DEMO, 7, "agent", "agent-1")])

    def test_limit_spans_repos(self):
        import os
        import tempfile

        from tests.test_mirror import MirrorTracker

        tmp = tempfile.TemporaryDirectory(prefix="corral-push-repos-")
        self.addCleanup(tmp.cleanup)
        store = board.open_store(os.path.join(tmp.name, "board.sqlite"))
        self.addCleanup(store.close)
        for repo, number in ((APP, 1), (DEMO, 7)):
            store.upsert_item(
                board.Item(repo=repo, number=number, tracker_updated_at=1000.0, author="alice"),
                operators=["alice"],
                now=1000.0,
            )
            store.enqueue(repo, number, "agent", "agent-1", now=1500.0)
        first, second = MirrorTracker(), MirrorTracker()
        result = mirror.push_repos(
            store,
            [(APP, first), (DEMO, second)],
            operators={APP: ["alice"], DEMO: ["alice"]},
            now=1600.0,
            limit=1,
        )
        self.assertEqual(result.sent, 1)
        self.assertEqual(first.writes, [(APP, 1, "agent", "agent-1")])
        self.assertEqual(second.writes, [])


class DriveFanOutTest(WiringCase):
    def run_pass(self, factory, **fields):
        from tests.test_drive import hermetic_pass

        params = {
            "tracker_factory": factory,
            "operators": {APP: ["alice"], DEMO: ["alice"]},
        }
        params.update(fields)
        return hermetic_pass(
            self.store,
            os.path.join(self.tmp.name, "drive.lock"),
            self.out,
            self.err,
            plan=False,
            found=(),
            **params,
        )

    def seed_row(self, repo, number, author="alice"):
        self.store.upsert_item(
            board.Item(
                repo=repo,
                number=number,
                tracker_updated_at=T0 - 100,
                author=author,
            ),
            operators=["alice"],
            now=T0 - 100,
        )

    def test_pass_syncs_and_mirrors_each_repo_through_its_tracker(self):
        app = FakeTracker([reading(repo=APP, number=1, updated_at=T0)])
        demo = FakeBoardTracker(
            items=[reading(repo=DEMO, number=7, updated_at=T0)],
            board_items=[reading(repo=DEMO, number=7, updated_at=T0)],
        )
        self.seed_row(APP, 1)
        self.seed_row(DEMO, 7)
        self.store.enqueue(APP, 1, "agent", "agent-1", now=T0 - 50)
        self.store.enqueue(DEMO, 7, "agent", "agent-7", now=T0 - 50)
        code = self.run_pass(self.factory_for({APP: app, DEMO: demo}))
        self.assertEqual(code, 0)
        row, _ = self.store.item(DEMO, 7, now=T0)
        self.assertIsNotNone(row)
        self.assertEqual(
            self.store.get_cursor(DEMO, sync.BOARD_CURSOR_NAME),
            tracker.format_cursor(T0),
        )
        self.assertEqual(list(self.store.queued_writes()), [])
        self.assertIn(("write", APP, 1, "agent", "agent-1"), app.calls)
        self.assertIn(("write", DEMO, 7, "agent", "agent-7"), demo.calls)

    def test_repo_unknown_to_the_factory_is_an_error_line(self):
        self.seed_row("acme/ghost", 9)
        self.seed_row(APP, 1)
        app = FakeTracker([reading(repo=APP, number=1, updated_at=T0)])
        code = self.run_pass(self.factory_for({APP: app}))
        self.assertEqual(code, 0)
        self.assertIn("acme/ghost", self.err.getvalue())

    def test_freshness_reread_uses_the_factory_tracker(self):
        self.seed_row(DEMO, 7)
        demo = FakeBoardTracker()
        demo.by_key[(DEMO, 7)] = reading(repo=DEMO, number=7, updated_at=T0)
        live, problem = drive.refresh_row(
            self.store,
            self.factory_for({DEMO: demo}),
            DEMO,
            7,
            operators=["alice"],
            now=T0,
            stale_after=0,
        )
        self.assertIsNone(problem)
        self.assertEqual(live.number, 7)
        self.assertIn(("item", DEMO, 7), demo.calls)


class ProductionTrackersTest(WiringCase):
    def test_builds_one_tracker_per_variant(self):
        from corral.github.tracker import GithubTracker
        from corral.trackers import BoardBackedTracker as Backed

        mapping, problem = trackers.production_trackers(self.root, ["app", "demo"], git=self.git)
        self.assertIsNone(problem)
        self.assertIsInstance(mapping["app"].tracker, GithubTracker)
        self.assertEqual(mapping["app"].variant, "labels")
        self.assertEqual(mapping["app"].tracker_repo, APP)
        self.assertIsInstance(mapping["demo"].tracker, Backed)
        self.assertEqual(mapping["demo"].variant, "board")
        self.assertEqual(mapping["demo"].tracker_repo, DEMO)

    def test_bad_variant_is_a_problem_naming_the_repo(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                TWO_REPO_TOML.replace('tracker.variant = "board"', 'tracker.variant = "bord"')
            )
        mapping, problem = trackers.production_trackers(self.root, ["app", "demo"], git=self.git)
        self.assertEqual(mapping, {})
        detail = problem or ""
        self.assertIn("demo", detail)
        self.assertIn("tracker.variant", detail)


class DoctorFetchTest(unittest.TestCase):
    def test_production_fetch_uses_the_factory(self):
        from corral import doctor
        from corral.github import gh

        cfg = config()
        with mock.patch.object(trackers, "board_schema_for", return_value="SCHEMA") as fetch:
            found = doctor.production_board_fetch(cfg)
        self.assertEqual(found, "SCHEMA")
        args, kwargs = fetch.call_args
        self.assertIs(args[0], cfg)
        self.assertIs(kwargs["run"], gh.run)


class RealTrackerEndToEndTest(WiringCase):
    """One board repo end to end through the real tracker, canned payloads only."""

    def canned_demo(self):
        run = BoardFakeRun()
        run.script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        page = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "PVTI-7",
                            "updatedAt": "2026-10-28T13:00:00Z",
                            "content": {
                                "__typename": "Issue",
                                "number": 7,
                                "repository": {"nameWithOwner": DEMO},
                            },
                            "fieldValues": {"nodes": [text_value("F_agent", "Agent", "agent-1")]},
                        }
                    ],
                }
            }
        }
        run.script("BoardItems", [page, page])
        run.script(
            "BoardWriteText",
            [{"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI-7"}}}],
        )
        run.add_rest(
            f"repos/{DEMO}/issues?state=all&sort=updated&direction=asc&per_page=100",
            [item_payload(7)],
        )
        rest_issue(run, 7, repo=DEMO)
        return run

    def test_sync_push_and_mirror_through_the_real_board_tracker(self):
        run = self.canned_demo()
        real = trackers.BoardBackedTracker(config(), run, pacer=FakePacer(), max_pages=5)
        app = FakeTracker([reading(repo=APP, number=1, updated_at=T0)])
        for repo, number in ((APP, 1), (DEMO, 7)):
            self.store.upsert_item(
                board.Item(repo=repo, number=number, tracker_updated_at=1000.0, author="alice"),
                operators=["alice"],
                now=1000.0,
            )
        self.store.enqueue(APP, 1, "agent", "agent-1", now=T0)
        self.store.enqueue(DEMO, 7, "agent", "agent-9", now=T0)
        code = self.run_sync(self.factory_for({APP: app, DEMO: real}), as_json=True, push=True)
        self.assertEqual(code, 0)
        row, _ = self.store.item(DEMO, 7, now=T0)
        self.assertEqual(row.tracker_updated_at, tracker.parse_cursor("2026-10-28T13:00:00Z"))
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["repos"]["demo"]["board_seen"], 1)
        self.assertEqual(doc["mirror"]["sent"], 2)
        writes = run.graphql_variables("BoardWriteText")
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["text"], "agent-9")
        self.assertEqual(len(run.graphql_variables("BoardSchemaByNumber")), 1)
