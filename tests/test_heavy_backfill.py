"""The heavy lane, ticket 3: fair order, bounded backfill and reaping.

The head waiter admits first when the holder sum plus its weight fits
the budget. Otherwise the lane backfills: it scans later waiters in
arrival order for the first one that fits, skipping rows whose
``skipped`` count reached ``heavy.max_skips``. Each passed-over waiter
gains one ``skipped``. Dead holders (their scope unit is gone) and
stale waiters (their pid is dead, or they outlived their deadline plus
a grace bound) are reaped under the lock on the next admit pass. Tests
inject fakes at every seam (clock, pid liveness, scope existence); no
test touches systemd, `/proc`, the network or real state dirs.
"""

import argparse
import json
import os
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
    tmp = tempfile.TemporaryDirectory(prefix="korapy-heavy-backfill-")
    test.addCleanup(tmp.cleanup)
    test.tmp = os.path.realpath(tmp.name)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


ARRIVED = 1_700_000_000.0
HOLDER_ID = "1" * 32
WAITER_ID = "2" * 32
THIRD_ID = "3" * 32
BIG = heavy.Weight(cpu_m=3000, memory_mb=6144)
SMALL = heavy.Weight(cpu_m=1000, memory_mb=1024)
FULL = heavy.Weight(cpu_m=4000, memory_mb=8192)
BUDGET = resources.Usage(cpu_m=4000, memory_mb=8192, pids=0)


def _alive(*pids):
    """An is_alive fake alive exactly for the given pids."""
    return lambda pid: pid in pids


def _gone(*units):
    """A scope_exists fake gone exactly for the given units."""
    return lambda unit: unit not in units


class BackfillCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _admit_holder(self, weight=BIG, held_id=HOLDER_ID, at=ARRIVED):
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=held_id)):
            heavy.join_lane(["make", "check"], weight, BUDGET, arrived=at, pid=99, timeout_s=600)
        return heavy.poll_once(held_id, weight, BUDGET, at, unit="heavy-held")

    def _join(self, command, weight, hex_id, pid=4242, timeout_s=600, at=ARRIVED):
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=hex_id)):
            return heavy.join_lane(
                command, weight, BUDGET, arrived=at, pid=pid, timeout_s=timeout_s
            )

    def test_backfill_admits_a_small_job_past_a_big_head(self):
        self._admit_holder()
        head = self._join(["make", "check"], BIG, WAITER_ID)
        small = self._join(["make", "build"], SMALL, THIRD_ID)
        holder = heavy.poll_once(small["id"], SMALL, BUDGET, ARRIVED + 1.0)
        self.assertIsNotNone(holder)
        self.assertEqual(holder["id"], small["id"])
        lane = heavy.read_lane()
        self.assertEqual([h["id"] for h in lane["holders"]], [HOLDER_ID, THIRD_ID])
        (kept,) = lane["waiters"]
        self.assertEqual(kept["id"], head["id"])
        self.assertEqual(kept["skipped"], 1)

    def test_head_first_when_the_head_fits(self):
        head = self._join(["make", "check"], BIG, WAITER_ID)
        small = self._join(["make", "build"], SMALL, THIRD_ID)
        self.assertIsNone(heavy.poll_once(small["id"], SMALL, BUDGET, ARRIVED + 1.0))
        holder = heavy.poll_once(head["id"], BIG, BUDGET, ARRIVED + 1.0)
        self.assertIsNotNone(holder)
        self.assertEqual(heavy.read_lane()["waiters"][0]["skipped"], 0)

    def test_backfill_skips_rows_at_the_bound(self):
        self._admit_holder()
        self._join(["make", "check"], BIG, WAITER_ID)
        middle = self._join(["make", "build"], SMALL, THIRD_ID)
        lane = heavy.read_lane()
        lane["waiters"][1]["skipped"] = heavy.DEFAULT_MAX_SKIPS
        heavy._store_locked(heavy.lane_path(), lane)
        late = self._join(["make", "lint"], SMALL, "4" * 32)
        holder = heavy.poll_once(late["id"], SMALL, BUDGET, ARRIVED + 1.0)
        self.assertIsNotNone(holder)
        lane = heavy.read_lane()
        by_id = {row["id"]: row for row in lane["waiters"]}
        self.assertEqual(by_id[WAITER_ID]["skipped"], 1)
        self.assertEqual(by_id[middle["id"]]["skipped"], heavy.DEFAULT_MAX_SKIPS + 1)
        self.assertNotIn(late["id"], by_id)

    def test_skip_bound_on_the_head_stops_backfill(self):
        self._admit_holder()
        head = self._join(["make", "check"], BIG, WAITER_ID)
        small = self._join(["make", "build"], SMALL, THIRD_ID)
        lane = heavy.read_lane()
        lane["waiters"][0]["skipped"] = heavy.DEFAULT_MAX_SKIPS
        heavy._store_locked(heavy.lane_path(), lane)
        self.assertIsNone(
            heavy.poll_once(
                small["id"], SMALL, BUDGET, ARRIVED + 1.0, max_skips=heavy.DEFAULT_MAX_SKIPS
            )
        )
        lane = heavy.read_lane()
        self.assertEqual(len(lane["holders"]), 1)
        by_id = {row["id"]: row for row in lane["waiters"]}
        self.assertEqual(by_id[head["id"]]["skipped"], heavy.DEFAULT_MAX_SKIPS)
        self.assertEqual(by_id[small["id"]]["skipped"], 0)

    def test_zero_max_skips_is_head_only(self):
        self._admit_holder()
        self._join(["make", "check"], BIG, WAITER_ID)
        small = self._join(["make", "build"], SMALL, THIRD_ID)
        self.assertIsNone(heavy.poll_once(small["id"], SMALL, BUDGET, ARRIVED + 1.0, max_skips=0))

    def test_each_passed_over_waiter_gains_one_skip(self):
        self._admit_holder()
        first = self._join(["make", "check"], BIG, WAITER_ID, timeout_s=600)
        second = self._join(["make", "check"], BIG, THIRD_ID, timeout_s=600)
        third = self._join(["make", "build"], SMALL, "4" * 32, timeout_s=600)
        self.assertIsNotNone(heavy.poll_once(third["id"], SMALL, BUDGET, ARRIVED + 1.0))
        lane = heavy.read_lane()
        by_id = {row["id"]: row for row in lane["waiters"]}
        self.assertEqual(by_id[first["id"]]["skipped"], 1)
        self.assertEqual(by_id[second["id"]]["skipped"], 1)

    def test_no_fitting_later_waiter_admits_nobody(self):
        self._admit_holder()
        head = self._join(["make", "check"], BIG, WAITER_ID)
        self.assertIsNone(heavy.poll_once(head["id"], BIG, BUDGET, ARRIVED + 1.0))
        lane = heavy.read_lane()
        self.assertEqual(len(lane["holders"]), 1)
        self.assertEqual(lane["waiters"][0]["skipped"], 0)


class ConfigCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def test_max_skips_defaults_to_the_bound(self):
        cfg = heavy.check_table({}, "korapy.toml")
        self.assertEqual(cfg.max_skips, heavy.DEFAULT_MAX_SKIPS)

    def test_max_skips_parses(self):
        cfg = heavy.check_table({"max_skips": 3}, "korapy.toml")
        self.assertEqual(cfg.max_skips, 3)

    def test_max_skips_zero_parses(self):
        cfg = heavy.check_table({"max_skips": 0}, "korapy.toml")
        self.assertEqual(cfg.max_skips, 0)

    def test_max_skips_must_be_a_non_negative_count(self):
        for bad in (-1, "many", True, 1.5, None, float("inf"), float("nan")):
            with self.subTest(value=bad):
                with self.assertRaises(settings.SettingsError):
                    heavy.check_table({"max_skips": bad}, "korapy.toml")

    def test_poll_once_refuses_a_bad_max_skips(self):
        with self.assertRaises(heavy.HeavyError):
            heavy.poll_once(WAITER_ID, SMALL, BUDGET, ARRIVED, max_skips=-1)


class ReapCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _holder_at(self, at, held_id=HOLDER_ID, unit="heavy-held"):
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=held_id)):
            heavy.join_lane(["make", "check"], FULL, BUDGET, arrived=at, pid=99, timeout_s=600)
        return heavy.poll_once(held_id, FULL, BUDGET, at, unit=unit)

    def _join(self, hex_id, pid=4242, timeout_s=600, at=ARRIVED):
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=hex_id)):
            return heavy.join_lane(
                ["make", "build"], SMALL, BUDGET, arrived=at, pid=pid, timeout_s=timeout_s
            )

    def test_dead_holder_is_reaped_and_frees_its_weight(self):
        self._holder_at(ARRIVED)
        head = self._join(WAITER_ID)
        holder = heavy.poll_once(
            head["id"],
            SMALL,
            BUDGET,
            ARRIVED + 10.0,
            unit="heavy-next",
            scope_exists=_gone("heavy-held"),
        )
        self.assertIsNotNone(holder)
        self.assertEqual(holder["id"], head["id"])
        lane = heavy.read_lane()
        self.assertEqual([h["id"] for h in lane["holders"]], [head["id"]])
        self.assertEqual(lane["waiters"], [])
        (row,) = [r for r in lane["recent"] if r.get("outcome") == "reaped"]
        self.assertEqual(row["unit"], "heavy-held")
        runs = os.path.join(os.environ["KORAPY_STATE"], "heavy", "runs")
        self.assertFalse(os.path.exists(runs))

    def test_live_holder_keeps_its_weight(self):
        self._holder_at(ARRIVED)
        head = self._join(WAITER_ID)
        self.assertIsNone(
            heavy.poll_once(head["id"], SMALL, BUDGET, ARRIVED + 10.0, scope_exists=_gone())
        )
        lane = heavy.read_lane()
        self.assertEqual([h["id"] for h in lane["holders"]], [HOLDER_ID])

    def test_holder_without_a_unit_is_never_reaped(self):
        self._holder_at(ARRIVED, unit="")
        head = self._join(WAITER_ID)
        self.assertIsNone(
            heavy.poll_once(
                head["id"],
                SMALL,
                BUDGET,
                ARRIVED + 10.0,
                scope_exists=_gone(""),
            )
        )
        self.assertEqual([h["id"] for h in heavy.read_lane()["holders"]], [HOLDER_ID])

    def test_young_holder_is_not_reaped_inside_the_start_window(self):
        self._holder_at(ARRIVED)
        head = self._join(WAITER_ID)
        self.assertIsNone(
            heavy.poll_once(
                head["id"],
                SMALL,
                BUDGET,
                ARRIVED + 1.0,
                scope_exists=_gone("heavy-held"),
            )
        )
        self.assertEqual([h["id"] for h in heavy.read_lane()["holders"]], [HOLDER_ID])

    def test_holder_pid_is_never_consulted(self):
        self._holder_at(ARRIVED)
        head = self._join(WAITER_ID, pid=4242)
        self.assertIsNone(
            heavy.poll_once(
                head["id"],
                SMALL,
                BUDGET,
                ARRIVED + 10.0,
                scope_exists=_gone(),
                is_alive=_alive(4242),
            )
        )
        self.assertEqual([h["id"] for h in heavy.read_lane()["holders"]], [HOLDER_ID])

    def test_stale_head_with_a_dead_pid_no_longer_wedges_the_lane(self):
        dead = self._join(WAITER_ID, pid=4242)
        live = self._join(THIRD_ID, pid=7)
        holder = heavy.poll_once(
            live["id"], SMALL, BUDGET, ARRIVED + 1.0, is_alive=_alive(7, os.getpid())
        )
        self.assertIsNotNone(holder)
        self.assertEqual(holder["id"], live["id"])
        lane = heavy.read_lane()
        self.assertNotIn(dead["id"], [w["id"] for w in lane["waiters"]])

    def test_waiter_past_deadline_plus_grace_is_reaped_despite_a_live_pid(self):
        row = self._join(WAITER_ID, pid=4242, timeout_s=600)
        with self.assertRaises(heavy.LaneTimeout):
            heavy.poll_once(
                row["id"],
                SMALL,
                BUDGET,
                ARRIVED + 600.0 + heavy.WAITER_GRACE_S + 1.0,
                is_alive=_alive(4242),
            )
        self.assertEqual(heavy.read_lane()["waiters"], [])

    def test_waiter_inside_grace_with_a_live_pid_is_kept(self):
        first = self._join(WAITER_ID, pid=4242, timeout_s=600)
        second = self._join(THIRD_ID, pid=7, timeout_s=1200)
        self.assertIsNone(
            heavy.poll_once(
                second["id"],
                SMALL,
                BUDGET,
                ARRIVED + 601.0,
                is_alive=_alive(4242, 7),
            )
        )
        ids = [w["id"] for w in heavy.read_lane()["waiters"]]
        self.assertIn(first["id"], ids)

    def test_waiter_with_an_unknown_pid_reaps_only_by_the_clock(self):
        self._holder_at(ARRIVED)
        row = self._join(WAITER_ID, pid=0, timeout_s=600)
        self.assertIsNone(
            heavy.poll_once(row["id"], SMALL, BUDGET, ARRIVED + 1.0, is_alive=_alive())
        )
        with self.assertRaises(heavy.LaneTimeout):
            heavy.poll_once(
                row["id"],
                SMALL,
                BUDGET,
                ARRIVED + 600.0 + heavy.WAITER_GRACE_S + 1.0,
                is_alive=_alive(),
            )
        self.assertEqual(heavy.read_lane()["waiters"], [])

    def test_reaped_rows_land_in_a_bounded_recent_ring(self):
        lane = heavy.empty_lane()
        lane["recent"] = [{"at": heavy._iso(ARRIVED), "outcome": "released"} for _ in range(60)]
        heavy._store_locked(heavy.lane_path(), lane)
        self._holder_at(ARRIVED)
        head = self._join(WAITER_ID)
        heavy.poll_once(
            head["id"],
            SMALL,
            BUDGET,
            ARRIVED + 10.0,
            unit="heavy-next",
            scope_exists=_gone("heavy-held"),
        )
        recent = heavy.read_lane()["recent"]
        self.assertEqual(len(recent), 50)
        self.assertEqual(recent[-1]["outcome"], "reaped")
        self.assertEqual(recent[-1]["unit"], "heavy-held")


class WiringCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _project(self, body):
        with open(os.path.join(self.tmp, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(body)

    def _run_args(self, command, timeout=None, plan=False, as_json=False):
        return argparse.Namespace(
            heavy_verb="run", command=list(command), timeout=timeout, plan=plan, json=as_json
        )

    def _lane_run(self):
        return heavy.LaneRun(
            result=heavy.RunResult(
                command_exit=0,
                exit_code=0,
                output="",
                weight=SMALL,
                wall_s=1.0,
                unit="heavy-abc",
                argv_hash="ab" * 32,
            ),
            waited_s=0.0,
        )

    def test_run_forwards_max_skips_and_live_seams(self):
        from korapy import heavycli

        self._project("[heavy]\ncpu_m = 4000\nmemory_mb = 8192\nmax_skips = 3\n")
        with mock.patch.object(heavy, "run_through_lane", return_value=self._lane_run()) as runner:
            code = heavycli.cmd_run(self._run_args(["--", "make", "build"]), root=self.tmp)
        self.assertEqual(code, 0)
        _, kwargs = runner.call_args
        self.assertEqual(kwargs["max_skips"], 3)
        self.assertIs(kwargs["scope_exists"], heavy.scope_exists)
        self.assertIs(kwargs["is_alive"], heavy.pid_alive)

    def test_run_defaults_max_skips_without_the_key(self):
        from korapy import heavycli

        self._project("[heavy]\ncpu_m = 4000\nmemory_mb = 8192\n")
        with mock.patch.object(heavy, "run_through_lane", return_value=self._lane_run()) as runner:
            code = heavycli.cmd_run(self._run_args(["--", "make", "build"]), root=self.tmp)
        self.assertEqual(code, 0)
        _, kwargs = runner.call_args
        self.assertEqual(kwargs["max_skips"], heavy.DEFAULT_MAX_SKIPS)

    def test_live_pid_probe_reads_only(self):
        self.assertTrue(heavy.pid_alive(os.getpid()))
        self.assertFalse(heavy.pid_alive(1 << 30))
        self.assertFalse(heavy.pid_alive(0))
        self.assertFalse(heavy.pid_alive(-3))

    def test_rollup_schema_ignores_reaped_rows(self):
        lane = heavy.empty_lane()
        lane["recent"] = [
            {
                "at": heavy._iso(ARRIVED),
                "outcome": "reaped",
                "unit": "heavy-held",
                "id": HOLDER_ID,
                "command": ["make", "check"],
            }
        ]
        report = heavy.status_report(lane, ARRIVED)
        self.assertEqual(report["recent"], lane["recent"])
        golden_path = os.path.join(os.path.dirname(__file__), "golden", "heavy-status.json")
        with open(golden_path, encoding="utf-8") as handle:
            golden = json.load(handle)
        self.assertEqual(set(golden), set(report))


if __name__ == "__main__":
    unittest.main()
