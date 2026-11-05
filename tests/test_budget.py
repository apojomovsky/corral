"""Tests for korapy.budget.

The budget is arithmetic over a diff's changed-line count plus one git
call, so every test either exercises the pure functions directly or stubs
the git call: no test needs a real repository.

The invariant that matters is on the numbers a dispatcher pastes, not on
any intermediate: the printed pair must itself satisfy the review gate's
floor (hard limit at least three times the expectation) and stay under the
machine ceiling.
"""

import io
import os
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
import unittest
from unittest import mock

from korapy import budget as bu
from korapy import settings as S


def resolve_settings(**kw):
    """Settings resolved with no ambient environment: hermetic by default."""
    kw.setdefault("env", {})
    return S.resolve(**kw)


CUSTOM = {
    "base_minutes": 10,
    "seconds_per_line": 10,
    "limit_multiple": 2,
    "floor_minutes": 20,
    "ceiling_minutes": 60,
}


def custom_project():
    return {"review": {"budget": dict(CUSTOM)}}


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

    def test_a_hand_built_floor_violation_is_a_settings_error(self):
        # numbers() never sees a hand-built Numbers, so budget() reports
        # the broken floor itself: a SettingsError (exit 4), not a traceback.
        cfg = bu.Numbers(
            base_minutes=1,
            seconds_per_line=1,
            limit_multiple=1,
            floor_minutes=30,
            ceiling_minutes=120,
        )
        with self.assertRaisesRegex(S.SettingsError, "below the 30-minute floor"):
            bu.budget(0, cfg)


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
            self.assertLessEqual(expected, bu._max_expected(bu.numbers()))
            self.assertGreaterEqual(expected, bu.BASE_MINUTES)

    def test_the_ceiling_is_still_above_the_gate_minimum(self):
        # If these ever crossed, every budget would be forced outside the
        # gate's own bounds and no line count would be reviewable at all.
        top = bu._max_expected(bu.numbers()) * bu.LIMIT_MULTIPLE
        self.assertLess(top, bu.MACHINE_CEILING_MINUTES)
        self.assertGreater(top, bu.GATE_MIN_LIMIT_MINUTES)

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
            shortened = bu.natural_expected_minutes(lines) > bu._max_expected(bu.numbers())
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

    def test_the_trailer_uses_the_configured_numbers(self):
        # The regression this guards: the header computed from cfg while
        # the trailer resolved fresh defaults, so one block printed two
        # contradictory budgets.
        cfg = bu.Numbers(
            base_minutes=10,
            seconds_per_line=10,
            limit_multiple=2,
            floor_minutes=20,
            ceiling_minutes=60,
        )
        proc = subprocess.CompletedProcess([], 0, "100\t0\tsrc/a.rs\n", "")
        with mock.patch.object(bu, "_git", return_value=proc):
            text = bu.dispatch_block("origin/master", cfg=cfg)
        self.assertIn("expected 27 minutes, hard limit 54 minutes", text)
        self.assertIn("expected duration: 27 minutes", text)
        self.assertIn("hard limit: 54 minutes", text)


class CliTest(unittest.TestCase):
    def test_budget_verb_prints_the_block(self):
        from korapy import cli

        proc = subprocess.CompletedProcess([], 0, "4\t0\tsrc/a.rs\n", "")
        out = io.StringIO()
        with mock.patch.object(bu, "_git", return_value=proc), redirect_stdout(out):
            rc = cli.main(["budget"])
        self.assertEqual(rc, 0)
        self.assertIn("expected duration:", out.getvalue())

    def test_budget_verb_accepts_an_explicit_base(self):
        from korapy import cli

        proc = subprocess.CompletedProcess([], 0, "4\t0\tsrc/a.rs\n", "")
        out = io.StringIO()
        with mock.patch.object(bu, "_git", return_value=proc), redirect_stdout(out):
            rc = cli.main(["budget", "--base", "HEAD~3"])
        self.assertEqual(rc, 0)
        self.assertIn("HEAD~3", out.getvalue())


class RegistrationTest(unittest.TestCase):
    def test_every_number_is_a_registered_int_setting(self):
        expected = {
            "review.budget.base_minutes": bu.BASE_MINUTES,
            "review.budget.seconds_per_line": bu.SECONDS_PER_LINE,
            "review.budget.limit_multiple": bu.LIMIT_MULTIPLE,
            "review.budget.floor_minutes": bu.GATE_MIN_LIMIT_MINUTES,
            "review.budget.ceiling_minutes": bu.MACHINE_CEILING_MINUTES,
        }
        for key, default in expected.items():
            with self.subTest(key=key):
                self.assertIn(key, S.REGISTRY)
                self.assertIs(S.REGISTRY[key].type, int)
                self.assertEqual(S.REGISTRY[key].default, default)

    def test_budget_registers_its_settings(self):
        self.assertIn("korapy.budget", S.PROVIDERS)

    def test_defaults_resolve_with_no_file(self):
        resolved = resolve_settings()
        self.assertEqual(resolved["review.budget.base_minutes"], bu.BASE_MINUTES)
        self.assertEqual(resolved["review.budget.seconds_per_line"], bu.SECONDS_PER_LINE)
        self.assertEqual(resolved["review.budget.limit_multiple"], bu.LIMIT_MULTIPLE)
        self.assertEqual(resolved["review.budget.floor_minutes"], bu.GATE_MIN_LIMIT_MINUTES)
        self.assertEqual(resolved["review.budget.ceiling_minutes"], bu.MACHINE_CEILING_MINUTES)


class LayersTest(unittest.TestCase):
    def test_a_project_file_overrides_each_number(self):
        resolved = resolve_settings(project=custom_project())
        for dotted, value in CUSTOM.items():
            self.assertEqual(resolved[f"review.budget.{dotted}"], value)

    def test_a_repo_entry_wins_for_that_repo_only(self):
        project = {"repo": [{"name": "a", "review": {"budget": {"base_minutes": 5}}}]}
        self.assertEqual(
            resolve_settings(project=project, repo="a")["review.budget.base_minutes"], 5
        )
        self.assertEqual(
            resolve_settings(project=project)["review.budget.base_minutes"], bu.BASE_MINUTES
        )

    def test_shared_and_repo_layers_combine(self):
        project = {
            "review": {"budget": {"base_minutes": 5}},
            "repo": [{"name": "a", "review": {"budget": {"seconds_per_line": 9}}}],
        }
        resolved = resolve_settings(project=project, repo="a")
        self.assertEqual(resolved["review.budget.base_minutes"], 5)
        self.assertEqual(resolved["review.budget.seconds_per_line"], 9)

    def test_env_overrides_a_file(self):
        resolved = resolve_settings(
            project=custom_project(),
            env={"KORAPY_REVIEW_BUDGET_BASE_MINUTES": "3"},
        )
        self.assertEqual(resolved["review.budget.base_minutes"], 3)

    def test_a_host_limit_clamps_a_project_value(self):
        resolved = resolve_settings(
            host={"limits": {"review.budget.base_minutes": 5}},
            project={"review": {"budget": {"base_minutes": 50}}},
        )
        value = resolved.values["review.budget.base_minutes"]
        self.assertEqual(value.value, 5)
        self.assertEqual(value.clamped_from, 50)

    def test_an_unknown_budget_key_names_the_file_and_key(self):
        with self.assertRaisesRegex(S.SettingsError, r"korapy\.toml.*'review\.budget\.bogus'"):
            resolve_settings(project={"review": {"budget": {"bogus": 1}}})

    def test_a_wrong_type_names_the_file_and_key(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"korapy\.toml.*review\.budget\.base_minutes must be int"
        ):
            resolve_settings(project={"review": {"budget": {"base_minutes": "fast"}}})

    def test_a_bool_is_not_an_int(self):
        with self.assertRaises(S.SettingsError):
            resolve_settings(project={"review": {"budget": {"base_minutes": True}}})


class RangeSourceTest(unittest.TestCase):
    """A bad budget number names the source that set it, not just the key.

    The range lives in the settings layer where the file (or variable) is
    known; numbers() keeps only the cross-key checks.
    """

    def test_a_non_positive_host_value_names_the_host_file(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"host-config\.toml.*review\.budget\.base_minutes"
        ):
            resolve_settings(
                host={"review": {"budget": {"base_minutes": 0}}},
                host_name="host-config.toml",
            )

    def test_a_non_positive_project_value_names_the_project_file(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"korapy\.toml.*review\.budget\.seconds_per_line"
        ):
            resolve_settings(project={"review": {"budget": {"seconds_per_line": -2}}})

    def test_a_non_positive_repo_value_names_the_entry(self):
        project = {"repo": [{"name": "a", "review": {"budget": {"limit_multiple": 0}}}]}
        with self.assertRaisesRegex(
            S.SettingsError, r"\[\[repo\]\] a.*review\.budget\.limit_multiple"
        ):
            resolve_settings(project=project, repo="a")

    def test_a_non_positive_env_value_names_the_variable(self):
        with self.assertRaisesRegex(
            S.SettingsError,
            r"KORAPY_REVIEW_BUDGET_FLOOR_MINUTES.*review\.budget\.floor_minutes",
        ):
            resolve_settings(env={"KORAPY_REVIEW_BUDGET_FLOOR_MINUTES": "0"})

    def test_a_non_positive_flag_value_names_the_key(self):
        with self.assertRaisesRegex(S.SettingsError, r"flag.*review\.budget\.ceiling_minutes"):
            resolve_settings(flags={"review.budget.ceiling_minutes": 0})


class HostCeilingTest(unittest.TestCase):
    """A host [limits] ceiling is a value too: it runs the range check.

    The check used to run only where a value was set, so a ceiling of 0
    for a checked setting was clamped in unchecked: seconds per line 0
    then dies with ZeroDivisionError in max_reviewable_lines, and a limit
    multiple of 0 divides by zero inside numbers(). A ceiling of 0 (or
    below) for a checked setting is a SettingsError naming the host
    limits file and key instead.
    """

    def ceiling_host(self, short, ceiling):
        return {"limits": {"review": {"budget": {short: ceiling}}}}

    def test_a_zero_host_ceiling_is_refused_for_each_checked_setting(self):
        for key in bu.NUMBERS_KEYS:
            short = key.removeprefix("review.budget.")
            pattern = r"host-config\.toml.*\[limits\].*" + key.replace(".", r"\.")
            with self.subTest(key=key):
                with self.assertRaisesRegex(S.SettingsError, pattern):
                    resolve_settings(
                        host=self.ceiling_host(short, 0),
                        host_name="host-config.toml",
                    )

    def test_a_negative_host_ceiling_is_refused_for_each_checked_setting(self):
        for key in bu.NUMBERS_KEYS:
            short = key.removeprefix("review.budget.")
            pattern = r"host-config\.toml.*\[limits\].*" + key.replace(".", r"\.")
            with self.subTest(key=key):
                with self.assertRaisesRegex(S.SettingsError, pattern):
                    resolve_settings(
                        host=self.ceiling_host(short, -1),
                        host_name="host-config.toml",
                    )


class NumbersTest(unittest.TestCase):
    def test_no_resolved_settings_reads_as_todays_constants(self):
        cfg = bu.numbers()
        self.assertEqual(cfg.base_minutes, bu.BASE_MINUTES)
        self.assertEqual(cfg.seconds_per_line, bu.SECONDS_PER_LINE)
        self.assertEqual(cfg.limit_multiple, bu.LIMIT_MULTIPLE)
        self.assertEqual(cfg.floor_minutes, bu.GATE_MIN_LIMIT_MINUTES)
        self.assertEqual(cfg.ceiling_minutes, bu.MACHINE_CEILING_MINUTES)

    def test_a_resolved_project_is_read_verbatim(self):
        cfg = bu.numbers(resolve_settings(project=custom_project()))
        self.assertEqual(
            cfg,
            bu.Numbers(
                base_minutes=10,
                seconds_per_line=10,
                limit_multiple=2,
                floor_minutes=20,
                ceiling_minutes=60,
            ),
        )

    def test_non_positive_numbers_are_refused(self):
        for key in CUSTOM:
            bad = dict(CUSTOM)
            for value in (0, -1):
                bad[key] = value
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(S.SettingsError, key):
                        bu.numbers(resolve_settings(project={"review": {"budget": bad}}))

    def test_a_floor_at_or_above_the_ceiling_is_refused(self):
        # The floor-versus-ceiling rule lives in the Numbers construction,
        # so the error names the fields, not the layers that set them.
        bad = dict(CUSTOM, floor_minutes=60)
        with self.assertRaisesRegex(
            S.SettingsError,
            r"review\.budget\.floor_minutes \(60\) must be below "
            r"review\.budget\.ceiling_minutes \(60\)",
        ):
            bu.numbers(resolve_settings(project={"review": {"budget": bad}}))

    def test_a_cross_key_error_reports_each_value_layer(self):
        # The floor comes from the environment here, the base and multiple
        # from the project file: the smallest-limit error names the layer
        # behind each value.
        resolved = resolve_settings(
            project=custom_project(),
            env={"KORAPY_REVIEW_BUDGET_FLOOR_MINUTES": "30"},
        )
        with self.assertRaisesRegex(
            S.SettingsError,
            r"smallest limit \(20 minutes, from base_minutes 10 set by project "
            r"and limit_multiple 2 set by project\) is below "
            r"review\.budget\.floor_minutes \(30, from env\)",
        ):
            bu.numbers(resolved)

    def test_a_clamped_value_reports_the_host_limits(self):
        # A host [limits] clamp rewrites the value after the project set
        # it, so the cross-key error must name the host limits rather
        # than the layer the value was set by.
        resolved = resolve_settings(
            host={"limits": {"review.budget.base_minutes": 5}},
            project={"review": {"budget": dict(CUSTOM, base_minutes=50)}},
        )
        with self.assertRaisesRegex(S.SettingsError, r"base_minutes 5 set by host limits"):
            bu.numbers(resolved)

    def test_a_missing_setting_is_a_settings_error(self):
        with self.assertRaisesRegex(S.SettingsError, "review.budget.base_minutes"):
            bu.numbers({})

    def test_a_complete_plain_dict_resolves_verbatim(self):
        # numbers() takes plain mappings too, not only Resolved: with no
        # .values layer record there is nothing to blame, so a complete
        # dict reads verbatim instead of raising TypeError.
        plain = dict(zip(bu.NUMBERS_KEYS, (10, 10, 2, 20, 60)))
        self.assertEqual(
            bu.numbers(plain),
            bu.Numbers(
                base_minutes=10,
                seconds_per_line=10,
                limit_multiple=2,
                floor_minutes=20,
                ceiling_minutes=60,
            ),
        )

    def test_a_plain_dict_floor_error_names_the_fields(self):
        # The floor-versus-ceiling rule lives in the Numbers construction,
        # which has no layer record: the error names the fields, never a
        # generic origin.
        plain = dict(zip(bu.NUMBERS_KEYS, (10, 10, 2, 60, 60)))
        with self.assertRaisesRegex(
            S.SettingsError,
            r"review\.budget\.floor_minutes \(60\) must be below "
            r"review\.budget\.ceiling_minutes \(60\)",
        ):
            bu.numbers(plain)

    def test_a_plain_mapping_with_each_field_at_zero_is_refused(self):
        # A plain mapping never passes through the settings layer, so the
        # construction is what refuses the zero divisor: no ZeroDivisionError
        # reaches the arithmetic below.
        for key in bu.NUMBERS_KEYS:
            plain = dict(zip(bu.NUMBERS_KEYS, (10, 10, 2, 20, 60)))
            plain[key] = 0
            with self.subTest(key=key):
                with self.assertRaisesRegex(S.SettingsError, key.replace(".", r"\.")):
                    bu.numbers(plain)

    def test_a_floor_no_limit_can_clear_is_refused(self):
        # base * multiple below the floor: even an empty diff prints a
        # limit under the gate minimum, so the file is wrong, not the diff.
        bad = {
            "base_minutes": 1,
            "seconds_per_line": 1,
            "limit_multiple": 1,
            "floor_minutes": 30,
            "ceiling_minutes": 120,
        }
        with self.assertRaisesRegex(
            S.SettingsError,
            r"smallest limit \(1 minutes, from base_minutes 1 set by project "
            r"and limit_multiple 1 set by project\) is below "
            r"review\.budget\.floor_minutes \(30, from project\)",
        ):
            bu.numbers(resolve_settings(project={"review": {"budget": bad}}))


class NumbersConstructionTest(unittest.TestCase):
    """Every Numbers field is validated once, at construction.

    The construction is the one range check: a zero or negative field, a
    non-integer, or a floor at or above the ceiling is a SettingsError
    (exit 4) naming the field, never a ZeroDivisionError further down.
    """

    def valid(self, **kw):
        args = {
            "base_minutes": 10,
            "seconds_per_line": 10,
            "limit_multiple": 2,
            "floor_minutes": 20,
            "ceiling_minutes": 60,
        }
        args.update(kw)
        return args

    def test_each_field_at_zero_is_refused(self):
        for field in (
            "base_minutes",
            "seconds_per_line",
            "limit_multiple",
            "floor_minutes",
            "ceiling_minutes",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(S.SettingsError, rf"review\.budget\.{field}"):
                    bu.Numbers(**self.valid(**{field: 0}))

    def test_each_field_negative_is_refused(self):
        for field in (
            "base_minutes",
            "seconds_per_line",
            "limit_multiple",
            "floor_minutes",
            "ceiling_minutes",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(S.SettingsError, rf"review\.budget\.{field}"):
                    bu.Numbers(**self.valid(**{field: -1}))

    def test_each_field_must_be_an_int(self):
        for field in (
            "base_minutes",
            "seconds_per_line",
            "limit_multiple",
            "floor_minutes",
            "ceiling_minutes",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(S.SettingsError, rf"review\.budget\.{field}"):
                    bu.Numbers(**self.valid(**{field: "10"}))
                with self.assertRaisesRegex(S.SettingsError, rf"review\.budget\.{field}"):
                    bu.Numbers(**self.valid(**{field: True}))

    def test_a_floor_at_or_above_the_ceiling_is_refused(self):
        for floor, ceiling in ((60, 60), (61, 60)):
            with self.subTest(floor=floor, ceiling=ceiling):
                with self.assertRaisesRegex(
                    S.SettingsError,
                    r"review\.budget\.floor_minutes \(%d\) must be below "
                    r"review\.budget\.ceiling_minutes \(%d\)" % (floor, ceiling),
                ):
                    bu.Numbers(**self.valid(floor_minutes=floor, ceiling_minutes=ceiling))

    def test_a_valid_construction_resolves_verbatim(self):
        self.assertEqual(
            bu.Numbers(**self.valid()),
            bu.Numbers(
                base_minutes=10,
                seconds_per_line=10,
                limit_multiple=2,
                floor_minutes=20,
                ceiling_minutes=60,
            ),
        )

    # Two budget()/max_reviewable_lines() zero-field tests were deleted
    # here: Numbers(**{field: 0}) raises in __post_init__ before either
    # function runs, so both duplicated test_each_field_at_zero_is_refused.


class OneResolverTest(unittest.TestCase):
    """Every entry point reads the same numbers through `numbers()`."""

    @classmethod
    def cfg(cls):
        return bu.Numbers(
            base_minutes=10,
            seconds_per_line=10,
            limit_multiple=2,
            floor_minutes=20,
            ceiling_minutes=60,
        )

    def test_natural_expectation_uses_the_configured_base_and_rate(self):
        cfg = self.cfg()
        self.assertEqual(bu.natural_expected_minutes(0, cfg), 10)
        # (10*60 + 10*60 + 59) // 60 = 20, not the default 16.
        self.assertEqual(bu.natural_expected_minutes(60, cfg), 20)

    def test_max_expected_and_reviewable_lines_derive_from_the_ceiling(self):
        cfg = self.cfg()
        # (60 - 1) // 2 = 29; (29*60 - 10*60) // 10 = 114.
        self.assertEqual(bu.budget(0, cfg), (10, 20))
        self.assertEqual(bu.max_reviewable_lines(cfg), 114)

    def test_splitting_flags_exactly_the_shortened_expectations(self):
        cfg = self.cfg()
        self.assertFalse(bu.needs_splitting(bu.max_reviewable_lines(cfg), cfg))
        self.assertTrue(bu.needs_splitting(bu.max_reviewable_lines(cfg) + 1, cfg))

    def test_the_printed_pair_clears_a_configured_floor(self):
        cfg = self.cfg()
        for lines in (0, 114, 10_000):
            expected, limit = bu.budget(lines, cfg)
            self.assertGreaterEqual(limit, cfg.floor_minutes)
            self.assertEqual(limit, expected * cfg.limit_multiple)
            self.assertLess(limit, cfg.ceiling_minutes)

    def test_describe_and_dispatch_use_the_same_numbers(self):
        cfg = self.cfg()
        proc = subprocess.CompletedProcess([], 0, "100\t0\tsrc/a.rs\n", "")
        with mock.patch.object(bu, "_git", return_value=proc):
            report = bu.describe("origin/master", cfg=cfg)
            text = bu.dispatch_block("origin/master", cfg=cfg)
        # (10*60 + 10*100 + 59) // 60 = 27; 27 * 2 = 54.
        self.assertIn("expected duration: 27 minutes", report)
        self.assertIn("hard limit: 54 minutes", report)
        self.assertIn("expected 27 minutes, hard limit 54 minutes", text)

    def test_a_resolved_project_reaches_every_caller(self):
        cfg = bu.numbers(resolve_settings(project=custom_project()))
        self.assertEqual(cfg, self.cfg())
        self.assertEqual(bu.budget(0, cfg), (10, 20))

    def test_defaults_are_byte_identical_to_the_constants(self):
        # The product default stays as is: with no settings, the numbers
        # are today's constants and the printed pair is unchanged.
        self.assertEqual(bu.budget(530), (39, 117))
        self.assertEqual(bu.max_reviewable_lines(), 240)


class BudgetFlagsTest(unittest.TestCase):
    def run_budget(self, argv, project_toml=None):
        from korapy import cli

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if project_toml is not None:
            with open(os.path.join(tmp.name, "korapy.toml"), "w", encoding="utf-8") as handle:
                handle.write(project_toml)
        config_home = os.path.join(tmp.name, "config")
        os.makedirs(config_home)
        proc = subprocess.CompletedProcess([], 0, "100\t0\tsrc/a.rs\n", "")
        out = io.StringIO()
        # A hermetic host config: the real ~/.config/korapy is never read.
        with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config_home}, clear=True):
            with mock.patch.object(bu, "_git", return_value=proc), redirect_stdout(out):
                rc = cli.main(["budget", "--root", tmp.name, *argv])
        return rc, out.getvalue()

    def test_shared_settings_apply_without_a_repo(self):
        rc, out = self.run_budget(
            [],
            "[review.budget]\nbase_minutes = 10\nseconds_per_line = 10\n"
            "limit_multiple = 2\nfloor_minutes = 20\nceiling_minutes = 60\n",
        )
        self.assertEqual(rc, 0)
        self.assertIn("expected 27 minutes, hard limit 54 minutes", out)

    def test_repo_flag_selects_the_entry(self):
        rc, out = self.run_budget(
            ["--repo", "a"],
            '[[repo]]\nname = "a"\n[repo.review.budget]\nbase_minutes = 10\n'
            "seconds_per_line = 10\nlimit_multiple = 2\nfloor_minutes = 20\n"
            "ceiling_minutes = 60\n",
        )
        self.assertEqual(rc, 0)
        self.assertIn("expected 27 minutes, hard limit 54 minutes", out)

    def test_a_bad_value_is_exit_4(self):
        from io import StringIO
        import contextlib

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write("[review.budget]\nbase_minutes = 0\n")
        config_home = os.path.join(tmp.name, "config")
        os.makedirs(config_home)
        proc = subprocess.CompletedProcess([], 128, "", "")
        err, out = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config_home}, clear=True):
            with mock.patch.object(bu, "_git", return_value=proc):
                with contextlib.redirect_stderr(err), redirect_stdout(out):
                    from korapy import cli

                    rc = cli.main(["budget", "--root", tmp.name])
        self.assertEqual(rc, 4)
        self.assertIn("review.budget.base_minutes", err.getvalue())


class StrictFlagsTest(unittest.TestCase):
    """Both spellings share one parser and one root/repo check.

    Unknown or valueless flags exit 4, and a --root without a korapy.toml
    exits 4, whether the budget runs as `korapy budget` or as
    `python -m korapy.budget`. This replaces the lax main_cli test, which
    pinned the old hand-rolled parsing that silently ignored all of these.
    """

    def run_both(self, argv, project_toml=None, with_root=True):
        """[(verb rc, out, err), (module rc, out, err)] for the same flags.

        A temp dir is the --root when with_root holds; it carries
        project_toml, or no korapy.toml when project_toml is None. The
        working directory is always the temp dir, so a missing --root never
        accidentally finds a project file uphill.
        """
        from korapy import cli

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if project_toml is not None:
            with open(os.path.join(tmp.name, "korapy.toml"), "w", encoding="utf-8") as handle:
                handle.write(project_toml)
        flags = ["--root", tmp.name, *argv] if with_root else list(argv)
        config_home = os.path.join(tmp.name, "config")
        os.makedirs(config_home)
        proc = subprocess.CompletedProcess([], 0, "100\t0\tsrc/a.rs\n", "")
        results = []
        with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config_home}, clear=True):
            with mock.patch.object(bu, "_git", return_value=proc):
                with mock.patch.object(os, "getcwd", return_value=tmp.name):
                    calls = (lambda: cli.main(["budget", *flags]), lambda: bu.main_cli(flags))
                    for call in calls:
                        out, err = io.StringIO(), io.StringIO()
                        with redirect_stdout(out), redirect_stderr(err):
                            results.append((call(), out.getvalue(), err.getvalue()))
        return results

    def test_unknown_flag_exits_4_in_both_spellings(self):
        for rc, out, err in self.run_both(["--bogus"], project_toml="", with_root=False):
            with self.subTest(rc=rc):
                self.assertEqual(rc, 4)

    def test_valueless_trailing_flag_exits_4_in_both_spellings(self):
        for rc, out, err in self.run_both(["--root"], project_toml="", with_root=False):
            with self.subTest(rc=rc):
                self.assertEqual(rc, 4)

    def test_root_without_a_project_file_exits_4_in_both_spellings(self):
        for rc, out, err in self.run_both([], project_toml=None):
            with self.subTest(rc=rc, err=err):
                self.assertEqual(rc, 4)
                self.assertIn("no korapy.toml in --root", err)

    def test_repo_without_a_root_exits_4_in_both_spellings(self):
        results = self.run_both(["--repo", "a"], project_toml=None, with_root=False)
        for rc, out, err in results:
            with self.subTest(rc=rc, err=err):
                self.assertEqual(rc, 4)
                self.assertIn("--repo needs an ops root", err)

    def test_root_repo_and_base_agree_in_both_spellings(self):
        results = self.run_both(["--base", "HEAD~3"], project_toml="")
        for rc, out, err in results:
            with self.subTest(rc=rc, out=out):
                self.assertEqual(rc, 0)
                self.assertIn("HEAD~3", out)

    def test_repo_flag_selects_the_entry_in_both_spellings(self):
        toml = (
            '[[repo]]\nname = "a"\n[repo.review.budget]\nbase_minutes = 10\n'
            "seconds_per_line = 10\nlimit_multiple = 2\nfloor_minutes = 20\n"
            "ceiling_minutes = 60\n"
        )
        results = self.run_both(["--repo", "a"], project_toml=toml)
        for rc, out, err in results:
            with self.subTest(rc=rc, out=out):
                self.assertEqual(rc, 0)
                self.assertIn("expected 27 minutes, hard limit 54 minutes", out)

    def test_a_bad_value_is_exit_4_in_both_spellings(self):
        results = self.run_both([], project_toml="[review.budget]\nbase_minutes = 0\n")
        for rc, out, err in results:
            with self.subTest(rc=rc, err=err):
                self.assertEqual(rc, 4)
                self.assertIn("review.budget.base_minutes", err)

    def test_the_standalone_docstring_names_all_three_flags(self):
        doc = bu.main_cli.__doc__
        assert doc is not None
        for flag in ("--base", "--root", "--repo"):
            with self.subTest(flag=flag):
                self.assertIn(flag, doc)


if __name__ == "__main__":
    unittest.main()
