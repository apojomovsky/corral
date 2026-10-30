"""The drive pass: lock, read-only gating, the candidate table. Fakes only.

The lock is a real `flock`, but only over files inside temporary
directories; the store is a temporary board store; policies are stub
modules written to a temporary directory; every host read (projects,
live state, limits, quotas, ledger, labels) is injected plain data.
No tracker, no network, no subprocess, no real state dir.
"""

import argparse
import errno
import fcntl
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from corral import admission, board, drive, launch, quota_ledger
from corral import projects as projects_mod


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


def policy_source(wants):
    """A stub policy module naming the given `(repo, number, kind)` wants."""
    lines = [
        "def eligible(rows, *, now):",
        "    from corral.eligibility import StartRequest",
        "    return [",
    ]
    for repo, number, kind in wants:
        lines.append(f"        StartRequest({repo!r}, {number!r}, {kind!r}),")
    lines.append("    ]")
    return "\n".join(lines) + "\n"


def project_named(name, entry=None, root=None):
    """A registered project, optionally naming a policy entry."""
    return projects_mod.Project(
        name=name,
        unit_prefix=name + "-agent",
        root=root or os.path.join(tempfile.gettempdir(), "corral-drive-ops", name),
        eligibility=entry,
    )


def hermetic_pass(store, lock_path, out, err, plan=True, found=(), policy_dir=None, **fields):
    """`run_pass` with every host read injected: no outside world."""
    if policy_dir is not None and found:
        entries = []
        for name, wants in found:
            path = os.path.join(policy_dir, name + ".py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(policy_source(wants))
            entries.append(project_named(name, path + ":eligible"))
        found = entries
    values = {
        "plan": plan,
        "store": store,
        "lock_path": lock_path,
        "out": out,
        "err": err,
        "now": 2000.0,
        "projects_fn": lambda: list(found),
        "live": frozenset(),
        "limits": admission.Limits(total=4),
        "quotas": {},
        "ledger": {},
        "read_labels": lambda kind, repo, number: [],
        "hold_label": "hold",
        "present_agents": frozenset(),
        "live_units": frozenset(),
        "gone_s": 3600.0,
    }
    values.update(fields)
    return drive.run_pass(**values)


class RenderTest(unittest.TestCase):
    def test_rows_print_with_verdict_and_reason_plus_summary(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="start",
                    reason="fresh",
                ),
                drive.Candidate(
                    project="red",
                    repo="app",
                    number=9,
                    kind="start",
                    verdict="skip",
                    reason="held",
                ),
            ],
            label="plan",
        )
        self.assertIn("blue app#7: start (fresh)", text)
        self.assertIn("red app#9: skip (held)", text)
        self.assertTrue(text.splitlines()[-1].startswith("plan:"))
        self.assertIn("2 candidate(s)", text)
        self.assertIn("would start 1", text)

    def test_rows_without_a_project_print_bare(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="skip",
                    reason="withdrawn by this pass",
                ),
            ],
            label="plan",
        )
        self.assertIn("app#7: skip (withdrawn by this pass)", text)

    def test_summary_counts_each_proceeding_kind_separately(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=1,
                    kind="start",
                    verdict="start",
                    reason="fresh",
                ),
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=2,
                    kind="resume",
                    verdict="resume",
                    reason="back",
                ),
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=3,
                    kind="review",
                    verdict="review",
                    reason="slot",
                ),
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=4,
                    kind="fix",
                    verdict="fix",
                    reason="round",
                ),
                drive.Candidate(
                    project="red",
                    repo="app",
                    number=5,
                    kind="resume",
                    verdict="skip",
                    reason="held",
                ),
            ],
            label="plan",
        )
        self.assertIn("5 candidate(s)", text)
        self.assertIn("would start 1", text)
        self.assertIn("would resume 1", text)
        self.assertIn("would review 1", text)
        self.assertIn("would fix 1", text)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_pass(self, store, plan=True, found=(), **fields):
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store, self.lock, out, err, plan=plan, found=found, policy_dir=self.policies, **fields
        )
        return code, out.getvalue(), err.getvalue()

    def test_plan_prints_gated_verdict_rows_and_exits_zero(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "closed"), ("api", 2, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("app#7: start", out)
        self.assertNotIn("app#9", out)
        self.assertNotIn("api#2", out)
        self.assertIn("would start 1", out)

    def test_plan_with_no_projects_prints_an_empty_table(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertNotIn("app#7", out)
        self.assertIn("0 candidate(s)", out)

    def test_plan_leaves_store_and_outbox_untouched(self):
        store = stored(self.db, [("app", 7, "open")])
        store.enqueue("app", 7, "status", "working", now=1500.0)
        store.close()
        with open(self.db, "rb") as handle:
            before_bytes = handle.read()
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, _, _ = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        store.close()
        with open(self.db, "rb") as handle:
            after_bytes = handle.read()
        self.assertEqual(before_bytes, after_bytes)
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        self.assertEqual(len(store.outbox_rows()), 1)

    def test_plan_on_empty_store_exits_zero(self):
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, out, _ = self.run_pass(store)
        self.assertEqual(code, 0)
        self.assertIn("0 candidate", out)

    def test_plan_never_takes_the_lock(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with open(self.lock, "w") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            code, out, err = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("app#7: start", out)

    def test_non_plan_pass_takes_and_releases_the_lock(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store, plan=False, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("app#7: start", out)
        with open(self.lock, "r") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_second_concurrent_non_plan_pass_exits_two(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with open(self.lock, "w") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            code, _, err = self.run_pass(store, plan=False)
        self.assertEqual(code, 2)
        self.assertIn("already running", err)

    def test_lock_setup_failure_is_exit_four(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w") as handle:
            handle.write("not a directory")
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            os.path.join(blocker, "drive.lock"),
            out,
            err,
            plan=False,
        )
        self.assertEqual(code, 4)
        self.assertIn("corral drive:", err.getvalue())
        self.assertNotIn("already running", err.getvalue())

    def test_corrupt_row_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        store.close()
        raw = sqlite3.connect(self.db)
        try:
            raw.execute("INSERT INTO items(repo, number, labels) VALUES('app', 1, 'bogus')")
            raw.commit()
        finally:
            raw.close()
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, _, err = self.run_pass(store)
        self.assertEqual(code, 4)
        self.assertIn("corral drive:", err)

    def test_raw_sqlite_error_is_exit_four(self):
        class BrokenStore:
            def items(self, **kwargs):
                raise sqlite3.OperationalError("boom")

        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(BrokenStore(), self.lock, out, err)
        self.assertEqual(code, 4)
        self.assertIn("corral drive:", err.getvalue())

    def test_plan_summary_says_plan(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, _ = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[-1].startswith("plan:"), out)

    def test_locking_pass_summary_says_pass(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, _ = self.run_pass(store, plan=False, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[-1].startswith("pass:"), out)

    def test_non_contention_flock_error_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        real = drive._flock

        def broken(_fd: int, _op: int) -> None:
            raise OSError(errno.ENOLCK, "No locks available")

        drive._flock = broken
        try:
            code, _, err = self.run_pass(store, plan=False)
        finally:
            drive._flock = real
        self.assertEqual(code, 4)
        self.assertIn("corral drive:", err)
        self.assertNotIn("already running", err)

    def test_unreadable_live_state_is_exit_three(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        kept = {k: v for k, v in os.environ.items() if not k.startswith("CORRAL_")}
        env = dict(
            kept,
            CORRAL_CONFIG=os.path.join(self.tmp.name, "config"),
            CORRAL_STATE=os.path.join(self.tmp.name, "state"),
        )

        def broken(*args, **kwargs):
            raise RuntimeError("unit listing unreachable")

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(launch, "admission_state", broken),
        ):
            out, err = io.StringIO(), io.StringIO()
            code = hermetic_pass(store, self.lock, out, err, projects_fn=lambda: [], live=None)
        self.assertEqual(code, 3)
        self.assertIn("cannot read live state", err.getvalue())

    def test_corrupt_ledger_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with mock.patch.object(
            quota_ledger,
            "load",
            side_effect=quota_ledger.LedgerError("quota ledger is corrupt"),
        ):
            out, err = io.StringIO(), io.StringIO()
            code = hermetic_pass(store, self.lock, out, err, projects_fn=lambda: [], ledger=None)
        self.assertEqual(code, 4)
        self.assertIn("corral drive:", err.getvalue())

    def test_project_registry_load_failure_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)

        def broken():
            raise projects_mod.ProjectsError("projects.d/blue.toml: boom")

        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(store, self.lock, out, err, projects_fn=broken)
        self.assertEqual(code, 4)
        self.assertIn("blue.toml", err.getvalue())


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
