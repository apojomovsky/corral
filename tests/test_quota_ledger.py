"""The quota ledger: readings, reactive marks, and the locked quota.json file."""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from corral import paths, quota_ledger as ql

T0 = 1_800_000_000.0


def windows(used=0.25, resets_at=T0 + 3600):
    return {"session": {"used_pct": used, "resets_at": resets_at}}


class RecordReadings(unittest.TestCase):
    def test_success_stores_windows_and_read_at(self):
        fresh = ql.record_reading(ql.empty(), "provider-a", windows(), T0)
        self.assertEqual(
            fresh["readings"]["provider-a"],
            {"windows": windows(), "read_at": T0},
        )

    def test_success_replaces_a_recorded_failure(self):
        failed = ql.record_failure(ql.empty(), "provider-a", "boom", T0)
        fresh = ql.record_reading(failed, "provider-a", windows(), T0 + 10)
        self.assertNotIn("error", fresh["readings"]["provider-a"])
        self.assertEqual(fresh["readings"]["provider-a"]["read_at"], T0 + 10)

    def test_success_leaves_other_providers_alone(self):
        ledger = ql.record_reading(ql.empty(), "provider-a", windows(), T0)
        fresh = ql.record_reading(ledger, "provider-b", windows(used=0.5), T0)
        self.assertEqual(fresh["readings"]["provider-a"]["windows"], windows())
        self.assertEqual(fresh["readings"]["provider-b"]["windows"], windows(used=0.5))

    def test_success_does_not_mutate_its_input(self):
        ledger = ql.empty()
        ql.record_reading(ledger, "provider-a", windows(), T0)
        self.assertEqual(ledger, ql.empty())

    def test_success_rejects_a_non_numeric_window(self):
        with self.assertRaises(ql.LedgerError):
            ql.record_reading(ql.empty(), "provider-a", {"session": {"used_pct": "high"}}, T0)

    def test_failure_stores_error_and_time_with_no_windows(self):
        fresh = ql.record_failure(ql.empty(), "provider-a", "reader timed out", T0)
        entry = fresh["readings"]["provider-a"]
        self.assertEqual(entry, {"error": "reader timed out", "at": T0})
        self.assertNotIn("windows", entry)

    def test_failure_replaces_a_previous_reading(self):
        ledger = ql.record_reading(ql.empty(), "provider-a", windows(), T0)
        fresh = ql.record_failure(ledger, "provider-a", "reader timed out", T0 + 10)
        self.assertNotIn("windows", fresh["readings"]["provider-a"])

    def test_failure_trims_long_text_to_one_line(self):
        long_error = "line one\nline two  " + "x" * (ql.ERROR_TEXT_LIMIT + 100)
        fresh = ql.record_failure(ql.empty(), "provider-a", long_error, T0)
        error = fresh["readings"]["provider-a"]["error"]
        self.assertNotIn("\n", error)
        self.assertLessEqual(len(error), ql.ERROR_TEXT_LIMIT)


class ReactiveMarks(unittest.TestCase):
    def test_posting_a_new_mark_reports_moved(self):
        fresh, moved = ql.post_mark(ql.empty(), "provider-a", T0 + 60, "quota", "reader")
        self.assertTrue(moved)
        self.assertEqual(
            fresh["marks"]["provider-a"],
            {"until": T0 + 60, "reason": "quota", "source": "reader"},
        )

    def test_a_later_until_extends_the_mark(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 60, "quota", "reader")
        fresh, moved = ql.post_mark(ledger, "provider-a", T0 + 120, "quota", "reader")
        self.assertTrue(moved)
        self.assertEqual(fresh["marks"]["provider-a"]["until"], T0 + 120)

    def test_an_earlier_until_is_a_no_op(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 120, "quota", "reader")
        fresh, moved = ql.post_mark(ledger, "provider-a", T0 + 60, "other", "probe")
        self.assertFalse(moved)
        self.assertEqual(
            fresh["marks"]["provider-a"],
            {"until": T0 + 120, "reason": "quota", "source": "reader"},
        )

    def test_an_equal_until_is_a_no_op(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 60, "quota", "reader")
        _, moved = ql.post_mark(ledger, "provider-a", T0 + 60, "quota", "reader")
        self.assertFalse(moved)

    def test_a_non_numeric_until_is_rejected(self):
        with self.assertRaises(ql.LedgerError):
            ql.post_mark(ql.empty(), "provider-a", "soon", "quota", "reader")  # ty: ignore[invalid-argument-type]

    def test_posting_does_not_mutate_its_input(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 60)
        before = {"readings": {}, "marks": dict(ledger["marks"])}
        ql.post_mark(ledger, "provider-a", T0 + 30)
        self.assertEqual(ledger["marks"], before["marks"])

    def test_active_mark_returns_the_entry_before_its_until(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 60, "quota", "reader")
        self.assertEqual(
            ql.active_mark(ledger, "provider-a", T0 + 59),
            {"until": T0 + 60, "reason": "quota", "source": "reader"},
        )

    def test_active_mark_is_gone_at_and_past_its_until(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 60)
        self.assertIsNone(ql.active_mark(ledger, "provider-a", T0 + 60))
        self.assertIsNone(ql.active_mark(ledger, "provider-a", T0 + 61))

    def test_active_mark_is_none_for_an_unmarked_provider(self):
        self.assertIsNone(ql.active_mark(ql.empty(), "provider-b", T0))

    def test_prune_drops_only_expired_marks(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 60, "quota", "reader")
        ledger, _ = ql.post_mark(ledger, "provider-b", T0 - 1, "quota", "reader")
        fresh, dropped = ql.prune(ledger, T0)
        self.assertEqual(dropped, ["provider-b"])
        self.assertEqual(set(fresh["marks"]), {"provider-a"})
        self.assertIn("provider-a", ledger["marks"])

    def test_prune_with_nothing_expired_drops_nothing(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 60)
        fresh, dropped = ql.prune(ledger, T0)
        self.assertEqual((dropped, fresh["marks"]), ([], ledger["marks"]))

    def test_helpers_keep_unknown_top_level_keys(self):
        ledger = {**ql.empty(), "future": {"v": 1}}
        fresh = ql.record_reading(ledger, "provider-a", windows(), T0)
        self.assertEqual(fresh["future"], {"v": 1})
        fresh, _ = ql.post_mark(ledger, "provider-a", T0 + 60)
        self.assertEqual(fresh["future"], {"v": 1})


class LedgerFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "quota.json")

    def test_save_and_load_round_trip(self):
        ledger = ql.record_reading(ql.empty(), "provider-a", windows(), T0)
        ledger, _ = ql.post_mark(ledger, "provider-b", T0 + 60, "quota", "reader")
        ql.save(ledger, self.path)
        self.assertEqual(ql.load(self.path), ledger)

    def test_save_creates_missing_parent_dirs(self):
        nested = os.path.join(os.path.dirname(self.path), "sub", "dir", "quota.json")
        ql.save(ql.empty(), nested)
        self.assertEqual(ql.load(nested), ql.empty())

    def test_load_of_a_missing_file_is_an_empty_ledger(self):
        self.assertEqual(ql.load(self.path), ql.empty())

    def test_load_of_a_corrupt_file_raises(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(ql.LedgerError):
            ql.load(self.path)

    def test_load_of_a_wrong_shaped_file_raises(self):
        for bad in (["readings"], "text", {"readings": ["provider-a"]}):
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(bad, handle)
            with self.subTest(bad=bad), self.assertRaises(ql.LedgerError):
                ql.load(self.path)

    def test_failed_update_leaves_the_corrupt_file_untouched(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(ql.LedgerError):
            ql.update(lambda ledger: ledger, self.path)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json")

    def test_update_applies_a_change_under_the_lock(self):
        ql.save(ql.empty(), self.path)
        out = ql.update(
            lambda ledger: ql.record_reading(ledger, "provider-a", windows(), T0),
            self.path,
        )
        self.assertEqual(ql.load(self.path)["readings"], out["readings"])
        self.assertIn("provider-a", out["readings"])

    def test_update_accepts_an_in_place_change(self):
        ql.save(ql.empty(), self.path)

        def change(ledger):
            ledger["marks"]["provider-a"] = {"until": T0 + 60, "reason": "", "source": ""}

        ql.update(change, self.path)
        self.assertEqual(ql.load(self.path)["marks"]["provider-a"]["until"], T0 + 60)

    def test_concurrent_updates_all_land(self):
        ql.save(ql.empty(), self.path)
        errors = []

        def work(index):
            name = f"provider-{chr(ord('a') + index)}"
            try:
                ql.update(
                    lambda ledger: ql.record_reading(ledger, name, windows(), T0),
                    self.path,
                )
            except Exception as exc:  # pragma: no cover - fails the test below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(ql.load(self.path)["readings"]), 8)

    def test_concurrent_saves_never_corrupt_the_file(self):
        errors = []

        def work(index):
            name = f"provider-{chr(ord('a') + index % 4)}"
            try:
                for _ in range(10):
                    ql.save(ql.record_reading(ql.empty(), name, windows(), T0), self.path)
            except Exception as exc:  # pragma: no cover - fails the test below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        ledger = ql.load(self.path)
        self.assertEqual(len(ledger["readings"]), 1)

    def test_ledger_path_comes_from_paths_state_dir(self):
        with mock.patch.object(paths, "state_dir", return_value="/o/state") as state:
            self.assertEqual(ql.ledger_path(), "/o/state/quota.json")
            state.assert_called_once_with()

    def test_ledger_path_accepts_an_explicit_state_dir(self):
        self.assertEqual(ql.ledger_path("/o/state"), "/o/state/quota.json")


if __name__ == "__main__":
    unittest.main()
