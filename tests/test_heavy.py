"""The heavy lane, ticket 1: weighted slots, scope limits, takeoff routing and raw rows.

Config comes from the project's ``[heavy]`` table (parsed by
``check_table``); the lane budget is decided by ``lane_budget``; commands
run in a transient scope built by ``scope_argv`` through ``run_heavy``;
every run appends one ledger row. Tests inject fakes for the process
seams (``run``, ``teardown``, ``scope_cgroup``); the file reader, clock,
host and pressure readers default to the live ones. No test touches
systemd, ``/proc`` or ``/sys``; the state dir stays under ``KORAPY_STATE``.
"""

import inspect
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from korapy import gitenv, heavy, settings


def _sandbox(test):
    """The env sandbox the korapy tests share: temp dirs, no leaked config."""
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        del os.environ[name]
    for name in list(settings.REGISTRY.values()):
        os.environ.pop(name.env, None)
    tmp = tempfile.TemporaryDirectory(prefix="korapy-heavy-")
    test.addCleanup(tmp.cleanup)
    test.tmp = os.path.realpath(tmp.name)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


CPU_STAT = (
    "usage_usec 412700000\n"
    "user_usec 400000000\n"
    "system_usec 12700000\n"
    "nr_periods 0\n"
    "nr_throttled 3\n"
    "throttled_usec 12000\n"
)
CPU_PRESSURE = (
    "some avg10=2.50 avg60=1.00 avg300=0.50 total=8400000\n"
    "full avg10=1.00 avg60=0.50 avg300=0.20 total=1000000\n"
)
MEMORY_PRESSURE = (
    "some avg10=0.80 avg60=0.40 avg300=0.10 total=0\n"
    "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
)


class _FakeClock:
    """A clock returning scripted readings, one per call."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.readings[min(self.calls, len(self.readings)) - 1]


class _FakeRunner:
    """A `run` seam capturing its call and answering with a scripted process."""

    def __init__(self, code=0, stdout="", stderr=""):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append((list(argv), cwd, timeout))
        return subprocess.CompletedProcess(
            args=argv, returncode=self.code, stdout=self.stdout, stderr=self.stderr
        )


class HeavyCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)


class ConfigCase(HeavyCase):
    def test_no_table_reads_as_lane_off(self):
        self.assertIsNone(heavy.check_table(None, "korapy.toml"))

    def test_empty_table_reads_as_defaults_with_no_commands(self):
        cfg = heavy.check_table({}, "korapy.toml")
        self.assertEqual(cfg.default, heavy.Weight(cpu_m=1000, memory_mb=1000))
        self.assertEqual(cfg.commands, {})
        self.assertIsNone(cfg.cpu_m)
        self.assertIsNone(cfg.memory_mb)
        self.assertEqual(cfg.starved_ratio, 0.2)
        self.assertEqual(cfg.ledger_period, "month")

    def test_full_table_parses(self):
        cfg = heavy.check_table(
            {
                "cpu_m": 4000,
                "memory_mb": 8192,
                "default_weight": {"cpu_m": 2000, "memory_mb": 2048},
                "commands": {"make check": {"cpu_m": 3000, "memory_mb": 6144}},
                "starved_ratio": 0.25,
                "ledger_period": "month",
            },
            "korapy.toml",
        )
        self.assertEqual(cfg.cpu_m, 4000)
        self.assertEqual(cfg.memory_mb, 8192)
        self.assertEqual(cfg.default, heavy.Weight(cpu_m=2000, memory_mb=2048))
        self.assertEqual(cfg.commands, {"make check": heavy.Weight(cpu_m=3000, memory_mb=6144)})

    def test_unknown_key_is_refused_naming_it(self):
        with self.assertRaisesRegex(settings.SettingsError, "bogus"):
            heavy.check_table({"bogus": 1}, "korapy.toml")

    def test_non_table_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            heavy.check_table(["make check"], "korapy.toml")

    def test_caps_must_be_positive_whole_numbers(self):
        for key in ("cpu_m", "memory_mb"):
            for bad in (0, -1, True, "4000", 1.5):
                with self.subTest(key=key, value=bad):
                    with self.assertRaises(settings.SettingsError):
                        heavy.check_table({key: bad}, "korapy.toml")

    def test_integral_float_caps_read_as_ints(self):
        cfg = heavy.check_table({"cpu_m": 4000.0, "memory_mb": 8192.0}, "korapy.toml")
        self.assertEqual((cfg.cpu_m, cfg.memory_mb), (4000, 8192))

    def test_default_weight_needs_both_numbers(self):
        with self.assertRaises(settings.SettingsError):
            heavy.check_table({"default_weight": {"cpu_m": 1000}}, "korapy.toml")
        with self.assertRaises(settings.SettingsError):
            heavy.check_table({"default_weight": "1000"}, "korapy.toml")

    def test_command_entries_need_both_numbers(self):
        with self.assertRaises(settings.SettingsError):
            heavy.check_table({"commands": {"make check": {"cpu_m": 1}}}, "korapy.toml")
        with self.assertRaises(settings.SettingsError):
            heavy.check_table({"commands": {"": {"cpu_m": 1, "memory_mb": 2}}}, "p")
        with self.assertRaises(settings.SettingsError):
            heavy.check_table({"commands": ["make check"]}, "korapy.toml")

    def test_empty_command_entry_uses_the_default_weight(self):
        cfg = heavy.check_table(
            {
                "default_weight": {"cpu_m": 1000, "memory_mb": 1024},
                "commands": {"make build": {}},
            },
            "korapy.toml",
        )
        self.assertTrue(heavy.is_heavy(cfg, "make build"))
        self.assertEqual(heavy.weight_for(cfg, "make build"), heavy.Weight(1000, 1024))

    def test_starved_ratio_out_of_range_is_refused(self):
        for bad in (-0.1, 1.5, "half", True):
            with self.subTest(value=bad):
                with self.assertRaises(settings.SettingsError):
                    heavy.check_table({"starved_ratio": bad}, "korapy.toml")

    def test_ledger_period_other_than_month_is_refused(self):
        with self.assertRaisesRegex(settings.SettingsError, "month"):
            heavy.check_table({"ledger_period": "week"}, "korapy.toml")

    def test_project_table_is_carried_raw_for_the_lane(self):
        resolved = settings.resolve(project={"heavy": {"cpu_m": 4000, "commands": {}}}, env={})
        self.assertEqual(resolved.heavy, {"cpu_m": 4000, "commands": {}})
        cfg = heavy.check_table(resolved.heavy, "korapy.toml")
        self.assertEqual(cfg.cpu_m, 4000)
        self.assertEqual(cfg.commands, {})

    def test_absent_table_carries_empty(self):
        resolved = settings.resolve(project={}, env={})
        self.assertEqual(resolved.heavy, {})
        self.assertIsNone(heavy.check_table(resolved.heavy or None, "p"))


class RoutingCase(HeavyCase):
    def test_listed_command_is_heavy(self):
        cfg = heavy.check_table(
            {"commands": {"make check": {"cpu_m": 3000, "memory_mb": 6144}}}, "p"
        )
        self.assertTrue(heavy.is_heavy(cfg, "make check"))
        self.assertFalse(heavy.is_heavy(cfg, "make lint"))
        self.assertFalse(heavy.is_heavy(cfg, "make check --extra"))

    def test_weight_prefers_the_command_entry(self):
        cfg = heavy.check_table(
            {
                "default_weight": {"cpu_m": 1000, "memory_mb": 1024},
                "commands": {"make check": {"cpu_m": 3000, "memory_mb": 6144}},
            },
            "p",
        )
        self.assertEqual(heavy.weight_for(cfg, "make check"), heavy.Weight(3000, 6144))
        self.assertEqual(heavy.weight_for(cfg, "make lint"), heavy.Weight(1000, 1024))

    def test_routing_without_config_never_matches(self):
        self.assertFalse(heavy.is_heavy(None, "make check"))


class BudgetCase(HeavyCase):
    def _host(self):
        from korapy import resources

        return resources.Usage(cpu_m=8000, memory_mb=16384, pids=200)

    def test_no_config_means_no_budget(self):
        self.assertIsNone(heavy.lane_budget(None, self._host(), where="host config"))

    def test_no_host_budget_means_no_enforcement(self):
        cfg = heavy.check_table({"cpu_m": 4000, "memory_mb": 8192}, "p")
        self.assertIsNone(heavy.lane_budget(cfg, None, where="host config"))

    def test_missing_caps_default_to_the_host_budget(self):
        from korapy import resources

        cfg = heavy.check_table({}, "p")
        budget = heavy.lane_budget(cfg, self._host(), where="host config")
        self.assertEqual(budget, resources.Usage(cpu_m=8000, memory_mb=16384, pids=0))

    def test_fitting_caps_win(self):
        from korapy import resources

        cfg = heavy.check_table({"cpu_m": 4000, "memory_mb": 8192}, "p")
        budget = heavy.lane_budget(cfg, self._host(), where="host config")
        self.assertEqual(budget, resources.Usage(cpu_m=4000, memory_mb=8192, pids=0))

    def test_a_cap_above_the_host_is_refused(self):
        cfg = heavy.check_table({"cpu_m": 9000, "memory_mb": 8192}, "p")
        with self.assertRaisesRegex(settings.SettingsError, "9000"):
            heavy.lane_budget(cfg, self._host(), where="host config")
        cfg = heavy.check_table({"cpu_m": 4000, "memory_mb": 20000}, "p")
        with self.assertRaisesRegex(settings.SettingsError, "20000"):
            heavy.lane_budget(cfg, self._host(), where="host config")

    def test_a_cap_equal_to_the_host_fits(self):
        from korapy import resources

        cfg = heavy.check_table({"cpu_m": 8000, "memory_mb": 16384}, "p")
        budget = heavy.lane_budget(cfg, self._host(), where="host config")
        self.assertEqual(budget, resources.Usage(cpu_m=8000, memory_mb=16384, pids=0))


class ScopeArgvCase(HeavyCase):
    def test_full_argv_pins_the_shape(self):
        argv = heavy.scope_argv(
            "heavy-abc123",
            ["make", "check"],
            "/work/wt",
            {"PATH": "/usr/bin:/bin", "HOME": "/home/someone"},
            memory_max=6144,
            cpu_quota=3000,
        )
        self.assertEqual(
            argv,
            [
                "systemd-run",
                "--user",
                "--scope",
                "--wait",
                "--unit=heavy-abc123",
                "--property=MemoryMax=6144M",
                "--property=CPUQuota=300%",
                "--working-directory=/work/wt",
                "--setenv=PATH=/usr/bin:/bin",
                "--setenv=HOME=/home/someone",
                "make",
                "check",
            ],
        )

    def test_no_limits_sets_no_properties(self):
        argv = heavy.scope_argv("heavy-x", ["true"], "/w", {"PATH": "p", "HOME": "h"})
        self.assertNotIn("--property=MemoryMax=1M", argv)
        self.assertEqual([a for a in argv if a.startswith("--property=")], [])

    def test_controllers_skip_the_properties_they_do_not_cover(self):
        argv = heavy.scope_argv(
            "heavy-x",
            ["true"],
            "/w",
            {},
            memory_max=100,
            cpu_quota=200,
            controllers={"memory"},
        )
        self.assertIn("--property=MemoryMax=100M", argv)
        self.assertEqual([a for a in argv if "CPUQuota" in a], [])

    def test_cpu_quota_renders_millicores_as_percent(self):
        argv = heavy.scope_argv("heavy-x", ["true"], "/w", {}, cpu_quota=2500, controllers=None)
        self.assertIn("--property=CPUQuota=250%", argv)

    def test_bad_unit_is_refused(self):
        for bad in ("", "heavy a", "heavy/x", "-heavy"):
            with self.subTest(unit=bad):
                with self.assertRaises(heavy.HeavyError):
                    heavy.scope_argv(bad, ["true"], "/w", {})

    def test_command_must_start_with_a_program(self):
        with self.assertRaises(heavy.HeavyError):
            heavy.scope_argv("heavy-x", [], "/w", {})
        with self.assertRaises(heavy.HeavyError):
            heavy.scope_argv("heavy-x", ["-x"], "/w", {})
        with self.assertRaises(heavy.HeavyError):
            heavy.scope_argv("heavy-x", [["make"]], "/w", {})


class RepoKeyCase(HeavyCase):
    def _repo(self, name="myrepo", with_file=True):
        top = os.path.join(self.tmp, "clone")
        os.makedirs(os.path.join(top, "sub", "deep"))
        subprocess.run(["git", "init", "-q", "-b", "main", top], check=True)
        subprocess.run(["git", "-C", top, "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", top, "config", "user.name", "t"], check=True)
        if with_file:
            with open(os.path.join(top, "korapy.toml"), "w", encoding="utf-8") as fh:
                fh.write(f'[[repo]]\nname = "{name}"\n')
            subprocess.run(["git", "-C", top, "add", "-A"], check=True)
            subprocess.run(["git", "-C", top, "commit", "-q", "-m", "x"], check=True)
        return top

    def test_checkout_file_name_wins_from_a_subdirectory(self):
        top = self._repo()
        self.assertEqual(heavy.repo_key(os.path.join(top, "sub", "deep")), "myrepo")

    def test_missing_file_reads_as_the_directory_name(self):
        top = self._repo(with_file=False)
        self.assertEqual(heavy.repo_key(os.path.join(top, "sub")), "clone")

    def test_linked_worktree_resolves_to_the_main_checkout(self):
        top = self._repo()
        work = os.path.join(self.tmp, "wt")
        subprocess.run(
            ["git", "-C", top, "worktree", "add", work, "-b", "feat/1-x"],
            check=True,
            capture_output=True,
        )
        self.assertEqual(heavy.repo_key(work), "myrepo")

    def test_linked_worktree_without_a_file_uses_the_main_name(self):
        top = self._repo(with_file=False)
        work = os.path.join(self.tmp, "wt")
        subprocess.run(
            ["git", "-C", top, "worktree", "add", work, "-b", "feat/1-x"],
            check=True,
            capture_output=True,
        )
        self.assertEqual(heavy.repo_key(work), "clone")

    def test_corrupt_file_raises(self):
        top = self._repo()
        with open(os.path.join(top, "korapy.toml"), "w", encoding="utf-8") as fh:
            fh.write("[[repo\n")
        with self.assertRaises(settings.SettingsError):
            heavy.repo_key(top)

    def test_gitenv_resolves_the_main_checkout_without_git(self):
        top = self._repo()
        self.assertIsNone(gitenv.worktree_main_checkout(top))


class ArgvHashCase(HeavyCase):
    def test_spacing_ambiguity_hashes_differently(self):
        self.assertNotEqual(heavy.argv_hash(["a b", "c"]), heavy.argv_hash(["a", "b c"]))

    def test_hash_is_a_stable_hex_digest(self):
        first = heavy.argv_hash(["make", "check"])
        self.assertEqual(first, heavy.argv_hash(["make", "check"]))
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_hash_pins_the_nul_joined_layout(self):
        import hashlib

        self.assertEqual(
            heavy.argv_hash(["make", "check"]),
            hashlib.sha256(b"make\x00check\x00").hexdigest(),
        )


class HostLabelCase(HeavyCase):
    def test_label_is_created_once_as_hex(self):
        first = heavy.host_label()
        path = os.path.join(os.environ["KORAPY_STATE"], "heavy", "host-id")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first)
        int(first, 16)
        self.assertEqual(heavy.host_label(), first)

    def test_ledger_path_names_host_and_period(self):
        label = "9c2e0123456789abcdef0123456789ab"
        path = heavy.ledger_path(label=label, period="2026-11")
        self.assertEqual(
            path,
            os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs", f"{label}-2026-11.jsonl"),
        )

    def test_hostile_stored_label_is_refused(self):
        root = os.path.join(os.environ["KORAPY_STATE"], "heavy")
        os.makedirs(root, exist_ok=True)
        for hostile in (
            "../escape",
            "/absolute",
            "9c2e",
            "9C2E0123456789ABCDEF0123456789AB",
            "x" * 32,
        ):
            with self.subTest(label=hostile):
                with open(os.path.join(root, "host-id"), "w", encoding="utf-8") as fh:
                    fh.write(hostile + "\n")
                with self.assertRaises(heavy.HeavyError):
                    heavy.host_label()

    def test_hostile_explicit_label_is_refused(self):
        with self.assertRaises(heavy.HeavyError):
            heavy.ledger_path(label="../escape", period="2026-11")


class HostInfoCase(HeavyCase):
    def test_host_info_reads_the_fixtures(self):
        info = heavy.host_info(
            read=lambda path: "MemTotal:       16777216 kB\nMemAvailable: 8000000 kB\n",
            cpu_count=8,
            python_version="3.11",
        )
        self.assertEqual(info, {"cores": 8, "mem_mb": 16384.0, "python": "3.11"})

    def test_missing_core_count_defaults_to_the_live_count(self):
        def boom(path):
            raise OSError("gone")

        info = heavy.host_info(read=boom, python_version="3.11")
        self.assertIsInstance(info["cores"], int)
        self.assertIsNone(info["mem_mb"])
        self.assertEqual(info["python"], "3.11")

    def test_bad_core_counts_read_as_unknown(self):
        for bad in (True, False, -1):
            with self.subTest(value=bad):
                info = heavy.host_info(
                    read=lambda path: "MemTotal:       16777216 kB\n",
                    cpu_count=bad,
                    python_version="3.11",
                )
                self.assertIsNone(info["cores"])
                self.assertEqual(info["mem_mb"], 16384.0)


class RunHeavyCase(HeavyCase):
    def _cgroup(self, files):
        root = os.path.join(self.tmp, "cgroup")
        os.makedirs(root)
        for name, text in files.items():
            with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        return root

    def _standard_cgroup(self):
        return self._cgroup(
            {
                "cpu.stat": CPU_STAT,
                "memory.peak": "5463080960\n",
                "pids.peak": "96\n",
                "cpu.pressure": CPU_PRESSURE,
                "memory.pressure": MEMORY_PRESSURE,
            }
        )

    def _run_kwargs(self, run, **overrides):
        from korapy import resources

        kwargs = {
            "role": "takeoff",
            "repo": "myrepo",
            "weight": heavy.Weight(cpu_m=3000, memory_mb=6144),
            "budget": resources.Usage(cpu_m=4000, memory_mb=8192, pids=0),
            "run": run,
            "teardown": mock.Mock(),
            "read": None,
            "clock": _FakeClock([1000.0, 1182.4]),
            "host": {"cores": 8, "mem_mb": 16384.0, "python": "3.11"},
            "pressure": resources.Pressure(
                load_per_cpu=0.4,
                mem_available_pct=62.0,
                saturated=False,
                cpu_some_avg10=2.5,
                memory_some_avg10=0.8,
            ),
            "timeout": 1800,
        }
        if "scope_cgroup" not in overrides:
            kwargs["scope_cgroup"] = mock.Mock(return_value=self._standard_cgroup())
        kwargs.update(overrides)
        return kwargs

    def _ledger_rows(self):
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        self.assertTrue(os.path.isdir(runs), "no ledger dir was written")
        paths = sorted(
            os.path.join(runs, name) for name in os.listdir(runs) if name.endswith(".jsonl")
        )
        self.assertEqual(len(paths), 1, paths)
        with open(paths[0], encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_happy_path_records_the_pinned_row(self):
        run = _FakeRunner(code=0, stdout="green\n", stderr="")
        result = heavy.run_heavy(
            ["make", "check"], self.tmp, {"PATH": "p", "HOME": "h"}, **self._run_kwargs(run)
        )
        self.assertEqual(result.command_exit, 0)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.weight, heavy.Weight(cpu_m=3000, memory_mb=6144))
        self.assertEqual(result.wall_s, 182.4)
        self.assertEqual(result.argv_hash, heavy.argv_hash(["make", "check"]))
        self.assertTrue(result.unit.startswith("heavy-"))
        argv, cwd, timeout = run.calls[0]
        self.assertEqual(cwd, self.tmp)
        self.assertEqual(timeout, 1800)
        self.assertEqual(
            argv[:5], ["systemd-run", "--user", "--scope", "--wait", f"--unit={result.unit}"]
        )
        self.assertIn("--property=MemoryMax=6144M", argv)
        self.assertIn("--property=CPUQuota=300%", argv)
        self.assertEqual(argv[-2:], ["make", "check"])
        with open(
            os.path.join(os.environ["KORAPY_STATE"], "heavy", "host-id"), encoding="utf-8"
        ) as fh:
            label = fh.read()
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0],
            {
                "schema_version": 1,
                "at": "1970-01-01T00:16:40+00:00",
                "host": {
                    "label": label,
                    "cores": 8,
                    "mem_mb": 16384.0,
                    "python": "3.11",
                },
                "repo": "myrepo",
                "argv_hash": heavy.argv_hash(["make", "check"]),
                "role": "takeoff",
                "argv": ["make", "check"],
                "wall_s": 182.4,
                "cpu_s": 412.7,
                "peak_mb": 5210.0,
                "pids_peak": 96,
                "avg_cores": 2.263,
                "throttled_us": 12000,
                "cpu_pressure_us": 8400000,
                "mem_pressure_us": 0,
                "starved": False,
                "exit": 0,
                "waited_s": 0.0,
                "weight": {"cpu_m": 3000, "memory_mb": 6144},
                "host_pressure": {
                    "load_per_cpu": 0.4,
                    "mem_available_pct": 62.0,
                    "saturated": False,
                    "cpu_some_avg10": 2.5,
                    "memory_some_avg10": 0.8,
                },
            },
        )

    def test_vanished_cgroup_records_null_measurement(self):
        run = _FakeRunner(code=0, stdout="green\n", stderr="")
        result = heavy.run_heavy(
            ["make", "check"],
            self.tmp,
            {},
            **self._run_kwargs(run, scope_cgroup=mock.Mock(return_value=None)),
        )
        self.assertEqual(result.exit_code, 0)
        (row,) = self._ledger_rows()
        self.assertEqual(row["exit"], 0)
        for key in (
            "cpu_s",
            "peak_mb",
            "pids_peak",
            "avg_cores",
            "throttled_us",
            "cpu_pressure_us",
            "mem_pressure_us",
        ):
            self.assertIsNone(row[key], key)
        self.assertFalse(row["starved"])
        self.assertEqual(row["weight"], {"cpu_m": 3000, "memory_mb": 6144})

    def test_public_exits_pass_through_and_others_map_to_1(self):
        for code, mapped in (
            (0, 0),
            (1, 1),
            (2, 2),
            (3, 3),
            (4, 4),
            (75, 75),
            (5, 1),
            (127, 1),
            (255, 1),
        ):
            with self.subTest(code=code):
                run = _FakeRunner(code=code, stdout="out\n", stderr="err\n")
                result = heavy.run_heavy(
                    ["make", "check"],
                    self.tmp,
                    {},
                    **self._run_kwargs(run, scope_cgroup=mock.Mock(return_value=None)),
                )
                self.assertEqual(result.command_exit, code)
                self.assertEqual(result.exit_code, mapped)
                self.assertIn("out", result.output)
                self.assertIn("err", result.output)

    def test_timeout_propagates_after_teardown_without_a_row(self):
        import subprocess as sp

        def hanging(argv, cwd, timeout):
            raise sp.TimeoutExpired(argv, timeout)

        teardown = mock.Mock()
        with self.assertRaises(sp.TimeoutExpired):
            heavy.run_heavy(
                ["make", "check"], self.tmp, {}, **self._run_kwargs(hanging, teardown=teardown)
            )
        teardown.assert_called_once()
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        self.assertFalse(os.path.exists(runs))

    def test_unreachable_manager_raises_with_exit_3(self):
        run = _FakeRunner(code=1, stderr="Failed to connect to bus: no medium found\n")
        teardown = mock.Mock()
        with self.assertRaises(heavy.ScopeUnreachable) as ctx:
            heavy.run_heavy(
                ["make", "check"],
                self.tmp,
                {},
                **self._run_kwargs(run, teardown=teardown),
            )
        self.assertEqual(ctx.exception.exit_code, 3)
        teardown.assert_not_called()
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        self.assertFalse(os.path.exists(runs))

    def test_failed_scope_start_raises_a_usage_error(self):
        run = _FakeRunner(code=1, stderr="Failed to start transient scope unit: bad property\n")
        with self.assertRaises(heavy.HeavyError) as ctx:
            heavy.run_heavy(["make", "check"], self.tmp, {}, **self._run_kwargs(run))
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_missing_scope_program_is_a_usage_error_without_a_row(self):
        def missing(argv, cwd, timeout):
            raise FileNotFoundError("systemd-run")

        teardown = mock.Mock()
        with self.assertRaisesRegex(heavy.HeavyError, "systemd-run") as ctx:
            heavy.run_heavy(
                ["make", "check"], self.tmp, {}, **self._run_kwargs(missing, teardown=teardown)
            )
        self.assertEqual(ctx.exception.exit_code, 4)
        teardown.assert_not_called()
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        self.assertFalse(os.path.exists(runs))

    def test_default_run_raises_when_the_program_is_missing(self):
        with self.assertRaises(OSError):
            heavy.default_run(["korapy-no-such-program", "--version"], self.tmp, 30)

    def test_manager_like_command_stderr_reads_as_a_lane_failure(self):
        # The twin the units start path accepts: a command whose own
        # stderr names a manager failure verbatim reads as one.
        run = _FakeRunner(code=1, stderr="Failed to start transient scope unit: nope\n")
        with self.assertRaises(heavy.HeavyError):
            heavy.run_heavy(["make", "check"], self.tmp, {}, **self._run_kwargs(run))

    def test_no_budget_runs_unlimited_in_a_scope(self):
        run = _FakeRunner(code=0)
        result = heavy.run_heavy(
            ["make", "check"], self.tmp, {}, **self._run_kwargs(run, budget=None)
        )
        self.assertEqual(result.exit_code, 0)
        argv = run.calls[0][0]
        self.assertEqual([a for a in argv if a.startswith("--property=")], [])

    def test_starved_sample_flags_but_keeps_its_peak(self):
        root = self._cgroup(
            {
                "cpu.stat": CPU_STAT,
                "memory.peak": "5463080960\n",
                "pids.peak": "96\n",
                "cpu.pressure": CPU_PRESSURE.replace("total=8400000", "total=90000000"),
                "memory.pressure": MEMORY_PRESSURE,
            }
        )
        run = _FakeRunner(code=0)
        heavy.run_heavy(
            ["make", "check"],
            self.tmp,
            {},
            **self._run_kwargs(run, scope_cgroup=mock.Mock(return_value=root)),
        )
        (row,) = self._ledger_rows()
        self.assertTrue(row["starved"])
        self.assertEqual(row["peak_mb"], 5210.0)

    def test_empty_role_is_refused(self):
        run = _FakeRunner()
        with self.assertRaises(heavy.HeavyError):
            heavy.run_heavy(["make", "check"], self.tmp, {}, **self._run_kwargs(run, role=""))

    def test_non_positive_timeout_is_refused(self):
        run = _FakeRunner()
        with self.assertRaises(heavy.HeavyError):
            heavy.run_heavy(["make", "check"], self.tmp, {}, **self._run_kwargs(run, timeout=0))

    def test_control_seams_are_required(self):
        params = inspect.signature(heavy.run_heavy).parameters
        for name in ("run", "teardown", "scope_cgroup"):
            with self.subTest(seam=name):
                self.assertIs(params[name].default, inspect.Parameter.empty)

    def test_teardown_failure_is_loud_after_the_row_lands(self):
        run = _FakeRunner(code=0)

        def failing(unit):
            raise heavy.HeavyError("cannot stop scope")

        with self.assertRaises(heavy.HeavyError):
            heavy.run_heavy(
                ["make", "check"],
                self.tmp,
                {},
                **self._run_kwargs(
                    run,
                    scope_cgroup=mock.Mock(return_value=None),
                    teardown=failing,
                ),
            )
        (row,) = self._ledger_rows()
        self.assertEqual(row["exit"], 0)


if __name__ == "__main__":
    unittest.main()
