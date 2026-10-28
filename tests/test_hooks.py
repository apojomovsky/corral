"""The commit-msg, pre-commit and pre-push hooks, run as git runs them."""

import os
import subprocess
import sys
import tempfile
import unittest
from importlib import resources
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

    def test_a_second_push_of_the_same_tree_skips_the_suite(self):
        r = git(self.repo, "push", "origin", "master:copy", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("suite already passed for this tree", r.stdout + r.stderr)

    def test_a_new_tree_runs_the_suite_again(self):
        self.commit("chore: three", name="h")
        r = git(self.repo, "push", "origin", "master", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("suite already passed", r.stdout + r.stderr)

    def test_the_hook_and_corral_push_share_the_stamp_name(self):
        from corral import push

        lines = [
            l for l in (HOOKS / "pre-push").read_text().splitlines() if l.startswith("stamp_file=")
        ]
        self.assertEqual(len(lines), 1)
        self.assertIn("/" + push.STAMP_NAME + '"', lines[0])

    def test_deleting_a_remote_branch_is_refused(self):
        r = git(self.repo, "push", "-q", "origin", ":master", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)


class PackagedHooks(unittest.TestCase):
    """`corral init` ships copies of the repo hooks: drift must fail loudly."""

    def test_commit_msg_matches_the_repo_hook_byte_for_byte(self):
        packaged = resources.files("corral").joinpath("hooks/commit-msg").read_bytes()
        self.assertEqual(packaged, (HOOKS / "commit-msg").read_bytes())

    def test_pre_push_ships_the_guard_block_verbatim(self):
        repo_lines = (HOOKS / "pre-push").read_text().splitlines()
        end = next(i for i, line in enumerate(repo_lines) if line == "done")
        packaged = (
            resources.files("corral").joinpath("hooks/pre-push").read_text("utf-8").splitlines()
        )
        self.assertEqual(packaged[-1], "done")
        self.assertEqual(_code(packaged), _code(repo_lines[: end + 1]))


def _code(lines):
    """A hook block without its header comments: the shebang stays."""
    return [line for line in lines if line.startswith("#!") or not line.startswith("#")]


class PackagedHookRuns(unittest.TestCase):
    """The packaged hooks run as subprocesses in a scratch repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = clean_env()
        self.repo = Path(self.tmp.name) / "repo"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)], check=True, env=self.env
        )
        git(self.repo, "config", "user.name", "suite", env=self.env)
        git(self.repo, "config", "user.email", "suite@corral.invalid", env=self.env)
        (self.repo / "f").write_text("one")
        git(self.repo, "add", "f", env=self.env)
        git(self.repo, "commit", "-qm", "chore: one", env=self.env)
        (self.repo / "g").write_text("two")
        git(self.repo, "add", "g", env=self.env)
        git(self.repo, "commit", "-qm", "chore: two", env=self.env)
        base = resources.files("corral").joinpath("hooks")
        self.pre_push = str(base.joinpath("pre-push"))
        self.commit_msg = str(base.joinpath("commit-msg"))

    def run_pre_push(self, stdin, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", self.pre_push],
            cwd=str(self.repo),
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
        )

    def run_commit_msg(self, message):
        target = self.repo / "MSG"
        target.write_text(message)
        return subprocess.run(
            [sys.executable, self.commit_msg, str(target)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_pre_push_with_empty_stdin_passes(self):
        self.assertEqual(self.run_pre_push("").returncode, 0)

    def test_pre_push_refuses_a_non_fast_forward(self):
        old = git(self.repo, "rev-parse", "HEAD~1", env=self.env).stdout.strip()
        new = git(self.repo, "rev-parse", "HEAD", env=self.env).stdout.strip()
        line = f"refs/heads/main {old} refs/heads/main {new}\n"
        refused = self.run_pre_push(line)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("refusing", refused.stderr)
        approved = self.run_pre_push(line, {"CORRAL_FORCE_PUSH_APPROVED": "1"})
        self.assertEqual(approved.returncode, 0, approved.stderr)

    def test_packaged_commit_msg_rejects_an_em_dash(self):
        proc = self.run_commit_msg("docs: one \u2014 two\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("em-dashes", proc.stderr)

    def test_packaged_commit_msg_accepts_a_conventional_subject(self):
        self.assertEqual(self.run_commit_msg("feat(api): add the thing\n").returncode, 0)


if __name__ == "__main__":
    unittest.main()
