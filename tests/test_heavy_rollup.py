"""The heavy lane, ticket 4: the run ledger reader and the rollup document.

Raw rows stay host-local JSONL; the rollup regenerates the portable
per-key summary from them. Tests use synthetic rows in temp dirs only:
no systemd, no /proc or /sys, no network, and the state dir stays under
temp dirs.
"""

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from korapy import heavy

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "heavy-rollup.json"

NOW = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

LABEL = "9c2e0123456789abcdef0123456789ab"


def _sandbox(test):
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


def _at(days_ago=0.0):
    moment = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
    moment -= datetime.timedelta(days=days_ago)
    return moment.isoformat()


def _raw(
    repo="a-project",
    argv_hash="h1",
    role="takeoff",
    days_ago=0.0,
    wall_s=180.0,
    cpu_s=360.0,
    peak_mb=5100.0,
    starved=False,
    exit=0,
):
    return {
        "schema_version": 1,
        "at": _at(days_ago),
        "host": {"label": LABEL, "cores": 8, "mem_mb": 16384.0, "python": "3.11"},
        "repo": repo,
        "argv_hash": argv_hash,
        "role": role,
        "argv": ["make", "check"],
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_mb": peak_mb,
        "pids_peak": 96,
        "avg_cores": round(cpu_s / wall_s, 3),
        "throttled_us": 12000,
        "cpu_pressure_us": 8400000,
        "mem_pressure_us": 0,
        "starved": starved,
        "exit": exit,
        "waited_s": 38.1,
        "weight": {"cpu_m": 3000, "memory_mb": 6144},
        "host_pressure": {
            "load_per_cpu": 0.4,
            "mem_available_pct": 62.0,
            "saturated": False,
            "cpu_some_avg10": 2.5,
            "memory_some_avg10": 0.8,
        },
    }


def _write_runs(state_dir, files):
    runs = os.path.join(state_dir, "heavy", "runs")
    os.makedirs(runs, exist_ok=True)
    for name, lines in files.items():
        with open(os.path.join(runs, name), "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
    return runs


def _fixture():
    first = [
        json.dumps(
            _raw(
                argv_hash="h1",
                role="takeoff",
                days_ago=3.0,
                wall_s=170.0,
                cpu_s=340.0,
                peak_mb=5000.0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h1",
                role="takeoff",
                days_ago=2.0,
                wall_s=180.0,
                cpu_s=360.0,
                peak_mb=5100.0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h1",
                role="takeoff",
                days_ago=1.0,
                wall_s=190.0,
                cpu_s=380.0,
                peak_mb=5200.0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h1",
                role="takeoff",
                days_ago=0.0,
                wall_s=200.0,
                cpu_s=400.0,
                peak_mb=5300.0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h2",
                role="hand",
                days_ago=1.0,
                wall_s=50.0,
                cpu_s=100.0,
                peak_mb=1000.0,
                exit=0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h2",
                role="hand",
                days_ago=0.0,
                wall_s=150.0,
                cpu_s=300.0,
                peak_mb=6000.0,
                exit=1,
            ),
            sort_keys=True,
        ),
        "this line is not json",
        "[1, 2]",
    ]
    second = [
        json.dumps(
            _raw(
                argv_hash="h3",
                role="takeoff",
                days_ago=3.0,
                wall_s=60.0,
                cpu_s=120.0,
                peak_mb=2000.0,
                exit=0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h3",
                role="takeoff",
                days_ago=2.0,
                wall_s=60.0,
                cpu_s=120.0,
                peak_mb=2000.0,
                exit=1,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h3",
                role="takeoff",
                days_ago=1.0,
                wall_s=60.0,
                cpu_s=120.0,
                peak_mb=2000.0,
                exit=0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h3",
                role="takeoff",
                days_ago=0.0,
                wall_s=60.0,
                cpu_s=120.0,
                peak_mb=2000.0,
                exit=1,
            ),
            sort_keys=True,
        ),
        json.dumps(
            _raw(
                argv_hash="h9",
                role="takeoff",
                days_ago=100.0,
                wall_s=10.0,
                cpu_s=10.0,
                peak_mb=99999.0,
            ),
            sort_keys=True,
        ),
        json.dumps(
            {**_raw(argv_hash="h3", role="takeoff", days_ago=0.0), "schema_version": 2},
            sort_keys=True,
        ),
    ]
    return {
        f"{LABEL}-2026-11.jsonl": first,
        f"{LABEL}-2026-10.jsonl": second,
    }


class RollupCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-rollup-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        _sandbox(self)


class RollupPathCase(RollupCase):
    def test_default_path_lives_under_the_lane_dir(self):
        self.assertEqual(
            heavy.rollup_path(),
            os.path.join(os.environ["KORAPY_STATE"], "heavy", "rollup.json"),
        )

    def test_explicit_state_dir_wins(self):
        self.assertEqual(
            heavy.rollup_path(self.tmp),
            os.path.join(self.tmp, "heavy", "rollup.json"),
        )


class CollectCase(RollupCase):
    def test_missing_runs_dir_reads_as_empty(self):
        self.assertEqual(heavy.collect_ledger_rows(self.tmp), ([], [], 0))

    def test_collect_reads_every_file_and_counts_the_corrupt(self):
        _write_runs(self.tmp, _fixture())
        rows, sources, skipped = heavy.collect_ledger_rows(self.tmp)
        self.assertEqual(
            sources,
            sorted([f"{LABEL}-2026-11.jsonl", f"{LABEL}-2026-10.jsonl"]),
        )
        self.assertEqual(skipped, 3)
        self.assertEqual(len(rows), 11)
        self.assertTrue(all(row["repo"] == "a-project" for row in rows))

    def test_non_jsonl_files_are_ignored(self):
        _write_runs(self.tmp, {**_fixture(), "notes.txt": ["hello"]})
        _rows, sources, skipped = heavy.collect_ledger_rows(self.tmp)
        self.assertNotIn("notes.txt", sources)
        self.assertEqual(skipped, 3)


class BuildRollupCase(RollupCase):
    def _document(self):
        _write_runs(self.tmp, _fixture())
        rows, sources, skipped = heavy.collect_ledger_rows(self.tmp)
        self.assertEqual(skipped, 3)
        return heavy.build_rollup(rows, sources, now=NOW)

    def test_envelope_carries_version_sources_and_keys(self):
        document = self._document()
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            document["sources"],
            sorted([f"{LABEL}-2026-11.jsonl", f"{LABEL}-2026-10.jsonl"]),
        )
        self.assertEqual(
            sorted(document["keys"]),
            ["a-project|h1|takeoff", "a-project|h2|hand", "a-project|h3|takeoff"],
        )

    def test_steady_key_reports_high_percentile_peak_and_median_duration(self):
        key = self._document()["keys"]["a-project|h1|takeoff"]
        self.assertEqual(
            key,
            {
                "peak_mb_p50": 5100,
                "peak_mb_p90": 5300,
                "dur_s_p50": 180.0,
                "dur_s_p90": 200.0,
                "samples": 4,
                "flaky": False,
                "avg_cores": 2.0,
            },
        )

    def test_young_key_reports_the_maximum_observed(self):
        key = self._document()["keys"]["a-project|h2|hand"]
        self.assertEqual(key["peak_mb_p90"], 6000)
        self.assertEqual(key["samples"], 2)
        self.assertFalse(key["flaky"])

    def test_mixed_exits_flag_the_key(self):
        key = self._document()["keys"]["a-project|h3|takeoff"]
        self.assertTrue(key["flaky"])
        self.assertEqual(key["samples"], 4)

    def test_forgotten_rows_leave_no_key(self):
        document = self._document()
        self.assertNotIn("a-project|h9|takeoff", document["keys"])

    def test_empty_ledger_rolls_up_to_empty_keys(self):
        self.assertEqual(
            heavy.build_rollup([], [], now=NOW),
            {"schema_version": 1, "sources": [], "keys": {}},
        )

    def test_rollup_carries_hashes_never_argv_or_host_labels(self):
        document = self._document()
        keys_text = json.dumps(document["keys"], sort_keys=True)
        self.assertNotIn("make", keys_text)
        self.assertNotIn("check", keys_text)
        self.assertNotIn("label", keys_text)
        self.assertNotIn(LABEL, keys_text)

    def test_golden_file_pins_the_schema(self):
        document = self._document()
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        self.assertEqual(text, GOLDEN.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
