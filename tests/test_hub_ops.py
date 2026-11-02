"""H4 operator verbs: remove-host, release, rotate-token. Fakes and loopback only.

Covers ticket H4 of docs/08-hub-multihost.md section 10: `remove-host`
bumps the membership epoch and releases that host's claims with epochs
kept for the next mint, per-ticket `release` force-releases one claim
as an operator override, and `rotate-token` revokes and re-mints one
host's token. Every test uses a temp hub database; no test touches the
network, a real state dir, or another project.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from corral import hub, paths


def temp_db():
    tmp = tempfile.TemporaryDirectory(prefix="corral-hub-ops-")
    return tmp, os.path.join(tmp.name, "hub.sqlite")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        self.store = hub.open_db(self.db)
        self.addCleanup(self.store.close)

    def add_host(self, host_id="host-1"):
        return hub.add_host(self.store, host_id)

    def claim_row(self, repo="app", number=7):
        return self.store.execute(
            "SELECT agent, host_id, epoch, lease_expires FROM claims WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()

    def membership_epoch(self):
        return self.store.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()[
            0
        ]


class MembershipEpochTest(StoreCase):
    def test_starts_at_zero(self):
        self.assertEqual(hub.membership_epoch(self.store), 0)

    def test_matches_the_meta_row(self):
        self.assertEqual(hub.membership_epoch(self.store), self.membership_epoch())


class RemoveHostTest(StoreCase):
    def test_remove_bumps_epoch_and_releases_only_that_hosts_claims(self):
        token = self.add_host("host-1")
        self.add_host("host-2")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.grant(self.store, "app", 8, "drive-bbb", "host-2", now=1000.0, lease_ttl_s=90)

        epoch = hub.remove_host(self.store, "host-1", now=2000.0)

        self.assertEqual(epoch, 1)
        self.assertEqual(hub.membership_epoch(self.store), 1)
        agent, host, kept, _ = self.claim_row("app", 7)
        self.assertFalse(agent)
        self.assertFalse(host)
        self.assertEqual(kept, 1)
        agent, host, kept, _ = self.claim_row("app", 8)
        self.assertEqual((agent, host, kept), ("drive-bbb", "host-2", 1))
        self.assertIsNone(hub.verify_token(self.store, token))

    def test_remove_twice_bumps_once_then_refuses(self):
        self.add_host("host-1")
        self.assertEqual(hub.remove_host(self.store, "host-1", now=2000.0), 1)
        with self.assertRaises(hub.HubError):
            hub.remove_host(self.store, "host-1", now=2001.0)
        self.assertEqual(hub.membership_epoch(self.store), 1)

    def test_remove_unknown_host_is_refused(self):
        with self.assertRaises(hub.HubError):
            hub.remove_host(self.store, "host-9", now=2000.0)
        self.assertEqual(hub.membership_epoch(self.store), 0)

    def test_remove_without_claims_still_bumps(self):
        self.add_host("host-1")
        self.assertEqual(hub.remove_host(self.store, "host-1", now=2000.0), 1)

    def test_removed_host_rejoins_as_new_with_fencing_intact(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.remove_host(self.store, "host-1", now=2000.0)

        fresh = hub.add_host(self.store, "host-1")
        self.assertEqual(hub.verify_token(self.store, fresh), "host-1")
        receipt = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=2001.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 2)
        self.assertEqual(hub.membership_epoch(self.store), 1)

    def test_remove_is_logged_without_the_token(self):
        token = self.add_host("host-1")
        with self.assertLogs("corral.hub", level="INFO") as logs:
            hub.remove_host(self.store, "host-1", now=2000.0)
        self.assertTrue(any("host-1" in line for line in logs.output))
        self.assertFalse(any(token in line for line in logs.output))


class ReleaseClaimTest(StoreCase):
    def test_release_clears_holder_and_keeps_epoch(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)

        kept = hub.release_claim(self.store, "app", 7, now=2000.0, reason="stuck unit")

        self.assertEqual(kept, 1)
        agent, host, epoch, _ = self.claim_row("app", 7)
        self.assertFalse(agent)
        self.assertFalse(host)
        self.assertEqual(epoch, 1)

    def test_next_grant_after_release_fences(self):
        self.add_host("host-1")
        self.add_host("host-2")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.release_claim(self.store, "app", 7, now=2000.0, reason="stuck unit")

        receipt = hub.grant(self.store, "app", 7, "drive-bbb", "host-2", now=2001.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 2)

    def test_release_leaves_other_tickets_alone(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.grant(self.store, "app", 8, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.release_claim(self.store, "app", 7, now=2000.0, reason="stuck unit")
        agent, _, epoch, _ = self.claim_row("app", 8)
        self.assertEqual((agent, epoch), ("drive-aaa", 1))

    def test_release_unknown_ticket_is_refused(self):
        self.add_host("host-1")
        with self.assertRaises(hub.HubError):
            hub.release_claim(self.store, "app", 7, now=2000.0, reason="typo")

    def test_release_twice_is_refused(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.release_claim(self.store, "app", 7, now=2000.0, reason="stuck unit")
        with self.assertRaises(hub.HubError):
            hub.release_claim(self.store, "app", 7, now=2001.0, reason="again")

    def test_release_rejects_bad_shapes(self):
        self.add_host("host-1")
        with self.assertRaises(hub.HubError):
            hub.release_claim(self.store, "", 7, now=2000.0)
        with self.assertRaises(hub.HubError):
            hub.release_claim(self.store, "app", 0, now=2000.0)

    def test_release_logs_the_reason(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        with self.assertLogs("corral.hub", level="INFO") as logs:
            hub.release_claim(self.store, "app", 7, now=2000.0, reason="stuck unit")
        self.assertTrue(any("stuck unit" in line for line in logs.output))


class RotateTokenTest(StoreCase):
    def test_rotate_replaces_the_token_and_kills_the_old_one(self):
        old = self.add_host("host-1")

        new = hub.rotate_token(self.store, "host-1")

        self.assertTrue(new)
        self.assertNotEqual(new, old)
        self.assertIsNone(hub.verify_token(self.store, old))
        self.assertEqual(hub.verify_token(self.store, new), "host-1")

    def test_rotate_leaves_other_hosts_alone(self):
        self.add_host("host-1")
        other = self.add_host("host-2")
        hub.rotate_token(self.store, "host-1")
        self.assertEqual(hub.verify_token(self.store, other), "host-2")

    def test_rotate_unknown_host_is_refused(self):
        with self.assertRaises(hub.HubError):
            hub.rotate_token(self.store, "host-9")

    def test_rotated_token_never_touches_the_logs(self):
        self.add_host("host-1")
        with self.assertLogs("corral.hub", level="INFO") as logs:
            new = hub.rotate_token(self.store, "host-1")
        self.assertTrue(any("host-1" in line for line in logs.output))
        self.assertFalse(any(new in line for line in logs.output))


class OperatorCommandTest(unittest.TestCase):
    def parse(self, *argv):
        from corral import cli

        return cli.build_parser().parse_args(["hub", *argv])

    def run_hub(self, *argv):
        from unittest import mock

        args = self.parse(*argv)
        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                return args.func(args), state

    def seed(self, state, host_id="host-1"):
        store = hub.open_db(paths.hub_db_path({"CORRAL_HUB_STATE": state}))
        try:
            token = hub.add_host(store, host_id)
            receipt = hub.grant(store, "app", 7, "drive-aaa", host_id, now=1000.0, lease_ttl_s=90)
        finally:
            store.close()
        return token, receipt

    def test_remove_host_verb_bumps_and_second_remove_is_usage(self):
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                self.seed(state)
                args = self.parse("remove-host", "--id", "host-1")
                self.assertEqual(args.func(args), 0)
                again = self.parse("remove-host", "--id", "host-1")
                self.assertEqual(again.func(again), 4)

    def test_release_verb_frees_the_ticket(self):
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                self.seed(state)
                args = self.parse("release", "app", "7", "--reason", "stuck unit")
                self.assertEqual(args.func(args), 0)
                store = hub.open_db(paths.hub_db_path())
                try:
                    row = store.execute(
                        "SELECT agent, epoch FROM claims WHERE repo=? AND number=?",
                        ("app", 7),
                    ).fetchone()
                finally:
                    store.close()
                self.assertEqual(tuple(row), ("", 1))

    def test_release_unknown_ticket_is_usage(self):
        code, _ = self.run_hub("release", "app", "7")
        self.assertEqual(code, 4)

    def test_release_rejects_a_bad_number(self):
        code, _ = self.run_hub("release", "app", "0")
        self.assertEqual(code, 4)

    def test_rotate_token_verb_prints_a_working_token_once(self):
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                old, _ = self.seed(state)
                args = self.parse("rotate-token", "--id", "host-1")
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(args.func(args), 0)
                fresh = out.getvalue().strip()
                self.assertTrue(fresh)
                self.assertNotEqual(fresh, old)
                store = hub.open_db(paths.hub_db_path())
                try:
                    self.assertEqual(hub.verify_token(store, fresh), "host-1")
                    self.assertIsNone(hub.verify_token(store, old))
                finally:
                    store.close()

    def test_rotate_unknown_host_is_usage(self):
        code, _ = self.run_hub("rotate-token", "--id", "host-9")
        self.assertEqual(code, 4)

    def test_open_store_failures_are_usage_errors_with_stderr(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        from corral import paths

        for argv in (
            ("remove-host", "--id", "host-1"),
            ("release", "app", "7"),
            ("rotate-token", "--id", "host-1"),
        ):
            with self.subTest(argv=argv):
                args = self.parse(*argv)
                err = io.StringIO()
                with mock.patch.object(
                    paths, "hub_db_path", side_effect=paths.PathError("no state")
                ):
                    with redirect_stderr(err):
                        code = args.func(args)
                self.assertEqual(code, 4)
                self.assertTrue(err.getvalue())


if __name__ == "__main__":
    unittest.main()
