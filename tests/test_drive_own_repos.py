"""A drive pass only drives its own repositories. Fakes only.

The board store is shared per host and keyed by tracker repository, so
one project's rows sit next to another's. Each project must only see
the repositories its own ops root lists: the pass filters candidates
per project through the one resolver, never claims a foreign row, and
the launch path refuses a foreign candidate instead of starting it.
The lock is a real `flock`, but only over files inside temporary
directories; the store is a temporary board store; policies are stub
modules written to a temporary directory; project configs are real
`korapy.toml` files in temporary ops roots; every other host read is
injected plain data. No `gh`, no systemd, no network, no real state
dir.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from korapy import admission, board, drive
from korapy import projects as projects_mod
from korapy import settings
from tests.test_drive import stored
from tests.test_drive_claims import FakeLauncher
from tests.test_drive_stuck import FakeStop
from tests.test_tracker import FakeTracker

APP = "acme/app"
WEB = "acme/web"
OTHER = "acme/other"
NOW = 2000.0
HOST = "host-1"
HOLDER = "drive-a1b2c3d4-11"

POLICY_ALL = """\
def eligible(rows, *, now):
    from korapy.eligibility import StartRequest
    return [
        StartRequest(row.repo, row.number, "start")
        for row in rows
        if row.state == "open" and not row.agent
    ]
"""


def dead_git(args, cwd):
    """A git seam that must never fire: every fixture names its tracker repo."""
    raise AssertionError(f"unexpected git call: {args!r} in {cwd!r}")


def write_ops(root, entries):
    """An ops root whose `korapy.toml` lists `{short: tracker}` entries."""
    lines = []
    for short, tracker_repo in entries.items():
        lines.append("[[repo]]")
        lines.append(f'name = "{short}"')
        lines.append(f'tracker.repo = "{tracker_repo}"')
    with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


class OwnReposCase(unittest.TestCase):
    """Two projects on one host, each driving its own repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-own-repos-")
        self.addCleanup(self.tmp.cleanup)
        self.blue_root = os.path.join(self.tmp.name, "blue-ops")
        self.green_root = os.path.join(self.tmp.name, "green-ops")
        os.makedirs(self.blue_root)
        os.makedirs(self.green_root)
        write_ops(self.blue_root, {"app": APP})
        write_ops(self.green_root, {"web": WEB})
        self.policy_dir = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policy_dir)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")

    def project(self, name, root):
        path = os.path.join(self.policy_dir, name + ".py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(POLICY_ALL)
        return projects_mod.Project(
            name=name,
            unit_prefix=name + "-agent-",
            root=root,
            eligibility=path + ":eligible",
        )

    def projects(self):
        return [
            self.project("blue", self.blue_root),
            self.project("green", self.green_root),
        ]

    def driven(self, registered=None):
        found = registered if registered is not None else self.projects()
        return {project.name: drive.driven_repos(project, git=dead_git) for project in found}

    def run_pass(self, store, out, err, *, plan, projects, driven, **over):
        values = {
            "plan": plan,
            "store": store,
            "lock_path": self.lock,
            "out": out,
            "err": err,
            "now": NOW,
            "projects_fn": lambda: list(projects),
            "live": frozenset(),
            "limits": admission.Limits(total=4),
            "quotas": {},
            "ledger": {},
            "read_labels": lambda kind, repo, number: [],
            "hold_label": "hold",
            "present_agents": frozenset(),
            "live_units": frozenset(),
            "gone_s": 3600.0,
            "driven": driven,
        }
        values.update(over)
        return drive.run_pass(**values)


class PlanScopeTest(OwnReposCase):
    def test_each_projects_plan_lists_only_its_own_repository(self):
        store = stored(self.db, [(APP, 7, "open"), (WEB, 9, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = self.run_pass(
            store, out, err, plan=True, projects=self.projects(), driven=self.driven()
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        self.assertIn(f"blue {APP}#7", text)
        self.assertIn(f"green {WEB}#9", text)
        self.assertNotIn(f"blue {WEB}", text)
        self.assertNotIn(f"green {APP}", text)

    def test_the_plan_names_how_many_foreign_rows_it_ignored(self):
        store = stored(self.db, [(APP, 7, "open"), (WEB, 9, "open"), (WEB, 10, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = self.run_pass(
            store, out, err, plan=True, projects=self.projects(), driven=self.driven()
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        self.assertIn("project blue: ignored 2 foreign rows", text)
        self.assertIn("project green: ignored 1 foreign row", text)

    def test_a_policy_request_for_a_foreign_repo_is_dropped(self):
        from tests.test_drive import policy_source

        store = stored(self.db, [(APP, 7, "open"), (WEB, 9, "open")])
        self.addCleanup(store.close)
        path = os.path.join(self.policy_dir, "blue.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(policy_source([(APP, 7, "start"), (WEB, 9, "start")]))
        blue = projects_mod.Project(
            name="blue",
            unit_prefix="blue-agent-",
            root=self.blue_root,
            eligibility=path + ":eligible",
        )
        out, err = io.StringIO(), io.StringIO()
        code = self.run_pass(
            store, out, err, plan=True, projects=[blue], driven=self.driven([blue])
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        self.assertIn(f"blue {APP}#7", text)
        self.assertNotIn(WEB, text)

    def test_two_projects_on_one_host_both_proceed(self):
        store = stored(self.db, [(APP, 7, "open"), (WEB, 9, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = self.run_pass(
            store,
            out,
            err,
            plan=True,
            projects=self.projects(),
            driven=self.driven(),
            limits=admission.Limits(
                total=4,
                projects={"blue": {"floor": 0, "ceiling": 2}, "green": {"floor": 0, "ceiling": 2}},
            ),
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        self.assertIn(f"blue {APP}#7: start", text)
        self.assertIn(f"green {WEB}#9: start", text)


class ClaimScopeTest(OwnReposCase):
    def test_a_foreign_row_is_never_claimed(self):
        store = stored(self.db, [(APP, 7, "open"), (WEB, 9, "open"), (OTHER, 11, "open")])
        self.addCleanup(store.close)
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        code = self.run_pass(
            store,
            out,
            err,
            plan=False,
            projects=self.projects(),
            driven=self.driven(),
            launcher=launcher,
            host=HOST,
            stale_after=3600.0,
            tracker_factory=None,
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        by_project = {(call[0], call[1]) for call in launcher.calls}
        self.assertIn(("blue", APP), by_project)
        self.assertIn(("green", WEB), by_project)
        self.assertNotIn(("blue", WEB), by_project)
        self.assertNotIn(("green", APP), by_project)
        self.assertIsNone(store.claim_of(OTHER, 11))

    def test_the_launch_path_refuses_a_foreign_candidate(self):
        store = stored(self.db, [(WEB, 9, "open")])
        self.addCleanup(store.close)
        gated = drive.Gated(
            project="blue",
            repo=WEB,
            number=9,
            kind="start",
            verdict="start",
            reason=f"{WEB}#9: unclaimed, start slot",
            code="",
        )
        with self.assertRaises(drive.ForeignRepoError) as caught:
            drive._claim_and_launch(
                store,
                [gated],
                tracker_factory=None,
                operators={},
                launcher=FakeLauncher(),
                host=HOST,
                now=NOW,
                stale_after=3600.0,
                driven={"blue": frozenset({APP})},
            )
        self.assertIn(WEB, str(caught.exception))
        self.assertIsNone(store.claim_of(WEB, 9))

    def test_the_launch_path_refusal_exits_4_naming_the_repo(self):
        store = stored(self.db, [(WEB, 9, "open")])
        self.addCleanup(store.close)
        gated = drive.Gated(
            project="blue",
            repo=WEB,
            number=9,
            kind="start",
            verdict="start",
            reason=f"{WEB}#9: unclaimed, start slot",
            code="",
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            drive, "_gate_all", return_value=([gated], [], [(WEB, 9)], 0, False)
        ):
            code = self.run_pass(
                store,
                out,
                err,
                plan=False,
                projects=self.projects(),
                driven=self.driven(),
                launcher=FakeLauncher(),
                host=HOST,
                stale_after=3600.0,
                tracker_factory=None,
            )
        self.assertEqual(code, 4, out.getvalue() + err.getvalue())
        self.assertIn(WEB, err.getvalue())
        self.assertIsNone(store.claim_of(WEB, 9))


class PullsScopeTest(OwnReposCase):
    def test_a_claiming_pass_lists_no_foreign_repository(self):
        store = stored(self.db, [(APP, 7, "open"), (WEB, 9, "open"), (OTHER, 11, "open")])
        self.addCleanup(store.close)
        fake = FakeTracker()
        out, err = io.StringIO(), io.StringIO()
        code = self.run_pass(
            store,
            out,
            err,
            plan=False,
            projects=self.projects(),
            driven=self.driven(),
            launcher=FakeLauncher(),
            host=HOST,
            stale_after=3600.0,
            tracker_factory=lambda repo: fake,
            index_for=drive.pulls_index_for(lambda repo: fake),
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        listed = sorted(call[1] for call in fake.calls if call[0] == "pulls_index")
        self.assertEqual(listed, [APP, WEB])


class ShortNameCollisionTest(OwnReposCase):
    def test_same_short_name_projects_keep_their_own_rows(self):
        write_ops(self.green_root, {"app": OTHER})
        store = stored(self.db, [(APP, 7, "open"), (OTHER, 11, "open")])
        self.addCleanup(store.close)
        registered = self.projects()
        driven = self.driven(registered)
        self.assertEqual(driven, {"blue": frozenset({APP}), "green": frozenset({OTHER})})
        out, err = io.StringIO(), io.StringIO()
        code = self.run_pass(store, out, err, plan=True, projects=registered, driven=driven)
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        self.assertIn(f"blue {APP}#7", text)
        self.assertIn(f"green {OTHER}#11", text)
        self.assertNotIn(f"blue {OTHER}", text)
        self.assertNotIn(f"green {APP}", text)


class ResolverTest(OwnReposCase):
    def test_driven_repos_uses_the_roots_own_tracker_spelling(self):
        (project,) = [item for item in self.projects() if item.name == "blue"]
        self.assertEqual(drive.driven_repos(project, git=dead_git), frozenset({APP}))

    def test_driven_repos_shares_one_resolution_when_given(self):
        (project,) = [item for item in self.projects() if item.name == "blue"]
        resolved = {self.blue_root: {"app": (APP, ["operator"])}}
        with mock.patch.object(
            drive.syncli,
            "root_short_map",
            side_effect=AssertionError("the pass shares one resolution"),
        ):
            self.assertEqual(drive.driven_repos(project, resolved=resolved), frozenset({APP}))

    def test_driven_repos_is_empty_without_entries(self):
        bare = os.path.join(self.tmp.name, "bare-ops")
        os.makedirs(bare)
        project = projects_mod.Project(name="bare", unit_prefix="bare-agent-", root=bare)
        self.assertEqual(drive.driven_repos(project, git=dead_git), frozenset())

    def test_driven_repos_refuses_a_broken_tracker_repo(self):
        write_ops(self.blue_root, {"app": "not-a-repo"})
        (project,) = [item for item in self.projects() if item.name == "blue"]
        with self.assertRaises(settings.SettingsError):
            drive.driven_repos(project, git=dead_git)

    def test_driven_repos_refuses_a_root_without_a_resolution(self):
        (project,) = [item for item in self.projects() if item.name == "blue"]
        with self.assertRaises(settings.SettingsError):
            drive.driven_repos(project, resolved={})


class StuckScopeTest(OwnReposCase):
    def test_the_stuck_sweep_leaves_a_foreign_claim_held(self):
        store = stored(self.db, [(OTHER, 11, "open")])
        self.addCleanup(store.close)
        store.claim(OTHER, 11, HOLDER, HOST, now=NOW - 8000.0)
        stop, launcher = FakeStop(), FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        restarted, capped = drive.handle_stuck(
            store,
            agent_states={},
            stale_s=600.0,
            gone_s=3600.0,
            live_units=frozenset(),
            now=NOW,
            host=HOST,
            stuck_project=lambda repo, number: "blue",
            stop_unit=stop,
            restarter=launcher,
            tracker_factory=None,
            restarts_path=None,
            out=out,
            err=err,
            read_labels=lambda kind, repo, number: [],
            hold_label="hold",
            requested={(OTHER, 11)},
            driven={"blue": frozenset({APP})},
        )
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        found = store.claim_of(OTHER, 11)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, HOLDER)
        self.assertIn(OTHER, out.getvalue())


if __name__ == "__main__":
    unittest.main()
