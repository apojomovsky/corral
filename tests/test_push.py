import io
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stdout

from corral import push


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

    def run(self, argv, timeout=300):
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

        from corral.gitenv import clean_env

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = clean_env()
        subprocess.run(["git", "init", "-q", tmp.name], check=True, env=self.env)
        self.root = os.path.realpath(tmp.name)

    def run_git(self, argv, timeout=None):
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
