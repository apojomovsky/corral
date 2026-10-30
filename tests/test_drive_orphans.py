"""The drive orphan recovery pass: release provably-dead claims. Fakes only.

The store is a temporary board store; registry presence and live units
are injected plain sets, never the real registry or service manager;
the tracker is an in-memory double; the launcher is a recording fake.
No `gh`, no systemd, no network, no real state dir.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from corral import board, drive, registry, settings, units
from corral import projects as projects_mod
from tests.test_drive import stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_tracker import FakeTracker, reading

NOW = 20000.0
GONE_S = 3600.0
OLD = NOW - 8000.0
YOUNG = NOW - 1000.0
AGENT = "drive-host-1-12000-acme--app-7"
HOST = "host-1"


def claimed(store, repo, number, agent=AGENT, *, at=OLD):
    """Claim (`repo`, `number`) for `agent` at `at`; return the claim."""
    return store.claim(repo, number, agent, HOST, now=at)


def orphan_kwargs(**over):
    """Hermetic orphan inputs for `drive_pass`: nothing present, nothing live."""
    values = {
        "present_agents": frozenset(),
        "live_units": frozenset(),
        "gone_s": GONE_S,
    }
    values.update(over)
    return values


class ClaimOfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-orphan-claimof-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_unclaimed_row_has_no_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        self.assertIsNone(store.claim_of("app", 7))

    def test_claimed_row_reports_agent_and_epoch(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch, found.claimed_at), (made.agent, 1, OLD))

    def test_released_row_has_no_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        store.release("app", 7, made.agent, made.epoch, now=NOW)
        self.assertIsNone(store.claim_of("app", 7))


class IsOrphanTest(unittest.TestCase):
    def dead(self, **over):
        params = {
            "repo": "app",
            "number": 7,
            "agent": AGENT,
            "host": HOST,
            "epoch": 1,
            "claimed_at": OLD,
        }
        params.update(over)
        return board.Claim(**params)  # type: ignore[arg-type]

    def check(self, claim, **over):
        values = {
            "now": NOW,
            "gone_s": GONE_S,
            "present_agents": frozenset(),
            "live_units": frozenset(),
        }
        values.update(over)
        return drive.is_orphan(claim, **values)  # type: ignore[arg-type]

    def test_a_dead_managed_claim_is_an_orphan(self):
        self.assertTrue(self.check(self.dead()))

    def test_no_claim_row_is_not_an_orphan(self):
        self.assertFalse(self.check(None))

    def test_a_present_agent_is_left_alone(self):
        self.assertFalse(self.check(self.dead(), present_agents=frozenset({AGENT})))

    def test_a_live_unit_is_left_alone(self):
        self.assertFalse(self.check(self.dead(), live_units=frozenset({("app", 7)})))

    def test_a_live_unit_for_another_ticket_does_not_cover(self):
        self.assertTrue(self.check(self.dead(), live_units=frozenset({("app", 9)})))

    def test_a_young_claim_is_left_alone(self):
        self.assertFalse(self.check(self.dead(claimed_at=YOUNG)))

    def test_a_claim_exactly_twice_gone_old_is_left_alone(self):
        self.assertFalse(self.check(self.dead(claimed_at=NOW - 2 * GONE_S)))

    def test_a_claim_just_past_twice_gone_is_an_orphan(self):
        self.assertTrue(self.check(self.dead(claimed_at=NOW - 2 * GONE_S - 1.0)))

    def test_a_foreign_holder_is_never_an_orphan(self):
        self.assertFalse(self.check(self.dead(agent="manual-9")))


class ReleaseOrphansTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-orphans-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def recover(self, store, **over):
        out = io.StringIO()
        values = orphan_kwargs()
        values.update(over)
        released = drive.release_orphans(store, now=NOW, out=out, **values)  # type: ignore[arg-type]
        return released, out.getvalue()

    def test_release_clears_the_holder_and_logs(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        released, logged = self.recover(store)
        self.assertEqual(released, 1)
        self.assertIn("app#7", logged)
        self.assertIn("released dead claim", logged)
        self.assertIsNone(store.claim_of("app", 7))

    def test_release_keeps_the_epoch_for_the_next_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        self.recover(store)
        next_claim = store.claim("app", 7, "drive-host-1-20000-acme--app-7", HOST, now=NOW)
        self.assertEqual(next_claim.epoch, made.epoch + 1)
        with self.assertRaises(board.Claimed):
            store.release("app", 7, made.agent, made.epoch, now=NOW)

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
        released, logged = self.recover(store)
        self.assertEqual(released, 0)
        self.assertIn("app#7", logged)
        self.assertIn("raced a re-claim", logged)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, "drive-host-1-19999-acme--app-7")

    def test_uncertain_rows_are_left_alone_without_lines(self):
        store = stored(
            self.db,
            [
                ("app", 1, "open"),
                ("app", 2, "open"),
                ("app", 3, "open"),
                ("app", 4, "open"),
                ("app", 5, "open"),
            ],
        )
        self.addCleanup(store.close)
        claimed(store, "app", 1)
        claimed(store, "app", 2)
        claimed(store, "app", 3, at=YOUNG)
        claimed(store, "app", 4, agent="manual-9")
        released, logged = self.recover(
            store,
            present_agents=frozenset({AGENT}),
            live_units=frozenset({("app", 2)}),
        )
        self.assertEqual(released, 0)
        self.assertEqual(logged, "")
        for number, want in ((1, AGENT), (2, AGENT), (3, AGENT), (4, "manual-9"), (5, "")):
            row, _ = store.item("app", number, now=NOW)
            self.assertEqual(row.agent, want)


class ProductionOrphanInputsTest(unittest.TestCase):
    def test_live_and_stale_entries_count_as_present(self):
        registered = [
            projects_mod.Project(name="blue", unit_prefix="blue-agent-", root="/tmp/ops-blue"),
            projects_mod.Project(name="green", unit_prefix="green-agent-", root="/tmp/ops-green"),
        ]
        entries = [
            ({"id": "live-1"}, registry.LIVE),
            ({"id": "stale-1"}, registry.STALE),
            ({"id": "finished-1"}, registry.FINISHED),
            ({"id": "gone-1"}, registry.GONE),
        ]
        listings = {
            "blue-agent-": [
                ("blue-agent-app-7", "active", ""),
                ("blue-agent-app-8", "inactive", ""),
            ],
            "green-agent-": [("green-agent-app-9", "active", "")],
        }
        with (
            mock.patch.object(drive.registry, "config", return_value={"gone_s": GONE_S}),
            mock.patch.object(drive.registry, "visible", return_value=entries),
            mock.patch.object(
                drive.units,
                "list_units",
                side_effect=lambda prefix, run=None: listings[prefix],
            ),
        ):
            present, tickets, gone_s = drive.production_orphan_inputs(registered, now=NOW)
        self.assertEqual(present, {"live-1", "stale-1"})
        self.assertEqual(tickets, {("app", 7), ("app", 9)})
        self.assertEqual(gone_s, GONE_S)


class OrphanPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-orphan-pass-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_the_pass_releases_the_orphan_with_a_logbook_line(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(store, self.lock, out, err, now=NOW, **orphan_kwargs())
        self.assertEqual(code, 0)
        self.assertIn("released dead claim", out.getvalue())
        self.assertIsNone(store.claim_of("app", 7))

    def test_the_pass_leaves_a_present_holder_claimed(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store, self.lock, out, err, now=NOW, **orphan_kwargs(present_agents=frozenset({AGENT}))
        )
        self.assertEqual(code, 0)
        self.assertNotIn("released dead claim", out.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)

    def test_plan_releases_nothing(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        store.close()
        with open(self.db, "rb") as handle:
            before = handle.read()
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(store, self.lock, out, err, plan=True, now=NOW, **orphan_kwargs())
        self.assertEqual(code, 0)
        self.assertNotIn("released dead claim", out.getvalue())
        store.close()
        with open(self.db, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_an_unreachable_unit_listing_skips_recovery_and_continues(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker(
            [
                reading(repo="app", number=7, updated_at=19000.0),
                reading(repo="app", number=9, updated_at=19000.0),
            ]
        )
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            drive, "production_orphan_inputs", side_effect=units.Unreachable("no bus")
        ):
            code = drive_pass(
                store,
                self.lock,
                out,
                err,
                found=[("blue", [("app", 9, "start")])],
                policy_dir=self.policies,
                fake_tracker=fake,
                launcher=launcher,
                now=NOW,
                gone_s=None,
            )
        self.assertEqual(code, 0)
        self.assertIn("orphan recovery skipped", err.getvalue())
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, AGENT)
        self.assertEqual(len(launcher.calls), 1)

    def test_bad_registry_settings_yields_exit_4(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            drive,
            "production_orphan_inputs",
            side_effect=settings.SettingsError("bad registry"),
        ):
            code = drive_pass(store, self.lock, out, err, now=NOW, gone_s=None)
        self.assertEqual(code, 4)
        self.assertIn("bad registry", err.getvalue())

    def test_a_store_error_skips_recovery_and_keeps_rows_held(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        claimed(store, "app", 9)

        def failing(repo, number, agent, epoch, *, now):
            raise board.StoreError("boom")

        store.release = failing  # type: ignore[method-assign]
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(store, self.lock, out, err, now=NOW, **orphan_kwargs())
        self.assertEqual(code, 0)
        self.assertIn("orphan recovery skipped", err.getvalue())
        for number in (7, 9):
            row, _ = store.item("app", number, now=NOW)
            self.assertEqual(row.agent, AGENT)

    def test_the_pass_never_adopts_a_tracker_shown_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker(
            [reading(repo="app", number=7, agent="drive-host-9-0-acme--app-7", updated_at=19000.0)]
        )
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            fake_tracker=fake,
            launcher=launcher,
            now=NOW,
            **orphan_kwargs(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        row, _ = store.item("app", 7, now=NOW)
        self.assertTrue(row.agent.startswith("drive-host-1-"))
        self.assertNotIn("drive-host-9-", row.agent)


if __name__ == "__main__":
    unittest.main()
