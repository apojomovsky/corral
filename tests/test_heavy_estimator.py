"""The heavy lane, ticket 4: the estimator, the run ledger reader and rollup.

Pure statistics over raw ledger rows: robust per-key estimates (median
plus high percentile, small-sample maximum, starved exclusion, cold
start, role fallback, clamping), the wait-deadline and ETA feeds, flaky
detection, and the portable rollup document. Tests use synthetic rows
only: no systemd, no /proc or /sys, no network, and the state dir stays
under temp dirs.
"""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from korapy import heavy, resources, settings


def _sandbox(test):
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for var in ("KORAPY_CONFIG", "KORAPY_STATE", "KORAPY_CACHE", "KORAPY_SESSIONS"):
        os.environ[var] = os.path.join(test.tmp, var.lower().replace("korapy_", ""))
    os.environ["HOME"] = os.path.join(test.tmp, "home")


NOW = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()


def _at(days_ago=0.0):
    moment = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
    moment -= datetime.timedelta(days=days_ago)
    return moment.isoformat()


def _row(
    repo="a-project",
    argv_hash="abc123",
    role="takeoff",
    days_ago=0.0,
    wall_s=180.0,
    cpu_s=412.7,
    peak_mb=5210.0,
    starved=False,
    exit=0,
):
    row = {
        "schema_version": 1,
        "at": _at(days_ago),
        "repo": repo,
        "argv_hash": argv_hash,
        "role": role,
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_mb": peak_mb,
        "starved": starved,
        "exit": exit,
    }
    return row


class EstimatorCase(unittest.TestCase):
    tmp: str

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-estimator-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        _sandbox(self)


class KeyCase(EstimatorCase):
    def test_key_joins_repo_hash_and_role(self):
        self.assertEqual(
            heavy.estimate_key("a-project", "abc123", "takeoff"), "a-project|abc123|takeoff"
        )

    def test_key_pins_the_pipe_layout(self):
        self.assertEqual(heavy.estimate_key("r", "h", "o"), "r|h|o")


class ColdStartCase(EstimatorCase):
    def test_no_rows_means_no_estimates(self):
        self.assertEqual(heavy.estimates_for([], now=NOW), {})

    def test_only_invalid_rows_means_no_estimates(self):
        rows = [{"bogus": True}, "not-a-dict", {"repo": "r"}]
        self.assertEqual(heavy.estimates_for(rows, now=NOW), {})

    def test_request_weight_without_an_estimate_uses_the_default(self):
        default = heavy.Weight(cpu_m=1000, memory_mb=1024)
        self.assertEqual(heavy.request_weight(None, default, None), default)


class YoungKeyCase(EstimatorCase):
    def test_single_sample_prices_at_what_it_measured(self):
        rows = [_row(peak_mb=6000.0, wall_s=200.0, cpu_s=400.0)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.samples, 1)
        self.assertEqual(estimate.memory_mb, 6000)
        self.assertFalse(estimate.flaky)

    def test_two_samples_predict_the_maximum_not_the_average(self):
        rows = [_row(peak_mb=1000.0), _row(peak_mb=6000.0)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.memory_mb, 6000)

    def test_young_key_reports_its_durations(self):
        rows = [_row(wall_s=100.0), _row(wall_s=300.0)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.dur_s_p50, 100.0)
        self.assertEqual(estimate.dur_s_p90, 300.0)


class SteadyStateCase(EstimatorCase):
    def _ten(self):
        return [
            _row(
                peak_mb=float(i * 100),
                wall_s=float(i * 10),
                cpu_s=float(i * 10) * 2.0,
                days_ago=float(10 - i),
            )
            for i in range(1, 11)
        ]

    def test_peak_prediction_is_the_high_percentile(self):
        (estimate,) = heavy.estimates_for(self._ten(), now=NOW).values()
        self.assertEqual(estimate.samples, 10)
        self.assertEqual(estimate.memory_mb, 900)
        self.assertEqual(estimate.peak_mb_p90, 900)

    def test_typical_duration_is_the_median(self):
        (estimate,) = heavy.estimates_for(self._ten(), now=NOW).values()
        self.assertEqual(estimate.dur_s_p50, 50.0)
        self.assertEqual(estimate.dur_s_p90, 90.0)

    def test_cpu_prediction_is_the_high_percentile_of_observed_parallelism(self):
        rows = [_row(cpu_s=100.0, wall_s=50.0, days_ago=float(9 - i)) for i in range(8)]
        rows += [_row(cpu_s=900.0, wall_s=50.0, days_ago=0.0) for _ in range(2)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.cpu_m, 18000)

    def test_missing_cpu_data_leaves_cpu_unpredicted(self):
        rows = [_row(cpu_s=None, wall_s=50.0) for _ in range(4)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertIsNone(estimate.cpu_m)
        self.assertIsNotNone(estimate.memory_mb)

    def test_request_weight_falls_back_per_dimension(self):
        rows = [_row(cpu_s=None, wall_s=50.0, peak_mb=2000.0) for _ in range(4)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        default = heavy.Weight(cpu_m=1000, memory_mb=1024)
        weight = heavy.request_weight(estimate, default, None)
        self.assertEqual(weight, heavy.Weight(cpu_m=1000, memory_mb=2000))


class StarvedCase(EstimatorCase):
    def test_starved_rows_leave_duration_learning_but_keep_their_peak(self):
        rows = [
            _row(wall_s=100.0, peak_mb=1000.0),
            _row(wall_s=200.0, peak_mb=2000.0),
            _row(wall_s=300.0, peak_mb=3000.0),
            _row(wall_s=1000.0, peak_mb=9000.0, starved=True),
        ]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.dur_s_p50, 200.0)
        self.assertEqual(estimate.memory_mb, 9000)


class WindowCase(EstimatorCase):
    def test_rows_past_ninety_days_are_forgotten(self):
        rows = [_row(peak_mb=100.0, days_ago=100.0) for _ in range(5)]
        rows += [_row(peak_mb=6000.0, days_ago=1.0)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.samples, 1)
        self.assertEqual(estimate.memory_mb, 6000)

    def test_only_the_trailing_twenty_rows_learn(self):
        rows = [_row(peak_mb=1.0, wall_s=1.0, days_ago=30.0)]
        rows += [_row(peak_mb=6000.0, wall_s=200.0, days_ago=float(19 - i)) for i in range(20)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.samples, 20)
        self.assertEqual(estimate.memory_mb, 6000)

    def test_rows_from_the_future_are_ignored(self):
        rows = [_row(peak_mb=6000.0, days_ago=1.0), _row(peak_mb=1.0, days_ago=-1.0)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual(estimate.samples, 1)
        self.assertEqual(estimate.memory_mb, 6000)


class RoleFallbackCase(EstimatorCase):
    def test_a_young_role_borrows_its_command_rows(self):
        rows = [
            _row(role="takeoff", peak_mb=float(p), days_ago=float(i))
            for i, p in enumerate([100, 200, 300, 400, 500])
        ]
        rows += [
            _row(role="hand", peak_mb=800.0, days_ago=0.0),
            _row(role="hand", peak_mb=900.0, days_ago=0.0),
        ]
        estimates = heavy.estimates_for(rows, now=NOW)
        hand = estimates["a-project|abc123|hand"]
        self.assertEqual(hand.samples, 7)
        self.assertEqual(hand.memory_mb, 900)
        takeoff = estimates["a-project|abc123|takeoff"]
        self.assertEqual(takeoff.samples, 5)
        self.assertEqual(takeoff.memory_mb, 500)

    def test_roles_never_borrow_across_commands(self):
        rows = [
            _row(argv_hash="aaa", role="takeoff", peak_mb=100.0),
            _row(argv_hash="bbb", role="hand", peak_mb=900.0),
        ]
        estimates = heavy.estimates_for(rows, now=NOW)
        self.assertEqual(estimates["a-project|aaa|takeoff"].memory_mb, 100)
        self.assertEqual(estimates["a-project|bbb|hand"].memory_mb, 900)

    def test_a_changed_command_is_a_new_key(self):
        rows = [_row(argv_hash="aaa", peak_mb=100.0) for _ in range(5)]
        rows += [_row(argv_hash="bbb", peak_mb=900.0)]
        estimates = heavy.estimates_for(rows, now=NOW)
        self.assertEqual(estimates["a-project|bbb|takeoff"].memory_mb, 900)

    def test_fallback_window_reads_the_most_recent_rows_across_roles(self):
        rows = [
            _row(
                role="takeoff",
                peak_mb=float((22 - i) * 100),
                days_ago=float(i),
                exit=0 if i >= 10 else 1,
            )
            for i in range(21, 0, -1)
        ]
        rows += [
            _row(role="hand", peak_mb=99999.0, days_ago=20.5, exit=0),
            _row(role="hand", peak_mb=2200.0, days_ago=0.0, exit=0),
        ]
        estimates = heavy.estimates_for(rows, now=NOW)
        hand = estimates["a-project|abc123|hand"]
        self.assertEqual(hand.samples, 20)
        self.assertEqual(hand.memory_mb, 2000)
        self.assertFalse(hand.flaky)


class FlakyCase(EstimatorCase):
    def test_mixed_exits_read_as_flaky(self):
        rows = [_row(exit=code) for code in [0, 0, 1, 1, 0, 0, 0, 0, 0, 0]]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertTrue(estimate.flaky)
        self.assertTrue(heavy.is_flaky([0, 0, 1, 1, 0, 0, 0, 0, 0, 0]))

    def test_clean_runs_are_not_flaky(self):
        rows = [_row(exit=0) for _ in range(6)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertFalse(estimate.flaky)
        self.assertFalse(heavy.is_flaky([0, 0, 0]))

    def test_constant_failure_is_not_flaky(self):
        self.assertFalse(heavy.is_flaky([1] * 10))

    def test_one_sided_history_is_not_flaky(self):
        self.assertFalse(heavy.is_flaky([0] * 9 + [1]))
        self.assertFalse(heavy.is_flaky([1] * 9 + [0]))

    def test_only_the_trailing_ten_exits_count(self):
        self.assertFalse(heavy.is_flaky([0, 0, 1, 1] + [0] * 10))
        self.assertTrue(heavy.is_flaky([1] * 8 + [0, 0, 1, 1] + [0] * 8))


class RequestWeightCase(EstimatorCase):
    def _estimate(self, cpu_m=2450, memory_mb=5100):
        rows = [_row(peak_mb=5100.0, cpu_s=122.5, wall_s=50.0) for _ in range(4)]
        (estimate,) = heavy.estimates_for(rows, now=NOW).values()
        self.assertEqual((estimate.cpu_m, estimate.memory_mb), (cpu_m, memory_mb))
        return estimate

    def test_predictions_pass_through_under_a_roomy_cap(self):
        estimate = self._estimate()
        cap = resources.Usage(cpu_m=4000, memory_mb=8192, pids=0)
        self.assertEqual(
            heavy.request_weight(estimate, heavy.Weight(), cap),
            heavy.Weight(cpu_m=2450, memory_mb=5100),
        )

    def test_predictions_clamp_to_the_lane_cap(self):
        estimate = self._estimate()
        cap = resources.Usage(cpu_m=2000, memory_mb=4096, pids=0)
        self.assertEqual(
            heavy.request_weight(estimate, heavy.Weight(), cap),
            heavy.Weight(cpu_m=2000, memory_mb=4096),
        )

    def test_no_cap_means_no_upper_bound(self):
        estimate = self._estimate()
        self.assertEqual(
            heavy.request_weight(estimate, heavy.Weight(), None),
            heavy.Weight(cpu_m=2450, memory_mb=5100),
        )

    def test_a_prediction_never_rounds_to_zero(self):
        estimate = self._estimate()._replace(cpu_m=0, memory_mb=0)
        self.assertEqual(
            heavy.request_weight(estimate, heavy.Weight(), None), heavy.Weight(cpu_m=1, memory_mb=1)
        )


class FeedsCase(EstimatorCase):
    def test_short_suite_keeps_the_configured_timeout(self):
        self.assertEqual(heavy.wait_deadline_s(1800, 3, 240.0), 1800)

    def test_long_suite_waits_longer_by_construction(self):
        self.assertEqual(heavy.wait_deadline_s(1800, 3, 1800.0), 5400)

    def test_no_estimate_means_the_configured_timeout(self):
        self.assertEqual(heavy.wait_deadline_s(900, 3, None), 900)

    def test_deadline_factors_must_be_positive(self):
        for bad in (0, -1, True, "3", float("nan"), float("inf")):
            with self.subTest(factor=bad):
                with self.assertRaises(heavy.HeavyError):
                    heavy.wait_deadline_s(1800, bad, 240.0)

    def test_deadline_timeouts_must_be_non_negative(self):
        for bad in (-1, True, "1800", float("nan")):
            with self.subTest(timeout=bad):
                with self.assertRaises(heavy.HeavyError):
                    heavy.wait_deadline_s(bad, 3, 240.0)

    def test_holder_eta_counts_down_the_typical_run(self):
        self.assertAlmostEqual(heavy.holder_eta_s(180.0, 38.1), 141.9)
        self.assertEqual(heavy.holder_eta_s(180.0, 200.0), 0.0)
        self.assertIsNone(heavy.holder_eta_s(None, 38.1))

    def test_waiter_eta_sums_the_blocking_durations(self):
        self.assertEqual(heavy.waiter_eta_s([100.0, 200.0]), 300.0)
        self.assertEqual(heavy.waiter_eta_s([]), 0.0)
        self.assertIsNone(heavy.waiter_eta_s([100.0, None]))


class NormalizeCase(EstimatorCase):
    def test_valid_row_normalizes(self):
        normalized = heavy.normalize_row(_row())
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["repo"], "a-project")
        self.assertEqual(normalized["at"], NOW)

    def test_missing_timestamp_skips_the_row(self):
        row = _row()
        del row["at"]
        self.assertIsNone(heavy.normalize_row(row))

    def test_garbage_timestamp_skips_the_row(self):
        row = _row()
        row["at"] = "not-a-time"
        self.assertIsNone(heavy.normalize_row(row))

    def test_negative_measurements_skip_the_row(self):
        for key in ("wall_s", "cpu_s", "peak_mb"):
            with self.subTest(key=key):
                row = _row()
                row[key] = -1.0
                self.assertIsNone(heavy.normalize_row(row))

    def test_wrong_types_skip_the_row(self):
        row = _row()
        row["starved"] = "yes"
        self.assertIsNone(heavy.normalize_row(row))
        row = _row()
        row["exit"] = "zero"
        self.assertIsNone(heavy.normalize_row(row))
        row = _row()
        row["repo"] = ""
        self.assertIsNone(heavy.normalize_row(row))

    def test_unknown_fields_and_missing_schema_are_kept(self):
        row = _row()
        row["future_field"] = "ignored"
        del row["schema_version"]
        self.assertIsNotNone(heavy.normalize_row(row))

    def test_newer_schema_is_skipped(self):
        row = _row()
        row["schema_version"] = 2
        self.assertIsNone(heavy.normalize_row(row))

    def test_non_dict_rows_are_skipped(self):
        self.assertIsNone(heavy.normalize_row([1, 2]))
        self.assertIsNone(heavy.normalize_row("row"))

    def test_normalization_is_idempotent(self):
        once = heavy.normalize_row(_row())
        assert once is not None
        self.assertEqual(heavy.normalize_row(dict(once)), once)

    def test_epoch_timestamps_pass_through(self):
        row = _row()
        row["at"] = NOW
        normalized = heavy.normalize_row(row)
        assert normalized is not None
        self.assertEqual(normalized["at"], NOW)

    def test_bad_epoch_timestamps_skip_the_row(self):
        for bad in (True, float("nan"), float("inf"), -1.0):
            with self.subTest(at=bad):
                row = _row()
                row["at"] = bad
                self.assertIsNone(heavy.normalize_row(row))


class WaitFactorConfigCase(EstimatorCase):
    def test_default_factor_is_three(self):
        cfg = heavy.check_table({}, "korapy.toml")
        self.assertEqual(cfg.wait_factor, 3.0)

    def test_explicit_factor_parses(self):
        cfg = heavy.check_table({"wait_factor": 2}, "korapy.toml")
        self.assertEqual(cfg.wait_factor, 2.0)

    def test_bad_factors_are_refused(self):
        for bad in (0, -1, True, "3", 1.5 * float("nan")):
            with self.subTest(factor=bad):
                with self.assertRaises(settings.SettingsError):
                    heavy.check_table({"wait_factor": bad}, "korapy.toml")

    def test_unknown_keys_stay_refused(self):
        with self.assertRaisesRegex(settings.SettingsError, "bogus"):
            heavy.check_table({"wait_factor": 3, "bogus": 1}, "korapy.toml")


if __name__ == "__main__":
    unittest.main()
