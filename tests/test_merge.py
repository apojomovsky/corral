"""Tests for `korapy merge`: the serialized, verified squash-merge verb.

Real git repositories in temp dirs (local only, no network); `origin`
remotes name a forge repo but are never contacted. The forge is always
an in-memory fake; no test calls a real `gh`. Records live under
`KORAPY_SESSIONS` and locks under `KORAPY_STATE`, both pointed at temp
dirs, never the host's.
"""

import argparse
import errno
import fcntl
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

from korapy import forge, merge, mergecli, paths, reviewcli, reviewrecord, settings
from tests.test_forge import MERGE_SHA, FakeForge, make_pr
from tests.test_reviewrecord import git_in

REPO = "lib"
TICKET = 1
FORGE_REPO = "acme/lib"
PR = 7


class UnverifiedForge(FakeForge):
    """A forge whose merge answers a SHA no later read shows."""

    def merge(self, repo, number, *, head_sha, method):
        self.calls.append(("merge", repo, number, head_sha, method))
        return MERGE_SHA


class RacedForge(FakeForge):
    """The PR closes between the pre-read and the merge (a lost race)."""

    def merge(self, repo, number, *, head_sha, method):
        self.calls.append(("merge", repo, number, head_sha, method))
        pr = self.pulls[(repo, number)]
        self.pulls[(repo, number)] = pr._replace(state=forge.CLOSED)
        raise forge.ForgeRefused(f"PR {number} is closed: nothing to merge")


class MethodRefusedForge(FakeForge):
    """The forge disallows the method; the PR stays open at the head."""

    def merge(self, repo, number, *, head_sha, method):
        self.calls.append(("merge", repo, number, head_sha, method))
        raise forge.ForgeRefused(
            f"PR {number} merge refused: the repository settings allow squash only (HTTP 405)"
        )


class SequenceForge(FakeForge):
    """A forge serving a scripted PR per `pull_request` read, then holding.

    `reads` lists the `PullRequest` objects successive reads return; once
    one remains, every later read returns it. `merge` stamps that last
    read merged with `MERGE_SHA`, like `FakeForge.merge`, so the
    post-merge verify sees the merge. Tests drive `--wait` polling
    without any clock or network.
    """

    def __init__(self, reads):
        self.reads = list(reads)
        self.requested = []
        self.calls = []

    def pull_request(self, repo, number):
        self.requested.append((repo, number))
        if len(self.reads) > 1:
            return self.reads.pop(0)
        return self.reads[0]

    def merge(self, repo, number, *, head_sha, method):
        from korapy.github import forge as ghforge

        self.calls.append(("merge", repo, number, head_sha, method))
        if method not in ("squash", "merge"):
            raise forge.ForgeError(f"unsupported merge method {method!r}")
        if not ghforge.is_repo_name(repo):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        pr = self.reads[0]
        if pr.state != forge.OPEN:
            raise forge.ForgeRefused(f"PR {number} is {pr.state}, nothing to merge")
        if pr.head_sha != head_sha:
            raise forge.ForgeRefused(f"PR {number} head moved: want {head_sha}")
        self.reads[0] = pr._replace(state=forge.MERGED, merge_sha=MERGE_SHA)
        return MERGE_SHA


class MergeCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in [n for n in os.environ if n.startswith("GIT_")]:
            del os.environ[name]
        tmp = tempfile.TemporaryDirectory(prefix="korapy-merge-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp, "state")
        os.environ["KORAPY_SESSIONS"] = os.path.join(self.tmp, "sessions")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp, "config")
        self.dir = os.path.join(self.tmp, "clone")
        os.makedirs(self.dir)
        self.git("init", "-q", "-b", "master")
        self.write("a.txt", "one\ntwo\nthree\n")
        self.commit("init")
        self.git("checkout", "-q", "-b", "feat/1-x")
        self.write("feature.txt", "feature\n")
        self.commit("feature work")
        self.head = self.rev("HEAD")
        self.base = self.rev("master")
        self.approve(self.head)
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

    def approve(self, commit, verdict="approve"):
        reviewrecord.write_record(
            REPO,
            TICKET,
            commit,
            reviewer="reviewer-1",
            author="author-1",
            verdict=verdict,
            sessions_root=os.environ["KORAPY_SESSIONS"],
        )

    def fake(self, **overrides):
        fields = {
            "repo": FORGE_REPO,
            "number": PR,
            "base_sha": self.base,
            "head_sha": self.head,
        }
        fields.update(overrides)
        return FakeForge(pulls=[make_pr(**fields)])

    def do_merge(self, fake=None, **overrides):
        params: dict[str, Any] = {
            "head_sha": self.head,
            "forge": fake or self.fake(),
            "cwd": self.dir,
        }
        params.update(overrides)
        return merge.run_merge(REPO, TICKET, FORGE_REPO, PR, **params)

    def row(self, result, name):
        for check in result.checks:
            if check.name == name:
                return check
        self.fail(f"no check named {name!r}")

    def hold_lock(self):
        """Hold the per-repo merge lock the way another merger would."""
        path = merge.lock_path(FORGE_REPO)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd


class CoreTest(MergeCase):
    def test_lock_path_is_the_one_place_for_the_per_repo_lock(self):
        self.assertEqual(
            merge.lock_path(FORGE_REPO),
            os.path.join(os.environ["KORAPY_STATE"], "merges", "acme+lib.lock"),
        )

    def test_success_merges_and_verifies(self):
        fake = self.fake()
        result = self.do_merge(fake)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.merged)
        self.assertEqual(result.merge_sha, MERGE_SHA)
        self.assertEqual(
            [check.name for check in result.checks],
            ["lock", "open", "head", "review", "checks", "fork"],
        )
        for check in result.checks:
            self.assertEqual(check.status, "ok", check.name)
        self.assertEqual(result.skipped, [])
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "squash")])

    def test_lock_race_is_exit_2_without_merging(self):
        fake = self.fake()
        self.hold_lock()
        result = self.do_merge(fake)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.merged)
        self.assertIsNone(result.merge_sha)
        self.assertEqual(self.row(result, "lock").status, "fail")
        self.assertEqual(fake.calls, [])

    def test_plan_with_a_held_lock_is_informational(self):
        fake = self.fake()
        self.hold_lock()
        result = self.do_merge(fake, plan=True)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.merged)
        self.assertIn("held", self.row(result, "lock").detail)
        self.assertEqual(fake.calls, [])

    def test_plan_with_an_unreadable_lock_file_is_usage(self):
        fake = self.fake()
        path = merge.lock_path(FORGE_REPO)
        real_open = os.open

        def bad_open(p, *args, **kwargs):
            if p == path:
                raise OSError(errno.EACCES, "Permission denied")
            return real_open(p, *args, **kwargs)

        with mock.patch.object(merge.os, "open", side_effect=bad_open):
            with self.assertRaises(merge.MergeError) as caught:
                self.do_merge(fake, plan=True)
        self.assertIn("cannot open the merge lock", str(caught.exception))
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.requested, [])

    def test_plan_reports_without_merging(self):
        fake = self.fake()
        result = self.do_merge(fake, plan=True)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.merged)
        self.assertIsNone(result.merge_sha)
        for check in result.checks:
            self.assertEqual(check.status, "ok", check.name)
        self.assertEqual(fake.calls, [])

    def test_plan_with_a_failing_row_is_exit_1_without_merging(self):
        fake = self.fake(checks_state="failing")
        result = self.do_merge(fake, plan=True)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.row(result, "checks").status, "fail")
        self.assertFalse(result.merged)
        self.assertEqual(fake.calls, [])

    def test_moved_head_refuses(self):
        fake = self.fake(head_sha="c" * 40)
        result = self.do_merge(fake)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no longer the tip", self.row(result, "head").detail)
        self.assertFalse(result.merged)
        self.assertEqual(fake.calls, [])

    def test_closed_and_merged_prs_refuse(self):
        for state in ("closed", "merged"):
            with self.subTest(state=state):
                fake = self.fake(state=state)
                result = self.do_merge(fake)
                self.assertEqual(result.exit_code, 1)
                self.assertIn(state, self.row(result, "open").detail)
                self.assertFalse(result.merged)
                self.assertEqual(fake.calls, [])

    def test_fork_refuses(self):
        fake = self.fake(from_fork=True)
        result = self.do_merge(fake)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("fork", self.row(result, "fork").detail)
        self.assertFalse(result.merged)
        self.assertEqual(fake.calls, [])

    def test_missing_review_refuses(self):
        self.write("more.txt", "more\n")
        self.commit("more work")
        head = self.rev("HEAD")
        result = self.do_merge(self.fake(head_sha=head), head_sha=head)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no approving review record", self.row(result, "review").detail)
        self.assertFalse(result.merged)

    def test_block_review_refuses(self):
        self.write("more.txt", "more\n")
        self.commit("more work")
        head = self.rev("HEAD")
        self.approve(head, verdict="block")
        result = self.do_merge(self.fake(head_sha=head), head_sha=head)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("block", self.row(result, "review").detail)
        self.assertFalse(result.merged)

    def test_inherited_review_across_a_clean_merge_up_passes(self):
        self.git("checkout", "-q", "master")
        self.write("master.txt", "master\n")
        self.commit("master work")
        master = self.rev("master")
        self.git("checkout", "-q", "feat/1-x")
        self.git("merge", "-q", "--no-ff", "-m", "merge up", "master")
        head = self.rev("HEAD")
        result = self.do_merge(self.fake(base_sha=master, head_sha=head), head_sha=head)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("inherited from", self.row(result, "review").detail)
        self.assertTrue(result.merged)

    def test_pending_and_failing_checks_refuse(self):
        for state, status in (("pending", "pending"), ("failing", "fail")):
            with self.subTest(state=state):
                result = self.do_merge(self.fake(checks_state=state))
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(self.row(result, "checks").status, status)
                self.assertFalse(result.merged)

    def test_skip_checks_waives_a_failing_row(self):
        result = self.do_merge(self.fake(checks_state="failing"), skips=["checks"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.row(result, "checks").status, "skipped")
        self.assertEqual(result.skipped, ["checks"])
        self.assertTrue(result.merged)

    def test_skip_of_anything_but_checks_is_usage(self):
        for name in ("lock", "open", "head", "review", "fork", "bogus"):
            with self.subTest(name=name):
                with self.assertRaises(merge.MergeError):
                    self.do_merge(self.fake(), skips=[name])

    def test_bad_method_is_usage(self):
        with self.assertRaises(merge.MergeError):
            self.do_merge(self.fake(), method="rebase")

    def test_missing_head_is_usage(self):
        for head in (None, "", "short"):
            with self.subTest(head=head):
                with self.assertRaises(merge.MergeError):
                    self.do_merge(self.fake(), head_sha=head)

    def test_bad_ticket_is_usage(self):
        with self.assertRaises(merge.MergeError):
            merge.run_merge("", TICKET, FORGE_REPO, PR, head_sha=self.head, forge=self.fake())
        with self.assertRaises(merge.MergeError):
            merge.run_merge(REPO, 0, FORGE_REPO, PR, head_sha=self.head, forge=self.fake())

    def test_missing_pr_is_forge_error(self):
        with self.assertRaises(forge.ForgeError):
            self.do_merge(FakeForge())

    def test_verify_mismatch_fails_closed(self):
        pulls = [make_pr(repo=FORGE_REPO, number=PR, base_sha=self.base, head_sha=self.head)]
        fake = UnverifiedForge(pulls=pulls)
        result = self.do_merge(fake)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.merged)
        self.assertIsNone(result.merge_sha)
        self.assertEqual(self.row(result, "verify").status, "fail")
        self.assertEqual(len(fake.calls), 1)

    def test_merge_time_refusal_reports_the_failing_row(self):
        fake = RacedForge(
            pulls=[make_pr(repo=FORGE_REPO, number=PR, base_sha=self.base, head_sha=self.head)]
        )
        result = self.do_merge(fake)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.merged)
        self.assertEqual(self.row(result, "open").status, "fail")
        self.assertEqual(len(fake.calls), 1)


class MethodSettingTest(unittest.TestCase):
    def resolve(self, **kw):
        kw.setdefault("env", {})
        return settings.resolve(**kw)

    def test_provider_is_registered(self):
        self.assertIn("korapy.merge", settings.PROVIDERS)
        self.assertEqual(settings.REGISTRY["merge.method"].default, "squash")

    def test_default_is_squash(self):
        resolved = self.resolve()
        self.assertEqual(resolved["merge.method"], "squash")
        self.assertEqual(resolved.values["merge.method"].origin, settings.DEFAULT)

    def test_project_file_selects_merge(self):
        resolved = self.resolve(project={"merge": {"method": "merge"}})
        self.assertEqual(resolved["merge.method"], "merge")
        self.assertEqual(resolved.values["merge.method"].origin, settings.PROJECT)

    def test_env_selects_merge(self):
        resolved = self.resolve(env={"KORAPY_MERGE_METHOD": "merge"})
        self.assertEqual(resolved["merge.method"], "merge")

    def test_anything_but_squash_or_merge_is_rejected(self):
        for where in (
            {"project": {"merge": {"method": "rebase"}}},
            {"project": {"merge": {"method": "bogus"}}},
            {"project": {"merge": {"method": 7}}},
            {"host": {"merge": {"method": "rebase"}}},
            {"env": {"KORAPY_MERGE_METHOD": "rebase"}},
        ):
            with self.subTest(where=where):
                with self.assertRaises(settings.SettingsError) as ctx:
                    self.resolve(**where)
                self.assertIn("merge.method", str(ctx.exception))


class ResolveMethodTest(unittest.TestCase):
    def configured(self, method):
        return settings.resolve(project={"merge": {"method": method}}, env={})

    def test_flag_wins_over_config(self):
        resolved = self.configured("merge")
        self.assertEqual(merge.resolve_method(flag="squash", resolved=resolved), "squash")

    def test_config_applies_without_a_flag(self):
        self.assertEqual(merge.resolve_method(resolved=self.configured("merge")), "merge")

    def test_default_is_squash(self):
        self.assertEqual(merge.resolve_method(), "squash")
        self.assertEqual(merge.resolve_method(resolved=settings.resolve(env={})), "squash")

    def test_unknown_flag_or_value_names_the_setting(self):
        bad = settings.Resolved(values={"merge.method": settings.Value("rebase", settings.PROJECT)})
        for kw in ({"flag": "rebase"}, {"flag": "bogus"}, {"resolved": bad}):
            with self.subTest(kw=kw):
                with self.assertRaises(merge.MergeError) as ctx:
                    merge.resolve_method(**kw)
                self.assertIn("merge.method", str(ctx.exception))


class MergeMethodTest(MergeCase):
    def test_absent_method_leaves_the_default_to_the_resolver(self):
        params = inspect.signature(merge.run_merge).parameters
        self.assertIsNone(params["method"].default)
        fake = self.fake()
        result = self.do_merge(fake)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.merged)
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, merge.DEFAULT_METHOD)])

    def test_merge_commit_merges_and_verifies(self):
        fake = self.fake()
        result = self.do_merge(fake, method="merge")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.merged)
        self.assertEqual(result.merge_sha, MERGE_SHA)
        for check in result.checks:
            self.assertEqual(check.status, "ok", check.name)
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "merge")])

    def test_plan_with_merge_method_reports_without_merging(self):
        fake = self.fake()
        result = self.do_merge(fake, plan=True, method="merge")
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.merged)
        self.assertEqual(fake.calls, [])

    def test_refused_method_is_exit_1_naming_the_setting(self):
        pulls = [make_pr(repo=FORGE_REPO, number=PR, base_sha=self.base, head_sha=self.head)]
        fake = MethodRefusedForge(pulls=pulls)
        result = self.do_merge(fake, method="merge")
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.merged)
        self.assertIsNone(result.merge_sha)
        row = self.row(result, "method")
        self.assertEqual(row.status, "fail")
        self.assertIn("merge.method", row.detail)
        for check in result.checks:
            if check.name != "method":
                self.assertEqual(check.status, "ok", check.name)

    def test_race_refusal_still_names_the_row_not_the_method(self):
        fake = RacedForge(
            pulls=[make_pr(repo=FORGE_REPO, number=PR, base_sha=self.base, head_sha=self.head)]
        )
        result = self.do_merge(fake, method="merge")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.row(result, "open").status, "fail")
        self.assertNotIn("method", [check.name for check in result.checks])

    def test_verify_mismatch_for_merge_method_fails_closed(self):
        pulls = [make_pr(repo=FORGE_REPO, number=PR, base_sha=self.base, head_sha=self.head)]
        fake = UnverifiedForge(pulls=pulls)
        result = self.do_merge(fake, method="merge")
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.merged)
        self.assertEqual(self.row(result, "verify").status, "fail")

    def test_rebase_and_unknown_methods_are_usage(self):
        for method in ("rebase", "bogus", ""):
            with self.subTest(method=method):
                with self.assertRaises(merge.MergeError) as ctx:
                    self.do_merge(self.fake(), method=method)
                self.assertIn("merge.method", str(ctx.exception))


class PendingWaitTest(MergeCase):
    """Pending checks are a wait, not a failure; `--wait` polls the forge."""

    def pr(self, checks_state, pending=0):
        return make_pr(
            repo=FORGE_REPO,
            number=PR,
            base_sha=self.base,
            head_sha=self.head,
            checks_state=checks_state,
            checks_pending=pending,
        )

    def clock(self):
        now = [1000.0]
        sleeps = []

        def now_fn():
            return now[0]

        def sleep_fn(secs):
            sleeps.append(secs)
            now[0] += secs

        return now_fn, sleep_fn, sleeps

    def test_pending_checks_report_pending_with_wait_advice(self):
        result = self.do_merge(self.fake(checks_state="pending", checks_pending=2), plan=True)
        self.assertEqual(result.exit_code, 1)
        row = self.row(result, "checks")
        self.assertEqual(row.status, "pending")
        self.assertIn("2", row.detail)
        self.assertIn("wait", row.detail)
        self.assertNotIn("fix the branch", row.detail)
        self.assertFalse(result.merged)

    def test_pending_checks_block_the_merge_without_merging(self):
        result = self.do_merge(self.fake(checks_state="pending", checks_pending=2))
        self.assertEqual(result.exit_code, 1)
        row = self.row(result, "checks")
        self.assertEqual(row.status, "pending")
        self.assertIn("2", row.detail)
        self.assertFalse(result.merged)
        self.assertIsNone(result.merge_sha)

    def test_failing_checks_still_fail_with_fix_advice(self):
        result = self.do_merge(self.fake(checks_state="failing"), plan=True)
        self.assertEqual(result.exit_code, 1)
        row = self.row(result, "checks")
        self.assertEqual(row.status, "fail")
        self.assertIn("fix the branch", row.detail)

    def test_mixed_pending_checks_and_failing_review_failing_wins(self):
        self.write("more.txt", "more\n")
        self.commit("more work")
        head = self.rev("HEAD")
        self.approve(head, verdict="block")
        fake = SequenceForge([self.pr("pending", 2)._replace(head_sha=head)])
        now_fn, sleep_fn, sleeps = self.clock()
        result = merge.run_merge(
            REPO,
            TICKET,
            FORGE_REPO,
            PR,
            head_sha=head,
            forge=fake,
            cwd=self.dir,
            wait=True,
            timeout=100,
            poll_s=10,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.row(result, "review").status, "fail")
        self.assertEqual(self.row(result, "checks").status, "pending")
        self.assertEqual(sleeps, [])
        self.assertEqual(len(fake.requested), 1)

    def test_wait_polls_until_passing_then_merges(self):
        fake = SequenceForge([self.pr("pending", 2), self.pr("pending", 1), self.pr("passing")])
        now_fn, sleep_fn, sleeps = self.clock()
        result = merge.run_merge(
            REPO,
            TICKET,
            FORGE_REPO,
            PR,
            head_sha=self.head,
            forge=fake,
            cwd=self.dir,
            wait=True,
            timeout=100,
            poll_s=10,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.merged)
        self.assertEqual(result.merge_sha, MERGE_SHA)
        self.assertEqual(sleeps, [10, 10])
        self.assertEqual(len(fake.calls), 1)

    def test_wait_stops_on_a_failing_check(self):
        fake = SequenceForge([self.pr("pending", 2), self.pr("failing")])
        now_fn, sleep_fn, sleeps = self.clock()
        result = merge.run_merge(
            REPO,
            TICKET,
            FORGE_REPO,
            PR,
            head_sha=self.head,
            forge=fake,
            cwd=self.dir,
            wait=True,
            timeout=100,
            poll_s=10,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.row(result, "checks").status, "fail")
        self.assertIn("fix the branch", self.row(result, "checks").detail)
        self.assertFalse(result.merged)
        self.assertEqual(fake.calls, [])
        self.assertEqual(sleeps, [10])

    def test_wait_times_out_with_pending(self):
        fake = SequenceForge([self.pr("pending", 3)])
        now_fn, sleep_fn, sleeps = self.clock()
        result = merge.run_merge(
            REPO,
            TICKET,
            FORGE_REPO,
            PR,
            head_sha=self.head,
            forge=fake,
            cwd=self.dir,
            wait=True,
            timeout=100,
            poll_s=10,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        self.assertEqual(result.exit_code, 1)
        row = self.row(result, "checks")
        self.assertEqual(row.status, "pending")
        self.assertIn("3", row.detail)
        self.assertIn("wait", row.detail)
        self.assertNotIn("fix the branch", row.detail)
        self.assertFalse(result.merged)
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(sleeps), 10)

    def test_wait_default_timeout_is_ten_minutes(self):
        self.assertEqual(merge.DEFAULT_WAIT_TIMEOUT_S, 600)

    def test_bad_timeout_is_usage(self):
        for timeout in ("soon", -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(merge.MergeError):
                    self.do_merge(self.fake(), wait=True, timeout=timeout)


class CliTest(MergeCase):
    def args(self, **overrides):
        base = {
            "repo": REPO,
            "number": TICKET,
            "pr": PR,
            "forge_repo": FORGE_REPO,
            "head": self.head,
            "method": "squash",
            "skip": [],
            "json": False,
            "plan": False,
            "wait": False,
            "timeout": None,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def call(self, args, fake):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mergecli.cmd_merge(args, forge=fake)
        return code, out.getvalue(), err.getvalue()

    def test_json_document_shape(self):
        code, out, _ = self.call(self.args(json=True), self.fake())
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(
            set(doc),
            {
                "schema_version",
                "repo",
                "pr",
                "head",
                "method",
                "checks",
                "skipped",
                "merged",
                "merge_sha",
            },
        )
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["repo"], FORGE_REPO)
        self.assertEqual(doc["pr"], PR)
        self.assertEqual(doc["head"], self.head)
        self.assertEqual(doc["method"], "squash")
        self.assertTrue(doc["merged"])
        self.assertEqual(doc["merge_sha"], MERGE_SHA)
        for row in doc["checks"]:
            self.assertEqual(set(row), {"name", "status", "detail"})

    def test_text_output_names_each_row_and_the_outcome(self):
        code, out, _ = self.call(self.args(), self.fake())
        self.assertEqual(code, 0)
        self.assertIn("merge open: ok", out)
        self.assertIn(f"merged {MERGE_SHA}", out)

    def test_refusal_text_names_the_failing_row(self):
        code, out, _ = self.call(self.args(), self.fake(from_fork=True))
        self.assertEqual(code, 1)
        self.assertIn("merge fork: fail", out)
        self.assertIn("not merged", out)

    def test_missing_pr_is_usage(self):
        code, _, err = self.call(self.args(pr=None), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("--pr", err)

    def test_missing_head_is_usage(self):
        code, _, err = self.call(self.args(head=None), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("--head", err)

    def test_bad_forge_repo_is_usage(self):
        code, _, err = self.call(self.args(forge_repo="not-a-repo"), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("--forge-repo", err)

    def test_no_origin_and_no_flag_is_usage(self):
        code, _, err = self.call(self.args(forge_repo=None), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("--forge-repo", err)

    def test_origin_remote_supplies_the_forge_repo(self):
        self.git("remote", "add", "origin", "git@github.com:acme/lib.git")
        code, out, _ = self.call(self.args(forge_repo=None), self.fake())
        self.assertEqual(code, 0)
        self.assertIn("approved by", out)

    def test_skip_of_anything_but_checks_is_usage(self):
        code, _, err = self.call(self.args(skip=["review"]), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("review", err)

    def test_unreachable_forge_is_exit_3(self):
        code, _, err = self.call(self.args(), FakeForge())
        self.assertEqual(code, 3)
        self.assertIn("forge unreachable", err)

    def test_plan_passes_through(self):
        code, out, _ = self.call(self.args(plan=True), self.fake())
        self.assertEqual(code, 0)
        self.assertIn("not merged", out)

    def test_plan_with_an_unreadable_lock_file_is_exit_4(self):
        path = merge.lock_path(FORGE_REPO)
        real_open = os.open

        def bad_open(p, *args, **kwargs):
            if p == path:
                raise OSError(errno.EACCES, "Permission denied")
            return real_open(p, *args, **kwargs)

        fake = self.fake()
        with mock.patch.object(merge.os, "open", side_effect=bad_open):
            code, _, err = self.call(self.args(plan=True), fake)
        self.assertEqual(code, 4)
        self.assertIn("cannot open the merge lock", err)
        self.assertEqual(fake.requested, [])

    def test_method_flag_reaches_the_forge(self):
        fake = self.fake()
        code, out, _ = self.call(self.args(method="merge"), fake)
        self.assertEqual(code, 0)
        self.assertIn(f"merged {MERGE_SHA}", out)
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "merge")])

    def test_no_flag_defaults_to_squash(self):
        fake = self.fake()
        code, out, _ = self.call(self.args(method=None), fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "squash")])

    def test_no_flag_json_reports_squash(self):
        code, out, _ = self.call(self.args(method=None, json=True), self.fake())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["method"], "squash")

    def test_project_config_selects_the_method(self):
        self.write("korapy.toml", '[merge]\nmethod = "merge"\n')
        fake = self.fake()
        code, out, _ = self.call(self.args(method=None, json=True), fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["method"], "merge")
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "merge")])

    def test_method_flag_wins_over_project_config(self):
        self.write("korapy.toml", '[merge]\nmethod = "merge"\n')
        fake = self.fake()
        code, _, _ = self.call(self.args(method="squash"), fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "squash")])

    def test_per_repo_method_wins_over_project_level(self):
        self.write(
            "korapy.toml",
            '[merge]\nmethod = "squash"\n'
            '\n[[repo]]\nname = "lib"\n'
            '\n[repo.merge]\nmethod = "merge"\n',
        )
        fake = self.fake()
        code, out, _ = self.call(self.args(method=None, json=True), fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["method"], "merge")
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "merge")])

    def test_unmatched_repo_name_falls_back_to_project_level(self):
        self.write(
            "korapy.toml",
            '[merge]\nmethod = "merge"\n'
            '\n[[repo]]\nname = "lib"\n'
            '\n[repo.merge]\nmethod = "squash"\n',
        )
        reviewrecord.write_record(
            "other",
            TICKET,
            self.head,
            reviewer="reviewer-1",
            author="author-1",
            verdict="approve",
            sessions_root=os.environ["KORAPY_SESSIONS"],
        )
        fake = self.fake()
        code, out, _ = self.call(self.args(method=None, repo="other", json=True), fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["method"], "merge")
        self.assertEqual(fake.calls, [("merge", FORGE_REPO, PR, self.head, "merge")])

    def test_bad_method_flag_is_usage(self):
        code, _, err = self.call(self.args(method="rebase"), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("merge.method", err)

    def test_bad_configured_method_is_usage(self):
        self.write("korapy.toml", '[merge]\nmethod = "rebase"\n')
        code, _, err = self.call(self.args(method=None), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("merge.method", err)

    def test_refused_method_text_names_the_setting(self):
        pulls = [make_pr(repo=FORGE_REPO, number=PR, base_sha=self.base, head_sha=self.head)]
        code, out, _ = self.call(self.args(method="merge"), MethodRefusedForge(pulls=pulls))
        self.assertEqual(code, 1)
        self.assertIn("merge method: fail", out)
        self.assertIn("merge.method", out)
        self.assertIn("not merged", out)

    def test_pending_text_names_the_wait(self):
        code, out, _ = self.call(
            self.args(plan=True), self.fake(checks_state="pending", checks_pending=1)
        )
        self.assertEqual(code, 1)
        self.assertIn("merge checks: pending", out)
        self.assertIn("1", out)
        self.assertIn("wait", out)
        self.assertIn("not merged", out)

    def test_timeout_without_wait_is_usage(self):
        code, _, err = self.call(self.args(timeout="60"), self.fake())
        self.assertEqual(code, 4)
        self.assertIn("--wait", err)

    def test_bad_timeout_is_usage(self):
        for timeout in ("soon", "-1"):
            with self.subTest(timeout=timeout):
                code, _, err = self.call(self.args(wait=True, timeout=timeout), self.fake())
                self.assertEqual(code, 4)
                self.assertIn("--timeout", err)

    def test_wait_merges_once_the_checks_pass(self):
        base = make_pr(repo=FORGE_REPO, number=PR, base_sha=self.base, head_sha=self.head)
        fake = SequenceForge(
            [
                base._replace(checks_state="pending"),
                base._replace(checks_state="passing"),
            ]
        )
        now = [1000.0]
        with (
            mock.patch.object(merge, "_monotonic", side_effect=lambda: now[0]),
            mock.patch.object(
                merge, "_sleep", side_effect=lambda secs: now.__setitem__(0, now[0] + secs)
            ),
        ):
            code, out, _ = self.call(self.args(wait=True, timeout="100"), fake)
        self.assertEqual(code, 0)
        self.assertIn(f"merged {MERGE_SHA}", out)
        self.assertEqual(len(fake.calls), 1)

    def test_wait_flags_parse(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mergecli.add_parser(sub)
        ns = parser.parse_args(
            [
                "merge",
                REPO,
                str(TICKET),
                "--pr",
                str(PR),
                "--head",
                self.head,
                "--wait",
                "--timeout",
                "60",
            ]
        )
        self.assertTrue(ns.wait)
        self.assertEqual(ns.timeout, "60")
        ns = parser.parse_args(["merge", REPO, str(TICKET), "--pr", str(PR), "--head", self.head])
        self.assertFalse(ns.wait)
        self.assertIsNone(ns.timeout)


def _no_flock(fd, op):
    """A flock seam that fails the test when the probe takes any lock."""
    raise AssertionError(f"plan probe took a lock: flock({fd}, {op})")


class ShaAgreementTest(unittest.TestCase):
    """`merge` and `review show` agree on what a full commit SHA is.

    The pattern lives once, in `korapy.merge` (`FULL_SHA`); `reviewcli`
    reuses it, so a loosened copy can never make `merge` accept a head
    the forge pin then refuses.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-sha-")
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)

    def test_no_second_sha_copy_in_reviewcli(self):
        self.assertFalse(hasattr(reviewcli, "_FULL_SHA"))
        self.assertFalse(hasattr(reviewcli, "FULL_SHA"))

    def test_full_shas_agree(self):
        valid = [
            "a" * 40,
            "F" * 40,
            "abCD" * 10,
            "0" * 64,
            "B" * 64,
            "  abCD0123456789abcdef0123456789abcdef0123\n",
        ]
        for raw in valid:
            with self.subTest(raw=raw):
                self.assertTrue(merge.FULL_SHA.fullmatch(raw.strip()))
                self.assertEqual(reviewcli._commit(raw, self.dir), raw.strip().lower())

    def test_non_shas_agree(self):
        invalid = [None, "", "short", "g" * 40, "a" * 39, "a" * 41, "a" * 63, "a" * 65]
        for raw in invalid:
            with self.subTest(raw=raw):
                text = "" if raw is None else raw
                self.assertIsNone(merge.FULL_SHA.fullmatch(text))
                self.assertIsNone(reviewcli._commit(raw, self.dir))


class LockTableTest(unittest.TestCase):
    """The lock-table parser: pure, fixture-driven, never touching `/proc`."""

    KEY = "08:01:12345"

    def table(self, *lines):
        return "\n".join(lines) + "\n"

    def test_write_lock_on_the_file_is_held(self):
        text = self.table(f"64: FLOCK  ADVISORY  WRITE 999 {self.KEY} 0 EOF")
        self.assertTrue(merge._lock_held(text, self.KEY))

    def test_read_lock_on_the_file_is_held(self):
        text = self.table(f"64: FLOCK  ADVISORY  READ 999 {self.KEY} 0 EOF")
        self.assertTrue(merge._lock_held(text, self.KEY))

    def test_lock_on_another_file_is_free(self):
        text = self.table("64: FLOCK  ADVISORY  WRITE 999 08:01:99999 0 EOF")
        self.assertFalse(merge._lock_held(text, self.KEY))

    def test_posix_lock_on_the_file_is_free(self):
        text = self.table(f"64: POSIX  ADVISORY  WRITE 999 {self.KEY} 0 EOF")
        self.assertFalse(merge._lock_held(text, self.KEY))

    def test_blocked_request_lines_never_match(self):
        text = self.table(f"8: -> FLOCK  ADVISORY  WRITE 999 {self.KEY} 0 EOF")
        self.assertFalse(merge._lock_held(text, self.KEY))

    def test_partial_keys_never_match(self):
        text = self.table("64: FLOCK  ADVISORY  WRITE 999 08:01:1234567 0 EOF")
        self.assertFalse(merge._lock_held(text, "08:01:123"))

    def test_empty_and_garbage_tables_are_free(self):
        self.assertFalse(merge._lock_held("", self.KEY))
        self.assertFalse(merge._lock_held("not a lock table\n", self.KEY))


class PlanLockTest(MergeCase):
    """`--plan` never writes and never takes the merge lock."""

    def held_path(self):
        path = merge.lock_path(FORGE_REPO)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def test_plan_creates_nothing_in_the_state_dir(self):
        result = self.do_merge(self.fake(), plan=True)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(os.path.exists(os.environ["KORAPY_STATE"]))

    def test_plan_probe_takes_no_lock(self):
        self.hold_lock()
        with mock.patch.object(merge, "_flock", _no_flock):
            result = self.do_merge(self.fake(), plan=True)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.merged)
        self.assertIn("held", self.row(result, "lock").detail)

    def test_lock_free_without_a_lock_file_is_free_and_creates_nothing(self):
        path = merge.lock_path(FORGE_REPO)
        with mock.patch.object(merge, "_flock", _no_flock):
            self.assertTrue(merge._lock_free(path))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.dirname(path)))

    def test_lock_free_sees_a_live_lock_without_taking_it(self):
        path = self.held_path()
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(merge, "_flock", _no_flock):
                self.assertFalse(merge._lock_free(path))
        finally:
            os.close(fd)
        with mock.patch.object(merge, "_flock", _no_flock):
            self.assertTrue(merge._lock_free(path))

    def test_lock_free_with_an_unreadable_table_is_usage(self):
        path = self.held_path()
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with mock.patch.object(merge, "_read_locks", side_effect=OSError("no proc")):
            with self.assertRaises(merge.MergeError):
                merge._lock_free(path)


if __name__ == "__main__":
    unittest.main()
