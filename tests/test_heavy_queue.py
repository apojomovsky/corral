"""The heavy lane, ticket 2: the queue file, waiters, status and plan.

`lane.json` plus its lock sidecar is the only queue: join appends one
waiter row, the admit loop promotes the fitting head to a holder, and
release removes the holder row. `heavy run --plan` reports the verdict
without queueing anything; `heavy status` reads holders, waiters and
learned costs. Past the wait deadline the waiter leaves with exit 75; a
busy lock refuses with 75; a corrupt file refuses with exit 4 and is
never reset. Tests inject fakes at every seam (clock, sleep, pid, uuid,
file locks); no test touches systemd, `/proc`, the network or real
state dirs.
"""

import contextlib
import fcntl
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from korapy import heavy, resources, settings


def _sandbox(test):
    """The env sandbox the korapy tests share: temp dirs, no leaked config."""
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        del os.environ[name]
    for name in list(settings.REGISTRY.values()):
        os.environ.pop(name.env, None)
    tmp = tempfile.TemporaryDirectory(prefix="korapy-heavy-queue-")
    test.addCleanup(tmp.cleanup)
    test.tmp = os.path.realpath(tmp.name)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


ARRIVED = 1_700_000_000.0
ARRIVED_ISO = "2023-11-14T22:13:20+00:00"
HOLDER_ID = "1" * 32
WAITER_ID = "2" * 32
WEIGHT = heavy.Weight(cpu_m=3000, memory_mb=6144)
SMALL = heavy.Weight(cpu_m=1000, memory_mb=1024)
BUDGET = resources.Usage(cpu_m=4000, memory_mb=8192, pids=0)


def _budget_dict(usage):
    if usage is None:
        return None
    return {"cpu_m": usage.cpu_m, "memory_mb": usage.memory_mb}


class _LaneClock:
    """A scripted wall clock: `sleep` advances it, so waits run instantly."""

    def __init__(self, start):
        self.now_value = start
        self.sleeps = []

    def __call__(self):
        return self.now_value

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now_value += secs


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


class QueueCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _join(self, command=("make", "check"), weight=WEIGHT, **overrides):
        params = {
            "weight": weight,
            "budget": BUDGET,
            "arrived": ARRIVED,
            "pid": 4242,
            "timeout_s": 600,
        }
        params.update(overrides)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=HOLDER_ID)):
            return heavy.join_lane(list(command), **params)


class ConfigCase(QueueCase):
    def test_no_wait_timeout_defaults_to_1800(self):
        cfg = heavy.check_table({}, "korapy.toml")
        self.assertEqual(cfg.wait_timeout_s, 1800)

    def test_wait_timeout_parses(self):
        cfg = heavy.check_table({"wait_timeout_s": 3600}, "korapy.toml")
        self.assertEqual(cfg.wait_timeout_s, 3600)

    def test_integral_float_wait_timeout_reads_as_int(self):
        cfg = heavy.check_table({"wait_timeout_s": 60.0}, "korapy.toml")
        self.assertEqual(cfg.wait_timeout_s, 60)

    def test_wait_timeout_must_be_positive(self):
        for bad in (0, -5, "soon", True, 1.5, None):
            with self.subTest(value=bad):
                with self.assertRaises(settings.SettingsError):
                    heavy.check_table({"wait_timeout_s": bad}, "korapy.toml")

    def test_unknown_heavy_key_still_refused(self):
        with self.assertRaisesRegex(settings.SettingsError, "bogus"):
            heavy.check_table({"bogus": 1}, "korapy.toml")


class PathsCase(QueueCase):
    def test_lane_and_lock_paths_live_under_the_heavy_dir(self):
        self.assertEqual(
            heavy.lane_path(),
            os.path.join(os.environ["KORAPY_STATE"], "heavy", "lane.json"),
        )
        self.assertEqual(
            heavy.lock_path(),
            os.path.join(os.environ["KORAPY_STATE"], "heavy", "lane.lock"),
        )


class LaneFileCase(QueueCase):
    def test_missing_file_reads_as_an_empty_lane(self):
        self.assertEqual(
            heavy.read_lane(),
            {
                "schema_version": 1,
                "budget": None,
                "holders": [],
                "waiters": [],
                "costs": {},
                "recent": [],
            },
        )

    def test_corrupt_file_is_refused_naming_it(self):
        path = heavy.lane_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json{{{")
        with self.assertRaisesRegex(heavy.HeavyError, "lane.json") as ctx:
            heavy.read_lane()
        self.assertEqual(ctx.exception.exit_code, 4)
        with self.assertRaises(heavy.HeavyError):
            self._join()
        with self.assertRaises(heavy.HeavyError):
            heavy.release_lane(HOLDER_ID, BUDGET)

    def test_corrupt_file_is_never_reset(self):
        path = heavy.lane_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json{{{")
        for _ in range(2):
            with self.assertRaises(heavy.HeavyError):
                self._join()
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "not json{{{")
        self.assertFalse(os.path.exists(path + ".corrupt-"))

    def test_wrong_schema_version_is_refused(self):
        path = heavy.lane_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 2, "holders": [], "waiters": []}, handle)
        with self.assertRaisesRegex(heavy.HeavyError, "schema_version"):
            heavy.read_lane()

    def test_unknown_fields_are_ignored(self):
        path = heavy.lane_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "budget": None,
                    "holders": [],
                    "waiters": [],
                    "costs": {},
                    "recent": [],
                    "future": {"rows": []},
                },
                handle,
            )
        self.assertEqual(heavy.read_lane()["waiters"], [])

    def test_busy_lock_refuses_75(self):
        path = heavy.lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        held = open(path, "a+")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(heavy.LaneLocked) as ctx:
            self._join(lock_timeout=0.1)
        self.assertEqual(ctx.exception.exit_code, 75)


class JoinCase(QueueCase):
    def test_join_appends_the_waiter_row(self):
        row = self._join()
        self.assertEqual(row["id"], HOLDER_ID)
        self.assertEqual(row["pid"], 4242)
        self.assertEqual(row["command"], ["make", "check"])
        self.assertEqual(row["argv_hash"], heavy.argv_hash(["make", "check"]))
        self.assertEqual(row["weight"], {"cpu_m": 3000, "memory_mb": 6144})
        self.assertEqual(row["arrived_at"], ARRIVED_ISO)
        self.assertEqual(row["deadline_at"], "2023-11-14T22:23:20+00:00")
        self.assertEqual(row["skipped"], 0)
        lane = heavy.read_lane()
        self.assertEqual(lane["waiters"], [row])
        self.assertEqual(lane["holders"], [])
        self.assertEqual(lane["budget"], {"cpu_m": 4000, "memory_mb": 8192})

    def test_join_without_a_budget_caches_null(self):
        self._join(budget=None)
        self.assertIsNone(heavy.read_lane()["budget"])

    def test_zero_timeout_deadline_equals_arrival(self):
        row = self._join(timeout_s=0)
        self.assertEqual(row["deadline_at"], row["arrived_at"])

    def test_negative_timeout_is_refused(self):
        with self.assertRaises(heavy.HeavyError):
            self._join(timeout_s=-5)

    def test_nonfinite_timeout_is_refused(self):
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(timeout=bad), self.assertRaises(heavy.HeavyError):
                self._join(timeout_s=bad)

    def test_join_creates_the_lane_dir(self):
        self._join()
        self.assertTrue(os.path.isfile(heavy.lane_path()))


class FitPlanCase(QueueCase):
    def _lane(self, holders=(), waiters=()):
        return {
            "schema_version": 1,
            "budget": _budget_dict(BUDGET),
            "holders": list(holders),
            "waiters": list(waiters),
            "costs": {},
            "recent": [],
        }

    def _waiter(self, weight=WEIGHT, arrived=ARRIVED):
        return {
            "id": WAITER_ID,
            "pid": 4242,
            "command": ["make", "check"],
            "argv_hash": heavy.argv_hash(["make", "check"]),
            "weight": {"cpu_m": weight.cpu_m, "memory_mb": weight.memory_mb},
            "arrived_at": ARRIVED_ISO,
            "deadline_at": ARRIVED_ISO,
            "skipped": 0,
        }

    def test_no_budget_admits_everything(self):
        lane = self._lane()
        report = heavy.plan_report(WEIGHT, lane, None)
        self.assertTrue(report["admitted"])
        self.assertEqual(report["position"], 0)
        self.assertIn("no lane budget", report["reason"])

    def test_fitting_head_admits(self):
        report = heavy.plan_report(WEIGHT, self._lane(), BUDGET)
        self.assertTrue(report["admitted"])
        self.assertEqual(report["position"], 0)
        self.assertEqual(report["weight"], {"cpu_m": 3000, "memory_mb": 6144})
        self.assertIn("slot free", report["reason"])

    def test_over_budget_refuses_with_numbers(self):
        report = heavy.plan_report(heavy.Weight(cpu_m=9000, memory_mb=100), self._lane(), BUDGET)
        self.assertFalse(report["admitted"])
        self.assertEqual(report["position"], 0)
        self.assertIn("9000", report["reason"])

    def test_waiters_ahead_block_a_fitting_request(self):
        lane = self._lane(waiters=[self._waiter()])
        report = heavy.plan_report(SMALL, lane, BUDGET)
        self.assertFalse(report["admitted"])
        self.assertEqual(report["position"], 1)
        self.assertIn(WAITER_ID[:8], report["reason"])

    def test_held_weight_counts_against_the_cap(self):
        holder = {
            "id": HOLDER_ID,
            "pid": 99,
            "command": ["make", "check"],
            "weight": {"cpu_m": 3000, "memory_mb": 6144},
            "since": ARRIVED_ISO,
            "eta_s": None,
        }
        lane = self._lane(holders=[holder])
        fitting = heavy.plan_report(heavy.Weight(cpu_m=1000, memory_mb=1024), lane, BUDGET)
        self.assertTrue(fitting["admitted"])
        over = heavy.plan_report(heavy.Weight(cpu_m=1001, memory_mb=1024), lane, BUDGET)
        self.assertFalse(over["admitted"])

    def test_plan_shape_pins_schema_and_weight(self):
        report = heavy.plan_report(WEIGHT, self._lane(), BUDGET)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(
            set(report), {"schema_version", "admitted", "position", "weight", "reason"}
        )


class StatusCase(QueueCase):
    def _lane_with_rows(self):
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=HOLDER_ID)):
            heavy.join_lane(
                ["make", "check"], WEIGHT, BUDGET, arrived=ARRIVED, pid=99, timeout_s=600
            )
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            heavy.join_lane(
                ["make", "build"], SMALL, BUDGET, arrived=ARRIVED, pid=4242, timeout_s=600
            )
        lane = heavy.read_lane()
        holder = dict(lane["waiters"][0])
        holder.update({"since": holder.pop("arrived_at"), "eta_s": None})
        for key in ("deadline_at", "skipped", "argv_hash", "pid"):
            holder.pop(key, None)
        lane["holders"] = [holder]
        lane["waiters"] = lane["waiters"][1:]
        return lane

    def test_status_matches_the_golden_file(self):
        lane = self._lane_with_rows()
        report = heavy.status_report(lane, ARRIVED + 12.0)
        golden = os.path.join(os.path.dirname(__file__), "golden", "heavy-status.json")
        with open(golden, encoding="utf-8") as handle:
            self.assertEqual(report, json.load(handle))

    def test_plan_matches_the_golden_file(self):
        lane = self._lane_with_rows()
        report = heavy.plan_report(SMALL, lane, BUDGET)
        golden = os.path.join(os.path.dirname(__file__), "golden", "heavy-plan.json")
        with open(golden, encoding="utf-8") as handle:
            self.assertEqual(report, json.load(handle))

    def test_waited_seconds_count_from_arrival(self):
        lane = self._lane_with_rows()
        report = heavy.status_report(lane, ARRIVED + 12.0)
        (waiter,) = report["waiters"]
        self.assertEqual(waiter["waited_s"], 12.0)
        self.assertEqual(waiter["skipped"], 0)
        self.assertIsNone(waiter["eta_s"])

    def test_empty_lane_status_is_empty_lists(self):
        report = heavy.status_report(heavy.read_lane(), ARRIVED)
        self.assertEqual(
            report,
            {
                "schema_version": 1,
                "budget": None,
                "holders": [],
                "waiters": [],
                "costs": {},
                "recent": [],
            },
        )


class WaitReleaseCase(QueueCase):
    def test_head_waiter_is_promoted_when_it_fits(self):
        first = self._join(timeout_s=600)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            heavy.join_lane(["make", "build"], SMALL, BUDGET, arrived=ARRIVED, pid=7, timeout_s=600)
        holder = heavy.poll_once(first["id"], WEIGHT, BUDGET, ARRIVED + 1.0)
        self.assertIsNotNone(holder)
        self.assertEqual(holder["id"], first["id"])
        self.assertEqual(holder["since"], "2023-11-14T22:13:21+00:00")
        lane = heavy.read_lane()
        self.assertEqual([h["id"] for h in lane["holders"]], [first["id"]])
        self.assertEqual([w["id"] for w in lane["waiters"]], [WAITER_ID])

    def test_non_head_waiter_keeps_waiting(self):
        self._join(timeout_s=600)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            second = heavy.join_lane(
                ["make", "build"], SMALL, BUDGET, arrived=ARRIVED, pid=7, timeout_s=600
            )
        self.assertIsNone(heavy.poll_once(second["id"], SMALL, BUDGET, ARRIVED + 1.0))

    def test_over_budget_head_keeps_waiting(self):
        row = self._join(weight=heavy.Weight(cpu_m=9000, memory_mb=100), timeout_s=600)
        self.assertIsNone(
            heavy.poll_once(
                row["id"],
                heavy.Weight(cpu_m=9000, memory_mb=100),
                BUDGET,
                ARRIVED + 1.0,
            )
        )

    def test_expired_waiter_is_removed_with_75(self):
        cap = heavy.Weight(cpu_m=4000, memory_mb=8192)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=HOLDER_ID)):
            blocker = heavy.join_lane(
                ["make", "check"], cap, BUDGET, arrived=ARRIVED, pid=1, timeout_s=600
            )
        heavy.poll_once(blocker["id"], cap, BUDGET, ARRIVED)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            mine = heavy.join_lane(
                ["make", "build"], SMALL, BUDGET, arrived=ARRIVED, pid=4242, timeout_s=0
            )
        with self.assertRaises(heavy.LaneTimeout) as ctx:
            heavy.poll_once(mine["id"], SMALL, BUDGET, ARRIVED)
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertEqual([w["id"] for w in heavy.read_lane()["waiters"]], [])
        self.assertEqual([h["id"] for h in heavy.read_lane()["holders"]], [blocker["id"]])

    def test_zero_timeout_admits_at_once_when_a_slot_is_free(self):
        row = self._join(timeout_s=0)
        holder = heavy.poll_once(row["id"], WEIGHT, BUDGET, ARRIVED)
        self.assertIsNotNone(holder)
        self.assertEqual(holder["id"], row["id"])

    def test_missing_waiter_row_reads_as_expired(self):
        with self.assertRaises(heavy.LaneTimeout):
            heavy.poll_once("0" * 32, WEIGHT, BUDGET, ARRIVED)

    def test_wait_loop_polls_until_the_deadline(self):
        clock = _LaneClock(ARRIVED)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=HOLDER_ID)):
            heavy.join_lane(
                ["make", "check"], WEIGHT, BUDGET, arrived=ARRIVED, pid=1, timeout_s=600
            )
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            mine = heavy.join_lane(
                ["make", "build"], SMALL, BUDGET, arrived=ARRIVED, pid=4242, timeout_s=2.5
            )
        with self.assertRaises(heavy.LaneTimeout) as ctx:
            heavy.wait_for_slot(
                mine["id"],
                SMALL,
                BUDGET,
                now=clock,
                asleep=clock.sleep,
            )
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertEqual(clock.sleeps, [1.0, 1.0, 1.0])
        remaining = [w["id"] for w in heavy.read_lane()["waiters"]]
        self.assertNotIn(mine["id"], remaining)

    def test_wait_loop_admits_without_sleeping_on_a_free_slot(self):
        clock = _LaneClock(ARRIVED)
        row = self._join(timeout_s=600)
        holder = heavy.wait_for_slot(row["id"], WEIGHT, BUDGET, now=clock, asleep=clock.sleep)
        self.assertEqual(holder["id"], row["id"])
        self.assertEqual(clock.sleeps, [])

    def test_release_removes_the_holder_and_refreshes_the_cache(self):
        first = self._join(timeout_s=600)
        heavy.poll_once(first["id"], WEIGHT, BUDGET, ARRIVED + 1.0)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            other = heavy.join_lane(
                ["make", "build"], SMALL, BUDGET, arrived=ARRIVED, pid=7, timeout_s=600
            )
        heavy.poll_once(other["id"], SMALL, BUDGET, ARRIVED + 2.0)
        smaller = resources.Usage(cpu_m=2000, memory_mb=2048, pids=0)
        heavy.release_lane(first["id"], smaller)
        lane = heavy.read_lane()
        self.assertEqual([h["id"] for h in lane["holders"]], [other["id"]])
        self.assertEqual(lane["budget"], {"cpu_m": 2000, "memory_mb": 2048})

    def test_release_of_an_unknown_holder_is_a_noop(self):
        heavy.release_lane("0" * 32, BUDGET)
        self.assertEqual(heavy.read_lane()["holders"], [])


class HandWeightCase(QueueCase):
    def test_no_config_reads_as_the_default_weight(self):
        self.assertEqual(heavy.hand_weight(None, "make check"), heavy.Weight())

    def test_listed_command_prefers_its_entry(self):
        cfg = heavy.check_table(
            {
                "default_weight": {"cpu_m": 1000, "memory_mb": 1024},
                "commands": {"make check": {"cpu_m": 3000, "memory_mb": 6144}},
            },
            "p",
        )
        self.assertEqual(heavy.hand_weight(cfg, "make check"), WEIGHT)
        self.assertEqual(heavy.hand_weight(cfg, "make lint"), heavy.Weight(1000, 1024))

    def test_hand_weight_agrees_with_weight_for_on_listed_commands(self):
        cfg = heavy.check_table(
            {
                "default_weight": {"cpu_m": 1000, "memory_mb": 1024},
                "commands": {"make check": {"cpu_m": 3000, "memory_mb": 6144}},
            },
            "p",
        )
        self.assertEqual(heavy.hand_weight(cfg, "make check"), heavy.weight_for(cfg, "make check"))

    def test_hand_weight_falls_back_only_where_weight_for_refuses(self):
        cfg = heavy.check_table(
            {
                "default_weight": {"cpu_m": 1000, "memory_mb": 1024},
                "commands": {"make check": {"cpu_m": 3000, "memory_mb": 6144}},
            },
            "p",
        )
        with self.assertRaises(heavy.HeavyError):
            heavy.weight_for(cfg, "make lint")
        self.assertEqual(heavy.hand_weight(cfg, "make lint"), cfg.default)
        with self.assertRaises(heavy.HeavyError):
            heavy.weight_for(None, "make check")
        self.assertEqual(heavy.hand_weight(None, "make check"), heavy.Weight())

    def test_command_name_joins_argv(self):
        self.assertEqual(heavy.command_name(["make", "check"]), "make check")

    def test_command_name_refuses_an_empty_command(self):
        with self.assertRaises(heavy.HeavyError):
            heavy.command_name([])


class RunThroughLaneCase(QueueCase):
    def _seams(self, run, **overrides):
        params = {
            "run": run,
            "teardown": mock.Mock(),
            "scope_cgroup": mock.Mock(return_value=None),
            "read": None,
            "now": _LaneClock(ARRIVED),
            "asleep": mock.Mock(),
            "host": {"cores": 8, "mem_mb": 16384.0, "python": "3.11"},
            "pressure": resources.Pressure(
                load_per_cpu=0.4,
                mem_available_pct=62.0,
                saturated=False,
                cpu_some_avg10=2.5,
                memory_some_avg10=0.8,
            ),
            "pid": 4242,
            "timeout": 1800,
        }
        params.update(overrides)
        return params

    def test_immediate_slot_runs_and_releases(self):
        run = _FakeRunner(code=0, stdout="green\n", stderr="")
        clock = _LaneClock(ARRIVED)
        lane_run = heavy.run_through_lane(
            ["make", "check"],
            self.tmp,
            {"PATH": "p"},
            weight=WEIGHT,
            budget=BUDGET,
            timeout_s=600,
            **self._seams(run, now=clock, asleep=clock.sleep),
        )
        self.assertEqual(lane_run.result.command_exit, 0)
        self.assertEqual(lane_run.result.exit_code, 0)
        self.assertEqual(lane_run.waited_s, 0.0)
        self.assertEqual(heavy.read_lane()["holders"], [])
        self.assertEqual(heavy.read_lane()["waiters"], [])

    def test_waited_seconds_reach_the_ledger_row(self):
        run = _FakeRunner(code=0)
        clock = _LaneClock(ARRIVED)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=HOLDER_ID)):
            heavy.join_lane(
                ["make", "check"], WEIGHT, BUDGET, arrived=ARRIVED, pid=1, timeout_s=600
            )
        heavy.poll_once(HOLDER_ID, WEIGHT, BUDGET, ARRIVED)

        def asleep(secs):
            clock.sleep(secs)
            if clock.now_value >= ARRIVED + 5:
                heavy.release_lane(HOLDER_ID, BUDGET)

        tight = resources.Usage(cpu_m=3000, memory_mb=6144, pids=0)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            lane_run = heavy.run_through_lane(
                ["make", "build"],
                self.tmp,
                {},
                weight=SMALL,
                budget=tight,
                timeout_s=600,
                **self._seams(run, now=clock, asleep=asleep),
            )
        self.assertEqual(lane_run.waited_s, 5.0)
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        names = sorted(os.listdir(runs))
        self.assertEqual(len(names), 1)
        with open(os.path.join(runs, names[0]), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        build_rows = [row for row in rows if row["argv"] == ["make", "build"]]
        self.assertEqual(len(build_rows), 1)
        self.assertEqual(build_rows[0]["waited_s"], 5.0)

    def test_wait_expired_never_runs(self):
        run = _FakeRunner(code=0)
        clock = _LaneClock(ARRIVED)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=HOLDER_ID)):
            heavy.join_lane(
                ["make", "check"], WEIGHT, BUDGET, arrived=ARRIVED, pid=1, timeout_s=600
            )
        heavy.poll_once(HOLDER_ID, WEIGHT, BUDGET, ARRIVED)
        with self.assertRaises(heavy.LaneTimeout) as ctx:
            heavy.run_through_lane(
                ["make", "build"],
                self.tmp,
                {},
                weight=SMALL,
                budget=resources.Usage(cpu_m=3000, memory_mb=6144, pids=0),
                timeout_s=0,
                **self._seams(run, now=clock, asleep=clock.sleep),
            )
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertEqual(run.calls, [])

    def test_run_failure_still_releases_the_slot(self):
        def hanging(argv, cwd, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        clock = _LaneClock(ARRIVED)
        with self.assertRaises(subprocess.TimeoutExpired):
            heavy.run_through_lane(
                ["make", "check"],
                self.tmp,
                {},
                weight=WEIGHT,
                budget=BUDGET,
                timeout_s=600,
                **self._seams(hanging, now=clock, asleep=clock.sleep),
            )
        self.assertEqual(heavy.read_lane()["holders"], [])

    def test_run_heavy_refuses_a_bad_waited_value(self):
        run = _FakeRunner(code=0)
        with self.assertRaises(heavy.HeavyError):
            heavy.run_heavy(
                ["make", "check"],
                self.tmp,
                {},
                role="hand",
                repo="myrepo",
                weight=WEIGHT,
                budget=BUDGET,
                run=run,
                teardown=mock.Mock(),
                scope_cgroup=mock.Mock(return_value=None),
                timeout=1800,
                waited_s=-1,
            )

    def test_run_heavy_records_waited_seconds(self):
        run = _FakeRunner(code=0)
        result = heavy.run_heavy(
            ["make", "check"],
            self.tmp,
            {},
            role="hand",
            repo="myrepo",
            weight=WEIGHT,
            budget=BUDGET,
            run=run,
            teardown=mock.Mock(),
            scope_cgroup=mock.Mock(return_value=None),
            clock=_LaneClock(ARRIVED),
            host={"cores": 8, "mem_mb": 16384.0, "python": "3.11"},
            pressure=None,
            timeout=1800,
            waited_s=38.1,
        )
        self.assertEqual(result.exit_code, 0)
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        names = sorted(os.listdir(runs))
        with open(os.path.join(runs, names[0]), encoding="utf-8") as handle:
            (row,) = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(row["waited_s"], 38.1)


class BypassCase(QueueCase):
    """Ticket 6: `--bypass` skips the queue but stays visible and measured.

    The bypass runs now inside the same scope limits the request would
    have carried, is not charged against the lane budget, leaves no
    waiter or holder row behind, still appends its ledger row, and
    records a `bypassed` row naming its reason in the recent ring.
    """

    def _seams(self, run, **overrides):
        params = {
            "run": run,
            "teardown": mock.Mock(),
            "scope_cgroup": mock.Mock(return_value=None),
            "clock": _LaneClock(ARRIVED),
            "host": {"cores": 8, "mem_mb": 16384.0, "python": "3.11"},
            "pressure": None,
            "timeout": 1800,
        }
        params.update(overrides)
        return params

    def _bypass(self, run, **overrides):
        params = {
            "role": "hand",
            "repo": "a-project",
            "reason": "release fix",
            "weight": WEIGHT,
            "budget": BUDGET,
        }
        params.update(overrides)
        return heavy.run_bypass(
            ["make", "check"], self.tmp, {"PATH": "p"}, **params, **self._seams(run)
        )

    def _ledger_rows(self):
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        rows = []
        for name in sorted(os.listdir(runs)):
            with open(os.path.join(runs, name), encoding="utf-8") as handle:
                rows.extend(json.loads(line) for line in handle if line.strip())
        return rows

    def test_bypass_runs_without_queueing_and_records_the_reason(self):
        run = _FakeRunner(code=0, stdout="green\n", stderr="")
        result = self._bypass(run)
        self.assertEqual(result.exit_code, 0)
        lane = heavy.read_lane()
        self.assertEqual(lane["holders"], [])
        self.assertEqual(lane["waiters"], [])
        (entry,) = lane["recent"]
        self.assertEqual(entry["outcome"], "bypassed")
        self.assertEqual(entry["reason"], "release fix")
        self.assertEqual(entry["command"], ["make", "check"])
        (row,) = self._ledger_rows()
        self.assertEqual(row["role"], "hand")
        self.assertEqual(row["waited_s"], 0.0)
        self.assertEqual(row["exit"], 0)

    def test_bypass_carries_the_scope_limits_without_charging_the_budget(self):
        run = _FakeRunner(code=0)
        blocker = self._join(timeout_s=600)
        heavy.poll_once(blocker["id"], WEIGHT, BUDGET, ARRIVED + 1.0)
        result = self._bypass(run)
        self.assertEqual(result.exit_code, 0)
        (scope, _, _) = run.calls[0]
        self.assertIn("--property=MemoryMax=6144M", scope)
        self.assertIn("--property=CPUQuota=300%", scope)
        lane = heavy.read_lane()
        self.assertEqual([h["id"] for h in lane["holders"]], [blocker["id"]])
        self.assertEqual(lane["waiters"], [])

    def test_bypass_empty_reason_is_refused_before_running(self):
        run = _FakeRunner(code=0)
        with self.assertRaises(heavy.HeavyError):
            self._bypass(run, reason="")
        self.assertEqual(run.calls, [])
        self.assertEqual(heavy.read_lane()["recent"], [])
        self.assertFalse(os.path.exists(os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")))

    def test_bypass_command_failure_still_records(self):
        run = _FakeRunner(code=3, stdout="", stderr="boom\n")
        result = self._bypass(run)
        self.assertEqual(result.exit_code, 3)
        (entry,) = heavy.read_lane()["recent"]
        self.assertEqual(entry["outcome"], "bypassed")
        (row,) = self._ledger_rows()
        self.assertEqual(row["exit"], 3)

    def test_bypass_scope_failure_records_nothing(self):
        def missing(argv, cwd, timeout):
            raise OSError("no such program")

        with self.assertRaises(heavy.HeavyError):
            self._bypass(missing)
        self.assertEqual(heavy.read_lane()["recent"], [])

    def test_bypass_record_lock_busy_warns_and_returns_the_run(self):
        run = _FakeRunner(code=0, stdout="green\n", stderr="")
        warnings = []
        path = heavy.lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        held = open(path, "a+")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = self._bypass(run, warn=warnings.append, lock_timeout=0.1)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("bypass", warnings[0])
        self.assertIn("locked by another process", warnings[0])
        (row,) = self._ledger_rows()
        self.assertEqual(row["exit"], 0)
        fcntl.flock(held, fcntl.LOCK_UN)
        self.assertEqual(heavy.read_lane()["recent"], [])

    def test_bypass_record_corrupt_lane_warns_and_returns_the_run(self):
        run = _FakeRunner(code=0, stdout="green\n", stderr="")
        warnings = []
        path = heavy.lane_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        result = self._bypass(run, warn=warnings.append)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("bypass", warnings[0])
        self.assertIn("unparsable", warnings[0])
        (row,) = self._ledger_rows()
        self.assertEqual(row["exit"], 0)

    def test_bypass_record_failure_warns_on_stderr_by_default(self):
        run = _FakeRunner(code=0, stdout="green\n", stderr="")
        path = heavy.lane_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = self._bypass(run)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("bypass", err.getvalue())
        self.assertIn("unparsable", err.getvalue())

    def test_record_bypass_names_the_reason_in_the_ring(self):
        heavy.record_bypass(["make", "check"], "hotfix", now=ARRIVED)
        (entry,) = heavy.read_lane()["recent"]
        self.assertEqual(entry["outcome"], "bypassed")
        self.assertEqual(entry["reason"], "hotfix")
        self.assertEqual(entry["at"], "2023-11-14T22:13:20+00:00")

    def test_record_bypass_empty_reason_is_refused(self):
        with self.assertRaises(heavy.HeavyError):
            heavy.record_bypass(["make", "check"], "", now=ARRIVED)
        self.assertEqual(heavy.read_lane()["recent"], [])

    def test_recent_ring_trims_to_the_limit(self):
        for index in range(heavy.RECENT_LIMIT + 5):
            heavy.record_bypass(["make", "check"], f"fix {index}", now=ARRIVED + index)
        recent = heavy.read_lane()["recent"]
        self.assertEqual(len(recent), heavy.RECENT_LIMIT)
        self.assertEqual(recent[0]["reason"], "fix 5")
        self.assertEqual(recent[-1]["reason"], f"fix {heavy.RECENT_LIMIT + 4}")


class ReleaseRingCase(QueueCase):
    """Ticket 6: the normal path records a `released` row in the recent ring."""

    def test_release_appends_the_released_row(self):
        first = self._join(timeout_s=600)
        heavy.poll_once(first["id"], WEIGHT, BUDGET, ARRIVED + 1.0)
        heavy.release_lane(first["id"], BUDGET, now=ARRIVED + 9.0)
        lane = heavy.read_lane()
        self.assertEqual(lane["holders"], [])
        (entry,) = lane["recent"]
        self.assertEqual(entry["outcome"], "released")
        self.assertEqual(entry["id"], first["id"])
        self.assertEqual(entry["command"], ["make", "check"])
        self.assertEqual(entry["unit"], "")
        self.assertEqual(entry["at"], "2023-11-14T22:13:29+00:00")

    def test_release_of_an_unknown_holder_records_nothing(self):
        heavy.release_lane("0" * 32, BUDGET, now=ARRIVED)
        self.assertEqual(heavy.read_lane()["recent"], [])


if __name__ == "__main__":
    unittest.main()
