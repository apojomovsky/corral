"""The drive skeleton: lock, read-only `--plan`, candidate table. Fakes only.

The lock is a real `flock`, but only over files inside temporary
directories, and the store is a temporary board store. No tracker, no
network, no subprocess, no real state dir.
"""

import argparse
import fcntl
import io
import os
import tempfile
import unittest

from corral import board, drive


def stored(path, items):
    """A board store holding the given `(repo, number, state)` rows."""
    store = board.open_store(path)
    for repo, number, state in items:
        store.upsert_item(
            board.Item(
                repo=repo,
                number=number,
                kind="issue",
                title=f"Title {number}",
                body="Body",
                state=state,
                author="operator",
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
    return store


class CandidatesTest(unittest.TestCase):
    def test_open_items_become_no_policy_candidates_in_order(self):
        rows = drive.candidates(
            [
                board.Item(repo="app", number=9, state="open"),
                board.Item(repo="app", number=3, state="closed"),
                board.Item(repo="app", number=7, state="open"),
            ]
        )
        self.assertEqual(
            [(row.repo, row.number, row.verdict) for row in rows],
            [("app", 7, "no policy"), ("app", 9, "no policy")],
        )

    def test_no_open_items_means_no_candidates(self):
        self.assertEqual(drive.candidates([board.Item(repo="app", number=1, state="closed")]), [])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")

    def run_pass(self, store, plan=True):
        out, err = io.StringIO(), io.StringIO()
        code = drive.run_pass(plan=plan, store=store, lock_path=self.lock, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_plan_prints_verdict_rows_and_exits_zero(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "closed"), ("api", 2, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("api#2: no policy", out)
        self.assertIn("app#7: no policy", out)
        self.assertNotIn("#9", out)
        self.assertLess(out.index("api#2"), out.index("app#7"))

    def test_plan_leaves_store_and_outbox_untouched(self):
        store = stored(self.db, [("app", 7, "open")])
        store.enqueue("app", 7, "status", "working", now=1500.0)
        before_rows = [
            (row.repo, row.number, row.field, row.value, row.state) for row in store.outbox_rows()
        ]
        store.close()
        with open(self.db, "rb") as handle:
            before_bytes = handle.read()
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, _, _ = self.run_pass(store)
        self.assertEqual(code, 0)
        store.close()
        with open(self.db, "rb") as handle:
            after_bytes = handle.read()
        self.assertEqual(before_bytes, after_bytes)
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        after_rows = [
            (row.repo, row.number, row.field, row.value, row.state) for row in store.outbox_rows()
        ]
        self.assertEqual(before_rows, after_rows)
        self.assertEqual(len(after_rows), 1)

    def test_plan_on_empty_store_exits_zero(self):
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, out, _ = self.run_pass(store)
        self.assertEqual(code, 0)
        self.assertIn("0 candidate", out)

    def test_pass_releases_the_lock(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, _ = self.run_pass(store)
        self.assertEqual(code, 0)
        with open(self.lock, "r") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_second_concurrent_pass_exits_two(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with open(self.lock, "w") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            code, _, err = self.run_pass(store)
        self.assertEqual(code, 2)
        self.assertIn("already running", err)


class LockPathTest(unittest.TestCase):
    def test_lock_path_comes_from_the_state_dir(self):
        tmp = tempfile.TemporaryDirectory(prefix="corral-drive-state-")
        self.addCleanup(tmp.cleanup)
        old = os.environ.get("CORRAL_STATE")
        os.environ["CORRAL_STATE"] = tmp.name
        try:
            from corral import paths

            self.assertEqual(drive.lock_path(), os.path.join(paths.state_dir(), "drive.lock"))
            self.assertEqual(drive.lock_path(), os.path.join(tmp.name, "drive.lock"))
        finally:
            if old is None:
                del os.environ["CORRAL_STATE"]
            else:
                os.environ["CORRAL_STATE"] = old


class CmdDriveTest(unittest.TestCase):
    def test_unreadable_store_is_exit_four(self):
        tmp = tempfile.TemporaryDirectory(prefix="corral-drive-cmd-")
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "board.sqlite"), "w") as handle:
            handle.write("not a database")
        old = os.environ.get("CORRAL_STATE")
        os.environ["CORRAL_STATE"] = tmp.name
        try:
            code = drive.cmd_drive(argparse.Namespace(plan=True))
        finally:
            if old is None:
                del os.environ["CORRAL_STATE"]
            else:
                os.environ["CORRAL_STATE"] = old
        self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
