"""`corral leakcheck` on real temporary repositories."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, leakcli, settings
from corral.gitenv import clean_env

TERM = "Acme Client"


class RepoCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in [n for n in os.environ if n.startswith("GIT_")]:
            del os.environ[name]
        os.environ.pop("BASE_REF", None)
        tmp = tempfile.TemporaryDirectory(prefix="corral-leakcheck-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.config)
        os.environ["CORRAL_CONFIG"] = self.config
        for setting in settings.REGISTRY.values():
            os.environ.pop(setting.env, None)
        host = mock.patch.object(leakcli, "_hostname", return_value="host-1.example.com")
        host.start()
        self.addCleanup(host.stop)
        home = mock.patch.object(leakcli, "_homedir", return_value="/users/someone")
        home.start()
        self.addCleanup(home.stop)
        self.repo = os.path.join(self.tmp, "clone")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "master")
        self.write("work.txt", "ordinary work\n")
        self.git("add", "-A")
        self.commit("base commit")
        self.base = self.rev("HEAD")

    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            check=True,
            capture_output=True,
            text=True,
            env=clean_env(),
        )

    def rev(self, ref):
        proc = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        return proc.stdout.strip()

    def commit(self, msg, cwd=None):
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg, cwd=cwd)

    def write(self, rel, text, root=None):
        with open(os.path.join(root or self.repo, rel), "w", encoding="utf-8") as handle:
            handle.write(text)

    def make_ops(self, text):
        ops = os.path.join(self.tmp, "ops")
        os.makedirs(ops, exist_ok=True)
        with open(os.path.join(ops, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)
        return ops

    def make_registered(self, private_text):
        """An ops root registered for the clone, holding `private_text`."""
        from corral import projects

        ops = self.make_ops(f'[[repo]]\nname = "app"\nclone.path = "{self.repo}"\n\n{private_text}')
        directory = projects.projects_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "demo.toml"), "w", encoding="utf-8") as handle:
            handle.write(f'name = "demo"\nunit_prefix = "demo-agent"\nroot = "{ops}"\n')
        return ops

    def run_cli(self, *argv, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        previous = os.getcwd()
        os.chdir(cwd or self.repo)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["leakcheck", *argv])
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()


class VerbTest(RepoCase):
    def test_a_commit_with_a_term_exits_1_and_prints_the_term(self):
        ops = self.make_ops('[private]\nterms = ["Acme Client"]\n')
        self.write("work.txt", "ordinary work\nAcme Client plans\n")
        self.git("add", "-A")
        self.commit("land Acme Client work")
        code, out, _ = self.run_cli("--ops", ops, "--base", self.base)
        self.assertEqual(code, 1)
        self.assertIn(TERM, out)
        self.assertIn("commit ", out)
        self.assertIn("corral.toml", out)

    def test_a_clean_repo_exits_0(self):
        ops = self.make_ops('[private]\nterms = ["Acme Client"]\n')
        self.write("work.txt", "ordinary work\nmore work\n")
        self.git("add", "-A")
        self.commit("more ordinary work")
        code, out, _ = self.run_cli("--ops", ops, "--base", self.base)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_a_term_in_the_branch_name_is_reported(self):
        ops = self.make_ops('[private]\nterms = ["acme-client"]\n')
        self.git("checkout", "-q", "-b", "feat/acme-client-x")
        code, out, _ = self.run_cli("--ops", ops, "--base", self.base)
        self.assertEqual(code, 1)
        self.assertIn("branch: acme-client (corral.toml)", out)

    def test_a_term_in_the_body_file_is_reported(self):
        ops = self.make_ops('[private]\nterms = ["Acme Client"]\n')
        body = os.path.join(self.tmp, "body.md")
        with open(body, "w", encoding="utf-8") as handle:
            handle.write("this ships Acme Client work\n")
        code, out, _ = self.run_cli("--ops", ops, "--base", self.base, "--body", body)
        self.assertEqual(code, 1)
        self.assertIn("body: Acme Client (corral.toml)", out)

    def test_added_diff_lines_carry_their_path_and_line(self):
        ops = self.make_ops('[private]\npatterns = ["acme-[0-9]+"]\n')
        self.write("work.txt", "ordinary work\nticket acme-42\n")
        self.git("add", "-A")
        self.commit("ordinary follow-up")
        code, out, _ = self.run_cli("--ops", ops, "--base", self.base)
        self.assertEqual(code, 1)
        self.assertIn("diff work.txt:2", out)
        self.assertIn("acme-[0-9]+ (corral.toml)", out)

    def test_json_pins_the_schema(self):
        ops = self.make_ops('[private]\nterms = ["Acme Client"]\n')
        self.write("work.txt", "Acme Client plans\n")
        self.git("add", "-A")
        self.commit("ordinary follow-up")
        code, out, _ = self.run_cli("--ops", ops, "--base", self.base, "--json")
        self.assertEqual(code, 1)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        (finding,) = document["findings"]
        self.assertEqual(
            sorted(finding),
            ["column", "excerpt", "line", "source", "term", "where"],
        )
        self.assertEqual(finding["term"], TERM)

    def test_without_an_ops_root_only_builtins_apply_with_a_note(self):
        code, out, err = self.run_cli("--base", self.base)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("built-ins", err)

    def test_a_bad_pattern_is_exit_4_and_names_the_pattern(self):
        ops = self.make_ops('[private]\npatterns = ["acme-([0-9]+"]\n')
        code, _, err = self.run_cli("--ops", ops, "--base", self.base)
        self.assertEqual(code, 4)
        self.assertIn("acme-([0-9]+", err)

    def test_a_bad_pattern_in_the_registered_ops_file_is_exit_4(self):
        self.make_registered('[private]\npatterns = ["acme-([0-9]+"]\n')
        code, _, err = self.run_cli("--base", self.base)
        self.assertEqual(code, 4)
        self.assertIn("acme-([0-9]+", err)

    def test_a_malformed_registered_ops_file_is_exit_4(self):
        self.make_registered("[private\n")
        code, _, err = self.run_cli("--base", self.base)
        self.assertEqual(code, 4)
        self.assertIn("corral.toml", err)

    def test_a_registered_ops_root_guards_without_ops(self):
        self.make_registered('[private]\nterms = ["Acme Client"]\n')
        self.write("work.txt", "ordinary work\nAcme Client plans\n")
        self.git("add", "-A")
        self.commit("ordinary follow-up")
        code, out, err = self.run_cli("--base", self.base)
        self.assertEqual(code, 1)
        self.assertIn("diff work.txt:2", out)
        self.assertNotIn("built-ins", err)

    def test_an_undecodable_body_file_is_exit_4(self):
        ops = self.make_ops('[private]\nterms = ["Acme Client"]\n')
        body = os.path.join(self.tmp, "body.md")
        with open(body, "wb") as handle:
            handle.write(b"\xff\xfe not utf-8")
        code, _, err = self.run_cli("--ops", ops, "--base", self.base, "--body", body)
        self.assertEqual(code, 4, err)
        self.assertIn("cannot read body file", err)

    def test_a_bad_base_ref_is_exit_4(self):
        ops = self.make_ops('[private]\nterms = ["Acme Client"]\n')
        code, _, err = self.run_cli("--ops", ops, "--base", "no-such-ref")
        self.assertEqual(code, 4, err)

    def test_an_ops_dir_without_corral_toml_is_exit_4(self):
        empty = os.path.join(self.tmp, "empty-ops")
        os.makedirs(empty)
        code, _, err = self.run_cli("--ops", empty, "--base", self.base)
        self.assertEqual(code, 4, err)

    def test_two_registrations_for_one_clone_are_ambiguous(self):
        from corral import projects

        self.make_registered('[private]\nterms = ["Acme Client"]\n')
        other = os.path.join(self.tmp, "ops2")
        os.makedirs(other)
        with open(os.path.join(other, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(f'[[repo]]\nname = "app"\nclone.path = "{self.repo}"\n')
        with open(
            os.path.join(projects.projects_dir(), "other.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write(f'name = "other"\nunit_prefix = "other-agent"\nroot = "{other}"\n')
        code, _, err = self.run_cli("--base", self.base)
        self.assertEqual(code, 4, err)
        self.assertIn("ambiguous", err)

    def test_a_worktree_subdirectory_resolves_the_registered_ops_root(self):
        self.make_registered('[private]\nterms = ["Acme Client"]\n')
        wt = os.path.join(self.tmp, "wt")
        self.git("worktree", "add", "-q", wt, "-b", "feature")
        sub = os.path.join(wt, "src", "deep")
        os.makedirs(sub)
        self.write("work.txt", "ordinary work\nAcme Client plans\n", root=wt)
        self.git("add", "-A", cwd=wt)
        self.commit("ordinary follow-up", cwd=wt)
        code, out, _ = self.run_cli("--base", self.base, cwd=sub)
        self.assertEqual(code, 1)
        self.assertIn("diff work.txt:2", out)

    def test_relative_clone_path_in_registration_resolves_from_foreign_cwd(self):
        from corral import projects

        ops = self.make_ops(
            '[[repo]]\nname = "app"\nclone.path = "../clone"\n\n[private]\nterms = ["Acme Client"]\n'
        )
        directory = projects.projects_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "demo.toml"), "w", encoding="utf-8") as handle:
            handle.write(f'name = "demo"\nunit_prefix = "demo-agent"\nroot = "{ops}"\n')
        foreign = os.path.join(self.tmp, "elsewhere", "deep")
        os.makedirs(foreign)
        previous = os.getcwd()
        os.chdir(foreign)
        try:
            found = leakcli.find_ops_root(self.repo)
        finally:
            os.chdir(previous)
        self.assertEqual(os.path.realpath(found), os.path.realpath(ops))


if __name__ == "__main__":
    unittest.main()
