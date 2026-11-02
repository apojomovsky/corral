"""The host-wide resource ledger and the single-instance guard.

File mechanics only: a versioned JSON ledger beside a state-dir marker,
a timed flock plus atomic write, and a guard refusing a second state dir
on the same host. No subprocess, no clock reads except the injected lock
deadline, no state dir outside the temp paths the tests pass in.
"""

import ast
import errno
import fcntl
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from corral import host_ledger as hl
from corral import paths

STATE = "/o/state"
OTHER = "/other/state"


class LedgerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="corral-host-ledger-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "host-resources.json")
        self.state = os.path.join(self.dir, "state")
        self.marker = os.path.join(self.dir, hl.MARKER_NAME)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()


class LedgerFile(LedgerCase):
    def test_empty_has_version_one_and_no_budget(self):
        self.assertEqual(
            hl.empty(), {"version": 1, "host_budget": None, "projects": {}, "foreign": []}
        )

    def test_save_and_load_round_trip(self):
        ledger = {
            "version": 1,
            "host_budget": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
            "projects": {"web": {"weight": 1, "cpu_floor_m": 500, "memory_floor_mb": 1024}},
            "foreign": [],
        }
        hl.save(ledger, self.path)
        self.assertEqual(hl.load(self.path), ledger)

    def test_save_creates_missing_parent_dirs(self):
        nested = os.path.join(self.dir, "sub", "dir", "host-resources.json")
        hl.save(hl.empty(), nested)
        self.assertEqual(hl.load(nested), hl.empty())

    def test_load_of_a_missing_file_is_an_empty_ledger(self):
        self.assertEqual(hl.load(self.path), hl.empty())

    def test_load_of_a_corrupt_file_raises_and_names_it(self):
        self.write("{not json")
        with self.assertRaises(hl.LedgerError) as caught:
            hl.load(self.path)
        self.assertIn(self.path, str(caught.exception))
        self.assertEqual(hl.LedgerError.exit_code, 4)

    def test_load_of_a_newer_version_refuses(self):
        self.write(json.dumps({"version": 2, "projects": {}, "foreign": []}))
        with self.assertRaises(hl.LedgerError) as caught:
            hl.load(self.path)
        message = str(caught.exception)
        self.assertIn(self.path, message)
        self.assertIn("2", message)

    def test_load_of_a_wrong_shaped_file_raises(self):
        for bad in (
            ["version"],
            "text",
            {"version": 1, "projects": ["web"], "foreign": []},
            {"version": 1, "projects": {}, "foreign": {}},
            {"version": 1, "projects": {}, "foreign": [{"cpu_m": 1}]},
            {"version": 1, "host_budget": 8000, "projects": {}, "foreign": []},
        ):
            with self.subTest(bad=bad), self.assertRaises(hl.LedgerError):
                self.write(json.dumps(bad))
                hl.load(self.path)

    def test_helpers_keep_unknown_top_level_keys(self):
        ledger = {**hl.empty(), "future": {"v": 1}}
        hl.save(ledger, self.path)
        self.assertEqual(hl.load(self.path)["future"], {"v": 1})

    def test_failed_update_leaves_the_corrupt_file_untouched(self):
        self.write("{not json")
        change = mock.Mock(side_effect=lambda ledger: ledger)
        with self.assertRaises(hl.LedgerError):
            hl.update(change, self.path, self.state)
        change.assert_not_called()
        self.assertEqual(self.read(), "{not json")

    def test_update_applies_a_change_under_the_lock(self):
        hl.save(hl.empty(), self.path)
        out = hl.update(
            lambda ledger: {**ledger, "foreign": [{"name": "desk", "claimed_at": 0.0}]},
            self.path,
            self.state,
        )
        self.assertEqual(hl.load(self.path)["foreign"], out["foreign"])
        self.assertEqual(len(out["foreign"]), 1)

    def test_a_change_returning_none_still_creates_the_missing_file(self):
        out = hl.update(lambda ledger: None, self.path, self.state)
        self.assertEqual(out, hl.empty())
        self.assertEqual(hl.load(self.path), hl.empty())

    def test_update_writes_the_marker_beside_a_new_ledger(self):
        hl.update(lambda ledger: None, self.path, self.state)
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_concurrent_updates_all_land(self):
        hl.save(hl.empty(), self.path)
        errors = []

        def work(index):
            try:
                hl.update(
                    lambda ledger: {
                        **ledger,
                        "foreign": [
                            *ledger["foreign"],
                            {"name": f"desk-{index}", "claimed_at": 0.0},
                        ],
                    },
                    self.path,
                    self.state,
                )
            except Exception as exc:  # pragma: no cover - fails the test below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(hl.load(self.path)["foreign"]), 4)


class _FakeClock:
    """A monotonic clock a fake sleep advances: lock waits take no time."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _ContendedFlock:
    """A `_flock` fake refusing the first `times` acquires, None for always."""

    def __init__(self, times):
        self.times = times
        self.calls = []

    def __call__(self, fd, flags):
        self.calls.append(flags)
        if self.times is None or len(self.calls) <= self.times:
            raise OSError(errno.EWOULDBLOCK, "Resource temporarily unavailable")


class TimedLock(LedgerCase):
    def patch_lock_seams(self, flock, clock):
        for name, fake in (
            ("_flock", flock),
            ("_monotonic", clock.monotonic),
            ("_sleep", clock.sleep),
        ):
            patcher = mock.patch.object(hl, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_free_lock_needs_no_wait(self):
        clock = _FakeClock()
        self.patch_lock_seams(_ContendedFlock(0), clock)
        with hl._locked(self.path):
            pass
        self.assertEqual(clock.sleeps, [])

    def test_contention_retries_until_the_lock_frees(self):
        clock = _FakeClock()
        flock = _ContendedFlock(3)
        self.patch_lock_seams(flock, clock)
        with hl._locked(self.path):
            pass
        acquires = [flags for flags in flock.calls if flags & fcntl.LOCK_EX]
        self.assertEqual(len(acquires), 4)
        self.assertEqual(len(clock.sleeps), 3)
        self.assertEqual(flock.calls[-1], fcntl.LOCK_UN)

    def test_a_lock_busy_past_the_timeout_refuses_with_75(self):
        clock = _FakeClock()
        self.patch_lock_seams(_ContendedFlock(None), clock)
        change = mock.Mock(side_effect=lambda ledger: ledger)
        with self.assertRaises(hl.LedgerLocked) as caught:
            hl.update(change, self.path, self.state, timeout=hl.LOCK_TIMEOUT_S)
        self.assertIn("try later", str(caught.exception))
        self.assertEqual(hl.LedgerLocked.exit_code, 75)
        change.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_a_non_contention_flock_error_raises_at_once(self):
        clock = _FakeClock()

        def bad_flock(fd, flags):
            raise OSError(errno.EINVAL, "Invalid argument")

        self.patch_lock_seams(bad_flock, clock)
        with self.assertRaises(hl.LedgerError) as caught:
            hl.update(lambda ledger: ledger, self.path, self.state)
        self.assertEqual(hl.LedgerError.exit_code, 4)
        self.assertIn(self.path, str(caught.exception))
        self.assertEqual(clock.sleeps, [])

    def test_a_lock_held_by_a_thread_blocks_then_lands(self):
        hl.save(hl.empty(), self.path)
        entered = threading.Event()
        release = threading.Event()

        def slow(ledger):
            entered.set()
            self.assertTrue(release.wait(timeout=10))
            return ledger

        first = threading.Thread(target=hl.update, args=(slow, self.path, self.state), daemon=True)
        first.start()
        self.assertTrue(entered.wait(timeout=10))
        timer = threading.Timer(0.5, release.set)
        timer.start()
        try:
            hl.update(lambda ledger: ledger, self.path, self.state)
        finally:
            timer.cancel()
        first.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertEqual(hl.load(self.path), hl.empty())


class Guard(LedgerCase):
    def test_ensure_creates_the_ledger_and_marks_the_state_dir(self):
        ledger = hl.ensure(self.state, self.path)
        self.assertEqual(ledger, hl.empty())
        self.assertEqual(hl.load(self.path), hl.empty())
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_ensure_with_a_matching_marker_reads_the_ledger(self):
        hl.ensure(self.state, self.path)
        out = hl.update(
            lambda ledger: {**ledger, "host_budget": {"cpu_m": 1}},
            self.path,
            self.state,
        )
        self.assertEqual(hl.ensure(self.state, self.path), out)

    def test_ensure_with_a_foreign_state_dir_refuses_with_4(self):
        hl.ensure(self.state, self.path)
        before = self.read()
        with self.assertRaises(hl.StateDirError) as caught:
            hl.ensure(OTHER, self.path)
        message = str(caught.exception)
        self.assertIn(self.state, message)
        self.assertIn(OTHER, message)
        self.assertIn(self.marker, message)
        self.assertEqual(hl.StateDirError.exit_code, 4)
        self.assertEqual(self.read(), before)
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_update_with_a_foreign_state_dir_calls_no_change(self):
        hl.ensure(self.state, self.path)
        change = mock.Mock(side_effect=lambda ledger: ledger)
        with self.assertRaises(hl.StateDirError):
            hl.update(change, self.path, OTHER)
        change.assert_not_called()

    def test_a_ledger_without_a_marker_adopts_the_callers_state_dir(self):
        hl.save(hl.empty(), self.path)
        self.assertEqual(hl.ensure(self.state, self.path), hl.empty())
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_a_foreign_marker_with_a_missing_ledger_still_refuses(self):
        with open(self.marker, "w", encoding="utf-8") as handle:
            handle.write(OTHER)
        with self.assertRaises(hl.StateDirError):
            hl.ensure(self.state, self.path)
        self.assertFalse(os.path.exists(self.path))


class Defaults(unittest.TestCase):
    def test_ledger_path_comes_from_paths_host_ledger_path(self):
        with mock.patch.object(
            paths, "host_ledger_path", return_value="/x/state/corral/host-resources.json"
        ) as ledger:
            self.assertEqual(hl.ledger_path(), "/x/state/corral/host-resources.json")
            ledger.assert_called_once_with()

    def test_marker_path_sits_beside_the_ledger(self):
        self.assertEqual(
            hl.marker_path("/x/state/corral/host-resources.json"),
            os.path.join("/x/state/corral", hl.MARKER_NAME),
        )

    def test_marker_path_defaults_to_the_resolved_ledger(self):
        with mock.patch.object(
            paths, "host_ledger_path", return_value="/x/state/corral/host-resources.json"
        ):
            self.assertEqual(hl.marker_path(), os.path.join("/x/state/corral", hl.MARKER_NAME))


class NoLiveSeams(unittest.TestCase):
    def test_the_module_imports_no_live_seam(self):
        root = Path(__file__).resolve().parent.parent
        source = root / "src" / "corral" / "host_ledger.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        allowed = {
            "collections.abc",
            "contextlib",
            "corral",
            "errno",
            "fcntl",
            "json",
            "os",
            "time",
        }
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(part.name for part in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
        self.assertEqual(found - allowed, set())


if __name__ == "__main__":
    unittest.main()
