"""A registry short name is never a claim's tracker repo. Fakes only.

The registry entry for a worker records its ticket with the short
``[[repo]]`` name from the project's ``korapy.toml`` (``app``), while
the board store claims the same ticket under the tracker repo
(``acme/app``). Every join between the two goes through the one
short-to-tracker resolver; a raw ``ticket != (claim.repo,
claim.number)`` comparison can never match and mistakes every finished
worker for a crash.

The lock is a real ``flock`` over temporary files only; the store is a
temporary board store; the registry and the project config are real
files in temporary directories, written through the production
writers (``registry.register``/``done``, ``store.claim``,
``korapy.toml`` plus ``tracker.repo``). Unit listings are a recording
fake at the seam. No ``gh``, no systemd, no network, no real state
dir.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from korapy import board, drive, registry, settings
from korapy import projects as projects_mod
from tests.test_drive import stored
from tests.test_drive_claims import FakeLauncher, drive_pass
from tests.test_drive_review import review_pass
from tests.test_drive_stuck import FakeStop
from tests.test_tracker import FakeTracker, reading

SHORT = "app"
TRACKER = "acme/app"
NUMBER = 416
NOW = 20000.0
OLD = NOW - 4000.0
STALE_S = 600.0
GONE_S = 3600.0
HOLDER = "drive-a1b2c3d4-416"
UNIT = "blue-agent-app-416"
UNIT_R1 = "blue-agent-app-416-r1"
HOST = "host-1"

TOML = """\
[[repo]]
name = "app"
clone.path = "clone"
tracker.repo = "acme/app"
"""


def dead_git(args, cwd):
    """A git seam that must never fire: the fixture names its tracker repo."""
    raise AssertionError(f"unexpected git call: {args!r} in {cwd!r}")


class RepoKeyCase(unittest.TestCase):
    """A project whose short name and tracker repo differ, on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-repo-key-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ops")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.regdir = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.regdir)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.registered = [
            projects_mod.Project(name="blue", unit_prefix="blue-agent-", root=self.root),
        ]
        self.cfg = registry.default_config()
        patcher = mock.patch.object(drive.registry, "config", return_value=dict(self.cfg))
        self.addCleanup(patcher.stop)
        patcher.start()

    def register_done(self, agent_id, *, at, role="worker"):
        registry.register(
            agent_id,
            "muse",
            ticket=registry.make_ticket(SHORT, NUMBER, "blue"),
            role=role,
            now=at,
            path=self.regdir,
        )
        registry.done(agent_id, now=at + 1.0, path=self.regdir)

    def register_open(self, agent_id, *, at, role="worker"):
        registry.register(
            agent_id,
            "muse",
            ticket=registry.make_ticket(SHORT, NUMBER, "blue"),
            role=role,
            now=at,
            path=self.regdir,
        )

    def claimed_tracker_store(self, *, at=OLD, agent=HOLDER):
        store = stored(self.db, [(TRACKER, NUMBER, "open")])
        self.addCleanup(store.close)
        store.claim(TRACKER, NUMBER, agent, HOST, now=at)
        return store

    def builders(self, *, now=NOW):
        states, stale_s, tickets = drive.production_stuck_inputs(
            self.registered, now=now, path=self.regdir, git=dead_git
        )
        finished_for = drive.production_finished_for(
            self.registered, now=now, cfg=self.cfg, path=self.regdir, git=dead_git
        )
        return states, stale_s, tickets, finished_for


class BuilderKeyTest(RepoKeyCase):
    def test_the_ticket_index_and_finished_map_use_the_tracker_repo(self):
        self.register_done(UNIT, at=OLD)
        self.register_done(UNIT_R1, at=OLD + 100.0)
        store = self.claimed_tracker_store()
        states, _, tickets, finished_for = self.builders()
        self.assertEqual(tickets, {UNIT: (TRACKER, NUMBER), UNIT_R1: (TRACKER, NUMBER)})
        self.assertEqual(finished_for, {(TRACKER, NUMBER): (UNIT_R1, "worker")})
        claim = store.claim_of(TRACKER, NUMBER)
        self.assertIsNotNone(claim)
        assert claim is not None
        seen = drive.entry_for_claim(claim, agent_states=states, agent_tickets=tickets)
        self.assertIsNotNone(seen)
        assert seen is not None
        self.assertEqual(seen[0], UNIT_R1)
        self.assertEqual(seen[1], registry.FINISHED)

    def test_entries_for_an_unmapped_name_keep_their_short_key(self):
        self.register_done(UNIT, at=OLD)
        with mock.patch.object(drive, "production_repo_map", return_value={}):
            states, _, tickets, finished_for = self.builders()
        self.assertEqual(tickets, {UNIT: (SHORT, NUMBER)})
        self.assertEqual(finished_for, {(SHORT, NUMBER): (UNIT, "worker")})
        self.assertIn(UNIT, states)


class OneMapPerPassTest(RepoKeyCase):
    def test_given_the_pass_map_the_builders_never_resolve_their_own(self):
        self.register_done(UNIT_R1, at=OLD)
        with mock.patch.object(
            drive,
            "production_repo_map",
            side_effect=AssertionError("the pass must build the map once"),
        ):
            states, _, tickets = drive.production_stuck_inputs(
                self.registered, now=NOW, path=self.regdir, git=dead_git, mapping={SHORT: TRACKER}
            )
            finished_for = drive.production_finished_for(
                self.registered,
                now=NOW,
                cfg=self.cfg,
                path=self.regdir,
                git=dead_git,
                mapping={SHORT: TRACKER},
            )
        self.assertEqual(tickets, {UNIT_R1: (TRACKER, NUMBER)})
        self.assertEqual(finished_for, {(TRACKER, NUMBER): (UNIT_R1, "worker")})
        self.assertIn(UNIT_R1, states)

    def test_given_the_pass_map_orphan_inputs_never_resolve_their_own(self):
        listed = [(UNIT_R1, "active", "running")]
        with (
            mock.patch.object(drive.units, "list_units", return_value=list(listed)),
            mock.patch.object(
                drive,
                "production_repo_map",
                side_effect=AssertionError("the pass must build the map once"),
            ),
        ):
            _present, live, _gone = drive.production_orphan_inputs(
                self.registered,
                now=NOW,
                path=self.regdir,
                git=dead_git,
                mapping={SHORT: TRACKER},
            )
        self.assertEqual(live, {(TRACKER, NUMBER)})

    def test_the_pass_map_is_the_one_resolver_over_the_roots(self):
        self.assertEqual(drive.production_repo_map(self.registered, git=dead_git), {SHORT: TRACKER})

    def test_the_admission_live_set_keys_on_the_tracker_repo(self):
        triples = {("blue", SHORT, NUMBER), ("blue", "other/repo", 9)}
        with mock.patch.object(drive.launch, "admission_state", return_value=triples):
            live = drive._default_live(self.registered, git=dead_git)
        self.assertEqual(live, {("blue", TRACKER, NUMBER), ("blue", "other/repo", 9)})

    def test_the_admission_live_set_takes_the_pass_map_when_given(self):
        triples = {("blue", SHORT, NUMBER)}
        with (
            mock.patch.object(drive.launch, "admission_state", return_value=triples),
            mock.patch.object(
                drive,
                "production_repo_map",
                side_effect=AssertionError("the pass must build the map once"),
            ),
        ):
            live = drive._default_live(self.registered, mapping={SHORT: TRACKER})
        self.assertEqual(live, {("blue", TRACKER, NUMBER)})

    def test_the_pass_threads_its_map_into_the_admission_live_set(self):
        store = self.claimed_tracker_store()
        triples = {("blue", SHORT, NUMBER)}
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(drive.launch, "admission_state", return_value=triples),
            mock.patch.object(
                drive,
                "production_repo_map",
                side_effect=AssertionError("the pass must build the map once"),
            ),
        ):
            code = drive.run_pass(
                plan=True,
                store=store,
                lock_path=os.path.join(self.tmp.name, "drive.lock"),
                out=out,
                err=err,
                now=NOW,
                projects_fn=lambda: list(self.registered),
                live=None,
                limits=drive.admission.Limits(total=4),
                quotas={},
                ledger={},
                read_labels=lambda kind, repo, number: [],
                hold_label="hold",
                present_agents=frozenset(),
                live_units=frozenset(),
                gone_s=3600.0,
                repo_map={SHORT: TRACKER},
            )
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_a_claiming_pass_threads_its_map_into_orphan_recovery(self):
        store = self.claimed_tracker_store()
        seen = {}

        def fake_orphan(registered, *, now, path=None, git=None, mapping=None):
            seen.update(registered=registered, mapping=mapping)
            return frozenset(), frozenset(), GONE_S

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(drive, "production_orphan_inputs", side_effect=fake_orphan),
            mock.patch.object(
                drive,
                "production_repo_map",
                side_effect=AssertionError("the pass must build the map once"),
            ),
        ):
            code = drive.run_pass(
                plan=False,
                store=store,
                lock_path=os.path.join(self.tmp.name, "drive.lock"),
                out=out,
                err=err,
                now=NOW,
                projects_fn=lambda: list(self.registered),
                live=frozenset(),
                limits=drive.admission.Limits(total=4),
                quotas={},
                ledger={},
                read_labels=lambda kind, repo, number: [],
                hold_label="hold",
                present_agents=None,
                live_units=None,
                gone_s=None,
                repo_map={SHORT: TRACKER},
            )
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(seen.get("mapping"), {SHORT: TRACKER})
        self.assertIsNotNone(seen.get("registered"))


class BrokenConfigTest(unittest.TestCase):
    """A project root whose korapy.toml cannot resolve refuses, never guesses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-broken-config-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ops")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\ntracker.repo = "not-a-repo"\n')
        self.registered = [
            projects_mod.Project(name="blue", unit_prefix="blue-agent-", root=self.root),
        ]
        patcher = mock.patch.object(
            drive.registry, "config", return_value=registry.default_config()
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_stuck_inputs_refuse_a_broken_project_config(self):
        with self.assertRaises(settings.SettingsError):
            drive.production_stuck_inputs(self.registered, now=NOW, git=dead_git)

    def test_finished_for_refuses_a_broken_project_config(self):
        with self.assertRaises(settings.SettingsError):
            drive.production_finished_for(self.registered, now=NOW, git=dead_git)

    def test_orphan_inputs_refuse_a_broken_project_config(self):
        with self.assertRaises(settings.SettingsError):
            drive.production_orphan_inputs(self.registered, now=NOW, git=dead_git)


class FinishedKeptTest(RepoKeyCase):
    def sweep(self, store, **over):
        values = {
            "agent_states": {},
            "stale_s": STALE_S,
            "gone_s": GONE_S,
            "live_units": frozenset(),
            "now": NOW,
            "host": HOST,
            "stuck_project": lambda repo, number: "blue",
            "stop_unit": FakeStop(),
            "restarter": FakeLauncher(),
            "tracker_factory": None,
            "restarts_path": None,
            "read_labels": lambda kind, repo, number: [],
            "hold_label": "hold",
            "requested": {(TRACKER, NUMBER)},
        }
        values.update(over)
        stop, launcher = values["stop_unit"], values["restarter"]
        out, err = io.StringIO(), io.StringIO()
        result = drive.handle_stuck(store, out=out, err=err, **values)
        return result, out.getvalue(), err.getvalue(), stop, launcher

    def test_a_finished_ok_worker_is_kept_never_restarted(self):
        self.register_done(UNIT, at=OLD)
        self.register_done(UNIT_R1, at=OLD + 100.0)
        store = self.claimed_tracker_store()
        states, _, tickets, _ = self.builders()
        (restarted, capped), text, _, stop, launcher = self.sweep(
            store, agent_states=states, agent_tickets=tickets
        )
        self.assertEqual((restarted, capped), (0, False))
        self.assertEqual(stop.calls, [])
        self.assertEqual(launcher.calls, [])
        self.assertIn("finished, not stuck, claim kept", text)
        self.assertNotIn("restarted", text)
        found = store.claim_of(TRACKER, NUMBER)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.agent, HOLDER)

    def test_a_crashed_worker_without_a_close_is_still_a_restart_candidate(self):
        self.register_open(UNIT_R1, at=NOW - STALE_S - 1.0)
        store = self.claimed_tracker_store()
        states, _, tickets, _ = self.builders()
        (restarted, _capped), text, _, stop, launcher = self.sweep(
            store, agent_states=states, agent_tickets=tickets
        )
        self.assertEqual(restarted, 1)
        self.assertEqual(stop.calls, [(TRACKER, NUMBER)])
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, agent, _title, _author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", TRACKER, NUMBER, "start"))
        self.assertEqual(agent, HOLDER)
        self.assertIn("restarted", text)


class OrphanUnitsKeyTest(RepoKeyCase):
    def test_live_units_are_listed_under_the_tracker_repo(self):
        self.register_done(UNIT_R1, at=OLD)
        listed = [(UNIT_R1, "active", "running")]
        with mock.patch.object(drive.units, "list_units", return_value=list(listed)):
            present, live, _gone = drive.production_orphan_inputs(
                self.registered, now=NOW, path=self.regdir, git=dead_git
            )
        self.assertEqual(live, {(TRACKER, NUMBER)})
        self.assertEqual(present, set())


class ReviewerTakeoverKeyTest(RepoKeyCase):
    def test_the_finished_worker_gets_its_reviewer(self):
        self.register_done(UNIT_R1, at=1000.0)
        store = stored(self.db, [(TRACKER, NUMBER, "open")])
        self.addCleanup(store.close)
        made = store.claim(TRACKER, NUMBER, HOLDER, HOST, now=1000.0)
        states, _, tickets, finished_for = self.builders(now=2000.0)
        fake = FakeTracker([reading(repo=TRACKER, number=NUMBER, updated_at=1900.0)])
        launcher = FakeLauncher()
        out, err = io.StringIO(), io.StringIO()
        policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(policies)
        code = review_pass(
            store,
            os.path.join(self.tmp.name, "drive.lock"),
            out,
            err,
            found=[("blue", [(TRACKER, NUMBER, "review")])],
            policy_dir=policies,
            fake_tracker=fake,
            launcher=launcher,
            agent_states=states,
            agent_tickets=tickets,
            finished_for=finished_for,
            now=2000.0,
        )
        text = out.getvalue()
        self.assertEqual(code, 0, text + err.getvalue())
        self.assertEqual(len(launcher.calls), 1)
        project, repo, number, kind, _agent, _title, author = launcher.calls[0]
        self.assertEqual((project, repo, number, kind), ("blue", TRACKER, NUMBER, "review"))
        self.assertEqual(author, HOLDER)
        self.assertIn("takes over", text)
        found = store.claim_of(TRACKER, NUMBER)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertNotEqual(found.agent, HOLDER)
        self.assertEqual(found.epoch, made.epoch + 1)


if __name__ == "__main__":
    unittest.main()
