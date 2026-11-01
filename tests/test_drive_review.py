"""The drive reviewer and fix-round pass: schedule reviewers, resume or redo fixes.

Reviewers start only for PR-ready rows (a finished worker proves pushed
work exists to review) with the pre-release worker id passed as the
author; fix rounds resume the holder's session when it is still usable
and otherwise restart on the same branch; merge-ready rows (reviewed
since the last work) are ignored. Fakes only: a temporary board store,
stub policies, injected registry states and a recording launcher. No
`gh`, no systemd, no network, no real state dir.
"""

import io
import json
import os
import tempfile
import unittest

from corral import board, drive, registry
from corral.github.tracker import NEEDS_DECISION
from tests.test_drive import stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_drive_stuck import AGENT, HOST, NOW, OLD, claimed
from tests.test_tracker import FakeTracker, reading

WORKER = AGENT
REVIEWER = "drive-host-1-17000-acme--app-7"
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
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-review-holders-")
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

    def test_a_missing_state_map_releases_nothing_and_keeps_the_claim(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        before = claimed(store, "app", 7)
        out = io.StringIO()
        self.assertEqual(drive.release_finished(store, agent_states=None, now=NOW, out=out), 0)
        self.assertEqual(out.getvalue(), "")
        after = store.claim_of("app", 7)
        assert after is not None
        self.assertEqual((after.agent, after.epoch), (before.agent, before.epoch))


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


class ProductionFinishedForTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-review-registry-")
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
        found = drive.production_finished_for(now=3000.0, cfg=self.cfg, path=self.dir)
        self.assertEqual(found, {("app", 7): ("reviewer-1", "reviewer")})

    def test_live_entries_and_ticketless_entries_are_ignored(self):
        self.register_done("worker-1", "app", 7, "worker", at=1000.0)
        registry.register("live-1", "muse", now=2990.0, path=self.dir)
        registry.register("lonely-1", "muse", now=2000.0, path=self.dir)
        registry.done("lonely-1", now=2001.0, path=self.dir)
        found = drive.production_finished_for(now=3000.0, cfg=self.cfg, path=self.dir)
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
        found = drive.production_finished_for(now=3000.0, cfg=self.cfg, path=self.dir)
        self.assertEqual(found, {("app", 7): ("old-1", "")})


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
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-review-pass-")
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

    def test_a_reviewer_starts_with_the_released_worker_as_author(self):
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
            agent_states={WORKER: (FINISHED, NOW - 10.0)},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, title, author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", "app", 7, "review"))
        self.assertEqual(author, WORKER)
        self.assertTrue(agent.startswith(drive.MANAGED_PREFIX))
        self.assertNotEqual(agent, WORKER)
        self.assertIn("reviewed 1", out.splitlines()[-1])

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


class FixPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-fix-pass-")
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


class FixBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-fix-budget-")
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


class ReviewFixDayCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-review-day-cap-")
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


if __name__ == "__main__":
    unittest.main()
