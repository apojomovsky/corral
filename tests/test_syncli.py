"""`corral sync`: feed the board store from the tracker. Fakes only."""

import argparse
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from corral import board, cli, settings, sync, syncli, tracker
from tests.test_tracker import FakeTracker, reading


REPO = "acme/app"
T0 = 1_800_000_100.0


class Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class FakeGit:
    """A git seam double: records calls, answers the origin lookup."""

    def __init__(self, stdout="git@github.com:acme/app.git\n", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list = []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        return Proc(self.returncode, self.stdout)


def git_ok(url="git@github.com:acme/app.git\n"):
    return FakeGit(stdout=url)


def git_dead():
    return FakeGit(stdout="", returncode=1)


TOML = """\
[[repo]]
name = "app"
clone.path = "clone"
"""


class SyncRootCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-synccli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.store = board.open_store(os.path.join(self.root, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_sync(self, fake, names=(), git=None, **over):
        params = {
            "resync": False,
            "audit": None,
            "as_json": False,
            "tracker": fake,
            "store": self.store,
            "git": git if git is not None else git_ok(),
            "now": T0,
            "out": self.out,
            "err": self.err,
        }
        params.update(over)
        return syncli.sync_root(self.root, list(names), **params)


class ExitCodeTest(SyncRootCase):
    def test_synced_items_exit_0(self):
        fake = FakeTracker([reading(number=1, updated_at=T0)])
        code = self.run_sync(fake)
        self.assertEqual(code, 0)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.title, "Title")
        self.assertEqual(
            self.store.get_cursor(REPO, sync.CURSOR_NAME),
            tracker.format_cursor(T0),
        )

    def test_empty_feed_exits_1(self):
        self.assertEqual(self.run_sync(FakeTracker()), 1)

    def test_tracker_down_exits_3(self):
        fake = FakeTracker()
        fake.changes_error = "connection refused"
        self.assertEqual(self.run_sync(fake), 3)

    def test_partial_errors_still_exit_0(self):
        fake = FakeTracker(
            [
                reading(number=1, updated_at=T0 - 50),
                reading(number=2, updated_at=T0),
            ]
        )
        fake.items_break_after = 1
        code = self.run_sync(fake, resync=True)
        self.assertEqual(code, 0)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.number, 1)

    def test_total_resync_failure_exits_3(self):
        fake = FakeTracker([reading(number=1, updated_at=T0)])
        fake.items_break_after = 0
        self.assertEqual(self.run_sync(fake, resync=True), 3)

    def test_unknown_repo_exits_4(self):
        self.assertEqual(self.run_sync(FakeTracker(), names=["nope"]), 4)

    def test_no_repo_entries_exits_4(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write("")
        self.assertEqual(self.run_sync(FakeTracker()), 4)


class WiringTest(SyncRootCase):
    def test_tracker_repo_setting_skips_origin(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML + 'tracker.repo = "acme/other"\n')
        git = git_ok()
        fake = FakeTracker([reading(repo="acme/other", number=1, updated_at=T0)])
        code = self.run_sync(fake, git=git)
        self.assertEqual(code, 0)
        self.assertEqual(git.calls, [])
        row, _ = self.store.item("acme/other", 1, now=T0)
        self.assertIsNotNone(row)

    def test_origin_fallback_names_tracker_repo(self):
        fake = FakeTracker([reading(number=1, updated_at=T0)])
        self.assertEqual(self.run_sync(fake), 0)

    def test_missing_origin_exits_4(self):
        self.assertEqual(self.run_sync(FakeTracker(), git=git_dead()), 4)
        self.assertIn("origin", self.err.getvalue())

    def test_operators_default_to_owner_only(self):
        fake = FakeTracker(
            [
                reading(number=1, author="acme", updated_at=T0),
                reading(number=2, author="mallory", updated_at=T0),
            ]
        )
        self.assertEqual(self.run_sync(fake), 0)
        owned, _ = self.store.item(REPO, 1, now=T0)
        self.assertFalse(owned.external)
        stranger, _ = self.store.item(REPO, 2, now=T0)
        self.assertTrue(stranger.external)

    def test_operators_setting_names_trusted_logins(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML + 'tracker.operators = ["mallory"]\n')
        fake = FakeTracker([reading(number=2, author="mallory", updated_at=T0)])
        self.assertEqual(self.run_sync(fake), 0)
        row, _ = self.store.item(REPO, 2, now=T0)
        self.assertFalse(row.external)

    def test_resync_marks_absent_dirty(self):
        self.store.upsert_item(
            board.Item(repo=REPO, number=5, tracker_updated_at=T0 - 200, author="acme"),
            operators=["acme"],
            now=T0 - 200,
        )
        fake = FakeTracker([reading(number=1, author="acme", updated_at=T0)])
        self.assertEqual(self.run_sync(fake, resync=True), 0)
        row, _ = self.store.item(REPO, 5, now=T0)
        self.assertEqual(row.confidence, board.DIRTY)

    def test_resync_conflict_prints_on_stderr_without_changing_exit(self):
        self.store.upsert_item(
            board.Item(repo=REPO, number=1, tracker_updated_at=T0 - 200, author="acme"),
            operators=["acme"],
            now=T0 - 200,
        )
        self.store.claim(REPO, 1, "agent-1", "host-1", now=T0 - 100)
        fake = FakeTracker([reading(number=1, author="acme", agent="agent-2", updated_at=T0)])
        self.assertEqual(self.run_sync(fake, resync=True), 0)
        self.assertIn(f"{REPO}#1: store=agent-1 tracker=agent-2", self.err.getvalue())

    def test_resync_conflict_appears_in_json(self):
        self.store.upsert_item(
            board.Item(repo=REPO, number=1, tracker_updated_at=T0 - 200, author="acme"),
            operators=["acme"],
            now=T0 - 200,
        )
        self.store.claim(REPO, 1, "agent-1", "host-1", now=T0 - 100)
        fake = FakeTracker([reading(number=1, author="acme", agent="agent-2", updated_at=T0)])
        self.assertEqual(self.run_sync(fake, resync=True, as_json=True), 0)
        doc = json.loads(self.out.getvalue())
        self.assertEqual(
            doc["repos"]["app"]["conflicts"],
            [f"{REPO}#1: store=agent-1 tracker=agent-2"],
        )

    def test_audit_rereads_only_limit(self):
        for number in (1, 2, 3):
            self.store.upsert_item(
                board.Item(
                    repo=REPO,
                    number=number,
                    tracker_updated_at=T0 - 200,
                    author="acme",
                ),
                operators=["acme"],
                now=T0 - 200 + number,
            )
        fake = FakeTracker(
            [reading(number=number, author="acme", updated_at=T0) for number in (1, 2, 3)]
        )
        self.assertEqual(self.run_sync(fake, audit=2), 0)
        self.assertEqual(len(fake.calls), 2)

    def test_json_output_parses(self):
        fake = FakeTracker([reading(number=1, updated_at=T0)])
        self.assertEqual(self.run_sync(fake, as_json=True), 0)
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["repos"]["app"]["seen"], 1)
        self.assertEqual(doc["repos"]["app"]["errors"], [])
        self.assertEqual(doc["seen"], 1)

    def test_json_reports_tracker_errors(self):
        fake = FakeTracker()
        fake.changes_error = "connection refused"
        self.assertEqual(self.run_sync(fake, as_json=True), 3)
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["seen"], 0)
        self.assertTrue(doc["repos"]["app"]["errors"])


class ParserTest(unittest.TestCase):
    def test_sync_is_wired_with_expected_flags(self):
        args = cli.build_parser().parse_args(["sync"])
        self.assertEqual(args.func.__name__, "cmd_sync")
        self.assertEqual(args.repo, [])
        self.assertFalse(args.resync)
        self.assertIsNone(args.audit)
        self.assertFalse(args.json)

    def test_resync_and_audit_are_exclusive(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["sync", "--resync", "--audit", "3"])

    def test_cmd_sync_without_root_exits_4(self):
        empty = tempfile.mkdtemp(prefix="corral-sync-noroot-")
        self.addCleanup(lambda: os.rmdir(empty))
        namespace = argparse.Namespace(
            repo=[], resync=False, audit=None, json=False, func=syncli.cmd_sync
        )
        with mock.patch("os.getcwd", return_value=empty):
            self.assertEqual(syncli.cmd_sync(namespace), 4)

    def test_cmd_sync_rejects_bad_audit_limit(self):
        with tempfile.TemporaryDirectory(prefix="corral-sync-root-") as root:
            with open(os.path.join(root, "corral.toml"), "w", encoding="utf-8") as handle:
                handle.write(TOML)
            namespace = argparse.Namespace(
                repo=[], resync=False, audit=0, json=False, func=syncli.cmd_sync
            )
            with mock.patch("os.getcwd", return_value=root):
                self.assertEqual(syncli.cmd_sync(namespace), 4)


class ProvidersTest(unittest.TestCase):
    def test_sync_registers_tracker_settings(self):
        self.assertIn("corral.sync", settings.PROVIDERS)
        self.assertEqual(settings.REGISTRY["tracker.repo"].default, "")
        self.assertEqual(settings.REGISTRY["tracker.operators"].default, [])


class RepoOperatorsTest(SyncRootCase):
    def test_configured_operators_resolve_per_entry(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML + 'tracker.operators = ["mallory"]\n')
        entries, problem = syncli.repo_operators(self.root, ["app"], git_ok())
        self.assertIsNone(problem)
        self.assertEqual(entries, {"app": ("acme/app", ["mallory"])})

    def test_missing_operators_default_to_the_owner(self):
        entries, problem = syncli.repo_operators(self.root, ["app"], git_ok())
        self.assertIsNone(problem)
        self.assertEqual(entries, {"app": ("acme/app", ["acme"])})

    def test_a_bad_entry_returns_the_problem(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML + 'tracker.repo = "not-a-repo"\n')
        entries, problem = syncli.repo_operators(self.root, ["app"], git_ok())
        self.assertIsNone(entries)
        self.assertIn("not-a-repo", problem)


if __name__ == "__main__":
    unittest.main()
