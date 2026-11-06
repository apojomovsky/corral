"""The heavy lane, ticket 5: PSI admission and the doctor rows.

Saturated pressure delays admission even while shares are free; an
unknown reading (absent files, unparsable content) never blocks, using
the shared `[resources.pressure]` caps. The lane budget re-resolves
from settings on every admit pass, not once per run. The doctor names
lane health: holders and waiters, stuck rows past the deadline scale,
bypass counts, and the corrupt-`lane.json` repair. Tests inject fakes
at every seam (clock, pid liveness, scope existence, pressure
readings); no test touches systemd, `/proc`, the network or real
state dirs.
"""

import os
import tempfile
import unittest
from typing import Any
from unittest import mock

from korapy import heavy, pressure, resources, settings


def _sandbox(test):
    """The env sandbox the korapy tests share: temp dirs, no leaked config."""
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        del os.environ[name]
    for name in list(settings.REGISTRY.values()):
        os.environ.pop(name.env, None)
    tmp = tempfile.TemporaryDirectory(prefix="korapy-heavy-psi-")
    test.addCleanup(tmp.cleanup)
    test.tmp = os.path.realpath(tmp.name)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


ARRIVED = 1_700_000_000.0
ARRIVED_ISO = "2023-11-14T22:13:20+00:00"
HOLDER_ID = "1" * 32
WAITER_ID = "2" * 32
WEIGHT = heavy.Weight(cpu_m=1000, memory_mb=1024)
BUDGET = resources.Usage(cpu_m=4000, memory_mb=8192, pids=0)
CAPS = resources.PressureCaps(max_load_per_cpu=2.0, min_mem_available_pct=10.0)


def _lane(holders=(), waiters=()):
    return {
        "schema_version": 1,
        "budget": {"cpu_m": BUDGET.cpu_m, "memory_mb": BUDGET.memory_mb},
        "holders": list(holders),
        "waiters": list(waiters),
        "costs": {},
        "recent": [],
    }


def _waiter(deadline_iso=ARRIVED_ISO, weight=WEIGHT, wid=WAITER_ID):
    return {
        "id": wid,
        "pid": 4242,
        "command": ["make", "check"],
        "argv_hash": heavy.argv_hash(["make", "check"]),
        "weight": {"cpu_m": weight.cpu_m, "memory_mb": weight.memory_mb},
        "arrived_at": ARRIVED_ISO,
        "deadline_at": deadline_iso,
        "skipped": 0,
    }


def _hot():
    """A saturated reading: load past the cap while shares stand free."""
    return pressure.Reading(load_per_cpu=9.9, mem_available_pct=60.0)


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


def _cool():
    """A quiet reading: nothing past either cap."""
    return pressure.Reading(load_per_cpu=0.4, mem_available_pct=60.0)


class PressureBlocksCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def test_unknown_reading_never_blocks(self):
        for unknown in (None, pressure.Reading(), {}, {"load_per_cpu": None}):
            with self.subTest(reading=unknown):
                blocked, reason = heavy.pressure_blocks(unknown, CAPS)
                self.assertFalse(blocked)
                self.assertEqual(reason, "")

    def test_absent_files_read_unknown_and_never_block(self):
        def _missing(path):
            raise OSError("absent")

        reading, _notes = resources.read_pressure(read=_missing, cpu_count=8)
        self.assertIsNone(reading.load_per_cpu)
        blocked, _reason = heavy.pressure_blocks(reading, CAPS)
        self.assertFalse(blocked)

    def test_saturated_load_blocks_while_shares_are_free(self):
        blocked, reason = heavy.pressure_blocks(_hot(), CAPS)
        self.assertTrue(blocked)
        self.assertIn("load per cpu", reason)

    def test_low_memory_blocks(self):
        reading = pressure.Reading(load_per_cpu=0.4, mem_available_pct=2.0)
        blocked, reason = heavy.pressure_blocks(reading, CAPS)
        self.assertTrue(blocked)
        self.assertIn("MemAvailable", reason)

    def test_generous_caps_keep_a_busy_host_unblocked(self):
        wide = resources.PressureCaps(max_load_per_cpu=99.0, min_mem_available_pct=1.0)
        blocked, _reason = heavy.pressure_blocks(_hot(), wide)
        self.assertFalse(blocked)

    def test_a_half_blind_reading_still_decides(self):
        reading = pressure.Reading(load_per_cpu=9.9, mem_available_pct=None)
        blocked, _reason = heavy.pressure_blocks(reading, CAPS)
        self.assertTrue(blocked)


class PlanPressureCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def test_saturated_refuses_while_a_slot_is_free(self):
        lane = _lane()
        report = heavy.plan_report(WEIGHT, lane, BUDGET, pressure=_hot())
        self.assertFalse(report["admitted"])
        self.assertIn("saturated", report["reason"])

    def test_unknown_pressure_admits_when_a_slot_is_free(self):
        lane = _lane()
        for unknown in (None, pressure.Reading()):
            with self.subTest(reading=unknown):
                report = heavy.plan_report(WEIGHT, lane, BUDGET, pressure=unknown)
                self.assertTrue(report["admitted"])


class PollPressureCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _join(self, timeout_s=600, weight=WEIGHT, hex_id=WAITER_ID):
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=hex_id)):
            return heavy.join_lane(
                ["make", "check"],
                weight,
                BUDGET,
                arrived=ARRIVED,
                pid=4242,
                timeout_s=timeout_s,
            )

    def test_saturated_head_keeps_waiting(self):
        row = self._join()
        self.assertIsNone(
            heavy.poll_once(row["id"], WEIGHT, BUDGET, ARRIVED + 1.0, pressure=_hot())
        )
        self.assertEqual([w["id"] for w in heavy.read_lane()["waiters"]], [row["id"]])

    def test_saturated_past_the_deadline_still_expires_75(self):
        row = self._join(timeout_s=0)
        with self.assertRaises(heavy.LaneTimeout) as ctx:
            heavy.poll_once(row["id"], WEIGHT, BUDGET, ARRIVED, pressure=_hot())
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertEqual(heavy.read_lane()["waiters"], [])

    def test_unknown_pressure_admits(self):
        row = self._join()
        holder = heavy.poll_once(
            row["id"], WEIGHT, BUDGET, ARRIVED + 1.0, pressure=pressure.Reading()
        )
        self.assertIsNotNone(holder)

    def test_live_budget_reresolves_every_pass(self):
        row = self._join()
        tight = resources.Usage(cpu_m=1, memory_mb=1, pids=0)
        self.assertIsNone(
            heavy.poll_once(row["id"], WEIGHT, BUDGET, ARRIVED + 1.0, budget_fn=lambda: tight)
        )
        self.assertEqual(heavy.read_lane()["holders"], [])
        holder = heavy.poll_once(
            row["id"],
            WEIGHT,
            tight,
            ARRIVED + 2.0,
            budget_fn=lambda: BUDGET,
        )
        self.assertIsNotNone(holder)


class RunThroughLanePressureCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def test_admit_pass_reading_lands_in_the_ledger_row(self):
        import json as _json
        import subprocess

        class _Runner:
            def __call__(self, argv, cwd, timeout):
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="green\n", stderr=""
                )

        clock = _LaneClock(ARRIVED)

        def _press(load, mem=60.0):
            return resources.Pressure(load_per_cpu=load, mem_available_pct=mem, saturated=False)

        readings = iter([_press(9.9), _press(9.9), _press(0.4)])

        lane_run = heavy.run_through_lane(
            ["make", "check"],
            self.tmp,
            {"PATH": "p"},
            weight=WEIGHT,
            budget=BUDGET,
            timeout_s=600,
            timeout=1800,
            run=_Runner(),
            teardown=mock.Mock(),
            scope_cgroup=mock.Mock(return_value=None),
            now=clock,
            asleep=clock.sleep,
            host={"cores": 8, "mem_mb": 16384.0, "python": "3.11"},
            pid=4242,
            pressure_fn=lambda: next(readings),
        )
        self.assertEqual(lane_run.result.command_exit, 0)
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        names = sorted(os.listdir(runs))
        self.assertEqual(len(names), 1)
        with open(os.path.join(runs, names[0]), encoding="utf-8") as handle:
            rows = [_json.loads(line) for line in handle if line.strip()]
        check_rows = [row for row in rows if row["argv"] == ["make", "check"]]
        self.assertEqual(len(check_rows), 1)
        self.assertEqual(check_rows[0]["host_pressure"]["load_per_cpu"], 0.4)

    def test_explicit_ledger_pressure_wins_over_the_admit_reading(self):
        import json as _json
        import subprocess

        class _Runner:
            def __call__(self, argv, cwd, timeout):
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="green\n", stderr=""
                )

        clock = _LaneClock(ARRIVED)
        ledger = resources.Pressure(load_per_cpu=1.1, mem_available_pct=55.0, saturated=False)
        lane_run = heavy.run_through_lane(
            ["make", "check"],
            self.tmp,
            {"PATH": "p"},
            weight=WEIGHT,
            budget=BUDGET,
            timeout_s=600,
            timeout=1800,
            run=_Runner(),
            teardown=mock.Mock(),
            scope_cgroup=mock.Mock(return_value=None),
            now=clock,
            asleep=clock.sleep,
            host={"cores": 8, "mem_mb": 16384.0, "python": "3.11"},
            pressure=ledger,
            pid=4242,
            pressure_fn=lambda: resources.Pressure(
                load_per_cpu=0.2, mem_available_pct=70.0, saturated=False
            ),
        )
        self.assertEqual(lane_run.result.command_exit, 0)
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        names = sorted(os.listdir(runs))
        with open(os.path.join(runs, names[0]), encoding="utf-8") as handle:
            rows = [_json.loads(line) for line in handle if line.strip()]
        check_rows = [row for row in rows if row["argv"] == ["make", "check"]]
        self.assertEqual(check_rows[0]["host_pressure"]["load_per_cpu"], 1.1)


class StuckCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _holder(self, since_iso=ARRIVED_ISO, argv_hash="h1"):
        return {
            "id": HOLDER_ID,
            "pid": 99,
            "unit": "heavy-deadbeef",
            "command": ["make", "check"],
            "argv_hash": argv_hash,
            "weight": {"cpu_m": 1000, "memory_mb": 1024},
            "since": since_iso,
        }

    def test_waiter_past_its_deadline_is_stuck(self):
        lane = _lane(waiters=[_waiter(deadline_iso=ARRIVED_ISO)])
        stuck = heavy.stuck_waiters(lane, ARRIVED + 120.0)
        self.assertEqual([w["id"] for w in stuck], [WAITER_ID])

    def test_waiter_inside_its_deadline_is_not_stuck(self):
        lane = _lane(waiters=[_waiter(deadline_iso="2023-11-14T23:13:20+00:00")])
        self.assertEqual(heavy.stuck_waiters(lane, ARRIVED + 1.0), [])

    def test_holder_past_wait_factor_times_p90_is_stuck(self):
        lane = _lane(holders=[self._holder()])
        estimates = {"a-project|h1|takeoff": _estimate(p90=100.0)}
        stuck = heavy.stuck_holders(lane, estimates, ARRIVED + 400.0, 3.0)
        self.assertEqual([h["id"] for h, _e, _p in stuck], [HOLDER_ID])

    def test_holder_inside_the_scaled_typical_is_not_stuck(self):
        lane = _lane(holders=[self._holder()])
        estimates = {"a-project|h1|takeoff": _estimate(p90=100.0)}
        self.assertEqual(heavy.stuck_holders(lane, estimates, ARRIVED + 10.0, 3.0), [])

    def test_holder_without_an_estimate_is_never_stuck(self):
        lane = _lane(holders=[self._holder()])
        self.assertEqual(heavy.stuck_holders(lane, {}, ARRIVED + 100000.0, 3.0), [])

    def test_bypassed_rows_count(self):
        recent = [
            {"outcome": "bypassed", "reason": "release fix"},
            {"outcome": "released"},
            {"outcome": "bypassed", "reason": "hotfix"},
        ]
        self.assertEqual(heavy.count_outcomes(recent).get("bypassed"), 2)

    def test_corrupt_detail_names_bytes_and_repair(self):
        detail = heavy.corrupt_lane_detail(38, 1_700_000_000.0)
        self.assertIn("lane.json unparsable", detail)
        self.assertIn("38 bytes", detail)
        self.assertIn("moving it aside", detail)


def _estimate(p90=100.0):
    return heavy.Estimate(
        key="a-project|h1|takeoff",
        samples=6,
        cpu_m=2000,
        memory_mb=5120,
        dur_s_p50=80.0,
        dur_s_p90=p90,
        peak_mb_p50=5000,
        peak_mb_p90=5400,
        flaky=False,
        avg_cores=2.0,
    )


class DoctorHeavyCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _readings(self, **overrides):
        from korapy import resourcedoctor

        fields: dict[str, Any] = {
            "host": resources.Usage(cpu_m=8000, memory_mb=16000, pids=4096),
            "shares": {},
            "foreign": [],
            "gone_s": 600.0,
            "now": ARRIVED,
            "limits_error": None,
            "priced_roles": frozenset(),
            "live_roles": (),
            "controllers": frozenset({"cpu", "memory", "pids"}),
            "controllers_error": None,
            "ledger": {"projects": {}, "foreign": []},
            "ledger_error": None,
            "ledger_path": "/state/host-resources.json",
            "marker": "/state",
            "marker_error": None,
            "state_dir": "/state",
            "marker_path": "/state/marker",
            "sensor_errors": (),
            "slices": {},
            "config_path": "/config/config.toml",
        }
        fields.update(overrides)
        return resourcedoctor.ResourceReadings(**fields)

    def test_empty_lane_is_ok(self):
        from korapy import resourcedoctor

        rows = {row.name: row for row in resourcedoctor.check(self._readings())}
        self.assertEqual(rows["heavy"].status, "ok")
        self.assertIn("0 holder(s), 0 waiter(s)", rows["heavy"].detail)
        self.assertEqual(rows["heavy-stuck"].status, "ok")

    def test_holders_and_waiters_count_in_the_detail(self):
        from korapy import resourcedoctor

        lane = _lane(holders=[{"id": HOLDER_ID}], waiters=[_waiter(), _waiter()])
        rows = {row.name: row for row in resourcedoctor.check(self._readings(heavy_lane=lane))}
        self.assertEqual(rows["heavy"].status, "ok")
        self.assertIn("1 holder(s), 2 waiter(s)", rows["heavy"].detail)

    def test_bypassed_rows_count_in_the_detail(self):
        from korapy import resourcedoctor

        lane = _lane()
        lane["recent"] = [
            {"outcome": "bypassed", "reason": "release fix"},
            {"outcome": "released"},
        ]
        rows = {row.name: row for row in resourcedoctor.check(self._readings(heavy_lane=lane))}
        self.assertIn("1 bypassed", rows["heavy"].detail)

    def test_corrupt_lane_fails_with_the_repair(self):
        from korapy import resourcedoctor

        rows = {
            row.name: row
            for row in resourcedoctor.check(
                self._readings(
                    heavy_error="lane file /state/heavy/lane.json is unparsable: oops",
                    heavy_path="/state/heavy/lane.json",
                    heavy_bytes=38,
                    heavy_mtime=ARRIVED,
                )
            )
        }
        self.assertEqual(rows["heavy"].status, "fail")
        self.assertIn("lane.json unparsable, 38 bytes", rows["heavy"].detail)
        self.assertIn("moving it aside", rows["heavy"].detail)
        self.assertIn("corrupt-", rows["heavy"].fix)
        self.assertNotIn("heavy-stuck", rows)

    def test_stuck_waiter_warns(self):
        from korapy import resourcedoctor

        lane = _lane(waiters=[_waiter()])
        rows = {
            row.name: row
            for row in resourcedoctor.check(
                self._readings(heavy_lane=lane, heavy_now=ARRIVED + 120.0)
            )
        }
        self.assertEqual(rows["heavy-stuck"].status, "warn")
        self.assertIn(WAITER_ID[:8], rows["heavy-stuck"].detail)

    def test_stuck_holder_warns(self):
        from korapy import resourcedoctor

        lane = _lane(
            holders=[
                {
                    "id": HOLDER_ID,
                    "command": ["make", "check"],
                    "argv_hash": "h1",
                    "weight": {"cpu_m": 1000, "memory_mb": 1024},
                    "since": ARRIVED_ISO,
                }
            ]
        )
        rows = {
            row.name: row
            for row in resourcedoctor.check(
                self._readings(
                    heavy_lane=lane,
                    heavy_now=ARRIVED + 400.0,
                    heavy_estimates={"a-project|h1|takeoff": _estimate(p90=100.0)},
                )
            )
        }
        self.assertEqual(rows["heavy-stuck"].status, "warn")
        self.assertIn(HOLDER_ID[:8], rows["heavy-stuck"].detail)


if __name__ == "__main__":
    unittest.main()
