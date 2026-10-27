"""Review records, and the `corral review` verbs.

Offline: records land in temp session roots; commit resolution runs real
git against throwaway repositories in temp dirs.
"""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, mergeup, paths, reviewrecord

REPO = "lib"


def git_in(cwd, *cmd):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=suite",
            "-c",
            "user.email=suite@corral.invalid",
            "-c",
            "commit.gpgsign=false",
            *cmd,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(path):
    """A one-commit repository at `path`; returns the commit's full sha."""
    git_in(path, "init", "-q", "-b", "master")
    git_in(path, "commit", "-q", "--allow-empty", "-m", "one")
    return git_in(path, "rev-parse", "HEAD")


class RecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def write(self, commit="abc123def456", verdict="approve", **kw):
        kw.setdefault("reviewer", "reviewer-1")
        kw.setdefault("author", "author-1")
        return reviewrecord.write_record(
            REPO, 1, commit, verdict=verdict, sessions_root=self.root, **kw
        )

    def test_the_default_root_is_the_sessions_dir(self):
        with mock.patch.dict(os.environ, {"CORRAL_SESSIONS": self.root}):
            path = reviewrecord.write_record(
                REPO, 1, "abc123", reviewer="r", verdict="approve", author="a"
            )
        self.assertEqual(
            os.path.dirname(path),
            os.path.join(paths.ticket_session_dir(REPO, 1, root=self.root), "reviews"),
        )
        self.assertTrue(path.startswith(self.root + os.sep))

    def test_written_record_reads_back(self):
        self.write(question="any defect?", findings=[{"severity": "low", "text": "t"}])
        record = reviewrecord.read_record(REPO, 1, "abc123def456", sessions_root=self.root)
        self.assertEqual(record["verdict"], "approve")
        self.assertEqual(record["reviewer"], "reviewer-1")
        self.assertEqual(record["commit"], "abc123def456")
        self.assertEqual(record["question"], "any defect?")
        self.assertEqual(record["findings"], [{"severity": "low", "text": "t"}])

    def test_a_different_commit_or_ticket_has_no_record(self):
        self.write()
        self.assertIsNone(reviewrecord.read_record(REPO, 1, "def456", sessions_root=self.root))
        self.assertIsNone(
            reviewrecord.read_record(REPO, 2, "abc123def456", sessions_root=self.root)
        )
        self.assertIsNone(
            reviewrecord.read_record("app", 1, "abc123def456", sessions_root=self.root)
        )

    def test_a_retry_is_a_no_op_and_any_other_second_write_is_refused(self):
        self.write(findings=[{"severity": "high", "text": "a real bug"}], now=1.0)
        self.write(now=2.0)
        for kw in ({"verdict": "block"}, {"reviewer": "reviewer-2"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.write(**kw)
        record = reviewrecord.read_record(REPO, 1, "abc123def456", sessions_root=self.root)
        self.assertEqual(
            (record["verdict"], record["reviewer"], record["at"], record["findings"]),
            ("approve", "reviewer-1", 1.0, [{"severity": "high", "text": "a real bug"}]),
        )
        self.assertEqual(
            os.listdir(reviewrecord.reviews_dir(REPO, 1, self.root)), ["review-abc123def456.json"]
        )

    def test_a_commit_sharing_the_file_prefix_does_not_share_the_record(self):
        first, second = "a" * 12 + "1" * 28, "a" * 12 + "2" * 28
        self.write(first)
        self.assertIsNotNone(reviewrecord.read_record(REPO, 1, first, sessions_root=self.root))
        self.assertIsNone(reviewrecord.read_record(REPO, 1, second, sessions_root=self.root))

    def test_approves(self):
        self.assertTrue(reviewrecord.approves({"verdict": "approve"}))
        self.assertTrue(reviewrecord.approves({"verdict": "approve-with-findings"}))
        self.assertFalse(reviewrecord.approves({"verdict": "block"}))
        self.assertFalse(reviewrecord.approves(None))

    def test_bad_inputs_are_refused(self):
        for kw in (
            {"verdict": "looks-fine"},
            {"reviewer": ""},
            {"author": ""},
            {"reviewer": "agent-1", "author": "agent-1"},
            {"commit": ""},
        ):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.write(**kw)
        self.assertFalse(os.path.exists(reviewrecord.reviews_dir(REPO, 1, self.root)))

    def test_a_repo_name_that_leaves_the_root_is_refused(self):
        for repo in ("../x", "a/b", ".hidden", "", "x.git"):
            with self.subTest(repo=repo), self.assertRaises(paths.PathError):
                reviewrecord.reviews_dir(repo, 1, self.root)
        for number in (0, -1, True, "3"):
            with self.subTest(number=number), self.assertRaises(paths.PathError):
                reviewrecord.reviews_dir(REPO, number, self.root)

    def test_commit_ids_are_lowercase_and_stable(self):
        self.assertEqual(reviewrecord.commit_id("A" * 40), "a" * 12)
        self.assertEqual(reviewrecord.commit_id({"sha": "b" * 40}), "b" * 12)
        self.assertEqual(len(reviewrecord.commit_id("HEAD")), 12)
        self.assertEqual(reviewrecord.commit_id(""), "")


class ResolveCommitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        os.makedirs(self.repo)
        self.head = make_repo(self.repo)

    def test_head_and_a_short_sha_resolve(self):
        self.assertEqual(mergeup.resolve_commit("HEAD", self.repo), self.head)
        self.assertEqual(mergeup.resolve_commit(self.head[:7], self.repo), self.head)

    def test_a_branch_named_like_the_sha_cannot_shadow_the_commit(self):
        prefix = self.head[:7]
        git_in(self.repo, "commit", "-q", "--allow-empty", "-m", "two")
        git_in(self.repo, "branch", prefix)
        self.assertEqual(mergeup.resolve_commit(prefix, self.repo), self.head)

    def test_a_one_to_three_char_id_is_never_read_as_a_branch(self):
        prefix = self.head[:3]
        git_in(self.repo, "commit", "-q", "--allow-empty", "-m", "two")
        git_in(self.repo, "branch", prefix)
        self.assertIsNone(mergeup.resolve_commit(prefix, self.repo))

    def test_an_ambiguous_prefix_is_refused(self):
        stream = "".join(
            f"commit refs/heads/bulk\ncommitter t <t@corral.invalid> {i} +0000\n"
            f"data {len(f'c{i}') + 1}\nc{i}\n\n"
            for i in range(1, 2001)
        )
        subprocess.run(
            ["git", "fast-import", "--quiet"],
            cwd=self.repo,
            input=stream,
            check=True,
            text=True,
            capture_output=True,
        )
        seen = set()
        for sha in git_in(self.repo, "rev-list", "bulk").split():
            if sha[:4] in seen:
                self.assertIsNone(mergeup.resolve_commit(sha[:4], self.repo))
                return
            seen.add(sha[:4])
        self.fail("2000 commits must collide on 4 hex characters")

    def test_junk_resolves_to_none(self):
        for raw in ("", "-x", "a9c992g", "nope"):
            with self.subTest(raw=raw):
                self.assertIsNone(mergeup.resolve_commit(raw, self.repo))


class VerbTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sessions = os.path.join(tmp.name, "sessions")
        self.repo = os.path.join(tmp.name, "repo")
        os.makedirs(self.repo)
        self.head = make_repo(self.repo)
        env = mock.patch.dict(os.environ, {"CORRAL_SESSIONS": self.sessions, "CORRAL_AGENT_ID": ""})
        env.start()
        self.addCleanup(env.stop)
        old = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, old)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def record(self, commit, *extra):
        return self.run_cli(
            "review",
            "record",
            REPO,
            "7",
            "--commit",
            commit,
            "--verdict",
            "approve",
            "--author",
            "author-1",
            *extra,
        )

    def test_record_then_show(self):
        code, out, _ = self.record(
            self.head[:7], "--reviewer", "reviewer-1", "--finding", "low: a nit", "--finding", "odd"
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith(f"review-{self.head[:12]}.json"))
        code, out, _ = self.run_cli("review", "show", REPO, "7")
        self.assertEqual(code, 0)
        self.assertIn(f"approve by reviewer-1 on {self.head[:12]}", out)
        self.assertIn("- low: a nit", out)
        self.assertIn("- note: odd", out)
        code, out, _ = self.run_cli("review", "show", REPO, "7", "--json")
        payload = json.loads(out)
        self.assertEqual((payload["verdict"], payload["inherited_from"]), ("approve", None))

    def test_show_without_a_record_exits_1(self):
        code, _, err = self.run_cli("review", "show", REPO, "7")
        self.assertEqual(code, 1)
        self.assertIn("no review record", err)

    def test_the_reviewer_comes_from_the_agent_id(self):
        with mock.patch.dict(os.environ, {"CORRAL_AGENT_ID": "agent-9"}):
            self.assertEqual(self.record("HEAD")[0], 0)
        record = reviewrecord.read_record(REPO, 7, self.head)
        self.assertEqual(record["reviewer"], "agent-9")

    def test_no_reviewer_at_all_is_refused(self):
        code, _, err = self.record("HEAD")
        self.assertEqual(code, 4)
        self.assertIn("--reviewer", err)

    def test_a_self_review_and_a_changed_verdict_are_refused(self):
        code, _, err = self.record("HEAD", "--reviewer", "a-1", "--author", "a-1")
        self.assertEqual(code, 4)
        self.assertIn("own reviewer", err)
        self.assertEqual(self.record("HEAD", "--reviewer", "r-1")[0], 0)
        code, _, err = self.run_cli(
            "review",
            "record",
            REPO,
            "7",
            "--commit",
            "HEAD",
            "--verdict",
            "block",
            "--reviewer",
            "r-1",
            "--author",
            "author-1",
        )
        self.assertEqual(code, 4)
        self.assertIn("already reviewed", err)

    def test_no_author_is_a_usage_error(self):
        code, _, err = self.run_cli(
            "review",
            "record",
            REPO,
            "7",
            "--commit",
            "HEAD",
            "--verdict",
            "approve",
            "--reviewer",
            "r-1",
        )
        self.assertEqual(code, 4)
        self.assertFalse(os.path.exists(reviewrecord.reviews_dir(REPO, 7)))

    def test_show_of_a_block_exits_1(self):
        code, _, _ = self.run_cli(
            "review",
            "record",
            REPO,
            "7",
            "--commit",
            "HEAD",
            "--verdict",
            "block",
            "--reviewer",
            "r-1",
            "--author",
            "author-1",
        )
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("review", "show", REPO, "7")
        self.assertEqual(code, 1)
        self.assertIn("block by r-1", out)
        self.assertEqual(self.run_cli("review", "show", REPO, "7", "--json")[0], 1)

    def test_an_unresolvable_id_is_refused_not_hashed(self):
        for commit in ("1234567" if not self.head.startswith("1234567") else "7654321", "a9c992g"):
            code, _, _ = self.record(commit, "--reviewer", "r-1")
            self.assertEqual(code, 4, commit)
        self.assertFalse(os.path.exists(reviewrecord.reviews_dir(REPO, 7)))

    def test_a_full_sha_from_another_checkout_is_taken_lowercased(self):
        self.assertEqual(self.record("A" * 40, "--reviewer", "r-1")[0], 0)
        self.assertTrue(os.path.exists(reviewrecord.record_path(REPO, 7, "a" * 40)))

    def test_a_bad_repo_name_is_a_usage_error(self):
        code, _, _ = self.run_cli(
            "review",
            "record",
            "../x",
            "7",
            "--commit",
            "HEAD",
            "--verdict",
            "approve",
            "--reviewer",
            "r",
            "--author",
            "a",
        )
        self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
