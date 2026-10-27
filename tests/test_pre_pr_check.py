"""The takeoff ritual (`scripts/pre_pr_check.py`) on scratch repos."""

import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from .test_hooks import clean_env

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pre_pr_check.py"
_spec = importlib.util.spec_from_file_location("pre_pr_check", SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

GOOD_BODY = "Closes #7\n\nWhat and why.\n\n## Review\n\nReviewer: reviewer-7. No findings.\n"


class Body(unittest.TestCase):
    def test_a_good_body_passes(self):
        self.assertEqual(check.check_body(GOOD_BODY, "feat/7-thing"), [])

    def test_a_literal_newline_is_refused(self):
        found = check.check_body(GOOD_BODY + "a\\nb", "feat/7-thing")
        self.assertTrue(any("literal" in f for f in found))

    def test_a_literal_newline_inside_code_passes(self):
        self.assertEqual(check.check_body(GOOD_BODY + '`--body "a\\nb"`', "feat/7-thing"), [])

    def test_the_branch_ticket_must_be_linked(self):
        found = check.check_body(GOOD_BODY.replace("#7", "#8"), "feat/7-thing")
        self.assertTrue(any("does not link #7" in f for f in found))

    def test_refs_counts_as_a_link(self):
        self.assertEqual(check.check_body(GOOD_BODY.replace("Closes", "Refs"), "feat/7-thing"), [])

    def test_an_empty_review_section_is_refused(self):
        found = check.check_body("Closes #7\n\n## Review\n\n", "feat/7-thing")
        self.assertTrue(any("## Review" in f for f in found))

    def test_a_missing_review_section_is_refused(self):
        found = check.check_body("Closes #7\n", "feat/7-thing")
        self.assertTrue(any("## Review" in f for f in found))


class Tree(unittest.TestCase):
    """A main clone with origin/master and a ticket worktree under .worktrees/."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = clean_env()
        self.patch_env()
        root = Path(tmp.name)
        self.remote, self.main = root / "remote.git", root / "main"
        self.run_git(root, "init", "-q", "--bare", str(self.remote))
        self.run_git(root, "init", "-q", "-b", "master", str(self.main))
        self.commit(self.main, "chore: base")
        self.run_git(self.main, "remote", "add", "origin", str(self.remote))
        self.run_git(self.main, "push", "-q", "origin", "master")
        self.tree = self.main / ".worktrees" / "7-thing"
        self.run_git(
            self.main,
            "worktree",
            "add",
            "-q",
            "-b",
            "feat/7-thing",
            str(self.tree),
            "origin/master",
        )
        self.body = root / "body.md"
        self.body.write_text(GOOD_BODY)

    def patch_env(self):
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def run_git(self, cwd, *args):
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def commit(self, cwd, message, name="f", text=None):
        (Path(cwd) / name).write_text(text or message)
        self.run_git(cwd, "add", name)
        self.run_git(cwd, "commit", "-q", "--no-verify", "-m", message)

    def takeoff(self, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(["--body", str(self.body), "--cwd", str(cwd or self.tree)])
        return code, err.getvalue()

    def test_a_clean_ticket_worktree_passes(self):
        self.commit(self.tree, "feat(x): add the thing", name="g")
        self.assertEqual(self.takeoff(), (0, ""))

    def test_the_main_clone_is_refused(self):
        self.run_git(self.main, "checkout", "-q", "-b", "feat/8-other")
        code, err = self.takeoff(self.main)
        self.assertEqual(code, 1)
        self.assertIn("not a worktree under .worktrees/", err)

    def test_a_dirty_tree_is_refused(self):
        (self.tree / "loose").write_text("x")
        code, err = self.takeoff()
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", err)

    def test_a_tree_behind_master_is_refused(self):
        self.commit(self.main, "chore: moves on", name="h")
        self.run_git(self.main, "push", "-q", "origin", "master")
        self.run_git(self.tree, "fetch", "-q", "origin")
        code, err = self.takeoff()
        self.assertEqual(code, 1)
        self.assertIn("behind origin/master", err)

    def test_a_bad_commit_subject_is_refused(self):
        self.commit(self.tree, "added a thing", name="g")
        code, err = self.takeoff()
        self.assertEqual(code, 1)
        self.assertIn("subject must be", err)

    def test_an_em_dash_in_an_added_line_is_refused(self):
        self.commit(self.tree, "docs: add a line", name="g", text="one — two\n")
        code, err = self.takeoff()
        self.assertEqual(code, 1)
        self.assertIn("em-dash in an added line", err)

    def test_a_missing_body_is_a_usage_error(self):
        self.body.unlink()
        self.assertEqual(self.takeoff()[0], 4)


if __name__ == "__main__":
    unittest.main()
