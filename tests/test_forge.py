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
    """In-memory forge for tests: canned PRs plus a branch index.

    The remote side (`current_user`, `repo_exists`, `create_repo`,
    `repo_empty`, `clone_url`, `describe_create`) is in-memory too:
    `repos` maps `"owner/name"` to a dict with `private`, `empty` and
    `url`. `fail` names methods that raise `ForgeError` (unreachable)
    and `race` names `"owner/name"` repos whose create raises
    `ForgeExists` even though the pre-check missed them.
    """

    def __init__(self, pulls=(), index=None, *, user="operator", repos=None, fail=(), race=()):
        self.pulls = {(pr.repo, pr.number): pr for pr in pulls}
        self.index = dict(index or {})
        self.requested = []
        self.user = user
        self.repos = dict(repos or {})
        self.fail = set(fail)
        self.race = set(race)
        self.created = []
        self.calls = []

    def pull_request(self, repo, number):
        self.requested.append((repo, number))
        try:
            return self.pulls[(repo, number)]
        except KeyError:
            raise forge.ForgeError(f"no such PR: {repo}#{number}") from None

    def pr_index(self, repo):
        self.requested.append((repo,))
        return dict(self.index)

    def _fail(self, name):
        if name in self.fail:
            raise forge.ForgeError(f"forge unreachable during {name}")

    def current_user(self):
        self.calls.append(("current_user",))
        self._fail("current_user")
        if isinstance(self.user, Exception):
            raise self.user
        return self.user

    def repo_exists(self, repo):
        self.calls.append(("repo_exists", repo))
        self._fail("repo_exists")
        return repo in self.repos

    def create_repo(self, owner, name, *, private=True):
        self.calls.append(("create_repo", owner, name, private))
        self._fail("create_repo")
        repo = f"{owner}/{name}"
        if repo in self.repos or repo in self.race:
            raise forge.ForgeExists(f"remote {repo} already exists")
        url = f"https://example.com/{repo}.git"
        self.repos[repo] = {"private": private, "empty": True, "url": url}
        self.created.append((owner, name, private))
        return url

    def repo_empty(self, repo):
        self.calls.append(("repo_empty", repo))
        self._fail("repo_empty")
        try:
            return self.repos[repo]["empty"]
        except KeyError:
            raise forge.ForgeError(f"no such repo: {repo}") from None

    def clone_url(self, repo):
        self.calls.append(("clone_url", repo))
        self._fail("clone_url")
        try:
            return self.repos[repo]["url"]
        except KeyError:
            raise forge.ForgeError(f"no such repo: {repo}") from None

    def describe_create(self, name, owner=None, *, private=True):
        self.calls.append(("describe_create", name, owner, private))
        visibility = "--private" if private else "--public"
        target = f"{owner}/{name}" if owner else name
        return f"gh repo create {target} {visibility}"


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


class CmdRun:
    """A fake `gh` runner keyed by the full argv tuple."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        answer = self.responses[tuple(args)]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)


class CurrentUserTest(unittest.TestCase):
    def test_login_round_trips(self):
        run = CmdRun({("api", "user"): {"login": "operator"}})
        self.assertEqual(ghforge.GithubForge(run).current_user(), "operator")
        self.assertEqual(run.calls, [["api", "user"]])

    def test_missing_login_is_forge_error(self):
        for payload in ({"login": ""}, {}, {"login": 7}, ["operator"]):
            with self.subTest(payload=payload):
                run = CmdRun({("api", "user"): payload})
                with self.assertRaises(forge.ForgeError):
                    ghforge.GithubForge(run).current_user()

    def test_runner_failure_is_forge_error(self):
        run = CmdRun({("api", "user"): RuntimeError("boom")})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).current_user()


class RepoExistsTest(unittest.TestCase):
    def test_present_repo_is_true(self):
        run = CmdRun({("api", "repos/acme/lib"): {"full_name": "acme/lib"}})
        self.assertTrue(ghforge.GithubForge(run).repo_exists("acme/lib"))

    def test_missing_repo_is_false(self):
        for message in ("gh: Not Found (HTTP 404)", "gh: not found"):
            with self.subTest(message=message):
                run = CmdRun({("api", "repos/acme/missing"): RuntimeError(message)})
                self.assertFalse(ghforge.GithubForge(run).repo_exists("acme/missing"))

    def test_unreachable_is_forge_error(self):
        run = CmdRun({("api", "repos/acme/lib"): RuntimeError("connection refused")})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).repo_exists("acme/lib")

    def test_bad_name_never_calls_the_runner(self):
        run = CmdRun({})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).repo_exists("not-a-repo")
        self.assertEqual(run.calls, [])


class CreateRepoTest(unittest.TestCase):
    URL = "https://github.com/acme/lib.git"

    def test_private_create_runs_the_exact_argv_and_returns_the_url(self):
        run = CmdRun(
            {
                ("repo", "create", "acme/lib", "--private"): "",
                ("api", "repos/acme/lib"): {"clone_url": self.URL},
            }
        )
        url = ghforge.GithubForge(run).create_repo("acme", "lib")
        self.assertEqual(url, self.URL)
        self.assertEqual(
            run.calls,
            [["repo", "create", "acme/lib", "--private"], ["api", "repos/acme/lib"]],
        )

    def test_public_create_passes_public(self):
        run = CmdRun(
            {
                ("repo", "create", "acme/lib", "--public"): "",
                ("api", "repos/acme/lib"): {"clone_url": self.URL},
            }
        )
        url = ghforge.GithubForge(run).create_repo("acme", "lib", private=False)
        self.assertEqual(url, self.URL)

    def test_already_exists_is_forge_exists(self):
        run = CmdRun(
            {("repo", "create", "acme/lib", "--private"): RuntimeError("Name already exists")}
        )
        with self.assertRaises(forge.ForgeExists):
            ghforge.GithubForge(run).create_repo("acme", "lib")

    def test_unreachable_is_forge_error(self):
        run = CmdRun(
            {("repo", "create", "acme/lib", "--private"): RuntimeError("connection refused")}
        )
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).create_repo("acme", "lib")

    def test_unusable_answer_is_forge_error(self):
        run = CmdRun(
            {
                ("repo", "create", "acme/lib", "--private"): "",
                ("api", "repos/acme/lib"): {"clone_url": ""},
            }
        )
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).create_repo("acme", "lib")

    def test_bad_shapes_never_call_the_runner(self):
        run = CmdRun({})
        adapter = ghforge.GithubForge(run)
        for owner, name in (("", "lib"), ("ac me", "lib"), ("acme", "li/b"), ("acme", "lib.git")):
            with self.subTest(owner=owner, name=name):
                with self.assertRaises(forge.ForgeError):
                    adapter.create_repo(owner, name)
        self.assertEqual(run.calls, [])


class RepoEmptyTest(unittest.TestCase):
    def test_no_branches_is_empty(self):
        run = CmdRun({("api", "repos/acme/lib/branches"): []})
        self.assertTrue(ghforge.GithubForge(run).repo_empty("acme/lib"))

    def test_a_branch_is_not_empty(self):
        run = CmdRun({("api", "repos/acme/lib/branches"): [{"name": "main"}]})
        self.assertFalse(ghforge.GithubForge(run).repo_empty("acme/lib"))

    def test_malformed_answer_is_forge_error(self):
        run = CmdRun({("api", "repos/acme/lib/branches"): {"name": "main"}})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).repo_empty("acme/lib")

    def test_runner_failure_is_forge_error(self):
        run = CmdRun({("api", "repos/acme/lib/branches"): RuntimeError("boom")})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).repo_empty("acme/lib")

    def test_bad_name_never_calls_the_runner(self):
        run = CmdRun({})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).repo_empty("not-a-repo")
        self.assertEqual(run.calls, [])


class CloneUrlTest(unittest.TestCase):
    URL = "https://github.com/acme/lib.git"

    def test_url_round_trips(self):
        run = CmdRun({("api", "repos/acme/lib"): {"clone_url": self.URL}})
        self.assertEqual(ghforge.GithubForge(run).clone_url("acme/lib"), self.URL)

    def test_missing_url_is_forge_error(self):
        run = CmdRun({("api", "repos/acme/lib"): {"full_name": "acme/lib"}})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).clone_url("acme/lib")

    def test_runner_failure_is_forge_error(self):
        run = CmdRun({("api", "repos/acme/lib"): RuntimeError("boom")})
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).clone_url("acme/lib")


class DescribeCreateTest(unittest.TestCase):
    def test_private_is_the_default(self):
        self.assertEqual(
            ghforge.GithubForge(CmdRun({})).describe_create("lib", "acme"),
            "gh repo create acme/lib --private",
        )

    def test_public_and_missing_owner(self):
        adapter = ghforge.GithubForge(CmdRun({}))
        self.assertEqual(
            adapter.describe_create("lib", "acme", private=False),
            "gh repo create acme/lib --public",
        )
        self.assertEqual(adapter.describe_create("lib"), "gh repo create lib --private")

    def test_bad_shapes_are_forge_error(self):
        adapter = ghforge.GithubForge(CmdRun({}))
        for name, owner in (("", "acme"), ("li/b", "acme"), ("lib.git", "acme"), ("lib", "ac me")):
            with self.subTest(name=name, owner=owner):
                with self.assertRaises(forge.ForgeError):
                    adapter.describe_create(name, owner)

    def test_describe_is_pure(self):
        run = CmdRun({})
        ghforge.GithubForge(run).describe_create("lib", "acme")
        self.assertEqual(run.calls, [])


class ForgeExistsTest(unittest.TestCase):
    def test_exit_code_is_race_lost(self):
        self.assertEqual(forge.ForgeExists("taken").exit_code, 2)
        self.assertIsInstance(forge.ForgeExists("taken"), forge.ForgeError)


class FakeForgeRemoteTest(unittest.TestCase):
    def empty_repo(self):
        return {"private": True, "empty": True, "url": "https://example.com/acme/lib.git"}

    def test_round_trip(self):
        fake = FakeForge(user="operator", repos={"acme/lib": self.empty_repo()})
        self.assertEqual(fake.current_user(), "operator")
        self.assertTrue(fake.repo_exists("acme/lib"))
        self.assertFalse(fake.repo_exists("acme/other"))
        self.assertTrue(fake.repo_empty("acme/lib"))
        self.assertEqual(fake.clone_url("acme/lib"), "https://example.com/acme/lib.git")
        self.assertEqual(fake.describe_create("lib", "acme"), "gh repo create acme/lib --private")

    def test_create_records_visibility(self):
        fake = FakeForge(user="operator")
        url = fake.create_repo("acme", "lib", private=False)
        self.assertEqual(url, "https://example.com/acme/lib.git")
        self.assertEqual(fake.created, [("acme", "lib", False)])
        self.assertFalse(fake.repos["acme/lib"]["private"])
        self.assertTrue(fake.repo_empty("acme/lib"))

    def test_create_on_an_existing_repo_raises_exists(self):
        fake = FakeForge(user="operator", repos={"acme/lib": self.empty_repo()})
        with self.assertRaises(forge.ForgeExists):
            fake.create_repo("acme", "lib")

    def test_race_repo_raises_exists_despite_a_negative_precheck(self):
        fake = FakeForge(user="operator", race={"acme/lib"})
        self.assertFalse(fake.repo_exists("acme/lib"))
        with self.assertRaises(forge.ForgeExists):
            fake.create_repo("acme", "lib")

    def test_fail_names_raise_forge_error(self):
        fake = FakeForge(user="operator", fail={"current_user", "repo_exists"})
        with self.assertRaises(forge.ForgeError):
            fake.current_user()
        with self.assertRaises(forge.ForgeError):
            fake.repo_exists("acme/lib")

    def test_user_exception_is_raised(self):
        fake = FakeForge(user=forge.ForgeError("down"))
        with self.assertRaises(forge.ForgeError):
            fake.current_user()


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


class RepoNameHelper(unittest.TestCase):
    def test_the_cli_and_adapter_share_one_definition(self):
        from corral import reviewcli

        self.assertFalse(hasattr(reviewcli, "_FORGE_REPO"))
        for good in ("acme/lib", "a-b/c.d"):
            self.assertTrue(ghforge.is_repo_name(good))
        for bad in ("lib", "a/b/c", "a b/c", "", None, 7):
            self.assertFalse(ghforge.is_repo_name(bad))


if __name__ == "__main__":
    unittest.main()
