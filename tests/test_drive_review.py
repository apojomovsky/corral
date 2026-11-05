"""The drive reviewer and fix-round pass: schedule reviewers, resume or redo fixes.

Reviewers start only for PR-ready rows (a finished worker proves pushed
work exists to review) with the pre-takeover worker id passed as the
author; fix rounds resume the holder's session when it is still usable
and otherwise restart on the same branch; merge-ready rows (reviewed
since the last work) are ignored. Fakes only: a temporary board store,
stub policies, injected registry states and a recording launcher. No
`gh`, no systemd, no network, no real state dir.
"""

import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from korapy import board, drive, registry
from korapy.tracker import NEEDS_DECISION
from tests.test_drive import stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_drive_stuck import AGENT, HOST, NOW, OLD, UNIT, UNIT_R1, claimed
from tests.test_drive_stuck import HOLDER, FakeStop
from tests.test_tracker import FakeTracker, reading

WORKER = AGENT
REVIEWER = "drive-host-1-17000-acme--app-7"
RIVAL = "drive-host-1-19999-acme--app-7"
FINISHED = registry.FINISHED


def review_pass(
    store,
    lock_path,
    out,
    err,
    *,
    agent_states=None,
    finished_for=None,
    live_units=frozenset(),
    **fields,
):
    """`drive_pass` with reviewer-scheduling defaults: fakes, a host, a cap."""
    values = {"agent_states": agent_states, "finished_for": finished_for}
    values.update(fields)
    return drive_pass(
        store,
        lock_path,
        out,
        err,
        live_units=live_units,
        stale_s=600.0,
        **values,
    )


class FinishedHoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-review-holders-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_a_finished_managed_holder_is_captured_by_ticket(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        holders = drive.finished_holders(store, agent_states={WORKER: (FINISHED, NOW)})
        self.assertEqual(holders, {("app", 7): WORKER})

    def test_live_stale_gone_foreign_and_unknown_holders_are_ignored(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        claimed(store, "app", 8, agent="manual-9")
        states = {
            WORKER: (registry.LIVE, NOW),
            "manual-9": (FINISHED, NOW),
        }
        holders = drive.finished_holders(store, agent_states=states)
        self.assertEqual(holders, {})
        self.assertEqual(drive.finished_holders(store, agent_states={}), {})

    def test_unclaimed_rows_name_no_holder(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        holders = drive.finished_holders(store, agent_states={WORKER: (FINISHED, NOW)})
        self.assertEqual(holders, {})

    def test_a_missing_state_map_reads_as_no_finished_holder(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        self.assertEqual(drive.finished_holders(store, agent_states=None), {})

    def test_a_finished_unit_entry_captures_the_drive_id_holder(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, HOLDER, HOST, now=NOW - 4000.0)
        holders = drive.finished_holders(
            store,
            agent_states={UNIT: (FINISHED, NOW)},
            agent_tickets={UNIT: ("app", 7)},
        )
        self.assertEqual(holders, {("app", 7): HOLDER})

    def test_a_finished_entry_for_another_ticket_captures_nothing(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, HOLDER, HOST, now=NOW - 4000.0)
        holders = drive.finished_holders(
            store,
            agent_states={UNIT: (FINISHED, NOW)},
            agent_tickets={UNIT: ("app", 8)},
        )
        self.assertEqual(holders, {})

    def test_a_transient_lock_on_the_scan_retries_then_reports(self):
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
            holders = drive.finished_holders(store, agent_states={WORKER: (FINISHED, NOW)})
        finally:
            store.items = original_items
        self.assertEqual(holders, {("app", 7): WORKER})

    def test_a_transient_lock_on_a_claim_read_retries_then_reports(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        original_claim_of = store.claim_of
        calls = []

        def flaky_claim_of(repo, number):
            calls.append(1)
            if len(calls) < 2:
                raise sqlite3.OperationalError("database is locked")
            return original_claim_of(repo, number)

        store.claim_of = flaky_claim_of
        try:
            holders = drive.finished_holders(store, agent_states={WORKER: (FINISHED, NOW)})
        finally:
            store.claim_of = original_claim_of
        self.assertEqual(holders, {("app", 7): WORKER})

    def test_a_persistent_lock_on_the_scan_raises_for_the_caller_to_skip(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        original_items = store.items
        calls = []

        def locked(*args, **kwargs):
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        store.items = locked
        try:
            with self.assertRaises(sqlite3.Error):
                drive.finished_holders(store, agent_states={WORKER: (FINISHED, NOW)})
        finally:
            store.items = original_items
        self.assertGreater(len(calls), 1)
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))

    def test_a_non_transient_scan_error_raises_at_once(self):
        for problem in (
            board.StoreError("unusable row app#7"),
            sqlite3.Error("disk write failed"),
        ):
            store = stored(self.db, [("app", 7, "open")])
            self.addCleanup(store.close)
            claimed(store, "app", 7)
            original_items = store.items
            calls = []

            def broken(*args, **kwargs):
                calls.append(1)
                raise problem

            store.items = broken
            try:
                with self.assertRaises(type(problem)):
                    drive.finished_holders(store, agent_states={WORKER: (FINISHED, NOW)})
            finally:
                store.items = original_items
            self.assertEqual(len(calls), 1)


class ResolveFinishedTest(unittest.TestCase):
    def test_a_same_pass_release_wins_with_the_known_role(self):
        resolved = drive.resolve_finished({("app", 7): (WORKER, "worker")}, {("app", 7): WORKER})
        self.assertEqual(resolved, {("app", 7): (WORKER, "worker")})

    def test_a_same_pass_release_of_an_unknown_agent_reads_as_unreviewed(self):
        resolved = drive.resolve_finished({}, {("app", 7): WORKER})
        self.assertEqual(resolved, {("app", 7): (WORKER, None)})

    def test_a_same_pass_release_supersedes_an_older_finished_entry(self):
        resolved = drive.resolve_finished(
            {("app", 7): ("drive-host-1-10000-acme--app-7", "reviewer")},
            {("app", 7): WORKER},
        )
        self.assertEqual(resolved, {("app", 7): (WORKER, None)})

    def test_cross_pass_entries_stand_without_a_release(self):
        finished = {("app", 7): (REVIEWER, "reviewer")}
        self.assertEqual(drive.resolve_finished(finished, {}), finished)
        self.assertEqual(drive.resolve_finished(None, None), {})

    def test_a_same_pass_release_keeps_the_role_through_the_ticket_resolver(self):
        resolved = drive.resolve_finished(
            {("app", 7): (UNIT, "reviewer")},
            {("app", 7): HOLDER},
            agent_states={UNIT: (FINISHED, NOW)},
            agent_tickets={UNIT: ("app", 7)},
        )
        self.assertEqual(resolved, {("app", 7): (HOLDER, "reviewer")})

    def test_a_release_for_a_newer_entry_drops_the_older_role(self):
        resolved = drive.resolve_finished(
            {("app", 7): (UNIT, "reviewer")},
            {("app", 7): HOLDER},
            agent_states={UNIT_R1: (FINISHED, NOW)},
            agent_tickets={UNIT_R1: ("app", 7)},
        )
        self.assertEqual(resolved, {("app", 7): (HOLDER, None)})


class FixModeTest(unittest.TestCase):
    def test_a_live_registry_entry_resumes(self):
        mode = drive.fix_mode(
            WORKER,
            repo="app",
            number=7,
            agent_states={WORKER: (registry.LIVE, NOW)},
            live_units=frozenset(),
        )
        self.assertEqual(mode, "resume")

    def test_a_stale_registry_entry_resumes(self):
        mode = drive.fix_mode(
            WORKER,
            repo="app",
            number=7,
            agent_states={WORKER: (registry.STALE, NOW)},
            live_units=frozenset(),
        )
        self.assertEqual(mode, "resume")

    def test_a_live_unit_without_an_entry_resumes(self):
        mode = drive.fix_mode(
            WORKER,
            repo="app",
            number=7,
            agent_states={},
            live_units=frozenset({("app", 7)}),
        )
        self.assertEqual(mode, "resume")

    def test_a_gone_holder_without_a_unit_restarts_fresh(self):
        mode = drive.fix_mode(
            WORKER,
            repo="app",
            number=7,
            agent_states={},
            live_units=frozenset(),
        )
        self.assertEqual(mode, "fresh")

    def test_a_finished_holder_without_a_unit_restarts_fresh(self):
        mode = drive.fix_mode(
            WORKER,
            repo="app",
            number=7,
            agent_states={WORKER: (FINISHED, NOW)},
            live_units=frozenset(),
        )
        self.assertEqual(mode, "fresh")

    def test_a_live_unit_entry_resumes_behind_a_drive_id_holder(self):
        mode = drive.fix_mode(
            HOLDER,
            repo="app",
            number=7,
            agent_states={UNIT: (registry.LIVE, NOW)},
            agent_tickets={UNIT: ("app", 7)},
            live_units=frozenset(),
        )
        self.assertEqual(mode, "resume")

    def test_a_finished_unit_entry_without_a_unit_restarts_fresh(self):
        mode = drive.fix_mode(
            HOLDER,
            repo="app",
            number=7,
            agent_states={UNIT: (FINISHED, NOW)},
            agent_tickets={UNIT: ("app", 7)},
            live_units=frozenset(),
        )
        self.assertEqual(mode, "fresh")


class ProductionFinishedForTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-review-registry-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.dir)
        self.cfg = registry.default_config()

    def register_done(self, agent, repo, number, role, *, at):
        registry.register(
            agent,
            "muse",
            ticket=registry.make_ticket(repo, number),
            role=role,
            now=at,
            path=self.dir,
        )
        registry.done(agent, now=at + 1.0, path=self.dir)

    def test_newest_finished_entry_wins_per_ticket(self):
        self.register_done("worker-1", "app", 7, "worker", at=1000.0)
        self.register_done("reviewer-1", "app", 7, "reviewer", at=2000.0)
        found = drive.production_finished_for([], now=3000.0, cfg=self.cfg, path=self.dir)
        self.assertEqual(found, {("app", 7): ("reviewer-1", "reviewer")})

    def test_live_entries_and_ticketless_entries_are_ignored(self):
        self.register_done("worker-1", "app", 7, "worker", at=1000.0)
        registry.register("live-1", "muse", now=2990.0, path=self.dir)
        registry.register("lonely-1", "muse", now=2000.0, path=self.dir)
        registry.done("lonely-1", now=2001.0, path=self.dir)
        found = drive.production_finished_for([], now=3000.0, cfg=self.cfg, path=self.dir)
        self.assertEqual(found, {("app", 7): ("worker-1", "worker")})

    def test_entries_without_a_role_read_as_unreviewed(self):
        registry.register(
            "old-1",
            "muse",
            ticket=registry.make_ticket("app", 7),
            now=1000.0,
            path=self.dir,
        )
        registry.done("old-1", now=1001.0, path=self.dir)
        found = drive.production_finished_for([], now=3000.0, cfg=self.cfg, path=self.dir)
        self.assertEqual(found, {("app", 7): ("old-1", "")})


class ProductionAgentRolesTest(unittest.TestCase):
    def test_roles_map_visible_entries_with_a_role(self):
        from unittest import mock

        entries = [
            ({"id": "worker-1", "role": "worker"}, registry.LIVE),
            ({"id": "reviewer-1", "role": "reviewer"}, registry.LIVE),
            ({"id": "norole-1"}, registry.LIVE),
        ]
        with (
            mock.patch.object(drive.registry, "config", return_value={}),
            mock.patch.object(drive.registry, "visible", return_value=entries),
        ):
            self.assertEqual(
                drive.production_agent_roles(now=NOW),
                {"worker-1": "worker", "reviewer-1": "reviewer"},
            )


class FixMessageTest(unittest.TestCase):
    def test_the_message_names_the_ticket_and_the_next_review(self):
        message = drive.fix_message("app", 7)
        self.assertIn("app#7", message)
        self.assertIn("review", message)
        self.assertIn("push", message)


class ReviewerPromptTest(unittest.TestCase):
    def test_the_author_block_names_the_worker_for_record_time(self):
        block = drive.reviewer_author_block(WORKER)
        self.assertIn(WORKER, block)
        self.assertIn("--author", block)
        self.assertIn("reviewer", block)

    def test_a_blank_author_is_refused_before_any_launch(self):
        with self.assertRaises(ValueError):
            drive.reviewer_author_block("  ")


class ReviewPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-review-pass-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_review(self, store, found, fake_tracker, launcher, **fields):
        out, err = io.StringIO(), io.StringIO()
        code = review_pass(
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

    def test_a_reviewer_takes_over_the_finished_worker_claim_as_author(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_review(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            agent_states={WORKER: (FINISHED, NOW - 10.0)},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, title, author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "review"))
        self.assertEqual(author, WORKER)
        self.assertTrue(agent.startswith(drive.MANAGED_PREFIX))
        self.assertNotEqual(agent, WORKER)
        self.assertIn("takes over", out)
        self.assertIn("reviewed 1", out.splitlines()[-1])
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, agent)
        self.assertEqual(found.epoch, made.epoch + 1)

    def test_a_reviewer_takes_over_a_finished_drive_id_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = store.claim("app", 7, HOLDER, HOST, now=OLD)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        stop, restarter = FakeStop(), FakeLauncher()
        budget = os.path.join(self.tmp.name, "restarts.json")
        code, out, _ = self.run_review(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            agent_states={UNIT: (FINISHED, NOW - 10.0)},
            agent_tickets={UNIT: ("app", 7)},
            finished_for={("app", 7): (UNIT, "")},
            stop_unit=stop,
            restarter=restarter,
            restarts_path=budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, title, author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "review"))
        self.assertEqual(author, HOLDER)
        self.assertTrue(agent.startswith(drive.MANAGED_PREFIX))
        self.assertNotEqual(agent, HOLDER)
        self.assertIn("takes over", out)
        self.assertNotIn("restart", out)
        self.assertEqual(stop.calls, [])
        self.assertEqual(restarter.calls, [])
        self.assertEqual(drive.load_restarts(budget), {})
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, agent)
        self.assertEqual(found.epoch, made.epoch + 1)

    def test_a_later_pass_reviews_with_the_finished_entry_as_author(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, _, _ = self.run_review(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            agent_states={},
            finished_for={("app", 7): (WORKER, "worker")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "review")
        self.assertEqual(launcher.calls[0][6], WORKER)

    def test_a_review_without_a_finished_worker_is_not_pr_ready(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_review(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            agent_states={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        row, _ = store.item("app", 7, now=NOW)
        self.assertEqual(row.agent, "")
        self.assertIn("nothing to review", out)

    def test_a_merge_ready_row_is_ignored(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_review(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            agent_states={},
            finished_for={("app", 7): (REVIEWER, "reviewer")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn("already reviewed", out)

    def test_a_review_waits_while_the_worker_holds_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_review(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            agent_states={WORKER: (registry.LIVE, NOW - 10.0)},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn("still holds the claim", out)

    def test_a_failed_reviewer_launch_releases_the_claim_for_a_later_pass(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = RuntimeError(
            "reviewer prompt override failed: no such template"
        )
        code, out, _ = self.run_review(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            agent_states={WORKER: (FINISHED, NOW - 10.0)},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "review")
        self.assertIsNone(store.claim_of("app", 7))
        self.assertIn("reviewer claim released", out)

    def test_a_persistent_lock_on_the_holder_capture_skips_the_step_and_keeps_claims(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        with mock.patch.object(
            drive,
            "finished_holders",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            code, _, err = self.run_review(
                store,
                [("blue", [("app", 7, "review")])],
                fake,
                launcher,
                agent_states={RIVAL: (FINISHED, NOW - 10.0)},
            )
        self.assertEqual(code, 0)
        self.assertIn("finished holder capture skipped", err)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_failed_reviewer_launch_with_a_release_race_keeps_the_rival_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = RuntimeError(
            "reviewer prompt override failed: no such template"
        )
        original_release = store.release

        def racy_release(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7) and agent != WORKER:
                original_release(repo, number, agent, epoch, now=now)
                store.claim(repo, number, REVIEWER, HOST, now=now)
            return original_release(repo, number, agent, epoch, now=now)

        store.release = racy_release
        try:
            code, out, _ = self.run_review(
                store,
                [("blue", [("app", 7, "review")])],
                fake,
                launcher,
                agent_states={WORKER: (FINISHED, NOW - 10.0)},
            )
        finally:
            store.release = original_release
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertIn("left held", out)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, REVIEWER)


class FixPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-fix-pass-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_fix(self, store, found, fake_tracker, launcher, **fields):
        out, err = io.StringIO(), io.StringIO()
        code = review_pass(
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

    def test_a_fix_resumes_the_live_holder_without_a_new_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={WORKER: (registry.LIVE, NOW - 10.0)},
            live_units=frozenset({("app", 7)}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, title, author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "resume"))
        self.assertEqual(agent, WORKER)
        self.assertIsNone(author)
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))
        self.assertIn("resumed 1", out.splitlines()[-1])

    def test_a_fix_without_a_usable_holder_restarts_on_a_fresh_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
            live_units=frozenset(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, title, author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "fix"))
        self.assertTrue(agent.startswith(drive.MANAGED_PREFIX))
        self.assertNotEqual(agent, WORKER)
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual(after.agent, agent)
        self.assertEqual(after.epoch, before.epoch + 1)
        self.assertIn("fixed 1", out.splitlines()[-1])

    def test_a_fix_after_review_starts_fresh_on_the_same_ticket(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
            finished_for={("app", 7): (REVIEWER, "reviewer")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "start")
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual(after.agent, launcher.calls[0][4])

    def test_a_fix_already_landed_is_skipped(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
            finished_for={("app", 7): (WORKER, "worker")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn("fix already landed", out)

    def test_a_refused_fix_launch_releases_the_fresh_claim(self):
        from korapy import launch

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = launch.CapFull("blue at 1 of 1 ceiling")
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
            live_units=frozenset(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "fix")
        self.assertIn("ceiling", out)
        self.assertIn("released", out)
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_live_unit_race_on_a_fix_launch_keeps_the_fresh_claim(self):
        from korapy import units

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = units.UnitExists("app-7 already live")
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
            live_units=frozenset(),
        )
        self.assertEqual(code, 0)
        self.assertIn("already live", out)
        held = store.claim_of("app", 7)
        self.assertIsNotNone(held)
        assert held is not None
        self.assertTrue(held.agent.startswith(drive.MANAGED_PREFIX))

    def test_a_released_fix_claim_fences_the_next_claim(self):
        from korapy import launch

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        refused = FakeLauncher()
        refused.errors[("app", 7)] = launch.CapFull("blue at 1 of 1 ceiling")
        code, _, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            refused,
            agent_states={},
            live_units=frozenset(),
        )
        self.assertEqual(code, 0)
        self.assertIsNone(store.claim_of("app", 7))
        second = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            fake_tracker=fake,
            launcher=second,
            now=2100.0,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(second.calls), 1)
        held = store.claim_of("app", 7)
        self.assertIsNotNone(held)
        assert held is not None
        self.assertEqual(held.epoch, before.epoch + 2)


class FixBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-fix-budget-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.budget = os.path.join(self.tmp.name, "restarts.json")

    def run_fix(self, store, found, fake_tracker, launcher, **fields):
        out, err = io.StringIO(), io.StringIO()
        fields.setdefault("now", NOW)
        fields.setdefault("restarts_path", self.budget)
        code = review_pass(
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

    def reading(self):
        return reading(repo="app", number=7, updated_at=NOW - 100.0)

    def test_a_successful_fresh_fix_records_a_restart(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([self.reading()])
        launcher = FakeLauncher()
        code, _, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "fix")
        self.assertEqual(drive.load_restarts(self.budget), {"app#7": [NOW]})

    def test_a_resume_with_an_exhausted_budget_still_launches(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        fake = FakeTracker([self.reading()])
        launcher = FakeLauncher()
        code, _, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={WORKER: (registry.LIVE, NOW - 10.0)},
            live_units=frozenset({("app", 7)}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "resume")
        self.assertEqual(launcher.calls[0][4], WORKER)
        self.assertEqual(
            drive.load_restarts(self.budget),
            {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]},
        )

    def test_an_exhausted_budget_gives_up_through_the_decision_seam(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        fake = FakeTracker([self.reading()])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn(("add_label", "app", 7, NEEDS_DECISION), fake.calls)
        self.assertTrue(any(call[0] == "comment" for call in fake.calls))
        self.assertIsNone(store.claim_of("app", 7))
        self.assertIn("gave up", out)
        self.assertNotEqual(before.agent, "")

    def test_an_exhausted_budget_without_a_tracker_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        drive.save_restarts(self.budget, {"app#7": [NOW - 100.0, NOW - 200.0, NOW - 300.0]})
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = review_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "fix")])],
            policy_dir=self.policies,
            fake_tracker=None,
            launcher=launcher,
            now=NOW,
            restarts_path=self.budget,
            agent_states={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))
        self.assertIn("claim kept", out.getvalue())

    def test_fix_rounds_stop_after_the_budget_and_reach_give_up(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([self.reading()])
        launcher = FakeLauncher()
        for _ in range(3):
            code, _, _ = self.run_fix(
                store,
                [("blue", [("app", 7, "fix")])],
                fake,
                launcher,
                agent_states={},
            )
            self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 3)
        self.assertTrue(all(call[3] == "fix" for call in launcher.calls))
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 3)
        self.assertIn(("add_label", "app", 7, NEEDS_DECISION), fake.calls)
        self.assertTrue(any(call[0] == "comment" for call in fake.calls))
        self.assertIsNone(store.claim_of("app", 7))
        self.assertIn("gave up", out)


class FreshFixSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-fix-save-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_fix(self, store, found, fake_tracker, launcher, **fields):
        out, err = io.StringIO(), io.StringIO()
        fields.setdefault("now", NOW)
        code = review_pass(
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

    def test_an_unsavable_budget_skips_the_fix_with_the_claim_kept(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        blocked = os.path.join(self.tmp.name, "restarts-dir")
        os.makedirs(blocked)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=NOW - 100.0)])
        launcher = FakeLauncher()
        code, out, err = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
            restarts_path=blocked,
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))
        self.assertIn("restart budget", out + err)
        self.assertIn("claim kept", out)
        self.assertFalse(any(call[0] in ("comment", "add_label") for call in fake.calls))

    def test_a_failed_fresh_fix_launch_leaves_the_budget_alone(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        budget = os.path.join(self.tmp.name, "restarts.json")
        fake = FakeTracker([reading(repo="app", number=7, updated_at=NOW - 100.0)])
        launcher = FakeLauncher()
        launcher.errors[("app", 7)] = RuntimeError("boom")
        code, _, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={},
            restarts_path=budget,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(drive.load_restarts(budget), {})


class FixBehindReviewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-fix-reviewer-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_fix(self, store, found, fake_tracker, launcher, **fields):
        out, err = io.StringIO(), io.StringIO()
        fields.setdefault("now", NOW)
        code = review_pass(
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

    def test_a_fix_behind_a_live_reviewer_is_skipped_with_the_claim_kept(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7, agent=REVIEWER)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=NOW - 100.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={REVIEWER: (registry.LIVE, NOW - 10.0)},
            live_units=frozenset({("app", 7)}),
            agent_roles={REVIEWER: "reviewer"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))
        self.assertIn("waits for release", out)

    def test_a_plan_shows_the_fix_waiting_behind_the_reviewer(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7, agent=REVIEWER)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=True,
            found=[("blue", [("app", 7, "fix")])],
            policy_dir=self.policies,
            agent_states={REVIEWER: (registry.LIVE, NOW - 10.0)},
            live_units=frozenset({("app", 7)}),
            agent_roles={REVIEWER: "reviewer"},
        )
        self.assertEqual(code, 0)
        self.assertIn("waits for release", out.getvalue())
        self.assertNotIn("fix round on managed holder", out.getvalue())
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual(after.agent, REVIEWER)

    def test_a_fix_proceeds_once_the_reviewer_is_released(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7, agent=REVIEWER)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=NOW - 100.0)])
        launcher = FakeLauncher()
        code, _, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={REVIEWER: (FINISHED, NOW - 10.0)},
            finished_for={("app", 7): (REVIEWER, "reviewer")},
            agent_roles={REVIEWER: "reviewer"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "start")
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual(after.agent, launcher.calls[0][4])

    def test_a_fix_behind_a_live_reviewer_waits_with_real_shaped_ids(self):
        holder, unit = "drive-a1b2c3d4-7", "blue-agent-app-7-r1"
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = store.claim("app", 7, holder, HOST, now=NOW - 4000.0)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=NOW - 100.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={unit: (registry.LIVE, NOW - 10.0)},
            agent_tickets={unit: ("app", 7)},
            live_units=frozenset({("app", 7)}),
            agent_roles={unit: "reviewer"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))
        self.assertIn("waits for release", out)

    def test_a_finished_reviewer_from_an_earlier_pass_is_released_with_real_ids(self):
        holder, unit = "drive-a1b2c3d4-7", "blue-agent-app-7-r1"
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, holder, HOST, now=NOW - 4000.0)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=NOW - 100.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_fix(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            agent_states={unit: (FINISHED, NOW - 10.0)},
            agent_tickets={unit: ("app", 7)},
            finished_for={("app", 7): (unit, "reviewer")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertIn("released finished reviewer", out)
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertNotEqual(after.agent, holder)


class ReviewFixDayCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-review-day-cap-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.day = os.path.join(self.tmp.name, "starts.json")

    def run_capped(self, store, found, fake_tracker, launcher, *, day_used, max_starts, **fields):
        drive.write_day_count(self.day, drive._day_string(NOW), day_used)
        out, err = io.StringIO(), io.StringIO()
        fields.setdefault("now", NOW)
        code = review_pass(
            store,
            self.lock,
            out,
            err,
            found=found,
            policy_dir=self.policies,
            fake_tracker=fake_tracker,
            launcher=launcher,
            day_path=self.day,
            max_starts=max_starts,
            **fields,
        )
        return code, out.getvalue(), err.getvalue()

    def day_starts(self):
        with open(self.day, encoding="utf-8") as handle:
            return json.load(handle)["starts"]

    def test_a_reviewer_is_skipped_at_the_cap(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_capped(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            day_used=1,
            max_starts=1,
            agent_states={WORKER: (FINISHED, NOW - 10.0)},
        )
        self.assertEqual(code, 1)
        self.assertEqual(launcher.calls, [])
        self.assertIn("day cap", out)
        self.assertEqual(self.day_starts(), 1)

    def test_a_reviewer_launch_spends_one_from_the_cap(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, _, _ = self.run_capped(
            store,
            [("blue", [("app", 7, "review")])],
            fake,
            launcher,
            day_used=0,
            max_starts=1,
            agent_states={WORKER: (FINISHED, NOW - 10.0)},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "review")
        self.assertEqual(self.day_starts(), 1)

    def test_a_fresh_fix_is_skipped_at_the_cap_and_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, out, _ = self.run_capped(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            day_used=1,
            max_starts=1,
            agent_states={},
        )
        self.assertEqual(code, 1)
        self.assertEqual(launcher.calls, [])
        self.assertIn("day cap", out)
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))
        self.assertEqual(self.day_starts(), 1)

    def test_a_fresh_fix_launch_spends_one_from_the_cap(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, _, _ = self.run_capped(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            day_used=0,
            max_starts=1,
            agent_states={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "fix")
        self.assertEqual(self.day_starts(), 1)

    def test_a_resume_reusing_the_live_unit_stays_exempt_at_the_cap(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        fake = FakeTracker([reading(repo="app", number=7, updated_at=1900.0)])
        launcher = FakeLauncher()
        code, _, _ = self.run_capped(
            store,
            [("blue", [("app", 7, "fix")])],
            fake,
            launcher,
            day_used=1,
            max_starts=1,
            agent_states={WORKER: (registry.LIVE, NOW - 10.0)},
            live_units=frozenset({("app", 7)}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(launcher.calls[0][3], "resume")
        self.assertEqual(self.day_starts(), 1)


class ReviewerReleaseTest(unittest.TestCase):
    """Direct pins for `drive._release_reviewer_claim`.

    Each test drives the helper directly against a temporary board
    store, one per fail-closed branch: an unreadable release read, an
    already gone claim, a claim changed under the pass, and a refused
    release. The success and re-claim branches already have pass-level
    pins in `ReviewPassTest`. Fakes only: no `gh`, no systemd, no
    network, no real state dir.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-reviewer-release-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")

    def test_an_unreadable_release_read_keeps_the_claim(self):
        for problem in (
            board.StoreError("unusable item app#7"),
            sqlite3.OperationalError("database is locked"),
        ):
            store = stored(self.db, [("app", 7, "open")])
            self.addCleanup(store.close)
            made = claimed(store, "app", 7)
            original_claim_of = store.claim_of

            def flaky_claim_of(repo, number, problem=problem):
                raise problem

            store.claim_of = flaky_claim_of
            try:
                suffix = drive._release_reviewer_claim(store, "app", 7, made.agent, now=NOW)
            finally:
                store.claim_of = original_claim_of
            self.assertIn("reviewer release unreadable", suffix)
            self.assertIn("claim kept", suffix)
            found = store.claim_of("app", 7)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_an_already_gone_claim_reopens_the_row(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        store.release("app", 7, made.agent, made.epoch, now=NOW)
        suffix = drive._release_reviewer_claim(store, "app", 7, made.agent, now=NOW)
        self.assertIn("reviewer claim already gone", suffix)
        self.assertIn("row open for a later pass", suffix)
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_changed_claim_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        suffix = drive._release_reviewer_claim(store, "app", 7, RIVAL, now=NOW)
        self.assertIn("reviewer claim changed under the pass", suffix)
        self.assertIn("left held", suffix)
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))

    def test_a_refused_release_keeps_the_claim(self):
        for problem in (
            board.ExternalItem("item app#7 is external: read-only"),
            board.StoreError("cannot write the store"),
            sqlite3.OperationalError("database is locked"),
        ):
            store = stored(self.db, [("app", 7, "open")])
            self.addCleanup(store.close)
            made = claimed(store, "app", 7)
            original_release = store.release

            def refused(repo, number, agent, epoch, *, now, problem=problem):
                raise problem

            store.release = refused
            try:
                suffix = drive._release_reviewer_claim(store, "app", 7, made.agent, now=NOW)
            finally:
                store.release = original_release
            self.assertIn("reviewer release refused", suffix)
            self.assertIn("claim kept", suffix)
            found = store.claim_of("app", 7)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))


class ClaimRaceTest(unittest.TestCase):
    """Fail-safe branches over a pre-released or re-claimed store.

    Each test drives `drive._claim_and_launch` directly with one gated
    row, after moving the real store under it the way a concurrent pass
    would: releasing the claim first, or re-claiming it for a rival
    mid-flight. Fakes only: a temporary board store, a temporary
    restart budget, and a recording launcher. No `gh`, no systemd, no
    network, no real state dir.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-claim-races-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.budget = os.path.join(self.tmp.name, "restarts.json")

    def launch(self, store, gated, **over):
        values = {
            "tracker_factory": None,
            "operators": {},
            "launcher": FakeLauncher(),
            "host": HOST,
            "now": NOW,
            "stale_after": 3600.0,
            "restarts_path": self.budget,
        }
        values.update(over)
        launcher = values["launcher"]
        rows, started, blocked, launched = drive._claim_and_launch(store, [gated], **values)
        return rows, started, blocked, launched, launcher

    def test_a_review_with_a_lost_worker_id_is_skipped_without_claiming(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="review",
            verdict="review",
            reason="app#7: unclaimed, review slot",
            code="",
        )
        rows, started, blocked, launched, launcher = self.launch(
            store, gated, released={}, finished={}
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].verdict, "skip")
        self.assertEqual(rows[0].code, "claim")
        self.assertIn("finished worker id lost", rows[0].reason)
        self.assertEqual((started, blocked, launched), (0, False, set()))
        self.assertEqual(launcher.calls, [])
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_reviewer_launch_skipped_without_a_launcher_releases_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="review",
            verdict="review",
            reason="app#7: unclaimed, review slot",
            code="",
        )
        rows, started, blocked, launched, _ = self.launch(
            store,
            gated,
            launcher=None,
            released={},
            finished={("app", 7): (WORKER, "worker")},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].kind, rows[0].verdict), ("review", "skip"))
        self.assertEqual(rows[0].code, "launch")
        self.assertIn("launch skipped (no launcher)", rows[0].reason)
        self.assertIn("reviewer claim released", rows[0].reason)
        self.assertEqual((started, blocked, launched), (0, False, set()))
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_resume_over_a_released_row_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        store.release("app", 7, made.agent, made.epoch, now=NOW)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="resume",
            verdict="resume",
            reason="app#7: resume on managed holder",
            code="",
        )
        rows, started, blocked, launched, launcher = self.launch(
            store, gated, agent_states={}, live_units=frozenset()
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].verdict, "skip")
        self.assertEqual(rows[0].code, "claim")
        self.assertIn("claim changed under the pass, left held", rows[0].reason)
        self.assertEqual((started, blocked, launched), (0, False, set()))
        self.assertEqual(launcher.calls, [])
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_resume_raced_reclaim_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="resume",
            verdict="resume",
            reason="app#7: resume on managed holder",
            code="",
        )
        original_refresh = store.refresh_claim

        def racy_refresh(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7) and agent == WORKER:
                store.release(repo, number, agent, epoch, now=now)
                store.claim(repo, number, RIVAL, HOST, now=now)
            return original_refresh(repo, number, agent, epoch, now=now)

        store.refresh_claim = racy_refresh
        try:
            rows, started, blocked, launched, launcher = self.launch(
                store,
                gated,
                agent_states={WORKER: (registry.LIVE, NOW - 10.0)},
                live_units=frozenset(),
            )
        finally:
            store.refresh_claim = original_refresh
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].kind, rows[0].verdict), ("resume", "skip"))
        self.assertIn("resume raced a re-claim, left held", rows[0].reason)
        self.assertEqual((started, blocked, launched), (0, False, set()))
        self.assertEqual(launcher.calls, [])
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, RIVAL)

    def test_a_fix_mint_raced_reclaim_names_the_rival(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="fix",
            verdict="fix",
            reason="app#7: fix round on managed holder",
            code="",
        )
        original_claim = store.claim

        def racy_claim(repo, number, agent, host, *, now):
            if (repo, number) == ("app", 7):
                original_claim(repo, number, RIVAL, host, now=now)
            return original_claim(repo, number, agent, host, now=now)

        store.claim = racy_claim
        try:
            rows, started, blocked, launched, launcher = self.launch(
                store, gated, agent_states={}, live_units=frozenset()
            )
        finally:
            store.claim = original_claim
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].kind, rows[0].verdict), ("fix", "skip"))
        self.assertIn("race lost to", rows[0].reason)
        self.assertIn(RIVAL, rows[0].reason)
        self.assertEqual((started, blocked, launched), (0, False, set()))
        self.assertEqual(launcher.calls, [])
        self.assertEqual(drive.load_restarts(self.budget), {})
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, RIVAL)

    def test_a_fix_release_raced_reclaim_is_left_held(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        claimed(store, "app", 7)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="fix",
            verdict="fix",
            reason="app#7: fix round on managed holder",
            code="",
        )
        original_release = store.release

        def racy_release(repo, number, agent, epoch, *, now):
            if (repo, number) == ("app", 7) and agent == WORKER:
                original_release(repo, number, agent, epoch, now=now)
                store.claim(repo, number, RIVAL, HOST, now=now)
            return original_release(repo, number, agent, epoch, now=now)

        store.release = racy_release
        try:
            rows, started, blocked, launched, launcher = self.launch(
                store, gated, agent_states={}, live_units=frozenset()
            )
        finally:
            store.release = original_release
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].kind, rows[0].verdict), ("fix", "skip"))
        self.assertIn("fix release raced a re-claim, left held", rows[0].reason)
        self.assertEqual((started, blocked, launched), (0, False, set()))
        self.assertEqual(launcher.calls, [])
        self.assertEqual(drive.load_restarts(self.budget), {})
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, RIVAL)

    def test_a_fix_release_refusal_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        made = claimed(store, "app", 7)
        gated = drive.Gated(
            project="blue",
            repo="app",
            number=7,
            kind="fix",
            verdict="fix",
            reason="app#7: fix round on managed holder",
            code="",
        )
        original_release = store.release

        def refused(repo, number, agent, epoch, *, now):
            raise board.StoreError("cannot write the store")

        store.release = refused
        try:
            rows, started, blocked, launched, launcher = self.launch(
                store, gated, agent_states={}, live_units=frozenset()
            )
        finally:
            store.release = original_release
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].kind, rows[0].verdict), ("fix", "skip"))
        self.assertIn("fix release refused", rows[0].reason)
        self.assertIn("left held", rows[0].reason)
        self.assertEqual((started, blocked, launched), (0, False, set()))
        self.assertEqual(launcher.calls, [])
        self.assertEqual(drive.load_restarts(self.budget), {})
        found = store.claim_of("app", 7)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.agent, found.epoch), (made.agent, made.epoch))


if __name__ == "__main__":
    unittest.main()
