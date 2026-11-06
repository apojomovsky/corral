"""`korapy heavy rollup`: the portable per-key summary from the run ledger.

Stdout always carries the rollup JSON document; `--out PATH`
additionally writes the same bytes atomically for the operator to
commit. Tests use temp dirs only: no systemd, no /proc or /sys, no
network, and the state dir stays under temp dirs or `KORAPY_STATE`.
"""

import io
import json
import os
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from korapy import cli, heavy, heavycli


def _sandbox(test):
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


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
        tmp = tempfile.TemporaryDirectory(prefix="korapy-heavy-cli-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
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


if __name__ == "__main__":
    unittest.main()
