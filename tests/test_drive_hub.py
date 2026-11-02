"""The drive hub-mode pass: hub claim first, reconcile, hub-down, single-host. Fakes only.

The hub client is a recording double: scripted receipts, conflicts and
outages, never a socket. The lock is a real `flock`, but only over
files inside temporary directories; the store is a temporary board
store; policies are stub modules written to a temporary directory; the
tracker is an in-memory double; the launcher and stop seam are
recording fakes. No `gh`, no systemd, no network, no real state dir.
"""

import argparse
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from corral import board, drive, drivehub, hub, registry, settings
from tests.test_drive import policy_source, stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_drive_stuck import AGENT, FakeStop, claimed
from tests.test_tracker import FakeTracker, reading

NOW = 2000.0
HOST = "host-1"
LEASE = 90.0


def hub_row(repo, number, agent, *, host_id=HOST, epoch=1, lease=NOW + LEASE):
    """One hub claims-table row, as `HubClient.claims` returns it."""
    return {
        "repo": repo,
        "number": number,
        "host_id": host_id,
        "agent": agent,
        "epoch": epoch,
        "lease_expires": lease,
        "updated_at": NOW - 10.0,
    }


class FakeHub:
    """A hub-client double: scripted receipts, conflicts and outages.

    `rows` is the `claims()` table; `receipts` maps (repo, number) to
    the epoch propose and renew grant; `conflicts` maps (repo, number)
    to the (agent, host_id, epoch) holder the call refuses with;
    `unreachable` makes every call raise `HubUnreachable`. Every call
    is recorded in `calls`, and `seen` pairs each call with the local
    agent `peek` reported at that moment, so tests can assert the hub
    was proposed before the local row was claimed.
    """

    def __init__(self, *, host_id=HOST):
        self.host_id = host_id
        self.calls = []
        self.seen = []
        self.rows = []
        self.receipts = {}
        self.conflicts = {}
        self.unreachable = False
        self.peek = lambda repo, number: None
        self.releases = []
        self.release_errors = {}

    def _check(self, op, repo, number, agent):
        try:
            local = self.peek(repo, number)
        except Exception:
            local = "unreadable"
        self.seen.append((op, repo, number, local))
        self.calls.append((op, repo, number, agent))
        if self.unreachable:
            raise hub.HubUnreachable("hub away")
        if (repo, number) in self.conflicts:
            holder = self.conflicts[(repo, number)]
            raise hub.Conflict(repo, number, *holder)

    def propose(self, repo, number, agent):
        self._check("propose", repo, number, agent)
        epoch = self.receipts.get((repo, number), 1)
        return hub.Receipt(repo, number, agent, self.host_id, epoch, NOW + LEASE)

    def renew(self, repo, number, agent, epoch):
        self._check("renew", repo, number, agent)
        kept = self.receipts.get((repo, number), epoch)
        return hub.Receipt(repo, number, agent, self.host_id, kept, NOW + LEASE)

    def release(self, repo, number, agent, epoch):
        self._check("release", repo, number, agent)
        self.releases.append((repo, number, agent, epoch))
        if (repo, number) in self.release_errors:
            raise self.release_errors[(repo, number)]
        return epoch

    def claims(self):
        self.calls.append(("claims",))
        if self.unreachable:
            raise hub.HubUnreachable("hub away")
        return [dict(row) for row in self.rows]


def hub_pass(
    store,
    lock_path,
    out,
    err,
    found,
    fake_tracker,
    launcher,
    fake_hub,
    policy_dir,
    **fields,
):
    """`drive_pass` with the hub conversation injected."""
    return drive_pass(
        store,
        lock_path,
        out,
        err,
        found=found,
        policy_dir=policy_dir,
        fake_tracker=fake_tracker,
        launcher=launcher,
        hub_client=fake_hub,
        hub_host_id=HOST,
        **fields,
    )


class HubComposeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-hub-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_hub_grant_is_adopted_locally_and_mirrored(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        hub_fake = FakeHub()
        hub_fake.receipts[("app", 7)] = 5
        hub_fake.peek = lambda repo, number: (
            found.agent if (found := store.claim_of(repo, number)) else None
        )
        out, err = io.StringIO(), io.StringIO()
        code = hub_pass(
            store,
            self.lock,
            out,
            err,
            [("blue", [("app", 7, "start")])],
            fake,
            launcher,
            hub_fake,
            self.policies,
        )
        self.assertEqual(code, 0)
        proposes = [call for call in hub_fake.calls if call[0] == "propose"]
        self.assertEqual(len(proposes), 1)
        agent = proposes[0][3]
        seen_propose = [seen for seen in hub_fake.seen if seen[0] == "propose"]
        self.assertEqual(seen_propose, [("propose", "app", 7, None)])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (agent, 5))
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(
            [call for call in fake.calls if call[0] == "write"],
            [("write", "app", 7, "agent", agent)],
        )

    def test_hub_conflict_skips_and_moves_to_the_next_candidate(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker(
            [
                reading(repo="app", number=7, updated_at=1900.0),
                reading(repo="app", number=8, updated_at=1900.0),
            ]
        )
        launcher = FakeLauncher()
        hub_fake = FakeHub()
        hub_fake.conflicts[("app", 7)] = ("drive-other", "host-2", 3)
        out, err = io.StringIO(), io.StringIO()
        code = hub_pass(
            store,
            self.lock,
            out,
            err,
            [("blue", [("app", 7, "start"), ("app", 8, "start")])],
            fake,
            launcher,
            hub_fake,
            self.policies,
        )
        self.assertEqual(code, 0)
        self.assertIsNone(store.claim_of("app", 7))
        self.assertIsNotNone(store.claim_of("app", 8))
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][1:3], ("app", 8))
        self.assertIn("race lost", out.getvalue())
        self.assertIn("app#7", out.getvalue())

    def test_local_race_after_a_hub_grant_releases_the_hub_row(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        holder = claimed(store, "app", 7, at=1000.0)
        hub_fake = FakeHub()
        hub_fake.receipts[("app", 7)] = 4
        mode = drivehub.HubMode(hub_fake, HOST)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="start",
            verdict="start",
            reason="app#7: unclaimed, start slot",
            code="",
        )
        agent, skipped = drive._mint_claim(store, gated, host=HOST, now=NOW, hub=mode)
        self.assertIsNone(agent)
        assert skipped is not None
        self.assertEqual(skipped.code, "claim")
        self.assertIn("race lost", skipped.reason)
        proposes = [call for call in hub_fake.calls if call[0] == "propose"]
        self.assertEqual(len(proposes), 1)
        self.assertEqual(hub_fake.releases, [("app", 7, proposes[0][3], 4)])
        kept = store.claim_of("app", 7)
        assert kept is not None
        self.assertEqual((kept.agent, kept.epoch), (holder.agent, holder.epoch))

    def test_local_race_releases_best_effort_when_the_hub_is_gone(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7, at=1000.0)
        hub_fake = FakeHub()
        hub_fake.receipts[("app", 7)] = 4
        hub_fake.release_errors[("app", 7)] = drive.HubGone("hub away")
        mode = drivehub.HubMode(hub_fake, HOST)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="start",
            verdict="start",
            reason="app#7: unclaimed, start slot",
            code="",
        )
        agent, skipped = drive._mint_claim(store, gated, host=HOST, now=NOW, hub=mode)
        self.assertIsNone(agent)
        assert skipped is not None
        self.assertEqual(skipped.code, "claim")
        self.assertIn("race lost", skipped.reason)

    def test_hub_down_skips_new_claims_resumes_held_work_and_exits_3(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7, at=1000.0)
        store.enqueue("app", 9, "status", "working", now=1000.0)
        fake = FakeTracker(
            [
                reading(repo="app", number=7, updated_at=1900.0),
                reading(repo="app", number=9, updated_at=1900.0),
            ]
        )
        launcher = FakeLauncher()
        hub_fake = FakeHub()
        hub_fake.unreachable = True
        out, err = io.StringIO(), io.StringIO()
        code = hub_pass(
            store,
            self.lock,
            out,
            err,
            [("blue", [("app", 9, "start"), ("app", 7, "resume")])],
            fake,
            launcher,
            hub_fake,
            self.policies,
            agent_states={made.agent: (registry.LIVE, 1990.0)},
        )
        self.assertEqual(code, 3)
        self.assertEqual(hub_fake.calls, [("claims",)])
        self.assertIsNone(store.claim_of("app", 9))
        self.assertIn("hub unreachable", out.getvalue())
        resumed = [call for call in launcher.calls if call[3] == "resume"]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0][1:3], ("app", 7))
        self.assertIn(("write", "app", 9, "status", "working"), fake.calls)

    def test_single_host_pass_mints_locally_with_zero_hub_calls(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        counter = FakeHub()
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
        )
        self.assertEqual(code, 0)
        self.assertEqual(counter.calls, [])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, 1)
        self.assertEqual(len(launcher.calls), 1)

    def test_plan_makes_no_hub_calls(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        hub_fake = FakeHub()
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
            hub_client=hub_fake,
            hub_host_id=HOST,
            now=NOW,
        )
        self.assertEqual(code, 0)
        self.assertEqual(hub_fake.calls, [])
        self.assertIsNone(store.claim_of("app", 7))

    def test_empty_hub_host_id_is_a_usage_error_that_keeps_claims(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        fake_hub = FakeHub(host_id="")
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=made.epoch + 1)]
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
            hub_client=fake_hub,
            hub_host_id="",
            now=NOW,
        )
        self.assertEqual(code, 4)
        self.assertEqual(fake_hub.calls, [])
        self.assertIn("host", err.getvalue())
        kept = store.claim_of("app", 7)
        assert kept is not None
        self.assertEqual((kept.agent, kept.epoch), (made.agent, made.epoch))

    def test_local_store_error_after_a_hub_grant_releases_the_hub_row(self):
        for failure in (
            board.StoreError("disk locked"),
            board.ExternalItem("app#7 is external"),
            sqlite3.Error("database is locked"),
        ):
            with self.subTest(failure=type(failure).__name__):
                store = stored(self.db, [("app", 7, "open")])
                self.addCleanup(store.close)
                hub_fake = FakeHub()
                hub_fake.receipts[("app", 7)] = 4
                mode = drivehub.HubMode(hub_fake, HOST)
                gated = drive.Gated(
                    project="blue",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="start",
                    reason="app#7: unclaimed, start slot",
                    code="",
                )
                with mock.patch.object(store, "claim", side_effect=failure):
                    agent, skipped = drive._mint_claim(store, gated, host=HOST, now=NOW, hub=mode)
                self.assertIsNone(agent)
                assert skipped is not None
                self.assertEqual(skipped.code, "claim")
                self.assertIn("claim refused", skipped.reason)
                proposes = [call for call in hub_fake.calls if call[0] == "propose"]
                self.assertEqual(len(proposes), 1)
                self.assertEqual(hub_fake.releases, [("app", 7, proposes[0][3], 4)])
                self.assertIsNone(store.claim_of("app", 7))

    def test_hub_error_is_a_usage_error(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()

        class BadHub(FakeHub):
            def propose(self, repo, number, agent):
                self.calls.append(("propose", repo, number, agent))
                raise hub.HubError("hub refused this host")

        out, err = io.StringIO(), io.StringIO()
        code = hub_pass(
            store,
            self.lock,
            out,
            err,
            [("blue", [("app", 7, "start")])],
            fake,
            launcher,
            BadHub(),
            self.policies,
        )
        self.assertEqual(code, 4)
        self.assertIsNone(store.claim_of("app", 7))
        self.assertEqual(launcher.calls, [])


class HubReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-reconcile-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def reconcile(self, store, fake_hub, **fields):
        stop = fields.pop("stop", FakeStop())
        out = fields.pop("out", io.StringIO())
        values = {"stop_unit": stop, "out": out, "err": io.StringIO(), "now": NOW}
        values.update(fields)
        hub = drivehub.HubMode(fake_hub, HOST)
        table = hub.reconcile(store, **values)
        return table, stop, out.getvalue()

    def test_fence_drops_the_superseded_row(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        other = claimed(store, "app", 8)
        fake_hub = FakeHub()
        fake_hub.rows = [
            hub_row("app", 7, "drive-elsewhere", host_id="host-2", epoch=4),
            hub_row("app", 8, other.agent, epoch=other.epoch),
        ]
        table, stop, logged = self.reconcile(store, fake_hub)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertIsNone(store.claim_of("app", 7))
        kept = store.claim_of("app", 8)
        assert kept is not None
        self.assertEqual(kept.epoch, other.epoch)
        self.assertEqual(table[("app", 7)], 4)
        self.assertIn("app#7", logged)

    def test_live_row_renews_when_the_lease_is_live(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=made.epoch)]
        table, stop, _ = self.reconcile(store, fake_hub)
        self.assertEqual(fake_hub.calls[0], ("claims",))
        self.assertIn(("renew", "app", 7, made.agent), fake_hub.calls)
        self.assertEqual(stop.calls, [])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, made.epoch)
        self.assertEqual(table[("app", 7)], made.epoch)

    def test_lapsed_row_regrants_and_adopts_the_bumped_epoch(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=made.epoch, lease=NOW - 1.0)]
        fake_hub.receipts[("app", 7)] = made.epoch + 1
        table, stop, logged = self.reconcile(store, fake_hub)
        self.assertIn(("propose", "app", 7, made.agent), fake_hub.calls)
        self.assertEqual(stop.calls, [])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, made.epoch + 1)
        self.assertEqual(table[("app", 7)], made.epoch + 1)
        self.assertIn("re-grant", logged)

    def test_renew_conflict_fences(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=made.epoch)]
        fake_hub.conflicts[("app", 7)] = ("drive-other", "host-2", 9)
        table, stop, logged = self.reconcile(store, fake_hub)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertIsNone(store.claim_of("app", 7))
        self.assertIn("app#7", logged)

    def test_regrant_conflict_fences(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=made.epoch, lease=NOW - 1.0)]
        fake_hub.conflicts[("app", 7)] = ("drive-other", "host-2", 9)
        _, stop, _ = self.reconcile(store, fake_hub)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertIsNone(store.claim_of("app", 7))

    def test_extra_hub_row_with_no_local_record_is_released(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, "drive-old", epoch=3)]
        table, stop, logged = self.reconcile(store, fake_hub)
        self.assertIn(("release", "app", 7, "drive-old"), fake_hub.calls)
        self.assertEqual(stop.calls, [])
        self.assertIn("app#7", logged)

    def test_missing_hub_row_keeps_the_local_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake_hub = FakeHub()
        fake_hub.rows = []
        _, stop, logged = self.reconcile(store, fake_hub)
        self.assertEqual(stop.calls, [])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, made.epoch)
        self.assertIn("no row", logged)

    def test_older_hub_epoch_keeps_the_local_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        first = claimed(store, "app", 7)
        store.release("app", 7, first.agent, first.epoch, now=1000.0)
        made = store.claim("app", 7, first.agent, HOST, now=1000.0)
        assert made.epoch > 1
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=1)]
        _, stop, logged = self.reconcile(store, fake_hub)
        self.assertEqual(stop.calls, [])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, made.epoch)
        self.assertIn("older", logged)

    def test_reconcile_continues_when_a_drop_fails(self):
        failures = {
            "race": board.Claimed("app#7 is held"),
            "missing row": board.StoreError("unknown item app#7"),
            "write failure": sqlite3.Error("database is locked"),
        }
        for name, failure in failures.items():
            with self.subTest(name):
                db = os.path.join(self.tmp.name, f"drop-{name.replace(' ', '-')}.sqlite")
                store = stored(db, [("app", 7, "open"), ("app", 8, "open")])
                self.addCleanup(store.close)
                first = claimed(store, "app", 7)
                second = claimed(store, "app", 8)
                fake_hub = FakeHub()
                fake_hub.rows = [
                    hub_row("app", 7, first.agent, epoch=first.epoch + 1),
                    hub_row("app", 8, second.agent, epoch=second.epoch + 1),
                ]
                real_release = store.release

                def flaky(repo, number, agent, epoch, *, now, _failure=failure):
                    if number == 7:
                        raise _failure
                    return real_release(repo, number, agent, epoch, now=now)

                stop = FakeStop()
                out = io.StringIO()
                with mock.patch.object(store, "release", side_effect=flaky):
                    table, _, logged = self.reconcile(store, fake_hub, stop=stop, out=out)
                self.assertEqual(stop.calls, [("app", 7), ("app", 8)])
                kept = store.claim_of("app", 7)
                assert kept is not None
                self.assertEqual(kept.epoch, first.epoch)
                self.assertIsNone(store.claim_of("app", 8))
                self.assertIn("left held", logged)

    def test_fence_never_touches_a_foreign_local_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "someone-else", "elsewhere", now=1000.0)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, "drive-mine", epoch=2)]
        _, stop, logged = self.reconcile(store, fake_hub)
        self.assertEqual(stop.calls, [])
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.agent, "someone-else")
        self.assertIn("foreign", logged)

    def test_fence_runs_before_renew_before_new_propose(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        stale = claimed(store, "app", 7)
        live = claimed(store, "app", 8)
        fake_hub = FakeHub()
        fake_hub.rows = [
            hub_row("app", 7, stale.agent, epoch=stale.epoch + 1),
            hub_row("app", 8, live.agent, epoch=live.epoch),
        ]
        order = []
        fake_hub.calls = order

        class OrderStop:
            def __call__(self, repo, number):
                order.append(("stop", repo, number))
                return [f"stopped-{repo}-{number}"]

        self.reconcile(store, fake_hub, stop=OrderStop())
        self.assertEqual(
            order,
            [
                ("claims",),
                ("stop", "app", 7),
                ("renew", "app", 8, live.agent),
            ],
        )


class HubStuckWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-hub-stuck-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def stuck_hub_pass(self, store, fake_hub, **fields):
        out, err = io.StringIO(), io.StringIO()
        values = {
            "agent_states": {},
            "present_agents": frozenset({AGENT}),
            "live_units": frozenset(),
            "gone_s": 3600.0,
            "stale_s": 600.0,
            "stop_unit": FakeStop(),
            "restarter": FakeLauncher(),
            "stuck_project": lambda repo, number: "blue",
        }
        values.update(fields)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            tracker_factory=None,
            operators=(),
            launcher=FakeLauncher(),
            hub_client=fake_hub,
            hub_host_id=HOST,
            now=NOW,
            **values,
        )
        return code, out.getvalue(), err.getvalue()

    def test_matching_epoch_restarts_as_today(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7, at=1000.0)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=made.epoch)]
        code, logged, _ = self.stuck_hub_pass(store, fake_hub)
        self.assertEqual(code, 0)
        self.assertIn("restarted", logged)

    def test_superseded_epoch_skips_when_the_drop_fails(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7, at=1000.0)
        fake_hub = FakeHub()
        fake_hub.rows = [hub_row("app", 7, made.agent, epoch=made.epoch + 1)]
        stop = FakeStop()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            store,
            "release",
            side_effect=board.Claimed("item app#7 is held by somebody-else"),
        ):
            code = drive_pass(
                store,
                self.lock,
                out,
                err,
                tracker_factory=None,
                operators=(),
                launcher=FakeLauncher(),
                hub_client=fake_hub,
                hub_host_id=HOST,
                now=NOW,
                agent_states={},
                present_agents=frozenset({AGENT}),
                live_units=frozenset(),
                gone_s=3600.0,
                stale_s=600.0,
                stop_unit=stop,
                restarter=FakeLauncher(),
                stuck_project=lambda repo, number: "blue",
            )
        self.assertEqual(code, 0)
        self.assertEqual(stop.calls, [("app", 7)])
        self.assertIn("supersedes", out.getvalue())

    def test_hub_down_skips_stuck_restarts(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7, at=1000.0)
        fake_hub = FakeHub()
        fake_hub.unreachable = True
        code, logged, _ = self.stuck_hub_pass(store, fake_hub)
        self.assertEqual(code, 3)
        self.assertIn("unreadable", logged)
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, made.epoch)


def resolved_with(hub_url="", hub_host_id=""):
    """A real `settings.Resolved`, so a dict can no longer stand in at the seam."""
    return settings.resolve(host={"hub": {"url": hub_url, "host_id": hub_host_id}}, env={})


class HubClientForTest(unittest.TestCase):
    def test_no_url_means_no_client(self):
        self.assertIsNone(drivehub.hub_client_for(resolved_with(), token_path="/t"))

    def test_url_without_host_id_is_refused(self):
        with self.assertRaises(drive.HubConfigError):
            drivehub.hub_client_for(resolved_with(hub_url="http://host-1:8000"), token_path="/t")

    def test_client_carries_url_host_and_token_path(self):
        client = drivehub.hub_client_for(
            resolved_with(hub_url="http://host-1:8000", hub_host_id="host-1"),
            token_path="/t/hub-token",
        )
        assert client is not None
        self.assertEqual(
            (client.url, client.host_id, client.token_path),
            ("http://host-1:8000", "host-1", "/t/hub-token"),
        )


class CmdDrivePlanTest(unittest.TestCase):
    """`cmd_drive --plan` over a real temporary root: exit 0, zero hub calls.

    The store, host config, project registry and ops root are real
    files in temporary directories; the seeded rows stand for
    tracker-synced state (`--plan` never contacts a tracker). The
    systemd seam reads empty through a stubbed `units.list_units`,
    and agent state reads empty from the fresh temp state dir. With
    no hub configured no hub client is ever built; with `hub.url`
    set the plan still makes zero hub calls, through a recording
    fake standing at the client seam.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-cmd-")
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.config = os.path.join(self.tmp.name, "config")
        self.ops = os.path.join(self.tmp.name, "ops")
        os.makedirs(os.path.join(self.config, "projects.d"))
        os.makedirs(self.ops)
        store = stored(os.path.join(self.state, "board.sqlite"), [("app", 7, "open")])
        store.close()
        with open(os.path.join(self.ops, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write("")
        with open(os.path.join(self.ops, "policy.py"), "w", encoding="utf-8") as handle:
            handle.write(policy_source([("app", 7, "start")]))
        with open(
            os.path.join(self.config, "projects.d", "blue.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                "name = 'blue'\n"
                "unit_prefix = 'blue-agent-'\n"
                f"root = {self.ops!r}\n"
                "eligibility = 'policy.py:eligible'\n"
            )
        env = {"CORRAL_STATE": self.state, "CORRAL_CONFIG": self.config}
        self._env_patch = mock.patch.dict(os.environ, env)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        units_patch = mock.patch("corral.units.list_units", return_value=[])
        units_patch.start()
        self.addCleanup(units_patch.stop)

    def run_plan(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(drive.sys, "stdout", out),
            mock.patch.object(drive.sys, "stderr", err),
        ):
            code = drivehub.cmd_drive(argparse.Namespace(plan=True, drive_verb=None))
        return code, out.getvalue(), err.getvalue()

    def test_plan_with_no_hub_configured_exits_zero(self):
        class BombClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("single-host plan must not build a hub client")

        with mock.patch.object(drivehub.hubmod, "HubClient", BombClient):
            code, logged, errors = self.run_plan()
        self.assertEqual(code, 0)
        self.assertIn("app#7", logged)
        self.assertEqual(errors, "")

    def test_plan_with_hub_configured_makes_zero_hub_calls(self):
        with open(os.path.join(self.config, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write('[hub]\nurl = "http://host-1:8000"\nhost_id = "host-1"\n')

        class FakeHubClient:
            built = []

            def __init__(self, url, host_id, token_path, **kwargs):
                FakeHubClient.built.append((url, host_id, token_path))
                self.calls = []

        with mock.patch.object(drivehub.hubmod, "HubClient", FakeHubClient):
            code, logged, errors = self.run_plan()
        self.assertEqual(code, 0)
        self.assertIn("app#7", logged)
        self.assertEqual(errors, "")
        self.assertEqual(FakeHubClient.built, [])


class HubVersionMappingTest(unittest.TestCase):
    def test_a_newer_hub_major_on_the_claim_path_is_a_config_error(self):
        class OldHost(FakeHub):
            def propose(self, repo, number, agent):
                raise hub.HubVersionError(
                    f"hub speaks api {hub.HUB_API + 1}, this host speaks api {hub.HUB_API}:"
                    " upgrade the hub and the hosts in lockstep"
                )

        mode = drivehub.HubMode(OldHost(), HOST)
        with self.assertRaises(drive.HubConfigError) as caught:
            mode.propose("app", 7, "drive-aaa")
        self.assertIn(str(hub.HUB_API + 1), str(caught.exception))
        self.assertIn(str(hub.HUB_API), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
