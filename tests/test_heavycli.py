"""`korapy heavy`: queue one command in the lane, plan it, or read the lane.

The verbs are thin parsers over ``heavy.py``: ``run`` joins the lane,
waits the turn, runs the command in a transient scope and releases the
slot; ``run --plan`` reports the verdict without queueing anything;
``status`` reads holders, waiters and learned costs. Tests inject a
fake lane runner and a temp project root; no test touches systemd,
`/proc`, the network or real state dirs.
"""

import argparse
import contextlib
import io
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


def _run_args(command, timeout=None, plan=False, json=False):
    return argparse.Namespace(
        heavy_verb="run", command=list(command), timeout=timeout, plan=plan, json=json
    )


def _status_args(json=False):
    return argparse.Namespace(heavy_verb="status", json=json)


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


class PlanCase(HeavyCliCase):
    def test_plan_free_slot_is_exit_0(self):
        from korapy import heavycli

        self._project()
        code, out, _ = self._read_stdout(
            heavycli.cmd_run, _run_args(["make", "check"], plan=True), root=self.tmp
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
            heavycli.cmd_run, _run_args(["make", "lint"], plan=True), root=self.tmp
        )
        self.assertEqual(code, 1)
        self.assertIn("would wait", out)

    def test_plan_missing_command_is_exit_4(self):
        from korapy import heavycli

        self._project()
        code, _, err = self._read_stdout(heavycli.cmd_run, _run_args([], plan=True), root=self.tmp)
        self.assertEqual(code, 4)
        self.assertIn("--", err)

    def test_plan_bad_timeout_is_exit_4(self):
        from korapy import heavycli

        self._project()
        for bad in ("soon", -5):
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


if __name__ == "__main__":
    unittest.main()
