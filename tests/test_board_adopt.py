"""The board step adopts missing issues every pass, sharing one adder with setup.

Fakes only: scripted GraphQL/REST doubles at the injected ``run`` seam
and ``FakePacer`` for the shared bucket. No ``gh``, no network, no real
state dir.
"""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from korapy import board, drive, sync, syncli, tracker, trackers
from korapy.github import board as board_mod
from korapy.github import gh
from tests.test_board_fields import FakePacer
from tests.test_board_provision import ProvisionFakeRun
from tests.test_board_reader import BoardFakeRun
from tests.test_board_sync import FakeBoardReader
from tests.test_board_wiring import FakeBoardTracker, WiringCase
from tests.test_tracker import FakeTracker, reading

REPO = "example/demo"
T0 = 1_800_000_100.0
DAY = 86400.0


def listed(number, state="open", closed_days_ago=None, pr=False):
    """One REST issue-listing payload: node id, state and closed time."""
    payload = {
        "number": number,
        "node_id": f"I_{number}",
        "state": state,
        "closed_at": None,
        "title": f"Issue {number}",
    }
    if closed_days_ago is not None:
        payload["closed_at"] = tracker.format_cursor(T0 - closed_days_ago * DAY)
    if pr:
        payload["pull_request"] = {"url": f"https://example.com/pull/{number}"}
    return payload


def listing_route(run, *pages):
    """Script the paged adoptable-issues REST listing (state=all)."""
    for index, page in enumerate(pages):
        path = f"repos/{REPO}/issues?state=all&per_page=100"
        if index:
            path += f"&page={index + 1}"
        run.add_rest(path, page)
    return run


def add_route(run, answers):
    """Script the add-item mutation answers."""
    return run.script("AddItem", list(answers))


def added_answer(item_id="ITEM_new"):
    return {"addProjectV2ItemById": {"item": {"id": item_id}}}


def rate_limited_answer():
    body = {
        "data": {"addProjectV2ItemById": None},
        "errors": [{"type": "RATE_LIMITED", "message": "slow down"}],
    }
    return gh.GhError("other", "slow down", output=json.dumps(body))


def loop(run, present=(), candidates=(), limit=None):
    """The shared adder over plain data and a fake run."""
    return board_mod.add_missing_issues(
        run,
        FakePacer(),
        project_id="PVT_1",
        present=set(present),
        candidates=list(candidates),
        limit=limit,
    )


def candidate(number, key=None):
    """One ``(key, content id, name)`` candidate for the shared loop."""
    return (key if key is not None else f"I_{number}", f"I_{number}", f"{REPO}#{number}")


class SharedLoopTest(unittest.TestCase):
    def test_a_missing_issue_is_added_once(self):
        run = ProvisionFakeRun()
        add_route(run, [added_answer("ITEM_2")])
        report = loop(run, candidates=[candidate(2)])
        self.assertEqual(report.added, (f"{REPO}#2",))
        self.assertEqual(report.kept, ())
        self.assertEqual(report.refused, ())
        self.assertIsNone(report.stopped)
        flags = run.flag_values("AddItem", "")
        self.assertIn("projectId=PVT_1", flags)
        self.assertIn("contentId=I_2", flags)
        self.assertNotIn("contentId=I_1", flags)

    def test_an_already_present_issue_is_kept_without_a_mutation(self):
        run = ProvisionFakeRun()
        report = loop(run, present={"I_2"}, candidates=[candidate(2)])
        self.assertEqual(report.added, ())
        self.assertEqual(report.kept, (f"{REPO}#2",))
        self.assertEqual(run.mutations(), [])

    def test_the_cap_leaves_the_rest_for_the_next_pass(self):
        run = ProvisionFakeRun()
        add_route(run, [added_answer("ITEM_2")])
        report = loop(run, candidates=[candidate(2), candidate(3)], limit=1)
        self.assertEqual(report.added, (f"{REPO}#2",))
        rerun = ProvisionFakeRun()
        add_route(rerun, [added_answer("ITEM_3")])
        again = loop(rerun, present={"I_2"}, candidates=[candidate(2), candidate(3)], limit=1)
        self.assertEqual(again.added, (f"{REPO}#3",))

    def test_refused_adds_consume_the_cap(self):
        run = ProvisionFakeRun()
        add_route(run, [gh.GhError("other", "boom (HTTP 500)") for _ in range(3)])
        report = loop(run, candidates=[candidate(2), candidate(3), candidate(4)], limit=2)
        self.assertEqual(len(report.refused), 2)
        self.assertEqual(report.added, ())
        self.assertEqual(len(run.mutations()), 2)

    def test_a_rate_limit_mid_pass_stops_with_the_rest_left(self):
        run = ProvisionFakeRun()
        add_route(run, [added_answer("ITEM_2"), rate_limited_answer()])
        report = loop(run, candidates=[candidate(2), candidate(3)])
        self.assertEqual(report.added, (f"{REPO}#2",))
        self.assertIsNotNone(report.stopped)
        self.assertGreater(report.stopped[1], 0)

    def test_a_failed_add_is_refused_and_retried_next_pass(self):
        run = ProvisionFakeRun()
        add_route(run, [gh.GhError("other", "boom (HTTP 500)")])
        report = loop(run, candidates=[candidate(2)])
        self.assertEqual(report.added, ())
        self.assertEqual(len(report.refused), 1)
        self.assertIn(f"{REPO}#2", report.refused[0])
        rerun = ProvisionFakeRun()
        add_route(rerun, [added_answer("ITEM_2")])
        again = loop(rerun, candidates=[candidate(2)])
        self.assertEqual(again.added, (f"{REPO}#2",))
        self.assertEqual(again.refused, ())

    def test_duplicate_candidate_keys_add_once(self):
        run = ProvisionFakeRun()
        add_route(run, [added_answer("ITEM_2")])
        report = loop(run, candidates=[candidate(2), candidate(2)])
        self.assertEqual(report.added, (f"{REPO}#2",))
        self.assertEqual(len(run.mutations()), 1)


class AdoptingTrackerTest(unittest.TestCase):
    def tracker(self, run):
        from tests.test_board_reader import config

        return trackers.BoardBackedTracker(
            config(), run, pacer=FakePacer(), max_pages=5, adopt_limit=20
        )

    def adopt(self, run, present=(), limit=20):
        return self.tracker(run).adopt_missing("PVT_1", REPO, set(present), limit=limit, now=T0)

    def test_adopt_adds_missing_open_and_recently_closed_only(self):
        run = BoardFakeRun()
        listing_route(
            run,
            [
                listed(1),
                listed(2),
                listed(3, state="closed", closed_days_ago=1),
                listed(4, state="closed", closed_days_ago=10),
                listed(5, pr=True),
            ],
        )
        add_route(run, [added_answer("ITEM_2"), added_answer("ITEM_3")])
        report = self.adopt(run, present={(REPO, 1)})
        self.assertEqual(report.added, (f"{REPO}#2", f"{REPO}#3"))
        self.assertEqual(report.kept, (f"{REPO}#1",))
        self.assertEqual(report.refused, ())
        self.assertIsNone(report.stopped)
        flags = run.graphql_variables("AddItem")
        added_ids = [call["contentId"] for call in flags]
        self.assertEqual(added_ids, ["I_2", "I_3"])

    def test_adopt_respects_the_cap(self):
        run = BoardFakeRun()
        listing_route(run, [listed(2), listed(3)])
        add_route(run, [added_answer("ITEM_2")])
        report = self.adopt(run, limit=1)
        self.assertEqual(report.added, (f"{REPO}#2",))

    def test_adopt_rate_limit_stops_cleanly(self):
        run = BoardFakeRun()
        listing_route(run, [listed(2), listed(3)])
        add_route(run, [added_answer("ITEM_2"), rate_limited_answer()])
        report = self.adopt(run)
        self.assertEqual(report.added, (f"{REPO}#2",))
        self.assertIsNotNone(report.stopped)

    def test_an_unexpected_state_fails_the_adopt(self):
        run = BoardFakeRun()
        listing_route(run, [listed(2, state="bogus")])
        with self.assertRaises(tracker.TrackerError):
            self.adopt(run)

    def test_zero_limit_lists_nothing(self):
        run = BoardFakeRun()
        seen = []
        run.rest_fallback = lambda path: seen.append(path) or json.dumps([])
        report = self.adopt(run, limit=0)
        self.assertEqual(report, tracker.BoardAdopt((), (), (), None))
        self.assertEqual(seen, [])

    def test_negative_limit_lists_nothing(self):
        run = BoardFakeRun()
        seen = []
        run.rest_fallback = lambda path: seen.append(path) or json.dumps([])
        report = self.adopt(run, limit=-1)
        self.assertEqual(report, tracker.BoardAdopt((), (), (), None))
        self.assertEqual(seen, [])


class SharedFunctionTest(unittest.TestCase):
    def test_populate_and_the_pass_call_the_same_adder(self):
        from tests.test_board_populate import issue, issues_route, items_page, item_node

        calls = []
        real = board_mod.add_missing_issues

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        populate_run = ProvisionFakeRun()
        populate_run.script("BoardItems", [items_page()])
        issues_route(populate_run, [issue(2)])
        add_route(populate_run, [added_answer("ITEM_2")])
        adopt_run = BoardFakeRun()
        listing_route(adopt_run, [listed(3)])
        add_route(adopt_run, [added_answer("ITEM_3")])
        from tests.test_board_reader import config

        backed = trackers.BoardBackedTracker(
            config(), adopt_run, pacer=FakePacer(), max_pages=5, adopt_limit=20
        )
        with mock.patch.object(board_mod, "add_missing_issues", new=spy):
            report = board_mod.GithubBoardProvisioner(populate_run, pacer=FakePacer()).populate(
                "PVT_1", REPO
            )
            adopted = backed.adopt_missing("PVT_1", REPO, set(), limit=20, now=T0)
        self.assertEqual(report.added, (f"{REPO}#2",))
        self.assertEqual(adopted.added, (f"{REPO}#3",))
        self.assertEqual(len(calls), 2)


class AdoptingReader(FakeBoardReader):
    """A board poll double that also records the adopt step."""

    def __init__(self, *args, adopt_report=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.board_adopt_limit = 20
        self.adopt_report = adopt_report
        self.adopt_calls = []

    def adopt_missing(self, project_id, repo, present, *, limit, now):
        self.adopt_calls.append(
            {"project_id": project_id, "repo": repo, "present": present, "limit": limit}
        )
        if isinstance(self.adopt_report, Exception):
            raise self.adopt_report
        return self.adopt_report


class BoardStepCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-adopt-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def poll(self, reader, max_pages=5):
        from korapy.github.board import BoardSchema

        return sync.sync_board_repo(
            self.store,
            reader,
            BoardSchema(project_id="PVT_1", fields={}),
            REPO,
            operators=["alice"],
            now=T0,
            max_pages=max_pages,
        )


class BoardStepAdoptTest(BoardStepCase):
    def adopted(self, added=(), refused=(), stopped=None):
        return tracker.BoardAdopt(tuple(added), (), tuple(refused), stopped)

    def test_a_complete_poll_adopts_what_it_does_not_hold(self):
        reader = AdoptingReader(
            [reading(repo=REPO, number=1, updated_at=T0)],
            adopt_report=self.adopted(refused=[f"{REPO}#9: boom"]),
        )
        result = self.poll(reader)
        self.assertEqual(len(reader.adopt_calls), 1)
        self.assertEqual(reader.adopt_calls[0]["present"], {(REPO, 1)})
        self.assertEqual(reader.adopt_calls[0]["limit"], 20)
        self.assertEqual(result.adopt_errors, (f"{REPO}#9: boom",))
        self.assertEqual(result.errors, ())
        self.assertEqual(result.refused, ())

    def test_a_truncated_poll_skips_the_adopt(self):
        reader = AdoptingReader(
            [
                reading(repo=REPO, number=1, updated_at=T0),
                reading(repo=REPO, number=2, updated_at=T0),
            ],
            page_size=1,
            adopt_report=self.adopted(),
        )
        result = self.poll(reader, max_pages=1)
        self.assertEqual(reader.adopt_calls, [])
        self.assertEqual(result.adopt_errors, ())

    def test_a_rate_limited_adopt_is_an_error_line(self):
        reader = AdoptingReader(
            [reading(repo=REPO, number=1, updated_at=T0)],
            adopt_report=self.adopted(stopped=("slow down", 60.0)),
        )
        result = self.poll(reader)
        self.assertEqual(result.adopt_errors, ())
        self.assertEqual(len(result.errors), 1)
        self.assertIn("rate limited", result.errors[0])
        self.assertIn(REPO, result.errors[0])

    def test_a_reader_without_an_adopt_cap_skips_the_adopt(self):
        result = self.poll(FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0)]))
        self.assertEqual(result.adopt_errors, ())
        self.assertEqual(result.errors, ())


class AdoptingBoardTracker(FakeBoardTracker):
    """A board wiring double that also records the adopt step."""

    def __init__(self, *args, adopt_report=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.board_adopt_limit = 20
        self.adopt_report = adopt_report
        self.adopt_calls = []

    def adopt_missing(self, project_id, repo, present, *, limit, now):
        self.adopt_calls.append(
            {"project_id": project_id, "repo": repo, "present": set(present), "limit": limit}
        )
        if isinstance(self.adopt_report, Exception):
            raise self.adopt_report
        return self.adopt_report


def failed_adopt():
    return tracker.BoardAdopt((), (), (f"example/demo#9: boom",), None)


class DriveAdoptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-adopt-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.store.upsert_item(
            board.Item(repo=REPO, number=7, tracker_updated_at=T0 - 100, author="alice"),
            operators=["alice"],
            now=T0 - 100,
        )

    def test_each_repo_adopts_up_to_its_own_cap(self):
        other = "example/other"
        self.store.upsert_item(
            board.Item(repo=other, number=7, tracker_updated_at=T0 - 100, author="alice"),
            operators=["alice"],
            now=T0 - 100,
        )
        first = AdoptingBoardTracker(
            items=[reading(repo=REPO, number=7, updated_at=T0)],
            board_items=[reading(repo=REPO, number=7, updated_at=T0)],
            adopt_report=failed_adopt(),
        )
        second = AdoptingBoardTracker(
            items=[reading(repo=other, number=7, updated_at=T0)],
            board_items=[reading(repo=other, number=7, updated_at=T0)],
            adopt_report=tracker.BoardAdopt((), (), (f"{other}#9: boom",), None),
        )
        by_repo = {REPO: first, other: second}
        seen, errors, store_errors, refused, adopt_errors = drive.sync_all(
            self.store, lambda repo: by_repo[repo], operators=["alice"], now=T0
        )
        self.assertEqual(first.adopt_calls[0]["limit"], 20)
        self.assertEqual(second.adopt_calls[0]["limit"], 20)
        self.assertEqual(adopt_errors, [f"{REPO}#9: boom", f"{other}#9: boom"])
        self.assertEqual(errors, [])

    def test_failed_adds_ride_along_without_failing_the_pass(self):
        fake = AdoptingBoardTracker(
            items=[reading(repo=REPO, number=7, updated_at=T0)],
            board_items=[reading(repo=REPO, number=7, updated_at=T0)],
            adopt_report=failed_adopt(),
        )
        seen, errors, store_errors, refused, adopt_errors = drive.sync_all(
            self.store, lambda repo: fake, operators=["alice"], now=T0
        )
        self.assertEqual(errors, [])
        self.assertEqual(refused, [])
        self.assertEqual(adopt_errors, [f"{REPO}#9: boom"])
        self.assertEqual(len(fake.adopt_calls), 1)
        self.assertEqual(fake.adopt_calls[0]["present"], {(REPO, 7)})


class SyncAdoptTest(WiringCase):
    def test_sync_logs_failed_adds_and_exits_zero(self):
        demo = AdoptingBoardTracker(
            items=[reading(repo="example/demo", number=7, updated_at=T0)],
            board_items=[reading(repo="example/demo", number=7, updated_at=T0)],
            adopt_report=failed_adopt(),
        )
        code = self.run_sync(self.factory_for({"acme/app": FakeTracker(), "example/demo": demo}))
        self.assertEqual(code, 0)
        self.assertIn("example/demo#9", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
