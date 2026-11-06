"""`korapy takeoff` on scratch repositories, with fakes at the seams.

Every git operation here is local: a bare directory is the "remote",
and the remote read (`ls_remote`) and the check commands (`run_cmd`)
are injected fakes. No `gh`, no network, no real state dirs.
"""

import io
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from korapy import heavy, leakcli, reviewrecord, settings, takeoff
from korapy.gitenv import clean_env

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
        tmp = tempfile.TemporaryDirectory(prefix="korapy-takeoff-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
            os.environ[var] = os.path.join(self.tmp, var.lower().replace("korapy_", ""))
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
        self.heavy_calls = []
        self.heavy_error = None
        self.heavy_result = heavy.RunResult(
            command_exit=0,
            exit_code=0,
            output="heavy ok\n",
            weight=heavy.Weight(cpu_m=3000, memory_mb=6144),
            wall_s=1.0,
            unit="heavy-test",
            argv_hash="aa",
        )
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
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
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

    def heavy_run(
        self,
        argv,
        cwd,
        env,
        *,
        role,
        repo,
        weight,
        budget,
        timeout,
        host=None,
        pressure=None,
        starved_ratio=0.2,
    ):
        self.heavy_calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "role": role,
                "repo": repo,
                "weight": weight,
                "budget": budget,
                "timeout": timeout,
                "starved_ratio": starved_ratio,
            }
        )
        if self.heavy_error is not None:
            raise self.heavy_error
        return self.heavy_result

    def gate(self, **kwargs):
        params = {
            "body_file": self.body,
            "with_test": False,
            "ops_dir": self.ops,
            "ls_remote": self.ls_remote,
            "run_cmd": self.run_cmd,
            "heavy_run": self.heavy_run,
        }
        params.update(kwargs)
        return takeoff.run_takeoff(self.work, **params)

    def write_heavy_ops(self, takeoff_cmd="make test", caps=True, host=True):
        lines = [
            "[[repo]]",
            f'name = "{REPO}"',
            f'clone.path = "{self.main}"',
            f'clone.default_branch = "{MAIN}"',
            f'checks.test = "{takeoff_cmd}"',
            "[heavy]",
        ]
        if caps:
            lines += ["cpu_m = 4000", "memory_mb = 8192"]
        lines += [
            "[heavy.default_weight]",
            "cpu_m = 1000",
            "memory_mb = 1024",
            '[heavy.commands."make check"]',
            "cpu_m = 3000",
            "memory_mb = 6144",
        ]
        self.write_ops("\n".join(lines) + "\n")
        if host:
            config = os.path.join(os.environ["KORAPY_CONFIG"], "config.toml")
            os.makedirs(os.path.dirname(config), exist_ok=True)
            with open(config, "w", encoding="utf-8") as handle:
                handle.write("[resources.host]\ncpu_m = 8000\nmemory_mb = 16384\npids = 200\n")

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
            heavy_run=self.heavy_run,
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
        shutil.rmtree(os.environ["KORAPY_SESSIONS"], ignore_errors=True)
        self.write_record(self.head, verdict="block")
        result = self.gate()
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "review"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn(reviewrecord.commit_id(self.head), found.detail)

    def test_missing_record_fails_and_names_the_commit(self):
        shutil.rmtree(os.environ["KORAPY_SESSIONS"], ignore_errors=True)
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

    def test_heavy_command_routes_with_the_takeoff_role(self):
        self.write_heavy_ops(takeoff_cmd="make check")
        result = self.gate(body_file=None)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.status(result, "checks"), "ok")
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(len(self.heavy_calls), 1)
        call = self.heavy_calls[0]
        self.assertEqual(call["argv"], ["/bin/sh", "-c", "make check"])
        self.assertEqual(call["cwd"], self.work)
        self.assertEqual(call["role"], "takeoff")
        self.assertEqual(call["repo"], heavy.repo_key(self.work))
        self.assertEqual(call["weight"], heavy.Weight(cpu_m=3000, memory_mb=6144))
        self.assertEqual(call["timeout"], 1800)
        self.assertEqual(call["starved_ratio"], 0.2)

    def test_unlisted_command_still_uses_the_plain_seam(self):
        self.write_heavy_ops(takeoff_cmd="make lint")
        self.cmd_results["make lint"] = (0, "fine\n")
        result = self.gate(body_file=None)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.heavy_calls, [])
        self.assertEqual(self.cmd_calls, [("make lint", self.work)])

    def test_heavy_command_without_an_entry_uses_the_default_weight(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make build"\n'
            "[heavy]\n"
            "[heavy.default_weight]\n"
            "cpu_m = 1000\n"
            "memory_mb = 1024\n"
            '[heavy.commands."make build"]\n'
        )
        result = self.gate(body_file=None)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(self.heavy_calls), 1)
        self.assertEqual(self.heavy_calls[0]["weight"], heavy.Weight(cpu_m=1000, memory_mb=1024))

    def test_heavy_command_failure_fails_the_checks_row(self):
        self.write_heavy_ops(takeoff_cmd="make check")
        self.heavy_result = heavy.RunResult(
            command_exit=2,
            exit_code=2,
            output="broken\n",
            weight=heavy.Weight(cpu_m=3000, memory_mb=6144),
            wall_s=3.0,
            unit="heavy-test",
            argv_hash="aa",
        )
        result = self.gate(body_file=None)
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("make check", found.detail)
        self.assertIn("exited 2", found.detail)
        self.assertEqual(self.cmd_calls, [])

    def test_heavy_timeout_fails_like_a_plain_timeout(self):
        self.write_heavy_ops(takeoff_cmd="make check")
        self.heavy_error = subprocess.TimeoutExpired(["systemd-run"], 1800)
        result = self.gate(body_file=None)
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "fail")
        self.assertIn("exceeded checks.timeout_s", found.detail)

    def test_unreachable_scope_exits_3_with_a_fail_row(self):
        self.write_heavy_ops(takeoff_cmd="make check")
        self.heavy_error = heavy.ScopeUnreachable("cannot start scope: no bus")
        result = self.gate(body_file=None)
        self.assertEqual(result.exit_code, 3)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "fail")
        self.assertEqual(self.cmd_calls, [])

    def test_heavy_usage_error_is_exit_4(self):
        self.write_heavy_ops(takeoff_cmd="make check")
        self.heavy_error = heavy.HeavyError("cannot stop scope")
        with self.assertRaises(takeoff.TakeoffError):
            self.gate(body_file=None)

    def test_lane_cap_above_the_host_budget_is_exit_4(self):
        self.write_heavy_ops(takeoff_cmd="make check")
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make check"\n'
            "[heavy]\n"
            "cpu_m = 9000\n"
            "memory_mb = 8192\n"
            '[heavy.commands."make check"]\n'
            "cpu_m = 3000\n"
            "memory_mb = 6144\n"
        )
        with self.assertRaisesRegex(takeoff.TakeoffError, "9000"):
            self.gate(body_file=None)

    def test_old_suite_lock_setting_is_exit_4(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "checks.suite_lock_timeout_s = 3600\n"
        )
        with self.assertRaisesRegex(takeoff.TakeoffError, "unknown setting"):
            self.gate(body_file=None)

    def test_plan_lists_heavy_commands_without_running(self):
        self.write_heavy_ops(takeoff_cmd="make check")
        result = self.gate(body_file=None, plan=True)
        planned = [c for c in result.checks if c.name == "checks"]
        self.assertEqual([(c.status, c.detail) for c in planned], [("planned", "`make check`")])
        self.assertEqual(self.heavy_calls, [])
        self.assertEqual(self.cmd_calls, [])
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
                heavy_run=self.heavy_run,
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
        self.assertNotIn("checks.suite_lock_timeout_s", settings.REGISTRY)
        self.assertIn("korapy.takeoff", settings.PROVIDERS)

    def test_each_policy_check_names_its_flag_skip(self):
        for name in takeoff.SKIPPABLE_CHECKS:
            with self.subTest(skip=name):
                result = self.gate(skips=[name])
                found = [c for c in result.checks if c.name == name][0]
                self.assertEqual(found.status, "skipped")
                self.assertEqual(found.skipped_by, "flag")
                self.assertEqual(found.detail, f"skipped by --skip {name}")
                for check in result.checks:
                    if check.name != name:
                        self.assertEqual(check.status, "ok", check.name)
                self.assertEqual(result.exit_code, 0)

    def test_skipped_checks_command_never_runs(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            'takeoff = ["make test"]\n'
        )
        result = self.gate(body_file=None, skips=["checks"])
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "skipped")
        self.assertEqual(found.skipped_by, "flag")
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(result.exit_code, 0)

    def test_skipped_review_passes_without_a_record(self):
        shutil.rmtree(os.environ["KORAPY_SESSIONS"], ignore_errors=True)
        result = self.gate(skips=["review"])
        found = [c for c in result.checks if c.name == "review"][0]
        self.assertEqual(found.status, "skipped")
        self.assertEqual(found.skipped_by, "flag")
        self.assertEqual(result.exit_code, 0)

    def test_config_off_skips_report_config(self):
        self.write("bad.txt", "bad\n", root=self.work)
        self.git("add", "-A", cwd=self.work)
        self.commit("added a thing", cwd=self.work)
        bad = self.rev("HEAD", cwd=self.work)
        self.write_record(bad, verdict="approve")
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
            "[git.commits]\n"
            "conventional = false\n"
            "[checks]\n"
            "no_em_dash = false\n"
            "[review]\n"
            "required = false\n"
        )
        result = self.gate(body_file=None)
        for name, detail in (
            ("commits", "git.commits.conventional is false"),
            ("em-dash", "checks.no_em_dash is false"),
            ("review", "review.required is false"),
        ):
            with self.subTest(check=name):
                found = [c for c in result.checks if c.name == name][0]
                self.assertEqual(found.status, "skipped")
                self.assertEqual(found.skipped_by, "config")
                self.assertEqual(found.detail, detail)

    def test_body_without_a_file_reports_a_config_skip(self):
        result = self.gate(body_file=None)
        found = [c for c in result.checks if c.name == "body"][0]
        self.assertEqual(found.status, "skipped")
        self.assertEqual(found.skipped_by, "config")
        self.assertEqual(found.detail, "no --body file given")

    def test_passing_rows_carry_no_skip_provenance(self):
        result = self.gate()
        for check in result.checks:
            self.assertIsNone(check.skipped_by, check.name)

    def test_an_unknown_skip_is_a_usage_error_naming_it(self):
        with self.assertRaisesRegex(takeoff.TakeoffError, "bogus"):
            self.gate(skips=["bogus"])

    def test_identity_checks_refuse_a_flag_skip(self):
        for name in ("base", "worktree"):
            with self.subTest(skip=name):
                with self.assertRaisesRegex(takeoff.TakeoffError, name):
                    self.gate(skips=[name])

    def test_a_flag_skip_wins_over_a_config_off_skip(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[review]\n"
            "required = false\n"
        )
        result = self.gate(body_file=None, skips=["review"])
        found = [c for c in result.checks if c.name == "review"][0]
        self.assertEqual(found.status, "skipped")
        self.assertEqual(found.skipped_by, "flag")
        self.assertEqual(found.detail, "skipped by --skip review")

    def test_plan_reports_each_command_as_planned_without_running(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            'takeoff = ["make lint", "make test"]\n'
        )
        result = self.gate(plan=True)
        planned = [c for c in result.checks if c.name == "checks"]
        self.assertEqual(
            [(c.status, c.detail) for c in planned],
            [("planned", "`make lint`"), ("planned", "`make test`")],
        )
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(result.exit_code, 0)

    def test_plan_runs_nothing_by_default(self):
        result = self.gate(body_file=None, plan=True)
        planned = [c for c in result.checks if c.name == "checks"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].status, "planned")
        self.assertEqual(planned[0].detail, "`make test`")
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(result.exit_code, 0)

    def test_plan_resolves_the_checks_that_need_no_execution(self):
        result = self.gate(plan=True)
        self.assertEqual(result.base, self.base)
        by_name = {}
        for check in result.checks:
            by_name.setdefault(check.name, []).append(check.status)
        self.assertEqual(
            by_name,
            {
                "base": ["ok"],
                "worktree": ["ok"],
                "commits": ["ok"],
                "em-dash": ["ok"],
                "body": ["ok"],
                "leak": ["ok"],
                "review": ["ok"],
                "checks": ["planned"],
            },
        )

    def test_plan_mirrors_a_resolved_failure(self):
        self.write("loose.txt", "uncommitted\n", root=self.work)
        result = self.gate(plan=True)
        self.assertEqual(result.exit_code, 1)
        found = [c for c in result.checks if c.name == "worktree"][0]
        self.assertEqual(found.status, "fail")
        planned = [c for c in result.checks if c.name == "checks"]
        self.assertEqual([c.status for c in planned], ["planned"])
        self.assertEqual(self.cmd_calls, [])

    def test_plan_runs_nothing(self):
        result = self.gate(body_file=None, plan=True)
        self.assertEqual(result.exit_code, 0)
        planned = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(planned.status, "planned")
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(self.heavy_calls, [])

    def test_plan_with_a_skip_reports_the_skip(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
            'checks.test = "make test"\n'
            "[checks]\n"
            'takeoff = ["make test"]\n'
        )
        result = self.gate(body_file=None, skips=["checks"], plan=True)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "skipped")
        self.assertEqual(found.skipped_by, "flag")
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(result.exit_code, 0)

    def test_plan_with_no_commands_is_ok_and_runs_nothing(self):
        self.write_ops(
            "[[repo]]\n"
            f'name = "{REPO}"\n'
            f'clone.path = "{self.main}"\n'
            f'clone.default_branch = "{MAIN}"\n'
        )
        result = self.gate(body_file=None, plan=True)
        found = [c for c in result.checks if c.name == "checks"][0]
        self.assertEqual(found.status, "ok")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(self.heavy_calls, [])

    def test_plan_with_test_flag_plans_the_test_command(self):
        result = self.gate(body_file=None, with_test=True, plan=True)
        planned = [c for c in result.checks if c.name == "checks"]
        self.assertEqual([(c.status, c.detail) for c in planned], [("planned", "`make test`")])
        self.assertEqual(self.cmd_calls, [])
        self.assertEqual(result.exit_code, 0)

    def test_plan_unreachable_remote_exits_3_without_running(self):
        self.ls_error = takeoff.LsRemoteError("connection refused")
        result = self.gate(plan=True)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(self.cmd_calls, [])

    def test_plan_unknown_skip_is_a_usage_error(self):
        with self.assertRaisesRegex(takeoff.TakeoffError, "bogus"):
            self.gate(plan=True, skips=["bogus"])

    def test_planned_rows_carry_no_skip_provenance_or_fix(self):
        result = self.gate(body_file=None, plan=True)
        for check in result.checks:
            if check.status == "planned":
                self.assertIsNone(check.skipped_by, check.name)
                self.assertEqual(check.fix, "", check.name)


class TakeoffCliCase(unittest.TestCase):
    def setUp(self):
        self.case = TakeoffCase("test_all_ok")
        self.case.setUp()

    def tearDown(self):
        self.case.doCleanups()

    def test_cli_human_output_names_every_check(self):
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
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
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
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
        from korapy import takeoffcli
        import argparse

        self.case.remote_sha = "f" * 40
        parser = argparse.ArgumentParser(prog="korapy")
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
        from korapy import takeoffcli
        import argparse

        self.case.ls_error = takeoff.LsRemoteError("connection refused")
        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(["takeoff", "--ops", self.case.ops, self.case.work])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 3)

    def test_cli_scope_unreachable_exits_3(self):
        from korapy import takeoffcli
        import argparse

        self.case.write_heavy_ops(takeoff_cmd="make check")
        self.case.heavy_error = heavy.ScopeUnreachable("cannot start scope: no bus")
        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(
            sub,
            ls_remote=self.case.ls_remote,
            run_cmd=self.case.run_cmd,
            heavy_run=self.case.heavy_run,
        )
        args = parser.parse_args(["takeoff", "--ops", self.case.ops, self.case.work])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 3)
        self.assertIn("takeoff checks: FAIL", out.getvalue())
        self.assertEqual(self.case.cmd_calls, [])

    def test_cli_heavy_command_routes_through_the_lane(self):
        from korapy import takeoffcli
        import argparse

        self.case.write_heavy_ops(takeoff_cmd="make check")
        self.case.cmd_results["make check"] = (0, "checked\n")
        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(
            sub,
            ls_remote=self.case.ls_remote,
            run_cmd=self.case.run_cmd,
            heavy_run=self.case.heavy_run,
        )
        args = parser.parse_args(["takeoff", "--ops", self.case.ops, self.case.work])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.case.heavy_calls), 1)
        self.assertEqual(self.case.cmd_calls, [])

    def test_verb_is_on_the_korapy_parser(self):
        from korapy import cli

        parser = cli.build_parser()
        args = parser.parse_args(["takeoff"])
        self.assertEqual(args.command, "takeoff")

    def test_cli_skip_flag_skips_a_check(self):
        from korapy import takeoffcli
        import argparse

        shutil.rmtree(os.environ["KORAPY_SESSIONS"], ignore_errors=True)
        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            [
                "takeoff",
                "--ops",
                self.case.ops,
                "--body",
                self.case.body,
                "--skip",
                "review",
                self.case.work,
            ]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 0)
        self.assertIn("takeoff review: skipped - skipped by --skip review", out.getvalue())

    def test_cli_json_reports_skip_provenance(self):
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            [
                "takeoff",
                "--ops",
                self.case.ops,
                "--body",
                self.case.body,
                "--json",
                "--skip",
                "review",
                self.case.work,
            ]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 0)
        doc = json.loads(out.getvalue())
        rows = {c["name"]: c for c in doc["checks"]}
        self.assertEqual(rows["review"]["status"], "skipped")
        self.assertEqual(rows["review"]["skipped_by"], "flag")
        for name, row in rows.items():
            if name != "review":
                self.assertIsNone(row["skipped_by"], name)
            self.assertIn("skipped_by", row, name)

    def test_cli_unknown_skip_exits_4(self):
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            ["takeoff", "--ops", self.case.ops, "--skip", "bogus", self.case.work]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 4)
        self.assertIn("bogus", err.getvalue())

    def test_cli_identity_skip_exits_4(self):
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            ["takeoff", "--ops", self.case.ops, "--skip", "base", self.case.work]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 4)
        self.assertIn("base", err.getvalue())

    def test_skip_help_names_every_skippable_check(self):
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        takeoff_parser = sub.choices["takeoff"]
        skip_help = ""
        for action in takeoff_parser._actions:
            if "--skip" in action.option_strings:
                skip_help = action.help or ""
        self.assertTrue(skip_help, "the takeoff parser has no --skip flag")
        for name in takeoff.SKIPPABLE_CHECKS:
            with self.subTest(skip=name):
                self.assertIn(name, skip_help)

    def test_cli_plan_json_lists_planned_commands_without_running(self):
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            [
                "takeoff",
                "--ops",
                self.case.ops,
                "--body",
                self.case.body,
                "--json",
                "--plan",
                self.case.work,
            ]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 0)
        doc = json.loads(out.getvalue())
        planned = [c for c in doc["checks"] if c["name"] == "checks"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["status"], "planned")
        self.assertEqual(planned[0]["detail"], "`make test`")
        self.assertIsNone(planned[0]["skipped_by"])
        self.assertEqual(self.case.cmd_calls, [])

    def test_cli_plan_text_names_the_planned_commands(self):
        from korapy import takeoffcli
        import argparse

        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(
            ["takeoff", "--ops", self.case.ops, "--body", self.case.body, "--plan", self.case.work]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 0)
        self.assertIn("takeoff checks: planned - `make test`", out.getvalue())
        self.assertEqual(self.case.cmd_calls, [])

    def test_cli_plan_mirrors_a_resolved_failure(self):
        from korapy import takeoffcli
        import argparse

        self.case.remote_sha = "f" * 40
        parser = argparse.ArgumentParser(prog="korapy")
        sub = parser.add_subparsers(dest="command", required=True)
        takeoffcli.add_parser(sub, ls_remote=self.case.ls_remote, run_cmd=self.case.run_cmd)
        args = parser.parse_args(["takeoff", "--ops", self.case.ops, "--plan", self.case.work])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.getvalue())
        self.assertEqual(self.case.cmd_calls, [])


class NoEmDashInCodeCase(unittest.TestCase):
    def test_no_literal_em_dash_in_takeoff_sources(self):
        literal = chr(0x2014)
        for name in ("src/korapy/takeoff.py", "src/korapy/takeoffcli.py", "tests/test_takeoff.py"):
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
            with open(path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, 1):
                    self.assertNotIn(literal, line, f"{name}:{lineno} carries a literal em-dash")


class TakeoffDocstringCase(unittest.TestCase):
    def test_exit_zero_line_names_planned_rows(self):
        self.assertIn("0 every check ok, skipped or planned", takeoff.__doc__ or "")


class TicketRepoRelativePath(unittest.TestCase):
    """`_ticket_repo` with a relative `clone.path`: the ops root wins from any cwd."""

    def test_relative_clone_path_matches_from_a_foreign_working_directory(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-ticket-repo-")
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
        with open(os.path.join(ops, "korapy.toml"), "w", encoding="utf-8") as handle:
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
