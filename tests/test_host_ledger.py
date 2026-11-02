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

    def test_update_and_load_round_trip(self):
        ledger = {
            "version": 1,
            "host_budget": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
            "projects": {"web": {"weight": 1, "cpu_floor_m": 500, "memory_floor_mb": 1024}},
            "foreign": [],
        }
        out = hl.update(lambda _old: ledger, path=self.path, state_dir=self.state)
        self.assertEqual(out, ledger)
        self.assertEqual(hl.load(self.path), ledger)

    def test_a_mutating_change_returning_none_leaves_the_file_unchanged(self):
        hl.ensure(state_dir=self.state, path=self.path)
        before = hl.load(self.path)

        def change(ledger):
            ledger["foreign"].append({"name": "desk", "claimed_at": 0.0})
            return None

        out = hl.update(change, path=self.path, state_dir=self.state)
        self.assertEqual(hl.load(self.path), before)
        self.assertEqual(len(out["foreign"]), 1)

    def test_path_state_dir_and_timeout_are_keyword_only(self):
        with self.assertRaises(TypeError):
            hl.ensure(self.state, self.path)  # ty: ignore[too-many-positional-arguments]
        with self.assertRaises(TypeError):
            hl.update(
                lambda ledger: ledger,
                self.path,  # ty: ignore[too-many-positional-arguments]
                self.state,
            )

    def test_ensure_creates_missing_parent_dirs(self):
        nested = os.path.join(self.dir, "sub", "dir", "host-resources.json")
        hl.ensure(state_dir=self.state, path=nested)
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
        out = hl.update(
            lambda ledger: {**ledger, "future": {"v": 1}}, path=self.path, state_dir=self.state
        )
        self.assertEqual(out["future"], {"v": 1})
        self.assertEqual(hl.load(self.path)["future"], {"v": 1})

    def test_failed_update_leaves_the_corrupt_file_untouched(self):
        self.write("{not json")
        change = mock.Mock(side_effect=lambda ledger: ledger)
        with self.assertRaises(hl.LedgerError):
            hl.update(change, path=self.path, state_dir=self.state)
        change.assert_not_called()
        self.assertEqual(self.read(), "{not json")

    def test_update_applies_a_change_under_the_lock(self):
        hl.ensure(state_dir=self.state, path=self.path)
        out = hl.update(
            lambda ledger: {**ledger, "foreign": [{"name": "desk", "claimed_at": 0.0}]},
            path=self.path,
            state_dir=self.state,
        )
        self.assertEqual(hl.load(self.path)["foreign"], out["foreign"])
        self.assertEqual(len(out["foreign"]), 1)

    def test_a_change_returning_none_still_creates_the_missing_file(self):
        out = hl.update(lambda ledger: None, path=self.path, state_dir=self.state)
        self.assertEqual(out, hl.empty())
        self.assertEqual(hl.load(self.path), hl.empty())

    def test_no_public_unguarded_writer(self):
        self.assertFalse(hasattr(hl, "save"))

    def test_write_to_a_foreign_state_dir_refuses_with_4(self):
        hl.ensure(state_dir=self.state, path=self.path)
        before = self.read()
        change = mock.Mock(side_effect=lambda ledger: ledger)
        with self.assertRaises(hl.StateDirError) as caught:
            hl.update(change, path=self.path, state_dir=OTHER)
        self.assertEqual(hl.StateDirError.exit_code, 4)
        change.assert_not_called()
        self.assertEqual(self.read(), before)
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_duplicate_foreign_claim_names_refuse_naming_the_name(self):
        self.write(
            json.dumps(
                {
                    "version": 1,
                    "host_budget": None,
                    "projects": {},
                    "foreign": [
                        {"name": "desk", "claimed_at": 0.0},
                        {"name": "desk", "claimed_at": 1.0},
                    ],
                }
            )
        )
        with self.assertRaises(hl.LedgerError) as caught:
            hl.load(self.path)
        self.assertIn("desk", str(caught.exception))
        self.assertEqual(hl.LedgerError.exit_code, 4)

    def test_a_directory_as_the_ledger_path_refuses_with_4(self):
        with self.assertRaises(hl.LedgerError) as caught:
            hl.load(self.dir)
        self.assertIn(self.dir, str(caught.exception))
        self.assertEqual(hl.LedgerError.exit_code, 4)
        with self.assertRaises(hl.LedgerError):
            hl.ensure(state_dir=self.state, path=self.dir)

    def test_ensure_on_a_corrupt_file_refuses_and_leaves_bytes_identical(self):
        self.write("{not json")
        with self.assertRaises(hl.LedgerError):
            hl.ensure(state_dir=self.state, path=self.path)
        self.assertEqual(self.read(), "{not json")

    def test_update_writes_the_marker_beside_a_new_ledger(self):
        hl.update(lambda ledger: None, path=self.path, state_dir=self.state)
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_concurrent_updates_all_land(self):
        hl.ensure(state_dir=self.state, path=self.path)
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
                    path=self.path,
                    state_dir=self.state,
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
            hl.update(change, path=self.path, state_dir=self.state, timeout=hl.LOCK_TIMEOUT_S)
        self.assertIn("try later", str(caught.exception))
        self.assertEqual(hl.LedgerLocked.exit_code, 75)
        change.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_ledger_locked_is_not_a_ledger_error(self):
        self.assertEqual(hl.LedgerLocked.exit_code, 75)
        self.assertEqual(hl.LedgerError.exit_code, 4)
        self.assertFalse(issubclass(hl.LedgerLocked, hl.LedgerError))
        self.assertNotIsInstance(hl.LedgerLocked("busy"), hl.LedgerError)
        with self.assertRaises(hl.LedgerLocked):
            raise hl.LedgerLocked("busy")

    def test_a_non_contention_flock_error_raises_at_once(self):
        clock = _FakeClock()

        def bad_flock(fd, flags):
            raise OSError(errno.EINVAL, "Invalid argument")

        self.patch_lock_seams(bad_flock, clock)
        with self.assertRaises(hl.LedgerError) as caught:
            hl.update(lambda ledger: ledger, path=self.path, state_dir=self.state)
        self.assertEqual(hl.LedgerError.exit_code, 4)
        self.assertIn(self.path, str(caught.exception))
        self.assertEqual(clock.sleeps, [])

    def test_a_lock_held_by_a_thread_blocks_then_lands(self):
        hl.ensure(state_dir=self.state, path=self.path)
        entered = threading.Event()
        release = threading.Event()

        def slow(ledger):
            entered.set()
            self.assertTrue(release.wait(timeout=10))
            return ledger

        first = threading.Thread(
            target=hl.update,
            kwargs={"change": slow, "path": self.path, "state_dir": self.state},
            daemon=True,
        )
        first.start()
        self.assertTrue(entered.wait(timeout=10))
        timer = threading.Timer(0.5, release.set)
        timer.start()
        try:
            hl.update(lambda ledger: ledger, path=self.path, state_dir=self.state)
        finally:
            timer.cancel()
        first.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertEqual(hl.load(self.path), hl.empty())


class Guard(LedgerCase):
    def test_ensure_creates_the_ledger_and_marks_the_state_dir(self):
        ledger = hl.ensure(state_dir=self.state, path=self.path)
        self.assertEqual(ledger, hl.empty())
        self.assertEqual(hl.load(self.path), hl.empty())
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_ensure_with_a_matching_marker_reads_the_ledger(self):
        hl.ensure(state_dir=self.state, path=self.path)
        out = hl.update(
            lambda ledger: {**ledger, "host_budget": {"cpu_m": 1}},
            path=self.path,
            state_dir=self.state,
        )
        self.assertEqual(hl.ensure(state_dir=self.state, path=self.path), out)

    def test_a_trailing_slash_state_dir_matches_itself(self):
        hl.ensure(state_dir=self.state + "/", path=self.path)
        hl.ensure(state_dir=self.state, path=self.path)
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_ensure_with_a_foreign_state_dir_refuses_with_4(self):
        hl.ensure(state_dir=self.state, path=self.path)
        before = self.read()
        with self.assertRaises(hl.StateDirError) as caught:
            hl.ensure(state_dir=OTHER, path=self.path)
        message = str(caught.exception)
        self.assertIn(self.state, message)
        self.assertIn(OTHER, message)
        self.assertIn(self.marker, message)
        self.assertEqual(hl.StateDirError.exit_code, 4)
        self.assertEqual(self.read(), before)
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_update_with_a_foreign_state_dir_calls_no_change(self):
        hl.ensure(state_dir=self.state, path=self.path)
        change = mock.Mock(side_effect=lambda ledger: ledger)
        with self.assertRaises(hl.StateDirError):
            hl.update(change, path=self.path, state_dir=OTHER)
        change.assert_not_called()

    def test_a_ledger_without_a_marker_adopts_the_callers_state_dir(self):
        self.write(json.dumps(hl.empty()))
        self.assertEqual(hl.ensure(state_dir=self.state, path=self.path), hl.empty())
        with open(self.marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.state)

    def test_a_foreign_marker_with_a_missing_ledger_still_refuses(self):
        with open(self.marker, "w", encoding="utf-8") as handle:
            handle.write(OTHER)
        with self.assertRaises(hl.StateDirError):
            hl.ensure(state_dir=self.state, path=self.path)
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


class DropIns(LedgerCase):
    """The drop-in foreign-claim dir, merged at read, never persisted."""

    def claims_dir(self):
        return os.path.join(self.dir, hl.FOREIGN_CLAIMS_DIR)

    def drop(self, name, payload):
        os.makedirs(self.claims_dir(), exist_ok=True)
        with open(os.path.join(self.claims_dir(), name), "w", encoding="utf-8") as handle:
            handle.write(payload if isinstance(payload, str) else json.dumps(payload))

    def test_the_claims_dir_sits_beside_the_ledger(self):
        self.assertEqual(hl.claims_dir(self.path), os.path.join(self.dir, hl.FOREIGN_CLAIMS_DIR))

    def test_a_missing_dir_reads_as_no_claims(self):
        self.assertEqual(hl.drop_in_claims(self.claims_dir()), [])

    def test_a_drop_in_merges_after_the_ledger_entries(self):
        self.write(json.dumps({**hl.empty(), "foreign": [{"name": "ledger", "cpu_m": 1}]}))
        self.drop("b.json", {"name": "b", "cpu_m": 2})
        self.drop("a.json", {"name": "a", "cpu_m": 3})
        merged = hl.load(self.path)
        self.assertEqual([entry["name"] for entry in merged["foreign"]], ["ledger", "a", "b"])

    def test_non_json_files_are_ignored(self):
        self.drop("note.txt", "not a claim")
        self.assertEqual(hl.drop_in_claims(self.claims_dir()), [])

    def test_a_subdirectory_counts_as_an_ignored_entry(self):
        os.makedirs(os.path.join(self.claims_dir(), "stale.d"), exist_ok=True)
        self.assertEqual(hl.ignored_drop_ins(self.claims_dir()), ["stale.d"])

    def test_a_claim_without_a_name_refuses_naming_the_file(self):
        self.drop("bad.json", {"cpu_m": 1})
        with self.assertRaisesRegex(hl.LedgerError, r"bad\.json"):
            hl.drop_in_claims(self.claims_dir())

    def test_malformed_json_refuses_naming_the_file(self):
        self.drop("bad.json", "{nope")
        with self.assertRaisesRegex(hl.LedgerError, r"bad\.json"):
            hl.drop_in_claims(self.claims_dir())

    def test_a_scalar_refuses_naming_the_file(self):
        self.drop("bad.json", "[1, 2]")
        with self.assertRaisesRegex(hl.LedgerError, r"bad\.json"):
            hl.drop_in_claims(self.claims_dir())

    def test_an_unknown_key_refuses_naming_the_key(self):
        self.drop("bad.json", {"name": "x", "turbo": True})
        with self.assertRaisesRegex(hl.LedgerError, r"bad\.json.*turbo"):
            hl.drop_in_claims(self.claims_dir())

    def test_a_bad_amount_refuses_naming_the_file(self):
        self.drop("bad.json", {"name": "x", "cpu_m": -1})
        with self.assertRaisesRegex(hl.LedgerError, r"bad\.json"):
            hl.drop_in_claims(self.claims_dir())

    def test_claimed_at_defaults_to_the_file_mtime(self):
        self.drop("a.json", {"name": "a", "cpu_m": 1})
        (found,) = hl.drop_in_claims(self.claims_dir())
        mtime = os.path.getmtime(os.path.join(self.claims_dir(), "a.json"))
        self.assertEqual(found["claimed_at"], mtime)

    def test_an_explicit_claimed_at_wins_over_the_mtime(self):
        self.drop("a.json", {"name": "a", "cpu_m": 1, "claimed_at": 12.5})
        (found,) = hl.drop_in_claims(self.claims_dir())
        self.assertEqual(found["claimed_at"], 12.5)

    def test_a_duplicate_name_across_ledger_and_drop_ins_refuses(self):
        self.write(json.dumps({**hl.empty(), "foreign": [{"name": "desk", "cpu_m": 1}]}))
        self.drop("desk.json", {"name": "desk", "cpu_m": 2})
        with self.assertRaisesRegex(hl.LedgerError, "desk"):
            hl.load(self.path)

    def test_update_never_persists_drop_ins(self):
        self.write(json.dumps(hl.empty()))
        self.drop("desk.json", {"name": "desk", "cpu_m": 100, "claimed_at": 1.0})
        out = hl.update(
            lambda ledger: {**ledger, "host_budget": {"cpu_m": 1}},
            path=self.path,
            state_dir=self.state,
        )
        self.assertEqual(out["host_budget"], {"cpu_m": 1})
        self.assertEqual(out["foreign"], [])
        self.assertEqual(hl.load(self.path)["foreign"][0]["name"], "desk")

    def test_a_drop_in_added_mid_update_keeps_the_ledger_owned_claim(self):
        self.write(json.dumps({**hl.empty(), "foreign": [{"name": "desk", "cpu_m": 1}]}))

        def change(ledger):
            self.drop("desk.json", {"name": "desk", "cpu_m": 999, "claimed_at": 1.0})
            return {**ledger, "host_budget": {"cpu_m": 1}}

        out = hl.update(change, path=self.path, state_dir=self.state)
        self.assertEqual(out["foreign"], [{"name": "desk", "cpu_m": 1}])
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["foreign"], [{"name": "desk", "cpu_m": 1}])

    def test_a_change_returning_copies_persists_only_ledger_owned_claims(self):
        self.write(json.dumps({**hl.empty(), "foreign": [{"name": "desk", "cpu_m": 1}]}))
        self.drop("away.json", {"name": "away", "cpu_m": 999, "claimed_at": 1.0})

        def change(ledger):
            return {**ledger, "foreign": [dict(entry) for entry in ledger["foreign"]]}

        out = hl.update(change, path=self.path, state_dir=self.state)
        self.assertEqual(out["foreign"], [{"name": "desk", "cpu_m": 1}])
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["foreign"], [{"name": "desk", "cpu_m": 1}])
        self.assertEqual(
            [entry["name"] for entry in hl.load(self.path)["foreign"]], ["desk", "away"]
        )

    def test_an_unlistable_claims_dir_refuses(self):
        with open(os.path.join(self.dir, "file-not-dir"), "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertRaises(hl.LedgerError):
            hl.drop_in_claims(os.path.join(self.dir, "file-not-dir"))

    def test_the_error_is_a_configuration_error(self):
        self.drop("bad.json", "{nope")
        with self.assertRaises(hl.LedgerError) as caught:
            hl.drop_in_claims(self.claims_dir())
        self.assertEqual(hl.LedgerError.exit_code, 4)


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
            "math",
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
