"""The drive stuck pass: finished release, restart budget, give-up. Fakes only.

The lock is a real `flock`, but only over files inside temporary
directories; the store is a temporary board store; registry presence
and unit liveness are injected plain data; the stop and relaunch seams
are recording fakes; the tracker is an in-memory double; the restart
budget lives in a temporary state file. No `gh`, no systemd, no
network, no real state dir.
"""

import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace

from corral import board, drive, registry
from corral import tracker as iface
from corral import projects as projects_mod
from corral.tracker import NEEDS_DECISION
from tests.test_drive import policy_source, stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_tracker import FakeTracker, reading

NOW = 20000.0
GONE_S = 3600.0
STALE_S = 600.0
OLD = NOW - 4000.0
OLDER = NOW - 8000.0
YOUNG = NOW - 100.0
AGENT = "drive-host-1-16000-acme--app-7"
HOST = "host-1"


def claimed(store, repo, number, agent=AGENT, *, at=OLD):
    """Claim (`repo`, `number`) for `agent` at `at`; return the claim."""
    return store.claim(repo, number, agent, HOST, now=at)


class FakeStop:
    """A stop-seam double: records tickets, plays failures, returns names."""

    def __init__(self):
        self.calls = []
        self.errors = {}

    def __call__(self, repo, number):
        self.calls.append((repo, number))
        if (repo, number) in self.errors:
            raise self.errors[(repo, number)]
        return [f"stopped-{repo}-{number}"]


def stuck_pass(store, lock_path, out, err, **fields):
    """`drive_pass` with stuck defaults: stale states, fakes, an owner."""
    values = {
        "agent_states": {},
        "stale_s": STALE_S,
        "stop_unit": FakeStop(),
        "restarter": FakeLauncher(),
        "stuck_project": lambda repo, number: "blue",
        "now": NOW,
    }
    values.update(fields)
    return drive_pass(store, lock_path, out, err, **values)


def wants(policy_dir, *tickets):
    """The `found`/`policy_dir` kwargs requesting `tickets` as starts.

    A stuck claim the fleet still wants stays restart-eligible, so
    every full-pass stuck test that expects a restart, a give-up or a
    cap verdict requests its claimed tickets this way, mirroring a
    project policy that keeps asking for its in-flight tickets. `start`
    on a claimed row is a gate no-op ("already claimed"), so the claim
    path never launches under these tests.
    """
    return {
        "found": [("blue", [(repo, number, "start") for repo, number in tickets])],
        "policy_dir": policy_dir,
    }


def live_holder(agent=AGENT, *, seen=OLD):
    """A live registry state for `agent`, last seen at `seen`."""
    return {agent: (registry.LIVE, seen)}


class ClassifyTest(unittest.TestCase):
    def check(self, **over):
        values = {
            "known": True,
            "age": 0.0,
            "unit_live": True,
            "stale_s": STALE_S,
            "gone_s": GONE_S,
        }
        values.update(over)
        return drive.classify_stuck(**values)

    def test_a_fresh_heartbeat_with_a_live_unit_is_healthy(self):
        self.assertEqual(self.check(age=10.0, unit_live=True), "healthy")

    def test_a_stale_heartbeat_with_a_live_unit_watches_once_more(self):
        self.assertEqual(self.check(age=STALE_S + 1.0, unit_live=True), "watch")

    def test_a_gone_heartbeat_restarts_even_with_a_live_unit(self):
        self.assertEqual(self.check(age=GONE_S + 1.0, unit_live=True), "restart")

    def test_a_fresh_heartbeat_with_a_dead_unit_watches_once_more(self):
        self.assertEqual(self.check(age=10.0, unit_live=False), "watch")

    def test_a_stale_heartbeat_with_a_dead_unit_restarts(self):
        self.assertEqual(self.check(age=STALE_S + 1.0, unit_live=False), "restart")

    def test_a_gone_heartbeat_with_a_dead_unit_restarts(self):
        self.assertEqual(self.check(age=GONE_S + 1.0, unit_live=False), "restart")

    def test_an_unknown_agent_restarts_when_its_unit_is_dead(self):
        self.assertEqual(self.check(known=False, age=float("inf"), unit_live=False), "restart")

    def test_an_unknown_agent_with_a_live_unit_watches_once_more(self):
        self.assertEqual(self.check(known=False, age=float("inf"), unit_live=True), "watch")

    def test_boundaries_watch_below_gone_and_restart_at_gone(self):
        self.assertEqual(self.check(age=GONE_S - 1.0, unit_live=True), "watch")
        self.assertEqual(self.check(age=GONE_S, unit_live=True), "restart")
        self.assertEqual(self.check(age=STALE_S, unit_live=False), "restart")


class BudgetTest(unittest.TestCase):
    def test_an_empty_budget_allows_a_restart(self):
        self.assertTrue(drive.restart_allowed([], now=NOW))

    def test_two_recent_restarts_allow_a_third(self):
        stamps = [NOW - 100.0, NOW - 200.0]
        self.assertTrue(drive.restart_allowed(stamps, now=NOW))

    def test_three_recent_restarts_refuse_a_fourth(self):
        stamps = [NOW - 100.0, NOW - 200.0, NOW - 300.0]
        self.assertFalse(drive.restart_allowed(stamps, now=NOW))

    def test_the_window_slides_so_old_stamps_stop_counting(self):
        stamps = [NOW - 100.0, NOW - 200.0, NOW - drive.RESTART_WINDOW_S - 1.0]
        self.assertTrue(drive.restart_allowed(stamps, now=NOW))

    def test_a_stamp_exactly_on_the_window_edge_has_expired(self):
        stamps = [NOW - drive.RESTART_WINDOW_S]
        self.assertTrue(drive.restart_allowed(stamps, now=NOW))

    def test_recording_prunes_expired_stamps_and_appends_now(self):
        stamps = [NOW - drive.RESTART_WINDOW_S - 1.0, NOW - 50.0]
        self.assertEqual(drive.record_restart(stamps, now=NOW), [NOW - 50.0, NOW])

    def test_a_missing_budget_file_reads_empty(self):
        self.assertEqual(drive.load_restarts("/nonexistent/dir/budget.json"), {})

    def test_a_corrupt_budget_file_reads_empty(self):
        with tempfile.TemporaryDirectory(prefix="corral-stuck-budget-") as tmp:
            path = os.path.join(tmp, "budget.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(drive.load_restarts(path), {})

    def test_a_budget_round_trips_through_its_file(self):
        with tempfile.TemporaryDirectory(prefix="corral-stuck-budget-") as tmp:
            path = os.path.join(tmp, "budget.json")
            drive.save_restarts(path, {"app#7": [NOW - 10.0]})
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw, {"app#7": [NOW - 10.0]})
            self.assertEqual(drive.load_restarts(path), {"app#7": [NOW - 10.0]})


class GiveUpBodyTest(unittest.TestCase):
    def test_the_body_names_the_ticket_and_what_was_seen(self):
        body = drive.give_up_body(
            "app", 7, observed="no registry entry and no live unit", restarts=3
        )
        self.assertIn("app#7", body)
        self.assertIn("no registry entry and no live unit", body)
        self.assertIn("3", body)

    def test_the_body_carries_no_agent_id_or_hostname(self):
        body = drive.give_up_body(
            "app", 7, observed="no registry entry and no live unit", restarts=3
        )
        self.assertNotIn(AGENT, body)
        self.assertNotIn(HOST, body)
        self.assertNotIn("drive-", body)


class ReviewedClaimsTest(unittest.TestCase):
    def test_a_finished_reviewer_is_free_for_release(self):
        authors = {("app", 7): AGENT}
        finished = {("app", 7): (AGENT, "reviewer")}
        self.assertEqual(drive.reviewed_claims(authors, finished), {("app", 7): AGENT})

    def test_a_finished_worker_is_kept_for_its_reviewer(self):
        authors = {("app", 7): AGENT}
        for finished in (
            {("app", 7): (AGENT, "worker")},
            {("app", 7): (AGENT, None)},
            {("app", 7): (AGENT, "")},
        ):
            self.assertEqual(drive.reviewed_claims(authors, finished), {})

    def test_a_reviewer_entry_for_another_agent_frees_nothing(self):
        authors = {("app", 7): AGENT}
        finished = {("app", 7): ("drive-host-1-19999-acme--app-7", "reviewer")}
        self.assertEqual(drive.reviewed_claims(authors, finished), {})

    def test_missing_maps_read_as_nothing_reviewed(self):
        self.assertEqual(drive.reviewed_claims(None, None), {})
        self.assertEqual(drive.reviewed_claims({}, {}), {})

    def test_a_live_unit_is_not_freed_for_a_fix_round(self):
        authors = {("app", 7): AGENT}
        finished = {("app", 7): (AGENT, "reviewer")}
        self.assertEqual(
            drive.reviewed_claims(authors, finished, live_units=frozenset({("app", 7)})),
            {},
        )

    def test_a_dead_unit_still_frees_a_finished_reviewer(self):
        authors = {("app", 7): AGENT}
        finished = {("app", 7): (AGENT, "reviewer")}
        self.assertEqual(
            drive.reviewed_claims(authors, finished, live_units=frozenset()),
            {("app", 7): AGENT},
        )


class ReleaseReviewedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-reviewed-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_a_finished_reviewer_is_released_with_a_completion_line(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        out = io.StringIO()
        released = drive.release_reviewed(
            store,
            claims={("app", 7): AGENT},
            live_units=frozenset(),
            now=NOW,
            out=out,
        )
        self.assertEqual(released, {("app", 7): AGENT})
        self.assertIn("app#7", out.getvalue())
        self.assertIn("finished reviewer", out.getvalue())
        self.assertIn(AGENT, out.getvalue())
        self.assertNotIn("released dead claim", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))
        following = store.claim("app", 7, "drive-host-1-20000-acme--app-7", HOST, now=NOW)
        self.assertEqual(following.epoch, made.epoch + 1)

    def test_a_finished_reviewer_with_a_live_unit_is_kept(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        out = io.StringIO()
        released = drive.release_reviewed(
            store,
            claims={("app", 7): AGENT},
            live_units=frozenset({("app", 7)}),
            now=NOW,
            out=out,
        )
        self.assertEqual(released, {})
        self.assertIn("live unit", out.getvalue())
        self.assertIn("claim kept", out.getvalue())
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_race_with_a_reclaim_skips_with_a_logbook_line(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        original = store.release

        def racy(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7) and agent == made.agent:
                original(repo, number, agent, epoch, now=now)
                store.claim(repo, number, "drive-host-1-19999-acme--app-7", HOST, now=now)
            return original(repo, number, agent, epoch, now=now)

        store.release = racy  # type: ignore[method-assign]
        out = io.StringIO()
        try:
            released = drive.release_reviewed(
                store,
                claims={("app", 7): AGENT},
                live_units=frozenset(),
                now=NOW,
                out=out,
            )
        finally:
            store.release = original  # type: ignore[method-assign]
        self.assertEqual(released, {})
        self.assertIn("app#7", out.getvalue())
        self.assertIn("left held", out.getvalue())
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, "drive-host-1-19999-acme--app-7")

    def test_a_store_error_on_one_row_keeps_it_and_releases_the_other(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        other = "drive-host-1-16000-acme--app-8"
        claimed(store, "app", 8, agent=other)
        original = store.release

        def flaky(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7):
                raise board.StoreError("disk away")
            return original(repo, number, agent, epoch, now=now)

        store.release = flaky  # type: ignore[method-assign]
        out = io.StringIO()
        try:
            released = drive.release_reviewed(
                store,
                claims={("app", 7): AGENT, ("app", 8): other},
                live_units=frozenset(),
                now=NOW,
                out=out,
            )
        finally:
            store.release = original  # type: ignore[method-assign]
        self.assertEqual(released, {("app", 8): other})
        text = out.getvalue()
        self.assertIn("app#7: finished release refused (disk away), left held", text)
        self.assertIn("app#8", text)
        self.assertIn("released finished reviewer", text)
        kept = store.claim_of("app", 7)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual((kept.agent, kept.epoch), (made.agent, made.epoch))
        self.assertIsNone(store.claim_of("app", 8))

    def test_a_sqlite_error_on_one_row_keeps_it_and_releases_the_other(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        other = "drive-host-1-16000-acme--app-8"
        claimed(store, "app", 8, agent=other)
        original = store.release

        def flaky(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7):
                raise sqlite3.Error("database is locked")
            return original(repo, number, agent, epoch, now=now)

        store.release = flaky  # type: ignore[method-assign]
        out = io.StringIO()
        try:
            released = drive.release_reviewed(
                store,
                claims={("app", 7): AGENT, ("app", 8): other},
                live_units=frozenset(),
                now=NOW,
                out=out,
            )
        finally:
            store.release = original  # type: ignore[method-assign]
        self.assertEqual(released, {("app", 8): other})
        text = out.getvalue()
        self.assertIn("app#7: finished release refused (database is locked), left held", text)
        self.assertIn("app#8", text)
        self.assertIn("released finished reviewer", text)
        kept = store.claim_of("app", 7)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual((kept.agent, kept.epoch), (made.agent, made.epoch))
        self.assertIsNone(store.claim_of("app", 8))

    def test_live_stale_and_unknown_holders_are_left_alone(self):
        store = stored(
            self.db,
            [("app", 1, "open"), ("app", 2, "open"), ("app", 3, "open"), ("app", 4, "open")],
        )
        self.addCleanup(store.close)
        for number in (1, 2, 3):
            claimed(store, "app", number)
        claimed(store, "app", 4, agent="manual-9")
        states = {
            AGENT: (registry.LIVE, NOW - 10.0),
        }
        self.assertEqual(drive.finished_holders(store, agent_states=states), {})
        for number, want in ((1, AGENT), (2, AGENT), (3, AGENT), (4, "manual-9")):
            row, _ = store.item("app", number, now=NOW)
            self.assertEqual(row.agent, want)

    def test_a_changed_claim_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out = io.StringIO()
        released = drive.release_reviewed(
            store,
            claims={("app", 7): "drive-host-1-19999-acme--app-7"},
            live_units=frozenset(),
            now=NOW,
            out=out,
        )
        self.assertEqual(released, {})
        self.assertIn("left held", out.getvalue())
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, AGENT)


class LiveUnitAgreementTest(unittest.TestCase):
    """Plan and give-up agree with the real pass on live units. Fakes only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-live-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_plan_and_real_agree_on_a_live_finished_reviewer_row(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        kwargs = {
            **wants(self.policies, ("app", 7)),
            "agent_states": {AGENT: (registry.FINISHED, OLD + 10.0)},
            "finished_for": {("app", 7): (AGENT, "reviewer")},
            "live_units": frozenset({("app", 7)}),
            "fake_tracker": FakeTracker([reading(repo="app", number=7, updated_at=19000.0)]),
        }
        plan_out, plan_err = io.StringIO(), io.StringIO()
        plan_code = stuck_pass(store, self.lock, plan_out, plan_err, plan=True, **kwargs)
        real_out, real_err = io.StringIO(), io.StringIO()
        real_code = stuck_pass(
            store, self.lock, real_out, real_err, restarts_path=self.budget, **kwargs
        )
        self.assertEqual(plan_code, 0)
        self.assertEqual(real_code, 0)
        self.assertIn("already claimed", plan_out.getvalue())
        self.assertIn("already claimed", real_out.getvalue())
        self.assertIsNotNone(store.claim_of("app", 7))

    def test_a_live_unit_past_the_budget_keeps_its_claim_without_a_decision_write(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={AGENT: (registry.STALE, OLD)},
            live_units=frozenset({("app", 7)}),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
            fake_tracker=fake,
        )
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("live unit", text)
        self.assertIn("claim kept", text)
        self.assertNotIn("gave up", text)
        self.assertEqual([call for call in fake.calls if call[0] in ("comment", "add_label")], [])
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))


class RestartPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-restart-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_gone_agent_with_a_dead_unit_restarts_reusing_its_agent_id(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, _title, _author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "start"))
        self.assertEqual(agent, AGENT)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))
        self.assertIn("app#7", out.getvalue())
        self.assertIn("restart", out.getvalue())
        self.assertEqual(drive.load_restarts(self.budget), {"app#7": [NOW]})

    def test_a_stale_heartbeat_with_a_live_unit_only_watches(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={AGENT: (registry.STALE, NOW - STALE_S - 1.0)},
            live_units=frozenset({("app", 7)}),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("watching once more", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_a_foreign_holder_is_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7, agent="manual-9")
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, "manual-9")

    def test_a_fresh_claim_is_left_alone_even_when_gone_and_dead(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7, at=NOW)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertNotIn("restart", out.getvalue())

    def test_a_failed_stop_keeps_the_claim_and_records_no_restart(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        stop.errors[("app", 7)] = units.UnitsError("no bus")
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn("claim kept", out.getvalue())
        self.assertEqual(drive.load_restarts(self.budget), {})
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_a_failed_launch_keeps_the_claim_and_records_no_restart(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        launcher.errors[("app", 7)] = units.Unreachable("no bus")
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertIn("claim kept", out.getvalue())
        self.assertEqual(drive.load_restarts(self.budget), {})
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_without_a_stop_seam_a_stuck_row_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=None,
            restarter=FakeLauncher(),
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("left held", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_without_a_known_project_a_stuck_row_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop = FakeStop()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            stuck_project=lambda repo, number: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertIn("left held", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_two_passes_record_two_restarts_inside_the_budget(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7, at=OLD)
        for step in range(2):
            out, err = io.StringIO(), io.StringIO()
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7)),
                agent_states={},
                stop_unit=FakeStop(),
                restarter=FakeLauncher(),
                restarts_path=self.budget,
                now=NOW + step * 10.0,
            )
            self.assertEqual(code, 0)
        stamps = drive.load_restarts(self.budget)["app#7"]
        self.assertEqual(len(stamps), 2)


class GiveUpPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-giveup-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def over_budget(self):
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})

    def test_past_the_budget_the_loop_comments_labels_then_releases(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        self.over_budget()
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
            fake_tracker=fake,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        comments = [call for call in fake.calls if call[0] == "comment"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][:3], ("comment", "app", 7))
        self.assertIn("app#7", comments[0][3])
        self.assertIn(("add_label", "app", 7, NEEDS_DECISION), fake.calls)
        self.assertIn("gave up", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_failed_decision_write_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        self.over_budget()
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        fake.comment_errors[("app", 7)] = iface.TrackerError("offline")
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            fake_tracker=fake,
        )
        self.assertEqual(code, 0)
        self.assertIn("claim kept", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_a_failed_label_write_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        self.over_budget()
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        fake.label_errors[("app", 7)] = iface.TrackerError("offline")
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            fake_tracker=fake,
        )
        self.assertEqual(code, 0)
        self.assertIn("claim kept", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_without_a_tracker_give_up_is_deferred_and_the_claim_is_kept(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        self.over_budget()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            fake_tracker=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("claim kept", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)


class FinishedPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-pass-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_finished_holder_is_kept_for_its_reviewer_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            live_units=frozenset(),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("finished", out.getvalue())
        self.assertIn("claim kept", out.getvalue())
        self.assertNotIn("released dead claim", out.getvalue())
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_withdrawn_finished_holder_is_released_with_the_reason(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 9)),
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            live_units=frozenset(),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("withdrawn by this pass's eligibility", out.getvalue())
        self.assertIn("not restarted", out.getvalue())
        self.assertIn("claim released", out.getvalue())
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_failed_finished_release_still_releases_the_other_finished_claim(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        other = "drive-host-1-16000-acme--app-8"
        claimed(store, "app", 8, agent=other)
        original = store.release

        def flaky(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7):
                raise board.StoreError("disk away")
            return original(repo, number, agent, epoch, now=now)

        store.release = flaky  # type: ignore[method-assign]
        stop, restarter, launcher = FakeStop(), FakeLauncher(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        try:
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7), ("app", 8)),
                agent_states={
                    AGENT: (registry.FINISHED, OLD + 10.0),
                    other: (registry.FINISHED, OLD + 10.0),
                },
                finished_for={
                    ("app", 7): (AGENT, "reviewer"),
                    ("app", 8): (other, "reviewer"),
                },
                live_units=frozenset(),
                restarts_path=self.budget,
                stop_unit=stop,
                restarter=restarter,
                launcher=launcher,
            )
        finally:
            store.release = original  # type: ignore[method-assign]
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("app#7: finished release refused (disk away), left held", text)
        self.assertIn("released finished reviewer", text)
        self.assertIn("finished, not stuck, claim kept", text)
        self.assertNotIn("released dead claim", text)
        self.assertNotIn("restarted", text)
        self.assertEqual(stop.calls, [])
        self.assertEqual(restarter.calls, [])
        self.assertEqual(drive.load_restarts(self.budget), {})
        kept = store.claim_of("app", 7)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual((kept.agent, kept.epoch), (made.agent, made.epoch))
        fresh = store.claim_of("app", 8)
        self.assertIsNotNone(fresh)
        assert fresh is not None
        self.assertNotEqual(fresh.agent, other)

    def test_a_locked_finished_release_still_releases_the_other_finished_claim(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        other = "drive-host-1-16000-acme--app-8"
        claimed(store, "app", 8, agent=other)
        original = store.release

        def flaky(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7):
                raise sqlite3.Error("database is locked")
            return original(repo, number, agent, epoch, now=now)

        store.release = flaky  # type: ignore[method-assign]
        stop, restarter, launcher = FakeStop(), FakeLauncher(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        try:
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7), ("app", 8)),
                agent_states={
                    AGENT: (registry.FINISHED, OLD + 10.0),
                    other: (registry.FINISHED, OLD + 10.0),
                },
                finished_for={
                    ("app", 7): (AGENT, "reviewer"),
                    ("app", 8): (other, "reviewer"),
                },
                live_units=frozenset(),
                restarts_path=self.budget,
                stop_unit=stop,
                restarter=restarter,
                launcher=launcher,
            )
        finally:
            store.release = original  # type: ignore[method-assign]
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("app#7: finished release refused (database is locked), left held", text)
        self.assertIn("released finished reviewer", text)
        self.assertIn("finished, not stuck, claim kept", text)
        self.assertNotIn("released dead claim", text)
        self.assertNotIn("restarted", text)
        self.assertEqual(stop.calls, [])
        self.assertEqual(restarter.calls, [])
        self.assertEqual(drive.load_restarts(self.budget), {})
        kept = store.claim_of("app", 7)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual((kept.agent, kept.epoch), (made.agent, made.epoch))
        fresh = store.claim_of("app", 8)
        self.assertIsNotNone(fresh)
        assert fresh is not None
        self.assertNotEqual(fresh.agent, other)

    def test_a_finished_holder_with_a_live_unit_is_kept_with_the_reason(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 9)),
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            live_units=frozenset({("app", 7)}),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("withdrawn by this pass's eligibility", out.getvalue())
        self.assertIn("live unit", out.getvalue())
        self.assertIn("claim kept", out.getvalue())
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_plan_writes_nothing_including_the_budget_file(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        store.close()
        with open(self.db, "rb") as handle:
            before = handle.read()
        with open(self.budget, "w", encoding="utf-8") as handle:
            handle.write("{}")
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            plan=True,
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        store.close()
        with open(self.db, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)
        with open(self.budget, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{}")


class ProductionStuckProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-project-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ops")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\ntracker.repo = "acme/app"\n')

    def test_the_first_project_mapping_the_repo_wins(self):
        from corral import projects as projects_mod

        blue = projects_mod.Project(
            name="blue", unit_prefix="blue-agent-", root=self.root, eligibility=None
        )
        self.assertEqual(drive.production_stuck_project([blue], "acme/app"), "blue")

    def test_an_unmapped_repo_has_no_project(self):
        from corral import projects as projects_mod

        blue = projects_mod.Project(
            name="blue", unit_prefix="blue-agent-", root=self.root, eligibility=None
        )
        self.assertIsNone(drive.production_stuck_project([blue], "other/repo"))


class ProductionStuckInputsTest(unittest.TestCase):
    def test_live_stale_and_finished_entries_report_with_heartbeats(self):
        from unittest import mock

        from corral import projects as projects_mod

        registered = [
            projects_mod.Project(name="blue", unit_prefix="blue-agent-", root="/tmp/x"),
        ]
        entries = [
            ({"id": "live-1", "last_seen": 100.0}, registry.LIVE),
            ({"id": "stale-1", "last_seen": 50.0}, registry.STALE),
            ({"id": "done-1", "last_seen": 70.0}, registry.FINISHED),
        ]
        with (
            mock.patch.object(
                drive.registry, "config", return_value={"stale_s": STALE_S, "gone_s": GONE_S}
            ),
            mock.patch.object(drive.registry, "visible", return_value=entries),
        ):
            states, stale_s = drive.production_stuck_inputs(registered, now=NOW)
        self.assertEqual(
            states,
            {
                "live-1": (registry.LIVE, 100.0),
                "stale-1": (registry.STALE, 50.0),
                "done-1": (registry.FINISHED, 70.0),
            },
        )
        self.assertEqual(stale_s, STALE_S)


def external_row(store, repo, number):
    """An open row whose author is not an operator: external, read-only."""
    store.upsert_item(
        board.Item(
            repo=repo,
            number=number,
            kind="issue",
            title=f"Title {number}",
            body="Body",
            state="open",
            author="mallory",
            labels=[],
            priority="",
            assignees=[],
            url=f"https://example.com/{repo}/{number}",
            tracker_updated_at=1000.0,
            extra={},
        ),
        operators=["operator"],
        now=1000.0,
    )


class RefreshClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-refresh-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_refresh_advances_claimed_at_and_keeps_agent_and_epoch(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        found = store.refresh_claim("app", 7, AGENT, made.epoch, now=NOW)
        self.assertEqual((found.agent, found.epoch, found.claimed_at), (AGENT, made.epoch, NOW))
        live = store.claim_of("app", 7)
        self.assertIsNotNone(live)
        assert live is not None
        self.assertEqual((live.agent, live.epoch, live.claimed_at), (AGENT, made.epoch, NOW))

    def test_refresh_twice_keeps_the_epoch_stable(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        store.refresh_claim("app", 7, AGENT, made.epoch, now=NOW)
        found = store.refresh_claim("app", 7, AGENT, made.epoch, now=NOW + 10.0)
        self.assertEqual(found.epoch, made.epoch)
        self.assertEqual(found.claimed_at, NOW + 10.0)

    def test_refresh_with_another_agent_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        with self.assertRaises(board.Claimed):
            store.refresh_claim("app", 7, "manual-9", made.epoch, now=NOW)

    def test_refresh_with_another_epoch_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        with self.assertRaises(board.Claimed):
            store.refresh_claim("app", 7, AGENT, made.epoch + 1, now=NOW)

    def test_refresh_with_no_claim_row_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with self.assertRaises(board.Claimed):
            store.refresh_claim("app", 7, AGENT, 1, now=NOW)

    def test_refresh_after_a_release_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        store.release("app", 7, AGENT, made.epoch, now=NOW)
        with self.assertRaises(board.Claimed):
            store.refresh_claim("app", 7, AGENT, made.epoch, now=NOW)

    def test_refresh_of_an_unknown_item_fails(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with self.assertRaises(board.StoreError):
            store.refresh_claim("app", 999, AGENT, 1, now=NOW)

    def test_refresh_of_a_closed_item_fails(self):
        store = stored(self.db, [("app", 7, "closed")])
        self.addCleanup(store.close)
        with self.assertRaises(board.StoreError):
            store.refresh_claim("app", 7, AGENT, 1, now=NOW)

    def test_refresh_of_an_external_item_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        external_row(store, "app", 8)
        with self.assertRaises(board.ExternalItem):
            store.refresh_claim("app", 8, AGENT, 1, now=NOW)


class RefreshRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-refresh-pass-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_repeated_restarts_reach_the_budget_then_give_up_never_orphaned(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        stop, launcher = FakeStop(), FakeLauncher()
        texts = []
        for index, now in enumerate([NOW + step * 3000.0 for step in range(4)]):
            out, err = io.StringIO(), io.StringIO()
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7)),
                agent_states={},
                stop_unit=stop,
                restarter=launcher,
                restarts_path=self.budget,
                fake_tracker=fake,
                now=now,
            )
            self.assertEqual(code, 0)
            texts.append(out.getvalue())
            if index < 3:
                found = store.claim_of("app", 7)
                self.assertIsNotNone(found)
                assert found is not None
                self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))
        self.assertEqual(len(launcher.calls), 3)
        for text in texts:
            self.assertNotIn("released dead claim", text)
        self.assertIn("gave up", texts[-1])
        self.assertIsNone(store.claim_of("app", 7))
        comments = [call for call in fake.calls if call[0] == "comment"]
        self.assertEqual(len(comments), 1)
        self.assertIn(("add_label", "app", 7, NEEDS_DECISION), fake.calls)

    def test_failing_launches_keep_the_claim_without_orphaning_or_spending(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = units.Unreachable("no bus")
        stop = FakeStop()
        for now in (NOW, NOW + 3000.0, NOW + 8000.0):
            out, err = io.StringIO(), io.StringIO()
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7)),
                agent_states={},
                stop_unit=stop,
                restarter=launcher,
                restarts_path=self.budget,
                now=now,
            )
            self.assertEqual(code, 0)
            self.assertIn("claim kept", out.getvalue())
            self.assertNotIn("released dead claim", out.getvalue())
            found = store.claim_of("app", 7)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))
        self.assertEqual(drive.load_restarts(self.budget), {})

    def test_past_the_budget_give_up_needs_no_working_launcher(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = units.Unreachable("no bus")
        stop = FakeStop()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
            fake_tracker=fake,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("gave up", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))


class BudgetSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-budget-save-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_failing_save_aborts_the_restart_and_keeps_the_claim(self):
        from unittest import mock

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(drive, "save_restarts", side_effect=OSError("disk gone")):
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7)),
                agent_states={},
                stop_unit=stop,
                restarter=launcher,
                restarts_path=self.budget,
            )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("claim kept", out.getvalue())
        self.assertIn("restart budget", err.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)
        self.assertEqual(drive.load_restarts(self.budget), {})

    def test_a_failed_launch_rolls_the_stamp_back(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = units.Unreachable("no bus")
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("claim kept", out.getvalue())
        self.assertEqual(drive.load_restarts(self.budget), {})
        launcher.errors.pop(("app", 7))
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=launcher,
            restarts_path=self.budget,
            now=NOW + 10.0,
        )
        self.assertEqual(code, 0)
        stamps = drive.load_restarts(self.budget)["app#7"]
        self.assertEqual(len(stamps), 1)

    def test_a_failed_stop_rolls_the_stamp_back(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop = FakeStop()
        stop.errors[("app", 7)] = units.UnitsError("no bus")
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=FakeLauncher(),
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("claim kept", out.getvalue())
        self.assertEqual(drive.load_restarts(self.budget), {})

    def test_a_failed_rollback_save_restores_the_prior_file(self):
        from unittest import mock

        from corral import units

        prior = {"other#9": [NOW - 500.0]}
        drive.save_restarts(self.budget, prior)
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop = FakeStop()
        stop.errors[("app", 7)] = units.UnitsError("no bus")
        real_save = drive.save_restarts
        calls = []

        def flaky_save(path, budgets):
            calls.append(dict(budgets))
            if len(calls) == 1:
                return real_save(path, budgets)
            raise OSError("disk gone")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(drive, "save_restarts", side_effect=flaky_save):
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7)),
                agent_states={},
                stop_unit=stop,
                restarter=FakeLauncher(),
                restarts_path=self.budget,
            )
        self.assertEqual(code, 0)
        self.assertIn("claim kept", out.getvalue())
        self.assertIn("restart budget", err.getvalue())
        self.assertEqual(drive.load_restarts(self.budget), prior)
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_a_failed_rollback_restore_fails_the_restart_closed(self):
        from unittest import mock

        from corral import units

        prior = {"other#9": [NOW - 500.0]}
        drive.save_restarts(self.budget, prior)
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop = FakeStop()
        stop.errors[("app", 7)] = units.UnitsError("no bus")
        real_save = drive.save_restarts
        save_calls = []

        def flaky_save(path, budgets):
            save_calls.append(dict(budgets))
            if len(save_calls) == 1:
                return real_save(path, budgets)
            raise OSError("disk gone")

        real_atomic = drive.paths.atomic_write_text
        atomic_calls = []

        def flaky_atomic(target, text):
            atomic_calls.append(target)
            if len(atomic_calls) == 1:
                return real_atomic(target, text)
            raise OSError("disk gone")

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(drive, "save_restarts", side_effect=flaky_save),
            mock.patch.object(drive.paths, "atomic_write_text", side_effect=flaky_atomic),
        ):
            code = stuck_pass(
                store,
                self.lock,
                out,
                err,
                **wants(self.policies, ("app", 7)),
                agent_states={},
                stop_unit=stop,
                restarter=FakeLauncher(),
                restarts_path=self.budget,
            )
        self.assertEqual(code, 0)
        self.assertIn("claim kept", out.getvalue())
        self.assertIn("cannot restore the restart budget", err.getvalue())
        kept = drive.load_restarts(self.budget)
        self.assertEqual(kept["other#9"], prior["other#9"])
        self.assertEqual(len(kept["app#7"]), 1)
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)


class GiveUpOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-giveup-order-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_give_up(self, store, fake):
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            fake_tracker=fake,
        )
        return code, out.getvalue()

    def test_the_label_goes_first_then_the_comment(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        code, text = self.run_give_up(store, fake)
        self.assertEqual(code, 0)
        self.assertIn("gave up", text)
        writes = [call for call in fake.calls if call[0] in ("add_label", "comment")]
        self.assertEqual([call[0] for call in writes], ["add_label", "comment"])

    def test_a_label_failure_posts_no_comment_and_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        fake.label_errors[("app", 7)] = iface.TrackerError("offline")
        code, text = self.run_give_up(store, fake)
        self.assertEqual(code, 0)
        self.assertIn("claim kept", text)
        self.assertEqual([call for call in fake.calls if call[0] == "comment"], [])
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_a_comment_failure_retries_without_posting_twice(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        fake.comment_errors[("app", 7)] = iface.TrackerError("offline")
        code, text = self.run_give_up(store, fake)
        self.assertEqual(code, 0)
        self.assertIn("claim kept", text)
        labels = [call for call in fake.calls if call[0] == "add_label"]
        self.assertEqual(labels, [("add_label", "app", 7, NEEDS_DECISION)])
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)
        del fake.comment_errors[("app", 7)]
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            **wants(self.policies, ("app", 7)),
            fake_tracker=fake,
            now=NOW + 10.0,
        )
        self.assertEqual(code, 0)
        self.assertIn("gave up", out.getvalue())
        comments = [call for call in fake.calls if call[0] == "comment"]
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0][3], comments[1][3])
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_repo_the_factory_does_not_know_defers_give_up_and_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)

        def missing(repo):
            raise KeyError(repo)

        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            **wants(self.policies, ("app", 7)),
            fake_tracker=None,
            tracker_factory=missing,
            now=NOW + 10.0,
        )
        self.assertEqual(code, 3)
        self.assertIn("no tracker for this repo", err.getvalue())
        self.assertIn("no tracker for this repo", out.getvalue())
        self.assertIn("claim kept", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)


class StuckErrorBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-branches-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_stuck(self, store, **over):
        values = {
            "agent_states": {},
            "stop_unit": FakeStop(),
            "restarter": FakeLauncher(),
            "restarts_path": self.budget,
            **wants(self.policies, ("app", 7), ("app", 9)),
        }
        values.update(over)
        stop, launcher = values["stop_unit"], values["restarter"]
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(store, self.lock, out, err, **values)
        return code, out.getvalue(), stop, launcher

    def test_a_refresh_race_leaves_the_row_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        store.refresh_claim = lambda *args, **kwargs: (_ for _ in ()).throw(
            board.Claimed("item app#7 is claimed by drive-host-1-19999-acme--app-7")
        )
        code, text, stop, launcher = self.run_stuck(store)
        self.assertEqual(code, 0)
        self.assertIn("restart raced a re-claim, left held", text)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_a_refresh_refusal_leaves_the_row_held(self):
        for problem in (
            board.ExternalItem("item app#7 is external: read-only"),
            board.StoreError("item app#7 is not open"),
        ):
            store = stored(self.db, [("app", 7, "open")])
            self.addCleanup(store.close)
            claimed(store, "app", 7)

            def refused(*args, problem=problem, **kwargs):
                raise problem

            store.refresh_claim = refused
            code, text, stop, launcher = self.run_stuck(store)
            self.assertEqual(code, 0)
            self.assertIn("restart claim refused", text)
            self.assertEqual(stop.calls, [])
            self.assertEqual(launcher.calls, [])
            row, _ = store.item("app", 7, now=NOW)
            self.assertEqual(row.agent, AGENT)

    def test_a_refresh_sqlite_error_leaves_the_row_held(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        claimed(store, "app", 9)
        original_refresh = store.refresh_claim

        def flaky_refresh(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7):
                raise sqlite3.OperationalError("database is locked")
            return original_refresh(repo, number, agent, epoch, now=now)

        store.refresh_claim = flaky_refresh
        try:
            code, text, stop, launcher = self.run_stuck(store)
        finally:
            store.refresh_claim = original_refresh
        self.assertEqual(code, 0)
        self.assertIn("restart claim refused", text)
        self.assertIn("database is locked", text)
        self.assertEqual(stop.calls, [("app", 9)])
        self.assertEqual(len(launcher.calls), 1)
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_a_give_up_release_race_leaves_the_row_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        original = store.release

        def racy(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7) and agent == made.agent:
                original(repo, number, agent, epoch, now=now)
                store.claim(repo, number, "drive-host-1-19999-acme--app-7", HOST, now=now)
            return original(repo, number, agent, epoch, now=now)

        store.release = racy  # type: ignore[method-assign]
        code, text, _stop, _launcher = self.run_stuck(store, fake_tracker=fake)
        self.assertEqual(code, 0)
        self.assertIn("give-up release raced a re-claim, left held", text)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, "drive-host-1-19999-acme--app-7")

    def sweep(self, store, **over):
        values = {
            "agent_states": {},
            "stale_s": STALE_S,
            "gone_s": GONE_S,
            "live_units": frozenset(),
            "now": NOW,
            "host": HOST,
            "stuck_project": lambda repo, number: "blue",
            "stop_unit": FakeStop(),
            "restarter": FakeLauncher(),
            "tracker_factory": None,
            "restarts_path": self.budget,
            "read_labels": lambda kind, repo, number: [],
        }
        values.update(over)
        stop, launcher = values["stop_unit"], values["restarter"]
        out, err = io.StringIO(), io.StringIO()
        result = drive.handle_stuck(store, out=out, err=err, **values)
        return result, out.getvalue(), err.getvalue(), stop, launcher

    def test_a_transient_lock_on_the_row_listing_retries_the_sweep(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        original_items = store.items
        calls = []

        def flaky_items(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return original_items(*args, **kwargs)

        store.items = flaky_items
        try:
            (restarted, capped), text, _, stop, launcher = self.sweep(store)
        finally:
            store.items = original_items
        self.assertEqual((restarted, capped), (1, False))
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)
        self.assertIn("restarted", text)

    def test_a_persistent_lock_on_the_row_listing_skips_the_sweep_with_claims_kept(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        original_items = store.items

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        store.items = locked
        try:
            with self.assertRaises(sqlite3.Error):
                self.sweep(store)
        finally:
            store.items = original_items
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_corrupt_row_listing_raises_at_once_without_a_retry(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        original_items = store.items
        calls = []

        def corrupt(*args, **kwargs):
            calls.append(1)
            raise board.StoreError("unusable row app#7")

        store.items = corrupt
        try:
            with self.assertRaises(board.StoreError):
                self.sweep(store)
        finally:
            store.items = original_items
        self.assertEqual(len(calls), 1)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_an_unreadable_claim_leaves_that_row_held_and_restarts_the_rest(self):
        for index, problem in enumerate(
            (
                sqlite3.OperationalError("database is locked"),
                board.StoreError("unusable item app#7"),
            )
        ):
            db = os.path.join(self.tmp.name, f"board-unreadable-{index}.sqlite")
            store = stored(db, [("app", 7, "open"), ("app", 9, "open")])
            self.addCleanup(store.close)
            claimed(store, "app", 7)
            claimed(store, "app", 9, agent="drive-host-1-16000-acme--app-9")
            original_claim_of = store.claim_of

            def flaky_claim_of(repo, number, problem=problem):
                if (repo, number) == ("app", 7):
                    raise problem
                return original_claim_of(repo, number)

            store.claim_of = flaky_claim_of
            try:
                (restarted, capped), text, _, stop, launcher = self.sweep(store)
            finally:
                store.claim_of = original_claim_of
            self.assertEqual((restarted, capped), (1, False))
            self.assertIn("app#7", text)
            self.assertIn("left held", text)
            self.assertEqual(stop.calls, [("app", 9)])
            self.assertEqual(len(launcher.calls), 1)
            row, _ = store.item("app", 7, now=NOW)
            self.assertEqual(row.agent, AGENT)


class StuckJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-journal-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.journal = os.path.join(self.tmp.name, "drive", "journal.jsonl")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def journal_entries(self):
        with open(self.journal, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().splitlines() if line.strip()]

    def test_a_restart_is_journaled_and_pass_end_counts_no_give_up(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            journal_path=self.journal,
        )
        self.assertEqual(code, 0)
        entries = self.journal_entries()
        restarts = [entry for entry in entries if entry["event"] == "restart"]
        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0]["ticket"], "app#7")
        self.assertEqual(restarts[0]["agent"], AGENT)
        self.assertEqual(entries[-1]["event"], "pass_end")
        self.assertEqual(entries[-1]["gave_up"], 0)

    def test_a_give_up_is_journaled_and_counted_in_pass_end(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=FakeStop(),
            restarter=FakeLauncher(),
            restarts_path=self.budget,
            fake_tracker=fake,
            journal_path=self.journal,
        )
        self.assertEqual(code, 0)
        self.assertIn("gave up", out.getvalue())
        entries = self.journal_entries()
        give_ups = [entry for entry in entries if entry["event"] == "give_up"]
        self.assertEqual(len(give_ups), 1)
        self.assertEqual(give_ups[0]["ticket"], "app#7")
        self.assertEqual(entries[-1]["event"], "pass_end")
        self.assertEqual(entries[-1]["gave_up"], 1)


class CapLabelTest(unittest.TestCase):
    def test_a_configured_cap_names_its_limit(self):
        self.assertEqual(drive.cap_label(50), "(50/day)")

    def test_a_missing_cap_never_prints_none(self):
        self.assertEqual(drive.cap_label(None), "(day cap)")


class StuckDayCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-daycap-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.day = os.path.join(self.tmp.name, "starts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def day_starts(self):
        with open(self.day, encoding="utf-8") as handle:
            return json.load(handle)["starts"]

    def test_a_stuck_restart_spends_one_from_the_day_cap(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        drive.write_day_count(self.day, drive._day_string(NOW), 0)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            **wants(self.policies, ("app", 7)),
            restarts_path=self.budget,
            day_path=self.day,
            max_starts=1,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(self.day_starts(), 1)

    def test_a_stuck_restart_at_the_cap_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        drive.write_day_count(self.day, drive._day_string(NOW), 1)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            **wants(self.policies, ("app", 7)),
            restarts_path=self.budget,
            day_path=self.day,
            max_starts=1,
        )
        self.assertEqual(code, 1)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("day cap", out.getvalue())
        self.assertEqual(self.day_starts(), 1)
        self.assertEqual(drive.load_restarts(self.budget), {})
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_fresh_start_fills_the_cap_so_the_stuck_row_is_left_held(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 9, agent="drive-host-1-16000-acme--app-9")
        drive.write_day_count(self.day, drive._day_string(NOW), 0)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        stop, launcher, restarter = FakeStop(), FakeLauncher(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "start"), ("app", 9, "start")])],
            policy_dir=self.policies,
            fake_tracker=fake,
            launcher=launcher,
            agent_states={},
            stop_unit=stop,
            restarter=restarter,
            restarts_path=self.budget,
            day_path=self.day,
            max_starts=1,
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][1:4], ("app", 7, "start"))
        self.assertEqual(restarter.calls, [])
        self.assertEqual(stop.calls, [])
        self.assertIn("day cap", out.getvalue())
        self.assertEqual(self.day_starts(), 1)
        found = store.claim_of("app", 9)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_give_up_past_the_restart_budget_still_releases_at_the_cap(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        drive.write_day_count(self.day, drive._day_string(NOW), 1)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=19000.0)])
        stop, restarter = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=stop,
            restarter=restarter,
            **wants(self.policies, ("app", 7)),
            restarts_path=self.budget,
            fake_tracker=fake,
            day_path=self.day,
            max_starts=1,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(restarter.calls, [])
        self.assertIn("gave up", out.getvalue())
        self.assertNotIn("day cap", out.getvalue())
        self.assertIn(("add_label", "app", 7, NEEDS_DECISION), fake.calls)
        self.assertTrue(any(call[0] == "comment" for call in fake.calls))
        self.assertIsNone(store.claim_of("app", 7))
        self.assertEqual(self.day_starts(), 1)

    def test_one_free_slot_restarts_the_first_row_and_caps_the_second(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        claimed(store, "app", 8, agent="drive-host-1-16000-acme--app-8")
        drive.write_day_count(self.day, drive._day_string(NOW), 0)
        stop, restarter = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={},
            stop_unit=stop,
            restarter=restarter,
            **wants(self.policies, ("app", 7), ("app", 8)),
            restarts_path=self.budget,
            day_path=self.day,
            max_starts=1,
        )
        self.assertEqual(code, 1)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(restarter.calls), 1)
        self.assertEqual(restarter.calls[0][1:4], ("app", 7, "start"))
        self.assertIn("day cap", out.getvalue())
        self.assertEqual(self.day_starts(), 1)


class HubEpochStuckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-hub-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_hub_stuck(self, store, *, hub_epoch_of, **fields):
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
            hub_epoch_of=hub_epoch_of,
            **fields,
        )
        return code, stop, launcher, out.getvalue()

    def test_a_superseded_epoch_skips_instead_of_restarting(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        code, stop, launcher, logged = self.run_hub_stuck(
            store, hub_epoch_of=lambda repo, number: made.epoch + 1
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("supersedes", logged)
        self.assertIn("app#7", logged)
        self.assertEqual(drive.load_restarts(self.budget), {})
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))

    def test_a_matching_epoch_restarts_as_today(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        code, stop, launcher, logged = self.run_hub_stuck(
            store, hub_epoch_of=lambda repo, number: made.epoch
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)
        self.assertIn("restart", logged)

    def test_an_unknown_hub_row_restarts_as_today(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        code, stop, launcher, _ = self.run_hub_stuck(store, hub_epoch_of=lambda repo, number: None)
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)

    def test_an_unreadable_hub_epoch_skips_without_stopping(self):
        def broken(repo, number):
            raise RuntimeError("hub away")

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        code, stop, launcher, logged = self.run_hub_stuck(store, hub_epoch_of=broken)
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("unreadable", logged)
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, made.epoch)

    def test_a_superseded_epoch_skips_a_would_be_give_up_without_posting(self):
        from tests.test_tracker import FakeTracker

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 10.0, NOW - 20.0, NOW - 30.0]})
        tracker = FakeTracker()
        code, stop, launcher, logged = self.run_hub_stuck(
            store,
            hub_epoch_of=lambda repo, number: made.epoch + 1,
            fake_tracker=tracker,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("supersedes", logged)
        self.assertNotIn("gave up", logged)
        self.assertEqual(
            [call for call in tracker.calls if call[0] in ("add_label", "comment")], []
        )

    def test_without_the_seam_a_stale_ticket_restarts_as_today(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            **wants(self.policies, ("app", 7)),
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)


class DropStaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drop-stale-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_stop_drop_and_log_without_touching_anyone_else(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        other = claimed(store, "app", 8)
        stop = FakeStop()
        out = io.StringIO()
        self.assertTrue(drive.drop_stale_claim(store, "app", 7, stop_unit=stop, out=out, now=NOW))
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertIsNone(store.claim_of("app", 7))
        kept = store.claim_of("app", 8)
        assert kept is not None
        self.assertEqual((kept.agent, kept.epoch), (other.agent, other.epoch))
        self.assertIn("app#7", out.getvalue())
        self.assertIn(str(made.epoch), out.getvalue())

    def test_no_local_claim_is_a_quiet_success_without_stopping(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        stop = FakeStop()
        out = io.StringIO()
        self.assertTrue(drive.drop_stale_claim(store, "app", 7, stop_unit=stop, out=out, now=NOW))
        self.assertEqual(stop.calls, [])
        self.assertIn("nothing to drop", out.getvalue())

    def test_a_failed_stop_keeps_the_claim(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop = FakeStop()
        stop.errors[("app", 7)] = units.Unreachable("unit away")
        out = io.StringIO()
        self.assertFalse(drive.drop_stale_claim(store, "app", 7, stop_unit=stop, out=out, now=NOW))
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))
        self.assertIn("claim kept", out.getvalue())

    def test_a_release_race_is_left_held(self):
        from unittest import mock

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop = FakeStop()

        def raced(repo, number, agent, epoch, *, now):
            raise board.Claimed("item app#7 is held by somebody-else")

        out = io.StringIO()
        with mock.patch.object(store, "release", side_effect=raced):
            self.assertFalse(
                drive.drop_stale_claim(store, "app", 7, stop_unit=stop, out=out, now=NOW)
            )
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertIn("left held", out.getvalue())

    def test_a_store_error_on_release_is_left_held(self):
        from unittest import mock

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop = FakeStop()

        def broken(repo, number, agent, epoch, *, now):
            raise board.StoreError("unknown item app#7")

        out = io.StringIO()
        with mock.patch.object(store, "release", side_effect=broken):
            self.assertFalse(
                drive.drop_stale_claim(store, "app", 7, stop_unit=stop, out=out, now=NOW)
            )
        self.assertEqual(stop.calls, [("app", 7)])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))
        self.assertIn("left held", out.getvalue())

    def test_a_write_error_on_release_is_left_held(self):
        from unittest import mock

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop = FakeStop()

        def broken(repo, number, agent, epoch, *, now):
            raise sqlite3.Error("disk write failed")

        out = io.StringIO()
        with mock.patch.object(store, "release", side_effect=broken):
            self.assertFalse(
                drive.drop_stale_claim(store, "app", 7, stop_unit=stop, out=out, now=NOW)
            )
        self.assertEqual(stop.calls, [("app", 7)])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))
        self.assertIn("left held", out.getvalue())


class RestartEligibilityTest(unittest.TestCase):
    """The stuck sweep consults row eligibility before restarting. Fakes only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-restart-eligibility-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.budget = os.path.join(self.tmp.name, "restarts.json")

    def sweep(self, store, **over):
        values = {
            "agent_states": {},
            "stale_s": STALE_S,
            "gone_s": GONE_S,
            "live_units": frozenset(),
            "now": NOW,
            "host": HOST,
            "stuck_project": lambda repo, number: "blue",
            "stop_unit": FakeStop(),
            "restarter": FakeLauncher(),
            "tracker_factory": None,
            "restarts_path": self.budget,
            "read_labels": lambda kind, repo, number: [],
            "hold_label": "hold",
            "requested": {("app", 7)},
        }
        values.update(over)
        stop, launcher = values["stop_unit"], values["restarter"]
        out, err = io.StringIO(), io.StringIO()
        result = drive.handle_stuck(store, out=out, err=err, **values)
        return result, out.getvalue(), err.getvalue(), stop, launcher

    def test_a_held_ticket_is_released_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        (restarted, capped), text, _, stop, launcher = self.sweep(
            store, read_labels=lambda kind, repo, number: ["hold"]
        )
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("held for a human", text)
        self.assertIn("not restarted", text)
        self.assertIn("claim released", text)
        self.assertIsNone(store.claim_of("app", 7))
        self.assertEqual(drive.load_restarts(self.budget), {})

    def test_every_refusal_branch_is_greppable_as_not_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        row, _ = store.item("app", 7, now=NOW)

        def failing(kind, repo, number):
            raise RuntimeError("labels away")

        cases = [
            drive.restart_refusal(
                replace(row, state="closed"),
                read_labels=lambda kind, repo, number: [],
                hold_label="hold",
                requested={("app", 7)},
            ),
            drive.restart_refusal(
                replace(row, external=True),
                read_labels=lambda kind, repo, number: [],
                hold_label="hold",
                requested={("app", 7)},
            ),
            drive.restart_refusal(row, read_labels=None, hold_label="hold", requested={("app", 7)}),
            drive.restart_refusal(
                row, read_labels=failing, hold_label="hold", requested={("app", 7)}
            ),
            drive.restart_refusal(
                row,
                read_labels=lambda kind, repo, number: ["hold"],
                hold_label="hold",
                requested={("app", 7)},
            ),
            drive.restart_refusal(
                replace(row, labels=[NEEDS_DECISION]),
                read_labels=lambda kind, repo, number: [],
                hold_label="hold",
                requested={("app", 7)},
            ),
            drive.restart_refusal(
                row,
                read_labels=lambda kind, repo, number: [],
                hold_label="hold",
                requested=frozenset(),
            ),
        ]
        self.assertEqual(len(cases), 7)
        for refusal in cases:
            self.assertIsNotNone(refusal)
            assert refusal is not None
            reason, _keep = refusal
            self.assertIn("app#7", reason)
            self.assertIn("not restarted", reason)

    def test_keep_branches_carry_the_greppable_marker_word_for_word(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        row, _ = store.item("app", 7, now=NOW)
        refusal = drive.restart_refusal(
            row, read_labels=None, hold_label="hold", requested={("app", 7)}
        )
        self.assertIsNotNone(refusal)
        assert refusal is not None
        self.assertEqual(
            refusal,
            (
                "app#7: hold labels unreadable (no label reader), restart deferred, not restarted",
                True,
            ),
        )

        def failing(kind, repo, number):
            raise RuntimeError("labels away")

        refusal = drive.restart_refusal(
            row, read_labels=failing, hold_label="hold", requested={("app", 7)}
        )
        self.assertIsNotNone(refusal)
        assert refusal is not None
        self.assertEqual(
            refusal,
            (
                "app#7: hold unreadable on app#7 (labels away): "
                "no resume until the labels read again, "
                "restart deferred, not restarted",
                True,
            ),
        )

    def test_a_finished_holder_is_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        (restarted, capped), text, _, stop, launcher = self.sweep(
            store,
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
        )
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("finished, not stuck, claim kept", text)
        self.assertNotIn("restarted", text)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_closed_ticket_is_released_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        row, _ = store.item("app", 7, now=NOW)
        store.upsert_item(replace(row, state="closed"), operators=["operator"], now=NOW)
        (restarted, capped), text, _, stop, launcher = self.sweep(store)
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("not open", text)
        self.assertIn("not restarted", text)
        self.assertIn("claim released", text)
        self.assertIsNone(store.claim_of("app", 7))
        self.assertEqual(drive.load_restarts(self.budget), {})

    def test_a_withdrawn_ticket_is_released_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        (restarted, capped), text, _, stop, launcher = self.sweep(store, requested=frozenset())
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("withdrawn by this pass's eligibility", text)
        self.assertIn("not restarted", text)
        self.assertIn("claim released", text)
        self.assertIsNone(store.claim_of("app", 7))
        self.assertEqual(drive.load_restarts(self.budget), {})

    def test_a_ticket_waiting_on_a_human_is_released_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        row, _ = store.item("app", 7, now=NOW)
        store.upsert_item(replace(row, labels=[NEEDS_DECISION]), operators=["operator"], now=NOW)
        (restarted, capped), text, _, stop, launcher = self.sweep(store)
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("waiting on a human", text)
        self.assertIn("not restarted", text)
        self.assertIn("claim released", text)
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_requested_ticket_still_restarts(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        (restarted, capped), text, _, stop, launcher = self.sweep(store)
        self.assertEqual((restarted, capped), (1, False))
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)
        self.assertIn("restarted", text)

    def test_a_restart_names_the_stop_verb_for_a_lasting_stop(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        (_, _), text, _, _, _ = self.sweep(store)
        self.assertIn("app#7", text)
        self.assertIn("restarted", text)
        self.assertIn("corral stop", text)

    def test_unreadable_labels_keep_the_claim_without_a_restart(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)

        def failing(kind, repo, number):
            raise RuntimeError("labels away")

        (restarted, capped), text, _, stop, launcher = self.sweep(store, read_labels=failing)
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("unreadable", text)
        self.assertIn("claim kept", text)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, AGENT)

    def test_a_release_race_on_an_ineligible_row_is_left_held(self):
        from unittest import mock

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)

        def raced(repo, number, agent, epoch, *, now):
            raise board.Claimed("item app#7 is held by somebody-else")

        with mock.patch.object(store, "release", side_effect=raced):
            (restarted, capped), text, _, stop, launcher = self.sweep(store, requested=frozenset())
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("withdrawn", text)
        self.assertIn("left held", text)

    def test_a_hold_on_a_linked_pull_is_released_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        row, _ = store.item("app", 7, now=NOW)

        def labels(kind, repo, number):
            if kind == "pr":
                return ["hold"]
            return []

        refusal = drive.restart_refusal(
            row,
            read_labels=labels,
            hold_label="hold",
            requested={("app", 7)},
            linked=[("app", 8)],
        )
        self.assertIsNotNone(refusal)
        assert refusal is not None
        reason, keep = refusal
        self.assertFalse(keep)
        self.assertIn("app#7", reason)
        self.assertIn("held for a human", reason)
        self.assertIn("not restarted", reason)

    def test_an_externally_owned_row_is_released_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        row, _ = store.item("app", 7, now=NOW)
        store.upsert_item(replace(row, author="mallory"), operators=["operator"], now=NOW)
        (restarted, capped), text, _, stop, launcher = self.sweep(store)
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("externally owned", text)
        self.assertIn("not restarted", text)
        self.assertIn("claim released", text)
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_misshapen_label_payload_keeps_the_claim_without_a_restart(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        (restarted, capped), text, _, stop, launcher = self.sweep(
            store, read_labels=lambda kind, repo, number: "hold"
        )
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("unreadable", text)
        self.assertIn("claim kept", text)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, AGENT)

    def test_a_release_store_error_on_an_ineligible_row_is_left_held(self):
        from unittest import mock

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)

        def broken(repo, number, agent, epoch, *, now):
            raise board.StoreError("unknown item app#7")

        with mock.patch.object(store, "release", side_effect=broken):
            (restarted, capped), text, _, stop, launcher = self.sweep(store, requested=frozenset())
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("withdrawn", text)
        self.assertIn("release refused", text)
        self.assertIn("left held", text)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))

    def test_without_a_label_reader_the_restart_is_deferred_and_the_claim_is_kept(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        (restarted, capped), text, _, stop, launcher = self.sweep(store, read_labels=None)
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("unreadable", text)
        self.assertIn("claim kept", text)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, AGENT)

    def test_a_held_row_past_its_budget_is_released_with_no_give_up_write(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stamps = [NOW - 100.0, NOW - 200.0, NOW - 300.0]
        drive.save_restarts(self.budget, {"app#7": stamps})
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        (restarted, capped), text, _, stop, launcher = self.sweep(
            store,
            read_labels=lambda kind, repo, number: ["hold"],
            tracker_factory=lambda repo: fake,
        )
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("app#7", text)
        self.assertIn("held for a human", text)
        self.assertIn("claim released", text)
        self.assertNotIn("gave up", text)
        self.assertNotIn(NEEDS_DECISION, text)
        self.assertEqual(fake.calls, [])
        self.assertEqual(drive.load_restarts(self.budget), {"app#7": stamps})
        self.assertIsNone(store.claim_of("app", 7))


class FinishedWorkerReviewTest(unittest.TestCase):
    """A finished worker without a PR is reviewed, never restarted. Fakes only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-finished-review-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_finished_worker_without_a_pr_is_reviewed_never_restarted(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher, stop = FakeLauncher(), FakeStop()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "review")])],
            policy_dir=self.policies,
            fake_tracker=fake,
            launcher=launcher,
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            live_units=frozenset(),
            stop_unit=stop,
            restarter=FakeLauncher(),
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "review")
        self.assertEqual(launcher.calls[0][6], AGENT)
        self.assertNotEqual(launcher.calls[0][4], AGENT)
        self.assertEqual(stop.calls, [])
        self.assertIn("takes over", out.getvalue())
        self.assertNotIn("restart", out.getvalue())
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, launcher.calls[0][4])
        self.assertEqual(found.epoch, made.epoch + 1)


class BasicInFlightTest(unittest.TestCase):
    """The ready-made basic policy keeps requesting in-flight tickets. Fakes only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-basic-in-flight-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.project = projects_mod.Project(
            name="blue",
            unit_prefix="blue-agent-",
            root=os.path.join(self.tmp.name, "ops", "blue"),
            eligibility="corral.policies:basic",
        )

    def test_basic_keeps_a_stuck_in_flight_ticket_restart_eligible(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[self.project],
            agent_states={},
            live_units=frozenset(),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("withdrawn by this pass's eligibility", out.getvalue())
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, _title, _author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "start"))
        self.assertEqual(agent, AGENT)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (AGENT, made.epoch))

    def test_basic_never_withdraws_a_live_in_flight_ticket(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[self.project],
            agent_states={},
            live_units=frozenset({("app", 7)}),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("withdrawn by this pass's eligibility", out.getvalue())
        self.assertIn("already claimed", out.getvalue())
        self.assertNotIn("claim released", out.getvalue())
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_basic_reviews_a_finished_worker_instead_of_restarting_it(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[self.project],
            launcher=launcher,
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            live_units=frozenset(),
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "review")
        self.assertEqual(launcher.calls[0][6], AGENT)
        self.assertIn("takes over", out.getvalue())
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, launcher.calls[0][4])

    def test_a_held_in_flight_ticket_is_withdrawn_but_kept_while_its_unit_is_live(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[self.project],
            read_labels=lambda kind, repo, number: ["hold"],
            agent_states={AGENT: (registry.LIVE, NOW - GONE_S - 1.0)},
            live_units=frozenset({("app", 7)}),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("held for a human", out.getvalue())
        self.assertIn("not restarted", out.getvalue())
        self.assertIn("live unit", out.getvalue())
        self.assertIn("claim kept", out.getvalue())
        self.assertNotIn("claim released", out.getvalue())
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_held_in_flight_ticket_is_released_once_its_unit_is_dead(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[self.project],
            read_labels=lambda kind, repo, number: ["hold"],
            agent_states={},
            live_units=frozenset(),
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("held for a human", out.getvalue())
        self.assertIn("not restarted", out.getvalue())
        self.assertIn("claim released", out.getvalue())
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_full_pass_releases_a_withdrawn_stuck_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 9, "start")])],
            policy_dir=self.policies,
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("withdrawn by this pass's eligibility", out.getvalue())
        self.assertIn("claim released", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))


if __name__ == "__main__":
    unittest.main()
