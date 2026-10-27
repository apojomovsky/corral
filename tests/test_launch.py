"""corral.launch and its CLI, end to end over fake seams.

A fake project: a temp ops root with a corral.toml holding several [[repo]]
entries, a bare repository per repo standing in for origin, and a clone.
Real git runs in those temp dirs (local remotes only). The service manager
is an in-memory fake: `list_fn` lists it, `run` starts a unit in it, and
`stop_fn` stops one. No test runs systemd, the runtimes, or touches the
real state, config or session dirs; the environment is cleared of CORRAL_*
and points at temp dirs.
"""

import dataclasses
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from contextlib import redirect_stdout

from corral import cli, launch, launchcli, paths, registry, runtimes, settings, units
from corral.runtimes import omp, opencode
from tests.test_reviewrecord import git_in

REPOS = ("lib", "my-repo")

TOML = """\
[agents]
cap = 2

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


class FakeSystem:
    """An in-memory service manager: units, starts and stops, nothing real."""

    def __init__(self):
        self.units = {}
        self.started = []
        self.stopped = []
        self.fail = None

    def list_fn(self, prefix):
        return [(name, state, "running") for name, state in self.units.items()]

    def run(self, argv, timeout=None):
        self.started.append(list(argv))
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.fail)
        unit = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--unit="))
        self.units[unit] = "active"
        return subprocess.CompletedProcess(argv, 0, "", "")

    def stop_fn(self, unit):
        self.stopped.append(unit)
        self.units[unit] = "inactive"

    def finish(self, unit):
        """The unit ended on its own: no longer live, still in the log."""
        self.units[unit] = "inactive"

    def seams(self):
        return launchcli.Seams(list_fn=self.list_fn, run=self.run, stop_fn=self.stop_fn)


class OpsCase(unittest.TestCase):
    """A temp ops root with two real clones, a fake service manager, and temp dirs."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-launch-"))
        self.root = os.path.join(self.tmp, "ops")
        os.makedirs(os.path.join(self.root, "clones"))
        for name in REPOS:
            self.make_clone(name)
        with open(os.path.join(self.root, settings.PROJECT_FILE), "w", encoding="utf-8") as h:
            h.write(TOML)
        kept = {k: v for k, v in os.environ.items() if not k.startswith("CORRAL_")}
        env = dict(kept)
        env.update(
            CORRAL_STATE=os.path.join(self.tmp, "state"),
            CORRAL_SESSIONS=os.path.join(self.tmp, "sessions"),
            CORRAL_CONFIG=os.path.join(self.tmp, "config"),
            CORRAL_CACHE=os.path.join(self.tmp, "cache"),
        )
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.enterContext(mock.patch.object(omp, "binary", return_value="/fake/omp"))
        self.enterContext(mock.patch.object(opencode, "binary", return_value="/fake/opencode"))
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

    def target(self, repo="lib", runtime=None):
        flags = {"agents.runtime": runtime} if runtime else None
        return launch.target(self.root, repo, flags=flags)

    def prompt_file(self, text="do the thing"):
        path = os.path.join(self.tmp, "prompt.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def session(self, repo, number):
        return paths.ticket_session_dir(repo, number)

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
        self.assertEqual(tgt.prefix, "corral-agent-")
        self.assertEqual(tgt.runtime, "omp")
        self.assertEqual(tgt.cap, 2)
        self.assertEqual(tgt.branch_template, "{type}/{issue}-{slug}")
        self.assertEqual(tgt.worktrees_root, ".worktrees/")

    def test_the_runtime_flag_overrides_the_setting(self):
        self.assertEqual(self.target("lib", runtime="opencode").runtime, "opencode")

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
        self.assertIn("corral.launch", settings.PROVIDERS)

    def test_the_runtime_limit_matches_the_runtime_default(self):
        self.assertEqual(runtimes.DEFAULT_MAX_TIME, "3h")
        self.assertEqual(launch.RUNTIME_MAX_S, 3 * 3600)


class AdmitTest(OpsCase):
    def test_a_free_host_admits_the_ticket(self):
        launch.admit(self.target(), 5, list_fn=lambda prefix: [])

    def test_a_live_unit_of_this_ticket_is_already_live(self):
        self.system.units["corral-agent-lib-5"] = "active"
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(self.target(), 5, list_fn=self.system.list_fn)
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_the_same_number_in_another_repo_is_not_live(self):
        self.system.units["corral-agent-my-repo-5"] = "active"
        launch.admit(self.target("lib"), 5, list_fn=self.system.list_fn)

    def test_a_full_host_refuses_with_exit_75(self):
        self.system.units["corral-agent-lib-6"] = "active"
        self.system.units["corral-agent-my-repo-7"] = "activating"
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target(), 5, list_fn=self.system.list_fn)
        self.assertEqual(ctx.exception.exit_code, 75)

    def test_a_registered_worker_on_this_host_holds_a_slot(self):
        registry.register("worker-1", "claude-code", ticket=registry.make_ticket("my-repo", 9))
        tgt = dataclasses.replace(self.target(), cap=1)
        with self.assertRaises(launch.CapFull):
            launch.admit(tgt, 5, list_fn=lambda prefix: [])

    def test_an_unreachable_service_manager_is_exit_3(self):
        def down(prefix):
            raise units.Unreachable("no bus")

        with self.assertRaises(units.Unreachable) as ctx:
            launch.admit(self.target(), 5, list_fn=down)
        self.assertEqual(ctx.exception.exit_code, 3)


class LaunchTest(OpsCase):
    def start(self, runtime, number=5, title="feat: add the thing", **kwargs):
        return launch.launch(
            self.target("lib", runtime),
            number,
            kwargs.pop("prompt", "do the thing"),
            title=title,
            list_fn=self.system.list_fn,
            run=self.system.run,
            **kwargs,
        )

    def check_full_path(self, runtime):
        unit, workdir = self.start(runtime, model="provider-a/model-1")
        self.assertEqual(unit, "corral-agent-lib-5")
        self.assertEqual(
            workdir,
            os.path.realpath(
                os.path.join(self.root, "clones", "lib", ".worktrees", "5-add-the-thing")
            ),
        )
        self.assertTrue(os.path.isdir(workdir))

        session = self.session("lib", 5)
        with open(os.path.join(session, "prompt.md"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "do the thing")
        self.assertEqual(runtimes.recorded(session), runtime)
        self.assertEqual(units.started_units(session), [unit])

        argv = self.system.started[-1]
        self.assertIn(f"--unit={unit}", argv)
        self.assertIn(f"--setenv=CORRAL_AGENT_ID={unit}", argv)
        self.assertIn(f"--working-directory={workdir}", argv)
        self.assertIn("do the thing", argv)
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
                list_fn=self.system.list_fn,
                run=self.system.run,
            )
        self.system.finish("corral-agent-lib-5")

        unit, _ = launch.resume(
            self.target("lib"), 5, "keep going", list_fn=self.system.list_fn, run=self.system.run
        )
        self.assertEqual(unit, "corral-agent-lib-5-r1")
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
            ["corral-agent-lib-5-r1"],
        )
        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            [],
        )

    def test_launch_resume_tail_and_stop_an_opencode_agent(self):
        session = self.check_full_path("opencode")
        self.assertTrue(os.path.exists(os.path.join(session, "opencode.json")))
        self.assertEqual(self.system.started[-1][0], "systemd-run")

        self.system.finish("corral-agent-lib-5")
        with open(os.path.join(session, "out.log"), "w", encoding="utf-8") as h:
            h.write(
                json.dumps(
                    {
                        "type": "tool_use",
                        "sessionID": "ses_1",
                        "part": {
                            "tool": "bash",
                            "state": {"input": {"description": "run the tests"}},
                        },
                    }
                )
                + "\n"
            )

        unit, _ = launch.resume(
            self.target("lib"), 5, "keep going", list_fn=self.system.list_fn, run=self.system.run
        )
        self.assertEqual(unit, "corral-agent-lib-5-r1")
        argv = self.system.started[-1]
        self.assertEqual(argv[argv.index("--session") + 1], "ses_1")

        out = io.StringIO()
        launch.tail(self.target("lib"), 5, out=out)
        self.assertIn("run the tests", out.getvalue())

        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            ["corral-agent-lib-5-r1"],
        )

    def test_the_runtime_config_is_written_into_the_session(self):
        self.start("opencode", runtime_config={"model": "provider-a/model-1"})
        with open(os.path.join(self.session("lib", 5), "opencode.json"), encoding="utf-8") as h:
            self.assertEqual(json.load(h), {"model": "provider-a/model-1"})

    def test_a_full_host_refuses_before_anything_is_made(self):
        self.system.units["corral-agent-my-repo-6"] = "active"
        self.system.units["corral-agent-my-repo-7"] = "active"
        with self.assertRaises(launch.CapFull):
            self.start("omp")
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_an_already_live_ticket_is_exit_2_and_makes_nothing(self):
        self.system.units["corral-agent-lib-5"] = "active"
        with self.assertRaises(units.UnitExists):
            self.start("omp")
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_an_unreachable_service_manager_is_exit_3(self):
        def down(prefix):
            raise units.Unreachable("no bus")

        with self.assertRaises(units.Unreachable) as ctx:
            launch.launch(self.target(), 5, "x", list_fn=down, run=self.system.run)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_a_start_that_loses_the_race_is_exit_2(self):
        self.system.fail = "Unit corral-agent-lib-5.service already exists."
        with self.assertRaises(units.UnitExists):
            self.start("omp")
        self.assertFalse(os.path.exists(os.path.join(self.session("lib", 5), units.UNITS_LOG)))

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
        tgt = dataclasses.replace(self.target("lib"), runtime="claude-code")
        with self.assertRaises(launch.LaunchError):
            launch.launch(tgt, 5, "x", list_fn=self.system.list_fn, run=self.system.run)
        self.assertEqual(self.system.started, [])

    def test_two_repos_keep_separate_sessions_and_units(self):
        self.start("omp", number=5)
        launch.launch(
            self.target("my-repo"),
            5,
            "other work",
            title="feat: other",
            list_fn=self.system.list_fn,
            run=self.system.run,
        )
        self.assertNotEqual(self.session("lib", 5), self.session("my-repo", 5))
        self.assertEqual(units.started_units(self.session("lib", 5)), ["corral-agent-lib-5"])
        self.assertEqual(
            units.started_units(self.session("my-repo", 5)), ["corral-agent-my-repo-5"]
        )
        self.assertEqual(
            launch.stop(
                self.target("lib"), 5, list_fn=self.system.list_fn, stop_fn=self.system.stop_fn
            ),
            ["corral-agent-lib-5"],
        )
        self.assertEqual(self.system.units["corral-agent-my-repo-5"], "active")


class ResumeTailTest(OpsCase):
    def test_resume_without_a_session_is_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, "no omp session to resume"):
            launch.resume(
                self.target(), 5, "keep going", list_fn=self.system.list_fn, run=self.system.run
            )
        self.assertEqual(self.system.started, [])

    def test_tail_without_activity_is_refused(self):
        with self.assertRaises(launch.LaunchError):
            launch.tail(self.target(), 5, out=io.StringIO())

    def test_resume_with_a_bad_message_is_refused(self):
        with self.assertRaises(launch.LaunchError):
            launch.resume(
                self.target(), 5, "--oops", list_fn=self.system.list_fn, run=self.system.run
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

    def test_launch_stop_and_stop_again_map_to_exit_codes(self):
        pf = self.prompt_file()
        code, out, _ = self.verb("launch", "lib", "5", "--prompt-file", pf, "--title", "feat: go")
        self.assertEqual(code, 0)
        self.assertIn("launched corral-agent-lib-5", out)

        code, out, _ = self.verb("stop", "lib", "5")
        self.assertEqual(code, 0)
        self.assertIn("stopped corral-agent-lib-5", out)

        code, out, _ = self.verb("stop", "lib", "5")
        self.assertEqual(code, 1)
        self.assertIn("nothing to stop", out)

    def test_a_full_host_is_exit_75_and_a_live_ticket_is_exit_2(self):
        pf = self.prompt_file()
        self.system.units["corral-agent-my-repo-6"] = "active"
        self.system.units["corral-agent-my-repo-7"] = "active"
        code, _, err = self.verb("launch", "lib", "5", "--prompt-file", pf)
        self.assertEqual(code, 75)
        self.assertIn("slots", err)

        self.system.units.clear()
        self.system.units["corral-agent-lib-5"] = "active"
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

    def test_a_root_without_corral_toml_is_exit_4(self):
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

    def test_the_verbs_are_registered_on_the_corral_command(self):
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
            self.target("lib", "omp"),
            5,
            "first prompt",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
        )
        self.system.finish("corral-agent-lib-5")
        session = self.session("lib", 5)
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
            return [("corral-agent-lib-5", "active", "running")]

        with self.assertRaises(units.UnitExists):
            launch.launch(
                self.target("lib", "opencode"),
                5,
                "second prompt",
                title="feat: add the thing",
                list_fn=flipping,
                run=self.system.run,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(runtimes.recorded(session), "omp")
        for name, text in before.items():
            self.assertEqual(_read(os.path.join(session, name)), text, name)
        self.assertFalse(os.path.exists(os.path.join(session, "opencode.json")))
        self.assertEqual(len(self.system.started), started)

    def test_two_sequential_resumes_take_generations_one_and_two(self):
        session = self.session("lib", 5)
        launch.launch(
            self.target("lib"),
            5,
            "go",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
        )
        self.system.finish("corral-agent-lib-5")
        with open(os.path.join(session, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")

        first, _ = launch.resume(
            self.target("lib"), 5, "again", list_fn=self.system.list_fn, run=self.system.run
        )
        self.system.finish(first)
        second, _ = launch.resume(
            self.target("lib"), 5, "again", list_fn=self.system.list_fn, run=self.system.run
        )
        self.assertEqual(first, "corral-agent-lib-5-r1")
        self.assertEqual(second, "corral-agent-lib-5-r2")
        self.assertEqual(
            units.started_units(session),
            ["corral-agent-lib-5", "corral-agent-lib-5-r1", "corral-agent-lib-5-r2"],
        )


class RegistryCapTest(OpsCase):
    """Admission counts registered agents under the target's own resolved settings."""

    def holders(self):
        registry.register("worker-1", "claude-code", ticket=registry.make_ticket("my-repo", 9))
        registry.register("worker-2", "claude-code", ticket=registry.make_ticket("lib", 9))

    def test_registered_workers_fill_the_cap_by_default(self):
        self.holders()
        with self.assertRaises(launch.CapFull):
            launch.admit(self.target(), 5, list_fn=lambda prefix: [])

    def test_count_toward_cap_false_in_the_ops_root_frees_the_slots(self):
        with open(os.path.join(self.root, settings.PROJECT_FILE), "a", encoding="utf-8") as handle:
            handle.write("\n[registry]\ncount_toward_cap = false\n")
        self.holders()
        tgt = self.target()
        self.assertFalse(tgt.resolved["registry.count_toward_cap"])
        launch.admit(tgt, 5, list_fn=lambda prefix: [])

    def test_a_registered_live_ticket_refuses_with_the_registry_note(self):
        registry.register("worker-1", "claude-code", ticket=registry.make_ticket("lib", 5))
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(self.target(), 5, list_fn=lambda prefix: [])
        self.assertEqual(
            str(ctx.exception),
            "#5 of lib already has a live agent (registered agent, counts until its heartbeat goes stale)",
        )

    def test_a_full_host_held_by_registered_agents_names_them(self):
        self.holders()
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target(), 5, list_fn=lambda prefix: [])
        self.assertEqual(
            str(ctx.exception),
            "2 of 2 agent slots are taken (registered agent, counts until its heartbeat goes stale): try later",
        )

    def test_a_unit_holder_gets_no_registry_note(self):
        self.system.units["corral-agent-lib-5"] = "active"
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(self.target(), 5, list_fn=self.system.list_fn)
        self.assertEqual(str(ctx.exception), "#5 of lib already has a live agent")

        self.system.units.clear()
        self.system.units["corral-agent-my-repo-6"] = "active"
        self.system.units["corral-agent-my-repo-7"] = "active"
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target(), 5, list_fn=self.system.list_fn)
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
            run=self.system.run,
        )
        self.system.units["corral-agent-lib-5-r7"] = "active"

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb(
                ["stop", "lib", "5", "--root", self.root], self.system.seams()
            )
        self.assertEqual(code, 2)
        self.assertIn("stopped corral-agent-lib-5", out.getvalue())
        self.assertIn(
            "live but not proven ours, not stopped: corral-agent-lib-5-r7", err.getvalue()
        )
        self.assertEqual(self.system.stopped, ["corral-agent-lib-5"])
        self.assertEqual(self.system.units["corral-agent-lib-5-r7"], "active")

    def test_only_an_unproven_unit_live_is_exit_2_with_nothing_stopped(self):
        self.system.units["corral-agent-lib-5-r7"] = "active"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb(
                ["stop", "lib", "5", "--root", self.root], self.system.seams()
            )
        self.assertEqual(code, 2)
        self.assertEqual(self.system.stopped, [])
        self.assertIn("corral-agent-lib-5-r7", err.getvalue())


if __name__ == "__main__":
    unittest.main()
