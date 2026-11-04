"""Tests for the forge interface and its GitHub adapter.

No test calls a real `gh`, the network, or real state dirs: the adapter
takes its runner as an argument and every test injects a fake.
`FakeForge` is the shared in-memory forge other test modules import.
"""

import json
import unittest
from unittest import mock

from corral import forge, paths
from corral.github import forge as ghforge

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
OTHER_SHA = "c" * 40
MERGE_SHA = "d" * 40


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
        "checks_state": "passing",
        "merge_sha": None,
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

    def merge(self, repo, number, *, head_sha, method):
        """Squash-merge a canned PR, refusing like the adapter does.

        A refused merge is ``ForgeRefused``; a malformed repo, head, or
        method is ``ForgeError``, raised before any lookup, as in the
        adapter. The merged PR is stamped ``merged`` with ``MERGE_SHA``
        so tests can verify the post-merge read.
        """
        self.calls.append(("merge", repo, number, head_sha, method))
        self._fail("merge")
        if method != "squash":
            raise forge.ForgeError(f"unsupported merge method {method!r}")
        if not ghforge.is_repo_name(repo):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        ghforge._sha(head_sha, "head sha")
        try:
            pr = self.pulls[(repo, number)]
        except KeyError:
            raise forge.ForgeError(f"no such PR: {repo}#{number}") from None
        if pr.state != forge.OPEN:
            raise forge.ForgeRefused(f"PR {number} is {pr.state}, nothing to merge")
        if pr.head_sha != head_sha:
            raise forge.ForgeRefused(f"PR {number} head moved: want {head_sha}")
        self.pulls[(repo, number)] = pr._replace(state=forge.MERGED, merge_sha=MERGE_SHA)
        return MERGE_SHA

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
        return self.clone_url(repo)

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
        if owner is None:
            if not paths.plain_repo_name(name):
                raise forge.ForgeError(f"unusable forge name {name!r}")
        else:
            if (
                not isinstance(owner, str)
                or not owner
                or "/" in owner
                or any(ch.isspace() for ch in owner)
            ):
                raise forge.ForgeError(f"unusable forge owner {owner!r}")
            if not paths.plain_repo_name(name):
                raise forge.ForgeError(f"unusable forge name {name!r}")
        visibility = "--private" if private else "--public"
        target = f"{owner}/{name}" if owner else name
        return f"gh repo create {target} {visibility}"


def pr_payload(number=7, **overrides):
    payload = {
        "number": number,
        "state": "open",
        "merged": False,
        "merged_at": None,
        "mergeable_state": "clean",
        "merge_commit_sha": None,
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
                checks_state="passing",
                merge_sha=None,
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
        payload = pr_payload(
            state="closed",
            merged_at="2026-10-01T00:00:00Z",
            merge_commit_sha=MERGE_SHA,
        )
        pr, _ = self.fetch(payload)
        self.assertEqual(pr.state, "merged")
        self.assertEqual(pr.merge_sha, MERGE_SHA)

    def test_merged_pr_from_merged_flag(self):
        payload = pr_payload(state="closed", merged=True, merge_commit_sha=MERGE_SHA)
        pr, _ = self.fetch(payload)
        self.assertEqual(pr.state, "merged")
        self.assertEqual(pr.merge_sha, MERGE_SHA)

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


class ChecksStateTest(unittest.TestCase):
    def state_of(self, payload):
        run = FakeRun({"repos/acme/lib/pulls/7": payload})
        return ghforge.GithubForge(run).pull_request("acme/lib", 7).checks_state

    def test_clean_is_passing(self):
        self.assertEqual(self.state_of(pr_payload(mergeable_state="clean")), "passing")

    def test_unknown_is_pending(self):
        self.assertEqual(self.state_of(pr_payload(mergeable_state="unknown")), "pending")

    def test_unready_states_are_failing(self):
        for raw in ("blocked", "behind", "unstable", "dirty", "draft", "has_hooks"):
            with self.subTest(raw=raw):
                self.assertEqual(self.state_of(pr_payload(mergeable_state=raw)), "failing")

    def test_missing_or_bad_state_is_forge_error(self):
        payload = pr_payload()
        del payload["mergeable_state"]
        bad = [payload]
        for raw in (None, 7, "", "CLEAN", "bogus"):
            bad.append(pr_payload(mergeable_state=raw))
        for payload in bad:
            with self.subTest(payload=payload):
                run = FakeRun({"repos/acme/lib/pulls/7": payload})
                with self.assertRaises(forge.ForgeError):
                    ghforge.GithubForge(run).pull_request("acme/lib", 7)


class MergeShaTest(unittest.TestCase):
    def sha_of(self, payload):
        run = FakeRun({"repos/acme/lib/pulls/7": payload})
        return ghforge.GithubForge(run).pull_request("acme/lib", 7).merge_sha

    def test_open_pr_has_no_merge_sha(self):
        self.assertIsNone(self.sha_of(pr_payload()))

    def test_open_pr_ignores_a_preview_sha(self):
        self.assertIsNone(self.sha_of(pr_payload(merge_commit_sha=MERGE_SHA)))

    def test_closed_unmerged_pr_has_no_merge_sha(self):
        self.assertIsNone(self.sha_of(pr_payload(state="closed")))

    def test_merged_pr_reports_its_merge_sha(self):
        payload = pr_payload(
            state="closed",
            merged=True,
            merged_at="2026-10-01T00:00:00Z",
            merge_commit_sha=MERGE_SHA,
        )
        self.assertEqual(self.sha_of(payload), MERGE_SHA)

    def test_merged_pr_without_a_sha_is_forge_error(self):
        for raw in (None, "short", 7):
            with self.subTest(raw=raw):
                payload = pr_payload(
                    state="closed",
                    merged=True,
                    merged_at="2026-10-01T00:00:00Z",
                    merge_commit_sha=raw,
                )
                run = FakeRun({"repos/acme/lib/pulls/7": payload})
                with self.assertRaises(forge.ForgeError):
                    ghforge.GithubForge(run).pull_request("acme/lib", 7)


class MergeRun:
    """A scripted `gh` runner for the merge path: GETs in call order, one PUT."""

    def __init__(self, gets, put):
        self.gets = list(gets)
        self.put = put
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        assert args[:1] == ["api"]
        if len(args) > 1 and args[1] == "-X":
            if isinstance(self.put, Exception):
                raise self.put
            assert self.put is not None, "test made no merge call"
            return json.dumps(self.put)
        payload = self.gets.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)


class MergeSeamTest(unittest.TestCase):
    def merged_pr(self, sha=MERGE_SHA):
        return pr_payload(
            state="closed",
            merged=True,
            merged_at="2026-10-01T00:00:00Z",
            merge_commit_sha=sha,
        )

    def test_squash_merge_pins_the_head_and_returns_the_verified_sha(self):
        put = {"merged": True, "sha": MERGE_SHA, "message": "Pull Request successfully merged."}
        run = MergeRun([pr_payload(), self.merged_pr()], put)
        sha = ghforge.GithubForge(run).merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")
        self.assertEqual(sha, MERGE_SHA)
        self.assertEqual(
            run.calls[1],
            [
                "api",
                "-X",
                "PUT",
                "repos/acme/lib/pulls/7/merge",
                "-f",
                "merge_method=squash",
                "-f",
                f"sha={HEAD_SHA}",
            ],
        )

    def test_merged_pr_refuses_without_merging(self):
        run = MergeRun([self.merged_pr()], None)
        with self.assertRaises(forge.ForgeRefused):
            ghforge.GithubForge(run).merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")
        self.assertEqual(len(run.calls), 1)

    def test_closed_pr_refuses_without_merging(self):
        run = MergeRun([pr_payload(state="closed")], None)
        with self.assertRaises(forge.ForgeRefused):
            ghforge.GithubForge(run).merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")
        self.assertEqual(len(run.calls), 1)

    def test_moved_head_refuses_without_merging(self):
        payload = pr_payload()
        payload["head"]["sha"] = OTHER_SHA
        run = MergeRun([payload], None)
        with self.assertRaises(forge.ForgeRefused):
            ghforge.GithubForge(run).merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")
        self.assertEqual(len(run.calls), 1)

    def test_missing_pr_is_forge_error(self):
        run = MergeRun([RuntimeError("gh exited 1")], None)
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")
        self.assertEqual(len(run.calls), 1)

    def test_unknown_method_is_forge_error_before_any_call(self):
        run = MergeRun([], None)
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).merge("acme/lib", 7, head_sha=HEAD_SHA, method="rebase")
        self.assertEqual(run.calls, [])

    def test_bad_shapes_never_call_the_runner(self):
        run = MergeRun([], None)
        adapter = ghforge.GithubForge(run)
        with self.assertRaises(forge.ForgeError):
            adapter.merge("not-a-repo", 7, head_sha=HEAD_SHA, method="squash")
        with self.assertRaises(forge.ForgeError):
            adapter.merge("acme/lib", 7, head_sha="short", method="squash")
        self.assertEqual(run.calls, [])

    def test_runner_failure_during_merge_is_forge_error(self):
        run = MergeRun([pr_payload()], RuntimeError("connection refused"))
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")

    def test_merge_refusal_at_put_is_forge_refused(self):
        for message in (
            "gh: Pull Request is not mergeable (HTTP 405)",
            "gh: Head branch was modified. Review and try the merge again. (HTTP 409)",
        ):
            with self.subTest(message=message):
                run = MergeRun([pr_payload()], RuntimeError(message))
                with self.assertRaises(forge.ForgeRefused):
                    ghforge.GithubForge(run).merge(
                        "acme/lib", 7, head_sha=HEAD_SHA, method="squash"
                    )

    def test_other_put_failures_are_forge_error_not_refused(self):
        for message in (
            "connection refused",
            "gh: Not Found (HTTP 404)",
            "gh: Forbidden (HTTP 403)",
            "gh: Validation Failed (HTTP 422)",
            "failed to connect to 127.0.0.1 port 4050: Connection refused",
        ):
            with self.subTest(message=message):
                run = MergeRun([pr_payload()], RuntimeError(message))
                with self.assertRaises(forge.ForgeError) as ctx:
                    ghforge.GithubForge(run).merge(
                        "acme/lib", 7, head_sha=HEAD_SHA, method="squash"
                    )
                self.assertNotIsInstance(ctx.exception, forge.ForgeRefused)

    def test_unaccepted_merge_answer_is_forge_error(self):
        for put in ({"merged": False, "sha": MERGE_SHA}, {"merged": True, "sha": "short"}):
            with self.subTest(put=put):
                run = MergeRun([pr_payload()], put)
                with self.assertRaises(forge.ForgeError):
                    ghforge.GithubForge(run).merge(
                        "acme/lib", 7, head_sha=HEAD_SHA, method="squash"
                    )

    def test_verify_mismatch_fails_closed(self):
        put = {"merged": True, "sha": MERGE_SHA, "message": "Pull Request successfully merged."}
        cases = [pr_payload(), self.merged_pr(sha=OTHER_SHA)]
        for verify in cases:
            with self.subTest(verify=verify):
                run = MergeRun([pr_payload(), verify], put)
                with self.assertRaises(forge.ForgeError):
                    ghforge.GithubForge(run).merge(
                        "acme/lib", 7, head_sha=HEAD_SHA, method="squash"
                    )


class FakeForgeMergeTest(unittest.TestCase):
    def test_merge_stamps_merged_with_the_merge_sha(self):
        fake = FakeForge(pulls=[make_pr()])
        sha = fake.merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")
        self.assertEqual(sha, MERGE_SHA)
        pr = fake.pull_request("acme/lib", 7)
        self.assertEqual(pr.state, forge.MERGED)
        self.assertEqual(pr.merge_sha, MERGE_SHA)

    def test_merge_refuses_a_closed_pr(self):
        fake = FakeForge(pulls=[make_pr(state="closed")])
        with self.assertRaises(forge.ForgeRefused):
            fake.merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")

    def test_merge_refuses_a_moved_head(self):
        fake = FakeForge(pulls=[make_pr()])
        with self.assertRaises(forge.ForgeRefused):
            fake.merge("acme/lib", 7, head_sha=OTHER_SHA, method="squash")

    def test_merge_refuses_an_unknown_method(self):
        fake = FakeForge(pulls=[make_pr()])
        with self.assertRaises(forge.ForgeError):
            fake.merge("acme/lib", 7, head_sha=HEAD_SHA, method="rebase")

    def test_merge_of_a_missing_pr_is_forge_error(self):
        fake = FakeForge(pulls=[make_pr()])
        with self.assertRaises(forge.ForgeError):
            fake.merge("acme/lib", 9, head_sha=HEAD_SHA, method="squash")

    def test_merge_rejects_bad_shapes_like_the_adapter(self):
        fake = FakeForge(pulls=[make_pr()])
        for repo, head in (("not-a-repo", HEAD_SHA), ("acme/lib", "short")):
            with self.subTest(repo=repo, head=head):
                with self.assertRaises(forge.ForgeError) as ctx:
                    fake.merge(repo, 7, head_sha=head, method="squash")
                self.assertNotIsInstance(ctx.exception, forge.ForgeRefused)

    def test_fail_names_raise_forge_error(self):
        fake = FakeForge(pulls=[make_pr()], fail={"merge"})
        with self.assertRaises(forge.ForgeError):
            fake.merge("acme/lib", 7, head_sha=HEAD_SHA, method="squash")

    def test_each_checks_state_round_trips(self):
        pulls = [
            make_pr(number=n, checks_state=state)
            for n, state in enumerate(("passing", "pending", "failing"), start=1)
        ]
        fake = FakeForge(pulls=pulls)
        for number, state in ((1, "passing"), (2, "pending"), (3, "failing")):
            with self.subTest(state=state):
                self.assertEqual(fake.pull_request("acme/lib", number).checks_state, state)


class ForgeRefusedTest(unittest.TestCase):
    def test_exit_code_is_gate_said_no(self):
        self.assertEqual(forge.ForgeRefused("no").exit_code, 1)
        self.assertIsInstance(forge.ForgeRefused("no"), forge.ForgeError)


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
        run = CmdRun({("api", "repos/acme/missing"): RuntimeError("gh: Not Found (HTTP 404)")})
        self.assertFalse(ghforge.GithubForge(run).repo_exists("acme/missing"))

    def test_port_number_is_not_a_missing_repo(self):
        run = CmdRun(
            {
                ("api", "repos/acme/lib"): RuntimeError(
                    "failed to connect to 127.0.0.1 port 4040: Connection refused"
                )
            }
        )
        with self.assertRaises(forge.ForgeError):
            ghforge.GithubForge(run).repo_exists("acme/lib")

    def test_unauthorized_is_unreachable(self):
        for message in (
            "gh: Bad credentials (HTTP 401)",
            "gh: Resource not accessible by personal access token (HTTP 403)",
        ):
            with self.subTest(message=message):
                run = CmdRun({("api", "repos/acme/lib"): RuntimeError(message)})
                with self.assertRaises(forge.ForgeError):
                    ghforge.GithubForge(run).repo_exists("acme/lib")

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
        for owner, name in (
            ("", "lib"),
            ("ac me", "lib"),
            ("acme", "li/b"),
            ("acme", "lib.git"),
            ("acme", "-lib"),
            ("acme", ".lib"),
            ("acme", "li:b"),
            ("acme", "lïb"),
            ("acme", ""),
            ("acme", "-"),
        ):
            with self.subTest(owner=owner, name=name):
                with self.assertRaises(forge.ForgeError):
                    adapter.create_repo(owner, name)
        self.assertEqual(run.calls, [])


class RepoEmptyTest(unittest.TestCase):
    def test_no_branches_is_empty(self):
        run = CmdRun(
            {
                ("api", "repos/acme/lib/branches"): [],
                ("api", "repos/acme/lib/tags"): [],
            }
        )
        self.assertTrue(ghforge.GithubForge(run).repo_empty("acme/lib"))

    def test_a_branch_is_not_empty(self):
        run = CmdRun(
            {
                ("api", "repos/acme/lib/branches"): [{"name": "main"}],
                ("api", "repos/acme/lib/tags"): [],
            }
        )
        self.assertFalse(ghforge.GithubForge(run).repo_empty("acme/lib"))

    def test_a_surviving_tag_is_not_empty(self):
        run = CmdRun(
            {
                ("api", "repos/acme/lib/branches"): [],
                ("api", "repos/acme/lib/tags"): [{"name": "v0.1"}],
            }
        )
        self.assertFalse(ghforge.GithubForge(run).repo_empty("acme/lib"))

    def test_malformed_answer_is_forge_error(self):
        run = CmdRun(
            {
                ("api", "repos/acme/lib/branches"): {"name": "main"},
                ("api", "repos/acme/lib/tags"): [],
            }
        )
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
