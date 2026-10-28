"""Tests for the forge interface and its GitHub adapter.

No test calls a real `gh`, the network, or real state dirs: the adapter
takes its runner as an argument and every test injects a fake.
`FakeForge` is the shared in-memory forge other test modules import.
"""

import json
import unittest
from unittest import mock

from corral import forge
from corral.github import forge as ghforge

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
OTHER_SHA = "c" * 40


def make_pr(number=7, repo="acme/lib", **overrides):
    fields = {
        "repo": repo,
        "number": number,
        "state": "open",
        "base_ref": "master",
        "base_sha": BASE_SHA,
        "head_ref": "feat/7-x",
        "head_sha": HEAD_SHA,
        "author": "reviewer-1",
        "from_fork": False,
        "labels": (),
    }
    fields.update(overrides)
    return forge.PullRequest(**fields)


class FakeForge:
    """In-memory forge for tests: canned PRs plus a branch index."""

    def __init__(self, pulls=(), index=None):
        self.pulls = {(pr.repo, pr.number): pr for pr in pulls}
        self.index = dict(index or {})
        self.requested = []

    def pull_request(self, repo, number):
        self.requested.append((repo, number))
        try:
            return self.pulls[(repo, number)]
        except KeyError:
            raise forge.ForgeError(f"no such PR: {repo}#{number}") from None

    def pr_index(self, repo):
        self.requested.append((repo,))
        return dict(self.index)


def pr_payload(number=7, **overrides):
    payload = {
        "number": number,
        "state": "open",
        "merged": False,
        "merged_at": None,
        "user": {"login": "author-1"},
        "labels": [{"name": "triage"}],
        "base": {
            "ref": "master",
            "sha": BASE_SHA,
            "repo": {"full_name": "acme/lib"},
        },
        "head": {
            "ref": "feat/7-x",
            "sha": HEAD_SHA,
            "repo": {"full_name": "acme/lib"},
        },
    }
    payload.update(overrides)
    return payload


class FakeRun:
    """A fake `gh` runner: path suffix to a JSON payload or an error."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        assert args[:1] == ["api"]
        path = args[1]
        answer = self.responses[path]
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)


class PullRequestTest(unittest.TestCase):
    def fetch(self, payload, number=7, repo="acme/lib"):
        run = FakeRun({f"repos/{repo}/pulls/{number}": payload})
        return ghforge.GithubForge(run).pull_request(repo, number), run

    def test_open_pr_same_repo(self):
        pr, run = self.fetch(pr_payload())
        self.assertEqual(
            pr,
            forge.PullRequest(
                repo="acme/lib",
                number=7,
                state="open",
                base_ref="master",
                base_sha=BASE_SHA,
                head_ref="feat/7-x",
                head_sha=HEAD_SHA,
                author="author-1",
                from_fork=False,
                labels=("triage",),
            ),
        )
        self.assertEqual(run.calls, [["api", "repos/acme/lib/pulls/7"]])

    def test_fork_pr_counts_as_fork(self):
        payload = pr_payload()
        payload["head"]["repo"] = {"full_name": "someone/lib"}
        pr, _ = self.fetch(payload)
        self.assertTrue(pr.from_fork)
        self.assertEqual(pr.repo, "acme/lib")

    def test_missing_head_repo_counts_as_fork(self):
        payload = pr_payload()
        payload["head"]["repo"] = None
        pr, _ = self.fetch(payload)
        self.assertTrue(pr.from_fork)

    def test_merged_pr_from_merged_at(self):
        payload = pr_payload(state="closed", merged_at="2026-10-01T00:00:00Z")
        pr, _ = self.fetch(payload)
        self.assertEqual(pr.state, "merged")

    def test_merged_pr_from_merged_flag(self):
        payload = pr_payload(state="closed", merged=True)
        pr, _ = self.fetch(payload)
        self.assertEqual(pr.state, "merged")

    def test_closed_unmerged_pr(self):
        payload = pr_payload(state="closed")
        pr, _ = self.fetch(payload)
        self.assertEqual(pr.state, "closed")

    def test_missing_pr_is_forge_error(self):
        missing = FakeRun({"repos/acme/lib/pulls/9": RuntimeError("gh exited 1")})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(missing).pull_request("acme/lib", 9)
        self.assertEqual(missing.calls, [["api", "repos/acme/lib/pulls/9"]])

    def test_runner_failure_is_forge_error(self):
        run = FakeRun({"repos/acme/lib/pulls/7": RuntimeError("boom")})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).pull_request("acme/lib", 7)

    def test_malformed_answers_are_forge_error(self):
        bad = [
            "not json",
            ["a", "list"],
            {"state": "open"},
            pr_payload(number=8),
            {**pr_payload(), "state": "draft"},
            {**pr_payload(), "base": {"ref": "master"}},
            {**pr_payload(), "head": {"ref": "x", "sha": HEAD_SHA, "repo": {}}},
            {**pr_payload(), "user": {}},
            {**pr_payload(), "labels": [{"name": 3}]},
            {**pr_payload(), "labels": "triage"},
        ]
        base = pr_payload()
        base["base"]["sha"] = "short"
        bad.append(base)
        for payload in bad:
            with self.subTest(payload=payload):
                run = FakeRun({"repos/acme/lib/pulls/7": payload})
                with self.assertRaises(forge.ForgeError):
                    ghforge.GithubForge(run).pull_request("acme/lib", 7)

    def test_bad_repo_never_calls_the_runner(self):
        run = FakeRun({})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).pull_request("not-a-repo", 7)
        self.assertEqual(run.calls, [])


class PrIndexTest(unittest.TestCase):
    def index_pr(self, ref, state="open", merged_at=None, merged=False, number=1):
        payload = pr_payload(number=number, state=state)
        payload["head"] = {"ref": ref, "sha": HEAD_SHA, "repo": {"full_name": "acme/lib"}}
        payload["merged_at"] = merged_at
        payload["merged"] = merged
        return payload

    def test_single_short_page(self):
        responses = {
            "repos/acme/lib/pulls?state=all&per_page=100&page=1": [
                self.index_pr("feat/1-a"),
                self.index_pr("feat/2-b", state="closed"),
                self.index_pr("feat/3-c", state="closed", merged_at="2026-10-01T00:00:00Z"),
            ]
        }
        index = ghforge.GithubForge(FakeRun(responses)).pr_index("acme/lib")
        self.assertEqual(index, {"feat/1-a": "open", "feat/2-b": "closed", "feat/3-c": "merged"})

    def test_pagination_follows_full_pages(self):
        with mock.patch.object(ghforge, "PER_PAGE", 2):
            first = [self.index_pr("feat/1-a", number=1), self.index_pr("feat/2-b", number=2)]
            second = [self.index_pr("feat/3-c", number=3)]
            responses = {
                "repos/acme/lib/pulls?state=all&per_page=2&page=1": first,
                "repos/acme/lib/pulls?state=all&per_page=2&page=2": second,
            }
            run = FakeRun(responses)
            index = ghforge.GithubForge(run).pr_index("acme/lib")
        self.assertEqual(index, {"feat/1-a": "open", "feat/2-b": "open", "feat/3-c": "open"})
        self.assertEqual(
            [c[1] for c in run.calls],
            [
                "repos/acme/lib/pulls?state=all&per_page=2&page=1",
                "repos/acme/lib/pulls?state=all&per_page=2&page=2",
            ],
        )

    def test_rest_api_only(self):
        responses = {"repos/acme/lib/pulls?state=all&per_page=100&page=1": []}
        run = FakeRun(responses)
        ghforge.GithubForge(run).pr_index("acme/lib")
        for call in run.calls:
            self.assertNotIn("graphql", call)

    def test_non_list_page_is_forge_error(self):
        responses = {"repos/acme/lib/pulls?state=all&per_page=100&page=1": {"number": 7}}
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(FakeRun(responses)).pr_index("acme/lib")

    def test_malformed_entry_is_forge_error(self):
        responses = {"repos/acme/lib/pulls?state=all&per_page=100&page=1": [{"state": "open"}]}
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(FakeRun(responses)).pr_index("acme/lib")

    def test_runner_failure_is_forge_error(self):
        responses = {"repos/acme/lib/pulls?state=all&per_page=100&page=1": RuntimeError("boom")}
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(FakeRun(responses)).pr_index("acme/lib")


class ForgeErrorTest(unittest.TestCase):
    def test_exit_code_is_unreachable(self):
        self.assertEqual(forge.ForgeError("x").exit_code, 3)
        self.assertIsInstance(forge.ForgeError("x"), Exception)


class FakeForgeTest(unittest.TestCase):
    def test_missing_pr_fails_closed(self):
        fake = FakeForge(pulls=[make_pr()])
        with self.assertRaises(forge.ForgeError):
            fake.pull_request("acme/lib", 999)
        self.assertEqual(fake.pull_request("acme/lib", 7), make_pr())

    def test_index_round_trips(self):
        fake = FakeForge(index={"feat/7-x": "merged"})
        self.assertEqual(fake.pr_index("acme/lib"), {"feat/7-x": "merged"})


class RemoteUrlTest(unittest.TestCase):
    def test_scp_like(self):
        self.assertEqual(ghforge.repo_from_remote_url("git@github.com:acme/lib.git"), "acme/lib")

    def test_https(self):
        self.assertEqual(
            ghforge.repo_from_remote_url("https://github.com/acme/lib.git"), "acme/lib"
        )
        self.assertEqual(ghforge.repo_from_remote_url("https://github.com/acme/lib"), "acme/lib")
        self.assertEqual(ghforge.repo_from_remote_url("https://github.com/acme/lib/"), "acme/lib")

    def test_ssh_and_git_schemes(self):
        self.assertEqual(
            ghforge.repo_from_remote_url("ssh://git@github.com/acme/lib.git"), "acme/lib"
        )
        self.assertEqual(ghforge.repo_from_remote_url("git://github.com/acme/lib.git"), "acme/lib")

    def test_non_github_remote_is_none(self):
        self.assertIsNone(ghforge.repo_from_remote_url("git@example.com:acme/lib.git"))
        self.assertIsNone(ghforge.repo_from_remote_url("https://example.com/acme/lib.git"))
        self.assertIsNone(ghforge.repo_from_remote_url("/srv/clones/lib"))
        self.assertIsNone(ghforge.repo_from_remote_url(""))
        self.assertIsNone(ghforge.repo_from_remote_url(None))

    def test_bad_paths_are_none(self):
        self.assertIsNone(ghforge.repo_from_remote_url("https://github.com/acme"))
        self.assertIsNone(ghforge.repo_from_remote_url("https://github.com/a/b/c"))
        self.assertIsNone(ghforge.repo_from_remote_url("git@github.com:acme/lib/.git"))


class OriginRepoTest(unittest.TestCase):
    def git(self, stdout="", returncode=0, fail=False):
        def run(args, cwd):
            self.assertEqual(args, ["remote", "get-url", "origin"])
            if fail:
                raise OSError("no git")
            return mock.Mock(returncode=returncode, stdout=stdout)

        return run

    def test_origin_url(self):
        run = self.git("git@github.com:acme/lib.git\n")
        self.assertEqual(ghforge.origin_repo("/clone", run), "acme/lib")

    def test_no_origin_is_none(self):
        run = self.git(stdout="", returncode=1)
        self.assertIsNone(ghforge.origin_repo("/clone", run))

    def test_git_failure_is_none(self):
        self.assertIsNone(ghforge.origin_repo("/clone", self.git(fail=True)))

    def test_non_github_origin_is_none(self):
        self.assertIsNone(ghforge.origin_repo("/clone", self.git("/srv/clones/lib\n")))


if __name__ == "__main__":
    unittest.main()
