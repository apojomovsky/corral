"""The drive claims pass: claim through the store, launch through a seam. Fakes only.

The lock is a real `flock`, but only over files inside temporary
directories; the store is a temporary board store; policies are stub
modules written to a temporary directory; the tracker is an in-memory
double; the launcher is a recording fake standing in for the existing
launch path. No `gh`, no systemd, no network, no real state dir.
"""

import io
import os
import tempfile
import unittest

from corral import admission, board, drive, sync, tracker
from corral import projects as projects_mod
from tests.test_drive import policy_source, stored
from tests.test_tracker import FakeTracker, reading


class FakeLauncher:
    """A launch-path double: records every handoff, plays failures per ticket."""

    def __init__(self):
        self.calls = []
        self.errors = {}

    def __call__(self, project, repo, number, kind, agent, title, *, now):
        self.calls.append((project, repo, number, kind, agent, title))
        if (repo, number) in self.errors:
            raise self.errors[(repo, number)]
        return f"{agent}-unit"


def drive_pass(
    store,
    lock_path,
    out,
    err,
    *,
    plan=False,
    found=(),
    policy_dir=None,
    fake_tracker=None,
    launcher=None,
    now=2000.0,
    stale_after=3600.0,
    operators=("alice", "operator"),
    **fields,
):
    """`run_pass` with every host read injected: no outside world."""
    if policy_dir is not None and found:
        entries = []
        for name, wants in found:
            path = os.path.join(policy_dir, name + ".py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(policy_source(wants))
            entries.append(
                projects_mod.Project(
                    name=name,
                    unit_prefix=name + "-agent",
                    root=os.path.join(policy_dir, "ops", name),
                    eligibility=path + ":eligible",
                )
            )
        found = entries
    values = {
        "plan": plan,
        "store": store,
        "lock_path": lock_path,
        "out": out,
        "err": err,
        "now": now,
        "projects_fn": lambda: list(found),
        "live": frozenset(),
        "limits": admission.Limits(total=4),
        "quotas": {},
        "ledger": {},
        "read_labels": lambda kind, repo, number: [],
        "hold_label": "hold",
        "tracker": fake_tracker,
        "operators": list(operators),
        "launcher": launcher,
        "host": "host-1",
        "stale_after": stale_after,
    }
    values.update(fields)
    return drive.run_pass(**values)


class MintTest(unittest.TestCase):
    def test_minted_id_carries_host_seconds_repo_and_number(self):
        self.assertEqual(
            drive.mint_agent("acme/app", 7, host="host-1", now=2000.0),
            "drive-host-1-2000-acme--app-7",
        )

    def test_minted_id_has_the_managed_prefix_and_no_slash(self):
        minted = drive.mint_agent("acme/app", 7, host="host-1", now=2000.0)
        self.assertTrue(minted.startswith(drive.MANAGED_PREFIX))
        self.assertNotIn("/", minted)
        self.assertTrue(drive.is_managed_agent(minted))

    def test_minted_id_sanitizes_a_host_slash(self):
        minted = drive.mint_agent("app", 1, host="a/b", now=1.0)
        self.assertNotIn("/", minted)
        self.assertTrue(minted.startswith("drive-"))

    def test_minted_ids_differ_per_ticket(self):
        first = drive.mint_agent("app", 7, host="host-1", now=2000.0)
        second = drive.mint_agent("app", 9, host="host-1", now=2000.0)
        self.assertNotEqual(first, second)


class ClaimLaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-claims-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_claims(self, store, found, fake_tracker, launcher, **fields):
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            found=found,
            policy_dir=self.policies,
            fake_tracker=fake_tracker,
            launcher=launcher,
            **fields,
        )
        return code, out.getvalue(), err.getvalue()

    def test_claim_then_launch_hands_the_minted_id_to_the_launcher(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, title="Title 7", updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, err = self.run_claims(store, [("blue", [("app", 7, "start")])], fake, launcher)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, title = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "start"))
        self.assertTrue(agent.startswith("drive-"))
        self.assertNotIn("/", agent)
        self.assertEqual(title, "Title 7")
        row, _ = store.item("app", 7, now=2000.0)
        self.assertEqual(row.agent, agent)
        self.assertIn("started", out)
        self.assertTrue(out.splitlines()[-1].startswith("pass:"))
        self.assertIn("started 1", out.splitlines()[-1])

    def test_mirror_drains_the_claim_write_on_the_healthy_pass(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, _, _ = self.run_claims(store, [("blue", [("app", 7, "start")])], fake, launcher)
        self.assertEqual(code, 0)
        self.assertEqual(store.queued_writes(), [])
        self.assertIn(("write", "app", 7, "agent", launcher.calls[0][4]), fake.calls)

    def test_plan_with_a_tracker_and_launcher_writes_nothing(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.enqueue("app", 7, "status", "working", now=1500.0)
        store.close()
        with open(self.db, "rb") as handle:
            before = handle.read()
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=True,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            fake_tracker=fake,
            launcher=launcher,
        )
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertEqual(launcher.calls, [])
        store.close()
        with open(self.db, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


class RaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-race-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_lost_claim_race_is_recorded_and_the_pass_continues(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        armed = {"rival": True}
        original = store.claim

        def racy(repo, number, agent, host, *, now):
            if armed["rival"] and (repo, number) == ("app", 7):
                armed["rival"] = False
                original(repo, number, "drive-rival-1", "host-9", now=now)
            return original(repo, number, agent, host, now=now)

        store.claim = racy
        fake = FakeTracker(
            [
                reading(repo="app", number=7, updated_at=1900.0),
                reading(repo="app", number=9, updated_at=1900.0),
            ]
        )
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "start"), ("app", 9, "start")])],
            policy_dir=self.policies,
            fake_tracker=fake,
            launcher=launcher,
        )
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("race lost to `drive-rival-1`", out.getvalue())
        started = {(repo, number) for _, repo, number, _, _, _ in launcher.calls}
        self.assertEqual(started, {("app", 9)})


class LaunchFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-launchfail-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_live_unit_race_is_recorded_and_the_claim_is_kept(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = units.UnitExists("app-7 already live")
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
        )
        self.assertEqual(code, 0)
        self.assertIn("already live", out.getvalue())
        row, _ = store.item("app", 7, now=2000.0)
        self.assertTrue(row.agent.startswith("drive-"))

    def test_an_unreachable_service_manager_exits_three_when_nothing_starts(self):
        from corral import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = units.Unreachable("no bus")
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
        )
        self.assertEqual(code, 3)
        self.assertIn("no bus", out.getvalue())


class ProductionLauncherTest(unittest.TestCase):
    def test_non_start_kinds_stay_with_a_later_ticket(self):
        from corral import launch

        launcher = drive.production_launcher([])
        with self.assertRaises(launch.LaunchError) as ctx:
            launcher("blue", "app", 7, "resume", "drive-x", "Title", now=2000.0)
        self.assertIn("later ticket", str(ctx.exception))

    def test_unknown_project_is_a_launch_error(self):
        from corral import launch

        launcher = drive.production_launcher([])
        with self.assertRaises(launch.LaunchError) as ctx:
            launcher("blue", "app", 7, "start", "drive-x", "Title", now=2000.0)
        self.assertIn("blue", str(ctx.exception))


class OutageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-outage-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_sync_outage_keeps_the_cursor_and_continues_on_store_data(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.set_cursor("app", sync.CURSOR_NAME, "OLD-CURSOR")
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        fake.changes_error = "connection refused"
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
        )
        self.assertEqual(code, 3)
        self.assertEqual(store.get_cursor("app", sync.CURSOR_NAME), "OLD-CURSOR")
        row, _ = store.item("app", 7, now=2000.0)
        self.assertEqual(row.title, "Title 7")
        self.assertEqual(len(launcher.calls), 1)
        self.assertIn("connection refused", err.getvalue())

    def test_failed_freshness_reread_fails_the_claim_closed(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.set_cursor("app", sync.CURSOR_NAME, tracker.format_cursor(1900.0))
        store.enqueue("app", 7, "status", "working", now=1500.0)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1500.0)])
        fake.changes_error = "connection refused"
        fake.item_errors[("app", 7)] = "gone from the tracker"
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
            stale_after=300.0,
        )
        self.assertEqual(code, 3)
        self.assertEqual(launcher.calls, [])
        self.assertIn("tracker unreachable", out.getvalue())
        self.assertIn("stale", out.getvalue())
        row, _ = store.item("app", 7, now=2000.0)
        self.assertEqual(row.agent, "")
        writes = [call for call in fake.calls if call[0] == "write"]
        self.assertEqual(len(writes), 1)

    def test_stale_row_with_a_healthy_reread_claims_and_starts(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.set_cursor("app", sync.CURSOR_NAME, tracker.format_cursor(1900.0))
        fake = FakeTracker([reading(repo="app", number=7, title="Fresh Title", updated_at=1500.0)])
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
            stale_after=300.0,
        )
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][5], "Fresh Title")


class MirrorBacklogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-backlog-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_failed_writes_stay_queued_and_drain_on_the_next_healthy_pass(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        failing = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        failing.write_errors[("app", 7)] = tracker.TrackerError("connection refused")
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            fake_tracker=failing,
            launcher=launcher,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(len(store.queued_writes()), 1)
        healthy = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher2 = FakeLauncher()
        out2, err2 = io.StringIO(), io.StringIO()
        code2 = drive_pass(
            store,
            self.lock,
            out2,
            err2,
            now=2100.0,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            fake_tracker=healthy,
            launcher=launcher2,
        )
        self.assertEqual(code2, 0)
        self.assertEqual(launcher2.calls, [])
        self.assertEqual(store.queued_writes(), [])
        self.assertIn("already claimed", out2.getvalue())


if __name__ == "__main__":
    unittest.main()
