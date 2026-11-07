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


def clean_env(email="suite@korapy.invalid"):
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
        # decides (an empty suite exits 5 on Python 3.12+). The suite is
        # tracked, like in a real checkout: untracked files count as a
        # dirty tree, which must run the suite.
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "__init__.py").write_text("")
        (self.repo / "tests" / "test_ok.py").write_text(
            "import unittest\n\nclass Ok(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n"
        )
        git(self.repo, "add", "tests", env=self.env)
        # Byte-compiled suite output must not count as a dirty tree: real
        # checkouts ignore it, so the scratch repo does too.
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        git(self.repo, "add", ".gitignore", env=self.env)
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
        env = dict(self.env, KORAPY_FORCE_PUSH_APPROVED="1")
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

    def test_the_hook_and_korapy_push_share_the_stamp_name(self):
        from korapy import push

        lines = [
            l for l in (HOOKS / "pre-push").read_text().splitlines() if l.startswith("stamp_file=")
        ]
        self.assertEqual(len(lines), 1)
        self.assertIn("/" + push.STAMP_NAME + '"', lines[0])

    def test_deleting_a_remote_branch_is_refused(self):
        r = git(self.repo, "push", "-q", "origin", ":master", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)


class MatrixProof(PrePush):
    """The pre-push hook reuses a fresh green-matrix proof for the same tree.

    The hook resolves the proof the way the takeoff does: the matrix
    stamp under the cache dir, for the identical tree, interpreter set
    and a clean tree. Anything else runs the suite as before.
    """

    def setUp(self):
        super().setUp()
        import time

        from korapy import checkstamp

        self.checkstamp = checkstamp
        self.time = time
        self.cache = Path(self.tmp.name) / "cache"
        self.cache.mkdir(exist_ok=True)
        src = Path(__file__).resolve().parent.parent / "src"
        self.proof_env = dict(self.env, PYTHONPATH=str(src), KORAPY_CACHE=str(self.cache))

    def write_stamp(self, tree=None, pythons="3.11 3.14", now=None):
        if tree is None:
            tree = git(self.repo, "rev-parse", "HEAD^{tree}", env=self.env).stdout.strip()
        text = self.checkstamp.format_stamp(tree, pythons, self.time.time() if now is None else now)
        (self.cache / self.checkstamp.STAMP_NAME).write_text(text, encoding="utf-8")

    def push(self, *args):
        return git(self.repo, "push", *args, env=self.proof_env)

    def test_a_fresh_matrix_proof_for_a_new_tree_skips_the_suite(self):
        self.commit("chore: three", name="h")
        self.write_stamp()
        r = self.push("origin", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("green matrix", r.stdout + r.stderr)

    def test_a_matrix_skip_leaves_the_own_stamp_on_the_older_tree(self):
        self.commit("chore: three", name="h")
        self.write_stamp()
        self.assertEqual(self.push("origin", "master").returncode, 0)
        stamp = (self.repo / ".git" / "korapy-pre-push-stamp").read_text(encoding="utf-8")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}", env=self.env).stdout.strip()
        self.assertNotIn(tree, stamp)

    def test_a_missing_matrix_proof_runs_the_suite(self):
        self.commit("chore: three", name="h")
        r = self.push("origin", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("green matrix", r.stdout + r.stderr)
        self.assertNotIn("suite already passed", r.stdout + r.stderr)

    def test_a_dirty_tree_with_a_fresh_proof_runs_the_suite(self):
        self.commit("chore: three", name="h")
        self.write_stamp()
        (self.repo / "loose").write_text("uncommitted")
        r = self.push("origin", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("green matrix", r.stdout + r.stderr)

    def test_a_dirty_tree_with_an_own_stamp_runs_the_suite(self):
        # The setUp push minted the own stamp for this tree. An untracked
        # failing test keeps the tree hash but dirties the working tree,
        # so the hook must run the suite and the push must fail.
        (self.repo / "tests" / "test_broken_untracked.py").write_text(
            "import unittest\n\nclass Broken(unittest.TestCase):\n"
            "    def test_broken(self):\n        self.fail('untracked')\n"
        )
        r = self.push("origin", "master:copy")
        # The hook exits 2; git reports the refused push as 1.
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("test suite failed", r.stdout + r.stderr)
        self.assertNotIn("suite already passed", r.stdout + r.stderr)
        self.assertNotIn("green matrix", r.stdout + r.stderr)

    def test_a_stale_matrix_proof_runs_the_suite(self):
        self.commit("chore: three", name="h")
        self.write_stamp(now=self.time.time() - self.checkstamp.STAMP_MAX_AGE_S - 1)
        r = self.push("origin", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("green matrix", r.stdout + r.stderr)

    def test_a_matrix_proof_for_another_tree_runs_the_suite(self):
        self.write_stamp()
        self.commit("chore: three", name="h")
        r = self.push("origin", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("green matrix", r.stdout + r.stderr)

    def test_a_corrupt_matrix_proof_runs_the_suite(self):
        self.commit("chore: three", name="h")
        (self.cache / self.checkstamp.STAMP_NAME).write_text("not-a-stamp\n", encoding="utf-8")
        r = self.push("origin", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("green matrix", r.stdout + r.stderr)

    def test_a_matrix_proof_for_other_interpreters_runs_the_suite(self):
        self.commit("chore: three", name="h")
        self.write_stamp(pythons="3.11")
        r = self.push("origin", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("green matrix", r.stdout + r.stderr)


class PackagedHooks(unittest.TestCase):
    """`korapy init` ships copies of the repo hooks: drift must fail loudly."""

    def test_commit_msg_matches_the_repo_hook_byte_for_byte(self):
        packaged = resources.files("korapy").joinpath("hooks/commit-msg").read_bytes()
        self.assertEqual(packaged, (HOOKS / "commit-msg").read_bytes())

    def test_pre_push_ships_the_guard_block_verbatim(self):
        repo_lines = (HOOKS / "pre-push").read_text().splitlines()
        end = next(i for i, line in enumerate(repo_lines) if line == "done")
        packaged = (
            resources.files("korapy").joinpath("hooks/pre-push").read_text("utf-8").splitlines()
        )
        self.assertEqual(packaged[-1], "done")
        self.assertEqual(_code(packaged), _code(repo_lines[: end + 1]))


def _code(lines):
    """A hook block minus its leading header comments: the shebang stays."""
    out = [lines[0]] if lines and lines[0].startswith("#!") else []
    rest = lines[1:] if out else list(lines)
    i = 0
    while i < len(rest) and rest[i].startswith("#"):
        i += 1
    return out + rest[i:]


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
        git(self.repo, "config", "user.email", "suite@korapy.invalid", env=self.env)
        (self.repo / "f").write_text("one")
        git(self.repo, "add", "f", env=self.env)
        git(self.repo, "commit", "-qm", "chore: one", env=self.env)
        (self.repo / "g").write_text("two")
        git(self.repo, "add", "g", env=self.env)
        git(self.repo, "commit", "-qm", "chore: two", env=self.env)
        base = resources.files("korapy").joinpath("hooks")
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
        approved = self.run_pre_push(line, {"KORAPY_FORCE_PUSH_APPROVED": "1"})
        self.assertEqual(approved.returncode, 0, approved.stderr)

    def test_packaged_commit_msg_rejects_an_em_dash(self):
        proc = self.run_commit_msg("docs: one \u2014 two\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("em-dashes", proc.stderr)

    def test_packaged_commit_msg_accepts_a_conventional_subject(self):
        self.assertEqual(self.run_commit_msg("feat(api): add the thing\n").returncode, 0)


if __name__ == "__main__":
    unittest.main()
