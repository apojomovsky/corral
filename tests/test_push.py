import io
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stdout

from korapy import checkstamp, push


def _proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["git"], rc, out, err)


class FakeGit:
    """A git remote plus the pre-push hook, with no network.

    Each `push` first runs the hook logic from `.githooks/pre-push`:
    a stamp hit skips the suite, otherwise the suite runs (counted) and
    stamps the tree. Then the scripted transport result applies: success
    lands HEAD on the remote unless `pin_remote` holds it back.
    """

    def __init__(self, head="HEAD1", tree="TREE1", digest="DIGEST1", remote="OLD", branch="fix/x"):
        self.envs = []
        self.head = head
        self.tree = tree
        self.digest = digest
        self.remote = remote
        self.branch = branch
        self.stamp = ""
        self.suite_runs = 0
        self.pushes = 0
        self.pin_remote = False
        self.push_script = []
        self.ls_error = None
        self.detached = False

    def _args(self, argv):
        assert argv[0] == "git"
        if argv[1] == "-C":
            return argv[3:]
        return argv[1:]

    def run(self, argv, timeout=300, env=None):
        self.envs.append(env)
        args = self._args(argv)
        if args == ["rev-parse", "--absolute-git-dir"]:
            return _proc(0, "/fake/.git\n")
        if args == ["rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"]:
            return _proc(0, "/fake/.githooks/pre-push\n")
        if args == ["rev-parse", "HEAD"]:
            return _proc(0, self.head + "\n")
        if args == ["rev-parse", "HEAD^{tree}"]:
            return _proc(0, self.tree + "\n")
        if args == ["branch", "--show-current"]:
            return _proc(0, "" if self.detached else self.branch + "\n")
        if args[:1] == ["hash-object"]:
            return _proc(0, self.digest + "\n")
        if args[:1] == ["ls-remote"]:
            if self.ls_error is not None:
                return _proc(*self.ls_error)
            ref = args[2]
            body = f"{self.remote}\t{ref}\n" if self.remote else ""
            return _proc(0, body)
        if args[:1] == ["push"]:
            self.pushes += 1
            if self.stamp.strip() != f"{self.tree} {self.digest}":
                self.suite_runs += 1
                self.stamp = f"{self.tree} {self.digest}\n"
            if self.push_script:
                rc, err = self.push_script.pop(0)
                if rc == 0 and not self.pin_remote:
                    self.remote = self.head
                return _proc(rc, "", err)
            if not self.pin_remote:
                self.remote = self.head
            return _proc(0, "")
        raise AssertionError(f"unexpected git call: {args}")

    def read(self, path):
        if not self.stamp:
            raise OSError("no stamp")
        return self.stamp

    def write(self, path, text):
        self.stamp = text


def _push(git, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = push.verified_push(
            "/fake", run=git.run, out=lambda msg: print(msg, file=buf), **kwargs
        )
    return code, buf.getvalue()


class VerifiedPushTest(unittest.TestCase):
    def test_first_push_verifies(self):
        git = FakeGit()
        code, out = _push(git)
        self.assertEqual(code, 0)
        self.assertEqual(git.pushes, 1)
        self.assertIn("verified", out)

    def test_retry_after_dropped_push_skips_suite(self):
        git = FakeGit()
        git.push_script = [(128, "error: failed to push some refs"), (0, "")]
        code, out = _push(git)
        self.assertEqual(code, 0)
        self.assertEqual(git.pushes, 2)
        self.assertEqual(git.suite_runs, 1)
        self.assertIn("retrying once", out)

    def test_mismatch_after_retry_exits_nonzero(self):
        git = FakeGit()
        git.pin_remote = True
        code, _ = _push(git)
        self.assertEqual(code, 1)
        self.assertEqual(git.pushes, 2)

    def test_hook_refusal_does_not_retry(self):
        git = FakeGit()
        git.push_script = [(1, "pre-push: test suite failed\n")]
        code, _ = _push(git)
        self.assertEqual(code, 1)
        self.assertEqual(git.pushes, 1)
        self.assertEqual(git.suite_runs, 1)

    def test_unreachable_push_exits_three(self):
        git = FakeGit()
        git.push_script = [(128, "ssh: connect to host github.com: Connection timed out")]
        code, _ = _push(git)
        self.assertEqual(code, 3)
        self.assertEqual(git.pushes, 1)

    def test_unverifiable_remote_exits_three(self):
        git = FakeGit()
        git.ls_error = (128, "", "Could not resolve hostname github.com")
        code, _ = _push(git)
        self.assertEqual(code, 3)

    def test_detached_head_exits_config(self):
        git = FakeGit()
        git.detached = True
        code, _ = _push(git)
        self.assertEqual(code, 4)
        self.assertEqual(git.pushes, 0)


class ProofDecisionTest(unittest.TestCase):
    """The hook's one skip resolver: own stamp and matrix proof in, decision out.

    A dirty tree, a missing, corrupt, stale, foreign-tree or
    foreign-interpreter proof all run the suite. Only an own-stamp hit,
    or a fresh matrix proof for the identical clean tree, skips it.
    """

    TREE = "9" * 40
    OTHER = "8" * 40
    DIGEST = "d" * 40
    NOW = 1_800_000_000
    PYTHONS = ("3.11", "3.14")

    def decide(self, own="hit", matrix="fresh", dirty=False):
        stamps = {
            "hit": (f"{self.TREE} {self.DIGEST}\n", f"{self.TREE} {self.DIGEST}"),
            "miss": ("", f"{self.TREE} {self.DIGEST}"),
            "unresolvable": ("", ""),
        }
        matrices = {
            "fresh": checkstamp.format_stamp(self.TREE, self.PYTHONS, self.NOW),
            "missing": "",
            "corrupt": "not-a-stamp\n",
            "stale": checkstamp.format_stamp(
                self.TREE, self.PYTHONS, self.NOW - checkstamp.STAMP_MAX_AGE_S - 1
            ),
            "different-tree": checkstamp.format_stamp(self.OTHER, self.PYTHONS, self.NOW),
            "different-pythons": checkstamp.format_stamp(self.TREE, ("3.11",), self.NOW),
        }
        own_text, own_expected = stamps[own]
        return push.proof_decision(
            own_text,
            own_expected,
            matrices[matrix],
            tree=self.TREE,
            pythons=self.PYTHONS,
            now=self.NOW,
            dirty=dirty,
        )

    def test_own_stamp_only_skips_the_suite(self):
        self.assertEqual(self.decide(own="hit", matrix="missing"), (True, "own-stamp"))

    def test_matrix_proof_only_skips_the_suite(self):
        self.assertEqual(self.decide(own="miss", matrix="fresh"), (True, "matrix-proof"))

    def test_both_proofs_skip_on_the_own_stamp(self):
        self.assertEqual(self.decide(own="hit", matrix="fresh"), (True, "own-stamp"))

    def test_no_proof_runs_the_suite(self):
        self.assertEqual(self.decide(own="miss", matrix="missing"), (False, "missing"))

    def test_a_stale_matrix_proof_runs_the_suite(self):
        self.assertEqual(self.decide(own="miss", matrix="stale"), (False, "stale"))

    def test_a_matrix_proof_for_another_tree_runs_the_suite(self):
        self.assertEqual(
            self.decide(own="miss", matrix="different-tree"), (False, "different-tree")
        )

    def test_a_dirty_tree_runs_the_suite_despite_a_fresh_matrix_proof(self):
        self.assertEqual(self.decide(own="miss", matrix="fresh", dirty=True), (False, "dirty-tree"))

    def test_a_dirty_tree_runs_the_suite_despite_an_own_stamp_hit(self):
        self.assertEqual(self.decide(own="hit", matrix="fresh", dirty=True), (False, "dirty-tree"))

    def test_a_corrupt_matrix_proof_runs_the_suite(self):
        self.assertEqual(self.decide(own="miss", matrix="corrupt"), (False, "corrupt"))

    def test_a_matrix_proof_for_other_interpreters_runs_the_suite(self):
        self.assertEqual(
            self.decide(own="miss", matrix="different-pythons"),
            (False, "different-pythons"),
        )

    def test_an_unresolvable_tree_never_hits_the_own_stamp(self):
        self.assertEqual(self.decide(own="unresolvable", matrix="missing"), (False, "missing"))


class HookDecisionTest(unittest.TestCase):
    """hook_decision gathers the hook's real inputs through injectable seams."""

    TREE = "9" * 40
    DIGEST = "d" * 40
    NOW = 1_800_000_000

    def setUp(self):
        self.dirty = False
        self.own_text = ""
        self.matrix_text = ""

    def git(self, argv, timeout=300, env=None):
        args = argv[3:] if argv[1] == "-C" else argv[1:]
        if args == ["rev-parse", "--absolute-git-dir"]:
            return _proc(0, "/fake/.git\n")
        if args == ["rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"]:
            return _proc(0, "/fake/.githooks/pre-push\n")
        if args == ["rev-parse", "HEAD^{tree}"]:
            return _proc(0, self.TREE + "\n")
        if args[:1] == ["hash-object"]:
            return _proc(0, self.DIGEST + "\n")
        if args == ["status", "--porcelain"]:
            return _proc(0, " M f\n" if self.dirty else "")
        raise AssertionError(f"unexpected git call: {args}")

    def read(self, path):
        if not self.own_text:
            raise OSError("no stamp")
        return self.own_text

    def matrix_read(self, path):
        if not self.matrix_text:
            raise OSError("no stamp")
        return self.matrix_text

    def decide(self):
        return push.hook_decision(
            "/fake",
            run=self.git,
            read=self.read,
            matrix_read=self.matrix_read,
            env={},
            now=self.NOW,
        )

    def fresh_matrix(self):
        return checkstamp.format_stamp(self.TREE, checkstamp.MATRIX_PYTHONS, self.NOW)

    def test_an_own_stamp_hit_skips_the_suite(self):
        self.own_text = f"{self.TREE} {self.DIGEST}\n"
        self.assertEqual(self.decide(), (True, "own-stamp"))

    def test_a_fresh_matrix_proof_skips_the_suite(self):
        self.matrix_text = self.fresh_matrix()
        self.assertEqual(self.decide(), (True, "matrix-proof"))

    def test_a_dirty_tree_runs_the_suite(self):
        self.own_text = f"{self.TREE} {self.DIGEST}\n"
        self.matrix_text = self.fresh_matrix()
        self.dirty = True
        self.assertEqual(self.decide(), (False, "dirty-tree"))

    def test_no_proof_runs_the_suite(self):
        self.assertEqual(self.decide(), (False, "missing"))


class PushEnvTest(unittest.TestCase):
    ENV = {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_0": "git@github.com:",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def test_the_push_env_reaches_every_git_call(self):
        git = FakeGit()
        code, _ = _push(git, env=self.ENV)
        self.assertEqual(code, 0)
        self.assertTrue(git.envs)
        for seen in git.envs:
            self.assertEqual(seen, self.ENV)

    def test_no_env_means_no_extra_env_on_any_call(self):
        git = FakeGit()
        code, _ = _push(git)
        self.assertEqual(code, 0)
        self.assertTrue(git.envs)
        for seen in git.envs:
            self.assertIsNone(seen)

    def test_default_run_merges_the_env_over_the_process_env(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return push.subprocess.CompletedProcess(argv, 0, "", "")

        with unittest.mock.patch.object(push.subprocess, "run", fake_run):
            push.default_run(["git", "status"], env={"GIT_TERMINAL_PROMPT": "0"})
        self.assertEqual(seen["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("PATH", seen["env"])

    def test_default_run_without_env_inherits_the_process_env(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return push.subprocess.CompletedProcess(argv, 0, "", "")

        with unittest.mock.patch.object(push.subprocess, "run", fake_run):
            push.default_run(["git", "status"])
        self.assertIsNone(seen["env"])


class StampTest(unittest.TestCase):
    def test_stamp_hit_skips_suite(self):
        git = FakeGit()
        self.assertTrue(push.suite_needed("/fake", run=git.run, read=git.read))
        push.record_pass("/fake", run=git.run, write=git.write)
        self.assertFalse(push.suite_needed("/fake", run=git.run, read=git.read))

    def test_tree_change_invalidates_stamp(self):
        git = FakeGit()
        push.record_pass("/fake", run=git.run, write=git.write)
        git.tree = "TREE2"
        self.assertTrue(push.suite_needed("/fake", run=git.run, read=git.read))

    def test_hook_change_invalidates_stamp(self):
        git = FakeGit()
        push.record_pass("/fake", run=git.run, write=git.write)
        git.digest = "DIGEST2"
        self.assertTrue(push.suite_needed("/fake", run=git.run, read=git.read))

    def test_missing_hook_never_stamps(self):
        git = FakeGit()
        git.digest = ""
        self.assertTrue(push.suite_needed("/fake", run=git.run, read=git.read))
        self.assertFalse(push.record_pass("/fake", run=git.run, write=git.write))


if __name__ == "__main__":
    unittest.main()


class DefaultRunTimeoutTest(unittest.TestCase):
    """`git push` runs the full suite in its hook; it must outlive 300 s."""

    def _timeout_for(self, argv):
        seen = {}

        def fake_run(args, **kwargs):
            seen.update(kwargs)
            return push.subprocess.CompletedProcess(args, 0, "", "")

        with unittest.mock.patch.object(push.subprocess, "run", fake_run):
            push.default_run(argv)
        return seen["timeout"]

    def test_push_gets_the_long_timeout(self):
        self.assertEqual(
            self._timeout_for(["git", "-C", "/w", "push", "origin", "b"]), push.PUSH_TIMEOUT_S
        )

    def test_other_git_calls_keep_the_short_timeout(self):
        self.assertEqual(
            self._timeout_for(["git", "-C", "/w", "ls-remote", "origin"]), push.GIT_TIMEOUT_S
        )


class HookPathTest(unittest.TestCase):
    """The stamp follows the hook git actually runs."""

    def setUp(self):
        import os
        import tempfile

        from korapy.gitenv import clean_env

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = clean_env()
        subprocess.run(["git", "init", "-q", tmp.name], check=True, env=self.env)
        self.root = os.path.realpath(tmp.name)

    def run_git(self, argv, timeout=None, env=None):
        return subprocess.run(argv, capture_output=True, text=True, env=self.env)

    def test_the_default_is_the_git_dir_hooks(self):
        import os

        self.assertEqual(
            push.hook_path(self.root, self.run_git),
            os.path.join(self.root, ".git", "hooks", "pre-push"),
        )

    def test_core_hooks_path_is_honoured(self):
        import os

        subprocess.run(
            ["git", "-C", self.root, "config", "core.hooksPath", ".githooks"],
            check=True,
            env=self.env,
        )
        self.assertEqual(
            push.hook_path(self.root, self.run_git),
            os.path.join(self.root, ".githooks", "pre-push"),
        )
