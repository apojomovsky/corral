"""Tests for corral.budget.

The budget is arithmetic over a diff's changed-line count plus one git
call, so every test either exercises the pure functions directly or stubs
the git call: no test needs a real repository.

The invariant that matters is on the numbers a dispatcher pastes, not on
any intermediate: the printed pair must itself satisfy the review gate's
floor (hard limit at least three times the expectation) and stay under the
machine ceiling.
"""

import subprocess
import unittest
from unittest import mock

from corral import budget as bu


class BudgetTest(unittest.TestCase):
    def test_grows_with_the_diff(self):
        small = bu.budget(10)
        large = bu.budget(1000)
        self.assertLess(small[0], large[0], "more lines must ask for more time")
        self.assertLess(small[1], large[1])

    def test_carries_the_fixed_floor_for_build_and_gates(self):
        expected, limit = bu.budget(0)
        self.assertEqual(expected, bu.BASE_MINUTES)
        self.assertEqual(limit, bu.BASE_MINUTES * bu.LIMIT_MULTIPLE)

    def test_the_printed_pair_always_satisfies_the_gate_floor(self):
        # The regression this guards: rounding the expectation and the limit
        # independently printed 22/65 for a 66-line diff, where 3 x 22 > 65.
        for lines in (0, 1, 66, 100, 230, 231, 530, 100_000):
            expected, limit = bu.budget(lines)
            self.assertGreaterEqual(
                limit * 1,
                expected * bu.LIMIT_MULTIPLE,
                f"{lines} lines printed {expected}m/{limit}m, below the 3x floor",
            )


class CeilingTest(unittest.TestCase):
    """A limit the dispatcher cannot act on is not a limit. The runtime kills
    a subagent at the machine ceiling, so a stated hard limit at or above it
    would never be reached deliberately."""

    def test_no_budget_ever_reaches_the_machine_ceiling(self):
        for lines in (0, 100, 230, 530, 2000, 100_000):
            _, limit = bu.budget(lines)
            self.assertLess(
                limit,
                bu.MACHINE_CEILING_MINUTES,
                f"{lines} lines produced an unreachable limit",
            )

    def test_three_times_the_expectation_always_fits(self):
        for lines in (0, 100, 530, 100_000):
            expected, _ = bu.budget(lines)
            self.assertLessEqual(expected, bu.MAX_EXPECTED_MINUTES)
            self.assertGreaterEqual(expected, bu.BASE_MINUTES)

    def test_the_ceiling_is_still_above_the_gate_minimum(self):
        # If these ever crossed, every budget would be forced outside the
        # gate's own bounds and no line count would be reviewable at all.
        self.assertGreater(bu.MAX_LIMIT_MINUTES, bu.GATE_MIN_LIMIT_MINUTES)

    def test_the_gate_minimum_holds_for_every_diff(self):
        # The floor holds because the base expectation alone exceeds a third
        # of it; asserting it keeps that a fact rather than a coincidence if
        # BASE_MINUTES or LIMIT_MULTIPLE is ever tuned.
        for lines in (0, 1, 66, 240, 1000, 100_000):
            _, limit = bu.budget(lines)
            self.assertGreaterEqual(limit, bu.GATE_MIN_LIMIT_MINUTES, f"{lines} lines")

    def test_the_constants_keep_the_floor_satisfiable(self):
        lowest_lock = bu.BASE_MINUTES * bu.LIMIT_MULTIPLE
        self.assertGreaterEqual(
            lowest_lock,
            bu.GATE_MIN_LIMIT_MINUTES,
            "the smallest possible limit must clear the gate's minimum",
        )


class SplitTest(unittest.TestCase):
    def test_a_short_diff_is_one_review(self):
        self.assertFalse(bu.needs_splitting(100))

    def test_a_530_line_diff_is_reported_as_needing_a_split(self):
        # 530 added lines is a real review that overran its budget.
        self.assertTrue(bu.needs_splitting(530))

    def test_splitting_is_flagged_exactly_when_the_expectation_is_shortened(self):
        # The threshold and the cap must be the same boundary, or a diff
        # gets a shortened expectation with no warning (or a warning with a
        # full-length one).
        for lines in (0, 229, 230, 231, 1000):
            shortened = bu.natural_expected_minutes(lines) > bu.MAX_EXPECTED_MINUTES
            self.assertEqual(bu.needs_splitting(lines), shortened, f"{lines} lines")

    def test_the_boundary_line_count_is_flagged(self):
        # The line count that saturates the ceiling is the dangerous one:
        # it must warn rather than pass silently.
        at_cap = bu.max_reviewable_lines()
        self.assertTrue(bu.needs_splitting(at_cap + 1))
        _, limit = bu.budget(at_cap + 1)
        self.assertLess(limit, bu.MACHINE_CEILING_MINUTES)


class ChangedLinesTest(unittest.TestCase):
    def _run(self, stdout, returncode=0):
        proc = subprocess.CompletedProcess([], returncode, stdout, "")
        return mock.patch.object(bu, "_git", return_value=proc)

    def test_counts_added_and_removed_lines(self):
        with self._run("10\t2\tsrc/a.rs\n3\t4\tsrc/b.rs\n"):
            self.assertEqual(bu.changed_lines("origin/master"), 19)

    def test_an_unresolvable_range_is_none_not_zero(self):
        # Zero would read as "a tiny diff"; None lets the caller say it does
        # not know rather than print a budget computed from nothing.
        with self._run("", returncode=128):
            self.assertIsNone(bu.changed_lines("nope"))

    def test_binary_files_contribute_no_lines(self):
        with self._run("-\t-\tassets/logo.png\n5\t0\tsrc/a.rs\n"):
            self.assertEqual(bu.changed_lines("origin/master"), 5)

    def test_a_rename_reports_the_edit_not_the_path(self):
        # git reports a rename's content change the same way, so the path
        # shape must not change the count.
        with self._run("4\t0\tsrc/{old.rs => new.rs}\n"):
            self.assertEqual(bu.changed_lines("origin/master"), 4)

    def test_an_empty_diff_is_zero(self):
        with self._run(""):
            self.assertEqual(bu.changed_lines("origin/master"), 0)

    def test_a_malformed_line_is_skipped_not_fatal(self):
        with self._run("garbage\n5\t0\tsrc/a.rs\n"):
            self.assertEqual(bu.changed_lines("origin/master"), 5)


class DispatchBlockTest(unittest.TestCase):
    def test_names_the_question_scope_and_budget(self):
        proc = subprocess.CompletedProcess([], 0, "100\t50\tsrc/a.rs\n", "")
        with mock.patch.object(bu, "_git", return_value=proc):
            text = bu.dispatch_block("origin/master")
        self.assertIn("question:", text)
        self.assertIn("expected", text)
        self.assertIn("hard limit", text)
        self.assertIn("150 changed lines", text)
        self.assertIn("PARTIAL", text, "the wrap-up rule must travel with the budget")

    def test_says_so_when_the_budget_cannot_be_computed(self):
        proc = subprocess.CompletedProcess([], 128, "", "")
        with mock.patch.object(bu, "_git", return_value=proc):
            text = bu.dispatch_block("nope")
        self.assertIn("unknown", text)
        self.assertIn("does not resolve", text)

    def test_an_oversized_diff_tells_the_dispatcher_to_split(self):
        proc = subprocess.CompletedProcess([], 0, "530\t0\tsrc/a.rs\n", "")
        with mock.patch.object(bu, "_git", return_value=proc):
            text = bu.dispatch_block("origin/master")
        self.assertIn("Split it", text)
        self.assertIn("530 lines", text)


class CliTest(unittest.TestCase):
    def test_budget_verb_prints_the_block(self):
        from corral import cli

        proc = subprocess.CompletedProcess([], 0, "4\t0\tsrc/a.rs\n", "")
        argv = ["budget"]
        with mock.patch.object(bu, "_git", return_value=proc):
            rc = cli.main(argv)
        self.assertEqual(rc, 0)

    def test_budget_verb_accepts_an_explicit_base(self):
        from corral import cli

        proc = subprocess.CompletedProcess([], 0, "4\t0\tsrc/a.rs\n", "")
        with mock.patch.object(bu, "_git", return_value=proc):
            rc = cli.main(["budget", "--base", "HEAD~3"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
