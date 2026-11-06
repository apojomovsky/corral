"""`korapy heavy`: queue one command, plan it, read the lane, roll it up.

The verbs are thin parsers over ``heavy.py``: ``run`` joins the lane,
waits the turn, runs the command in a transient scope and releases the
slot; ``run --plan`` reports the verdict without queueing anything;
``status`` reads holders, waiters and learned costs; ``rollup``
regenerates the portable per-key summary from the raw run ledger.
Tests inject a fake lane runner and temp dirs only: no systemd,
`/proc` or `/sys`, no network, and the state dir stays under temp
dirs or `KORAPY_STATE`.
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from korapy import cli, heavy, heavycli, resources, settings


def _sandbox(test):
    """The env sandbox the korapy tests share: temp dirs, no leaked config."""
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        del os.environ[name]
    for name in list(settings.REGISTRY.values()):
        os.environ.pop(name.env, None)
    tmp = tempfile.TemporaryDirectory(prefix="korapy-heavy-cli-")
    test.addCleanup(tmp.cleanup)
    test.tmp = os.path.realpath(tmp.name)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


ARRIVED = 1_700_000_000.0
HOLDER_ID = "1" * 32
WAITER_ID = "2" * 32
WEIGHT = heavy.Weight(cpu_m=3000, memory_mb=6144)
SMALL = heavy.Weight(cpu_m=1000, memory_mb=1024)
BUDGET = resources.Usage(cpu_m=4000, memory_mb=8192, pids=0)


def _run_args(command, timeout=None, plan=False, json=False, bypass=None, role="hand"):
    return argparse.Namespace(
        heavy_verb="run",
        command=list(command),
        timeout=timeout,
        plan=plan,
        json=json,
        bypass=bypass,
        role=role,
    )


def _status_args(json=False):
    return argparse.Namespace(heavy_verb="status", json=json)


def _cool():
    from korapy import pressure

    return pressure.Reading(load_per_cpu=0.4, mem_available_pct=60.0)


def _hot():
    from korapy import pressure

    return pressure.Reading(load_per_cpu=9.9, mem_available_pct=60.0)


def _lane_run(exit_code=0, stdout="out\n", stderr="err\n", waited_s=2.5):
    return heavy.LaneRun(
        result=heavy.RunResult(
            command_exit=exit_code,
            exit_code=exit_code if exit_code in heavy.PUBLIC_CODES else 1,
            output=stdout + stderr,
            weight=WEIGHT,
            wall_s=1.2,
            unit="heavy-abc123",
            argv_hash="ab" * 32,
            stdout=stdout,
            stderr=stderr,
            measured={"cpu_m": 2000, "memory_mb": 5120.0},
        ),
        waited_s=waited_s,
    )


def _write_row(state_dir, row):
    runs = os.path.join(state_dir, "heavy", "runs")
    os.makedirs(runs, exist_ok=True)
    label = "9c2e0123456789abcdef0123456789ab"
    with open(os.path.join(runs, f"{label}-2026-11.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return heavy


def _row(**overrides):
    row = {
        "schema_version": 1,
        "at": "2026-11-04T10:00:00+00:00",
        "repo": "a-project",
        "argv_hash": "h1",
        "role": "takeoff",
        "wall_s": 180.0,
        "cpu_s": 360.0,
        "peak_mb": 5100.0,
        "starved": False,
        "exit": 0,
    }
    row.update(overrides)
    return row


class RollupCliCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _args(self, **overrides):
        params = {"out": None}
        params.update(overrides)
        return types.SimpleNamespace(**params)


class ParserCase(RollupCliCase):
    def test_rollup_parses(self):
        args = cli.build_parser().parse_args(["heavy", "rollup"])
        self.assertEqual(args.heavy_verb, "rollup")
        self.assertIsNone(args.out)

    def test_rollup_out_parses(self):
        args = cli.build_parser().parse_args(["heavy", "rollup", "--out", "rollup.json"])
        self.assertEqual(args.out, "rollup.json")


class CmdRollupCase(RollupCliCase):
    def test_empty_ledger_exits_zero_with_empty_keys(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = heavycli.cmd_rollup(self._args(), state_dir=self.tmp)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out.getvalue()),
            {"schema_version": 1, "sources": [], "keys": {}},
        )
        self.assertEqual(err.getvalue(), "")
        with open(heavy.rollup_path(self.tmp), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), out.getvalue())

    def test_rows_roll_up_on_stdout(self):
        _write_row(self.tmp, _row())
        _write_row(self.tmp, _row())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = heavycli.cmd_rollup(self._args(), state_dir=self.tmp)
        self.assertEqual(code, 0)
        document = json.loads(out.getvalue())
        self.assertEqual(document["keys"]["a-project|h1|takeoff"]["samples"], 2)
        self.assertEqual(err.getvalue(), "")

    def test_out_writes_the_same_bytes_atomically(self):
        _write_row(self.tmp, _row())
        target = os.path.join(self.tmp, "ops", "rollup.json")
        os.makedirs(os.path.join(self.tmp, "ops"), exist_ok=True)
        out = io.StringIO()
        with redirect_stdout(out):
            code = heavycli.cmd_rollup(self._args(out=target), state_dir=self.tmp)
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), out.getvalue())

    def test_rollup_refreshes_the_default_file(self):
        _write_row(self.tmp, _row())
        _write_row(self.tmp, _row())
        default = heavy.rollup_path(self.tmp)
        self.assertFalse(os.path.exists(default))
        out = io.StringIO()
        with redirect_stdout(out):
            code = heavycli.cmd_rollup(self._args(), state_dir=self.tmp)
        self.assertEqual(code, 0)
        with open(default, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), out.getvalue())

    def test_default_refresh_failure_is_a_usage_error(self):
        _write_row(self.tmp, _row())
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("korapy.paths.atomic_write_text", side_effect=OSError("denied")),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = heavycli.cmd_rollup(self._args(), state_dir=self.tmp)
        self.assertEqual(code, 4)
        self.assertIn("cannot write", err.getvalue())
        self.assertTrue(json.loads(out.getvalue())["keys"])

    def test_default_failure_still_writes_the_requested_out_file(self):
        from korapy import paths as _paths

        _write_row(self.tmp, _row())
        default = heavy.rollup_path(self.tmp)
        target = os.path.join(self.tmp, "ops", "rollup.json")
        os.makedirs(os.path.join(self.tmp, "ops"), exist_ok=True)
        real_write = _paths.atomic_write_text

        def _fail_default(path, text):
            if path == default:
                raise OSError("denied")
            return real_write(path, text)

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("korapy.paths.atomic_write_text", side_effect=_fail_default),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = heavycli.cmd_rollup(self._args(out=target), state_dir=self.tmp)
        self.assertEqual(code, 4)
        self.assertIn(default, err.getvalue())
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), out.getvalue())

    def test_missing_out_parent_is_a_usage_error(self):
        _write_row(self.tmp, _row())
        target = os.path.join(self.tmp, "no-such-dir", "rollup.json")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = heavycli.cmd_rollup(self._args(out=target), state_dir=self.tmp)
        self.assertEqual(code, 4)
        self.assertIn(target, err.getvalue())
        self.assertTrue(json.loads(out.getvalue())["keys"])

    def test_corrupt_rows_count_on_stderr_and_still_exit_zero(self):
        _write_row(self.tmp, _row())
        runs = os.path.join(self.tmp, "heavy", "runs")
        name = next(name for name in os.listdir(runs) if name.endswith(".jsonl"))
        with open(os.path.join(runs, name), "a", encoding="utf-8") as handle:
            handle.write("not json\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = heavycli.cmd_rollup(self._args(), state_dir=self.tmp)
        self.assertEqual(code, 0)
        self.assertIn("skipped 1", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["keys"]["a-project|h1|takeoff"]["samples"], 1)

    def test_unreadable_state_is_a_usage_error(self):
        with mock.patch("korapy.heavy.collect_ledger_rows", side_effect=heavy.HeavyError("gone")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = heavycli.cmd_rollup(self._args(), state_dir=self.tmp)
        self.assertEqual(code, 4)
        self.assertIn("gone", err.getvalue())

    def test_end_to_end_through_main(self):
        _write_row(os.environ["KORAPY_STATE"], _row())
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["heavy", "rollup"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["keys"]["a-project|h1|takeoff"]["samples"], 1)


class HeavyCliCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        _sandbox(self)

    def _project(self, body="[heavy]\ncpu_m = 4000\nmemory_mb = 8192\n"):
        with open(os.path.join(self.tmp, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(body)

    def _seed_lane(self):
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=HOLDER_ID)):
            heavy.join_lane(
                ["make", "check"], WEIGHT, BUDGET, arrived=ARRIVED, pid=99, timeout_s=600
            )
        heavy.poll_once(HOLDER_ID, WEIGHT, BUDGET, ARRIVED)
        with mock.patch.object(heavy.uuid, "uuid4", return_value=mock.Mock(hex=WAITER_ID)):
            heavy.join_lane(
                ["make", "build"], SMALL, BUDGET, arrived=ARRIVED, pid=4242, timeout_s=600
            )

    def _read_stdout(self, func, *args, **kwargs):
        from korapy import heavycli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(*args, **kwargs)
        return code, out.getvalue(), err.getvalue()


class StatusCase(HeavyCliCase):
    def test_status_json_names_holders_waiters_and_costs(self):
        from korapy import heavycli

        self._seed_lane()
        code, out, _ = self._read_stdout(
            heavycli.cmd_status, _status_args(json=True), now=ARRIVED + 12.0
        )
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["budget"], {"cpu_m": 4000, "memory_mb": 8192})
        (holder,) = doc["holders"]
        self.assertEqual(holder["id"], HOLDER_ID)
        self.assertEqual(holder["command"], ["make", "check"])
        self.assertIsNone(holder["eta_s"])
        (waiter,) = doc["waiters"]
        self.assertEqual(waiter["id"], WAITER_ID)
        self.assertEqual(waiter["waited_s"], 12.0)
        self.assertEqual(doc["costs"], {})
        self.assertEqual(doc["recent"], [])

    def test_status_text_lists_holders_and_waiters(self):
        from korapy import heavycli

        self._seed_lane()
        code, out, _ = self._read_stdout(heavycli.cmd_status, _status_args(), now=ARRIVED + 12.0)
        self.assertEqual(code, 0)
        self.assertIn("holder", out)
        self.assertIn(HOLDER_ID[:8], out)
        self.assertIn("waiter", out)
        self.assertIn(WAITER_ID[:8], out)

    def test_status_empty_lane_is_exit_0(self):
        from korapy import heavycli

        code, out, _ = self._read_stdout(heavycli.cmd_status, _status_args(json=True))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["holders"], [])
        self.assertEqual(doc["waiters"], [])
        self.assertIsNone(doc["budget"])

    def test_status_corrupt_is_exit_4(self):
        from korapy import heavycli

        path = heavy.lane_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json{{{")
        code, _, err = self._read_stdout(heavycli.cmd_status, _status_args(json=True))
        self.assertEqual(code, 4)
        self.assertIn("lane.json", err)

    def test_status_text_names_recent_bypass_rows(self):
        from korapy import heavycli

        self._seed_lane()
        heavy.record_bypass(["make", "check"], "release fix", now=ARRIVED + 20.0)
        heavy.release_lane(HOLDER_ID, BUDGET, now=ARRIVED + 21.0)
        code, out, _ = self._read_stdout(heavycli.cmd_status, _status_args(), now=ARRIVED + 30.0)
        self.assertEqual(code, 0)
        self.assertIn("bypassed", out)
        self.assertIn("release fix", out)
        self.assertIn("make check", out)
        self.assertIn("released", out)

    def test_status_text_with_no_recent_says_so(self):
        from korapy import heavycli

        self._seed_lane()
        code, out, _ = self._read_stdout(heavycli.cmd_status, _status_args(), now=ARRIVED + 12.0)
        self.assertEqual(code, 0)
        self.assertIn("recent", out)


class PlanCase(HeavyCliCase):
    def test_plan_free_slot_is_exit_0(self):
        from korapy import heavycli

        self._project()
        code, out, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], plan=True),
            root=self.tmp,
            pressure=_cool(),
        )
        self.assertEqual(code, 0)
        self.assertIn("would run", out)

    def test_plan_json_reports_the_verdict_shape(self):
        from korapy import heavycli

        self._project()
        code, out, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], plan=True, json=True),
            root=self.tmp,
            pressure=_cool(),
        )
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(
            doc,
            {
                "schema_version": 1,
                "admitted": True,
                "position": 0,
                "weight": {"cpu_m": 1000, "memory_mb": 1000},
                "reason": doc["reason"],
            },
        )

    def test_plan_blocked_lane_is_exit_1(self):
        from korapy import heavycli

        self._project()
        os.makedirs(os.environ["KORAPY_CONFIG"], exist_ok=True)
        host_config = os.path.join(os.environ["KORAPY_CONFIG"], "config.toml")
        with open(host_config, "w", encoding="utf-8") as handle:
            handle.write("[resources.host]\ncpu_m = 8000\nmemory_mb = 16384\npids = 200\n")
        self._seed_lane()
        code, out, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "lint"], plan=True),
            root=self.tmp,
            pressure=_cool(),
        )
        self.assertEqual(code, 1)
        self.assertIn("would wait", out)

    def test_plan_saturated_pressure_is_exit_1(self):
        from korapy import heavycli

        self._project()
        code, out, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], plan=True),
            root=self.tmp,
            pressure=_hot(),
        )
        self.assertEqual(code, 1)
        self.assertIn("saturated", out)

    def test_plan_missing_command_is_exit_4(self):
        from korapy import heavycli

        self._project()
        code, _, err = self._read_stdout(heavycli.cmd_run, _run_args([], plan=True), root=self.tmp)
        self.assertEqual(code, 4)
        self.assertIn("--", err)

    def test_plan_bad_timeout_is_exit_4(self):
        from korapy import heavycli

        self._project()
        for bad in ("soon", -5, "inf", float("inf")):
            with self.subTest(timeout=bad):
                code, _, _ = self._read_stdout(
                    heavycli.cmd_run,
                    _run_args(["make", "check"], plan=True, timeout=bad),
                    root=self.tmp,
                )
                self.assertEqual(code, 4)

    def test_plan_uses_the_configured_wait_timeout(self):
        from korapy import heavycli

        self._project("[heavy]\nwait_timeout_s = 60\n")
        seen = {}

        def fake_lane(argv, cwd, env, **kwargs):
            seen.update(kwargs)
            return _lane_run()

        code, _, _ = self._read_stdout(
            heavycli.cmd_run, _run_args(["make", "check"]), root=self.tmp, run_lane=fake_lane
        )
        self.assertEqual(code, 0)
        self.assertEqual(seen["timeout_s"], 60)


class ParserSeparatorCase(HeavyCliCase):
    """The documented `--` separator spelling through the real parser.

    `heavy run` takes its command as an argparse REMAINDER, which keeps
    the literal `--`; the CLI layer strips one separator before the
    lane validates, so the exact documented spelling reaches the
    command. A command that really starts with `-` is still refused.
    """

    def _parse(self, argv):
        from korapy import cli

        return cli.build_parser().parse_args(argv)

    def test_separator_spelling_reaches_the_command(self):
        from korapy import heavycli

        self._project()
        seen = {}

        def fake_lane(argv, cwd, env, **kwargs):
            seen["argv"] = list(argv)
            return _lane_run()

        args = self._parse(["heavy", "run", "--", "make", "check"])
        code, _, _ = self._read_stdout(heavycli.cmd_run, args, root=self.tmp, run_lane=fake_lane)
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"], ["make", "check"])

    def test_timeout_before_separator_reaches_the_command(self):
        from korapy import heavycli

        self._project()
        seen = {}

        def fake_lane(argv, cwd, env, **kwargs):
            seen["argv"] = list(argv)
            seen.update(kwargs)
            return _lane_run()

        args = self._parse(["heavy", "run", "--timeout", "60", "--", "make", "check"])
        code, _, _ = self._read_stdout(heavycli.cmd_run, args, root=self.tmp, run_lane=fake_lane)
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"], ["make", "check"])
        self.assertEqual(seen["timeout_s"], 60.0)

    def test_command_starting_with_a_dash_is_still_refused(self):
        from korapy import heavycli

        self._project()
        calls = []

        def fake_lane(*args, **kwargs):
            calls.append((args, kwargs))
            return _lane_run()

        args = self._parse(["heavy", "run", "--", "--jobs"])
        code, _, err = self._read_stdout(heavycli.cmd_run, args, root=self.tmp, run_lane=fake_lane)
        self.assertEqual(code, 4)
        self.assertEqual(calls, [])
        self.assertIn("program name", err)


class RunCase(HeavyCliCase):
    def test_run_replays_streams_and_reports_the_exit(self):
        from korapy import heavycli

        self._project()
        code, out, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"]),
            root=self.tmp,
            run_lane=lambda *a, **k: _lane_run(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "out\n")
        self.assertIn("err\n", err)
        self.assertIn("exit 0", err)

    def test_run_json_verdict_goes_to_stderr(self):
        from korapy import heavycli

        self._project(
            "[heavy]\n"
            "cpu_m = 4000\nmemory_mb = 8192\n"
            '[heavy.commands."make check"]\ncpu_m = 3000\nmemory_mb = 6144\n'
        )
        code, out, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], json=True),
            root=self.tmp,
            run_lane=lambda *a, **k: _lane_run(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "out\nerr\n")
        doc = json.loads(err)
        self.assertEqual(
            doc,
            {
                "schema_version": 1,
                "command": ["make", "check"],
                "weight": {"cpu_m": 3000, "memory_mb": 6144},
                "waited_s": 2.5,
                "admitted": True,
                "bypassed": False,
                "exit_code": 0,
                "measured": {"cpu_m": 2000, "memory_mb": 5120.0},
            },
        )

    def test_run_maps_a_foreign_command_exit_to_1(self):
        from korapy import heavycli

        self._project()
        code, _, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"]),
            root=self.tmp,
            run_lane=lambda *a, **k: _lane_run(exit_code=5),
        )
        self.assertEqual(code, 1)

    def test_run_lane_timeout_is_75_without_running(self):
        from korapy import heavycli

        self._project()
        calls = []

        def fake_lane(*args, **kwargs):
            calls.append((args, kwargs))
            raise heavy.LaneTimeout("waited past its deadline with no free slot")

        code, _, err = self._read_stdout(
            heavycli.cmd_run, _run_args(["make", "check"]), root=self.tmp, run_lane=fake_lane
        )
        self.assertEqual(code, 75)
        self.assertEqual(len(calls), 1)

    def test_run_missing_command_is_exit_4(self):
        from korapy import heavycli

        self._project()
        code, _, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args([]),
            root=self.tmp,
            run_lane=lambda *a, **k: _lane_run(),
        )
        self.assertEqual(code, 4)

    def test_run_resolves_the_listed_weight(self):
        from korapy import heavycli

        self._project(
            "[heavy]\n"
            "cpu_m = 4000\nmemory_mb = 8192\n"
            '[heavy.commands."make check"]\ncpu_m = 3000\nmemory_mb = 6144\n'
        )
        seen = {}

        def fake_lane(argv, cwd, env, **kwargs):
            seen.update(kwargs)
            return _lane_run()

        self._read_stdout(
            heavycli.cmd_run, _run_args(["make", "check"]), root=self.tmp, run_lane=fake_lane
        )
        self.assertEqual(seen["weight"], heavy.Weight(cpu_m=3000, memory_mb=6144))

    def test_run_unlisted_command_queues_at_the_default_weight(self):
        from korapy import heavycli

        self._project()
        seen = {}

        def fake_lane(argv, cwd, env, **kwargs):
            seen.update(kwargs)
            return _lane_run()

        self._read_stdout(
            heavycli.cmd_run, _run_args(["make", "lint"]), root=self.tmp, run_lane=fake_lane
        )
        self.assertEqual(seen["weight"], heavy.Weight(cpu_m=1000, memory_mb=1000))


def _bypass_result(exit_code=0, wall_s=1.2):
    return heavy.RunResult(
        command_exit=exit_code,
        exit_code=exit_code if exit_code in heavy.PUBLIC_CODES else 1,
        output="out\nerr\n",
        weight=WEIGHT,
        wall_s=wall_s,
        unit="heavy-bypass1",
        argv_hash="ab" * 32,
        stdout="out\n",
        stderr="err\n",
        measured={"cpu_m": 2000, "memory_mb": 5120.0},
    )


class BypassCase(HeavyCliCase):
    """Ticket 6: `heavy run --bypass REASON` skips the queue but stays visible."""

    def test_bare_bypass_is_exit_4_without_running(self):
        from korapy import heavycli

        self._project()
        calls = []

        def fake_lane(*args, **kwargs):
            calls.append((args, kwargs))
            return _lane_run()

        def fake_bypass(*args, **kwargs):
            calls.append((args, kwargs))
            return _bypass_result()

        code, _, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], bypass=""),
            root=self.tmp,
            run_lane=fake_lane,
            run_bypass=fake_bypass,
        )
        self.assertEqual(code, 4)
        self.assertEqual(calls, [])
        self.assertIn("--bypass", err)

    def test_bypass_runs_now_with_the_reason_and_role(self):
        from korapy import heavycli

        self._project()
        seen = {}
        queued = []

        def fake_lane(*args, **kwargs):
            queued.append((args, kwargs))
            return _lane_run()

        def fake_bypass(argv, cwd, env, **kwargs):
            seen.update(kwargs)
            seen["argv"] = list(argv)
            return _bypass_result()

        code, out, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], bypass="release fix"),
            root=self.tmp,
            run_lane=fake_lane,
            run_bypass=fake_bypass,
            pressure=_cool(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(queued, [])
        self.assertEqual(seen["argv"], ["make", "check"])
        self.assertEqual(seen["reason"], "release fix")
        self.assertEqual(seen["role"], "hand")
        self.assertEqual(out, "out\n")
        self.assertIn("err\n", err)
        self.assertIn("release fix", err)

    def test_bypass_json_verdict_is_not_admitted(self):
        from korapy import heavycli

        self._project(
            "[heavy]\n"
            "cpu_m = 4000\nmemory_mb = 8192\n"
            '[heavy.commands."make check"]\ncpu_m = 3000\nmemory_mb = 6144\n'
        )
        code, out, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], bypass="hotfix", json=True),
            root=self.tmp,
            run_bypass=lambda *a, **k: _bypass_result(),
            pressure=_cool(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "out\nerr\n")
        doc = json.loads(err)
        self.assertEqual(
            doc,
            {
                "schema_version": 1,
                "command": ["make", "check"],
                "weight": {"cpu_m": 3000, "memory_mb": 6144},
                "waited_s": 0.0,
                "admitted": False,
                "bypassed": True,
                "exit_code": 0,
                "measured": {"cpu_m": 2000, "memory_mb": 5120.0},
            },
        )

    def test_bypass_scope_failure_is_exit_3(self):
        from korapy import heavycli

        self._project()

        def fake_bypass(*args, **kwargs):
            raise heavy.ScopeUnreachable("cannot start scope heavy-x: manager unreachable")

        code, _, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], bypass="hotfix"),
            root=self.tmp,
            run_bypass=fake_bypass,
            pressure=_cool(),
        )
        self.assertEqual(code, 3)

    def test_bypass_timeout_is_exit_1(self):
        from korapy import heavycli

        self._project()

        def fake_bypass(*args, **kwargs):
            raise subprocess.TimeoutExpired(["make", "check"], 30)

        code, _, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], bypass="hotfix"),
            root=self.tmp,
            run_bypass=fake_bypass,
            pressure=_cool(),
        )
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)

    def test_bypass_missing_command_is_exit_4(self):
        from korapy import heavycli

        self._project()
        code, _, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args([], bypass="hotfix"),
            root=self.tmp,
            run_bypass=lambda *a, **k: _bypass_result(),
        )
        self.assertEqual(code, 4)

    def test_bypass_with_plan_is_exit_4_without_running(self):
        from korapy import heavycli

        self._project()
        calls = []

        def fake_lane(*args, **kwargs):
            calls.append((args, kwargs))
            return _lane_run()

        def fake_bypass(*args, **kwargs):
            calls.append((args, kwargs))
            return _bypass_result()

        code, _, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], bypass="hotfix", plan=True),
            root=self.tmp,
            run_lane=fake_lane,
            run_bypass=fake_bypass,
            pressure=_cool(),
        )
        self.assertEqual(code, 4)
        self.assertEqual(calls, [])
        self.assertIn("--bypass", err)
        self.assertIn("--plan", err)


class RoleCase(HeavyCliCase):
    """Ticket 6: `heavy run --role NAME` names the ledger caller; empty is 4."""

    def test_role_reaches_the_lane_runner(self):
        from korapy import heavycli

        self._project()
        seen = {}

        def fake_lane(argv, cwd, env, **kwargs):
            seen.update(kwargs)
            return _lane_run()

        code, _, _ = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], role="nightly"),
            root=self.tmp,
            run_lane=fake_lane,
        )
        self.assertEqual(code, 0)
        self.assertEqual(seen["role"], "nightly")

    def test_takeoff_role_is_exit_4_without_running(self):
        from korapy import heavycli

        self._project()
        calls = []

        def fake_lane(*args, **kwargs):
            calls.append((args, kwargs))
            return _lane_run()

        def fake_bypass(*args, **kwargs):
            calls.append((args, kwargs))
            return _bypass_result()

        code, _, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], role="takeoff"),
            root=self.tmp,
            run_lane=fake_lane,
            run_bypass=fake_bypass,
            pressure=_cool(),
        )
        self.assertEqual(code, 4)
        self.assertEqual(calls, [])
        self.assertIn("--role", err)
        self.assertIn("takeoff", err)

    def test_takeoff_role_is_exit_4_on_the_bypass_path(self):
        from korapy import heavycli

        self._project()
        calls = []

        def fake_bypass(*args, **kwargs):
            calls.append((args, kwargs))
            return _bypass_result()

        code, _, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], bypass="hotfix", role="takeoff"),
            root=self.tmp,
            run_bypass=fake_bypass,
            pressure=_cool(),
        )
        self.assertEqual(code, 4)
        self.assertEqual(calls, [])
        self.assertIn("--role", err)

    def test_default_role_is_hand(self):
        from korapy import heavycli

        self._project()
        seen = {}

        def fake_lane(argv, cwd, env, **kwargs):
            seen.update(kwargs)
            return _lane_run()

        self._read_stdout(
            heavycli.cmd_run, _run_args(["make", "check"]), root=self.tmp, run_lane=fake_lane
        )
        self.assertEqual(seen["role"], "hand")

    def test_empty_role_is_exit_4_without_running(self):
        from korapy import heavycli

        self._project()
        calls = []

        def fake_lane(*args, **kwargs):
            calls.append((args, kwargs))
            return _lane_run()

        code, _, err = self._read_stdout(
            heavycli.cmd_run,
            _run_args(["make", "check"], role=""),
            root=self.tmp,
            run_lane=fake_lane,
        )
        self.assertEqual(code, 4)
        self.assertEqual(calls, [])
        self.assertIn("--role", err)

    def test_takeoff_and_hand_runs_share_the_repo_key(self):
        """Both callers resolve the ledger repo through `heavy.repo_key`."""

        self._project()
        self.assertEqual(heavy.repo_key(self.tmp), os.path.basename(self.tmp))

    def test_reserved_roles_hold_the_shared_takeoff_role(self):
        from korapy import heavycli

        self.assertEqual(heavycli.RESERVED_ROLES, frozenset({heavy.TAKEOFF_ROLE}))


class BypassParserCase(HeavyCliCase):
    """The documented `--bypass`/`--role` spelling through the real parser."""

    def _parse(self, argv):
        from korapy import cli

        return cli.build_parser().parse_args(argv)

    def test_bypass_and_role_parse_before_the_separator(self):
        args = self._parse(
            ["heavy", "run", "--role", "takeoff", "--bypass", "release fix", "--", "make", "check"]
        )
        self.assertEqual(args.role, "takeoff")
        self.assertEqual(args.bypass, "release fix")
        self.assertEqual(args.command, ["--", "make", "check"])

    def test_bare_bypass_parses_to_empty(self):
        args = self._parse(["heavy", "run", "--bypass", "--", "make", "check"])
        self.assertEqual(args.bypass, "")


if __name__ == "__main__":
    unittest.main()
