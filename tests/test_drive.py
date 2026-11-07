"""The drive pass: lock, read-only gating, the candidate table. Fakes only.

The lock is a real `flock`, but only over files inside temporary
directories; the store is a temporary board store; policies are stub
modules written to a temporary directory; every host read (projects,
live state, limits, quotas, ledger, labels) is injected plain data.
No tracker, no network, no subprocess, no real state dir.
"""

import argparse
import errno
import fcntl
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from korapy import admission, board, cli, drive, drivehub, launch, quota_ledger, settings
from korapy import syncli
from korapy import tracker
from korapy import projects as projects_mod
from korapy.github import tracker as ghtracker
from tests.test_tracker import FakeRun, FakeTracker, reading


def stored(path, items):
    """A board store holding the given `(repo, number, state)` rows."""
    store = board.open_store(path)
    for repo, number, state in items:
        store.upsert_item(
            board.Item(
                repo=repo,
                number=number,
                kind="issue",
                title=f"Title {number}",
                body="Body",
                state=state,
                author="operator",
                labels=[],
                priority="",
                assignees=[],
                url=f"https://example.com/{repo}/{number}",
                tracker_updated_at=1000.0,
                extra={},
            ),
            operators=["operator"],
            now=1000.0,
        )
    return store


def policy_source(wants):
    """A stub policy module naming the given `(repo, number, kind)` wants."""
    lines = [
        "def eligible(rows, *, now):",
        "    from korapy.eligibility import StartRequest",
        "    return [",
    ]
    for repo, number, kind in wants:
        lines.append(f"        StartRequest({repo!r}, {number!r}, {kind!r}),")
    lines.append("    ]")
    return "\n".join(lines) + "\n"


def project_named(name, entry=None, root=None):
    """A registered project, optionally naming a policy entry."""
    return projects_mod.Project(
        name=name,
        unit_prefix=name + "-agent-",
        root=root or os.path.join(tempfile.gettempdir(), "korapy-drive-ops", name),
        eligibility=entry,
    )


def driven_for(found, store):
    """`{project name: fixture repos}` so an older test drives its rows.

    `found` is `[(name, wants)]` or `[Project]` (`Project` is a
    named tuple, so a plain-tuple check alone cannot tell them
    apart); the repos are the policy wants plus the store rows, so
    every fixture row and every request stays driven exactly as
    before scoping existed. Tests that pin scoping pass `driven`
    explicitly instead.
    """
    names = [entry[0] if type(entry) is tuple else entry.name for entry in found]
    repos = set()
    for entry in found:
        if type(entry) is tuple:
            repos.update(repo for repo, _number, _kind in entry[1])
    try:
        repos.update(item.repo for item in store.items())
    except (board.StoreError, sqlite3.Error):
        pass
    return {name: frozenset(repos) for name in names}


def hermetic_pass(store, lock_path, out, err, plan=True, found=(), policy_dir=None, **fields):
    """`run_pass` with every host read injected: no outside world."""
    fallback_driven = driven_for(found, store)
    if policy_dir is not None and found:
        entries = []
        for name, wants in found:
            path = os.path.join(policy_dir, name + ".py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(policy_source(wants))
            entries.append(project_named(name, path + ":eligible"))
        found = entries
    values = {
        "plan": plan,
        "store": store,
        "lock_path": lock_path,
        "out": out,
        "err": err,
        "now": 2000.0,
        "projects_fn": lambda: list(found),
        "live": frozenset(),
        "limits": admission.Limits(total=4),
        "quotas": {},
        "ledger": {},
        "read_labels": lambda kind, repo, number: [],
        "hold_label": "hold",
        "present_agents": frozenset(),
        "live_units": frozenset(),
        "gone_s": 3600.0,
    }
    values.update(fields)
    values.setdefault("driven", fallback_driven)
    return drive.run_pass(**values)


class RenderTest(unittest.TestCase):
    def test_rows_print_with_verdict_and_reason_plus_summary(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="start",
                    reason="fresh",
                ),
                drive.Candidate(
                    project="red",
                    repo="app",
                    number=9,
                    kind="start",
                    verdict="skip",
                    reason="held",
                ),
            ],
            label="plan",
        )
        self.assertIn("blue app#7: start (fresh)", text)
        self.assertIn("red app#9: skip (held)", text)
        self.assertTrue(text.splitlines()[-1].startswith("plan:"))
        self.assertIn("2 candidate(s)", text)
        self.assertIn("would start 1", text)

    def test_rows_without_a_project_print_bare(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="skip",
                    reason="withdrawn by this pass",
                ),
            ],
            label="plan",
        )
        self.assertIn("app#7: skip (withdrawn by this pass)", text)

    def test_summary_counts_each_proceeding_kind_separately(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=1,
                    kind="start",
                    verdict="start",
                    reason="fresh",
                ),
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=2,
                    kind="resume",
                    verdict="resume",
                    reason="back",
                ),
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=3,
                    kind="review",
                    verdict="review",
                    reason="slot",
                ),
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=4,
                    kind="fix",
                    verdict="fix",
                    reason="round",
                ),
                drive.Candidate(
                    project="red",
                    repo="app",
                    number=5,
                    kind="resume",
                    verdict="skip",
                    reason="held",
                ),
            ],
            label="plan",
        )
        self.assertIn("5 candidate(s)", text)
        self.assertIn("would start 1", text)
        self.assertIn("would resume 1", text)
        self.assertIn("would review 1", text)
        self.assertIn("would fix 1", text)

    def test_summary_names_the_pulls_call_count(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="start",
                    reason="fresh",
                ),
            ],
            label="pass",
            plan=False,
            pulls_calls=3,
        )
        self.assertIn("pass: 1 candidate(s)", text)
        self.assertIn("started 1", text)
        self.assertIn("pulls calls 3", text)

    def test_summary_counts_zero_pulls_calls_by_default(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="start",
                    reason="fresh",
                ),
            ],
            label="plan",
        )
        self.assertIn("pulls calls 0", text)

    def test_summary_flags_a_limited_pulls_lookup(self):
        text = drive.render(
            [
                drive.Candidate(
                    project="blue",
                    repo="app",
                    number=7,
                    kind="start",
                    verdict="skip",
                    reason="app#7: linked pulls unreadable, start refused (pulls lookup limited)",
                ),
            ],
            label="pass",
            plan=False,
            pulls_calls=1,
            pulls_limited=True,
        )
        self.assertIn("pulls calls 1", text)
        self.assertIn("pulls lookup limited", text)


class PullsLookupPassTest(unittest.TestCase):
    """The batched pulls lookup over a full pass. Fakes only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-pulls-pass-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_pass(self, store, plan=False, found=(), **fields):
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store, self.lock, out, err, plan=plan, found=found, policy_dir=self.policies, **fields
        )
        return code, out.getvalue(), err.getvalue()

    def test_a_store_with_hundreds_of_rows_costs_one_pulls_listing(self):
        numbers = list(range(1, 301))
        store = stored(self.db, [("app", number, "open") for number in numbers])
        self.addCleanup(store.close)
        gh = FakeRun()
        gh.add(
            "repos/app/issues?state=all&sort=updated&direction=asc&per_page=100",
            [],
        )
        gh.add(
            "repos/app/pulls?state=open&sort=updated&direction=asc&per_page=100",
            [],
        )
        adapter = ghtracker.GithubTracker(gh)
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", number, "start") for number in numbers])],
            tracker_factory=lambda repo: adapter,
            index_for=drive.pulls_index_for(lambda repo: adapter),
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        pulls_calls = [call for call in gh.calls if call[1].startswith("repos/app/pulls?")]
        self.assertEqual(len(pulls_calls), 1)
        self.assertIn("pulls calls 1", out)

    def test_a_pass_with_no_fetchable_candidate_lists_nothing(self):
        store = stored(self.db, [("app", 7, "closed")])
        self.addCleanup(store.close)
        fake = FakeTracker()
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertEqual([call for call in fake.calls if call[0] == "pulls_index"], [])
        self.assertIn("pulls calls 0", out)

    def test_a_capped_pass_reports_limited_and_refuses_starts(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker()
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            pulls_max_calls=0,
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("pulls lookup limited", out)
        self.assertIn("linked pulls unreadable, start refused (pulls lookup limited", out)
        self.assertIn("pulls lookup limited", out.splitlines()[-1])

    def test_fix_on_a_live_claimed_row_with_unreadable_pulls_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        fake = FakeTracker()
        fake.index_errors["app"] = "tracker down"
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "fix")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            live=frozenset({("blue", "app", 7)}),
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("linked pulls unreadable, fix refused", out)
        self.assertIn("tracker down", out)
        self.assertEqual(len([call for call in fake.calls if call[0] == "pulls_index"]), 1)

    def test_resume_on_a_live_claimed_row_with_unreadable_pulls_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        fake = FakeTracker()
        fake.index_errors["app"] = "tracker down"
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "resume")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            live=frozenset({("blue", "app", 7)}),
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("linked pulls unreadable, resume refused", out)
        self.assertIn("tracker down", out)
        self.assertEqual(len([call for call in fake.calls if call[0] == "pulls_index"]), 1)

    def test_fix_on_a_live_claimed_row_with_a_held_pull_is_hold_skipped(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        fake = FakeTracker()
        fake.index["app"] = {7: [reading("app", 12, kind="pr", state="open")]}
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "fix")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            live=frozenset({("blue", "app", 7)}),
            read_labels=lambda kind, repo, number: ["hold"] if kind == "pr" else [],
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("app#12", out)
        self.assertIn("pulls calls 1", out)
        self.assertEqual(len([call for call in fake.calls if call[0] == "pulls_index"]), 1)

    def test_takeover_start_on_a_live_claimed_row_with_unreadable_pulls_is_refused(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        fake = FakeTracker()
        fake.index_errors["app"] = "tracker down"
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            live=frozenset({("blue", "app", 7)}),
            finished_for={("app", 7): ("drive-host-1-x", "worker")},
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("linked pulls unreadable, review refused", out)
        self.assertIn("tracker down", out)
        self.assertEqual(len([call for call in fake.calls if call[0] == "pulls_index"]), 1)

    def test_takeover_start_on_a_live_claimed_row_with_a_held_pull_is_hold_skipped(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        fake = FakeTracker()
        fake.index["app"] = {7: [reading("app", 12, kind="pr", state="open")]}
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            live=frozenset({("blue", "app", 7)}),
            finished_for={("app", 7): ("drive-host-1-x", "worker")},
            read_labels=lambda kind, repo, number: ["hold"] if kind == "pr" else [],
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("app#12", out)
        self.assertIn("pulls calls 1", out)
        self.assertEqual(len([call for call in fake.calls if call[0] == "pulls_index"]), 1)

    def test_takeover_starts_in_one_repo_share_a_single_listing(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1000.0)
        store.claim("app", 8, "drive-host-1-y", "host-1", now=1000.0)
        fake = FakeTracker()
        fake.index_errors["app"] = "tracker down"
        code, out, _ = self.run_pass(
            store,
            plan=False,
            found=[("blue", [("app", 7, "start"), ("app", 8, "start")])],
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
            live=frozenset({("blue", "app", 7), ("blue", "app", 8)}),
            finished_for={
                ("app", 7): ("drive-host-1-x", "worker"),
                ("app", 8): ("drive-host-1-y", "worker"),
            },
            stale_after=10**12,
            launcher=None,
        )
        self.assertEqual(code, 0)
        self.assertIn("app#7: linked pulls unreadable, review refused", out)
        self.assertIn("app#8: linked pulls unreadable, review refused", out)
        self.assertEqual(len([call for call in fake.calls if call[0] == "pulls_index"]), 1)

    def test_a_bad_pulls_setting_is_recorded_and_the_pass_caps_at_the_default(self):
        from korapy import eligibility

        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        items = store.items()
        project = project_named("blue", entry="dummy:eligible")
        outcome = eligibility.Outcome([eligibility.StartRequest("app", 7, "start")], None)
        fake = FakeTracker()
        out = io.StringIO()
        with (
            mock.patch.object(eligibility, "for_project", return_value=outcome),
            mock.patch.object(
                drive.settings,
                "load",
                side_effect=settings.SettingsError("bad pulls budget"),
            ),
        ):
            gated, problems, _, pulls_calls, pulls_limited = drive._gate_all(
                items,
                [project],
                out,
                now=2000.0,
                live_set=frozenset(),
                limits=admission.Limits(total=4),
                quota_for={"blue": drive.QuotaSource(chains={}, stale_after=10**12)},
                pool=None,
                ledger_data={},
                read_labels=lambda kind, repo, number: [],
                hold_label="hold",
                fix_threshold="high",
                index_for=drive.pulls_index_for(lambda repo: fake),
                pulls_max_calls=None,
                driven={"blue": frozenset({"app"})},
            )
        self.assertEqual(len(problems), 1)
        self.assertIn("bad pulls budget", problems[0])
        self.assertEqual(
            [call for call in fake.calls if call[0] == "pulls_index"],
            [("pulls_index", "app", drive.DEFAULT_PULLS_MAX_CALLS)],
        )
        self.assertEqual(pulls_calls, 1)
        self.assertFalse(pulls_limited)
        self.assertEqual(len(gated), 1)


class PullsBudgetValidationTest(unittest.TestCase):
    """`drive.pulls_max_calls` takes only a positive integer."""

    def test_the_validator_refuses_anything_but_a_positive_integer(self):
        for value in (0, -3, True, "x"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    drive._positive_calls(value)

    def test_the_validator_passes_a_positive_integer_through(self):
        self.assertEqual(drive._positive_calls(1), 1)
        self.assertEqual(drive._positive_calls(20), 20)

    def test_the_resolver_names_the_key(self):
        for value in (0, -3, True, "x"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(settings.SettingsError, "drive.pulls_max_calls"):
                    settings.resolve(project={"drive": {"pulls_max_calls": value}})

    def test_a_bad_file_value_names_the_file_and_key(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-pulls-budget-"))
        config = os.path.join(tmp, "config")
        os.makedirs(config)
        root = os.path.join(tmp, "ops")
        os.makedirs(root)
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write("[drive]\npulls_max_calls = 0\n")
        with self.assertRaisesRegex(
            settings.SettingsError, r"korapy\.toml.*drive\.pulls_max_calls"
        ):
            settings.load(root=root, env={"KORAPY_CONFIG": config})


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def run_pass(self, store, plan=True, found=(), **fields):
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store, self.lock, out, err, plan=plan, found=found, policy_dir=self.policies, **fields
        )
        return code, out.getvalue(), err.getvalue()

    def test_plan_prints_gated_verdict_rows_and_exits_zero(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "closed"), ("api", 2, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("app#7: start", out)
        self.assertNotIn("app#9", out)
        self.assertNotIn("api#2", out)
        self.assertIn("would start 1", out)

    def test_plan_with_no_projects_prints_an_empty_table(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertNotIn("app#7", out)
        self.assertIn("0 candidate(s)", out)

    def test_plan_leaves_store_and_outbox_untouched(self):
        store = stored(self.db, [("app", 7, "open")])
        store.enqueue("app", 7, "status", "working", now=1500.0)
        store.close()
        with open(self.db, "rb") as handle:
            before_bytes = handle.read()
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, _, _ = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        store.close()
        with open(self.db, "rb") as handle:
            after_bytes = handle.read()
        self.assertEqual(before_bytes, after_bytes)
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        self.assertEqual(len(store.outbox_rows()), 1)

    def test_plan_on_empty_store_exits_zero(self):
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, out, _ = self.run_pass(store)
        self.assertEqual(code, 0)
        self.assertIn("0 candidate", out)

    def test_plan_never_takes_the_lock(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with open(self.lock, "w") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            code, out, err = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("app#7: start", out)

    def test_plan_with_a_persistent_holder_lock_skips_the_capture(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with mock.patch.object(
            drive,
            "finished_holders",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            code, out, err = self.run_pass(
                store,
                found=[("blue", [("app", 7, "start")])],
                agent_states={},
            )
        self.assertEqual(code, 0)
        self.assertIn("app#7: start", out)
        self.assertIn("finished holder capture skipped", err)

    def test_non_plan_pass_takes_and_releases_the_lock(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store, plan=False, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("app#7: start", out)
        with open(self.lock, "r") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_second_concurrent_non_plan_pass_exits_two(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with open(self.lock, "w") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            code, _, err = self.run_pass(store, plan=False)
        self.assertEqual(code, 2)
        self.assertIn("already running", err)

    def test_lock_setup_failure_is_exit_four(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w") as handle:
            handle.write("not a directory")
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            os.path.join(blocker, "drive.lock"),
            out,
            err,
            plan=False,
        )
        self.assertEqual(code, 4)
        self.assertIn("korapy drive:", err.getvalue())
        self.assertNotIn("already running", err.getvalue())

    def test_corrupt_row_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        store.close()
        raw = sqlite3.connect(self.db)
        try:
            raw.execute("INSERT INTO items(repo, number, labels) VALUES('app', 1, 'bogus')")
            raw.commit()
        finally:
            raw.close()
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        code, _, err = self.run_pass(store)
        self.assertEqual(code, 4)
        self.assertIn("korapy drive:", err)

    def test_raw_sqlite_error_is_exit_four(self):
        class BrokenStore:
            def items(self, **kwargs):
                raise sqlite3.OperationalError("boom")

        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(BrokenStore(), self.lock, out, err)
        self.assertEqual(code, 4)
        self.assertIn("korapy drive:", err.getvalue())

    def test_plan_summary_says_plan(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, _ = self.run_pass(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[-1].startswith("plan:"), out)

    def test_locking_pass_summary_says_pass(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, _ = self.run_pass(store, plan=False, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[-1].startswith("pass:"), out)

    def test_non_contention_flock_error_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        real = drive._flock

        def broken(_fd: int, _op: int) -> None:
            raise OSError(errno.ENOLCK, "No locks available")

        drive._flock = broken
        try:
            code, _, err = self.run_pass(store, plan=False)
        finally:
            drive._flock = real
        self.assertEqual(code, 4)
        self.assertIn("korapy drive:", err)
        self.assertNotIn("already running", err)

    def test_unreadable_live_state_is_exit_three(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        kept = {k: v for k, v in os.environ.items() if not k.startswith("KORAPY_")}
        env = dict(
            kept,
            KORAPY_CONFIG=os.path.join(self.tmp.name, "config"),
            KORAPY_STATE=os.path.join(self.tmp.name, "state"),
        )

        def broken(*args, **kwargs):
            raise RuntimeError("unit listing unreachable")

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(launch, "admission_state", broken),
        ):
            out, err = io.StringIO(), io.StringIO()
            code = hermetic_pass(store, self.lock, out, err, projects_fn=lambda: [], live=None)
        self.assertEqual(code, 3)
        self.assertIn("cannot read live state", err.getvalue())

    def test_corrupt_ledger_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with mock.patch.object(
            quota_ledger,
            "load",
            side_effect=quota_ledger.LedgerError("quota ledger is corrupt"),
        ):
            out, err = io.StringIO(), io.StringIO()
            code = hermetic_pass(store, self.lock, out, err, projects_fn=lambda: [], ledger=None)
        self.assertEqual(code, 4)
        self.assertIn("korapy drive:", err.getvalue())

    def test_project_registry_load_failure_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)

        def broken():
            raise projects_mod.ProjectsError("projects.d/blue.toml: boom")

        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(store, self.lock, out, err, projects_fn=broken)
        self.assertEqual(code, 4)
        self.assertIn("blue.toml", err.getvalue())


class LockPathTest(unittest.TestCase):
    def test_lock_path_comes_from_the_state_dir(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-state-")
        self.addCleanup(tmp.cleanup)
        old = os.environ.get("KORAPY_STATE")
        os.environ["KORAPY_STATE"] = tmp.name
        try:
            from korapy import paths

            self.assertEqual(drive.lock_path(), os.path.join(paths.state_dir(), "drive.lock"))
            self.assertEqual(drive.lock_path(), os.path.join(tmp.name, "drive.lock"))
        finally:
            if old is None:
                del os.environ["KORAPY_STATE"]
            else:
                os.environ["KORAPY_STATE"] = old


class StatePathsTest(unittest.TestCase):
    def test_every_drive_state_path_comes_from_the_state_dir(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-state-")
        self.addCleanup(tmp.cleanup)
        old = os.environ.get("KORAPY_STATE")
        os.environ["KORAPY_STATE"] = tmp.name
        try:
            from korapy import paths

            state = paths.state_dir()
            self.assertEqual(drive.lock_path(), os.path.join(state, "drive.lock"))
            self.assertEqual(drive.drive_dir(), os.path.join(state, "drive"))
            self.assertEqual(drive.journal_path(), os.path.join(state, "drive", "journal.jsonl"))
            self.assertEqual(drive.pause_path(), os.path.join(state, "drive.pause"))
            self.assertEqual(drive.day_path(), os.path.join(state, "drive.starts.json"))
            self.assertEqual(drive.restarts_path(), os.path.join(state, "drive-restarts.json"))
        finally:
            if old is None:
                del os.environ["KORAPY_STATE"]
            else:
                os.environ["KORAPY_STATE"] = old


class HostFullExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-full-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_full_host_with_no_starts_exits_zero_not_75(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        launcher = mock.MagicMock()
        code = hermetic_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            live=frozenset({("blue", "app", 1)}),
            limits=admission.Limits(total=1, projects={"blue": {"floor": 0, "ceiling": None}}),
            launcher=launcher,
            host="host-1",
        )
        # A saturated host is not an error: the pass did its dispatch
        # work, so it exits 0, never cli.NO_SLOT (75, "no free slot").
        self.assertEqual(code, drive.OK)
        self.assertNotEqual(code, cli.NO_SLOT)
        self.assertIn("host full", out.getvalue())
        launcher.assert_not_called()


class CmdDriveTest(unittest.TestCase):
    def test_unreadable_store_is_exit_four(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-cmd-")
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "board.sqlite"), "w") as handle:
            handle.write("not a database")
        old = os.environ.get("KORAPY_STATE")
        os.environ["KORAPY_STATE"] = tmp.name
        try:
            code = drivehub.cmd_drive(argparse.Namespace(plan=True))
        finally:
            if old is None:
                del os.environ["KORAPY_STATE"]
            else:
                os.environ["KORAPY_STATE"] = old
        self.assertEqual(code, 4)


class CmdDriveMapFailureTest(unittest.TestCase):
    """A broken project config at map-build time refuses the pass.

    `production_repo_map` raising `SettingsError` through `cmd_drive`
    exits 4 in both the plan and the claiming path, naming the
    problem, instead of joining the pass against a guessed map.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-map-")
        self.addCleanup(self.tmp.cleanup)
        self.out, self.err = io.StringIO(), io.StringIO()

    def run_cmd(self, plan):
        host_settings = settings.resolve(
            env={}, flags={"drive.max_starts_per_day": 50, "board.hold_label": "paused"}
        )
        with (
            mock.patch.object(drive.board, "open_store", return_value=mock.MagicMock()),
            mock.patch.object(drive.settings, "load", return_value=host_settings),
            mock.patch.object(drive.projects, "load_all", return_value=[]),
            mock.patch.object(
                drive,
                "production_repo_map",
                side_effect=settings.SettingsError("test config names no tracker repo"),
            ),
            mock.patch.object(drive.sys, "stdout", self.out),
            mock.patch.object(drive.sys, "stderr", self.err),
        ):
            return drivehub.cmd_drive(argparse.Namespace(plan=plan, drive_verb=None))

    def test_plan_with_a_broken_repo_map_is_a_usage_error(self):
        self.assertEqual(self.run_cmd(plan=True), 4)
        self.assertIn("korapy drive: test config names no tracker repo", self.err.getvalue())

    def test_claiming_with_a_broken_repo_map_is_a_usage_error(self):
        self.assertEqual(self.run_cmd(plan=False), 4)
        self.assertIn("korapy drive: test config names no tracker repo", self.err.getvalue())


class HoldLabelWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-hold-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)

    def test_a_passed_hold_label_skips_the_host_defaults(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            drive, "_default_host_inputs", side_effect=AssertionError("must not resolve")
        ):
            code = hermetic_pass(
                store,
                self.lock,
                out,
                err,
                found=[("blue", [("app", 7, "start")])],
                policy_dir=self.policies,
            )
        self.assertEqual(code, 0)

    def passing_cmd(self, plan):
        seen = {}
        launcher_seen = {}
        built = {
            "stuck": mock.MagicMock(return_value=({}, 600.0, {})),
            "finished": mock.MagicMock(return_value={}),
            "orphan": mock.MagicMock(return_value=(set(), set(), 3600.0)),
        }

        def fake_run_pass(**kwargs):
            seen.update(kwargs)
            return 0

        store = mock.MagicMock()
        host_settings = settings.resolve(
            env={}, flags={"drive.max_starts_per_day": 50, "board.hold_label": "paused"}
        )
        old = os.environ.get("KORAPY_STATE")
        os.environ["KORAPY_STATE"] = self.tmp.name
        try:
            with (
                mock.patch.object(drive.board, "open_store", return_value=store),
                mock.patch.object(drive.settings, "load", return_value=host_settings) as loaded,
                mock.patch.object(drive.projects, "load_all", return_value=[]),
                mock.patch.object(
                    drive, "production_repo_map", return_value={"app": "acme/app"}
                ) as built_map,
                mock.patch.object(drive, "production_stuck_inputs", built["stuck"]),
                mock.patch.object(drive, "production_finished_for", built["finished"]),
                mock.patch.object(drive, "production_agent_roles", return_value={}),
                mock.patch.object(drive, "production_orphan_inputs", built["orphan"]),
                mock.patch.object(drive, "production_operators", return_value=({}, None)),
                mock.patch.object(
                    drive,
                    "production_launcher",
                    side_effect=lambda *args, **kwargs: launcher_seen.update(kwargs),
                ),
                mock.patch.object(drive, "production_stopper", return_value=None),
                mock.patch.object(drive, "run_pass", side_effect=fake_run_pass),
            ):
                code = drivehub.cmd_drive(argparse.Namespace(plan=plan, drive_verb=None))
        finally:
            if old is None:
                del os.environ["KORAPY_STATE"]
            else:
                os.environ["KORAPY_STATE"] = old
        return code, seen, launcher_seen, loaded, built, built_map

    def test_a_plan_reads_the_hold_label_once_and_passes_it_in(self):
        code, seen, _, loaded, _, _ = self.passing_cmd(plan=True)
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("hold_label"), "paused")
        self.assertEqual(seen.get("agent_tickets"), {})
        self.assertEqual(seen.get("live_units"), set())
        self.assertEqual(loaded.call_count, 1)

    def test_a_claiming_pass_reads_the_hold_label_once_and_passes_it_in(self):
        code, seen, launcher_seen, loaded, _, _ = self.passing_cmd(plan=False)
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("hold_label"), "paused")
        self.assertEqual(seen.get("agent_tickets"), {})
        self.assertEqual(launcher_seen.get("hold_label"), "paused")
        self.assertEqual(loaded.call_count, 1)

    def test_a_claiming_pass_reads_max_starts_from_the_resolved_settings(self):
        code, seen, _, _, _, _ = self.passing_cmd(plan=False)
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("max_starts"), 50)

    def test_a_plan_builds_one_repo_map_and_threads_it_to_every_builder(self):
        code, seen, _, _, built, built_map = self.passing_cmd(plan=True)
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("repo_map"), {"app": "acme/app"})
        self.assertEqual(built_map.call_count, 1)
        for name in ("stuck", "finished", "orphan"):
            self.assertEqual(built[name].call_args.kwargs.get("mapping"), {"app": "acme/app"}, name)

    def test_the_plan_pass_reads_the_same_projects_the_map_was_built_from(self):
        code, seen, _, _, _, _ = self.passing_cmd(plan=True)
        self.assertEqual(code, 0)
        self.assertEqual(seen["projects_fn"](), [])

    def test_a_claiming_pass_threads_the_same_map_into_the_pass(self):
        code, seen, _, _, _, built_map = self.passing_cmd(plan=False)
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("repo_map"), {"app": "acme/app"})
        self.assertEqual(built_map.call_count, 1)

    def test_a_claiming_pass_reads_the_same_projects_the_map_was_built_from(self):
        code, seen, _, _, _, _ = self.passing_cmd(plan=False)
        self.assertEqual(code, 0)
        self.assertEqual(seen["projects_fn"](), [])


class ClaimingPassResolvesOnceTest(unittest.TestCase):
    """The claiming pass resolves short-to-tracker once per root and shares it.

    One `repo_operators` call per registered root feeds the string
    map, the operators and the tracker factory, so a config edit
    between the builds cannot leave the factory without the map's
    repo: the factory serves every repo the map names.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-once-")
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.config = os.path.join(self.tmp.name, "config")
        self.root = os.path.join(self.tmp.name, "ops")
        os.makedirs(os.path.join(self.config, "projects.d"))
        os.makedirs(self.root)
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\ntracker.repo = "acme/app"\n')
        with open(
            os.path.join(self.config, "projects.d", "blue.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write(f"name = 'blue'\nunit_prefix = 'blue-agent-'\nroot = {self.root!r}\n")
        store = stored(os.path.join(self.state, "board.sqlite"), [])
        store.close()
        self.seen = {}
        self.out, self.err = io.StringIO(), io.StringIO()
        env = {"KORAPY_STATE": self.state, "KORAPY_CONFIG": self.config}
        self._env_patch = mock.patch.dict(os.environ, env)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_one_resolution_feeds_the_map_the_operators_and_the_factory(self):
        real_operators = syncli.repo_operators
        calls = []

        def counting(root, names, git):
            calls.append(root)
            return real_operators(root, names, git)

        def fake_run_pass(**kwargs):
            self.seen.update(kwargs)
            return 0

        with (
            mock.patch.object(syncli, "repo_operators", side_effect=counting),
            mock.patch.object(drive, "production_stuck_inputs", return_value=({}, 600.0, {})),
            mock.patch.object(drive, "production_finished_for", return_value={}),
            mock.patch.object(drive, "production_agent_roles", return_value={}),
            mock.patch.object(
                drive, "production_orphan_inputs", return_value=(set(), set(), 3600.0)
            ),
            mock.patch.object(drive, "production_launcher", return_value=mock.MagicMock()),
            mock.patch.object(drive, "production_stopper", return_value=None),
            mock.patch.object(drive, "run_pass", side_effect=fake_run_pass),
            mock.patch.object(drive.sys, "stdout", self.out),
            mock.patch.object(drive.sys, "stderr", self.err),
        ):
            code = drivehub.cmd_drive(argparse.Namespace(plan=False, drive_verb=None))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [self.root])
        self.assertEqual(self.seen.get("repo_map"), {"app": "acme/app"})
        self.assertEqual(self.seen.get("operators"), {"acme/app": ["acme"]})
        factory = self.seen.get("tracker_factory")
        self.assertIsNotNone(factory)
        assert factory is not None
        for repo in self.seen["repo_map"].values():
            self.assertIsNotNone(factory(repo))


class DriveSyncExitTest(unittest.TestCase):
    """The claiming pass buckets sync failures: corrupt rows exit 4."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-drive-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")

    def test_corrupt_row_exits_4_naming_the_row(self):
        store = stored(self.db, [("acme/app", 7, "open"), ("acme/api", 2, "open")])
        self.addCleanup(store.close)
        raw = sqlite3.connect(self.db)
        try:
            raw.execute(
                "UPDATE items SET extra=? WHERE repo=? AND number=?",
                ("{{{broken", "acme/app", 7),
            )
            raw.commit()
        finally:
            raw.close()
        table = {
            "acme/app": FakeTracker([]),
            "acme/api": FakeTracker([reading(repo="acme/api", number=2, updated_at=2000.0)]),
        }
        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            tracker_factory=table.__getitem__,
            now=2000.0,
        )
        self.assertEqual(code, 4)
        self.assertIn("acme/app#7", err.getvalue())

    def test_refused_only_sync_exits_1(self):
        store = stored(self.db, [("acme/app", 7, "open")])
        self.addCleanup(store.close)

        class RejectingTracker(FakeTracker):
            def changes(self, repo, cursor):
                raise tracker.TrackerRejected("tracker rejected: Validation Failed (HTTP 422)")

        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            tracker_factory=lambda repo: RejectingTracker([]),
            now=2000.0,
        )
        self.assertEqual(code, 1)
        self.assertIn("tracker rejected", err.getvalue())

    def test_refused_plus_outage_exits_3(self):
        store = stored(self.db, [("acme/app", 7, "open"), ("acme/api", 2, "open")])
        self.addCleanup(store.close)

        class MixedTracker(FakeTracker):
            def changes(self, repo, cursor):
                if repo == "acme/app":
                    raise tracker.TrackerRejected("tracker rejected: Validation Failed (HTTP 422)")
                raise tracker.TrackerError("connection refused")

        out, err = io.StringIO(), io.StringIO()
        code = hermetic_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            tracker_factory=lambda repo: MixedTracker([]),
            now=2000.0,
        )
        self.assertEqual(code, 3)
        self.assertIn("tracker rejected", err.getvalue())
        self.assertIn("connection refused", err.getvalue())


if __name__ == "__main__":
    unittest.main()
