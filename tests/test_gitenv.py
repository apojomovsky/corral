"""Tests for corral.gitenv and its hook-env regression guard.

A process born inside another git operation inherits that repo's GIT_*
vars, and subprocess inherits them by default: a fixture's `git init`
then re-initializes the outer repo instead of its own cwd. Observed once
against the live checkout, where it flipped the shared core.bare to true
and broke every worktree plus the live CLI.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from corral.gitenv import clean_env


class CleanEnvTest(unittest.TestCase):
    def test_drops_every_git_prefix_key_and_keeps_the_rest(self):
        env = {
            "GIT_DIR": "/x",
            "GIT_WORK_TREE": "/y",
            "GIT_INDEX_FILE": "/z",
            "GIT_PREFIX": "",
            "GIT_AUTHOR_NAME": "o",
            "PATH": "/bin",
            "HOME": "/h",
        }
        self.assertEqual(clean_env(env), {"PATH": "/bin", "HOME": "/h"})

    def test_empty_mapping_stays_empty(self):
        self.assertEqual(clean_env({}), {})

    def test_defaults_to_the_process_environment(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "/x", "KEPT": "1"}):
            got = clean_env()
        self.assertNotIn("GIT_DIR", got)
        self.assertEqual(got["KEPT"], "1")

    def test_result_is_a_copy(self):
        env = {"GIT_DIR": "/x", "A": "1"}
        clean_env(env)["A"] = "mutated"
        self.assertEqual(env["A"], "1")


@unittest.skipUnless(shutil.which("git"), "git not on PATH")
class HookEnvLeakTest(unittest.TestCase):
    """The incident mechanism against a sacrificial outer repo.

    Each test points hook-like GIT_* vars at a throwaway repo, then runs
    the exact write commands a fixture issues through clean_env(), and
    asserts the outer repo's config is byte-identical afterwards.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outer = os.path.join(self.tmp.name, "outer")
        os.makedirs(self.outer)
        self._git("init", "-q", "-b", "master", cwd=self.outer)
        self._git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
            cwd=self.outer,
        )
        self.config_path = os.path.join(self.outer, ".git", "config")
        with open(self.config_path, "rb") as f:
            self.before = f.read()
        self.assertIn(b"bare = false", self.before)

    def _git(self, *args, cwd):
        subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=clean_env()
        )

    def _hook_env(self):
        return {
            "GIT_DIR": os.path.join(self.outer, ".git"),
            "GIT_INDEX_FILE": os.path.join(self.outer, ".git", "index"),
            "GIT_WORK_TREE": self.outer,
            "GIT_PREFIX": "",
        }

    def _outer_config(self):
        with open(self.config_path, "rb") as f:
            return f.read()

    def test_init_of_a_fresh_dir_leaves_the_outer_config_alone(self):
        fresh = os.path.join(self.tmp.name, "fresh")
        with mock.patch.dict(os.environ, self._hook_env()):
            subprocess.run(
                ["git", "init", "-q", "-b", "master", fresh],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            )
        self.assertEqual(self._outer_config(), self.before)
        self.assertTrue(os.path.isdir(os.path.join(fresh, ".git")))

    def test_config_dash_c_leaves_the_outer_config_alone(self):
        fresh = os.path.join(self.tmp.name, "fresh")
        with mock.patch.dict(os.environ, self._hook_env()):
            subprocess.run(
                ["git", "init", "-q", "-b", "master", fresh],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            )
            subprocess.run(
                ["git", "-C", fresh, "config", "user.email", "t@t"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            )
        self.assertEqual(self._outer_config(), self.before)
        out = subprocess.run(
            ["git", "-C", fresh, "config", "user.email"],
            check=True,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        self.assertEqual(out.stdout.strip(), "t@t")

    def test_product_helpers_resolve_the_fixture_under_hook_env(self):
        from corral import budget, prose

        fresh = os.path.join(self.tmp.name, "fresh")
        subprocess.run(
            ["git", "init", "-q", "-b", "master", fresh],
            cwd=self.tmp.name,
            check=True,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        with mock.patch.dict(os.environ, self._hook_env()):
            for mod in (budget, prose):
                proc = mod._git(["rev-parse", "--show-toplevel"], cwd=fresh)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), fresh)
        self.assertEqual(self._outer_config(), self.before)


class WorktreeMainCheckoutTest(unittest.TestCase):
    """worktree_main_checkout reads the .git file without git."""

    def _layout(self, admin="worktrees/w"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        main = os.path.join(tmp.name, "main")
        admin_dir = os.path.join(main, ".git", admin)
        os.makedirs(admin_dir)
        with open(os.path.join(main, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/master\n")
        with open(os.path.join(admin_dir, "commondir"), "w") as f:
            f.write("../..")
        wt = os.path.join(tmp.name, "wt")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write("gitdir: %s" % admin_dir)
        return main, wt

    def test_gitfile_resolves_to_main_checkout(self):
        from corral.gitenv import worktree_main_checkout

        main, wt = self._layout()
        self.assertEqual(worktree_main_checkout(wt), main)

    def test_dangling_pointer_is_none(self):
        from corral.gitenv import worktree_main_checkout

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = os.path.join(tmp.name, "wt")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write("gitdir: /nonexistent-host-path/.git/worktrees/w")
        self.assertIsNone(worktree_main_checkout(wt))

    def test_git_dir_and_garbage_are_none(self):
        from corral.gitenv import worktree_main_checkout

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plain = os.path.join(tmp.name, "plain")
        os.makedirs(os.path.join(plain, ".git"))
        self.assertIsNone(worktree_main_checkout(plain))
        with open(os.path.join(tmp.name, "stray"), "w") as f:
            f.write("not a gitfile")
        self.assertIsNone(worktree_main_checkout(tmp.name))


if __name__ == "__main__":
    unittest.main()
