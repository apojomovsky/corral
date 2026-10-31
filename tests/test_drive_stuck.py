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

from corral import board, drive, registry
from corral import tracker as iface
from corral.github.tracker import NEEDS_DECISION
from tests.test_drive import stored
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

    def test_an_unknown_agent_restarts_even_with_a_live_unit(self):
        self.assertEqual(self.check(known=False, age=float("inf"), unit_live=True), "restart")

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


class ReleaseFinishedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-finished-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_a_finished_holder_is_released_with_a_completion_line(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        out = io.StringIO()
        states = {AGENT: (registry.FINISHED, NOW - 10.0)}
        released = drive.release_finished(store, agent_states=states, now=NOW, out=out)
        self.assertEqual(released, 1)
        self.assertIn("app#7", out.getvalue())
        self.assertIn("finished worker", out.getvalue())
        self.assertIn(AGENT, out.getvalue())
        self.assertNotIn("released dead claim", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_release_keeps_the_epoch_for_the_next_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        out = io.StringIO()
        states = {AGENT: (registry.FINISHED, NOW - 10.0)}
        drive.release_finished(store, agent_states=states, now=NOW, out=out)
        following = store.claim("app", 7, "drive-host-1-20000-acme--app-7", HOST, now=NOW)
        self.assertEqual(following.epoch, made.epoch + 1)

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
        states = {AGENT: (registry.FINISHED, NOW - 10.0)}
        out = io.StringIO()
        released = drive.release_finished(store, agent_states=states, now=NOW, out=out)
        self.assertEqual(released, 0)
        self.assertIn("app#7", out.getvalue())
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, "drive-host-1-19999-acme--app-7")

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
        out = io.StringIO()
        released = drive.release_finished(store, agent_states=states, now=NOW, out=out)
        self.assertEqual(released, 0)
        self.assertEqual(out.getvalue(), "")
        for number, want in ((1, AGENT), (2, AGENT), (3, AGENT), (4, "manual-9")):
            row, _ = store.item("app", number, now=NOW)
            self.assertEqual(row.agent, want)


class RestartPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-restart-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")

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
            agent_states={},
            stop_unit=stop,
            restarter=launcher,
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, _title = launcher.calls[0]
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

    def test_a_finished_holder_takes_the_completion_path_never_the_orphan_one(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            live_units=frozenset(),
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("finished worker", out.getvalue())
        self.assertNotIn("released dead claim", out.getvalue())
        self.assertNotIn("restart", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_finished_holder_is_released_even_with_a_live_unit(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out, err = io.StringIO(), io.StringIO()
        code = stuck_pass(
            store,
            self.lock,
            out,
            err,
            agent_states={AGENT: (registry.FINISHED, OLD + 10.0)},
            live_units=frozenset({("app", 7)}),
            restarts_path=self.budget,
        )
        self.assertEqual(code, 0)
        self.assertIn("finished worker", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))

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

    def run_give_up(self, store, fake):
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

    def run_stuck(self, store, **over):
        values = {
            "agent_states": {},
            "stop_unit": FakeStop(),
            "restarter": FakeLauncher(),
            "restarts_path": self.budget,
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


class StuckJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-stuck-journal-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.budget = os.path.join(self.tmp.name, "restarts.json")
        self.journal = os.path.join(self.tmp.name, "drive", "journal.jsonl")

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


if __name__ == "__main__":
    unittest.main()
