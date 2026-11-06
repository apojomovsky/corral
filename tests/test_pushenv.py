"""korapy.pushenv: pushing when the SSH agent is unusable.

The decision is pure over the probe result, the origin URL form and the
`gh` login; the probe and the login check arrive as fakes. No test runs
ssh-add, gh, the network or the real home dir.
"""

import os
import subprocess
import tempfile
import unittest

from korapy import pushenv


SSH = "git@github.com:owner/repo.git"
SSH_URL = "ssh://git@github.com/owner/repo.git"
HTTPS = "https://github.com/owner/repo.git"


def _proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["git"], rc, out, err)


def _refuse(*args, **kwargs):
    raise AssertionError("the seam must not run for this origin")


class UrlFormTest(unittest.TestCase):
    def test_scp_like_github_address_is_ssh(self):
        self.assertTrue(pushenv.is_ssh_github_url(SSH))

    def test_ssh_scheme_github_address_is_ssh(self):
        self.assertTrue(pushenv.is_ssh_github_url(SSH_URL))

    def test_https_github_address_is_not_ssh(self):
        self.assertFalse(pushenv.is_ssh_github_url(HTTPS))

    def test_non_github_ssh_address_is_not_rewritten(self):
        self.assertFalse(pushenv.is_ssh_github_url("git@example.com:owner/repo.git"))
        self.assertFalse(pushenv.is_ssh_github_url("ssh://git@example.com/owner/repo.git"))

    def test_local_paths_and_blanks_are_not_ssh(self):
        self.assertFalse(pushenv.is_ssh_github_url("/srv/repos/owner/repo.git"))
        self.assertFalse(pushenv.is_ssh_github_url(None))
        self.assertFalse(pushenv.is_ssh_github_url("   "))

    def test_the_match_ignores_case(self):
        self.assertTrue(pushenv.is_ssh_github_url("GIT@GITHUB.COM:owner/repo.git"))


class DecideTest(unittest.TestCase):
    def test_a_working_agent_pushes_over_ssh(self):
        decision = pushenv.decide(True, SSH, False)
        self.assertEqual(decision.mode, "ssh")

    def test_no_agent_with_gh_logged_in_pushes_over_https(self):
        decision = pushenv.decide(False, SSH, True)
        self.assertEqual(decision.mode, "https")
        self.assertIn("HTTPS", decision.note)

    def test_no_agent_and_no_gh_login_refuses(self):
        decision = pushenv.decide(False, SSH, False)
        self.assertEqual(decision.mode, "refuse")
        self.assertIn("agent", decision.note)
        self.assertIn("gh auth login", decision.note)

    def test_a_https_origin_needs_neither_probe_nor_login(self):
        for ssh_ok in (True, False):
            for gh_ok in (True, False):
                decision = pushenv.decide(ssh_ok, HTTPS, gh_ok)
                self.assertEqual(decision.mode, "ssh")

    def test_a_non_github_ssh_origin_is_left_alone(self):
        decision = pushenv.decide(False, "git@example.com:owner/repo.git", False)
        self.assertEqual(decision.mode, "ssh")

    def test_a_missing_origin_is_left_alone(self):
        self.assertEqual(pushenv.decide(False, None, False).mode, "ssh")
        self.assertEqual(pushenv.decide(False, "", False).mode, "ssh")


class HttpsEnvTest(unittest.TestCase):
    def test_the_env_rewrites_ssh_github_urls_and_names_the_gh_helper(self):
        env = pushenv.https_env()
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        pairs = [
            (env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"])
            for n in range(int(env["GIT_CONFIG_COUNT"]))
        ]
        self.assertIn(("url.https://github.com/.insteadOf", "git@github.com:"), pairs)
        self.assertIn(("url.https://github.com/.insteadOf", "ssh://git@github.com/"), pairs)
        self.assertIn(("credential.https://github.com.helper", "!gh auth git-credential"), pairs)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_extra_env_leaves_ssh_decisions_empty(self):
        self.assertEqual(pushenv.extra_env(pushenv.Decision("ssh", "")), {})

    def test_extra_env_merges_over_the_base_without_mutating_it(self):
        base = {"KORAPY_AGENT_ID": "unit-1", "PATH": "/usr/bin"}
        merged = pushenv.extra_env(pushenv.Decision("https", "note"), base=base)
        self.assertEqual(merged["KORAPY_AGENT_ID"], "unit-1")
        self.assertEqual(merged["GIT_CONFIG_COUNT"], "3")
        self.assertNotIn("GIT_CONFIG_COUNT", base)


class OriginUrlTest(unittest.TestCase):
    def test_the_origin_remote_is_read_locally(self):
        seen = []

        def git(args, cwd):
            seen.append((list(args), cwd))
            return _proc(0, SSH + "\n")

        self.assertEqual(pushenv.origin_url("/clone", git), SSH)
        self.assertEqual(seen, [(["remote", "get-url", "origin"], "/clone")])

    def test_an_unreadable_origin_reads_as_none(self):
        self.assertIsNone(pushenv.origin_url("/clone", lambda a, c: _proc(1, "", "boom")))
        self.assertIsNone(pushenv.origin_url("/clone", lambda a, c: None))

        def failing(args, cwd):
            raise OSError("no git")

        self.assertIsNone(pushenv.origin_url("/clone", failing))
        self.assertIsNone(pushenv.origin_url("/clone", lambda a, c: _proc(0, "  \n")))


class SshProbeTest(unittest.TestCase):
    def home_with_key(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-push-"))
        ssh_dir = os.path.join(tmp, ".ssh")
        os.makedirs(ssh_dir)
        with open(os.path.join(ssh_dir, "id_ed25519"), "w", encoding="utf-8") as handle:
            handle.write("fake-key")
        return tmp

    def test_an_answering_agent_is_usable_with_or_without_identities(self):
        for rc in (0, 1):
            self.assertTrue(pushenv.default_ssh_probe(run=lambda argv: _proc(rc), home="/none"))

    def test_a_refusing_socket_falls_back_to_a_default_key(self):
        def probe(argv):
            return _proc(2, "", "refused")

        self.assertTrue(pushenv.default_ssh_probe(run=probe, home=self.home_with_key()))
        self.assertFalse(pushenv.default_ssh_probe(run=probe, home="/none"))

    def test_a_missing_ssh_add_falls_back_to_a_default_key(self):
        def missing(argv):
            raise OSError("no ssh-add")

        self.assertTrue(pushenv.default_ssh_probe(run=missing, home=self.home_with_key()))
        self.assertFalse(pushenv.default_ssh_probe(run=missing, home="/none"))


class GhLoginTest(unittest.TestCase):
    def test_a_zero_status_is_logged_in(self):
        self.assertTrue(pushenv.default_gh_login(gh_run=lambda: "Logged in"))

    def test_a_failing_status_is_not_logged_in(self):
        def failing():
            raise Exception("not logged in")

        self.assertFalse(pushenv.default_gh_login(gh_run=failing))


class ResolveTest(unittest.TestCase):
    def test_a_healthy_agent_never_asks_about_gh(self):
        decision = pushenv.resolve(SSH, ssh_probe=lambda: True, gh_login=_refuse)
        self.assertEqual(decision.mode, "ssh")

    def test_a_non_ssh_origin_calls_no_seam(self):
        decision = pushenv.resolve(HTTPS, ssh_probe=_refuse, gh_login=_refuse)
        self.assertEqual(decision.mode, "ssh")

    def test_a_missing_origin_calls_no_seam(self):
        decision = pushenv.resolve(None, ssh_probe=_refuse, gh_login=_refuse)
        self.assertEqual(decision.mode, "ssh")

    def test_a_dead_agent_with_gh_falls_back_to_https(self):
        decision = pushenv.resolve(SSH, ssh_probe=lambda: False, gh_login=lambda: True)
        self.assertEqual(decision.mode, "https")

    def test_a_dead_agent_without_gh_refuses(self):
        decision = pushenv.resolve(SSH, ssh_probe=lambda: False, gh_login=lambda: False)
        self.assertEqual(decision.mode, "refuse")


class LaunchEnvTest(unittest.TestCase):
    def stub_git(self, url):
        def git(args, cwd):
            if list(args) == ["remote", "get-url", "origin"]:
                return _proc(0, (url + "\n") if url else "")
            raise AssertionError(f"unexpected git call: {args}")

        return git

    def test_an_ssh_origin_without_an_agent_gets_the_https_env(self):
        env = pushenv.launch_env(
            "/clone",
            git=self.stub_git(SSH),
            ssh_probe=lambda: False,
            gh_login=lambda: True,
        )
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_a_healthy_agent_gets_no_extra_env(self):
        env = pushenv.launch_env(
            "/clone",
            git=self.stub_git(SSH),
            ssh_probe=lambda: True,
            gh_login=_refuse,
        )
        self.assertEqual(env, {})

    def test_a_local_origin_gets_no_extra_env_and_calls_no_seam(self):
        env = pushenv.launch_env(
            "/clone",
            git=self.stub_git("/srv/repos/demo.git"),
            ssh_probe=_refuse,
            gh_login=_refuse,
        )
        self.assertEqual(env, {})

    def test_neither_path_refuses_naming_both_fixes(self):
        with self.assertRaises(pushenv.PushRefused) as ctx:
            pushenv.launch_env(
                "/clone",
                git=self.stub_git(SSH),
                ssh_probe=lambda: False,
                gh_login=lambda: False,
            )
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("agent", str(ctx.exception))
        self.assertIn("gh auth login", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
