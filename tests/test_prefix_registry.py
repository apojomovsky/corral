"""Ticket 125: one unit prefix, launch-time registration, snapshot caps.

A registered project's unit prefix is the one launch builds units with,
so admission, the projects live counts and stop all see the same agents.
A launch registers its unit in the agent registry, so the agent shows in
`agent list` and `snapshot` while it runs; resume and stop close the
entries they replace. Snapshot reads the per-repo cap from the registered
project's settings, not the host default.
"""

import argparse
import io
import json
import os
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral import agentcli, cli, launch, projects, prompts, registry, settings, units
from corral.runtimes import muse
from tests.test_launch import OpsCase, roomy
from tests.test_prompts import FULL
from tests.test_reviewrecord import git_in


class PrefixOps(OpsCase):
    """The launch ops case plus an [admission] cap and a host registration."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, settings.PROJECT_FILE), "a", encoding="utf-8") as h:
            h.write("\n[admission]\nrepo_cap = 1\n")
        directory = projects.projects_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "ops.toml"), "w", encoding="utf-8") as h:
            h.write(f'name = "ops"\nunit_prefix = "ops-agent"\nroot = "{self.root}"\nfloor = 0\n')

    def start(self, number=5, runtime="omp", repo="lib"):
        return launch.launch(
            launch.target(self.root, repo, flags={"agents.runtime": runtime} if runtime else None),
            number,
            "do the thing",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
        )


class PrefixTest(PrefixOps):
    def test_registered_root_resolves_the_registered_prefix(self):
        self.assertEqual(launch.target(self.root, "lib").prefix, "ops-agent")

    def test_projects_unit_prefix_is_the_single_place(self):
        resolved = settings.load(root=self.root, repo="lib")
        self.assertEqual(projects.unit_prefix(self.root, resolved), "ops-agent")
        self.assertEqual(projects.unit_prefix(self.root, resolved, registered=[]), "corral-agent-")

    def test_the_unit_name_starts_with_the_prefix_projects_counts_by(self):
        unit, _ = self.start()
        self.assertTrue(unit.startswith("ops-agent"))
        registered = projects.load_all()
        counts = launch.project_live_counts(registered, self.system.list_fn, registry.config())
        self.assertEqual(counts, {"ops": 1})

    def test_a_second_agent_on_the_repo_is_refused_under_repo_cap_1(self):
        self.start(number=5)
        with self.assertRaises(launch.CapFull) as ctx:
            self.start(number=6)
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertIn("repo cap", str(ctx.exception))


class LaunchRegistersTest(PrefixOps):
    def visible(self):
        return registry.visible(time.time(), cfg=registry.config())

    def live_ids(self):
        return [entry["id"] for entry, state in self.visible() if state == registry.LIVE]

    def test_launch_registers_the_unit_with_its_ticket(self):
        unit, _ = self.start()
        found = {entry["id"]: entry for entry, state in self.visible()}
        self.assertIn(unit, found)
        entry = found[unit]
        self.assertEqual(entry["runtime"], "omp")
        self.assertEqual(entry["ticket"], {"project": "ops", "repo": "lib", "number": 5})
        self.assertIn(unit, self.live_ids())

    def test_the_muse_runtime_maps_to_its_registry_name(self):
        with mock.patch.object(muse, "binary", return_value="/fake/muse"):
            unit, _ = self.start(runtime="muse")
        found = {entry["id"]: entry for entry, state in self.visible()}
        self.assertEqual(found[unit]["runtime"], "claude-code")

    def test_the_agent_stays_visible_to_admission_while_it_runs(self):
        self.start(number=5)
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(
                launch.target(self.root, "lib"), 5, list_fn=self.system.list_fn, usage=roomy
            )
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_the_registered_agent_lists(self):
        unit, _ = self.start()
        out = io.StringIO()
        args = argparse.Namespace(agent_verb="list", json=False)
        with redirect_stdout(out):
            self.assertEqual(agentcli.cmd_agent(args), 0)
        self.assertIn(unit, out.getvalue())

    def test_resume_retires_the_old_entry_and_registers_the_new(self):
        unit, _ = self.start()
        self.system.finish(unit)
        with open(os.path.join(self.session("lib", 5), "chat.jsonl"), "w") as h:
            h.write("{}\n")
        resumed, _ = launch.resume(
            launch.target(self.root, "lib"),
            5,
            "keep going",
            hold_reason_fn=lambda tgt, number: None,
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
        )
        self.assertEqual(resumed, "ops-agentlib-5-r1")
        states = {entry["id"]: state for entry, state in self.visible()}
        self.assertEqual(states[unit], registry.FINISHED)
        self.assertEqual(states[resumed], registry.LIVE)

    def test_stop_closes_the_entry(self):
        unit, _ = self.start()
        stopped = launch.stop(
            launch.target(self.root, "lib"),
            5,
            list_fn=self.system.list_fn,
            stop_fn=self.system.stop_fn,
        )
        self.assertEqual(stopped, [unit])
        states = {entry["id"]: state for entry, state in self.visible()}
        self.assertEqual(states[unit], registry.FINISHED)

    def test_relaunch_after_stop_is_admitted(self):
        unit, _ = self.start()
        launch.stop(
            launch.target(self.root, "lib"),
            5,
            list_fn=self.system.list_fn,
            stop_fn=self.system.stop_fn,
        )
        self.system.finish(unit)
        relaunched, _ = self.start()
        self.assertEqual(relaunched, "ops-agentlib-5-r1")
        self.assertIn(relaunched, self.live_ids())


class SnapshotCapTest(PrefixOps):
    def snapshot_json(self, cwd):
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(cwd)
        try:
            with (
                mock.patch("corral.accounts.providers", return_value=[]),
                mock.patch("corral.quota_ledger.load", return_value={"readings": {}, "marks": {}}),
                redirect_stdout(out),
            ):
                self.assertEqual(cli.main(["snapshot", "--json"]), 0)
        finally:
            os.chdir(old)
        return json.loads(out.getvalue())

    def test_snapshot_from_the_ops_root_resolves_the_project_repo_cap(self):
        doc = self.snapshot_json(self.root)
        self.assertEqual(doc["capacity"]["repo_cap"], 1)

    def test_snapshot_from_the_clone_resolves_the_project_repo_cap(self):
        doc = self.snapshot_json(os.path.join(self.root, "clones", "lib"))
        self.assertEqual(doc["capacity"]["repo_cap"], 1)

    def test_a_launched_agent_counts_in_snapshot(self):
        unit, _ = self.start()
        doc = self.snapshot_json(self.root)
        self.assertEqual(doc["capacity"]["live"], 1)
        self.assertEqual(doc["capacity"]["by_project"], {"ops": 1})
        self.assertIn(unit, [row["id"] for row in doc["agents"]])


class RegisterBeforeStartTest(PrefixOps):
    """A registry failure starts no unit; a start failure closes the entry."""

    def visible(self):
        return registry.visible(time.time(), cfg=registry.config())

    def resume(self, number=5):
        return launch.resume(
            launch.target(self.root, "lib"),
            number,
            "keep going",
            hold_reason_fn=lambda tgt, number: None,
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
        )

    def finish_and_seed_session(self, unit, number=5):
        self.system.finish(unit)
        with open(os.path.join(self.session("lib", number), "chat.jsonl"), "w") as handle:
            handle.write("{}\n")

    def live_units(self):
        return [name for name, state in self.system.units.items() if state == "active"]

    def test_a_registry_failure_starts_no_unit(self):
        with mock.patch.object(registry, "register", side_effect=registry.RegistryError("down")):
            with self.assertRaises(launch.LaunchError):
                self.start()
        self.assertEqual(self.system.started, [])
        self.assertEqual(self.live_units(), [])
        self.assertEqual(registry.load_all(), [])

    def test_an_os_error_from_the_registry_starts_no_unit(self):
        with mock.patch.object(registry, "register", side_effect=OSError("disk gone")):
            with self.assertRaises(launch.LaunchError):
                self.start()
        self.assertEqual(self.system.started, [])
        self.assertEqual(self.live_units(), [])

    def test_a_failed_start_closes_the_registered_entry(self):
        self.system.fail = "boom"
        with self.assertRaises(units.UnitsError):
            self.start()
        self.assertEqual(self.live_units(), [])
        entries = registry.load_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            registry.status(entries[0], time.time(), registry.config()), registry.FINISHED
        )

    def test_a_resume_with_a_failing_registry_starts_no_unit(self):
        unit, _ = self.start()
        self.finish_and_seed_session(unit)
        with mock.patch.object(registry, "register", side_effect=registry.RegistryError("down")):
            with self.assertRaises(launch.LaunchError):
                self.resume()
        self.assertEqual(len(self.system.started), 1)
        self.assertEqual(self.live_units(), [])

    def test_a_resume_with_a_failing_start_closes_the_registered_entry(self):
        unit, _ = self.start()
        self.finish_and_seed_session(unit)
        self.system.fail = "boom"
        with self.assertRaises(units.UnitsError):
            self.resume()
        self.assertEqual(self.live_units(), [])
        states = {entry["id"]: state for entry, state in self.visible()}
        self.assertEqual(states[unit], registry.FINISHED)
        resumed = units.unit_name("ops-agent", "lib", 5, 1)
        self.assertEqual(states[resumed], registry.FINISHED)


class HeartbeatForEveryPromptTest(PrefixOps):
    """Every prompt a launch starts carries the heartbeat instructions."""

    def launched_text(self, prompt, number=5, repo="lib"):
        launch.launch(
            launch.target(self.root, repo),
            number,
            prompt,
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
        )
        with open(os.path.join(self.session(repo, number), "prompt.md"), encoding="utf-8") as h:
            return h.read()

    def test_no_builtin_carries_its_own_heartbeat_copy(self):
        for role in prompts.ROLES:
            with self.subTest(role=role):
                self.assertNotIn("corral agent heartbeat", prompts._builtin_text(role))

    def test_the_worker_prompt_carries_the_heartbeat(self):
        text = self.launched_text(prompts.render("worker", dict(FULL), ops_root=self.root))
        self.assertIn("corral agent heartbeat", text)
        self.assertIn("corral agent done", text)
        self.assertIn("CORRAL_AGENT_ID", text)

    def test_the_reviewer_prompt_carries_the_heartbeat(self):
        text = self.launched_text(
            prompts.render("reviewer", dict(FULL), ops_root=self.root),
            number=6,
            repo="my-repo",
        )
        self.assertIn("corral agent heartbeat", text)
        self.assertIn("corral agent done", text)
        self.assertIn("CORRAL_AGENT_ID", text)

    def test_the_triage_prompt_carries_the_heartbeat(self):
        text = self.launched_text(
            prompts.render("triage", dict(FULL), ops_root=self.root),
            number=7,
            repo="my-repo",
        )
        self.assertIn("corral agent heartbeat", text)
        self.assertIn("corral agent done", text)
        self.assertIn("CORRAL_AGENT_ID", text)

    def test_a_prompt_file_carries_the_heartbeat(self):
        text = self.launched_text("do the thing")
        self.assertIn("corral agent heartbeat", text)
        self.assertIn("corral agent done", text)
        self.assertIn("CORRAL_AGENT_ID", text)


class StopClosesEveryEntryTest(PrefixOps):
    def test_stop_continues_through_a_failing_close(self):
        unit, _ = self.start()
        second = units.unit_name("ops-agent", "lib", 5, 1)
        registry.register(second, "omp", ticket=registry.make_ticket("lib", 5, "ops"))
        self.system.units[second] = "active"
        with open(os.path.join(self.session("lib", 5), "units.log"), "a", encoding="utf-8") as h:
            h.write(second + "\n")
        calls = []
        real_done = registry.done

        def flaky(agent_id, **kwargs):
            calls.append(agent_id)
            if len(calls) == 1:
                raise OSError("disk gone")
            return real_done(agent_id, **kwargs)

        with mock.patch.object(registry, "done", side_effect=flaky):
            with self.assertRaises(launch.LaunchError):
                launch.stop(
                    launch.target(self.root, "lib"),
                    5,
                    list_fn=self.system.list_fn,
                    stop_fn=self.system.stop_fn,
                )
        self.assertEqual(calls, [unit, second])
        self.assertIn(unit, self.system.stopped)
        self.assertIn(second, self.system.stopped)
        states = {entry["id"]: state for entry, state in registry.visible(time.time())}
        self.assertEqual(states[unit], registry.LIVE)
        self.assertEqual(states[second], registry.FINISHED)


class SnapshotOutsideTreeTest(PrefixOps):
    """Snapshot from a clone outside the ops-root tree still resolves the cap."""

    def make_outside_clone(self, name):
        origin = os.path.join(self.tmp, f"{name}.git")
        seed = os.path.join(self.tmp, f"seed-{name}")
        git_in(self.tmp, "init", "-q", "--bare", "-b", "main", origin)
        git_in(self.tmp, "init", "-q", "-b", "main", seed)
        git_in(seed, "commit", "-q", "--allow-empty", "-m", "one")
        git_in(seed, "remote", "add", "origin", origin)
        outside = os.path.join(self.tmp, f"{name}-clone")
        git_in(self.tmp, "clone", "-q", origin, outside)
        with open(os.path.join(self.root, settings.PROJECT_FILE), "a", encoding="utf-8") as h:
            h.write(f'\n[[repo]]\nname = "{name}"\nclone = {{ path = "{outside}" }}\n')
        return outside

    def snapshot_json(self, cwd):
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(cwd)
        try:
            with (
                mock.patch("corral.accounts.providers", return_value=[]),
                mock.patch("corral.quota_ledger.load", return_value={"readings": {}, "marks": {}}),
                redirect_stdout(out),
            ):
                self.assertEqual(cli.main(["snapshot", "--json"]), 0)
        finally:
            os.chdir(old)
        return json.loads(out.getvalue())

    def test_snapshot_from_a_clone_outside_the_ops_root(self):
        outside = self.make_outside_clone("wide")
        doc = self.snapshot_json(outside)
        self.assertEqual(doc["capacity"]["repo_cap"], 1)

    def test_snapshot_from_below_a_clone_outside_the_ops_root(self):
        outside = self.make_outside_clone("wide")
        sub = os.path.join(outside, "sub")
        os.makedirs(sub)
        doc = self.snapshot_json(sub)
        self.assertEqual(doc["capacity"]["repo_cap"], 1)


if __name__ == "__main__":
    unittest.main()
