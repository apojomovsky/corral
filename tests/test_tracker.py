"""The tracker interface and its GitHub adapter. Fakes only; no network."""

import calendar
import json
import unittest
from typing import Any

from korapy import board, hold, settings, tracker
from korapy.github import gh as ghmod
from korapy.github import tracker as ghtracker

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

    def __init__(self, items=(), *, labels=(), refused=(), ensure_error=None, labels_error=None):
        self.by_key = {(item.repo, item.number): item for item in items}
        self.changes_error = None
        self.item_errors = {}
        self.items_break_after = None
        self.write_errors = {}
        self.comment_errors = {}
        self.label_errors = {}
        self.repo_labels = {repo: set(names) for repo, names in labels}
        self.refused = set(refused)
        self.ensure_error = ensure_error
        self.labels_error = labels_error
        self.linked = {}
        self.linked_errors = {}
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

    def linked_pulls(self, repo, number):
        """Programmed open pull requests linked to the ticket."""
        self.calls.append(("linked_pulls", repo, number))
        if (repo, number) in self.linked_errors:
            raise tracker.TrackerError(self.linked_errors[(repo, number)])
        return list(self.linked.get((repo, number), ()))

    def write(self, repo, number, field, value):
        """Record one korapy-owned field write; programmed errors raise."""
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

    def missing_labels(self, repo):
        """Names of the bootstrap labels absent on ``repo``; a programmed error raises."""
        self.calls.append(("missing_labels", repo))
        if self.labels_error is not None:
            raise self.labels_error
        have = self.repo_labels.get(repo, set())
        return tuple(label.name for label in tracker.BOOTSTRAP_LABELS if label.name not in have)

    def ensure_labels(self, repo):
        """Idempotent in-memory bootstrap: create, keep, or refuse."""
        self.calls.append(("ensure_labels", repo))
        if self.ensure_error is not None:
            raise self.ensure_error
        have = self.repo_labels.setdefault(repo, set())
        created, kept, refused = [], [], []
        for wanted in tracker.BOOTSTRAP_LABELS:
            if wanted.name in have:
                kept.append(wanted.name)
            elif wanted.name in self.refused:
                refused.append(wanted.name)
            else:
                have.add(wanted.name)
                created.append(wanted.name)
        return tracker.LabelReport(tuple(created), tuple(kept), tuple(refused))


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


class SharedVocabularyTest(unittest.TestCase):
    """The adapter ranks and flags by the neutral protocol's words, not its own copies."""

    def test_the_adapter_derives_priority_from_the_shared_order(self):
        for higher, lower in zip(tracker.PRIORITIES, tracker.PRIORITIES[1:]):
            with self.subTest(higher=higher, lower=lower):
                self.assertEqual(
                    ghtracker.priority_of([f"priority:{lower}", f"priority:{higher}"]),
                    higher,
                )

    def test_the_decision_label_is_the_shared_one(self):
        self.assertIs(ghtracker.NEEDS_DECISION, tracker.NEEDS_DECISION)


class LabelApi:
    """A `gh` double for the labels endpoints: paged lists, recorded posts.

    The list endpoint pages like the adapter requests it
    (`?per_page=N&page=M`); an unpaged list call is rejected so a test
    that misses pagination fails instead of passing on one short page.
    `conflict` names labels whose create answers 422 already-exists
    (the concurrent-setup shape) and then reads back, like a label the
    other setup just created; `fail_single` names labels whose
    single-label read fails, for the fail-closed re-read path.
    """

    PATH = "repos/acme/app/labels"

    def __init__(
        self,
        have=(),
        refuse=(),
        fail_list=None,
        fail_names=(),
        fail_single=(),
        conflict=(),
        list_payload: object = None,
    ):
        self.have = dict(have)
        self.refuse = set(refuse)
        self.fail_list = fail_list
        self.fail_names = set(fail_names)
        self.fail_single = set(fail_single)
        self.conflict = set(conflict)
        self.list_payload = list_payload
        self.calls = []
        self.posted = []

    def _page(self, query):
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        per_page = int(params.get("per_page", "100"))
        page = int(params.get("page", "1"))
        names = list(self.have)
        start = (page - 1) * per_page
        return [
            {"name": name, "color": color, "description": desc}
            for name, (color, desc) in [(n, self.have[n]) for n in names[start : start + per_page]]
        ]

    def __call__(self, args):
        self.calls.append(list(args))
        if list(args[:2]) == ["api", self.PATH]:
            raise AssertionError(f"unpaged labels list {args!r}: request per_page and pages")
        if len(args) == 2 and args[0] == "api" and args[1].startswith(self.PATH + "?"):
            if self.fail_list is not None:
                raise self.fail_list
            if self.list_payload is not None:
                return json.dumps(self.list_payload)
            _, _, query = args[1].partition("?")
            return json.dumps(self._page(query))
        if (
            len(args) == 2
            and args[0] == "api"
            and args[1].startswith(self.PATH + "/")
            and "?" not in args[1]
        ):
            name = args[1][len(self.PATH) + 1 :]
            if name in self.fail_single:
                raise RuntimeError("gh: Not Found (HTTP 404)")
            try:
                color, desc = self.have[name]
            except KeyError:
                raise RuntimeError("gh: Not Found (HTTP 404)") from None
            return json.dumps({"name": name, "color": color, "description": desc})
        if list(args[:4]) == ["api", "--method", "POST", self.PATH]:
            fields = {}
            for flag, value in zip(args[4::2], args[5::2]):
                self.assert_flag(flag)
                key, _, val = value.partition("=")
                fields[key] = val
            name = fields.get("name")
            if name in self.fail_names:
                raise RuntimeError("gh: connection reset")
            if name in self.refuse:
                raise RuntimeError("gh: Resource not accessible by token (HTTP 403)")
            if name in self.conflict:
                self.have[name] = (fields.get("color"), fields.get("description"))
                raise RuntimeError(
                    'gh: Validation Failed: {"resource": "Label",'
                    ' "code": "already_exists"} (HTTP 422)'
                )
            self.posted.append(fields)
            self.have[name] = (fields.get("color"), fields.get("description"))
            return json.dumps({"name": name})
        raise AssertionError(f"unexpected gh call {args!r}")

    @staticmethod
    def assert_flag(flag):
        assert flag == "-f", f"unexpected flag {flag!r}"


class BootstrapLabelsTest(unittest.TestCase):
    """The bootstrap set is exactly the labels the rest of korapy reads."""

    def test_names_follow_the_rank_order(self):
        self.assertEqual(
            [label.name for label in tracker.BOOTSTRAP_LABELS],
            [
                "ready",
                "needs-triage",
                "needs-decision",
                "hold",
                "priority:urgent",
                "priority:high",
                "priority:normal",
                "priority:low",
                "priority:backlog",
            ],
        )

    def test_names_agree_with_the_readers(self):
        names = [label.name for label in tracker.BOOTSTRAP_LABELS]
        self.assertIn(settings.REGISTRY["board.ready"].default, names)
        self.assertIn(hold.HOLD_LABEL, names)
        self.assertIn(tracker.NEEDS_DECISION, names)
        for priority in tracker.PRIORITIES:
            self.assertIn(f"{tracker.PRIORITY_PREFIX}{priority}", names)
        self.assertEqual(len(names), len(set(names)))

    def test_the_adapter_colors_cover_the_set(self):
        self.assertEqual(
            set(ghtracker._BOOTSTRAP_COLORS), {label.name for label in tracker.BOOTSTRAP_LABELS}
        )


class RenderEnsureTest(unittest.TestCase):
    def test_created_kept_and_refused_render_on_their_own_lines(self):
        report = tracker.LabelReport(
            created=("ready",), kept=("hold", "needs-triage"), refused=("priority:high",)
        )
        self.assertEqual(
            tracker.render_ensure("acme/app", report),
            "created label ready on acme/app\n"
            "kept 2 labels on acme/app\n"
            "refused label priority:high on acme/app: ask an owner to create it,"
            " or move the board to the ops side\n",
        )

    def test_an_empty_report_renders_nothing(self):
        report = tracker.LabelReport(created=(), kept=(), refused=())
        self.assertEqual(tracker.render_ensure("acme/app", report), "")


class FakeEnsureTest(unittest.TestCase):
    def test_a_fresh_repo_creates_everything(self):
        fake = FakeTracker()
        report = fake.ensure_labels("acme/app")
        self.assertEqual(report.created, tuple(label.name for label in tracker.BOOTSTRAP_LABELS))
        self.assertEqual(report.kept, ())
        self.assertEqual(report.refused, ())
        self.assertEqual(fake.calls, [("ensure_labels", "acme/app")])

    def test_a_second_run_keeps_everything(self):
        fake = FakeTracker()
        first = fake.ensure_labels("acme/app")
        second = fake.ensure_labels("acme/app")
        self.assertEqual(second.created, ())
        self.assertEqual(second.kept, first.created)
        self.assertEqual(second.refused, ())

    def test_refused_labels_are_reported_and_the_rest_is_created(self):
        fake = FakeTracker(refused={"hold"})
        report = fake.ensure_labels("acme/app")
        self.assertEqual(report.refused, ("hold",))
        self.assertNotIn("hold", report.created)
        self.assertIn("ready", report.created)

    def test_a_programmed_failure_raises(self):
        fake = FakeTracker(ensure_error=tracker.TrackerError("down"))
        with self.assertRaises(tracker.TrackerError):
            fake.ensure_labels("acme/app")


class EnsureLabelsTest(unittest.TestCase):
    def setUp(self):
        self.api = LabelApi()
        self.adapter = ghtracker.GithubTracker(self.api)

    def names(self):
        return [label.name for label in tracker.BOOTSTRAP_LABELS]

    def test_an_empty_repo_creates_everything(self):
        report = self.adapter.ensure_labels("acme/app")
        self.assertEqual(report.created, tuple(self.names()))
        self.assertEqual(report.kept, ())
        self.assertEqual(report.refused, ())
        self.assertEqual([posted["name"] for posted in self.api.posted], self.names())

    def test_the_create_names_name_color_and_description(self):
        self.adapter.ensure_labels("acme/app")
        posted = self.api.posted[0]
        self.assertEqual(posted["name"], "ready")
        self.assertEqual(posted["color"], ghtracker._BOOTSTRAP_COLORS["ready"])
        self.assertEqual(
            posted["description"],
            next(label.description for label in tracker.BOOTSTRAP_LABELS if label.name == "ready"),
        )

    def test_existing_labels_are_kept_byte_identical(self):
        self.api = LabelApi(have={"ready": ("ff0000", "a human-edited description")})
        adapter = ghtracker.GithubTracker(self.api)
        report = adapter.ensure_labels("acme/app")
        self.assertIn("ready", report.kept)
        self.assertEqual(
            [posted["name"] for posted in self.api.posted],
            [name for name in self.names() if name != "ready"],
        )

    def test_a_rerun_posts_nothing(self):
        self.adapter.ensure_labels("acme/app")
        self.api.posted.clear()
        report = self.adapter.ensure_labels("acme/app")
        self.assertEqual(report.created, ())
        self.assertEqual(report.kept, tuple(self.names()))
        self.assertEqual(self.api.posted, [])

    def test_a_refused_label_is_reported_and_the_pass_continues(self):
        self.api = LabelApi(refuse={"hold"})
        report = ghtracker.GithubTracker(self.api).ensure_labels("acme/app")
        self.assertEqual(report.refused, ("hold",))
        self.assertNotIn("hold", report.created)
        self.assertIn("ready", report.created)

    def test_a_failing_create_is_a_tracker_error(self):
        self.api = LabelApi(fail_names={"ready"})
        with self.assertRaises(tracker.TrackerError):
            ghtracker.GithubTracker(self.api).ensure_labels("acme/app")

    def test_a_rate_limited_create_stays_rate_limited(self):
        self.api = LabelApi()
        original = self.api

        def limited(args):
            if list(args[:4]) == ["api", "--method", "POST", LabelApi.PATH]:
                raise RuntimeError("gh: API rate limit exceeded (HTTP 403)")
            return original(args)

        with self.assertRaises(tracker.TrackerRateLimited):
            ghtracker.GithubTracker(limited).ensure_labels("acme/app")

    def test_a_failing_list_is_a_tracker_error(self):
        self.api = LabelApi(fail_list=RuntimeError("gh failed"))
        with self.assertRaises(tracker.TrackerError):
            ghtracker.GithubTracker(self.api).ensure_labels("acme/app")

    def test_a_malformed_list_is_a_tracker_error(self):
        for payload in ({"name": "ready"}, [{"title": "ready"}]):
            with self.subTest(payload=payload):
                api = LabelApi(list_payload=payload)
                with self.assertRaises(tracker.TrackerError):
                    ghtracker.GithubTracker(api).ensure_labels("acme/app")

    def test_a_wanted_label_on_a_later_page_is_kept_not_created(self):
        old_per_page = ghtracker.PER_PAGE
        ghtracker.PER_PAGE = 2
        self.addCleanup(setattr, ghtracker, "PER_PAGE", old_per_page)
        extra = {f"extra-{index:02d}": ("cccccc", "unrelated") for index in range(36)}
        have = dict(extra)
        for name in self.names():
            have[name] = ("0e8a16", "board label")
        api = LabelApi(have=have)
        report = ghtracker.GithubTracker(api).ensure_labels("acme/app")
        self.assertEqual(report.created, ())
        self.assertEqual(set(report.kept), set(self.names()))
        self.assertEqual(api.posted, [])
        self.assertTrue(
            any(call == ["api", f"{LabelApi.PATH}?per_page=2&page=2"] for call in api.calls),
            f"no second page requested in {api.calls!r}",
        )

    def test_a_422_on_create_is_kept_after_a_reread(self):
        api = LabelApi(conflict={"ready"})
        report = ghtracker.GithubTracker(api).ensure_labels("acme/app")
        self.assertIn("ready", report.kept)
        self.assertNotIn("ready", report.created)
        self.assertEqual(report.refused, ())
        self.assertEqual(
            [posted["name"] for posted in api.posted],
            [name for name in self.names() if name != "ready"],
        )
        self.assertIn(["api", f"{LabelApi.PATH}/ready"], api.calls)

    def test_a_422_followed_by_a_missing_label_fails_closed(self):
        api = LabelApi(conflict={"ready"}, fail_single={"ready"})
        with self.assertRaises(tracker.TrackerError):
            ghtracker.GithubTracker(api).ensure_labels("acme/app")


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

    def test_feed_marks_pull_request_entries_as_pr(self):
        pr = item_payload(9)
        pr["pull_request"] = {"url": "https://example.com/pulls/9"}
        self.pages("acme/app", [item_payload(1), pr], [])
        self.gh.add("repos/acme/app/pulls/9", pull_payload(9, fork=False))
        found, _ = self.adapter.changes("acme/app", None)
        self.assertEqual([(item.number, item.kind) for item in found], [(1, "issue"), (9, "pr")])


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

    def test_comment_rate_limit_maps_to_tracker_rate_limited(self):
        self.error = ghmod.GhError("unavailable", "API rate limit exceeded, retry after 62s")
        with self.assertRaises(tracker.TrackerRateLimited) as ctx:
            self.adapter.comment("acme/app", 7, "body")
        self.assertEqual(ctx.exception.retry_after, 62.0)

    def test_label_failure_is_a_tracker_error(self):
        self.error = RuntimeError("connection refused")
        with self.assertRaises(tracker.TrackerError) as ctx:
            self.adapter.add_label("acme/app", 7, "needs-decision")
        self.assertNotIsInstance(ctx.exception, tracker.TrackerRateLimited)
        self.assertEqual(ctx.exception.exit_code, 3)


def pr_payload(number, body, fork=False, **over):
    """One pulls-endpoint payload; override any field."""
    payload = item_payload(number, body=body, **over)
    payload.pop("pull_request", None)
    payload["head"] = {"ref": f"topic-{number}", "repo": {"fork": fork}}
    return payload


class LinkedPullsTest(unittest.TestCase):
    """The open pull requests linked to a ticket. Fakes only; no network."""

    def setUp(self):
        self.gh = FakeRun()
        self.adapter = ghtracker.GithubTracker(self.gh)

    def listing(self, pulls):
        self.gh.add(
            "repos/acme/app/pulls?state=open&sort=updated&direction=asc&per_page=100",
            pulls,
        )
        for entry in pulls:
            self.gh.add(
                f"repos/acme/app/pulls/{entry['number']}",
                pull_payload(entry["number"], fork=False),
            )

    def test_an_open_pr_closing_the_ticket_is_linked(self):
        self.listing([pr_payload(9, "Fixes things.\n\nCloses #7")])
        found = self.adapter.linked_pulls("acme/app", 7)
        self.assertEqual(
            [(item.number, item.kind, item.state) for item in found], [(9, "pr", "open")]
        )

    def test_unrelated_and_other_ticket_prs_are_not_linked(self):
        self.listing(
            [
                pr_payload(9, "No ticket here"),
                pr_payload(10, "Closes #8"),
            ]
        )
        self.assertEqual(self.adapter.linked_pulls("acme/app", 7), [])

    def test_a_refs_pr_links_too(self):
        self.listing([pr_payload(9, "Partial win.\n\nRefs #7")])
        self.assertEqual([item.number for item in self.adapter.linked_pulls("acme/app", 7)], [9])

    def test_a_close_keyword_in_the_title_links(self):
        self.listing([pr_payload(9, "unrelated body", title="Fixes #7 on retry")])
        self.assertEqual([item.number for item in self.adapter.linked_pulls("acme/app", 7)], [9])

    def test_a_bare_title_mention_links_nothing(self):
        self.listing([pr_payload(9, "unrelated body", title="Follow-up for #7")])
        self.assertEqual(self.adapter.linked_pulls("acme/app", 7), [])

    def test_a_close_keyword_in_a_fence_links_nothing(self):
        self.listing([pr_payload(9, "```\nCloses #7\n```")])
        self.assertEqual(self.adapter.linked_pulls("acme/app", 7), [])

    def test_a_cross_repo_close_does_not_link_this_repo(self):
        self.listing([pr_payload(9, "Closes other/repo#7")])
        self.assertEqual(self.adapter.linked_pulls("acme/app", 7), [])

    def test_a_qualified_close_of_this_repo_links(self):
        self.listing([pr_payload(9, "Closes acme/app#7")])
        self.assertEqual([item.number for item in self.adapter.linked_pulls("acme/app", 7)], [9])

    def test_a_fork_pr_still_links(self):
        self.gh.add(
            "repos/acme/app/pulls?state=open&sort=updated&direction=asc&per_page=100",
            [pr_payload(9, "Closes #7")],
        )
        self.gh.add("repos/acme/app/pulls/9", pull_payload(9, fork=True))
        found = self.adapter.linked_pulls("acme/app", 7)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].from_fork)

    def test_a_listing_failure_is_a_tracker_error(self):
        self.gh.fail("repos/acme/app/pulls?state=open&sort=updated&direction=asc&per_page=100")
        with self.assertRaises(tracker.TrackerError) as ctx:
            self.adapter.linked_pulls("acme/app", 7)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_pages_combine(self):
        first = [pr_payload(n, f"Closes #{700 + n}") for n in range(100, 200)]
        self.gh.routes[
            "repos/acme/app/pulls?state=open&sort=updated&direction=asc&per_page=100"
        ] = json.dumps(first)
        self.gh.routes[
            "repos/acme/app/pulls?state=open&sort=updated&direction=asc&per_page=100&page=2"
        ] = json.dumps([pr_payload(9, "Closes #7")])
        for entry in first + [pr_payload(9, "Closes #7")]:
            self.gh.add(
                f"repos/acme/app/pulls/{entry['number']}",
                pull_payload(entry["number"], fork=False),
            )
        found = self.adapter.linked_pulls("acme/app", 7)
        self.assertEqual([item.number for item in found], [9])


if __name__ == "__main__":
    unittest.main()
