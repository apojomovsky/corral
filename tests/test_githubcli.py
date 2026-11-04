"""`corral github setup`: bootstrap the board labels. Fakes only."""

import argparse
import io
import os
import tempfile
import unittest
from unittest import mock

from corral import cli, githubcli, settings
from corral import tracker as iface
from tests.test_init import FakeGit
from tests.test_tracker import FakeTracker

TOML = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"
"""

TWO = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"

[[repo]]
name = "lib"
tracker.repo = "acme/lib"
"""

NAMES = [label.name for label in iface.BOOTSTRAP_LABELS]


class SetupRootCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-githubcli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_setup(self, factory, names=(), git=None):
        return githubcli.setup_root(
            self.root,
            list(names),
            tracker_factory=factory,
            git=git if git is not None else FakeGit(root=self.root),
            out=self.out,
            err=self.err,
        )


class ExitCodeTest(SetupRootCase):
    def test_a_fresh_repo_creates_everything_and_exits_0(self):
        fake = FakeTracker()
        code = self.run_setup({"acme/app": fake}.__getitem__)
        self.assertEqual(code, 0)
        self.assertIn("created label ready on acme/app\n", self.out.getvalue())
        self.assertIn("created label priority:backlog on acme/app\n", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_a_rerun_keeps_everything_and_exits_1(self):
        fake = FakeTracker()
        self.assertEqual(self.run_setup({"acme/app": fake}.__getitem__), 0)
        self.out = io.StringIO()
        code = self.run_setup({"acme/app": fake}.__getitem__)
        self.assertEqual(code, 1)
        self.assertIn(f"kept {len(NAMES)} labels on acme/app\n", self.out.getvalue())
        self.assertNotIn("created label", self.out.getvalue())

    def test_refused_labels_are_exit_1_with_a_fix_line(self):
        fake = FakeTracker(refused=set(NAMES))
        code = self.run_setup({"acme/app": fake}.__getitem__)
        self.assertEqual(code, 1)
        out = self.out.getvalue()
        self.assertNotIn("created label", out)
        self.assertIn(
            "refused label hold on acme/app: ask an owner to create it,"
            " or move the board to the ops side\n",
            out,
        )

    def test_created_plus_refused_is_still_1(self):
        fake = FakeTracker(refused={"hold"})
        code = self.run_setup({"acme/app": fake}.__getitem__)
        self.assertEqual(code, 1)
        out = self.out.getvalue()
        self.assertIn("created label ready on acme/app\n", out)
        self.assertIn("refused label hold on acme/app", out)

    def test_an_unreachable_tracker_is_exit_3(self):
        fake = FakeTracker(ensure_error=iface.TrackerError("connection refused"))
        code = self.run_setup({"acme/app": fake}.__getitem__)
        self.assertEqual(code, 3)
        self.assertIn("acme/app", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_a_rejected_label_create_is_exit_1_with_the_reason(self):
        fake = FakeTracker(
            ensure_error=iface.TrackerRejected("tracker rejected: Validation Failed (HTTP 422)")
        )
        code = self.run_setup({"acme/app": fake}.__getitem__)
        self.assertEqual(code, 1)
        self.assertIn("tracker rejected", self.err.getvalue())
        self.assertIn("acme/app", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_partial_failure_with_a_create_is_3(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        good = FakeTracker()
        bad = FakeTracker(ensure_error=iface.TrackerError("down"))
        code = self.run_setup({"acme/app": good, "acme/lib": bad}.__getitem__)
        self.assertEqual(code, 3)
        self.assertIn("created label ready on acme/app\n", self.out.getvalue())
        self.assertIn("acme/lib", self.err.getvalue())

    def test_failure_with_a_create_and_a_refusal_is_3(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        mixed = FakeTracker(refused={"hold"})
        bad = FakeTracker(ensure_error=iface.TrackerError("down"))
        code = self.run_setup({"acme/app": mixed, "acme/lib": bad}.__getitem__)
        self.assertEqual(code, 3)
        self.assertIn("refused label hold on acme/app", self.out.getvalue())
        self.assertIn("acme/lib", self.err.getvalue())

    def test_total_failure_is_exit_3(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        bad = FakeTracker(ensure_error=iface.TrackerError("down"))
        factory = {"acme/app": bad, "acme/lib": bad}.__getitem__
        self.assertEqual(self.run_setup(factory), 3)

    def test_only_a_subset_runs_when_named(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        app = FakeTracker()
        lib = FakeTracker()
        code = self.run_setup({"acme/app": app, "acme/lib": lib}.__getitem__, names=["lib"])
        self.assertEqual(code, 0)
        self.assertEqual(app.calls, [])
        self.assertIn(("ensure_labels", "acme/lib"), lib.calls)


class ConfigErrorTest(SetupRootCase):
    def test_an_unknown_repo_is_exit_4(self):
        fake = FakeTracker()
        code = self.run_setup({"acme/app": fake}.__getitem__, names=["nope"])
        self.assertEqual(code, 4)
        self.assertIn("nope", self.err.getvalue())
        self.assertEqual(fake.calls, [])

    def test_no_entries_is_exit_4(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[project]\nname = "demo"\n')
        fake = FakeTracker()
        code = self.run_setup({"acme/app": fake}.__getitem__)
        self.assertEqual(code, 4)
        self.assertEqual(fake.calls, [])

    def test_an_unresolvable_repo_is_exit_4(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\nclone.path = "clone"\n')
        git = FakeGit(root=self.root, origin_url=None)
        fake = FakeTracker()
        code = self.run_setup({"acme/app": fake}.__getitem__, git=git)
        self.assertEqual(code, 4)
        self.assertIn("origin", self.err.getvalue())
        self.assertEqual(fake.calls, [])

    def test_a_missing_tracker_is_exit_4(self):
        code = self.run_setup({}.__getitem__)
        self.assertEqual(code, 4)
        self.assertIn("no tracker", self.err.getvalue())


class OriginTest(SetupRootCase):
    def test_the_clone_origin_names_the_tracker_repo(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\nclone.path = "clone"\n')
        git = FakeGit(root=self.root, origin_url="git@github.com:acme/app.git\n")
        fake = FakeTracker()
        code = self.run_setup({"acme/app": fake}.__getitem__, git=git)
        self.assertEqual(code, 0)
        self.assertIn("created label ready on acme/app\n", self.out.getvalue())


class ParserTest(unittest.TestCase):
    def test_github_setup_is_wired_with_expected_flags(self):
        args = cli.build_parser().parse_args(["github", "setup"])
        self.assertEqual(args.func.__name__, "cmd_setup")
        self.assertEqual(args.repo, [])

    def test_github_needs_a_verb(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["github"])

    def test_cmd_setup_without_root_exits_4(self):
        empty = tempfile.mkdtemp(prefix="corral-github-noroot-")
        self.addCleanup(lambda: os.rmdir(empty))
        namespace = argparse.Namespace(repo=[], func=githubcli.cmd_setup)
        with mock.patch("os.getcwd", return_value=empty):
            self.assertEqual(githubcli.cmd_setup(namespace), 4)


if __name__ == "__main__":
    unittest.main()
