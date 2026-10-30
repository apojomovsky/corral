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


class WorkerHeartbeatTest(PrefixOps):
    def test_the_worker_prompt_names_the_heartbeat(self):
        text = prompts.render("worker", dict(FULL), ops_root=self.root)
        self.assertIn("corral agent heartbeat", text)
        self.assertIn("corral agent done", text)
        self.assertIn("CORRAL_AGENT_ID", text)


if __name__ == "__main__":
    unittest.main()
