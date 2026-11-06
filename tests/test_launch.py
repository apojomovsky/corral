"""korapy.launch and its CLI, end to end over fake seams.

A fake project: a temp ops root with a korapy.toml holding several [[repo]]
entries, a bare repository per repo standing in for origin, and a clone.
Real git runs in those temp dirs (local remotes only). The service manager
is an in-memory fake: `list_fn` lists it, `run` starts a unit in it, and
`stop_fn` stops one. No test runs systemd, the runtimes, or touches the
real state, config or session dirs; the environment is cleared of KORAPY_*
and points at temp dirs.
"""

import argparse
import copy
import dataclasses
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from contextlib import redirect_stdout

from korapy import accounts, board, cli, hold, host_ledger, launch, launchcli, models, paths
from korapy import pressure, profiles, registry, resources, runtimes
from korapy import settings, units
from korapy.runtimes import muse, omp
from tests.test_profiles_launch import FakeStore
from tests.test_reviewrecord import git_in
from tests.test_tracker import FakeTracker

REPOS = ("lib", "my-repo")

TOML = """\
[agents]
cap = 2

[profile]
default = "default"

[profiles.default]
runtime = "omp"

[[repo]]
name = "lib"
clone = { path = "clones/lib" }

[[repo]]
name = "my-repo"
clone = { path = "clones/my-repo" }

[[repo]]
name = "no-clone"

[[repo]]
name = "ghost"
clone = { path = "clones/ghost" }
"""


def _free_hold(tgt, number):
    """A `hold_reason_fn` fake: the ticket is free to resume."""
    return None


def roomy(path):
    """A `usage` fake with acres free: the disk gate never fires."""
    return (100 * 1024**3, 10 * 1024**3, 90 * 1024**3)


class FakeSystem:
    """An in-memory service manager: units, starts and stops, nothing real."""

    def __init__(self):
        self.units = {}
        self.started = []
        self.stopped = []
        self.fail = None
        self.ledger = {"version": 1, "host_budget": None, "projects": {}, "foreign": []}
        self.ledger_error = None

    def list_fn(self, prefix):
        return [(name, state, "running") for name, state in self.units.items()]

    def run(self, argv, timeout=None):
        self.started.append(list(argv))
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.fail)
        if argv[:3] == ["systemctl", "--user", "start"]:
            self.units[argv[3]] = "active"
        elif argv[:3] == ["systemctl", "--user", "set-property"]:
            pass
        else:
            unit = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--unit="))
            self.units[unit] = "active"
        return subprocess.CompletedProcess(argv, 0, "", "")

    def stop_fn(self, unit):
        self.stopped.append(unit)
        self.units[unit] = "inactive"

    def finish(self, unit):
        """The unit ended on its own: no longer live, still in the log."""
        self.units[unit] = "inactive"

    def host_ledger_fn(self):
        """The ensured host ledger: a copy of `ledger`, or `ledger_error`."""
        if self.ledger_error is not None:
            raise self.ledger_error
        return copy.deepcopy(self.ledger)

    def seams(self):
        return launchcli.Seams(
            list_fn=self.list_fn,
            run=self.run,
            stop_fn=self.stop_fn,
            host_ledger_fn=self.host_ledger_fn,
            labels_fn=lambda kind, repo, number: [],
            usage=roomy,
            controllers_read=lambda path: "cpu memory pids",
            tracker=FakeTracker([]),
        )


class OpsCase(unittest.TestCase):
    """A temp ops root with two real clones, a fake service manager, and temp dirs."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-launch-"))
        self.root = os.path.join(self.tmp, "ops")
        os.makedirs(os.path.join(self.root, "clones"))
        for name in REPOS:
            self.make_clone(name)
        with open(os.path.join(self.root, settings.PROJECT_FILE), "w", encoding="utf-8") as h:
            h.write(TOML)
        kept = {k: v for k, v in os.environ.items() if not k.startswith("KORAPY_")}
        env = dict(kept)
        env.update(
            KORAPY_STATE=os.path.join(self.tmp, "state"),
            KORAPY_SESSIONS=os.path.join(self.tmp, "sessions"),
            KORAPY_CONFIG=os.path.join(self.tmp, "config"),
            KORAPY_CACHE=os.path.join(self.tmp, "cache"),
        )
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.enterContext(mock.patch.object(omp, "binary", return_value="/fake/omp"))
        self.enterContext(mock.patch.object(muse, "binary", return_value="/fake/muse"))
        self.system = FakeSystem()

    def make_clone(self, name):
        origin = os.path.join(self.tmp, f"{name}.git")
        seed = os.path.join(self.tmp, f"seed-{name}")
        git_in(self.tmp, "init", "-q", "--bare", "-b", "main", origin)
        git_in(self.tmp, "init", "-q", "-b", "main", seed)
        git_in(seed, "commit", "-q", "--allow-empty", "-m", "one")
        git_in(seed, "remote", "add", "origin", origin)
        git_in(seed, "push", "-q", "origin", "main")
        git_in(self.tmp, "clone", "-q", origin, os.path.join(self.root, "clones", name))

    def target(self, repo="lib"):
        return launch.target(self.root, repo)

    def prompt_file(self, text="do the thing"):
        path = os.path.join(self.tmp, "prompt.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def session(self, repo, number):
        return paths.ticket_session_dir(repo, number)

    def leaf(self, repo, number, role="worker"):
        """The ticket's newest session leaf of `role`; None when never launched."""
        return paths.newest_session(repo, number, role)

    def worktrees_of(self, repo):
        clone = os.path.join(self.root, "clones", repo)
        return (
            sorted(name for name in os.listdir(os.path.join(clone, ".worktrees")))
            if os.path.isdir(os.path.join(clone, ".worktrees"))
            else []
        )


class TargetTest(OpsCase):
    def test_resolves_the_clone_and_the_launch_settings(self):
        tgt = self.target("lib")
        self.assertEqual(tgt.clone, os.path.join(self.root, "clones", "lib"))
        self.assertEqual(tgt.default_branch, "main")
        self.assertEqual(tgt.prefix, "korapy-agent-")
        self.assertEqual(tgt.cap, 2)
        self.assertEqual(tgt.branch_template, "{type}/{issue}-{slug}")
        self.assertEqual(tgt.worktrees_root, ".worktrees/")

    def test_a_repo_without_a_clone_path_is_a_configuration_error(self):
        with self.assertRaises(launch.LaunchError) as ctx:
            self.target("no-clone")
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_a_clone_path_that_is_not_a_git_clone_is_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, "not a git clone"):
            self.target("ghost")

    def test_an_absolute_clone_path_is_used_as_it_is(self):
        clone = os.path.join(self.root, "clones", "lib")
        with open(os.path.join(self.root, settings.PROJECT_FILE), "a", encoding="utf-8") as h:
            h.write(f'\n[[repo]]\nname = "abs"\nclone = {{ path = "{clone}" }}\n')
        self.assertEqual(self.target("abs").clone, clone)

    def test_an_unknown_repo_is_a_settings_error(self):
        with self.assertRaises(settings.SettingsError):
            self.target("nope")

    def test_a_bad_repo_name_is_refused(self):
        with self.assertRaises(launch.LaunchError):
            launch.target(self.root, "a/b")

    def test_the_clone_settings_and_the_provider_are_registered(self):
        self.assertEqual(settings.REGISTRY["clone.path"].default, "")
        self.assertEqual(settings.REGISTRY["clone.default_branch"].default, "main")
        self.assertIn("korapy.launch", settings.PROVIDERS)

    def test_the_runtime_limit_matches_the_runtime_default(self):
        self.assertEqual(runtimes.DEFAULT_MAX_TIME, "3h")
        self.assertEqual(launch.RUNTIME_MAX_S, 3 * 3600)


class AdmitTest(OpsCase):
    def test_a_free_host_admits_the_ticket(self):
        launch.admit(
            self.target(),
            5,
            list_fn=lambda prefix: [],
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )

    def test_a_live_unit_of_this_ticket_is_already_live(self):
        self.system.units["korapy-agent-lib-5"] = "active"
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(
                self.target(),
                5,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_the_same_number_in_another_repo_is_not_live(self):
        self.system.units["korapy-agent-my-repo-5"] = "active"
        launch.admit(
            self.target("lib"),
            5,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )

    def test_a_full_host_refuses_with_exit_75(self):
        self.system.units["korapy-agent-lib-6"] = "active"
        self.system.units["korapy-agent-my-repo-7"] = "activating"
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(
                self.target(),
                5,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(ctx.exception.exit_code, 75)

    def test_a_registered_worker_on_this_host_holds_a_slot(self):
        registry.register("worker-1", "omp", ticket=registry.make_ticket("my-repo", 9))
        tgt = dataclasses.replace(self.target(), cap=1)
        with self.assertRaises(launch.CapFull):
            launch.admit(
                tgt,
                5,
                list_fn=lambda prefix: [],
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )

    def test_an_unreachable_service_manager_is_exit_3(self):
        def down(prefix):
            raise units.Unreachable("no bus")

        with self.assertRaises(units.Unreachable) as ctx:
            launch.admit(
                self.target(),
                5,
                list_fn=down,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(ctx.exception.exit_code, 3)


HOST_BUDGET = "[resources.host]\ncpu_m = 8000\nmemory_mb = 16000\npids = 4096\n"

WORKER_PRICE = "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"

REVIEWER_PRICE = "[resources.role.reviewer]\ncpu_m = 500\nmemory_mb = 1024\n"

NOW = 1_000_000.0


class BudgetWiringTest(OpsCase):
    """The budget gate in `launch.admit` and the slice wiring in `launch`."""

    def host_config(self, text=HOST_BUDGET):
        config = os.environ["KORAPY_CONFIG"]
        os.makedirs(config, exist_ok=True)
        with open(os.path.join(config, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def register_web(self, share="weight = 1\n"):
        directory = os.path.join(os.environ["KORAPY_CONFIG"], "projects.d")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "web.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                'name = "web"\nunit_prefix = "korapy-agent-"\nroot = "'
                + os.path.realpath(self.root)
                + '"\n\n[resources]\n'
                + share
            )

    def admit(self, number=5, **kwargs):
        kwargs.setdefault("list_fn", self.system.list_fn)
        kwargs.setdefault("usage", roomy)
        kwargs.setdefault("host_ledger_fn", self.system.host_ledger_fn)
        kwargs.setdefault("controllers_read", lambda path: "cpu memory pids")
        return launch.admit(self.target(), number, **kwargs)

    def test_no_host_budget_passes_and_creates_no_slices(self):
        self.admit()
        unit, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.assertEqual(unit, "korapy-agent-lib-5")
        self.assertEqual(len(self.system.started), 1)
        self.assertFalse([a for a in self.system.started[0] if "Slice=" in a])

    def test_a_full_budget_refuses_with_75_before_anything_is_made(self):
        self.host_config(
            "[resources.host]\ncpu_m = 1000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        self.system.units["korapy-agent-lib-6"] = "active"
        with self.assertRaises(launch.CapFull) as caught:
            self.admit()
        self.assertEqual(caught.exception.exit_code, 75)
        self.assertIn("share", str(caught.exception))
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_disk_pressure_wins_over_the_budget(self):
        self.host_config(
            "[resources.host]\ncpu_m = 1000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        self.system.units["korapy-agent-lib-6"] = "active"

        def critical(path):
            return (100 * 1024**3, 99 * 1024**3, 1 * 1024**3)

        with self.assertRaises(launch.CapFull) as caught:
            self.admit(usage=critical)
        self.assertIn("disk", str(caught.exception))

    def test_the_count_gate_wins_over_the_budget(self):
        self.host_config(
            "[resources.host]\ncpu_m = 1000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        self.system.units["korapy-agent-lib-6"] = "active"
        self.system.units["korapy-agent-my-repo-7"] = "activating"
        with self.assertRaises(launch.CapFull) as caught:
            self.admit()
        self.assertIn("ceiling", str(caught.exception))
        self.assertNotIn("host full for", str(caught.exception))
        self.assertNotIn("share", str(caught.exception))

    def test_the_budget_gate_wins_over_quota(self):
        self.host_config(
            "[resources.host]\ncpu_m = 1000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        with open(os.path.join(self.root, settings.PROJECT_FILE), "w", encoding="utf-8") as handle:
            handle.write(TOML + "[profiles.default.models.worker]\nchain = ['fresh/model']\n")
        self.system.units["korapy-agent-lib-6"] = "active"
        pool = [accounts.Provider(name="fresh", reader="pkg:Reader", stale_after_s=3600.0)]
        quota_ledger = {
            "readings": {
                "fresh": {
                    "windows": {"hour": {"used_pct": 100, "resets_at": NOW + 3600}},
                    "read_at": NOW - 120,
                }
            },
            "marks": {},
        }
        with self.assertRaises(launch.CapFull):
            launch.launch(
                self.target("lib"),
                5,
                "do the thing",
                title="feat: add the thing",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                pool_fn=lambda: pool,
                ledger_fn=lambda: quota_ledger,
                now_fn=lambda: NOW,
                controllers_read=lambda path: "cpu memory pids",
            )
        self.assertEqual(self.system.started, [])

    def test_an_exhausted_quota_still_refuses_when_the_budget_is_roomy(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        with open(os.path.join(self.root, settings.PROJECT_FILE), "w", encoding="utf-8") as handle:
            handle.write(TOML + "[profiles.default.models.worker]\nchain = ['fresh/model']\n")
        pool = [accounts.Provider(name="fresh", reader="pkg:Reader", stale_after_s=3600.0)]
        quota_ledger = {
            "readings": {
                "fresh": {
                    "windows": {"hour": {"used_pct": 100, "resets_at": NOW + 3600}},
                    "read_at": NOW - 120,
                }
            },
            "marks": {},
        }
        with self.assertRaises(models.Exhausted) as caught:
            launch.launch(
                self.target("lib"),
                5,
                "do the thing",
                title="feat: add the thing",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                pool_fn=lambda: pool,
                ledger_fn=lambda: quota_ledger,
                now_fn=lambda: NOW,
                controllers_read=lambda path: "cpu memory pids",
            )
        self.assertEqual(caught.exception.exit_code, 75)
        self.assertEqual(self.system.started, [])

    def test_an_unknown_role_charge_is_a_configuration_error(self):
        self.host_config(HOST_BUDGET)
        with self.assertRaises(resources.LimitsError) as caught:
            self.admit(role="auditor")
        self.assertIn("auditor", str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 4)

    def test_an_empty_role_reaches_the_gate_as_unknown(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        with self.assertRaises(resources.LimitsError) as caught:
            self.admit(role="")
        self.assertIn("unknown role ''", str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 4)

    def test_an_unpriced_role_under_a_host_budget_is_refused_with_4(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        with self.assertRaisesRegex(launch.LaunchError, "reviewer") as caught:
            self.admit(role="reviewer")
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertIn("[resources.role.reviewer]", str(caught.exception))
        self.assertIn("config.toml", str(caught.exception))
        self.assertEqual(launchcli._exit_code(caught.exception), 4)

    def test_an_unpriced_launch_starts_nothing(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        with self.assertRaises(launch.LaunchError):
            launch.launch(
                self.target("lib"),
                5,
                "do the thing",
                title="feat: add the thing",
                role="reviewer",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                controllers_read=lambda path: "cpu memory pids",
            )
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_a_priced_reviewer_launches_with_its_own_charge(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE + REVIEWER_PRICE)
        self.register_web()
        unit, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            role="reviewer",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            controllers_read=lambda path: "cpu memory pids",
        )
        self.assertEqual(unit, "korapy-agent-lib-5")
        agent = self.system.started[-1]
        self.assertIn("--property=Slice=korapy-web.slice", agent)
        self.assertIn("--property=MemoryMax=1024M", agent)

    def test_no_host_budget_leaves_an_unpriced_role_untouched(self):
        unit, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            role="reviewer",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.assertEqual(unit, "korapy-agent-lib-5")
        self.assertEqual(len(self.system.started), 1)
        self.assertFalse([a for a in self.system.started[0] if "Slice=" in a])

    def test_a_reviewer_fits_where_a_worker_does_not(self):
        self.host_config(
            "[resources.host]\ncpu_m = 1500\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
            "[resources.role.reviewer]\ncpu_m = 500\nmemory_mb = 1024\n"
        )
        self.system.units["korapy-agent-lib-6"] = "active"
        with self.assertRaises(launch.CapFull):
            self.admit(role="worker")
        self.admit(role="reviewer")

    def test_an_expired_foreign_claim_frees_the_pool(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        self.system.ledger["foreign"] = [
            {"name": "desk", "cpu_m": 8000, "memory_mb": 16000, "claimed_at": NOW - 601.0}
        ]
        self.admit(now_fn=lambda: NOW)

    def test_a_live_foreign_claim_subtracts_from_the_pool(self):
        self.host_config(
            "[resources.host]\ncpu_m = 8000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        self.system.ledger["foreign"] = [
            {"name": "desk", "cpu_m": 7500, "memory_mb": 15000, "claimed_at": NOW}
        ]
        with self.assertRaises(launch.CapFull) as caught:
            self.admit(now_fn=lambda: NOW)
        self.assertEqual(caught.exception.exit_code, 75)

    def test_a_foreign_state_dir_is_exit_4(self):
        self.system.ledger_error = host_ledger.StateDirError("belongs elsewhere")
        with self.assertRaises(host_ledger.StateDirError) as caught:
            self.admit()
        self.assertEqual(caught.exception.exit_code, 4)

    def test_a_busy_ledger_lock_is_exit_75(self):
        self.system.ledger_error = host_ledger.LedgerLocked("busy")
        with self.assertRaises(host_ledger.LedgerLocked) as caught:
            self.admit()
        self.assertEqual(caught.exception.exit_code, 75)

    def test_a_corrupt_ledger_is_exit_4(self):
        self.system.ledger_error = host_ledger.LedgerError("corrupt file")
        with self.assertRaises(host_ledger.LedgerError) as caught:
            self.admit()
        self.assertEqual(caught.exception.exit_code, 4)

    def test_floors_above_the_host_name_the_file_and_key(self):
        self.host_config("[resources.host]\ncpu_m = 1000\nmemory_mb = 16000\npids = 4096\n")
        self.register_web("weight = 1\ncpu_floor_m = 5000\n")
        with self.assertRaisesRegex(settings.SettingsError, r"config\.toml.*cpu_m") as caught:
            self.admit()
        self.assertIn("[resources.host]", str(caught.exception))
        self.assertEqual(launchcli._exit_code(caught.exception), 4)

    def test_slices_are_created_parents_first_with_their_limits(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        self.register_web()
        unit, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            controllers_read=lambda path: "cpu memory pids",
        )
        self.assertEqual(unit, "korapy-agent-lib-5")
        self.assertEqual(len(self.system.started), 5)
        host_start, host_props, project_start, project_props, agent = self.system.started
        self.assertEqual(host_start, ["systemctl", "--user", "start", "korapy.slice"])
        self.assertEqual(
            host_props,
            [
                "systemctl",
                "--user",
                "set-property",
                "--runtime",
                "korapy.slice",
                "MemoryMax=16000M",
                "CPUQuota=800%",
            ],
        )
        self.assertEqual(project_start, ["systemctl", "--user", "start", "korapy-web.slice"])
        self.assertEqual(
            project_props,
            [
                "systemctl",
                "--user",
                "set-property",
                "--runtime",
                "korapy-web.slice",
                "MemoryMax=16000M",
                "CPUQuota=800%",
            ],
        )
        self.assertIn("--unit=korapy-agent-lib-5", agent)
        self.assertIn("--property=Slice=korapy-web.slice", agent)
        self.assertIn("--property=MemoryMax=2048M", agent)
        self.assertIn("--property=CPUQuota=100%", agent)

    def test_a_missing_required_controller_is_exit_4_before_anything_runs(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        self.register_web()
        with self.assertRaisesRegex(units.UnitsError, "memory") as caught:
            launch.launch(
                self.target("lib"),
                5,
                "do the thing",
                title="feat: add the thing",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                controllers_read=lambda path: "cpu pids",
            )
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_a_missing_required_controller_is_exit_4_in_admit(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        with self.assertRaisesRegex(units.UnitsError, "memory") as caught:
            self.admit(controllers_read=lambda path: "cpu pids")
        self.assertEqual(caught.exception.exit_code, 4)

    def test_a_resume_refuses_a_full_budget(self):
        self.host_config(
            "[resources.host]\ncpu_m = 8000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        unit, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            controllers_read=lambda path: "cpu memory pids",
        )
        self.system.finish(unit)
        with open(os.path.join(self.leaf("lib", 5), "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.system.units["korapy-agent-lib-6"] = "active"
        self.host_config(
            "[resources.host]\ncpu_m = 1000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        started = len(self.system.started)
        with self.assertRaises(launch.CapFull) as caught:
            launch.resume(
                self.target("lib"),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                controllers_read=lambda path: "cpu memory pids",
            )
        self.assertEqual(caught.exception.exit_code, 75)
        self.assertEqual(len(self.system.started), started)

    def test_the_budget_seam_is_required(self):
        with self.assertRaises(TypeError):
            launch.admit(  # ty: ignore[missing-argument]
                self.target(), 5, list_fn=lambda prefix: [], usage=roomy
            )
        with self.assertRaises(TypeError):
            launch.launch(  # ty: ignore[missing-argument]
                self.target(), 5, "x", list_fn=lambda prefix: [], run=self.system.run, usage=roomy
            )
        with self.assertRaises(TypeError):
            launch.resume(  # ty: ignore[missing-argument]
                self.target(),
                5,
                "x",
                hold_reason_fn=_free_hold,
                list_fn=lambda prefix: [],
                run=self.system.run,
                usage=roomy,
            )

    def test_a_full_budget_maps_to_exit_75_on_the_cli(self):
        self.host_config(
            "[resources.host]\ncpu_m = 1000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        self.system.units["korapy-agent-lib-6"] = "active"
        code = launchcli.run_verb(
            ["launch", "lib", "5", "--prompt-file", self.prompt_file(), "--root", self.root],
            self.system.seams(),
        )
        self.assertEqual(code, 75)

    def test_a_locked_ledger_maps_to_exit_75_on_the_cli(self):
        self.system.ledger_error = host_ledger.LedgerLocked("busy")
        code = launchcli.run_verb(
            ["launch", "lib", "5", "--prompt-file", self.prompt_file(), "--root", self.root],
            self.system.seams(),
        )
        self.assertEqual(code, 75)

    def test_a_corrupt_ledger_maps_to_exit_4_on_the_cli(self):
        self.system.ledger_error = host_ledger.LedgerError("corrupt file")
        code = launchcli.run_verb(
            ["launch", "lib", "5", "--prompt-file", self.prompt_file(), "--root", self.root],
            self.system.seams(),
        )
        self.assertEqual(code, 4)


class PressureWiringTest(OpsCase):
    """Saturation backoff in `launch.admit`: hot hosts wait with exit 75."""

    def host_config(self, text=HOST_BUDGET):
        config = os.environ["KORAPY_CONFIG"]
        os.makedirs(config, exist_ok=True)
        with open(os.path.join(config, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def admit(self, number=5, **kwargs):
        kwargs.setdefault("list_fn", self.system.list_fn)
        kwargs.setdefault("usage", roomy)
        kwargs.setdefault("host_ledger_fn", self.system.host_ledger_fn)
        kwargs.setdefault("controllers_read", lambda path: "cpu memory pids")
        return launch.admit(self.target(), number, **kwargs)

    def test_a_saturated_host_refuses_with_75_while_shares_are_free(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        hot = lambda: pressure.Reading(load_per_cpu=8.0, mem_available_pct=50.0)
        with self.assertRaises(launch.CapFull) as caught:
            self.admit(now_fn=lambda: NOW, pressure_fn=hot)
        self.assertEqual(caught.exception.exit_code, 75)
        self.assertIn("saturated", str(caught.exception))
        self.assertEqual(self.system.started, [])

    def test_an_unknown_reading_admits(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        unknown = lambda: pressure.Reading(load_per_cpu=None, mem_available_pct=None)
        self.admit(now_fn=lambda: NOW, pressure_fn=unknown)

    def test_no_seam_reads_as_unknown(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        self.admit(now_fn=lambda: NOW)

    def test_no_host_budget_skips_the_pressure_read(self):
        def explode():
            raise AssertionError("no budget configured: nothing may read pressure")

        self.admit(pressure_fn=explode)

    def test_a_saturated_host_maps_to_exit_75_on_the_cli(self):
        self.host_config(HOST_BUDGET + WORKER_PRICE)
        seams = self.system.seams()._replace(
            pressure_fn=lambda: pressure.Reading(load_per_cpu=8.0, mem_available_pct=50.0)
        )
        code = launchcli.run_verb(
            ["launch", "lib", "5", "--prompt-file", self.prompt_file(), "--root", self.root],
            seams,
        )
        self.assertEqual(code, 75)


class LaunchTest(OpsCase):
    def start(self, runtime, number=5, title="feat: add the thing", **kwargs):
        return launch.launch(
            self.target("lib"),
            number,
            kwargs.pop("prompt", "do the thing"),
            title=title,
            runtime=None if runtime == "omp" else runtime,
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=kwargs.pop("usage", roomy),
            host_ledger_fn=self.system.host_ledger_fn,
            **kwargs,
        )

    def check_full_path(self, runtime):
        unit, workdir = self.start(runtime, model="provider-a/model-1")
        self.assertEqual(unit, "korapy-agent-lib-5")
        self.assertEqual(
            workdir,
            os.path.realpath(
                os.path.join(self.root, "clones", "lib", ".worktrees", "5-add-the-thing")
            ),
        )
        self.assertTrue(os.path.isdir(workdir))

        session = self.leaf("lib", 5)
        with open(os.path.join(session, "prompt.md"), encoding="utf-8") as h:
            text = h.read()
        self.assertTrue(text.startswith("do the thing\n\n"))
        self.assertIn("korapy agent heartbeat", text)
        self.assertEqual(runtimes.recorded(session), runtime)
        self.assertEqual(units.started_units(session), [unit])

        argv = self.system.started[-1]
        self.assertIn(f"--unit={unit}", argv)
        self.assertIn(f"--setenv=KORAPY_AGENT_ID={unit}", argv)
        self.assertIn(f"--working-directory={workdir}", argv)
        self.assertTrue(any(item.startswith("do the thing\n\n") for item in argv))
        self.assertTrue(any("korapy agent heartbeat" in item for item in argv))
        return session

    def test_launch_resume_tail_and_stop_an_omp_agent(self):
        session = self.check_full_path("omp")
        self.assertTrue(os.path.exists(os.path.join(session, "overlay.yml")))
        self.assertIn("--model", self.system.started[-1])

        # The runtime wrote its session file: the handle resume reads, and the activity tail shows.
        with open(os.path.join(session, "chat.jsonl"), "w", encoding="utf-8") as h:
            h.write(
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "name": "bash",
                                    "intent": "run the tests",
                                    "arguments": {},
                                }
                            ],
                        }
                    }
                )
                + "\n"
            )

        # Resume refuses while the first generation is live.
        with self.assertRaises(units.UnitExists):
            launch.resume(
                self.target("lib"),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.system.finish("korapy-agent-lib-5")

        unit, _ = launch.resume(
            self.target("lib"),
            5,
            "keep going",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.assertEqual(unit, "korapy-agent-lib-5-r1")
        argv = self.system.started[-1]
        self.assertIn("-r", argv)
        self.assertEqual(argv[argv.index("-r") + 1], os.path.join(session, "chat.jsonl"))

        out = io.StringIO()
        launch.tail(self.target("lib"), 5, last=1, out=out)
        self.assertIn("run the tests", out.getvalue())

        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            ["korapy-agent-lib-5-r1"],
        )
        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            [],
        )

    def test_launch_resume_tail_and_stop_a_muse_agent(self):
        unit, workdir = self.start("muse", model="provider-a/model-1")
        self.assertEqual(unit, "korapy-agent-lib-5")
        self.assertTrue(os.path.isdir(workdir))
        session = self.leaf("lib", 5)
        self.assertTrue(os.path.exists(os.path.join(session, "muse.json")))
        self.assertEqual(runtimes.recorded(session), "muse")
        self.assertEqual(self.system.started[-1][0], "systemd-run")
        with open(os.path.join(session, "muse_session_id"), encoding="utf-8") as h:
            recorded = h.read().strip()
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--session-id") + 1], recorded)

        self.system.finish(unit)
        with open(os.path.join(session, "out.log"), "w", encoding="utf-8") as h:
            h.write(
                json.dumps(
                    {
                        "payload_type": "run.output.delta",
                        "payload": {"text": "run the tests"},
                        "sequence": 1,
                    }
                )
                + "\n"
            )

        resumed, _ = launch.resume(
            self.target("lib"),
            5,
            "keep going",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.assertEqual(resumed, "korapy-agent-lib-5-r1")
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--session-id") + 1], recorded)

        out = io.StringIO()
        launch.tail(self.target("lib"), 5, out=out)
        self.assertIn("run the tests", out.getvalue())

        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            ["korapy-agent-lib-5-r1"],
        )

    def test_the_runtime_config_is_written_into_the_session(self):
        self.start("muse", runtime_config={"model": "provider-a/model-1"})
        with open(os.path.join(self.leaf("lib", 5), "muse.json"), encoding="utf-8") as h:
            self.assertEqual(json.load(h), {"model": "provider-a/model-1"})

    def test_a_bad_model_in_the_runtime_config_is_a_configuration_error(self):
        with mock.patch.object(muse, "binary", return_value="/fake/muse"):
            with self.assertRaises(launch.LaunchError) as ctx:
                self.start("muse", runtime_config={"model": 123})
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(self.system.started, [])

    def test_a_full_host_refuses_before_anything_is_made(self):
        self.system.units["korapy-agent-my-repo-6"] = "active"
        self.system.units["korapy-agent-my-repo-7"] = "active"
        with self.assertRaises(launch.CapFull):
            self.start("omp")
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_an_already_live_ticket_is_exit_2_and_makes_nothing(self):
        self.system.units["korapy-agent-lib-5"] = "active"
        with self.assertRaises(units.UnitExists):
            self.start("omp")
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_a_bad_profile_beats_a_full_host(self):
        self.system.units["korapy-agent-my-repo-6"] = "active"
        self.system.units["korapy-agent-my-repo-7"] = "active"
        with self.assertRaises(settings.SettingsError) as caught:
            self.start("omp", profile="nope")
        self.assertIn("nope", str(caught.exception))
        self.assertEqual(launchcli._exit_code(caught.exception), 4)
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_a_bad_profile_beats_a_live_ticket(self):
        self.system.units["korapy-agent-lib-5"] = "active"
        with self.assertRaises(settings.SettingsError) as caught:
            self.start("omp", profile="nope")
        self.assertIn("nope", str(caught.exception))
        self.assertEqual(launchcli._exit_code(caught.exception), 4)
        self.assertEqual(self.system.started, [])
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_a_corrupt_store_with_no_profiles_still_launches(self):
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            text = handle.read()
        text = text.replace(
            '[profile]\ndefault = "default"\n\n[profiles.default]\nruntime = "omp"\n\n', ""
        )
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        path = board.db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"not a database")
        unit, workdir = self.start("omp")
        self.assertEqual(unit, "korapy-agent-lib-5")
        self.assertTrue(os.path.isdir(workdir))

    def test_no_profiles_launches_on_the_shipped_default_runtime(self):
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            text = handle.read()
        text = text.replace(
            '[profile]\ndefault = "default"\n\n[profiles.default]\nruntime = "omp"\n\n', ""
        )
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        unit, workdir = self.start("omp")
        self.assertEqual(unit, "korapy-agent-lib-5")
        self.assertTrue(os.path.isdir(workdir))
        session = self.leaf("lib", 5)
        self.assertEqual(runtimes.recorded(session), runtimes.DEFAULT_RUNTIME)
        recorded = profiles.recorded_profile(session)
        self.assertIsNotNone(recorded)
        assert recorded is not None
        self.assertEqual(recorded.name, profiles.DEFAULT_PROFILE_NAME)
        self.assertEqual(recorded.runtime, runtimes.DEFAULT_RUNTIME)

    def test_an_unreachable_service_manager_is_exit_3(self):
        def down(prefix):
            raise units.Unreachable("no bus")

        with self.assertRaises(units.Unreachable) as ctx:
            launch.launch(
                self.target(),
                5,
                "x",
                list_fn=down,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_a_start_that_loses_the_race_is_exit_2(self):
        self.system.fail = "Unit korapy-agent-lib-5.service already exists."
        with self.assertRaises(units.UnitExists):
            self.start("omp")
        self.assertFalse(os.path.exists(os.path.join(self.leaf("lib", 5), units.UNITS_LOG)))

    def test_bad_inputs_are_configuration_errors_before_anything_runs(self):
        for kwargs in (
            {"prompt": "-rf"},
            {"prompt": "   "},
            {"model": "-m"},
            {"runtime_config": ["not", "an", "object"]},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(launch.LaunchError) as ctx:
                    self.start("omp", **kwargs)
                self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(self.system.started, [])

    def test_an_unknown_runtime_is_a_configuration_error(self):
        with self.assertRaises(launch.LaunchError):
            launch.launch(
                self.target("lib"),
                5,
                "x",
                runtime="claude-code",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(self.system.started, [])

    def test_two_repos_keep_separate_sessions_and_units(self):
        self.start("omp", number=5)
        launch.launch(
            self.target("my-repo"),
            5,
            "other work",
            title="feat: other",
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.assertNotEqual(self.leaf("lib", 5), self.leaf("my-repo", 5))
        self.assertEqual(units.started_units(self.leaf("lib", 5)), ["korapy-agent-lib-5"])
        self.assertEqual(units.started_units(self.leaf("my-repo", 5)), ["korapy-agent-my-repo-5"])
        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            ["korapy-agent-lib-5"],
        )
        self.assertEqual(self.system.units["korapy-agent-my-repo-5"], "active")


class MuseSessionTest(OpsCase):
    """A muse launch records one uuid; the resume reuses it. Fakes only."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(muse, "binary", return_value="/fake/muse"))

    def test_launch_then_resume_uses_one_uuid(self):
        unit, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            runtime="muse",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.assertEqual(unit, "korapy-agent-lib-5")
        session = self.leaf("lib", 5)
        with open(os.path.join(session, "muse_session_id"), encoding="utf-8") as h:
            recorded = h.read().strip()
        self.assertEqual(str(uuid.UUID(recorded, version=4)), recorded)
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--session-id") + 1], recorded)

        self.system.finish(unit)
        with open(os.path.join(session, "out.log"), "w", encoding="utf-8") as h:
            h.write("{}\n")
        resumed, _ = launch.resume(
            self.target("lib"),
            5,
            "keep going",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.assertEqual(resumed, "korapy-agent-lib-5-r1")
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--session-id") + 1], recorded)
        self.assertEqual(argv[argv.index("--prompt-file") + 1], os.path.join(session, "message.md"))
        with open(os.path.join(session, "message.md"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "keep going")

    def test_resume_without_a_recorded_id_is_refused(self):
        unit, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            runtime="muse",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.system.finish(unit)
        os.remove(os.path.join(self.leaf("lib", 5), "muse_session_id"))
        with self.assertRaisesRegex(launch.LaunchError, "no muse session to resume"):
            launch.resume(
                self.target("lib"),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )
        self.assertEqual(len(self.system.started), 1)

    def test_a_failed_start_removes_only_the_recorded_session_id(self):
        self.system.fail = "Failed to connect to bus: no medium found"
        with self.assertRaises(units.Unreachable) as ctx:
            launch.launch(
                self.target("lib"),
                5,
                "do the thing",
                title="feat: add the thing",
                runtime="muse",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(ctx.exception.exit_code, 3)
        session = self.leaf("lib", 5)
        self.assertFalse(os.path.exists(os.path.join(session, "muse_session_id")))
        self.assertTrue(os.path.exists(os.path.join(session, "prompt.md")))
        self.assertTrue(os.path.exists(os.path.join(session, "muse.json")))
        self.assertEqual(runtimes.recorded(session), "muse")
        with self.assertRaisesRegex(launch.LaunchError, "no muse session to resume"):
            launch.resume(
                self.target("lib"),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )

    def test_a_failed_profile_record_drops_the_fresh_session_id(self):
        with open(os.path.join(self.root, settings.PROJECT_FILE), "a", encoding="utf-8") as h:
            h.write('[profiles.fast]\nruntime = "muse"\n')
        with mock.patch.object(
            profiles, "record_profile", side_effect=profiles.ProfileError("nope")
        ):
            with self.assertRaisesRegex(launch.LaunchError, "profile record"):
                launch.launch(
                    self.target("lib"),
                    5,
                    "do the thing",
                    title="feat: add the thing",
                    profile="fast",
                    runtime="muse",
                    list_fn=self.system.list_fn,
                    run=self.system.run,
                    usage=roomy,
                    host_ledger_fn=self.system.host_ledger_fn,
                    pool_fn=lambda: [],
                    ledger_fn=lambda: {"readings": {}, "marks": {}},
                    store=FakeStore(),
                    tracker=None,
                )
        session = self.leaf("lib", 5)
        self.assertFalse(os.path.exists(os.path.join(session, "muse_session_id")))
        self.assertTrue(os.path.exists(os.path.join(session, "prompt.md")))
        self.assertTrue(os.path.exists(os.path.join(session, "muse.json")))
        self.assertEqual(self.system.started, [])
        with self.assertRaisesRegex(launch.LaunchError, "no muse session to resume"):
            launch.resume(
                self.target("lib"),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )

    def test_tail_without_an_out_log_is_refused(self):
        launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            runtime="muse",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        with self.assertRaises(launch.LaunchError):
            launch.tail(self.target("lib"), 5, out=io.StringIO())


class FakeAdapter:
    """A runtime adapter fake: records the `argv` keywords, runs nothing."""

    name = "omp"

    def __init__(self):
        self.calls = []

    def argv(
        self,
        session_dir,
        prompt,
        *,
        model=None,
        resume=None,
        max_time=runtimes.DEFAULT_MAX_TIME,
        reviewer=False,
        session_id=None,
    ):
        self.calls.append(
            {
                "session_dir": session_dir,
                "model": model,
                "resume": resume,
                "reviewer": reviewer,
                "session_id": session_id,
            }
        )
        return ["fake-runtime", prompt]

    def env(self, session_dir):
        return {}

    def write_config(self, session_dir, config):
        pass

    def with_models(self, config, chains):
        return dict(config or {})

    def resume_handle(self, session_dir):
        return None

    def activity_file(self, session_dir):
        return None

    def options_schema(self):
        return {}


class ReviewerWiringTest(OpsCase):
    """`--role reviewer` reaches the runtime argv as `reviewer=True`. Fakes only."""

    def start(self, role, number=5, prompt="do the thing"):
        fake = FakeAdapter()
        with mock.patch.object(runtimes, "get", return_value=fake):
            unit, _ = launch.launch(
                self.target("lib"),
                number,
                prompt,
                title="feat: add the thing",
                role=role,
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        return fake, unit

    def test_the_reviewer_role_reaches_argv_and_other_roles_do_not(self):
        for role, want in (("reviewer", True), ("worker", False), ("triage", False)):
            with self.subTest(role=role):
                fake, unit = self.start(role, number=5)
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(fake.calls[0]["reviewer"], want)
                # The generation ends: the next launch closes its entry
                # and the host cap never fills across subtests.
                self.system.finish(unit)

    def test_no_role_means_no_reviewer(self):
        fake, _ = self.start(None)
        self.assertEqual(fake.calls[0]["reviewer"], False)

    def test_an_unknown_role_is_a_configuration_error(self):
        with self.assertRaises(launch.LaunchError) as ctx:
            self.start("auditor")
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(self.system.started, [])

    def test_the_role_is_recorded_for_the_resume_and_the_registry(self):
        fake, unit = self.start("reviewer")
        session = self.leaf("lib", 5, "reviewer")
        with open(os.path.join(session, "role"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "reviewer\n")
        (found,) = [e for e in registry.load_all() if e["id"] == unit]
        self.assertEqual(found["role"], "reviewer")

    def test_a_launch_without_a_role_records_no_role(self):
        _, unit = self.start(None)
        self.assertFalse(os.path.exists(os.path.join(self.leaf("lib", 5), "role")))
        (found,) = [e for e in registry.load_all() if e["id"] == unit]
        self.assertIsNone(found["role"])

    def test_the_cli_role_flag_records_the_role_in_the_registry(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb(
                [
                    "launch",
                    "lib",
                    "5",
                    "--role",
                    "reviewer",
                    "--title",
                    "feat: add the thing",
                    "--root",
                    self.root,
                ],
                self.system.seams(),
            )
        self.assertEqual(code, 0)
        (found,) = [e for e in registry.load_all() if e["id"] == "korapy-agent-lib-5"]
        self.assertEqual(found["role"], "reviewer")

    def test_resume_with_an_unknown_recorded_role_is_refused(self):
        _, unit = self.start("worker")
        self.system.finish(unit)
        with open(os.path.join(self.leaf("lib", 5), "role"), "w", encoding="utf-8") as handle:
            handle.write("auditor\n")
        before = len(self.system.started)
        with self.assertRaisesRegex(launch.LaunchError, "unknown role"):
            launch.resume(
                self.target("lib"),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )
        self.assertEqual(len(self.system.started), before)

    def test_resume_with_an_unreadable_role_record_is_refused(self):
        _, unit = self.start(None)
        self.system.finish(unit)
        os.mkdir(os.path.join(self.leaf("lib", 5), "role"))
        before = len(self.system.started)
        with self.assertRaisesRegex(launch.LaunchError, "cannot read the agent's role"):
            launch.resume(
                self.target("lib"),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )
        self.assertEqual(len(self.system.started), before)

    def test_a_relaunch_without_a_role_leaves_the_reviewer_record_alone(self):
        _, unit = self.start("reviewer")
        self.system.finish(unit)
        _, relaunched = self.start(None)
        with open(os.path.join(self.leaf("lib", 5, "reviewer"), "role"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "reviewer\n")
        self.assertFalse(os.path.exists(os.path.join(self.leaf("lib", 5), "role")))
        (found,) = [e for e in registry.load_all() if e["id"] == relaunched]
        self.assertIsNone(found["role"])

    def test_resume_uses_the_role_reread_under_the_spawn_lock(self):
        class ResumableAdapter(FakeAdapter):
            def resume_handle(self, session_dir):
                return "handle-1"

        fake = ResumableAdapter()
        real_recorded_role = launch._recorded_role
        reads = []

        def flipping(session):
            role = real_recorded_role(session)
            reads.append(role)
            if len(reads) == 1:
                paths.atomic_write_text(os.path.join(session, launch.ROLE_FILE), "reviewer\n")
            return role

        with mock.patch.object(runtimes, "get", return_value=fake):
            unit, _ = launch.launch(
                self.target("lib"),
                5,
                "do the thing",
                title="feat: add the thing",
                role="worker",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
            self.system.finish(unit)
            with mock.patch.object(launch, "_recorded_role", side_effect=flipping):
                resumed, _ = launch.resume(
                    self.target("lib"),
                    5,
                    "keep going",
                    hold_reason_fn=_free_hold,
                    list_fn=self.system.list_fn,
                    usage=roomy,
                    host_ledger_fn=self.system.host_ledger_fn,
                    run=self.system.run,
                )
        self.assertEqual(reads, ["worker", "reviewer"])
        self.assertTrue(fake.calls[-1]["reviewer"])
        (found,) = [e for e in registry.load_all() if e["id"] == resumed]
        self.assertEqual(found["role"], "reviewer")

    def test_resume_uses_the_model_reread_under_the_spawn_lock(self):
        class ResumableAdapter(FakeAdapter):
            def resume_handle(self, session_dir):
                return "handle-1"

        fake = ResumableAdapter()
        reads = []

        def flipping(session_dir):
            reads.append(session_dir)
            if len(reads) == 1:
                return profiles.RecordedProfile(
                    name="fast",
                    runtime="omp",
                    model="provider-a/gen-n",
                    layer=1,
                    layer_name="flag",
                    source="--profile fast",
                )
            return profiles.RecordedProfile(
                name="fast",
                runtime="omp",
                model="provider-a/gen-n-plus-1",
                layer=1,
                layer_name="flag",
                source="--profile fast",
            )

        with mock.patch.object(runtimes, "get", return_value=fake):
            unit, _ = launch.launch(
                self.target("lib"),
                5,
                "do the thing",
                title="feat: add the thing",
                role="worker",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
            self.system.finish(unit)
            with mock.patch.object(profiles, "recorded_profile", side_effect=flipping):
                resumed, _ = launch.resume(
                    self.target("lib"),
                    5,
                    "keep going",
                    hold_reason_fn=_free_hold,
                    list_fn=self.system.list_fn,
                    usage=roomy,
                    host_ledger_fn=self.system.host_ledger_fn,
                    run=self.system.run,
                )
        self.assertEqual(len(reads), 2)
        self.assertEqual(fake.calls[-1]["model"], "provider-a/gen-n-plus-1")
        self.assertEqual(fake.calls[-1]["resume"], "handle-1")
        (found,) = [e for e in registry.load_all() if e["id"] == resumed]
        self.assertEqual(found["model"], "provider-a/gen-n-plus-1")


class EnvRefusalTest(OpsCase):
    """An adapter environment refusal is a configuration error: exit 4, nothing started."""

    def test_an_env_value_error_refuses_the_launch(self):
        class RefusingAdapter(FakeAdapter):
            def env(self, session_dir):
                raise ValueError("cannot parse /s/options.json: want a JSON object")

        with mock.patch.object(runtimes, "get", return_value=RefusingAdapter()):
            with self.assertRaises(launch.LaunchError) as ctx:
                launch.launch(
                    self.target("lib"),
                    5,
                    "do the thing",
                    title="feat: add the thing",
                    list_fn=self.system.list_fn,
                    run=self.system.run,
                    usage=roomy,
                    host_ledger_fn=self.system.host_ledger_fn,
                )
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(self.system.started, [])


class MuseReviewerLaunchTest(OpsCase):
    """A reviewer muse launch carries `--disable-write`; a worker's does not."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(muse, "binary", return_value="/fake/muse"))

    def start(self, role, number=5):
        return launch.launch(
            self.target("lib"),
            number,
            "do the thing",
            title="feat: add the thing",
            role=role,
            runtime="muse",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )

    def test_reviewer_gets_disable_write_and_worker_does_not(self):
        self.start("reviewer")
        self.assertIn("--disable-write", self.system.started[-1])
        self.start("worker", number=6)
        self.assertNotIn("--disable-write", self.system.started[-1])

    def test_a_resume_keeps_the_recorded_reviewer_mode(self):
        unit, _ = self.start("reviewer")
        self.system.finish(unit)
        with open(
            os.path.join(self.leaf("lib", 5, "reviewer"), "out.log"), "w", encoding="utf-8"
        ) as h:
            h.write("{}\n")
        resumed, _ = launch.resume(
            self.target("lib"),
            5,
            "keep going",
            role="reviewer",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.assertEqual(resumed, "korapy-agent-lib-5-r1")
        self.assertIn("--disable-write", self.system.started[-1])
        (found,) = [e for e in registry.load_all() if e["id"] == resumed]
        self.assertEqual(found["role"], "reviewer")

    def test_a_worker_resume_stays_writable(self):
        unit, _ = self.start("worker", number=6)
        self.system.finish(unit)
        with open(os.path.join(self.leaf("lib", 6), "out.log"), "w", encoding="utf-8") as h:
            h.write("{}\n")
        launch.resume(
            self.target("lib"),
            6,
            "keep going",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.assertNotIn("--disable-write", self.system.started[-1])


class ResumeTailTest(OpsCase):
    def test_resume_without_a_session_is_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, "no omp session to resume"):
            launch.resume(
                self.target(),
                5,
                "keep going",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )
        self.assertEqual(self.system.started, [])

    def test_tail_without_activity_is_refused(self):
        with self.assertRaises(launch.LaunchError):
            launch.tail(self.target(), 5, out=io.StringIO())

    def test_resume_with_a_bad_message_is_refused(self):
        with self.assertRaises(launch.LaunchError):
            launch.resume(
                self.target(),
                5,
                "--oops",
                hold_reason_fn=_free_hold,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )

    def test_a_resume_whose_argv_fails_is_a_configuration_error(self):
        launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.system.finish("korapy-agent-lib-5")
        before = len(self.system.started)
        stub = mock.Mock()
        stub.name = "stub"
        stub.resume_handle.return_value = "handle-1"
        stub.argv.side_effect = ValueError("cannot parse /tmp/s/muse.json: bad")
        with mock.patch.object(runtimes, "get", return_value=stub):
            with self.assertRaises(launch.LaunchError) as ctx:
                launch.resume(
                    self.target(),
                    5,
                    "keep going",
                    hold_reason_fn=_free_hold,
                    list_fn=self.system.list_fn,
                    usage=roomy,
                    host_ledger_fn=self.system.host_ledger_fn,
                    run=self.system.run,
                )
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(len(self.system.started), before)


class HoldResumeTest(OpsCase):
    """A resume refuses a held ticket before the spawn lock or any worktree. Fakes only."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, settings.PROJECT_FILE), "a", encoding="utf-8") as h:
            h.write(
                '\n[[repo]]\nname = "held"\nclone = { path = "clones/lib" }\n'
                'tracker.repo = "example/lib"\n'
                '\n[[repo]]\nname = "paused"\nclone = { path = "clones/lib" }\n'
                'tracker.repo = "example/paused"\nboard.hold_label = "paused"\n'
            )

    def verb(self, repo, number, labels_fn):
        seams = self.system.seams()._replace(labels_fn=labels_fn)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb(
                ["resume", repo, str(number), "--message", "go", "--root", self.root], seams
            )
        return code, out.getvalue(), err.getvalue()

    def worktrees_root(self):
        return os.path.join(self.root, "clones", "lib", ".worktrees")

    def test_held_is_a_gate_error(self):
        self.assertTrue(issubclass(launch.HeldError, launch.LaunchError))
        self.assertEqual(launch.HeldError("held").exit_code, 1)
        self.assertIn(launch.HeldError, launchcli.KNOWN)

    def test_a_held_ticket_refuses_with_exit_1_and_starts_no_unit(self):
        labels = lambda kind, repo, number: ["hold"]  # noqa: E731
        code, _, err = self.verb("held", 5, labels)
        self.assertEqual(code, 1)
        self.assertIn(
            hold.reason(labels, ("example/lib", 5), (), hold_label="hold"),
            err,
        )
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.worktrees_root()))

    def test_labels_without_the_hold_resume_as_today(self):
        launch.launch(
            self.target("held"),
            5,
            "go",
            title="feat: go",
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.system.finish("korapy-agent-held-5")
        with open(os.path.join(self.leaf("held", 5), "chat.jsonl"), "w", encoding="utf-8") as h:
            h.write("{}\n")
        code, out, _ = self.verb("held", 5, lambda kind, repo, number: [])
        self.assertEqual(code, 0)
        self.assertIn("resumed as korapy-agent-held-5-r1", out)

    def test_a_label_read_that_raises_is_exit_1(self):
        def boom(kind, repo, number):
            raise RuntimeError("flaky")

        code, _, err = self.verb("held", 5, boom)
        self.assertEqual(code, 1)
        self.assertIn("unreadable", err)
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.worktrees_root()))

    def test_a_missing_label_seam_never_resumes(self):
        code, _, err = self.verb("held", 5, None)
        self.assertEqual(code, 1)
        self.assertIn("unreadable", err)
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.worktrees_root()))

    def test_a_custom_hold_label_is_honoured(self):
        code, _, err = self.verb("paused", 5, lambda kind, repo, number: ["paused"])
        self.assertEqual(code, 1)
        self.assertIn("paused", err)
        self.assertEqual(self.system.started, [])
        # The default label does not hold a ticket with a custom one:
        # the resume passes the gate and fails on the missing session.
        code, _, _ = self.verb("paused", 5, lambda kind, repo, number: ["hold"])
        self.assertEqual(code, 4)

    def test_resume_without_a_hold_reason_is_a_type_error(self):
        with self.assertRaises(TypeError):
            launch.resume(  # ty: ignore[missing-argument]
                self.target(),
                5,
                "go",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )


class StopTest(OpsCase):
    def test_stop_with_nothing_running_stops_nothing(self):
        self.assertEqual(
            launch.stop(self.target(), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn),
            [],
        )
        self.assertEqual(self.system.stopped, [])

    def test_stop_requires_both_seams(self):
        with self.assertRaises(TypeError):
            launch.stop(self.target(), 5, list_fn=self.system.list_fn)  # ty: ignore[missing-argument]
        with self.assertRaises(TypeError):
            launch.stop(self.target(), 5, stop_fn=self.system.stop_fn)  # ty: ignore[missing-argument]


class CliTest(OpsCase):
    def verb(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb([*argv, "--root", self.root], self.system.seams())
        return code, out.getvalue(), err.getvalue()

    def test_resume_and_tail_default_to_the_worker_role(self):
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            text = handle.read()
        text = text.replace(
            'name = "lib"\nclone = { path = "clones/lib" }',
            'name = "lib"\nclone = { path = "clones/lib" }\ntracker.repo = "example/lib"',
        )
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        worker, _ = launch.launch(
            self.target("lib"),
            5,
            "do the thing",
            title="feat: go",
            role="worker",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.system.finish(worker)
        reviewer, _ = launch.launch(
            self.target("lib"),
            5,
            "review it",
            title="feat: go",
            role="reviewer",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.system.finish(reviewer)
        for session, text in (
            (self.leaf("lib", 5, "worker"), "worker work"),
            (self.leaf("lib", 5, "reviewer"), "review notes"),
        ):
            with open(os.path.join(session, "chat.jsonl"), "w", encoding="utf-8") as h:
                h.write(
                    json.dumps(
                        {
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "name": "bash",
                                        "intent": text,
                                        "arguments": {},
                                    }
                                ],
                            }
                        }
                    )
                    + "\n"
                )
        code, out, _ = self.verb("resume", "lib", "5", "--message", "go on")
        self.assertEqual(code, 0)
        self.assertIn("resumed as korapy-agent-lib-5-r2", out)
        (found,) = [e for e in registry.load_all() if e["id"] == "korapy-agent-lib-5-r2"]
        self.assertEqual(found["role"], "worker")
        self.system.finish("korapy-agent-lib-5-r2")

        code, out, _ = self.verb("resume", "lib", "5", "--message", "go on", "--role", "reviewer")
        self.assertEqual(code, 0)
        self.assertIn("resumed as korapy-agent-lib-5-r3", out)

        code, out, _ = self.verb("tail", "lib", "5", "--last", "1")
        self.assertEqual(code, 0)
        self.assertIn("worker work", out)

        code, out, _ = self.verb("tail", "lib", "5", "--last", "1", "--role", "reviewer")
        self.assertEqual(code, 0)
        self.assertIn("review notes", out)

    def test_launch_stop_and_stop_again_map_to_exit_codes(self):
        pf = self.prompt_file()
        code, out, _ = self.verb("launch", "lib", "5", "--prompt-file", pf, "--title", "feat: go")
        self.assertEqual(code, 0)
        self.assertIn("launched korapy-agent-lib-5", out)

        code, out, _ = self.verb("stop", "lib", "5")
        self.assertEqual(code, 0)
        self.assertIn("stopped korapy-agent-lib-5", out)

        code, out, _ = self.verb("stop", "lib", "5")
        self.assertEqual(code, 1)
        self.assertIn("nothing to stop", out)

    def test_a_full_host_is_exit_75_and_a_live_ticket_is_exit_2(self):
        pf = self.prompt_file()
        self.system.units["korapy-agent-my-repo-6"] = "active"
        self.system.units["korapy-agent-my-repo-7"] = "active"
        code, _, err = self.verb("launch", "lib", "5", "--prompt-file", pf)
        self.assertEqual(code, 75)
        self.assertIn("at 2 of 2 ceiling", err)

        self.system.units.clear()
        self.system.units["korapy-agent-lib-5"] = "active"
        code, _, _ = self.verb("launch", "lib", "5", "--prompt-file", pf)
        self.assertEqual(code, 2)

    def test_an_unreachable_service_manager_is_exit_3(self):
        def down(prefix):
            raise units.Unreachable("no bus")

        self.enterContext(mock.patch.object(self.system, "list_fn", down))
        code, _, _ = self.verb("stop", "lib", "5")
        self.assertEqual(code, 3)

    def test_usage_and_configuration_errors_are_exit_4(self):
        pf = self.prompt_file()
        self.assertEqual(self.verb("launch", "nope", "5", "--prompt-file", pf)[0], 4)
        self.assertEqual(self.verb("resume", "lib", "5", "--message", "x")[0], 4)
        self.assertEqual(self.verb("tail", "lib", "5", "--last", "0")[0], 4)
        self.assertEqual(self.verb("launch", "lib", "5", "--prompt-file", "/no/such/file")[0], 4)
        self.assertEqual(self.verb("launch", "lib")[0], 4)

    def test_a_root_without_korapy_toml_is_exit_4(self):
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare)
        out, errs = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", errs):
            code = launchcli.run_verb(["stop", "lib", "5", "--root", bare], self.system.seams())
        self.assertEqual(code, 4)

    def test_a_runtime_config_must_be_a_json_object(self):
        pf = self.prompt_file()
        cfg = os.path.join(self.tmp, "cfg.json")
        with open(cfg, "w", encoding="utf-8") as h:
            h.write("[1]")
        self.assertEqual(
            self.verb("launch", "lib", "5", "--prompt-file", pf, "--runtime-config", cfg)[0], 4
        )

    def test_a_bad_per_launch_config_is_one_line_on_stderr_with_exit_4(self):
        pf = self.prompt_file()
        cfg = os.path.join(self.tmp, "cfg.json")
        with open(cfg, "w", encoding="utf-8") as h:
            h.write('{"model": 123}')
        with mock.patch.object(muse, "binary", return_value="/fake/muse"):
            code, _, err = self.verb(
                "launch",
                "lib",
                "5",
                "--prompt-file",
                pf,
                "--runtime",
                "muse",
                "--runtime-config",
                cfg,
            )
        self.assertEqual(code, 4)
        self.assertTrue(err.startswith("korapy: "))
        self.assertNotIn("\n", err.strip())
        self.assertEqual(self.system.started, [])

    def test_the_verbs_are_registered_on_the_korapy_command(self):
        with redirect_stdout(io.StringIO()):
            for verb in ("launch", "resume", "stop", "tail"):
                self.assertEqual(cli.main([verb, "--help"]), 0, verb)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class RaceTest(OpsCase):
    """The session is written only after the second admission, and generations follow the log."""

    def test_a_launch_that_loses_the_second_admit_leaves_the_live_session_alone(self):
        launch.launch(
            self.target("lib"),
            5,
            "first prompt",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.system.finish("korapy-agent-lib-5")
        session = self.leaf("lib", 5)
        before = {
            name: _read(os.path.join(session, name))
            for name in ("prompt.md", "runtime", "overlay.yml")
        }
        started = len(self.system.started)

        # The first admission sees nothing live; the second sees the ticket live.
        calls = []

        def flipping(prefix):
            calls.append(prefix)
            if len(calls) == 1:
                return []
            return [("korapy-agent-lib-5", "active", "running")]

        with self.assertRaises(units.UnitExists):
            launch.launch(
                self.target("lib"),
                5,
                "second prompt",
                runtime="muse",
                title="feat: add the thing",
                list_fn=flipping,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
                run=self.system.run,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(runtimes.recorded(session), "omp")
        for name, text in before.items():
            self.assertEqual(_read(os.path.join(session, name)), text, name)
        self.assertFalse(os.path.exists(os.path.join(session, "muse.json")))
        self.assertEqual(len(self.system.started), started)

    def test_two_sequential_resumes_take_generations_one_and_two(self):
        launch.launch(
            self.target("lib"),
            5,
            "go",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        session = self.leaf("lib", 5)
        self.system.finish("korapy-agent-lib-5")
        with open(os.path.join(session, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")

        first, _ = launch.resume(
            self.target("lib"),
            5,
            "again",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.system.finish(first)
        second, _ = launch.resume(
            self.target("lib"),
            5,
            "again",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.assertEqual(first, "korapy-agent-lib-5-r1")
        self.assertEqual(second, "korapy-agent-lib-5-r2")
        self.assertEqual(
            units.started_units(session),
            ["korapy-agent-lib-5", "korapy-agent-lib-5-r1", "korapy-agent-lib-5-r2"],
        )


class RegistryCapTest(OpsCase):
    """Admission counts registered agents under the target's own resolved settings."""

    def holders(self):
        registry.register("worker-1", "omp", ticket=registry.make_ticket("my-repo", 9))
        registry.register("worker-2", "omp", ticket=registry.make_ticket("lib", 9))

    def test_registered_workers_fill_the_cap_by_default(self):
        self.holders()
        with self.assertRaises(launch.CapFull):
            launch.admit(
                self.target(),
                5,
                list_fn=lambda prefix: [],
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )

    def test_count_toward_cap_false_in_the_ops_root_frees_the_slots(self):
        with open(os.path.join(self.root, settings.PROJECT_FILE), "a", encoding="utf-8") as handle:
            handle.write("\n[registry]\ncount_toward_cap = false\n")
        self.holders()
        tgt = self.target()
        launch.admit(
            tgt,
            5,
            list_fn=lambda prefix: [],
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )

    def test_a_registered_live_ticket_refuses_with_the_registry_note(self):
        registry.register("worker-1", "omp", ticket=registry.make_ticket("lib", 5))
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(
                self.target(),
                5,
                list_fn=lambda prefix: [],
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(
            str(ctx.exception),
            "#5 of lib already has a live agent (registered agent, counts until its heartbeat goes stale)",
        )

    def test_a_full_host_held_by_registered_agents_is_refused(self):
        self.holders()
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(
                self.target(),
                5,
                list_fn=lambda prefix: [],
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertEqual(
            str(ctx.exception),
            "start korapy-agent- lib#5 refused: korapy-agent- at 2 of 2 ceiling",
        )

    def test_a_unit_holder_gets_no_registry_note(self):
        self.system.units["korapy-agent-lib-5"] = "active"
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(
                self.target(),
                5,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertEqual(str(ctx.exception), "#5 of lib already has a live agent")

        self.system.units.clear()
        self.system.units["korapy-agent-my-repo-6"] = "active"
        self.system.units["korapy-agent-my-repo-7"] = "active"
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(
                self.target(),
                5,
                list_fn=self.system.list_fn,
                usage=roomy,
                host_ledger_fn=self.system.host_ledger_fn,
            )
        self.assertNotIn("registered", str(ctx.exception))


class StopUnprovenTest(OpsCase):
    """stop names a live unit the log does not prove ours, and exits 2 for it."""

    def test_an_unproven_live_unit_is_named_and_left_running(self):
        launch.launch(
            self.target(),
            5,
            "go",
            title="feat: go",
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.system.units["korapy-agent-lib-5-r7"] = "active"

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb(
                ["stop", "lib", "5", "--root", self.root], self.system.seams()
            )
        self.assertEqual(code, 2)
        self.assertIn("stopped korapy-agent-lib-5", out.getvalue())
        self.assertIn(
            "live but not proven ours, not stopped: korapy-agent-lib-5-r7", err.getvalue()
        )
        self.assertEqual(self.system.stopped, ["korapy-agent-lib-5"])
        self.assertEqual(self.system.units["korapy-agent-lib-5-r7"], "active")

    def test_only_an_unproven_unit_live_is_exit_2_with_nothing_stopped(self):
        self.system.units["korapy-agent-lib-5-r7"] = "active"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb(
                ["stop", "lib", "5", "--root", self.root], self.system.seams()
            )
        self.assertEqual(code, 2)
        self.assertEqual(self.system.stopped, [])
        self.assertIn("korapy-agent-lib-5-r7", err.getvalue())

    def start_two_roles(self):
        worker_unit, _ = launch.launch(
            self.target("lib"),
            5,
            "worker prompt",
            title="feat: worker",
            role="worker",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.system.finish(worker_unit)
        reviewer_unit, _ = launch.launch(
            self.target("lib"),
            5,
            "reviewer prompt",
            title="feat: reviewer",
            role="reviewer",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )
        self.system.units[worker_unit] = "active"
        return worker_unit, reviewer_unit

    def test_stop_reads_the_live_listing_once(self):
        tgt = self.target("lib")
        worker_unit, reviewer_unit = self.start_two_roles()
        calls = []

        def counting(prefix):
            calls.append(prefix)
            return self.system.list_fn(prefix)

        stopped = launch.stop(tgt, 5, list_fn=counting, stop_fn=self.system.stop_fn)
        self.assertEqual(sorted(stopped), sorted([worker_unit, reviewer_unit]))
        self.assertEqual(calls, [tgt.prefix])

    def test_stop_lets_an_unreachable_through_unwrapped(self):
        tgt = self.target("lib")
        self.start_two_roles()
        calls = []

        def counting(prefix):
            calls.append(prefix)
            return self.system.list_fn(prefix)

        seen = []

        def flaky_stop(unit):
            seen.append(unit)
            if len(seen) > 1:
                raise units.Unreachable("bus gone")
            self.system.stop_fn(unit)

        with self.assertRaises(units.Unreachable) as ctx:
            launch.stop(tgt, 5, list_fn=counting, stop_fn=flaky_stop)
        self.assertIs(type(ctx.exception), units.Unreachable)
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertEqual(calls, [tgt.prefix])

    def test_unproven_reads_the_live_listing_once(self):
        tgt = self.target("lib")
        self.start_two_roles()
        calls = []

        def counting(prefix):
            calls.append(prefix)
            return self.system.list_fn(prefix)

        self.assertEqual(launch.unproven(tgt, 5, list_fn=counting), [])
        self.assertEqual(calls, [tgt.prefix])


class ModelChainsFreshnessTest(unittest.TestCase):
    def test_chains_use_the_minimum_bound_of_their_own_providers(self):
        now = 1_800_000_000.0
        resolved = settings.resolve(project={}, env={})
        tgt = SimpleNamespace(resolved=resolved)
        pool = [
            accounts.Provider(name="fresh", reader="pkg:Reader", stale_after_s=3600.0),
            accounts.Provider(name="other", reader="pkg:Reader", stale_after_s=30.0),
        ]
        ledger = {
            "readings": {
                "fresh": {
                    "windows": {"hour": {"used_pct": 100, "resets_at": now + 3600}},
                    "read_at": now - 120,
                }
            },
            "marks": {},
        }
        with self.assertRaises(models.Exhausted):
            launch._model_chains(tgt, pool, ledger, now, {"worker": ["fresh/model"]})


class RoleSessionIsolationTest(OpsCase):
    """A reviewer launch must not overwrite the worker's session. Fakes only."""

    def start(self, role, number=5, prompt="do the thing"):
        return launch.launch(
            self.target("lib"),
            number,
            prompt,
            title="feat: add the thing",
            role=role,
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
        )

    def read(self, session, name):
        with open(os.path.join(session, name), encoding="utf-8") as handle:
            return handle.read()

    def test_a_reviewer_launch_keeps_the_worker_session(self):
        worker_unit, _ = self.start("worker", prompt="worker prompt")
        worker_session = paths.newest_session("lib", 5, "worker")
        self.assertIsNotNone(worker_session)
        self.system.finish(worker_unit)
        _, _ = self.start("reviewer", prompt="reviewer prompt")
        reviewer_session = paths.newest_session("lib", 5, "reviewer")
        self.assertIsNotNone(reviewer_session)
        self.assertNotEqual(worker_session, reviewer_session)
        self.assertEqual(self.read(worker_session, "prompt.md").splitlines()[0], "worker prompt")
        self.assertEqual(self.read(worker_session, "role"), "worker\n")
        self.assertEqual(self.read(reviewer_session, "role"), "reviewer\n")

    def test_resume_defaults_to_the_worker_session(self):
        worker_unit, _ = self.start("worker")
        self.system.finish(worker_unit)
        reviewer_unit, _ = self.start("reviewer")
        self.system.finish(reviewer_unit)
        with open(os.path.join(paths.newest_session("lib", 5, "worker"), "chat.jsonl"), "w") as h:
            h.write(json.dumps({"message": {"role": "assistant", "content": "worker work"}}) + "\n")
        resumed, _ = launch.resume(
            self.target("lib"),
            5,
            "keep going",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        (found,) = [e for e in registry.load_all() if e["id"] == resumed]
        self.assertEqual(found["role"], "worker")

    def test_resume_with_a_role_resumes_that_role(self):
        worker_unit, _ = self.start("worker")
        self.system.finish(worker_unit)
        reviewer_unit, _ = self.start("reviewer")
        self.system.finish(reviewer_unit)
        with open(os.path.join(paths.newest_session("lib", 5, "reviewer"), "chat.jsonl"), "w") as h:
            h.write(
                json.dumps({"message": {"role": "assistant", "content": "review notes"}}) + "\n"
            )
        resumed, _ = launch.resume(
            self.target("lib"),
            5,
            "keep going",
            role="reviewer",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        (found,) = [e for e in registry.load_all() if e["id"] == resumed]
        self.assertEqual(found["role"], "reviewer")

    def test_unit_names_stay_unique_across_roles(self):
        worker_unit, _ = self.start("worker")
        self.system.finish(worker_unit)
        reviewer_unit, _ = self.start("reviewer")
        self.assertEqual(worker_unit, "korapy-agent-lib-5")
        self.assertEqual(reviewer_unit, "korapy-agent-lib-5-r1")

    def test_stop_finds_the_live_unit_whichever_role_holds_it(self):
        worker_unit, _ = self.start("worker")
        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            [worker_unit],
        )
        reviewer_unit, _ = self.start("reviewer")
        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            [reviewer_unit],
        )

    def test_a_legacy_single_dir_session_resumes_as_the_worker(self):
        worker_unit, _ = self.start("worker")
        self.system.finish(worker_unit)
        with open(os.path.join(paths.newest_session("lib", 5, "worker"), "chat.jsonl"), "w") as h:
            h.write(json.dumps({"message": {"role": "assistant", "content": "legacy work"}}) + "\n")
        resumed, _ = launch.resume(
            self.target("lib"),
            5,
            "keep going",
            hold_reason_fn=_free_hold,
            list_fn=self.system.list_fn,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            run=self.system.run,
        )
        self.assertEqual(resumed, "korapy-agent-lib-5-r1")


class ProfileLaunchCliTest(OpsCase):
    """`launch`/`resume --profile` through the CLI, with fake store and tracker."""

    PROFILES_TOML = """\
[profiles.fast]
runtime = "muse"
"""

    def write_profiles(self):
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            text = handle.read()
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(text + self.PROFILES_TOML)

    def write_tracker_repo(self):
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            text = handle.read()
        text = text.replace(
            'name = "lib"\nclone = { path = "clones/lib" }',
            'name = "lib"\nclone = { path = "clones/lib" }\ntracker.repo = "example/lib"',
        )
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def seams(self, store=None, tracker=None):
        return launchcli.Seams(
            list_fn=self.system.list_fn,
            run=self.system.run,
            stop_fn=self.system.stop_fn,
            host_ledger_fn=self.system.host_ledger_fn,
            labels_fn=lambda kind, repo, number: [],
            usage=roomy,
            store=store,
            tracker=tracker,
        )

    def verb(self, *argv, **seams_kw):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb([*argv, "--root", self.root], self.seams(**seams_kw))
        return code, out.getvalue(), err.getvalue()

    def registered_runtime(self, unit):
        (found,) = [e for e in registry.load_all() if e["id"] == unit]
        return found["runtime"]

    def test_launch_with_profile_runs_the_profile_runtime(self):
        self.write_profiles()
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "fast",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        unit = out.split()[1]
        self.assertEqual(self.registered_runtime(unit), "muse")
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "runtime"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "muse\n")

    def test_launch_with_profile_registers_the_profile_name(self):
        self.write_profiles()
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "fast",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        unit = out.split()[1]
        (found,) = [e for e in registry.load_all() if e["id"] == unit]
        self.assertEqual(found["profile"], "fast")

    def test_launch_with_unknown_profile_is_exit_4(self):
        self.write_profiles()
        code, _, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "nope",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 4)
        self.assertIn("nope", err)

    def test_launch_with_a_truncated_store_is_exit_4(self):
        self.write_profiles()
        store = FakeStore(error=sqlite3.DatabaseError("file is not a database"))
        code, _, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            store=store,
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 4)
        self.assertIn("profile assignment", err)

    def test_launch_with_profile_survives_a_truncated_store(self):
        self.write_profiles()
        store = FakeStore(error=sqlite3.DatabaseError("file is not a database"))
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "fast",
            store=store,
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))

    def test_launch_without_a_profile_and_a_dead_tracker_is_exit_3(self):
        self.write_profiles()
        store = FakeStore(found=SimpleNamespace(labels=["ready"], extra={}), fresh=False)
        tracker = FakeTracker([])
        tracker.item_errors[("lib", 5)] = "down"
        code, _, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            store=store,
            tracker=tracker,
        )
        self.assertEqual(code, 3)

    def test_launch_with_profile_survives_a_dead_tracker(self):
        self.write_profiles()
        store = FakeStore(found=SimpleNamespace(labels=["ready"], extra={}), fresh=False)
        tracker = FakeTracker([])
        tracker.item_errors[("lib", 5)] = "down"
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "fast",
            store=store,
            tracker=tracker,
        )
        self.assertEqual((code, err), (0, ""))

    def test_launch_records_the_profile_for_resume(self):
        self.write_profiles()
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "fast",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        recorded = profiles.recorded_profile(paths.newest_session("lib", 5, "worker"))
        self.assertIsNotNone(recorded)
        assert recorded is not None
        self.assertEqual(recorded.name, "fast")
        self.assertEqual(recorded.runtime, "muse")
        self.assertEqual(recorded.layer, 1)

    def append_toml(self, text):
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            before = handle.read()
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(before + text)

    def test_launch_applies_muse_profile_options(self):
        self.append_toml(
            '[profiles.effort]\nruntime = "muse"\n'
            "[profiles.effort.options]\n"
            'reasoning_effort = "low"\n'
        )
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "effort",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "muse.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["reasoning_effort"], "low")
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "low")

    def test_launch_applies_omp_profile_options(self):
        self.append_toml(
            '[profiles.quiet]\nruntime = "omp"\n[profiles.quiet.options]\nauto_approve = false\n'
        )
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "quiet",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "options.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"auto_approve": False})
        self.assertNotIn("--auto-approve", self.system.started[-1])

    def test_runtime_config_beats_muse_profile_options(self):
        self.append_toml(
            '[profiles.effort]\nruntime = "muse"\n'
            "[profiles.effort.options]\n"
            'reasoning_effort = "low"\n'
            "parallel_tool_calls = false\n"
        )
        config_path = os.path.join(self.tmp, "runtime.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"reasoning_effort": "medium"}, handle)
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "effort",
            "--runtime-config",
            config_path,
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "muse.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(config["reasoning_effort"], "medium")
        self.assertIs(config["parallel_tool_calls"], False)
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "medium")
        self.assertNotIn("--parallel-tool-calls", argv)

    def test_runtime_config_beats_omp_profile_options(self):
        self.append_toml(
            '[profiles.quiet]\nruntime = "omp"\n[profiles.quiet.options]\nauto_approve = false\n'
        )
        config_path = os.path.join(self.tmp, "runtime.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"auto_approve": True}, handle)
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "quiet",
            "--runtime-config",
            config_path,
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("--auto-approve", self.system.started[-1])

    def test_resume_without_flags_reuses_the_recorded_options(self):
        self.append_toml(
            '[profiles.effort]\nruntime = "muse"\n'
            "[profiles.effort.options]\n"
            'reasoning_effort = "low"\n'
        )
        self.write_tracker_repo()
        code, out, _ = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "effort",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        code, out, err = self.verb(
            "resume", "lib", "5", "--message", "keep going", tracker=FakeTracker([])
        )
        self.assertEqual((code, err), (0, ""))
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "low")

    def test_resume_with_runtime_rebuilds_the_session_config(self):
        self.write_tracker_repo()
        code, out, _ = self.verb("launch", "lib", "5", "--role", "worker", "--title", "feat: go")
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        leaf = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(leaf, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"message": {"role": "assistant", "content": "work"}}) + "\n")
        with open(os.path.join(leaf, "options.json"), "w", encoding="utf-8") as handle:
            json.dump({"auto_approve": False}, handle)
        code, out, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--runtime",
            "omp",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(os.path.exists(os.path.join(leaf, "options.json")))
        self.assertIn("--auto-approve", self.system.started[-1])

    def test_resume_with_profile_and_model_keeps_the_options(self):
        self.append_toml(
            '[profiles.effort]\nruntime = "muse"\n'
            "[profiles.effort.options]\n"
            'reasoning_effort = "low"\n'
        )
        self.write_tracker_repo()
        code, out, _ = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "effort",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        code, out, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--profile",
            "effort",
            "--model",
            "provider-a/model-1",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "muse.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["reasoning_effort"], "low")
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--model") + 1], "model-1")

    def test_resume_onto_an_option_less_profile_sheds_the_old_keys(self):
        self.append_toml(
            '[profiles.effort]\nruntime = "muse"\n'
            "[profiles.effort.options]\n"
            'reasoning_effort = "low"\n'
            '[profiles.plain]\nruntime = "muse"\n'
        )
        self.write_tracker_repo()
        code, out, _ = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "effort",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        code, out, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--profile",
            "plain",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "muse.json"), encoding="utf-8") as handle:
            self.assertNotIn("reasoning_effort", json.load(handle))
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")

    def test_resume_onto_another_runtime_without_its_handle_is_exit_4(self):
        self.append_toml(
            '[profiles.quiet]\nruntime = "omp"\n'
            "[profiles.quiet.options]\n"
            "auto_approve = false\n"
            '[profiles.fast]\nruntime = "muse"\n'
        )
        self.write_tracker_repo()
        code, out, _ = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "quiet",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        code, _, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--profile",
            "fast",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 4)
        self.assertIn("no muse session to resume", err)

    def test_resume_with_profile_applies_the_new_options(self):
        self.append_toml(
            '[profiles.effort]\nruntime = "muse"\n'
            "[profiles.effort.options]\n"
            'reasoning_effort = "low"\n'
            '[profiles.calm]\nruntime = "muse"\n'
            "[profiles.calm.options]\n"
            'reasoning_effort = "medium"\n'
        )
        self.write_tracker_repo()
        code, out, _ = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "effort",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        code, out, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--profile",
            "calm",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "muse.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["reasoning_effort"], "medium")
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "medium")

    def test_resume_without_flags_reuses_the_recorded_profile_after_expiry(self):
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            text = handle.read()
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(
                text + '[profiles.fast]\nruntime = "muse"\n[profiles.slow]\nruntime = "omp"\n'
            )
        tgt = launch.target(self.root, "lib")
        now = 1_800_000_000.0
        live = profiles.Selections(
            default="slow",
            override=profiles.Override(scope="global", profile="fast", until=now + 3600),
        )
        unit, _ = launch.launch(
            tgt,
            5,
            "do the thing",
            title="feat: go",
            role="worker",
            model="provider-a/model-x",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            pool_fn=lambda: [],
            ledger_fn=lambda: {"readings": {}, "marks": {}},
            now_fn=lambda: now,
            selections=live,
            store=FakeStore(),
            tracker=None,
        )
        self.system.finish(unit)
        session = paths.newest_session("lib", 5, "worker")
        recorded = profiles.recorded_profile(session)
        self.assertIsNotNone(recorded)
        assert recorded is not None
        self.assertEqual(recorded.name, "fast")
        expired = profiles.Selections(
            default="slow",
            override=profiles.Override(scope="global", profile="fast", until=now - 10),
        )
        resumed, _ = launch.resume(
            tgt,
            5,
            "keep going",
            hold_reason_fn=_free_hold,
            role="worker",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            host_ledger_fn=self.system.host_ledger_fn,
            pool_fn=lambda: [],
            ledger_fn=lambda: {"readings": {}, "marks": {}},
            now_fn=lambda: now + 7200,
            selections=expired,
            store=FakeStore(),
            tracker=None,
        )
        (found,) = [e for e in registry.load_all() if e["id"] == resumed]
        self.assertEqual(found["model"], "provider-a/model-x")
        self.assertIn("model-x", " ".join(self.system.started[-1]))

    def test_resume_with_runtime_alone_runs_the_named_runtime(self):
        self.write_tracker_repo()
        code, out, _ = self.verb("launch", "lib", "5", "--role", "worker", "--title", "feat: go")
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"message": {"role": "assistant", "content": "work"}}) + "\n")
        with open(os.path.join(session, "muse_session_id"), "w", encoding="utf-8") as handle:
            handle.write("12345678-1234-5678-1234-567812345678\n")
        code, out, err = self.verb(
            "resume", "lib", "5", "--message", "keep going", "--runtime", "muse"
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.registered_runtime(out.split()[2]), "muse")

    def test_resume_with_an_unknown_runtime_alone_is_exit_4(self):
        self.write_tracker_repo()
        code, out, _ = self.verb("launch", "lib", "5", "--role", "worker", "--title", "feat: go")
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        session = paths.newest_session("lib", 5, "worker")
        with open(os.path.join(session, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"message": {"role": "assistant", "content": "work"}}) + "\n")
        code, _, err = self.verb(
            "resume", "lib", "5", "--message", "keep going", "--runtime", "nope"
        )
        self.assertEqual(code, 4)
        self.assertIn("nope", err)

    def test_resume_with_profile_records_the_new_profile(self):
        self.write_profiles()
        self.write_tracker_repo()
        toml_path = os.path.join(self.root, settings.PROJECT_FILE)
        with open(toml_path, encoding="utf-8") as handle:
            text = handle.read()
        with open(toml_path, "w", encoding="utf-8") as handle:
            handle.write(text + '[profiles.slow]\nruntime = "muse"\n')
        code, out, err = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "fast",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        self.system.finish(out.split()[1])
        code, out, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--profile",
            "slow",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        recorded = profiles.recorded_profile(paths.newest_session("lib", 5, "worker"))
        self.assertIsNotNone(recorded)
        assert recorded is not None
        self.assertEqual(recorded.name, "slow")

    def test_resume_with_profile_runs_the_profile_runtime(self):
        self.write_profiles()
        self.write_tracker_repo()
        code, out, _ = self.verb(
            "launch",
            "lib",
            "5",
            "--role",
            "worker",
            "--title",
            "feat: go",
            "--profile",
            "fast",
            tracker=FakeTracker([]),
        )
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        code, out, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--profile",
            "fast",
            tracker=FakeTracker([]),
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.registered_runtime(out.split()[2]), "muse")

    def test_resume_with_model_alone_keeps_the_old_path(self):
        self.write_tracker_repo()
        code, out, _ = self.verb("launch", "lib", "5", "--role", "worker", "--title", "feat: go")
        self.assertEqual(code, 0)
        self.system.finish(out.split()[1])
        with open(os.path.join(paths.newest_session("lib", 5, "worker"), "chat.jsonl"), "w") as h:
            h.write(json.dumps({"message": {"role": "assistant", "content": "work"}}) + "\n")
        code, out, err = self.verb(
            "resume",
            "lib",
            "5",
            "--message",
            "keep going",
            "--model",
            "m",
        )
        self.assertEqual((code, err), (0, ""))


class NoSliceStopgap(unittest.TestCase):
    """The managed slice hierarchy replaced the manual `--slice` stopgap."""

    def test_no_verb_takes_a_slice_flag(self):
        parser = cli.build_parser()
        subs = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        for verb in ("launch", "resume"):
            options = {
                option for action in subs.choices[verb]._actions for option in action.option_strings
            }
            self.assertNotIn("--slice", options, verb)


if __name__ == "__main__":
    unittest.main()
