"""The tracker interface and its GitHub adapter. Fakes only; no network."""

import calendar
import json
import unittest
from typing import Any

from corral import board, tracker
from corral.github import gh as ghmod
from corral.github import tracker as ghtracker

T0 = calendar.timegm((2026, 10, 28, 12, 0, 0, 0, 0, 0))
ISO0 = "2026-10-28T12:00:00Z"
ISO1 = "2026-10-28T13:00:00Z"
T1 = T0 + 3600.0


def item_payload(number, **over):
    """One issues-endpoint payload with sane defaults; override any field."""
    payload = {
        "number": number,
        "title": f"Title {number}",
        "body": f"Body {number}",
        "state": "open",
        "user": {"login": "alice"},
        "labels": [{"name": "ready"}, {"name": "priority:high"}],
        "assignees": [{"login": "bob"}],
        "html_url": f"https://example.com/acme/app/{number}",
        "updated_at": ISO0,
    }
    payload.update(over)
    return payload


def pull_payload(number, fork=False, **over):
    """One pulls-endpoint payload; only head.repo.fork matters here."""
    payload = {"number": number, "head": {"repo": {"fork": fork}}}
    payload.update(over)
    return payload


class FakeRun:
    """A `gh` seam double: path to JSON text or exception. Records calls."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, path, payload):
        self.routes[path] = json.dumps(payload)
        return self

    def fail(self, path, error=RuntimeError("gh failed")):
        self.routes[path] = error
        return self

    def __call__(self, args):
        self.calls.append(list(args))
        path = args[1]
        answer = self.routes.get(path, RuntimeError(f"no route for {path}"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self):
        return [args[1] for args in self.calls]


class FakeTracker:
    """An in-memory tracker double. Tests preload items and program failures."""

    def __init__(self, items=()):
        self.by_key = {(item.repo, item.number): item for item in items}
        self.changes_error = None
        self.item_errors = {}
        self.items_break_after = None
        self.write_errors = {}
        self.comment_errors = {}
        self.label_errors = {}
        self.calls = []

    def add(self, item):
        self.by_key[(item.repo, item.number)] = item
        return self

    def item(self, repo, number):
        self.calls.append(("item", repo, number))
        if (repo, number) in self.item_errors:
            raise tracker.TrackerError(self.item_errors[(repo, number)])
        try:
            return self.by_key[(repo, number)]
        except KeyError:
            raise tracker.TrackerError(f"unknown item {repo}#{number}") from None

    def changes(self, repo, cursor):
        self.calls.append(("changes", repo, cursor))
        if self.changes_error is not None:
            raise tracker.TrackerError(self.changes_error)
        since = tracker.parse_cursor(cursor) if cursor is not None else None
        found = [
            item
            for item in self.by_key.values()
            if item.repo == repo and (since is None or item.updated_at >= since)
        ]
        found.sort(key=lambda item: item.updated_at)
        if not found:
            return [], cursor
        return found, tracker.format_cursor(found[-1].updated_at)

    def items(self, repo):
        listed = sorted(
            (item for item in self.by_key.values() if item.repo == repo),
            key=lambda item: item.updated_at,
        )
        for index, item in enumerate(listed):
            if self.items_break_after is not None and index >= self.items_break_after:
                raise tracker.TrackerError(f"page error at index {index}")
            yield item

    def write(self, repo, number, field, value):
        """Record one corral-owned field write; programmed errors raise."""
        self.calls.append(("write", repo, number, field, value))
        if (repo, number) in self.write_errors:
            raise self.write_errors[(repo, number)]

    def comment(self, repo, number, body):
        """Record one decision comment; programmed errors raise."""
        self.calls.append(("comment", repo, number, body))
        if (repo, number) in self.comment_errors:
            raise self.comment_errors[(repo, number)]

    def add_label(self, repo, number, label):
        """Record one decision label; programmed errors raise."""
        self.calls.append(("add_label", repo, number, label))
        if (repo, number) in self.label_errors:
            raise self.label_errors[(repo, number)]


def reading(repo="acme/app", number=7, **over: Any):
    """One TrackerItem with sane defaults; override any field."""
    params: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "kind": "issue",
        "title": "Title",
        "body": "Body",
        "state": "open",
        "author": "alice",
        "labels": ("ready",),
        "priority": "high",
        "assignees": ("bob",),
        "url": "https://example.com/acme/app/7",
        "updated_at": float(T0),
        "from_fork": False,
    }
    params.update(over)
    return tracker.TrackerItem(**params)


class ItemTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeRun()
        self.adapter = ghtracker.GithubTracker(self.gh)

    def test_issue_payload_parses(self):
        self.gh.add("repos/acme/app/issues/7", item_payload(7))
        found = self.adapter.item("acme/app", 7)
        self.assertEqual(found.repo, "acme/app")
        self.assertEqual(found.number, 7)
        self.assertEqual(found.kind, "issue")
        self.assertEqual(found.title, "Title 7")
        self.assertEqual(found.body, "Body 7")
        self.assertEqual(found.state, "open")
        self.assertEqual(found.author, "alice")
        self.assertEqual(found.labels, ("ready", "priority:high"))
        self.assertEqual(found.priority, "high")
        self.assertEqual(found.assignees, ("bob",))
        self.assertEqual(found.url, "https://example.com/acme/app/7")
        self.assertEqual(found.updated_at, float(T0))
        self.assertFalse(found.from_fork)
        self.assertEqual(self.gh.paths(), ["repos/acme/app/issues/7"])

    def test_pr_in_issues_feed_is_kind_pr(self):
        payload = item_payload(9)
        payload["pull_request"] = {"url": "https://example.com/pulls/9"}
        self.gh.add("repos/acme/app/issues/9", payload)
        self.gh.add("repos/acme/app/pulls/9", pull_payload(9, fork=False))
        found = self.adapter.item("acme/app", 9)
        self.assertEqual(found.kind, "pr")
        self.assertFalse(found.from_fork)

    def test_fork_pr_reads_from_fork(self):
        payload = item_payload(9)
        payload["pull_request"] = {}
        self.gh.add("repos/acme/app/issues/9", payload)
        self.gh.add("repos/acme/app/pulls/9", pull_payload(9, fork=True))
        self.assertTrue(self.adapter.item("acme/app", 9).from_fork)

    def test_unreadable_pull_payload_is_external(self):
        payload = item_payload(9)
        payload["pull_request"] = {}
        self.gh.add("repos/acme/app/issues/9", payload)
        self.gh.fail("repos/acme/app/pulls/9")
        found = self.adapter.item("acme/app", 9)
        self.assertTrue(found.from_fork)
        self.assertTrue(board.is_external(found.author, ["alice"], found.from_fork))

    def test_pull_payload_is_cached_per_tracker(self):
        payload = item_payload(9)
        payload["pull_request"] = {}
        self.gh.add("repos/acme/app/issues/9", payload)
        self.gh.add("repos/acme/app/pulls/9", pull_payload(9, fork=False))
        self.adapter.item("acme/app", 9)
        self.adapter.item("acme/app", 9)
        pulls = [p for p in self.gh.paths() if p == "repos/acme/app/pulls/9"]
        self.assertEqual(len(pulls), 1)

    def test_gh_failure_is_a_tracker_error(self):
        self.gh.fail("repos/acme/app/issues/7")
        with self.assertRaises(tracker.TrackerError) as ctx:
            self.adapter.item("acme/app", 7)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_missing_item_is_a_tracker_error(self):
        self.gh.fail("repos/acme/app/issues/404", RuntimeError("HTTP 404"))
        with self.assertRaises(tracker.TrackerError):
            self.adapter.item("acme/app", 404)


class MalformedTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeRun()
        self.adapter = ghtracker.GithubTracker(self.gh)

    def check_bad(self, payload):
        self.gh.add("repos/acme/app/issues/7", payload)
        with self.assertRaises(tracker.TrackerError):
            self.adapter.item("acme/app", 7)

    def test_bad_json_is_an_error(self):
        self.gh.routes["repos/acme/app/issues/7"] = "not json{"
        with self.assertRaises(tracker.TrackerError):
            self.adapter.item("acme/app", 7)

    def test_non_dict_payload_is_an_error(self):
        self.check_bad([item_payload(7)])

    def test_bad_number_is_an_error(self):
        payload = item_payload(7)
        payload["number"] = "seven"
        self.check_bad(payload)

    def test_bad_state_is_an_error(self):
        self.check_bad(item_payload(7, state="draft"))

    def test_missing_updated_at_is_an_error(self):
        payload = item_payload(7)
        del payload["updated_at"]
        self.check_bad(payload)

    def test_bad_updated_at_is_an_error(self):
        self.check_bad(item_payload(7, updated_at="yesterday"))

    def test_bad_user_is_an_error(self):
        self.check_bad(item_payload(7, user={"login": 42}))

    def test_deleted_user_reads_empty_author(self):
        self.gh.add("repos/acme/app/issues/7", item_payload(7, user=None))
        self.assertEqual(self.adapter.item("acme/app", 7).author, "")

    def test_bad_labels_are_an_error(self):
        self.check_bad(item_payload(7, labels="ready"))
        self.check_bad(item_payload(7, labels=[{"title": "ready"}]))
        self.check_bad(item_payload(7, labels=[{"name": 42}]))

    def test_null_body_reads_empty(self):
        self.gh.add("repos/acme/app/issues/7", item_payload(7, body=None))
        self.assertEqual(self.adapter.item("acme/app", 7).body, "")

    def test_bad_assignees_are_an_error(self):
        self.check_bad(item_payload(7, assignees=[{"login": 42}]))


class PriorityTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeRun()
        self.adapter = ghtracker.GithubTracker(self.gh)

    def read_priority(self, labels):
        self.gh.add("repos/acme/app/issues/7", item_payload(7, labels=labels))
        return self.adapter.item("acme/app", 7).priority

    def test_highest_priority_label_wins(self):
        self.assertEqual(
            self.read_priority([{"name": "priority:low"}, {"name": "priority:high"}]),
            "high",
        )

    def test_urgent_outranks_high(self):
        self.assertEqual(
            self.read_priority([{"name": "priority:high"}, {"name": "priority:urgent"}]),
            "urgent",
        )

    def test_unknown_suffix_is_ignored(self):
        self.assertEqual(self.read_priority([{"name": "priority:someday"}]), "")

    def test_no_priority_label_reads_empty(self):
        self.assertEqual(self.read_priority([{"name": "ready"}]), "")


class PaginationTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeRun()
        self.adapter = ghtracker.GithubTracker(self.gh)
        self.old_per_page = ghtracker.PER_PAGE
        ghtracker.PER_PAGE = 2
        self.addCleanup(setattr, ghtracker, "PER_PAGE", self.old_per_page)

    def pages(self, repo="acme/app", *lists, since=None):
        base = f"repos/{repo}/issues?state=all&sort=updated&direction=asc&per_page=2"
        if since is not None:
            base += f"&since={since}"
        for index, entries in enumerate(lists, 1):
            path = base if index == 1 else f"{base}&page={index}"
            self.gh.add(path, entries)

    def test_changes_walks_pages_until_short(self):
        self.pages(
            "acme/app",
            [item_payload(1), item_payload(2)],
            [item_payload(3, updated_at=ISO1)],
        )
        found, cursor = self.adapter.changes("acme/app", None)
        self.assertEqual([item.number for item in found], [1, 2, 3])
        self.assertEqual(cursor, ISO1)

    def test_changes_sends_since(self):
        self.pages("acme/app", [], since=ISO0)
        found, cursor = self.adapter.changes("acme/app", ISO0)
        self.assertEqual(found, [])
        self.assertEqual(cursor, ISO0)

    def test_changes_keeps_newest_cursor(self):
        self.pages("acme/app", [item_payload(1, updated_at=ISO1)])
        _, cursor = self.adapter.changes("acme/app", None)
        self.assertEqual(cursor, ISO1)

    def test_page_error_is_an_error_not_partial(self):
        self.pages("acme/app", [item_payload(1), item_payload(2)])
        self.gh.fail("repos/acme/app/issues?state=all&sort=updated&direction=asc&per_page=2&page=2")
        with self.assertRaises(tracker.TrackerError):
            self.adapter.changes("acme/app", None)

    def test_items_walks_every_page(self):
        self.pages(
            "acme/app",
            [item_payload(1), item_payload(2)],
            [item_payload(3)],
        )
        self.assertEqual([item.number for item in self.adapter.items("acme/app")], [1, 2, 3])


class DecisionRecordTest(unittest.TestCase):
    """The fake Tracker records both decision-seam calls."""

    def test_comment_and_label_are_recorded(self):
        fake = FakeTracker()
        fake.comment("acme/app", 7, "Which shape should this take?")
        fake.add_label("acme/app", 7, "needs-decision")
        self.assertIn(
            ("comment", "acme/app", 7, "Which shape should this take?"),
            fake.calls,
        )
        self.assertIn(("add_label", "acme/app", 7, "needs-decision"), fake.calls)

    def test_scripted_comment_error_raises(self):
        fake = FakeTracker()
        fake.comment_errors[("acme/app", 7)] = tracker.TrackerError("down")
        with self.assertRaises(tracker.TrackerError):
            fake.comment("acme/app", 7, "body")

    def test_scripted_label_error_raises(self):
        fake = FakeTracker()
        fake.label_errors[("acme/app", 7)] = tracker.TrackerError("down")
        with self.assertRaises(tracker.TrackerError):
            fake.add_label("acme/app", 7, "needs-decision")


class DecisionAdapterTest(unittest.TestCase):
    """The GitHub adapter posts one comment and one label through `gh`."""

    def setUp(self):
        self.calls = []
        self.error = None

        def run(args):
            self.calls.append(list(args))
            if self.error is not None:
                raise self.error
            return ""

        self.adapter = ghtracker.GithubTracker(run)

    def test_comment_posts_one_comment(self):
        self.adapter.comment("acme/app", 7, "Which shape should this take?")
        self.assertEqual(len(self.calls), 1)
        posted = self.calls[0]
        self.assertIn("POST", posted)
        self.assertIn("repos/acme/app/issues/7/comments", posted)
        self.assertIn("body=Which shape should this take?", posted)

    def test_add_label_posts_needs_decision(self):
        self.adapter.add_label("acme/app", 7, "needs-decision")
        self.assertEqual(len(self.calls), 1)
        posted = self.calls[0]
        self.assertIn("POST", posted)
        self.assertIn("repos/acme/app/issues/7/labels", posted)
        self.assertIn("labels[]=needs-decision", posted)

    def test_add_label_refuses_any_other_name(self):
        with self.assertRaises(ValueError):
            self.adapter.add_label("acme/app", 7, "ready")
        self.assertEqual(self.calls, [])

    def test_comment_failure_is_a_tracker_error(self):
        self.error = RuntimeError("connection refused")
        with self.assertRaises(tracker.TrackerError) as ctx:
            self.adapter.comment("acme/app", 7, "body")
        self.assertNotIsInstance(ctx.exception, tracker.TrackerRateLimited)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_label_rate_limit_maps_to_tracker_rate_limited(self):
        self.error = ghmod.GhError("unavailable", "API rate limit exceeded, retry after 62s")
        with self.assertRaises(tracker.TrackerRateLimited) as ctx:
            self.adapter.add_label("acme/app", 7, "needs-decision")
        self.assertEqual(ctx.exception.retry_after, 62.0)


if __name__ == "__main__":
    unittest.main()
