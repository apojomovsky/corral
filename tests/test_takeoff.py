"""`corral takeoff` on scratch repositories, with fakes at the seams.

Every git operation here is local: a bare directory is the "remote",
and the remote read (`ls_remote`) and the check commands (`run_cmd`)
are injected fakes. No `gh`, no network, no real state dirs.
"""

import io
import json
import os
import subprocess
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import leakcli, reviewrecord, settings, takeoff
from corral.gitenv import clean_env

REPO = "myrepo"
NUMBER = 79
BRANCH = "feat/79-thing"
MAIN = "main"


def _proc(cmd, code, out):
    return subprocess.CompletedProcess(args=cmd, returncode=code, stdout=out, stderr="")


class TakeoffCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in [n for n in os.environ if n.startswith("GIT_")]:
            del os.environ[name]
        os.environ.pop("BASE_REF", None)
        for name in list(settings.REGISTRY.values()):
            os.environ.pop(name.env, None)
        tmp = tempfile.TemporaryDirectory(prefix="corral-takeoff-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        for var in ("CORRAL_CONFIG", "CORRAL_STATE", "CORRAL_CACHE", "CORRAL_SESSIONS"):
            os.environ[var] = os.path.join(self.tmp, var.lower().replace("corral_", ""))
        os.environ["HOME"] = os.path.join(self.tmp, "home")
        host = mock.patch.object(leakcli, "_hostname", return_value="host-1.example.com")
        host.start()
        self.addCleanup(host.stop)
        home = mock.patch.object(leakcli, "_homedir", return_value="/users/someone")
        home.start()
        self.addCleanup(home.stop)

        self.remote = os.path.join(self.tmp, "remote.git")
        self.main = os.path.join(self.tmp, "main")
        self.git("init", "-q", "--bare", cwd=self.tmp, target="remote.git")
        self.git("init", "-q", "-b", MAIN, cwd=self.tmp, target="main")
        self.write("work.txt", "ordinary work\n", root=self.main)
        self.git("add", "-A", cwd=self.main)
        self.commit("chore: base", cwd=self.main)
        self.git("remote", "add", "origin", self.remote, cwd=self.main)
        self.git("push", "-q", "origin", MAIN, cwd=self.main)
        self.base = self.rev("HEAD", cwd=self.main)

        self.work = os.path.join(self.main, ".worktrees", f"{NUMBER}-thing")
        self.git("worktree", "add", "-b", BRANCH, self.work, cwd=self.main)
        self.write("thing.txt", "the thing\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit(f"feat(thing): add the thing", cwd=self.work)
        self.head = self.rev("HEAD", cwd=self.work)

        self.ops = os.path.join(self.tmp, "ops")
        os.makedirs(self.ops)
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
        )
        self.body = os.path.join(self.tmp, "body.md")
        self.write_body(
            f"Closes #{NUMBER}\n\nDoes the thing.\n\n## Review\nreviewer-1: No findings.\n"
        )

        self.remote_sha = self.base
        self.ls_error = None
        self.ls_calls = []
        self.cmd_results = {}
        self.cmd_calls = []
        self.write_record(self.head, verdict="approve")

    def git(self, *args, cwd, target=None):
        if target is not None:
            args = (*args, target)
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=clean_env(),
        )

    def rev(self, ref, cwd):
        proc = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        return proc.stdout.strip()

    def commit(self, msg, cwd):
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg, cwd=cwd)

    def write(self, rel, text, root):
        with open(os.path.join(root, rel), "w", encoding="utf-8") as handle:
            handle.write(text)

    def write_ops(self, text):
        with open(os.path.join(self.ops, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def write_body(self, text):
        with open(self.body, "w", encoding="utf-8") as handle:
            handle.write(text)

    def write_record(self, commit, verdict="approve"):
        reviewrecord.write_record(
            REPO, NUMBER, commit, reviewer="reviewer-1", author="author-1", verdict=verdict
        )

    def ls_remote(self, branch, cwd):
        self.ls_calls.append((branch, cwd))
        if self.ls_error is not None:
            raise self.ls_error
        return self.remote_sha

    def run_cmd(self, cmd, cwd):
        self.cmd_calls.append((cmd, cwd))
        code, out = self.cmd_results.get(cmd, (0, "ok\n"))
        return _proc(cmd, code, out)

    def gate(self, **kwargs):
        params = {
            "body_file": self.body,
            "with_test": False,
            "ops_dir": self.ops,
            "ls_remote": self.ls_remote,
            "run_cmd": self.run_cmd,
        }
        params.update(kwargs)
        return takeoff.run_takeoff(self.work, **params)

    def status(self, result, name):
        for check in result.checks:
            if check.name == name:
                return check.status
        self.fail(f"no check named {name!r}")

    def test_all_ok(self):
        result = self.gate()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.base, self.base)
        self.assertEqual(
            [c.name for c in result.checks],
            ["base", "worktree", "commits", "em-dash", "body", "leak", "review", "checks"],
        )
        for check in result.checks:
            self.assertEqual(check.status, "ok", check.name)
        self.assertEqual(self.ls_calls, [(MAIN, self.work)])

    def test_json_document_is_pinned(self):
        result = self.gate()
        doc = {
            "schema_version": 1,
            "base": result.base,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                for c in result.checks
            ],
        }
        text = json.dumps(doc, sort_keys=True)
        roundtrip = json.loads(text)
        self.assertEqual(roundtrip["schema_version"], 1)
        self.assertEqual(roundtrip["base"], self.base)
        self.assertEqual(
            [(c["name"], c["status"]) for c in roundtrip["checks"]],
            [(c.name, "ok") for c in result.checks],
        )
        self.assertEqual(takeoff.SCHEMA_VERSION, 1)

    def test_main_clone_itself_is_refused(self):
        result = takeoff.run_takeoff(
            self.main,
            body_file=self.body,
            ops_dir=self.ops,
            ls_remote=self.ls_remote,
            run_cmd=self.run_cmd,
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.status(result, "worktree"), "fail")

    def test_dirty_tree_fails(self):
        self.write("loose.txt", "uncommitted\n", root=self.work)
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "worktree"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("uncommitted changes", found.detail)

    def test_status_failure_is_a_usage_error(self):
        real_git = takeoff._git

        def fake_git(args, cwd):
            if list(args) == ["status", "--porcelain"]:
                return _proc(args, 1, "")
            return real_git(args, cwd)

        with mock.patch.object(takeoff, "_git", side_effect=fake_git):
            with self.assertRaises(takeoff.TakeoffError):
                self.gate()

    def test_branch_must_match_the_ticket_pattern(self):
        self.git("checkout", "-q", "-b", "odd-name", cwd=self.work)
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "worktree"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("odd-name", found.detail)
        self.assertEqual(self.status(result, "body"), "fail")

    def test_behind_the_base_fails_with_a_merge_fix(self):
        self.write("next.txt", "next\n", root=self.main)
        self.git("add", "-A", cwd=self.main)
        self.commit("chore: advance the base", cwd=self.main)
        self.remote_sha = self.rev("HEAD", cwd=self.main)
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "worktree"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("behind", found.detail)
        self.assertIn(f"git merge {self.remote_sha}", found.fix)
        self.assertNotIn("rebase", found.fix)

    def test_base_missing_from_the_clone_fails_with_a_fetch_fix(self):
        self.remote_sha = "f" * 40
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "base"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn(f"git fetch origin {MAIN}", found.fix)

    def test_unreachable_remote_fails_closed_with_exit_3(self):
        self.ls_error = takeoff.LsRemoteError("connection refused")
        result = self.gate()
        self.assertEqual(result.exit_code, 3)
        found = [c for c in result.checks if c.name == "base"][0]
        self.assertEqual(found.status, "fail")

    def test_ls_timeout_fails_closed_with_exit_3(self):
        self.ls_error = subprocess.TimeoutExpired(["git", "ls-remote"], 30)
        result = self.gate()
        self.assertEqual(result.exit_code, 3)
        found = [c for c in result.checks if c.name == "base"][0]
        self.assertEqual(found.status, "fail")

    def test_ls_oserror_fails_closed_with_exit_3(self):
        self.ls_error = OSError("git is missing")
        result = self.gate()
        self.assertEqual(result.exit_code, 3)
        found = [c for c in result.checks if c.name == "base"][0]
        self.assertEqual(found.status, "fail")

    def test_a_moved_local_origin_master_does_not_pass_review(self):
        self.write("r.txt", "reviewed\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("feat(thing): the reviewed part", cwd=self.work)
        reviewed = self.rev("HEAD", cwd=self.work)
        self.write_record(reviewed, verdict="approve")
        self.git("checkout", "-q", MAIN, cwd=self.main)
        self.write("s.txt", "side\n", root=self.main)
        self.git("add", "-A", cwd=self.main)
        self.commit("feat(other): the side part", cwd=self.main)
        side = self.rev("HEAD", cwd=self.main)
        self.git("update-ref", "refs/remotes/origin/main", side, cwd=self.main)
        self.git("checkout", "-q", BRANCH, cwd=self.work)
        self.git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "merge",
            "--no-ff",
            "-q",
            "-m",
            f"Merge side into {BRANCH}",
            side,
            cwd=self.work,
        )
        merged = self.rev("HEAD", cwd=self.work)
        self.write_record(reviewed, verdict="approve")
        result = self.gate()
        self.assertEqual(merged, self.rev("HEAD", cwd=self.work))
        found = [c for c in result.checks if c.name == "review"][0]
        self.assertEqual(found.status, "fail", found.detail)
        self.assertIn(reviewrecord.commit_id(merged), found.detail)
        self.assertEqual(result.exit_code, 1)

    def test_a_clean_merge_up_of_the_remote_base_inherits_approval(self):
        self.write("r.txt", "reviewed\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("feat(thing): the reviewed part", cwd=self.work)
        reviewed = self.rev("HEAD", cwd=self.work)
        self.write_record(reviewed, verdict="approve")
        self.write("next.txt", "next\n", root=self.main)
        self.git("add", "-A", cwd=self.main)
        self.commit("chore: advance the base", cwd=self.main)
        self.remote_sha = self.rev("HEAD", cwd=self.main)
        self.git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "merge",
            "--no-ff",
            "-q",
            "-m",
            "Merge the base",
            self.remote_sha,
            cwd=self.work,
        )
        merged = self.rev("HEAD", cwd=self.work)
        self.write_record(reviewed, verdict="approve")
        result = self.gate()
        self.assertEqual(
            self.status(result, "review"),
            "ok",
            [(c.name, c.status, c.detail) for c in result.checks],
        )
        found = [c for c in result.checks if c.name == "review"][0]
        self.assertIn(reviewrecord.commit_id(reviewed), found.detail)
        self.assertEqual(self.status(result, "worktree"), "ok")

    def test_unconventional_commit_fails(self):
        self.write("bad.txt", "bad\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("added a thing", cwd=self.work)
        bad = self.rev("HEAD", cwd=self.work)
        self.write_record(bad, verdict="approve")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "commits"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn(bad[:7], found.detail)

    def test_conventional_check_off_skips(self):
        self.write("bad.txt", "bad\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("added a thing", cwd=self.work)
        bad = self.rev("HEAD", cwd=self.work)
        self.write_record(bad, verdict="approve")
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[git.commits]\n"
            "conventional = false\n"
        )
        result = self.gate()
        self.assertEqual(self.status(result, "commits"), "skipped")
        self.assertEqual(result.exit_code, 0)

    def test_em_dash_in_an_added_line_fails(self):
        self.write("dash.txt", "one \u2014 two\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("docs(thing): note the dash", cwd=self.work)
        head = self.rev("HEAD", cwd=self.work)
        self.write_record(head, verdict="approve")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.status(result, "em-dash"), "fail")
        self.assertEqual(self.status(result, "commits"), "ok")

    def test_em_dash_check_off_skips(self):
        self.write("dash.txt", "one \u2014 two\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("docs(thing): note the dash", cwd=self.work)
        head = self.rev("HEAD", cwd=self.work)
        self.write_record(head, verdict="approve")
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            "no_em_dash = false\n"
        )
        result = self.gate()
        self.assertEqual(self.status(result, "em-dash"), "skipped")
        self.assertEqual(self.status(result, "commits"), "ok")
        self.assertEqual(result.exit_code, 0)

    def test_review_required_off_skips(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[review]\n"
            "required = false\n"
        )
        result = self.gate(body_file=None)
        self.assertEqual(self.status(result, "review"), "skipped")
        self.assertEqual(result.exit_code, 0)

    def test_blocking_record_fails(self):
        shutil.rmtree(os.environ["CORRAL_SESSIONS"], ignore_errors=True)
        self.write_record(self.head, verdict="block")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "review"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn(reviewrecord.commit_id(self.head), found.detail)

    def test_missing_record_fails_and_names_the_commit(self):
        shutil.rmtree(os.environ["CORRAL_SESSIONS"], ignore_errors=True)
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "review"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn(reviewrecord.commit_id(self.head), found.detail)

    def test_body_with_a_literal_newline_fails(self):
        self.write_body(f"Closes #{NUMBER} with a literal \\n escape\n\n## Review\nok\n")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.status(result, "body"), "fail")

    def test_body_without_a_link_fails(self):
        self.write_body("No link here.\n\n## Review\nok\n")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "body"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn(str(NUMBER), found.detail)

    def test_body_with_an_empty_review_section_fails(self):
        self.write_body(f"Closes #{NUMBER}\n\n## Review\n")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.status(result, "body"), "fail")

    def test_body_with_an_em_dash_fails_the_em_dash_check(self):
        self.write_body(f"Closes #{NUMBER}\n\nOne \u2014 two.\n\n## Review\nok\n")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.status(result, "body"), "ok")
        self.assertEqual(self.status(result, "em-dash"), "fail")

    def test_body_without_a_file_is_skipped(self):
        result = self.gate(body_file=None)
        self.assertEqual(self.status(result, "body"), "skipped")
        self.assertEqual(result.exit_code, 0)

    def test_body_refs_link_counts(self):
        self.write_body(f"Refs #{NUMBER}\n\n## Review\nok\n")
        result = self.gate()
        self.assertEqual(self.status(result, "body"), "ok")

    def test_leak_finding_fails_without_naming_the_term(self):
        term = "Alpine Schema"
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[private]\n"
            f'terms = ["{term}"]\n'
        )
        self.write("leak.txt", "clean content\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit(f"feat(thing): add {term} support", cwd=self.work)
        head = self.rev("HEAD", cwd=self.work)
        self.write_record(head, verdict="approve")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "leak"][0]
        self.assertEqual(found.status, "fail")
        self.assertNotIn(term, found.detail)
        self.assertNotIn(term, found.fix)
        self.assertNotIn(
            term,
            json.dumps(
                {
                    "schema_version": 1,
                    "base": result.base,
                    "checks": [{"name": c.name, "detail": c.detail} for c in result.checks],
                }
            ),
        )

    def test_leak_term_errors_are_usage_errors(self):
        with mock.patch.object(
            leakcli, "guard_terms", side_effect=settings.SettingsError("bad pattern")
        ):
            with self.assertRaises(takeoff.TakeoffError):
                self.gate()

    def test_scrubbed_commit_message_hides_the_term(self):
        term = "Alpine Schema"
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[private]\n"
            f'terms = ["{term}"]\n'
        )
        self.write("bad.txt", "bad\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit(f"add {term} support", cwd=self.work)
        head = self.rev("HEAD", cwd=self.work)
        self.write_record(head, verdict="approve")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "commits"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn(head[:7], found.detail)
        self.assertNotIn(term, found.detail)
        self.assertIn("[redacted]", found.detail)

    def test_scrubbed_em_dash_line_hides_the_term(self):
        term = "Alpine Schema"
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[private]\n"
            f'terms = ["{term}"]\n'
        )
        self.write("dash.txt", f"one \u2014 {term} two\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("docs(thing): note the dash", cwd=self.work)
        head = self.rev("HEAD", cwd=self.work)
        self.write_record(head, verdict="approve")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "em-dash"][0]
        self.assertEqual(found.status, "fail")
        self.assertNotIn(term, found.detail)
        self.assertIn("[redacted]", found.detail)

    def test_scrubbed_command_tail_hides_the_term(self):
        term = "Alpine Schema"
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            'takeoff = ["make test"]\n'
            "[private]\n"
            f'terms = ["{term}"]\n'
        )
        self.cmd_results["make test"] = (1, f"failed on {term}\n")
        result = self.gate(body_file=None)
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "fail")
        self.assertNotIn(term, found.detail)
        self.assertIn("[redacted]", found.detail)

    def test_fallback_repo_uses_the_clone_name(self):
        reviewrecord.write_record(
            "main",
            NUMBER,
            self.head,
            reviewer="reviewer-1",
            author="author-1",
            verdict="approve",
        )
        result = self.gate(ops_dir=None, body_file=None)
        self.assertEqual(self.status(result, "review"), "ok")
        self.assertEqual(result.exit_code, 0)

    def test_takeoff_commands_run_through_the_seam(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            'takeoff = ["make test"]\n'
        )
        self.cmd_results["make test"] = (0, "all green\n")
        result = self.gate()
        self.assertEqual(self.status(result, "checks"), "ok")
        self.assertEqual(self.cmd_calls, [("make test", self.work)])
        self.assertEqual(result.exit_code, 0)

    def test_a_failing_command_shows_the_tail(self):
        lines = "".join(f"line{i:02d}\n" for i in range(1, 26))
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            'takeoff = ["make test"]\n'
        )
        self.cmd_results["make test"] = (1, lines)
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("line25", found.detail)
        self.assertIn("line06", found.detail)
        self.assertNotIn("line05", found.detail)
        self.assertIn("make test", found.fix)

    def test_a_timed_out_command_fails_with_the_raise_fix(self):
        lines = "".join(f"line{i:02d}\n" for i in range(1, 26))

        def slow(cmd, cwd):
            raise subprocess.TimeoutExpired(cmd, 1800, output=lines)

        result = self.gate(run_cmd=slow)
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("raise checks.timeout_s or fix the hang", found.fix)
        self.assertIn("line25", found.detail)
        self.assertIn("line06", found.detail)
        self.assertNotIn("line05", found.detail)

    def test_a_stalled_local_git_is_a_usage_error_not_a_traceback(self):
        boom = subprocess.TimeoutExpired(["git", "status"], 300)
        with mock.patch.object(takeoff.subprocess, "run", side_effect=boom):
            with self.assertRaises(takeoff.TakeoffError):
                takeoff._git(["status"], self.work)

    def test_an_invalid_timeout_setting_is_a_usage_error(self):
        for bad in ("0", "-5"):
            with self.subTest(value=bad):
                self.write_ops(
                    "[[repo]]\n"
                    f'name = "{REPO}"\n'
                    f'clone.path = "{self.main}"\n'
                    f'clone.default_branch = "{MAIN}"\n'
                    'checks.test = "make test"\n'
                    f"checks.timeout_s = {bad}\n"
                )
                with self.assertRaises(takeoff.TakeoffError):
                    self.gate()

    def test_a_detached_head_fails_the_worktree_check(self):
        self.git("checkout", "-q", "--detach", cwd=self.work)
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.status(result, "worktree"), "fail")

    def test_a_command_that_cannot_run_fails_the_checks_row(self):
        def broken(cmd, cwd):
            raise RuntimeError("no shell")

        result = self.gate(run_cmd=broken)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("could not run", found.detail)

    def test_a_leak_collection_failure_is_a_usage_error(self):
        with mock.patch.object(
            takeoff.leakcli, "collect", side_effect=takeoff.leakcli.CollectError("x")
        ):
            with self.assertRaises(takeoff.TakeoffError):
                self.gate()

    def test_production_run_cmd_kills_a_hanging_command(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            takeoff.production_run_cmd("sleep 30", self.work, timeout=1)

    def test_test_flag_adds_the_test_command(self):
        self.cmd_results["make test"] = (0, "green\n")
        result = self.gate(body_file=None, with_test=True)
        self.assertEqual(self.cmd_calls, [("make test", self.work)])
        self.assertEqual(self.status(result, "checks"), "ok")

    def test_test_flag_does_not_duplicate_the_list(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            'takeoff = ["make test"]\n'
        )
        self.cmd_results["make test"] = (0, "green\n")
        result = self.gate(body_file=None, with_test=True)
        self.assertEqual(self.cmd_calls, [("make test", self.work)])

    def test_no_commands_is_ok(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
        )
        result = self.gate(body_file=None)
        self.assertEqual(self.status(result, "checks"), "ok")
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(result.exit_code, 0)

    def test_test_command_runs_by_default(self):
        # The default fixture sets checks.test with no checks.takeoff list.
        self.cmd_results["make test"] = (0, "green\n")
        result = self.gate(body_file=None, with_test=False)
        self.assertEqual(self.cmd_calls, [("make test", self.work)])
        self.assertEqual(self.status(result, "checks"), "ok")

    def test_two_entries_sharing_a_clone_are_refused(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[[repo]]\n"
            'name = "other"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
        )
        with self.assertRaisesRegex(takeoff.TakeoffError, "distinct clone.path"):
            self.gate()

    def test_unreadable_body_is_a_usage_error(self):
        with self.assertRaises(takeoff.TakeoffError):
            self.gate(body_file=os.path.join(self.tmp, "missing.md"))

    def test_unknown_path_is_a_usage_error(self):
        with self.assertRaises(takeoff.TakeoffError):
            takeoff.run_takeoff(
                os.path.join(self.tmp, "nowhere"),
                ops_dir=self.ops,
                ls_remote=self.ls_remote,
                run_cmd=self.run_cmd,
            )

    def test_seams_have_no_default(self):
        run = getattr(takeoff, "run_takeoff")
        with self.assertRaises(TypeError):
            run(self.work)

    def test_settings_are_registered(self):
        self.assertEqual(settings.REGISTRY["checks.takeoff"].default, [])
        self.assertEqual(settings.REGISTRY["checks.no_em_dash"].default, True)
        self.assertEqual(settings.REGISTRY["review.required"].default, True)
        self.assertEqual(settings.REGISTRY["checks.timeout_s"].default, 1800)
        self.assertIn("corral.takeoff", settings.PROVIDERS)


class TakeoffCliCase(unittest.TestCase):
    def setUp(self):
        self.case = TakeoffCase("test_all_ok")
        self.case.setUp()

    def tearDown(self):
        self.case.doCleanups()

    def test_cli_human_output_names_every_check(self):
        from corral import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="corral")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            ["takeoff", "--ops", self.case.ops, "--body", self.case.body, self.case.work]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 0)
        text = out.getvalue()
        for name in ("base", "worktree", "commits", "em-dash", "body", "leak", "review", "checks"):
            self.assertIn(f"takeoff {name}: ok", text)

    def test_cli_json_flag_prints_the_pinned_document(self):
        from corral import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="corral")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            ["takeoff", "--ops", self.case.ops, "--body", self.case.body, "--json", self.case.work]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["base"], self.case.base)
        self.assertEqual(
            [c["name"] for c in doc["checks"]],
            ["base", "worktree", "commits", "em-dash", "body", "leak", "review", "checks"],
        )

    def test_cli_failure_prints_the_fix(self):
        from corral import takeoffcli
        import argparse

        self.case.remote_sha = "f" * 40
        parser = argparse.ArgumentParser(prog="corral")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(["takeoff", "--ops", self.case.ops, self.case.work])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.getvalue())
        self.assertIn(f"git fetch origin {MAIN}", out.getvalue())

    def test_cli_unreachable_remote_exits_3(self):
        from corral import takeoffcli
        import argparse

        self.case.ls_error = takeoff.LsRemoteError("connection refused")
        parser = argparse.ArgumentParser(prog="corral")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(["takeoff", "--ops", self.case.ops, self.case.work])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 3)

    def test_verb_is_on_the_corral_parser(self):
        from corral import cli

        parser = cli.build_parser()
        args = parser.parse_args(["takeoff"])
        self.assertEqual(args.command, "takeoff")


class NoEmDashInCodeCase(unittest.TestCase):
    def test_no_literal_em_dash_in_takeoff_sources(self):
        literal = chr(0x2014)
        for name in ("src/corral/takeoff.py", "src/corral/takeoffcli.py", "tests/test_takeoff.py"):
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
            with open(path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, 1):
                    self.assertNotIn(literal, line, f"{name}:{lineno} carries a literal em-dash")


class TicketRepoRelativePath(unittest.TestCase):
    """`_ticket_repo` with a relative `clone.path`: the ops root wins from any cwd."""

    def test_relative_clone_path_matches_from_a_foreign_working_directory(self):
        tmp = tempfile.TemporaryDirectory(prefix="corral-ticket-repo-")
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        ops = os.path.join(root, "ops")
        clone = os.path.join(root, "clone")
        # Nested, so a relative clone.path resolved against the working
        # directory lands somewhere else: only the ops root is correct.
        foreign = os.path.join(root, "elsewhere", "deep")
        os.makedirs(ops)
        os.makedirs(clone)
        os.makedirs(foreign)
        with open(os.path.join(ops, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\nclone.path = "../clone"\n')
        previous = os.getcwd()
        os.chdir(foreign)
        try:
            self.assertEqual(takeoff._ticket_repo(ops, os.path.realpath(clone)), ("app", True))
            self.assertEqual(takeoff._ticket_repo(None, os.path.realpath(clone)), ("clone", False))
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
