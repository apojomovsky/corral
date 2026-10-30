"""Board field carriage, ticket 1: extra through the tracker, sync, the store, and the gh seam.

No network, no subprocess, no real state dir: the tracker is a fake, the
store is a temporary file, and every ``graphql``/``paced_run`` call here
injects a fake run and a fake pacer.

A tripwire installed at import wraps the real pacer factory for the whole
run: any test that reaches the default pacer file records an offender, and
the suite fails at the end through the usual guard report. Real
subprocesses stay covered by the ``gh`` shim on PATH.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from corral import board, sync, tracker
from corral.github import gh
from corral.github import tracker as ghtracker
from tests import suiteguard
from tests.test_tracker import FakeRun, FakeTracker, reading

_REAL_PACER_FACTORY = gh._pacer


def _guarded_pacer_factory() -> gh.GraphqlPacer:
    """The real pacer factory, recording any test that reaches it.

    Installed at import so it watches the whole run: test discovery
    imports every module before running any test. A call means some test
    used the default pacer (the state dir file) instead of an injected
    fake, so it records an offender and the suite fails at the end. It
    still delegates, so behavior is unchanged.
    """
    suiteguard.record("reached the default GraphQL pacer (no injected pacer)")
    return _REAL_PACER_FACTORY()


_guarded_pacer_factory._corral_pacer_guard = True  # ty: ignore[unresolved-attribute]
gh._pacer = _guarded_pacer_factory  # ty: ignore[invalid-assignment]


class FakePacer:
    """A GraphqlPacer double: records calls, never touches the state dir."""

    def __init__(self):
        self.acquired = []
        self.charged = []
        self.cooldowns = []

    def acquire(self, cost=gh.GRAPHQL_CALL_COST):
        self.acquired.append(cost)

    def charge(self, cost):
        self.charged.append(cost)

    def cooldown(self, seconds):
        self.cooldowns.append(seconds)

    def cooldown_remaining(self):
        return 0.0


class GraphqlRun:
    """A `gh` seam double for `graphql`: path-shaped argv in, JSON out."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        if isinstance(self.payload, Exception):
            raise self.payload
        return json.dumps(self.payload)


def canned(**data):
    """A GraphQL run double answering `{"data": ...}`."""
    return GraphqlRun({"data": data})


def store_in(tmpdir):
    """An open board store inside `tmpdir`, closed by the caller."""
    store = board.open_store(os.path.join(tmpdir, "board.sqlite"))
    return store


class TrackerExtraTest(unittest.TestCase):
    def test_tracker_item_defaults_to_no_extra(self):
        self.assertIsNone(reading().extra)

    def test_fake_readings_compile_without_extra(self):
        found = reading(number=7)
        self.assertIsNone(found.extra)

    def test_rest_read_carries_no_board_concepts(self):
        from tests.test_tracker import item_payload

        run = FakeRun()
        run.add("repos/acme/app/issues/7", item_payload(7))
        found = ghtracker.GithubTracker(run).item("acme/app", 7)
        self.assertEqual(found.extra, {})

    def test_rest_reads_do_not_share_one_extra_dict(self):
        from tests.test_tracker import item_payload

        run = FakeRun()
        run.add("repos/acme/app/issues/7", item_payload(7))
        run.add("repos/acme/app/issues/8", item_payload(8))
        adapter = ghtracker.GithubTracker(run)
        first = adapter.item("acme/app", 7)
        second = adapter.item("acme/app", 8)
        self.assertEqual((first.extra, second.extra), ({}, {}))
        self.assertIsNot(first.extra, second.extra)


class ReadingCopyTest(unittest.TestCase):
    def test_reading_copies_extra(self):
        extra = {"priority": "high", "blocked_by": ["acme/app#1"]}
        found = sync._reading(reading(extra=extra))
        self.assertEqual(found.extra, extra)
        self.assertIsNot(found.extra, extra)

    def test_reading_copy_is_isolated_both_ways(self):
        extra = {"priority": "high"}
        found = sync._reading(reading(extra=extra))
        found.extra["priority"] = "low"
        self.assertEqual(extra, {"priority": "high"})
        other = sync._reading(reading(extra=extra))
        other.extra["pool"] = "dispatch"
        self.assertEqual(extra, {"priority": "high"})

    def test_reading_normalizes_missing_extra(self):
        self.assertEqual(sync._reading(reading()).extra, {})

    def test_reading_normalizes_a_non_dict_extra(self):
        self.assertEqual(sync._reading(reading(extra="junk")).extra, {})


class EffectivePriorityTest(unittest.TestCase):
    def test_board_priority_wins_when_present(self):
        self.assertEqual(board.effective_priority("low", {"priority": "high"}), "high")

    def test_label_rank_stands_without_a_board_key(self):
        self.assertEqual(board.effective_priority("low", {}), "low")
        self.assertEqual(board.effective_priority("low", {"pool": "dispatch"}), "low")

    def test_label_rank_stands_when_the_board_value_is_empty(self):
        self.assertEqual(board.effective_priority("low", {"priority": ""}), "low")
        self.assertEqual(board.effective_priority("low", {"priority": None}), "low")

    def test_missing_or_foreign_extra_keeps_the_label_rank(self):
        self.assertEqual(board.effective_priority("low", None), "low")
        self.assertEqual(board.effective_priority("low", "junk"), "low")

    def test_empty_label_rank_with_board_priority(self):
        self.assertEqual(board.effective_priority("", {"priority": "high"}), "high")

    def test_empty_ranks_stay_empty(self):
        self.assertEqual(board.effective_priority("", {}), "")


class StoreExtraMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-fields-")
        self.addCleanup(self.tmp.cleanup)
        self.store = store_in(self.tmp.name)
        self.addCleanup(self.store.close)

    def stored_extra(self, number=7):
        row, _ = self.store.item("app", number, now=3000.0)
        return row.extra

    def test_insert_persists_extra(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=1000.0,
                extra={"priority": "high", "pool": "dispatch"},
            ),
            operators=["alice"],
            now=1000.0,
        )
        self.assertEqual(self.stored_extra(), {"priority": "high", "pool": "dispatch"})

    def test_empty_reading_preserves_stored_board_extra(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Old",
                tracker_updated_at=1000.0,
                extra={"priority": "high"},
            ),
            operators=["alice", "bob"],
            now=1000.0,
        )
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="New",
                author="alice",
                tracker_updated_at=2000.0,
                extra={},
            ),
            operators=["alice", "bob"],
            now=2000.0,
        )
        row, _ = self.store.item("app", 7, now=2000.0)
        self.assertEqual(row.title, "New")
        self.assertEqual(row.extra, {"priority": "high"})

    def test_none_deletes_a_scalar_key(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=1000.0,
                extra={"priority": "high", "pool": "dispatch"},
            ),
            operators=["alice"],
            now=1000.0,
        )
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=2000.0,
                extra={"priority": None},
            ),
            operators=["alice"],
            now=2000.0,
        )
        self.assertEqual(self.stored_extra(), {"pool": "dispatch"})

    def test_absent_key_keeps_the_stored_value(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=1000.0,
                extra={"priority": "high", "pool": "dispatch"},
            ),
            operators=["alice"],
            now=1000.0,
        )
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=2000.0,
                extra={"pool": "review"},
            ),
            operators=["alice"],
            now=2000.0,
        )
        self.assertEqual(self.stored_extra(), {"priority": "high", "pool": "review"})

    def test_empty_list_clears_a_list_key(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=1000.0,
                extra={"blocked_by": ["acme/app#1"], "pool": "dispatch"},
            ),
            operators=["alice"],
            now=1000.0,
        )
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=2000.0,
                extra={"blocked_by": []},
            ),
            operators=["alice"],
            now=2000.0,
        )
        self.assertEqual(self.stored_extra(), {"pool": "dispatch"})

    def test_non_empty_list_overwrites(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=1000.0,
                extra={"blocked_by": ["acme/app#1"]},
            ),
            operators=["alice"],
            now=1000.0,
        )
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=2000.0,
                extra={"blocked_by": ["acme/app#2"]},
            ),
            operators=["alice"],
            now=2000.0,
        )
        self.assertEqual(self.stored_extra(), {"blocked_by": ["acme/app#2"]})

    def test_cleared_keys_on_insert_are_dropped(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                tracker_updated_at=1000.0,
                extra={"priority": None, "blocked_by": [], "pool": "dispatch"},
            ),
            operators=["alice"],
            now=1000.0,
        )
        self.assertEqual(self.stored_extra(), {"pool": "dispatch"})

    def test_stale_reading_keeps_stored_extra(self):
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Newer",
                tracker_updated_at=2000.0,
                extra={"priority": "high"},
            ),
            operators=["alice"],
            now=2000.0,
        )
        self.store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Older",
                tracker_updated_at=1500.0,
                extra={"priority": "low"},
            ),
            operators=["alice"],
            now=3000.0,
        )
        row, _ = self.store.item("app", 7, now=3000.0)
        self.assertEqual(row.title, "Newer")
        self.assertEqual(row.extra, {"priority": "high"})


class SyncExtraTest(unittest.TestCase):
    REPO = "acme/app"
    T0 = 1_800_000_100.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-fields-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.store = store_in(self.tmp.name)
        self.addCleanup(self.store.close)

    def sync(self, *items, now=None, cursor=None):
        return sync.sync_repo(
            self.store,
            FakeTracker(list(items)),
            self.REPO,
            operators=["alice"],
            now=self.T0 if now is None else now,
            cursor=cursor,
        )

    def stored_extra(self, number):
        row, _ = self.store.item(self.REPO, number, now=self.T0)
        return row.extra

    def test_board_concepts_persist_through_sync(self):
        self.sync(
            reading(
                repo=self.REPO,
                number=1,
                updated_at=self.T0,
                extra={"priority": "urgent", "pool": "dispatch"},
            )
        )
        self.assertEqual(self.stored_extra(1), {"priority": "urgent", "pool": "dispatch"})

    def test_rest_feed_reading_preserves_stored_board_extra(self):
        self.sync(
            reading(
                repo=self.REPO,
                number=1,
                updated_at=self.T0 - 100,
                extra={"priority": "high"},
            ),
            now=self.T0 - 100,
        )
        self.sync(
            reading(repo=self.REPO, number=1, title="Edited", updated_at=self.T0),
            now=self.T0,
        )
        row, _ = self.store.item(self.REPO, 1, now=self.T0)
        self.assertEqual(row.title, "Edited")
        self.assertEqual(row.extra, {"priority": "high"})

    def test_board_reading_with_none_deletes_the_scalar_key(self):
        self.sync(
            reading(
                repo=self.REPO,
                number=1,
                updated_at=self.T0 - 100,
                extra={"priority": "high", "pool": "dispatch"},
            ),
            now=self.T0 - 100,
        )
        self.sync(
            reading(
                repo=self.REPO,
                number=1,
                updated_at=self.T0,
                extra={"priority": None},
            ),
            now=self.T0,
        )
        self.assertEqual(self.stored_extra(1), {"pool": "dispatch"})

    def test_absent_key_survives_a_board_reading(self):
        self.sync(
            reading(
                repo=self.REPO,
                number=1,
                updated_at=self.T0 - 100,
                extra={"priority": "high", "pool": "dispatch"},
            ),
            now=self.T0 - 100,
        )
        self.sync(
            reading(
                repo=self.REPO,
                number=1,
                updated_at=self.T0,
                extra={"pool": "review"},
            ),
            now=self.T0,
        )
        self.assertEqual(self.stored_extra(1), {"priority": "high", "pool": "review"})

    def test_resync_and_audit_carry_extra(self):
        self.sync(
            reading(
                repo=self.REPO,
                number=1,
                updated_at=self.T0,
                extra={"priority": "high"},
            )
        )
        result = sync.audit_repo(
            self.store,
            FakeTracker(
                [
                    reading(
                        repo=self.REPO,
                        number=1,
                        title="Audited",
                        updated_at=self.T0 + 10,
                        extra={"priority": "urgent"},
                    )
                ]
            ),
            self.REPO,
            operators=["alice"],
            now=self.T0 + 10,
            limit=10,
        )
        self.assertEqual(result.items_seen, 1)
        row, _ = self.store.item(self.REPO, 1, now=self.T0 + 10)
        self.assertEqual(row.title, "Audited")
        self.assertEqual(row.extra, {"priority": "urgent"})


class RowPriorityTest(unittest.TestCase):
    def test_board_priority_wins_in_the_policy_row(self):
        from tests.test_eligibility import item as board_item

        from corral import eligibility

        row = eligibility.row_from_item(board_item(priority="low", extra={"priority": "high"}))
        self.assertEqual(row.priority, "high")

    def test_label_priority_stands_without_a_board_key(self):
        from tests.test_eligibility import item as board_item

        from corral import eligibility

        row = eligibility.row_from_item(board_item(priority="low", extra={}))
        self.assertEqual(row.priority, "low")


class GhSeamTest(unittest.TestCase):
    def test_graphql_uses_the_injected_run_and_pacer(self):
        run = canned(user={"login": "acme"})
        pacer = FakePacer()
        found = gh.graphql("{x}", run=run, pacer=pacer, owner="acme")
        self.assertEqual(found, {"user": {"login": "acme"}})
        self.assertEqual(pacer.acquired, [gh.GRAPHQL_CALL_COST])
        self.assertEqual(len(run.calls), 1)
        args, kwargs = run.calls[0]
        self.assertEqual(args[:2], ["api", "graphql"])
        self.assertIn("-f", args)
        self.assertIn("owner=acme", args)
        self.assertTrue(kwargs.get("paced"))

    def test_graphql_mutation_costs_more_than_a_read(self):
        run = canned(addProjectV2ItemById={})
        pacer = FakePacer()
        gh.graphql("mutation($p:ID!){ addProjectV2ItemById }", run=run, pacer=pacer)
        self.assertEqual(pacer.acquired, [gh.GRAPHQL_MUTATION_COST])

    def test_graphql_rate_limit_answer_cools_down_the_injected_pacer(self):
        payload = {
            "data": None,
            "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
        }
        run = GraphqlRun(payload)
        pacer = FakePacer()
        with self.assertRaises(gh.GhError) as caught:
            gh.graphql("{x}", run=run, pacer=pacer)
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertEqual(pacer.cooldowns, [gh.GRAPHQL_COOLDOWN_S])

    def test_graphql_run_failure_cools_down_on_rate_limit_text(self):
        run = GraphqlRun(gh.GhError("unavailable", "API rate limit exceeded"))
        pacer = FakePacer()
        with self.assertRaises(gh.GhError):
            gh.graphql("{x}", run=run, pacer=pacer)
        self.assertEqual(len(pacer.cooldowns), 1)

    def test_graphql_defaults_to_the_real_run_and_pacer(self):
        run = canned(user={"login": "acme"})
        pacer = FakePacer()
        with mock.patch.object(gh, "_pacer", return_value=pacer) as factory:
            found = gh.graphql("{x}", run=run)
        factory.assert_called_once_with()
        self.assertEqual(found, {"user": {"login": "acme"}})
        with mock.patch.object(gh, "run", return_value='{"data": {}}') as real:
            gh.graphql("{x}", pacer=pacer)
        real.assert_called_once()
        self.assertTrue(real.call_args[1].get("paced"))

    def test_paced_run_uses_the_injected_run_and_pacer(self):
        seen = []

        def run(args, **kwargs):
            seen.append((list(args), dict(kwargs)))
            return "ok"

        pacer = FakePacer()
        self.assertEqual(gh.paced_run(["api", "x"], run=run, pacer=pacer), "ok")
        self.assertEqual(pacer.acquired, [gh.REST_READ_COST])
        self.assertEqual(len(seen), 1)
        args, kwargs = seen[0]
        self.assertEqual(args, ["api", "x"])
        self.assertEqual(kwargs.get("timeout"), gh.GH_TIMEOUT_S)
        self.assertTrue(kwargs.get("paced"))

    def test_paced_run_honors_cost_and_timeout(self):
        seen = []

        def run(args, **kwargs):
            seen.append((list(args), dict(kwargs)))
            return "ok"

        pacer = FakePacer()
        gh.paced_run(
            ["api", "x"],
            cost=gh.REST_WRITE_COST,
            timeout=5.0,
            run=run,
            pacer=pacer,
        )
        self.assertEqual(pacer.acquired, [gh.REST_WRITE_COST])
        self.assertEqual(seen[0][1].get("timeout"), 5.0)

    def test_paced_run_rate_limit_cools_down_the_injected_pacer(self):
        def run(args, **kwargs):
            raise gh.GhError("unavailable", "API rate limit exceeded, retry after 62s")

        pacer = FakePacer()
        with self.assertRaises(gh.GhError):
            gh.paced_run(["api", "x"], run=run, pacer=pacer)
        self.assertEqual(pacer.cooldowns, [62.0])

    def test_paced_run_defaults_to_the_real_run_and_pacer(self):
        pacer = FakePacer()
        with mock.patch.object(gh, "_pacer", return_value=pacer) as factory:
            gh.paced_run(["api", "x"], run=lambda args, **kwargs: "ok")
        factory.assert_called_once_with()
        with mock.patch.object(gh, "run", return_value="ok") as real:
            gh.paced_run(["api", "x"], pacer=pacer)
        real.assert_called_once()


class PacerGuardTest(unittest.TestCase):
    """No test reaches the default pacer file or a real subprocess."""

    def test_tripwire_watches_the_real_pacer_factory(self):
        self.assertTrue(getattr(gh._pacer, "_corral_pacer_guard", False))

    def test_injected_calls_touch_neither_the_default_file_nor_a_subprocess(self):
        default_path = _REAL_PACER_FACTORY().path

        def snapshot():
            try:
                with open(default_path, "rb") as handle:
                    return handle.read()
            except FileNotFoundError:
                return None

        before = snapshot()
        run = canned(user={"login": "acme"})
        pacer = FakePacer()
        with mock.patch("subprocess.Popen") as spawn:
            self.assertEqual(gh.graphql("{x}", run=run, pacer=pacer), {"user": {"login": "acme"}})
            self.assertEqual(
                gh.paced_run(["api", "x"], run=lambda args, **kw: "ok", pacer=pacer),
                "ok",
            )
        spawn.assert_not_called()
        self.assertEqual(snapshot(), before)
        self.assertEqual(pacer.acquired, [gh.GRAPHQL_CALL_COST, gh.REST_READ_COST])


if __name__ == "__main__":
    unittest.main()
