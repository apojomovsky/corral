"""The commit-msg, pre-commit and pre-push hooks, run as git runs them."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / ".githooks"


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def clean_env(email="suite@corral.invalid"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="suite",
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME="suite",
        GIT_COMMITTER_EMAIL=email,
    )
    return env


class Repo(unittest.TestCase):
    hooks = ("commit-msg",)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = clean_env()
        self.repo = Path(self.tmp.name) / "repo"
        subprocess.run(
            ["git", "init", "-q", "-b", "master", str(self.repo)], check=True, env=self.env
        )
        hooks = Path(self.tmp.name) / "hooks"
        hooks.mkdir()
        for name in self.hooks:
            (hooks / name).symlink_to(HOOKS / name)
        git(self.repo, "config", "core.hooksPath", str(hooks), env=self.env)

    def commit(self, message, name="f", env=None):
        (self.repo / name).write_text(message)
        git(self.repo, "add", name, env=env or self.env)
        return git(self.repo, "commit", "-q", "-m", message, env=env or self.env)


class CommitMsg(Repo):
    def test_a_conventional_subject_passes(self):
        self.assertEqual(self.commit("chore(repo): add the skeleton").returncode, 0)

    def test_a_breaking_subject_without_scope_passes(self):
        self.assertEqual(self.commit("feat!: change the format").returncode, 0)

    def test_a_free_form_subject_is_refused(self):
        r = self.commit("added some files")
        self.assertEqual(r.returncode, 1)
        self.assertIn("subject must be", r.stderr)

    def test_a_trailer_is_refused(self):
        r = self.commit("chore: x\n\nCo-Authored-By: someone <a@b>")
        self.assertEqual(r.returncode, 1)
        self.assertIn("trailers", r.stderr)

    def test_an_em_dash_is_refused(self):
        r = self.commit("docs: one — two")
        self.assertEqual(r.returncode, 1)
        self.assertIn("em-dashes", r.stderr)

    def test_more_than_three_lines_is_refused(self):
        r = self.commit("chore: x\n\nline\nline")
        self.assertEqual(r.returncode, 1)
        self.assertIn("at most 3 lines", r.stderr)

    def test_a_fixture_identity_is_refused(self):
        r = self.commit("chore: x", env=clean_env("t@t"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("test-fixture value", r.stderr)

    def test_a_merge_subject_needs_a_merge_in_progress(self):
        self.assertEqual(self.commit("Merge branch 'x'").returncode, 1)

    def test_a_merge_subject_passes_during_a_merge(self):
        self.commit("chore: base")
        git(self.repo, "checkout", "-q", "-b", "side", env=self.env)
        self.commit("chore: side", name="g")
        git(self.repo, "checkout", "-q", "master", env=self.env)
        self.commit("chore: main", name="h")
        r = git(self.repo, "merge", "--no-ff", "-m", "Merge branch 'side'", "side", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)


class PrePush(Repo):
    hooks = ("pre-push",)

    def setUp(self):
        super().setUp()
        # The hook runs the suite of the repo it is pushed from; this scratch
        # repo has a one-test suite that passes, so only the force guard
        # decides (an empty suite exits 5 on Python 3.12+).
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "__init__.py").write_text("")
        (self.repo / "tests" / "test_ok.py").write_text(
            "import unittest\n\nclass Ok(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n"
        )
        self.remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, env=self.env)
        git(self.repo, "remote", "add", "origin", str(self.remote), env=self.env)
        self.commit("chore: one")
        self.commit("chore: two", name="g")
        r = git(self.repo, "push", "-q", "origin", "master", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_fast_forward_push_passes(self):
        self.commit("chore: three", name="h")
        r = git(self.repo, "push", "-q", "origin", "master", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_force_push_is_refused(self):
        git(self.repo, "reset", "-q", "--hard", "HEAD~1", env=self.env)
        r = git(self.repo, "push", "-q", "--force", "origin", "master", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing a force push", r.stderr)

    def test_an_approved_force_push_passes(self):
        git(self.repo, "reset", "-q", "--hard", "HEAD~1", env=self.env)
        env = dict(self.env, CORRAL_FORCE_PUSH_APPROVED="1")
        r = git(self.repo, "push", "-q", "--force", "origin", "master", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_deleting_a_remote_branch_is_refused(self):
        r = git(self.repo, "push", "-q", "origin", ":master", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)


if __name__ == "__main__":
    unittest.main()
