"""Tests for `corral review show`: the gate takes the base from the forge.

Real git repositories in temp dirs (local only, no network); `origin`
remotes name a forge repo but are never contacted. The forge is always
the in-memory `FakeForge`; no test calls a real `gh`. Records live
under `CORRAL_SESSIONS` pointed at a temp dir, never the host's.
"""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import forge, mergeup, reviewcli, reviewrecord
from tests.test_forge import FakeForge, make_pr
from tests.test_reviewrecord import git_in

REPO = "lib"
TICKET = 1
FORGE_REPO = "acme/lib"
PR = 7


class ShowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "clone")
        self.sessions = os.path.join(tmp.name, "sessions")
        os.makedirs(self.dir)
        self.git("init", "-q", "-b", "master")
        self.write("a.txt", "one\ntwo\nthree\n")
        self.commit("init")
        self.git("checkout", "-q", "-b", "feat/1-x")
        self.write("feature.txt", "feature\n")
        self.commit("feature work")
        self.reviewed = self.rev("HEAD")
        self.approve(self.reviewed)
        self.master_base = self.rev("master")
        env = mock.patch.dict(os.environ, {"CORRAL_SESSIONS": self.sessions})
        env.start()
        self.addCleanup(env.stop)
        here = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, here)

    def git(self, *cmd):
        return git_in(self.dir, *cmd)

    def rev(self, ref):
        return self.git("rev-parse", ref)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def approve(self, commit, verdict="approve", reviewer="reviewer-1"):
        reviewrecord.write_record(
            REPO,
            TICKET,
            commit,
            reviewer=reviewer,
            author="author-1",
            verdict=verdict,
            sessions_root=self.sessions,
        )

    def args(self, **overrides):
        base = {
            "repo": REPO,
            "number": TICKET,
            "commit": "HEAD",
            "pr": PR,
            "forge_repo": FORGE_REPO,
            "head": None,
            "json": False,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def show(self, args, fake):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = reviewcli.cmd_show(args, forge=fake)
        return code, out.getvalue(), err.getvalue()

    def forge(self, **overrides):
        fields = {
            "repo": FORGE_REPO,
            "number": PR,
            "base_sha": self.master_base,
            "head_sha": self.rev("HEAD"),
        }
        fields.update(overrides)
        return FakeForge(pulls=[make_pr(**fields)])

    def advance_master(self, name, text):
        self.git("checkout", "-q", "master")
        self.write(name, text)
        self.commit("master " + name)
        self.git("checkout", "-q", "feat/1-x")

    def test_direct_record_approves(self):
        code, out, _ = self.show(self.args(), self.forge())
        self.assertEqual(code, 0)
        self.assertIn("approve by reviewer-1", out)

    def test_clean_merge_up_over_the_forge_base_inherits(self):
        self.advance_master("master.txt", "master\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        code, out, _ = self.show(self.args(), self.forge(base_sha=self.rev("master")))
        self.assertEqual(code, 0)
        self.assertIn("inherited from", out)

    def test_blocked_commit_fails(self):
        self.git("checkout", "-q", "-b", "feat/1-y", self.reviewed)
        self.write("other.txt", "other\n")
        self.commit("other work")
        head = self.rev("HEAD")
        self.approve(head, verdict="block")
        code, _, _ = self.show(self.args(commit=head), self.forge(head_sha=head))
        self.assertEqual(code, 1)

    def test_moved_origin_master_does_not_authorize_an_unreviewed_merge(self):
        # The base moves on without review, and the branch merges the
        # unreviewed tip. The forge still names the reviewed base, so
        # the merge inherits nothing.
        self.advance_master("unreviewed.txt", "unreviewed\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        head = self.rev("HEAD")
        code, _, err = self.show(
            self.args(commit=head), self.forge(base_sha=self.master_base, head_sha=head)
        )
        self.assertEqual(code, 1)
        self.assertIn("no review record", err)
        # The moved local base would have inherited: the test is not vacuous.
        record, _ = mergeup.find_record(
            lambda c: reviewrecord.read_record(REPO, TICKET, c, self.sessions),
            head,
            self.rev("master"),
            self.dir,
        )
        self.assertIsNotNone(record)

    def test_forge_unreachable_fails_closed(self):
        class Down:
            def pull_request(self, repo, number):
                raise forge.ForgeError("down")

        code, _, err = self.show(self.args(), Down())
        self.assertEqual(code, 3)
        self.assertIn("forge unreachable", err)

    def test_missing_pr_fails_closed(self):
        code, _, err = self.show(self.args(), FakeForge())
        self.assertEqual(code, 3)
        self.assertIn("forge unreachable", err)

    def test_base_sha_absent_from_the_clone_fails_closed(self):
        code, _, err = self.show(self.args(), self.forge(base_sha="d" * 40))
        self.assertEqual(code, 1)
        self.assertIn("not present in this clone", err)

    def test_no_pr_fails_closed(self):
        code, _, err = self.show(self.args(pr=None), self.forge())
        self.assertEqual(code, 1)
        self.assertIn("--pr", err)

    def test_stale_commit_is_not_the_head(self):
        self.advance_master("master.txt", "master\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        code, _, err = self.show(
            self.args(commit=self.reviewed),
            self.forge(head_sha=self.rev("HEAD")),
        )
        self.assertEqual(code, 1)
        self.assertIn("is not the head", err)

    def test_head_pin_must_match_the_forge_head(self):
        code, _, err = self.show(self.args(head="e" * 40), self.forge())
        self.assertEqual(code, 1)
        self.assertIn("--head", err)

    def test_bad_forge_repo_is_usage(self):
        code, _, err = self.show(self.args(forge_repo="not-a-repo"), self.forge())
        self.assertEqual(code, 4)
        self.assertIn("--forge-repo", err)

    def test_no_origin_and_no_flag_fails_closed(self):
        code, _, err = self.show(self.args(forge_repo=None), self.forge())
        self.assertEqual(code, 1)
        self.assertIn("--forge-repo", err)

    def test_origin_remote_supplies_the_forge_repo(self):
        self.git("remote", "add", "origin", "git@github.com:acme/lib.git")
        code, out, _ = self.show(self.args(forge_repo=None), self.forge())
        self.assertEqual(code, 0)
        self.assertIn("approve by reviewer-1", out)

    def test_json_output(self):
        import json

        code, out, _ = self.show(self.args(json=True), self.forge())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["verdict"], "approve")


if __name__ == "__main__":
    unittest.main()
