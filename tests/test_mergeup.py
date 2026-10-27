"""A clean merge-up of the base inherits the reviewed parent's record.

Real git repositories in temp dirs; `origin/master` is a local ref, so no
network is involved.
"""

import os
import subprocess
import tempfile
import unittest

from corral import mergeup, reviewrecord

from tests.test_reviewrecord import git_in

REPO = "lib"
TICKET = 1
BASE = "origin/master"


class MergeUpTest(unittest.TestCase):
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

    def approve(self, commit, verdict="approve"):
        reviewrecord.write_record(
            REPO,
            TICKET,
            commit,
            reviewer="reviewer-1",
            author="author-1",
            verdict=verdict,
            sessions_root=self.sessions,
        )

    def advance_master(self, name, text):
        self.git("checkout", "-q", "master")
        self.write(name, text)
        self.commit("master " + name)
        self.git("update-ref", "refs/remotes/origin/master", "master")
        self.git("checkout", "-q", "feat/1-x")

    def merge_conflicting(self):
        proc = subprocess.run(
            [
                "git",
                "-c",
                "user.name=suite",
                "-c",
                "user.email=suite@corral.invalid",
                "merge",
                "--no-ff",
                "-m",
                "merge up",
                "master",
            ],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)

    def find(self, head, cwd=None):
        return mergeup.find_record(
            lambda c: reviewrecord.read_record(REPO, TICKET, c, self.sessions),
            head,
            BASE,
            cwd or self.dir,
        )

    def assert_inherits(self, head):
        record, commit = self.find(head)
        self.assertEqual(commit, self.reviewed)
        self.assertEqual(record["reviewer"], "reviewer-1")

    def assert_fresh_review_needed(self, head):
        self.assertEqual(self.find(head), (None, ""))

    def test_a_clean_merge_up_inherits_the_first_parents_record(self):
        self.advance_master("master.txt", "master\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        self.assert_inherits(self.rev("HEAD"))

    def test_the_heads_own_record_still_wins(self):
        self.advance_master("master.txt", "master\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        head = self.rev("HEAD")
        self.approve(head, verdict="block")
        record, commit = self.find(head)
        self.assertEqual((commit, record["verdict"]), (head, "block"))

    def test_two_merge_ups_in_a_row_inherit_the_reviewed_record(self):
        self.advance_master("one.txt", "1\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        self.advance_master("two.txt", "2\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up again", "master")
        self.assert_inherits(self.rev("HEAD"))

    def test_more_merge_ups_than_the_depth_limit_do_not_inherit(self):
        for i in range(mergeup.MAX_DEPTH + 1):
            self.advance_master(f"m{i}.txt", f"{i}\n")
            self.git("merge", "-q", "--no-ff", "-m", f"merge up {i}", "master")
        self.assert_fresh_review_needed(self.rev("HEAD"))

    def test_a_merge_with_a_resolution_hunk_does_not_inherit(self):
        self.write("a.txt", "one\nfeature two\nthree\n")
        self.commit("feature edits a")
        self.approve(self.rev("HEAD"))
        self.advance_master("a.txt", "one\nmaster two\nthree\n")
        self.merge_conflicting()
        self.write("a.txt", "one\nresolved by hand\nthree\n")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "merge up")
        self.assert_fresh_review_needed(self.rev("HEAD"))

    def test_a_conflict_resolved_by_taking_one_side_does_not_inherit(self):
        # `git show --cc` is empty for this merge, yet it dropped the
        # reviewed side of the file: a resolution all the same.
        self.write("a.txt", "one\nfeature two\nthree\n")
        self.commit("feature edits a")
        self.approve(self.rev("HEAD"))
        self.advance_master("a.txt", "one\nmaster two\nthree\n")
        self.merge_conflicting()
        self.git("checkout", "--theirs", "a.txt")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "merge up")
        head = self.rev("HEAD")
        self.assertEqual(self.git("show", "--format=", "--cc", head), "")
        self.assert_fresh_review_needed(head)

    def test_a_clean_merge_of_the_same_file_at_other_hunks_inherits(self):
        # `git show --cc` is not empty for this merge, yet nothing was
        # resolved: git merged both edits on its own.
        self.write("a.txt", "ONE\ntwo\nthree\n")
        self.commit("feature edits the top of a")
        self.reviewed = self.rev("HEAD")
        self.approve(self.reviewed)
        self.advance_master("a.txt", "one\ntwo\nTHREE\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        head = self.rev("HEAD")
        self.assertNotEqual(self.git("show", "--format=", "--cc", head), "")
        self.assert_inherits(head)

    def test_a_blocked_parent_is_not_inherited(self):
        self.git("checkout", "-q", "-b", "blocked", "feat/1-x")
        self.write("more.txt", "more\n")
        self.commit("blocked work")
        self.approve(self.rev("HEAD"), verdict="block")
        self.advance_master("master.txt", "master\n")
        self.git("checkout", "-q", "blocked")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        self.assert_fresh_review_needed(self.rev("HEAD"))

    def test_a_merge_of_a_feature_branch_does_not_inherit(self):
        self.git("checkout", "-q", "-b", "other", "master")
        self.write("other.txt", "other\n")
        self.commit("other feature")
        self.git("checkout", "-q", "feat/1-x")
        self.git("merge", "-q", "--no-ff", "-m", "merge other", "other")
        # Clean, but the second parent is not base history.
        self.assert_fresh_review_needed(self.rev("HEAD"))

    def test_a_plain_commit_after_the_review_does_not_inherit(self):
        self.write("more.txt", "more\n")
        self.commit("unreviewed follow-up")
        self.assert_fresh_review_needed(self.rev("HEAD"))

    def test_a_missing_clone_fails_closed(self):
        self.advance_master("master.txt", "master\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        record, _ = self.find(self.rev("HEAD"), cwd=os.path.join(self.dir, "nowhere"))
        self.assertIsNone(record)

    def test_a_merge_up_of_an_unreviewed_branch_has_no_record(self):
        self.write("more.txt", "more\n")
        self.commit("unreviewed follow-up")
        self.advance_master("master.txt", "master\n")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        self.assert_fresh_review_needed(self.rev("HEAD"))

    def test_option_like_refs_are_refused_before_git_runs(self):
        self.assertEqual(mergeup.clean_mergeup_parent("--all", BASE, self.dir), "")
        self.assertEqual(mergeup.clean_mergeup_parent(self.reviewed, "-x", self.dir), "")


if __name__ == "__main__":
    unittest.main()
