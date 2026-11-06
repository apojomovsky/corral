"""The fix-round threshold at pass level: lows merge, findings fix.

A reviewer finishes on a ticket and the next pass releases the
reviewer claim. A policy that read the review findings requests a fix
round. With an approving review carrying only lows the pass launches
nothing and the row rests merge-ready; with a normal finding the same
pass opens exactly one fix round. A ticket with an open pull request
that closes it is never offered as a fresh start; once the pull merges
and the ticket closes, the claim is released and nothing launches.
Fakes only: a temporary board store, stub policies, injected registry
states and a recording launcher. No `gh`, no systemd, no network, no
real state dir.
"""

import io
import os
import tempfile
import unittest

from korapy import drive, registry
from tests.test_drive import stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_tracker import FakeTracker, reading

NOW = 2000.0
HOST = "host-1"
REVIEWER = "drive-a1b2c3d4-7"
REVIEWER_UNIT = "blue-agent-app-7-r1"
FINISHED = registry.FINISHED


def lows_record():
    return {"verdict": "approve-with-findings", "findings": [{"severity": "low", "text": "t"}]}


def normal_record():
    return {"verdict": "approve-with-findings", "findings": [{"severity": "normal", "text": "t"}]}


class FixRoundThresholdPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-fix-round-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def reviewed_store(self):
        """An open row claimed by a finished reviewer, ready to release."""
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, REVIEWER, HOST, now=NOW - 10.0)
        return store

    def run_fix(self, store, record, **fields):
        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        values = {
            "found": [("blue", [("app", 7, "fix")])],
            "policy_dir": self.policies,
            "fake_tracker": FakeTracker([reading(repo="app", number=7, updated_at=1900.0)]),
            "launcher": launcher,
            "agent_states": {REVIEWER_UNIT: (FINISHED, NOW - 5.0)},
            "agent_tickets": {REVIEWER_UNIT: ("app", 7)},
            "finished_for": {("app", 7): (REVIEWER_UNIT, "reviewer")},
            "review_reader": lambda repo, number: record,
            "now": NOW,
        }
        values.update(fields)
        code = drive_pass(store, self.lock, out, err, **values)
        return code, out.getvalue(), err.getvalue(), launcher

    def test_lows_only_release_and_launch_nothing(self):
        store = self.reviewed_store()
        code, out, err, launcher = self.run_fix(store, lows_record())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(launcher.calls, [])
        self.assertIsNone(store.claim_of("app", 7))
        self.assertIn("released finished reviewer", out)
        self.assertIn("merge-ready, skipped", out)

    def test_a_normal_finding_opens_exactly_one_fix_round(self):
        store = self.reviewed_store()
        code, out, err, launcher = self.run_fix(store, normal_record())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, title, author = launcher.calls[0]
        self.assertEqual((project, repo, number), ("blue", "app", 7))
        self.assertTrue(agent.startswith(drive.MANAGED_PREFIX))
        self.assertIn("fix on an unclaimed ticket", out)
        self.assertIn("started 1", out.splitlines()[-1])

    def test_a_start_requested_for_a_lows_only_review_launches_nothing(self):
        store = self.reviewed_store()
        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            fake_tracker=FakeTracker([reading(repo="app", number=7, updated_at=1900.0)]),
            launcher=launcher,
            agent_states={REVIEWER_UNIT: (FINISHED, NOW - 5.0)},
            agent_tickets={REVIEWER_UNIT: ("app", 7)},
            finished_for={("app", 7): (REVIEWER_UNIT, "reviewer")},
            review_reader=lambda repo, number: lows_record(),
            now=NOW,
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIn("merge-ready, skipped", out.getvalue())


class OpenPullPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-open-pull-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_start(self, store, pulls_for, **fields):
        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        values = {
            "found": [("blue", [("app", 7, "start")])],
            "policy_dir": self.policies,
            "fake_tracker": FakeTracker([reading(repo="app", number=7, updated_at=1900.0)]),
            "launcher": launcher,
            "pulls_for": pulls_for,
            "now": NOW,
        }
        values.update(fields)
        code = drive_pass(store, self.lock, out, err, **values)
        return code, out.getvalue(), err.getvalue(), launcher

    def test_a_ticket_with_an_open_pull_is_never_a_fresh_start(self):
        from korapy.eligibility import LinkedPull

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, err, launcher = self.run_start(
            store, lambda repo, number: (LinkedPull(number=9, state="open"),)
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(launcher.calls, [])
        self.assertIn("pull #9 is open, not a fresh start", out)
        self.assertIsNone(store.claim_of("app", 7))

    def test_a_ticket_whose_pull_merged_and_closed_releases_its_claim(self):
        from dataclasses import replace

        from korapy import board
        from korapy.eligibility import LinkedPull

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, REVIEWER, HOST, now=NOW - 10.0)
        current, _ = store.item("app", 7, now=NOW)
        assert current is not None
        store.upsert_item(
            replace(current, state="closed", tracker_updated_at=NOW - 5.0),
            operators=["operator"],
            now=NOW - 5.0,
        )
        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            found=[("blue", [("app", 7, "fix")])],
            policy_dir=self.policies,
            fake_tracker=FakeTracker([reading(repo="app", number=7, updated_at=1900.0)]),
            launcher=launcher,
            agent_states={REVIEWER_UNIT: (FINISHED, NOW - 5.0)},
            agent_tickets={REVIEWER_UNIT: ("app", 7)},
            finished_for={("app", 7): (REVIEWER_UNIT, "reviewer")},
            pulls_for=lambda repo, number: (LinkedPull(number=9, state="closed"),),
            now=NOW,
        )
        self.assertEqual(code, 0)
        self.assertEqual(launcher.calls, [])
        self.assertIsNone(store.claim_of("app", 7))
        self.assertIn("released finished reviewer", out.getvalue())


if __name__ == "__main__":
    unittest.main()
