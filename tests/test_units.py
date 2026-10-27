"""Transient agent units: names, listing, the spawn lock, start and stop.

Fake `run`, `list_fn` and `stop_fn` only. No test starts or stops a unit.
"""

import ast
import fcntl
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from corral import settings, units

PREFIX = "corral-agent-"
REPOS = ("lib", "my-repo", "repo2", "a-5", "x-r3", "a.b")
#: Built from parts so this file never holds the production stop's name as a literal.
STOP_NAME = "".join(["systemctl", "_stop"])


def done(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeRun:
    """Records each argv and answers with a canned result or raises one."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else done(argv)


class Dirs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def session(self, name="s"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path


class NamesTest(unittest.TestCase):
    def test_unit_name_formats(self):
        self.assertEqual(units.unit_name(PREFIX, "lib", 5), "corral-agent-lib-5")
        self.assertEqual(units.unit_name(PREFIX, "lib", 5, 0), "corral-agent-lib-5")
        self.assertEqual(units.unit_name(PREFIX, "lib", 5, 2), "corral-agent-lib-5-r2")

    def test_round_trip_for_several_repos_tickets_and_generations(self):
        for repo in REPOS:
            for number in (1, 5, 120):
                for gen in (0, 1, 3, 12):
                    with self.subTest(repo=repo, number=number, gen=gen):
                        name = units.unit_name(PREFIX, repo, number, gen)
                        self.assertEqual(units.parse_unit(PREFIX, name), (repo, number, gen))
                        self.assertEqual(
                            units.parse_unit(PREFIX, name + ".service"), (repo, number, gen)
                        )

    def test_repo_names_with_hyphens_and_digits_split_correctly(self):
        self.assertEqual(units.parse_unit(PREFIX, "corral-agent-a-5-7"), ("a-5", 7, 0))
        self.assertEqual(units.parse_unit(PREFIX, "corral-agent-a-5-7-r2"), ("a-5", 7, 2))
        self.assertEqual(units.parse_unit(PREFIX, "corral-agent-x-r3-4"), ("x-r3", 4, 0))
        self.assertEqual(units.parse_unit(PREFIX, "corral-agent-repo2-9-r1"), ("repo2", 9, 1))

    def test_other_prefixes_and_malformed_names_do_not_parse(self):
        self.assertIsNone(units.parse_unit(PREFIX, "other-agent-lib-5"))
        self.assertIsNone(units.parse_unit(PREFIX, "corral-agentlib-5"))
        for bad in (
            "corral-agent-lib",
            "corral-agent-lib-0",
            "corral-agent-lib-05",
            "corral-agent-lib-5-r0",
            "corral-agent-lib-5-r",
            "corral-agent-lib-5-x",
            "corral-agent--5",
            "corral-agent-lib-5.timer",
            "corral-agent-x.git-5",
            "",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(units.parse_unit(PREFIX, bad))
        self.assertIsNone(units.parse_unit(PREFIX, None))

    def test_bad_inputs_are_usage_errors(self):
        for args in (
            ("", "lib", 1, 0),
            ("corral agent-", "lib", 1, 0),
            (PREFIX, "../lib", 1, 0),
            (PREFIX, "", 1, 0),
            (PREFIX, "lib.git", 1, 0),
            (PREFIX, "lib", 0, 0),
            (PREFIX, "lib", True, 0),
            (PREFIX, "lib", 1, -1),
        ):
            with self.subTest(args=args), self.assertRaises(units.UnitsError):
                units.unit_name(*args)
        with self.assertRaises(units.UnitsError):
            units.parse_unit("bad prefix", "x")

    def test_exit_codes(self):
        self.assertEqual(units.UnitExists.exit_code, 2)
        self.assertEqual(units.Unreachable.exit_code, 3)
        self.assertEqual(units.UnitsError.exit_code, 4)
        self.assertTrue(issubclass(units.UnitExists, units.UnitsError))
        self.assertTrue(issubclass(units.Unreachable, units.UnitsError))

    def test_the_prefix_setting_is_registered_with_its_default(self):
        self.assertEqual(settings.REGISTRY["agents.unit_prefix"].default, PREFIX)
        self.assertIn("corral.units", settings.PROVIDERS)


LISTING = "\n".join(
    [
        "corral-agent-lib-5.service loaded active running Agent lib 5",
        "corral-agent-lib-5-r1.service loaded inactive dead Agent lib 5 gen 1",
        "corral-agent-my-repo-7.service loaded failed failed Agent my-repo 7",
        "corral-agent-a-5-2-r4.service loaded activating start Agent a-5 2",
        "corral-agent-x-r3-9.service loaded deactivating stop-sigterm Agent",
        "corral-agent-repo2-1.service loaded reloading reload Agent repo2 1",
        "other-agent-lib-5.service loaded active running another fleet",
        "third-agent-3.service loaded active running another fleet",
        "corral-agent-broken.service loaded active running not one of ours",
    ]
)


class ListTest(unittest.TestCase):
    def test_lists_only_this_prefix_with_active_and_sub_states(self):
        fake = FakeRun(done([], stdout=LISTING))
        found = units.list_units(PREFIX, run=fake)
        self.assertEqual(
            found,
            [
                ("corral-agent-lib-5", "active", "running"),
                ("corral-agent-lib-5-r1", "inactive", "dead"),
                ("corral-agent-my-repo-7", "failed", "failed"),
                ("corral-agent-a-5-2-r4", "activating", "start"),
                ("corral-agent-x-r3-9", "deactivating", "stop-sigterm"),
                ("corral-agent-repo2-1", "reloading", "reload"),
            ],
        )
        self.assertEqual(
            fake.calls,
            [
                [
                    "systemctl",
                    "--user",
                    "list-units",
                    "corral-agent-*",
                    "--all",
                    "--no-legend",
                    "--no-pager",
                    "--plain",
                ]
            ],
        )

    def test_no_units_is_an_empty_list_not_an_error(self):
        self.assertEqual(units.list_units(PREFIX, run=FakeRun(done([], stdout=""))), [])
        blank = FakeRun(done([], stdout="\n   \n"))
        self.assertEqual(units.list_units(PREFIX, run=blank), [])

    def test_systemctl_that_cannot_run_or_fails_is_unreachable(self):
        for fake in (
            FakeRun(error=FileNotFoundError("systemctl")),
            FakeRun(done([], returncode=1, stderr="Failed to connect to bus")),
        ):
            with self.subTest(fake=fake), self.assertRaises(units.Unreachable) as caught:
                units.list_units(PREFIX, run=fake)
            self.assertEqual(caught.exception.exit_code, 3)

    def test_a_line_in_an_unknown_shape_is_unreachable_not_no_units(self):
        for stdout in ("corral-agent-lib-5.service loaded active\n", "garbage\n"):
            with self.subTest(stdout=stdout), self.assertRaises(units.Unreachable):
                units.list_units(PREFIX, run=FakeRun(done([], stdout=stdout)))

    def test_live_tickets_counts_live_states_only(self):
        found = units.list_units(PREFIX, run=FakeRun(done([], stdout=LISTING)))
        self.assertEqual(
            units.live_tickets(PREFIX, found),
            {("lib", 5), ("a-5", 2), ("x-r3", 9), ("repo2", 1)},
        )

    def test_live_tickets_ignores_other_prefixes_and_dead_units(self):
        units_seen = [
            ("corral-agent-lib-1", "active", "running"),
            ("corral-agent-lib-2", "inactive", "dead"),
            ("corral-agent-lib-3", "failed", "failed"),
            ("other-agent-lib-4", "active", "running"),
        ]
        self.assertEqual(units.live_tickets(PREFIX, units_seen), {("lib", 1)})
        self.assertEqual(units.live_tickets(PREFIX, []), set())


class SpawnLockTest(Dirs):
    def test_lock_is_held_inside_and_released_after(self):
        with mock.patch.dict(os.environ, {"CORRAL_STATE": self.session("state")}):
            lock_path = os.path.join(self.tmp, "state", units.SPAWN_LOCK)
            with units.spawn_lock():
                self.assertTrue(os.path.exists(lock_path))
                with open(lock_path, "a", encoding="utf-8") as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with open(lock_path, "a", encoding="utf-8") as other:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(other, fcntl.LOCK_UN)


class UnitArgvTest(unittest.TestCase):
    def test_exact_argv_with_env_items_and_passthrough(self):
        with mock.patch.dict(os.environ, {"PATH": "/p/bin"}):
            argv = units.unit_argv(
                "corral-agent-lib-5-r1",
                "/work/lib",
                "/sessions/lib-5",
                ["omp", "-p", "hi"],
                ["OPENCODE_CONFIG=/s/opencode.json", "AGENT_ID=a1"],
                3600,
            )
        home = os.path.expanduser("~")
        self.assertEqual(
            argv,
            [
                "systemd-run",
                "--user",
                "--collect",
                "--quiet",
                "--unit=corral-agent-lib-5-r1",
                "--property=RuntimeMaxSec=3600",
                "--property=StandardOutput=append:/sessions/lib-5/out.log",
                "--property=StandardError=append:/sessions/lib-5/err.log",
                "--working-directory=/work/lib",
                "--setenv=PATH=/p/bin",
                f"--setenv=HOME={home}",
                "--setenv=OPENCODE_CONFIG=/s/opencode.json",
                "--setenv=AGENT_ID=a1",
                "omp",
                "-p",
                "hi",
            ],
        )

    def test_one_setenv_per_env_item_and_mapping_accepted(self):
        with mock.patch.dict(os.environ, {"PATH": "/p/bin"}):
            expected = [
                "--setenv=PATH=/p/bin",
                f"--setenv=HOME={os.path.expanduser('~')}",
                "--setenv=A=1",
                "--setenv=B=2",
            ]
            for env in ({"A": "1", "B": "2"}, ["A=1", "B=2"]):
                with self.subTest(env=env):
                    argv = units.unit_argv("u", "/w", "/s", ["x"], env, 60)
                    setenv = [a for a in argv if a.startswith("--setenv=")]
                    self.assertEqual(setenv, expected)
                    self.assertEqual(argv[-1], "x")
            bare = units.unit_argv("u", "/w", "/s", ["x"], [], 60)
            self.assertEqual([a for a in bare if a.startswith("--setenv=")], expected[:2])

    def test_bad_runtime_limit_is_refused(self):
        for bad in (0, -1, True, "3600"):
            with self.subTest(bad=bad), self.assertRaises(units.UnitsError):
                units.unit_argv("u", "/w", "/s", ["x"], [], bad)

    def test_a_command_must_start_with_a_program_not_an_option(self):
        for command in ([], ["-p", "omp"], ["--help"]):
            with self.subTest(command=command), self.assertRaises(units.UnitsError):
                units.unit_argv("u", "/w", "/s", command, [], 60)
        argv = units.unit_argv("u", "/w", "/s", ["omp", "-p"], [], 60)
        self.assertEqual(argv[-2:], ["omp", "-p"])


class StartTest(Dirs):
    def argv(self, unit):
        return ["systemd-run", "--user", f"--unit={unit}", "omp"]

    def test_success_records_the_unit_in_units_log(self):
        session = self.session()
        fake = FakeRun()
        units.start(self.argv("corral-agent-lib-5"), session, "corral-agent-lib-5", run=fake)
        units.start(self.argv("corral-agent-lib-5-r1"), session, "corral-agent-lib-5-r1", run=fake)
        self.assertEqual(
            units.started_units(session), ["corral-agent-lib-5", "corral-agent-lib-5-r1"]
        )
        self.assertEqual(fake.calls[0], self.argv("corral-agent-lib-5"))

    def test_already_exists_or_already_loaded_is_unit_exists(self):
        session = self.session()
        for stderr in (
            "Unit corral-agent-lib-5.service already exists.",
            "Unit corral-agent-lib-5.service Already Loaded",
        ):
            with self.subTest(stderr=stderr):
                fake = FakeRun(done([], returncode=1, stderr=stderr))
                with self.assertRaises(units.UnitExists) as caught:
                    units.start(
                        self.argv("corral-agent-lib-5"), session, "corral-agent-lib-5", run=fake
                    )
                self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(units.started_units(session), [])

    def test_an_unreachable_user_manager_is_unreachable(self):
        session = self.session()
        for stderr in ("Failed to connect to bus: No medium found\n", "FAILED TO CONNECT TO BUS"):
            with self.subTest(stderr=stderr):
                fake = FakeRun(done([], returncode=1, stderr=stderr))
                with self.assertRaises(units.Unreachable) as caught:
                    units.start(self.argv("u-1"), session, "u-1", run=fake)
                self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(units.started_units(session), [])

    def test_other_failures_are_units_error(self):
        session = self.session()
        for fake in (
            FakeRun(done([], returncode=1, stderr="Failed to start: bad property\n")),
            FakeRun(done([], returncode=1, stderr="Unit file does not exist.\n")),
            FakeRun(error=PermissionError("systemd-run")),
        ):
            with self.subTest(fake=fake), self.assertRaises(units.UnitsError) as caught:
                units.start(self.argv("u-1"), session, "u-1", run=fake)
            self.assertNotIsInstance(caught.exception, units.UnitExists)
            self.assertEqual(caught.exception.exit_code, 4)
        self.assertEqual(units.started_units(session), [])

    def test_the_argv_must_name_the_unit_and_the_unit_must_be_well_formed(self):
        session = self.session()
        fake = FakeRun()
        with self.assertRaises(units.UnitsError):
            units.start(
                ["systemd-run", "--user", "--unit=other-1", "omp"], session, "u-1", run=fake
            )
        for bad in ("u.service", "u-0", "u", "-u-1", "u-1 2", ""):
            with self.subTest(unit=bad), self.assertRaises(units.UnitsError):
                units.start(self.argv(bad), session, bad, run=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(units.started_units(session), [])

    def test_next_gen_follows_the_log_across_several_generations(self):
        session = self.session()
        self.assertEqual(units.next_gen(session), 0)
        self.assertEqual(units.started_units(session), [])
        fake = FakeRun()
        for gen in (0, 1, 3):
            name = units.unit_name("corral-agent-", "a-5", 2, gen)
            units.start(self.argv(name), session, name, run=fake)
        self.assertEqual(units.next_gen(session), 4)

    def test_gen_is_per_session_dir(self):
        first = self.session("one")
        second = self.session("two")
        units.start(self.argv("u-1"), first, "u-1", run=FakeRun())
        units.start(self.argv("u-2"), first, "u-2", run=FakeRun())
        units.start(self.argv("v-1"), second, "v-1", run=FakeRun())
        self.assertEqual(units.next_gen(first), 1)
        self.assertEqual(units.next_gen(second), 1)

    def test_an_unreadable_log_is_an_error_not_a_fresh_start(self):
        session = self.session()
        os.mkdir(os.path.join(session, units.UNITS_LOG))
        with self.assertRaises(units.UnitsError):
            units.next_gen(session)


class StopTest(Dirs):
    def setUp(self):
        super().setUp()
        self.listing = [
            ("corral-agent-lib-5", "active", "running"),
            ("corral-agent-lib-5-r1", "activating", "start"),
            ("corral-agent-lib-5-r2", "inactive", "dead"),
            ("corral-agent-lib-50", "active", "running"),
            ("corral-agent-my-repo-5", "active", "running"),
            ("corral-agent-a-5-5", "active", "running"),
            ("corral-agent-a-5-5-r1", "deactivating", "stop-sigterm"),
            ("other-agent-lib-5", "active", "running"),
        ]
        self.logged = [
            "corral-agent-lib-5",
            "corral-agent-lib-5-r1",
            "corral-agent-lib-5-r2",
            "corral-agent-a-5-5",
            "corral-agent-a-5-5-r1",
        ]
        self.session = self.log(self.logged)
        self.stopped = []

    def log(self, names, name="ours"):
        path = self.session_path(name)
        with open(os.path.join(path, units.UNITS_LOG), "w", encoding="utf-8") as handle:
            handle.write("".join(n + "\n" for n in names))
        return path

    def session_path(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    def list_fn(self, prefix):
        self.assertEqual(prefix, PREFIX)
        return self.listing

    def stop_fn(self, unit):
        self.stopped.append(unit)

    def stop(self, repo, number, session=None):
        return units.stop(
            PREFIX,
            repo,
            number,
            session_dir=session or self.session,
            list_fn=self.list_fn,
            stop_fn=self.stop_fn,
        )

    def test_stops_every_live_logged_generation_of_one_ticket(self):
        found = self.stop("lib", 5)
        self.assertEqual(found, ["corral-agent-lib-5", "corral-agent-lib-5-r1"])
        self.assertEqual(self.stopped, found)

    def test_other_repos_and_tickets_are_untouched(self):
        self.assertEqual(self.stop("a-5", 5), ["corral-agent-a-5-5", "corral-agent-a-5-5-r1"])
        self.assertEqual(self.stopped, ["corral-agent-a-5-5", "corral-agent-a-5-5-r1"])
        self.stopped.clear()
        self.assertEqual(self.stop("my-repo", 5), [])
        self.assertEqual(self.stop("lib", 7), [])
        self.assertEqual(self.stop("other", 5), [])
        self.assertEqual(self.stopped, [])

    def test_a_colliding_prefix_unit_is_never_stopped_without_proof(self):
        # With the prefix "corral-agent-", the other fleet's unit parses as
        # ("x-repo", 5); only the units.log shows it is not ours.
        other = "corral-agent-x-repo-5"
        self.assertEqual(units.parse_unit(PREFIX, other), ("x-repo", 5, 0))
        self.assertEqual(units.parse_unit("corral-agent-x-", other), ("repo", 5, 0))
        self.listing.append((other, "active", "running"))
        session = self.log([], name="ticket-x-repo")
        self.assertEqual(self.stop("x-repo", 5, session=session), [])
        self.assertEqual(self.stopped, [])
        self.assertEqual(
            units.unproven(PREFIX, "x-repo", 5, session_dir=session, list_fn=self.list_fn),
            [other],
        )
        # Only when this ticket's own log records the unit is it stopped.
        session = self.log([other], name="ticket-x-repo-owned")
        self.assertEqual(self.stop("x-repo", 5, session=session), [other])

    def test_a_live_unit_missing_from_the_log_is_not_stopped_but_reported(self):
        session = self.log([], name="unlogged")
        self.assertEqual(self.stop("lib", 5, session=session), [])
        self.assertEqual(self.stopped, [])
        self.assertEqual(
            units.unproven(PREFIX, "lib", 5, session_dir=session, list_fn=self.list_fn),
            ["corral-agent-lib-5", "corral-agent-lib-5-r1"],
        )

    def test_a_failure_partway_names_the_units_already_stopped(self):
        def failing(unit):
            if unit == "corral-agent-lib-5-r1":
                raise units.Unreachable("no bus")
            self.stopped.append(unit)

        with self.assertRaises(units.Unreachable) as caught:
            units.stop(
                PREFIX,
                "lib",
                5,
                session_dir=self.session,
                list_fn=self.list_fn,
                stop_fn=failing,
            )
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("corral-agent-lib-5", str(caught.exception))
        self.assertIn("corral-agent-lib-5-r1", str(caught.exception))
        self.assertEqual(self.stopped, ["corral-agent-lib-5"])

    def test_bad_arguments_are_refused_before_anything_is_listed(self):
        def never(_prefix):
            self.fail("list_fn must not run for bad arguments")

        for repo, number in (("../lib", 5), ("lib.git", 5), ("lib", True), ("lib", "5")):
            with self.subTest(repo=repo, number=number), self.assertRaises(units.UnitsError):
                units.stop(
                    PREFIX,
                    repo,
                    number,
                    session_dir=self.session,
                    list_fn=never,
                    stop_fn=self.stop_fn,
                )
        self.assertEqual(self.stopped, [])

    def test_every_argument_is_required(self):
        full = {
            "session_dir": self.session,
            "list_fn": self.list_fn,
            "stop_fn": self.stop_fn,
        }
        for drop in full:
            kwargs = {k: v for k, v in full.items() if k != drop}
            with self.subTest(missing=drop), self.assertRaises(TypeError):
                units.stop(PREFIX, "lib", 5, **kwargs)
        self.assertEqual(self.stopped, [])

    def test_a_failed_list_stops_nothing(self):
        def unreachable(prefix):
            raise units.Unreachable("no bus")

        with self.assertRaises(units.Unreachable):
            units.stop(
                PREFIX,
                "lib",
                5,
                session_dir=self.session,
                list_fn=unreachable,
                stop_fn=self.stop_fn,
            )
        self.assertEqual(self.stopped, [])


class SealTest(unittest.TestCase):
    """Guards on the destructive seam and on the systemd command names in src."""

    @staticmethod
    def _modules(*parts):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        root = os.path.join(here, *parts)
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    yield name, ast.parse(handle.read())

    @staticmethod
    def _owners(tree):
        """Map each node id to the name of the innermost function holding it."""
        owner = {}
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef):
                for node in ast.walk(func):
                    owner[id(node)] = func.name
        return owner

    def test_src_names_systemctl_and_systemd_run_only_in_their_functions(self):
        found = set()
        for name, tree in self._modules("src", "corral"):
            owner = self._owners(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                where = owner.get(id(node), "<module>")
                for needle in ("systemctl", "systemd-run"):
                    if needle in node.value:
                        found.add((name, needle, where))
        self.assertEqual(
            found,
            {
                ("units.py", "systemctl", "list_units"),
                ("units.py", "systemctl", STOP_NAME),
                ("units.py", "systemd-run", "unit_argv"),
            },
        )

    def test_only_systemctl_stop_builds_a_stop_argv_in_src(self):
        found = set()
        for name, tree in self._modules("src", "corral"):
            for func in ast.walk(tree):
                if not isinstance(func, ast.FunctionDef):
                    continue
                for node in ast.walk(func):
                    if isinstance(node, (ast.List, ast.Tuple)):
                        strings = {
                            item.value
                            for item in ast.walk(node)
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        }
                        if {"systemctl", "stop"} <= strings:
                            found.add((name, func.name))
        self.assertEqual(found, {("units.py", STOP_NAME)})

    def test_no_test_reaches_the_production_stop(self):
        for name, tree in self._modules("tests"):
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    self.assertNotEqual(node.attr, STOP_NAME, name)
                elif isinstance(node, ast.Name):
                    self.assertNotEqual(node.id, STOP_NAME, name)
                elif isinstance(node, ast.alias):
                    self.assertFalse(node.name.endswith(STOP_NAME), name)
                    self.assertNotEqual(node.asname, STOP_NAME, name)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(STOP_NAME, node.value, name)


if __name__ == "__main__":
    unittest.main()
