"""The check-matrix stamp: a green `make check` proves the tree once.

The stamp is one line under the cache dir, `<tree> <pythons...> <epoch>`,
written by the matrix after every leg passes. `pre-pr-check --test`
reuses it for the identical tree and interpreter set instead of running
the suite again. `stamp_status` is the pure core: the file text, the
tree hash and the clock all arrive as arguments, so no test runs the
suite or touches git.
"""

import os
import unittest
from pathlib import Path

from korapy import checkstamp

TREE = "9" * 40
OTHER_TREE = "8" * 40
PYTHONS = ["3.11", "3.14"]
NOW = 1_800_000_000


def status(text, *, tree=TREE, pythons=PYTHONS, now=NOW):
    return checkstamp.stamp_status(text, tree=tree, pythons=pythons, now=now)


def fresh_text(tree=TREE, pythons=PYTHONS, now=NOW):
    return checkstamp.format_stamp(tree, pythons, now)


class Status(unittest.TestCase):
    def test_a_fresh_stamp_for_the_same_tree_reuses_the_proof(self):
        self.assertEqual(status(fresh_text()), "fresh")

    def test_a_missing_stamp_runs_the_suite(self):
        self.assertEqual(status(""), "missing")
        self.assertEqual(status(None), "missing")
        self.assertEqual(status("   \n"), "missing")

    def test_a_stale_stamp_runs_the_suite(self):
        old = NOW - checkstamp.STAMP_MAX_AGE_S - 1
        self.assertEqual(status(fresh_text(now=old)), "stale")

    def test_a_stamp_at_the_age_limit_still_counts(self):
        edge = NOW - checkstamp.STAMP_MAX_AGE_S
        self.assertEqual(status(fresh_text(now=edge)), "fresh")

    def test_a_stamp_from_the_future_runs_the_suite(self):
        self.assertEqual(status(fresh_text(now=NOW + 60)), "stale")

    def test_a_different_tree_runs_the_suite(self):
        self.assertEqual(status(fresh_text(tree=OTHER_TREE)), "different-tree")

    def test_an_unresolvable_tree_never_reuses(self):
        self.assertEqual(status(fresh_text(), tree=""), "different-tree")

    def test_different_interpreters_run_the_suite(self):
        self.assertEqual(status(fresh_text(pythons=["3.11"])), "different-pythons")
        self.assertEqual(status(fresh_text(), pythons=["3.11"]), "different-pythons")

    def test_interpreter_order_does_not_matter(self):
        self.assertEqual(status(fresh_text(pythons=["3.14", "3.11"])), "fresh")

    def test_no_expected_interpreters_never_reuses(self):
        self.assertEqual(status(fresh_text(), pythons=None), "different-pythons")

    def test_a_corrupt_stamp_runs_the_suite(self):
        for text in ("only-a-tree", f"{TREE} 3.11 not-an-epoch\n"):
            with self.subTest(text=text):
                self.assertEqual(status(text), "corrupt")


class TwoStamps(unittest.TestCase):
    """The pre-push stamp and the matrix stamp stay deliberately separate.

    Both prove "the suite passed for this tree", but for different gates:
    the pre-push hook stamps `<tree> <hook-digest>` under the git dir with
    no expiry (its suite runs on one interpreter at push time), while the
    matrix stamps `<tree> <pythons...> <epoch>` under the cache dir with a
    24h expiry (its suite runs once per interpreter at check time). These
    pins stop a later merge from silently unifying or confusing them: a
    pre-push-shaped line must never read as matrix proof.
    """

    def test_the_two_stamps_live_in_different_places(self):
        from korapy import push

        self.assertNotEqual(push.STAMP_NAME, checkstamp.STAMP_NAME)
        self.assertEqual(
            os.path.dirname(checkstamp.stamp_path({"KORAPY_CACHE": "/tmp/c"})),
            "/tmp/c",
        )

    def test_a_pre_push_shaped_line_never_proves_the_matrix(self):
        digest = "d" * 40
        self.assertEqual(status(f"{TREE} {digest}\n"), "corrupt")

    def test_only_the_matrix_stamp_expires(self):
        from korapy import push

        self.assertGreater(checkstamp.STAMP_MAX_AGE_S, 0)
        self.assertFalse(hasattr(push, "STAMP_MAX_AGE_S"))
        old = NOW - checkstamp.STAMP_MAX_AGE_S - 1
        self.assertEqual(status(fresh_text(now=old)), "stale")


def matrix_section():
    lines = (
        (Path(__file__).resolve().parent.parent / "Makefile")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    start = next(i for i, line in enumerate(lines) if line.startswith("test-matrix:"))
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i] and not lines[i].startswith((" ", "\t"))
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


class MatrixIsolation(unittest.TestCase):
    """Two concurrent `make check` runs must not share leg status."""

    def test_each_run_gets_a_private_log_dir(self):
        self.assertIn("mktemp -d", matrix_section())

    def test_no_shared_status_file_is_cleared_or_reused(self):
        self.assertNotIn("rm -f", matrix_section())

    def test_the_stamped_tree_is_taken_before_the_legs_start(self):
        section = matrix_section()
        first_tree = section.index("rev-parse")
        self.assertLess(first_tree, section.index("wait"))
        self.assertIn("tree_before", section)

    def test_a_tree_changed_mid_run_gets_no_stamp(self):
        section = matrix_section()
        self.assertIn("tree_after", section)
        self.assertLess(section.index("tree_after"), section.index(checkstamp.STAMP_NAME))


class TreeHashAgreement(unittest.TestCase):
    """Every `HEAD^{tree}` resolver must spell the same invocation."""

    def test_all_four_resolvers_name_the_same_tree(self):
        root = Path(__file__).resolve().parent.parent
        holders = [
            root / "Makefile",
            root / "scripts" / "pre_pr_check.py",
            root / "src" / "korapy" / "push.py",
            root / ".githooks" / "pre-push",
        ]
        for holder in holders:
            with self.subTest(holder=holder.name):
                text = holder.read_text(encoding="utf-8")
                self.assertIn("rev-parse", text)
                self.assertIn("HEAD^{tree}", text)


class SuiteCommandAgreement(unittest.TestCase):
    """Takeoff runs the same suite the Makefile defines, in one spelling."""

    def test_takeoff_names_the_makefile_unittest(self):
        import importlib.util

        script = Path(__file__).resolve().parent.parent / "scripts" / "pre_pr_check.py"
        spec = importlib.util.spec_from_file_location("pre_pr_check_unittest", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text(encoding="utf-8")
        definition = next(line for line in makefile.splitlines() if line.startswith("UNITTEST = "))
        self.assertEqual(definition.removeprefix("UNITTEST = ").split(), list(module.SUITE_ARGV))


class MatrixPythonsAgreement(unittest.TestCase):
    """The hook's proof check covers the interpreters `make check` stamps.

    The Makefile owns `PYTHONS`; `checkstamp.MATRIX_PYTHONS` repeats it
    for the pre-push hook, which cannot query make at push time. The two
    must agree, or a matrix proof would read as foreign interpreters.
    """

    def test_the_matrix_pythons_match_the_makefile(self):
        makefile = Path(__file__).resolve().parent.parent / "Makefile"
        definition = next(
            line
            for line in makefile.read_text(encoding="utf-8").splitlines()
            if line.startswith("PYTHONS ")
        )
        self.assertEqual(
            definition.removeprefix("PYTHONS ?= ").split(), list(checkstamp.MATRIX_PYTHONS)
        )


class StampPath(unittest.TestCase):
    def test_the_stamp_lives_under_the_cache_dir(self):
        env = {"KORAPY_CACHE": "/tmp/cache-for-a-stamp-test"}
        self.assertEqual(
            checkstamp.stamp_path(env),
            os.path.join("/tmp/cache-for-a-stamp-test", checkstamp.STAMP_NAME),
        )

    def test_the_makefile_writes_the_same_stamp_name(self):
        makefile = Path(__file__).resolve().parent.parent / "Makefile"
        self.assertIn(checkstamp.STAMP_NAME, makefile.read_text(encoding="utf-8"))

    def test_the_makefile_passes_its_pythons_to_pre_pr_check(self):
        makefile = Path(__file__).resolve().parent.parent / "Makefile"
        self.assertIn('--pythons "$(PYTHONS)"', makefile.read_text(encoding="utf-8"))

    def test_the_makefile_only_stamps_a_clean_tree(self):
        makefile = Path(__file__).resolve().parent.parent / "Makefile"
        self.assertIn("git status --porcelain", makefile.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
