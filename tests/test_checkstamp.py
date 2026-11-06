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
