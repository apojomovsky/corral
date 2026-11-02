"""`corral new` (ticket 1): scaffold templates, writes, exits, idempotency.

Local only, no `init` wiring yet. Every git call goes through the
`FakeGit` harness from `tests.test_init` (reused, not redefined); every
file lands in a temporary directory. The CLI wiring test runs the real
entry point, which runs real local git inside the temp tree, the same
way `tests/test_init.py` already does for `init`.
"""

import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, forge as iface, init, new, settings
from corral import tracker as tracker_iface
from tests.test_forge import FakeForge
from tests.test_init import FakeGit
from tests.test_tracker import FakeTracker

FIXTURE = "demo"

GOLDEN_BLANK = {
    ".github/workflows/check.yml": """\
name: check
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make check
""",
    ".gitignore": """\
.worktrees/
*~
*.swp
.DS_Store
""",
    "AGENTS.md": """\
# AGENTS.md

## Build

There is nothing to build yet.

## Test

Run `make test`. `make check` runs the same suite plus the remaining gates.

## Contribute

Small diffs, passing checks, and a Conventional Commit message.
""",
    "Makefile": (
        """\
.PHONY: test check setup-hooks

test:
\t@echo ok: replace this target with the project's real suite

check: test

setup-hooks:
\tgit config core.hooksPath .githooks
"""
    ),
    "README.md": """\
# demo

One line: what this project is and who it is for.

## Build

There is nothing to build yet. Say how here when there is.

## Test

make test

## Contribute

Keep changes small, run `make check`, and describe the why in the commit message.
""",
}

GOLDEN_PYUV = dict(
    GOLDEN_BLANK,
    **{
        ".gitignore": """\
.worktrees/
*~
*.swp
.DS_Store
__pycache__/
*.pyc
.venv/
dist/
""",
        "Makefile": (
            """\
.PHONY: test check setup-hooks

test:
\tuv run python -m unittest

check: test

setup-hooks:
\tgit config core.hooksPath .githooks
"""
        ),
        "pyproject.toml": """\
[project]
name = "demo"
version = "0.1.0"
description = "One line: what this project is and who it is for."
requires-python = ">=3.11"
dependencies = []
""",
        "tests/test_placeholder.py": """\
\"\"\"Placeholder test: replace it with the project's real suite.\"\"\"

import unittest


class Placeholder(unittest.TestCase):
    def test_placeholder(self):
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
""",
    },
)


def run_new(
    *,
    name=FIXTURE,
    path=None,
    template="blank",
    plan=False,
    force=False,
    git=None,
    forge=None,
    default_branch=None,
    sets=(),
    test_cmd=None,
    runtime=None,
    cap=None,
    ops=None,
    ops_name=None,
    create_remote=False,
    public=False,
    owner=None,
    resume=False,
    tracker=None,
):
    """`new.run` with captured output: `(code, stdout, stderr)`."""
    opts = new.Options(
        name=name,
        path=path,
        template=template,
        plan=plan,
        force=force,
        default_branch=default_branch,
        sets=sets,
        test_cmd=test_cmd,
        runtime=runtime,
        cap=cap,
        ops=ops,
        ops_name=ops_name,
        create_remote=create_remote,
        public=public,
        owner=owner,
        resume=resume,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if git is None and forge is None and tracker is None:
            code = new.run(opts)
        else:
            if tracker is None and forge is not None:
                tracker = FakeTracker()
            code = new.run(opts, git=git or FakeGit(root=path), forge=forge, tracker=tracker)
    return code, out.getvalue(), err.getvalue()


def isolated_config(test):
    """A throwaway `CORRAL_CONFIG` dir for the test (init registers the host)."""
    cfg = tempfile.mkdtemp(prefix="corral-config-")
    test.addCleanup(shutil.rmtree, cfg, True)
    patcher = mock.patch.dict(os.environ, {"CORRAL_CONFIG": cfg})
    patcher.start()
    test.addCleanup(patcher.stop)
    return cfg


class TemplateNames(unittest.TestCase):
    def test_names_lists_the_packaged_templates(self):
        self.assertEqual(new.names(), ["blank", "python-uv"])


class GoldenRenders(unittest.TestCase):
    def check_golden(self, template, golden):
        rendered = new.render(template, FIXTURE)
        self.assertEqual(sorted(rendered), sorted(golden))
        for rel, text in golden.items():
            self.assertEqual(rendered[rel], text.encode("utf-8"), rel)

    def test_blank_render_is_byte_identical(self):
        self.check_golden("blank", GOLDEN_BLANK)

    def test_python_uv_render_is_byte_identical(self):
        self.check_golden("python-uv", GOLDEN_PYUV)


class TemplateReads(unittest.TestCase):
    def test_is_dir_failure_in_names_is_exit_4(self):
        bad = mock.Mock()
        bad.name = "blank"
        bad.is_dir.side_effect = OSError("boom")
        root = mock.Mock()
        root.iterdir.return_value = [bad]
        with mock.patch.object(new, "_template_root", return_value=root):
            with self.assertRaises(new.NewError):
                new.names()

    def test_is_dir_failure_in_files_is_exit_4(self):
        child = mock.Mock()
        child.name = "README.md"
        child.is_dir.side_effect = OSError("boom")
        node = mock.Mock()
        node.iterdir.return_value = [child]
        troot = mock.Mock()
        troot.joinpath.return_value = node
        with mock.patch.object(new, "names", return_value=["blank"]):
            with mock.patch.object(new, "_template_root", return_value=troot):
                with self.assertRaises(new.NewError):
                    new.files("blank")


class StrictPlaceholder(unittest.TestCase):
    def test_name_is_substituted_everywhere(self):
        self.assertEqual(new.substitute("f.md", "{{name}} and {{name}}", "proj"), "proj and proj")

    def test_other_placeholder_is_exit_4_naming_file_and_placeholder(self):
        with self.assertRaises(new.NewError) as ctx:
            new.substitute("f.md", "a {{oops}} b", "proj")
        self.assertIn("f.md", str(ctx.exception))
        self.assertIn("{{oops}}", str(ctx.exception))

    def test_unclosed_braces_are_not_placeholders(self):
        self.assertEqual(new.substitute("f.md", "a {{ b", "proj"), "a {{ b")


class ScaffoldRun(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self, name=FIXTURE):
        return os.path.join(self.parent, name)

    def snapshot(self, root):
        """`relpath -> bytes` of every file under `root`."""
        found = {}
        for here, _, names in os.walk(root):
            for entry in names:
                if ".git" in here.split(os.sep):
                    continue
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    found[os.path.relpath(full, root)] = handle.read()
        return found

    def test_fresh_run_writes_the_render_and_inits_the_branch(self):
        fake = FakeGit(root=self.parent)
        code, out, err = run_new(path=self.parent, git=fake)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(fake.calls[0], (["rev-parse", "--show-toplevel"], self.target()))
        self.assertIn((["init", "-b", init.resolve_branch(None)], self.target()), fake.calls)
        rendered = new.render("blank", FIXTURE)
        for rel, blob in rendered.items():
            self.assertIn(rel, self.snapshot(self.target()))
            self.assertEqual(self.snapshot(self.target())[rel], blob, rel)
        self.assertIn("README.md", out)

    def test_rerun_is_nothing_to_do_and_changes_no_byte(self):
        fake = FakeGit(root=self.parent)
        code, _, _ = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 0)
        before = self.snapshot(self.target())
        rerun = FakeGit(root=self.target(), hooks=".githooks")
        code, out, _ = run_new(path=self.parent, git=rerun)
        self.assertEqual(code, 1)
        self.assertEqual(self.snapshot(self.target()), before)
        self.assertIn("is up to date", out)

    def test_plan_writes_nothing(self):
        before = self.snapshot(self.parent)
        fake = FakeGit(root=self.parent)
        code, out, _ = run_new(path=self.parent, plan=True, git=fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(self.parent), before)
        self.assertFalse(os.path.exists(self.target()))
        self.assertIn("would run git init", out)
        self.assertIn("a/README.md", out)
        self.assertIn("# demo", out)

    def test_plan_on_a_converged_tree_is_nothing_to_do(self):
        fake = FakeGit(root=self.parent)
        self.assertEqual(run_new(path=self.parent, git=fake)[0], 0)
        code, _, _ = run_new(
            path=self.parent, plan=True, git=FakeGit(root=self.target(), hooks=".githooks")
        )
        self.assertEqual(code, 1)

    def test_bad_names_are_exit_4_and_write_nothing(self):
        for bad in ("a/b", "x.git", ""):
            fake = FakeGit(root=self.parent)
            code, _, err = run_new(name=bad, path=self.parent, git=fake)
            self.assertEqual(code, 4, bad)
            self.assertEqual(fake.calls, [], bad)
        self.assertEqual(self.snapshot(self.parent), {})

    def test_unknown_template_is_exit_4_listing_the_known_names(self):
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, template="nope", git=fake)
        self.assertEqual(code, 4)
        self.assertIn("blank", err)
        self.assertIn("python-uv", err)
        self.assertEqual(fake.calls, [])
        self.assertFalse(os.path.exists(self.target()))

    def test_foreign_tree_is_exit_4_naming_the_file_and_untouched(self):
        target = self.target()
        os.makedirs(target)
        with open(os.path.join(target, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("someone else was here\n")
        before = self.snapshot(target)
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 4)
        self.assertIn("README.md", err)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.snapshot(target), before)

    def test_partial_tree_converges(self):
        target = self.target()
        os.makedirs(target)
        rendered = new.render("blank", FIXTURE)
        first, rest = sorted(rendered)[:2], sorted(rendered)[2:]
        self.assertTrue(rest)
        for rel in first:
            full = os.path.join(target, rel)
            os.makedirs(os.path.dirname(full) or target, exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(rendered[rel])
        fake = FakeGit(root=self.parent)
        code, _, _ = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 0)
        got = self.snapshot(target)
        for rel, blob in rendered.items():
            self.assertEqual(got[rel], blob, rel)
        self.assertIn("corral.toml", got)

    def test_force_leaves_a_differing_scaffold_file_alone(self):
        target = self.target()
        os.makedirs(target)
        edited = b"my own readme\n"
        with open(os.path.join(target, "README.md"), "wb") as handle:
            handle.write(edited)
        fake = FakeGit(root=self.parent)
        code, out, _ = run_new(path=self.parent, force=True, git=fake)
        self.assertEqual(code, 0)
        rendered = new.render("blank", FIXTURE)
        got = self.snapshot(target)
        for rel, blob in rendered.items():
            if rel == "README.md":
                continue
            self.assertEqual(got[rel], blob, rel)
        self.assertEqual(got["README.md"], edited)
        self.assertIn("corral.toml", got)
        self.assertIn("README.md", out)

    def test_force_rerun_with_only_a_differ_is_nothing_to_do(self):
        first = FakeGit(root=self.parent)
        self.assertEqual(run_new(path=self.parent, git=first)[0], 0)
        target = self.target()
        with open(os.path.join(target, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("my own readme\n")
        rerun = FakeGit(root=target, hooks=".githooks")
        code, out, _ = run_new(path=self.parent, force=True, git=rerun)
        self.assertEqual(code, 1)
        self.assertIn("README.md", out)


class BranchFlags(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self, name=FIXTURE):
        return os.path.join(self.parent, name)

    def init_calls(self, fake):
        return [args for args, _ in fake.calls if args[:1] == ["init"]]

    def test_default_branch_flag_sets_the_init_branch(self):
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake, default_branch="trunk")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(["init", "-b", "trunk"], self.init_calls(fake))

    def test_set_branch_sets_the_init_branch(self):
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake, sets=("clone.default_branch=trunk",))
        self.assertEqual((code, err), (0, ""))
        self.assertIn(["init", "-b", "trunk"], self.init_calls(fake))

    def test_equal_flag_and_set_agree(self):
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(
            path=self.parent,
            git=fake,
            default_branch="same",
            sets=("clone.default_branch=same",),
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn(["init", "-b", "same"], self.init_calls(fake))

    def test_conflicting_flag_and_set_is_exit_4_before_any_write(self):
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(
            path=self.parent,
            git=fake,
            default_branch="flag",
            sets=("clone.default_branch=other",),
        )
        self.assertEqual(code, 4)
        self.assertIn("--default-branch", err)
        self.assertIn("clone.default_branch", err)
        self.assertEqual(fake.calls, [])
        self.assertFalse(os.path.exists(self.target()))


class ScaffoldFailures(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name

    def target(self, name=FIXTURE):
        return os.path.join(self.parent, name)

    def snapshot(self, root):
        found = {}
        for here, _, names in os.walk(root):
            for entry in names:
                if ".git" in here.split(os.sep):
                    continue
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    found[os.path.relpath(full, root)] = handle.read()
        return found

    def test_git_init_failure_is_exit_4_and_writes_nothing(self):
        fake = FakeGit(root=self.parent, fail={"init"})
        code, _, err = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 4)
        self.assertIn("git init", err)
        self.assertEqual(
            [rel for rel in os.listdir(self.target()) if rel != ".git"]
            if os.path.exists(self.target())
            else [],
            [],
        )

    def test_target_existing_as_a_file_is_exit_4(self):
        with open(self.target(), "w", encoding="utf-8") as handle:
            handle.write("in the way\n")
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 4)
        self.assertIn("not a directory", err)
        self.assertEqual(fake.calls, [])

    def test_plan_on_a_file_target_matches_the_real_run(self):
        with open(self.target(), "w", encoding="utf-8") as handle:
            handle.write("in the way\n")
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, plan=True, git=fake)
        self.assertEqual(code, 4)
        self.assertIn("not a directory", err)
        self.assertEqual(fake.calls, [])

    def test_directory_at_a_scaffold_path_is_exit_4_and_untouched(self):
        target = self.target()
        os.makedirs(os.path.join(target, "README.md"))
        before = self.snapshot(target)
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 4)
        self.assertIn("README.md", err)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.snapshot(target), before)

    def test_dangling_symlink_at_a_scaffold_path_is_exit_4(self):
        target = self.target()
        os.makedirs(target)
        os.symlink(
            os.path.join(target, "no-such-file"),
            os.path.join(target, "README.md"),
        )
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 4)
        self.assertIn("README.md", err)
        self.assertEqual(fake.calls, [])
        self.assertTrue(os.path.islink(os.path.join(target, "README.md")))

    def test_symlinked_parent_is_never_followed_outside_the_target(self):
        target = self.target()
        outside = os.path.join(self.parent, "elsewhere")
        os.makedirs(os.path.join(outside, "workflows"))
        os.makedirs(target)
        os.symlink(outside, os.path.join(target, ".github"))
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 4)
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(os.path.join(outside, "workflows")), [])


class OnboardRun(unittest.TestCase):
    """Ticket 2: the scaffold is onboarded through the `init` writer."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        cfg = tempfile.mkdtemp(prefix="corral-config-")
        self.addCleanup(shutil.rmtree, cfg, True)
        patcher = mock.patch.dict(os.environ, {"CORRAL_CONFIG": cfg})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_dir = cfg

    def target(self, name=FIXTURE):
        return os.path.join(self.parent, name)

    def read(self, *parts):
        with open(os.path.join(*parts), encoding="utf-8") as handle:
            return handle.read()

    def toml(self, root=None):
        return self.read(root or self.target(), "corral.toml")

    def test_fresh_run_onboards_through_init_and_commits(self):
        fake = FakeGit(root=self.parent, status="?? README.md\n")
        code, out, err = run_new(path=self.parent, git=fake)
        self.assertEqual((code, err), (0, ""))
        branch = init.resolve_branch(None)
        self.assertIn(f'default_branch = "{branch}"', self.toml())
        self.assertTrue(os.path.isfile(os.path.join(self.target(), ".githooks", "commit-msg")))
        self.assertTrue(
            os.path.isfile(os.path.join(self.config_dir, "projects.d", f"{FIXTURE}.toml"))
        )
        self.assertIn(
            (["commit", "-m", f"chore: scaffold the {FIXTURE} project"], self.target()),
            fake.commits,
        )
        self.assertIn("corral github setup", out)
        self.assertIn("next: corral doctor", out)

    def test_set_branch_reaches_both_git_init_and_corral_toml(self):
        fake = FakeGit(root=self.parent, current="trunk", status="?? README.md\n")
        code, _, err = run_new(path=self.parent, git=fake, sets=("clone.default_branch=trunk",))
        self.assertEqual((code, err), (0, ""))
        self.assertIn((["init", "-b", "trunk"], self.target()), fake.calls)
        self.assertIn('default_branch = "trunk"', self.toml())

    def test_branch_agreement_follows_the_registry_default(self):
        with mock.patch.dict(settings.REGISTRY):
            settings.REGISTRY["clone.default_branch"] = settings.Setting(
                "clone.default_branch", str, "trunk", "the default branch of the clone"
            )
            fake = FakeGit(root=self.parent, current="", status="?? README.md\n")
            code, _, err = run_new(path=self.parent, git=fake)
        self.assertEqual((code, err), (0, ""))
        self.assertIn((["init", "-b", "trunk"], self.target()), fake.calls)
        self.assertIn('default_branch = "trunk"', self.toml())

    def test_collect_flags_land_in_corral_toml(self):
        fake = FakeGit(root=self.parent, status="?? README.md\n")
        code, _, err = run_new(
            path=self.parent,
            git=fake,
            test_cmd="make check",
            runtime="opencode",
            cap=3,
            sets=("board.ready=triage",),
        )
        self.assertEqual((code, err), (0, ""))
        text = self.toml()
        self.assertIn('test = "make check"', text)
        self.assertIn("cap = 3", text)
        self.assertIn('runtime = "opencode"', text)
        self.assertIn('ready = "triage"', text)

    def test_unknown_set_key_is_exit_4(self):
        fake = FakeGit(root=self.parent, status="?? README.md\n")
        code, _, err = run_new(path=self.parent, git=fake, sets=("nope.x=1",))
        self.assertEqual(code, 4)
        self.assertIn("nope.x", err)

    def test_rerun_converges_with_no_new_commit(self):
        first = FakeGit(root=self.parent, status="?? README.md\n")
        self.assertEqual(run_new(path=self.parent, git=first)[0], 0)
        self.assertEqual(len(first.commits), 1)
        before = {
            rel: self.read(self.target(), rel)
            for rel in ("corral.toml", "agents.overlay.md", "README.md")
        }
        rerun = FakeGit(root=self.target(), hooks=".githooks", status="")
        code, out, _ = run_new(path=self.parent, git=rerun)
        self.assertEqual(code, 1)
        self.assertEqual(rerun.commits, [])
        self.assertIn("is up to date", out)
        for rel, text in before.items():
            self.assertEqual(self.read(self.target(), rel), text)

    def test_failed_git_step_is_exit_4(self):
        for fail in ("status", "add", "commit"):
            with self.subTest(fail=fail):
                fake = FakeGit(root=self.parent, status="?? README.md\n", fail={fail})
                code, _, err = run_new(path=self.parent, git=fake)
                self.assertEqual(code, 4, fail)
                self.assertIn(fail, err, fail)

    def test_split_ops_writes_only_local_state_into_the_clone(self):
        ops_parent = self.parent
        fake = FakeGit(root=self.parent, status="?? README.md\n")
        code, out, err = run_new(path=self.parent, git=fake, ops="")
        self.assertEqual((code, err), (0, ""), out)
        ops_root = os.path.join(ops_parent, f"{FIXTURE}_ops")
        self.assertFalse(os.path.exists(os.path.join(self.target(), "corral.toml")))
        ops_toml = self.read(ops_root, "corral.toml")
        self.assertIn("[[repo]]", ops_toml)
        self.assertIn(f'name = "{FIXTURE}"', ops_toml)
        self.assertEqual(fake.hooks, os.path.join(os.path.realpath(ops_root), "hooks", FIXTURE))
        with open(
            os.path.join(self.target(), ".git", "info", "exclude"), encoding="utf-8"
        ) as handle:
            self.assertIn(".worktrees/", handle.read().splitlines())
        self.assertTrue(all(cwd == self.target() for _, cwd in fake.commits), fake.commits)
        self.assertTrue(os.path.isfile(os.path.join(ops_root, "agents.overlay.md")))

    def test_plan_on_a_missing_target_shows_the_onboard(self):
        fake = FakeGit(root=self.parent)
        code, out, err = run_new(path=self.parent, plan=True, git=fake)
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(os.path.exists(self.target()))
        self.assertIn("would run git init", out)
        self.assertIn("a/README.md", out)
        self.assertIn("corral init", out)
        self.assertIn("would commit", out)
        self.assertIn("next: corral doctor", out)


class GenericTemplates(unittest.TestCase):
    def test_no_render_leaks_host_identity(self):
        secrets = [socket.gethostname(), os.path.expanduser("~")]
        for var in ("USER", "LOGNAME"):
            value = os.environ.get(var, "")
            if value:
                secrets.append(value)
        for template in new.names():
            text = "\n".join(
                blob.decode("utf-8") for blob in new.render(template, FIXTURE).values()
            )
            for secret in secrets:
                self.assertNotIn(secret, text, template)


class ProviderLoading(unittest.TestCase):
    """Follow-up fix: the branch lookup loads its own providers.

    `new.run` in a process that never imported the CLI (or `launch`)
    failed with "clone.default_branch is not registered" (exit 4 on a
    valid request); the suite masked it because `cli` imports the
    providers. `init.resolve_branch` loads them itself now, so the run
    succeeds and the key is registered afterwards.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def _evict_launch(self):
        saved_module = sys.modules.pop("corral.launch", None)
        saved = {}
        for key in ("clone.path", "clone.default_branch"):
            if key in settings.REGISTRY:
                saved[key] = settings.REGISTRY.pop(key)
        self.addCleanup(self._restore_launch, saved_module, saved)

    def _restore_launch(self, saved_module, saved):
        sys.modules.pop("corral.launch", None)
        if saved_module is not None:
            sys.modules["corral.launch"] = saved_module
        settings.REGISTRY.update(saved)

    def test_new_run_registers_the_branch_setting_without_launch(self):
        self._evict_launch()
        self.assertNotIn("clone.default_branch", settings.REGISTRY)
        fake = FakeGit(root=self.parent)
        code, _, err = run_new(path=self.parent, git=fake, forge=FakeForge())
        self.assertEqual((code, err), (0, ""))
        self.assertIn("clone.default_branch", settings.REGISTRY)
        self.assertIn((["init", "-b", "main"], os.path.join(self.parent, FIXTURE)), fake.calls)


class ConvergedOutput(unittest.TestCase):
    """Follow-up fix: the next-step line prints on the OK path only."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self):
        return os.path.join(self.parent, FIXTURE)

    def test_converged_rerun_prints_no_action_line(self):
        first = FakeGit(root=self.parent, status="?? README.md\n")
        self.assertEqual(run_new(path=self.parent, git=first)[0], 0)
        rerun = FakeGit(root=self.target(), hooks=".githooks", status="")
        code, out, _ = run_new(path=self.parent, git=rerun)
        self.assertEqual(code, 1)
        self.assertIn("is up to date", out)
        self.assertNotIn("next:", out)

    def test_converged_plan_prints_no_action_line(self):
        first = FakeGit(root=self.parent, status="?? README.md\n")
        self.assertEqual(run_new(path=self.parent, git=first)[0], 0)
        rerun = FakeGit(root=self.target(), hooks=".githooks", status="")
        code, out, _ = run_new(path=self.parent, git=rerun, plan=True, forge=FakeForge())
        self.assertEqual(code, 1)
        self.assertIn("is up to date", out)
        self.assertNotIn("next:", out)


class SymlinkedParentWrite(unittest.TestCase):
    """Follow-up fix: `_write_file` checks before creating parents."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name

    def test_write_through_a_symlinked_parent_creates_nothing_outside(self):
        target = os.path.join(self.parent, "demo")
        os.makedirs(target)
        outside = os.path.join(self.parent, "elsewhere")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(target, ".github"))
        with self.assertRaises(new.NewError):
            new._write_file(target, ".github/workflows/check.yml", b"data\n")
        self.assertEqual(os.listdir(outside), [])
        self.assertFalse(os.path.exists(os.path.join(target, ".github", "workflows")))


class SplitCommitScope(unittest.TestCase):
    """Follow-up: the initial commit covers only the clone.

    The ops root is a separate working tree with its own history, so
    the operator commits it; `new` leaves it alone.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self):
        return os.path.join(self.parent, FIXTURE)

    def test_the_initial_commit_leaves_the_ops_root_alone(self):
        fake = FakeGit(root=self.parent, status="?? README.md\n")
        code, _, err = run_new(path=self.parent, git=fake, ops="")
        self.assertEqual((code, err), (0, ""))
        ops_root = os.path.join(self.parent, f"{FIXTURE}_ops")
        self.assertTrue(os.path.isfile(os.path.join(ops_root, "corral.toml")))
        self.assertTrue(fake.commits)
        self.assertTrue(all(cwd == self.target() for _, cwd in fake.commits))


class RemoteFlagValidation(unittest.TestCase):
    """`--public` and `--resume` need `--create-remote` (exit 4, no writes)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self):
        return os.path.join(self.parent, FIXTURE)

    def test_public_without_create_remote_is_exit_4_before_any_write(self):
        fake = FakeGit(root=self.parent)
        forge = FakeForge()
        code, _, err = run_new(path=self.parent, git=fake, forge=forge, public=True)
        self.assertEqual(code, 4)
        self.assertIn("--public", err)
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(fake.calls, [])
        self.assertEqual(forge.calls, [])

    def test_resume_without_create_remote_is_exit_4_before_any_write(self):
        fake = FakeGit(root=self.parent)
        forge = FakeForge()
        code, _, err = run_new(path=self.parent, git=fake, forge=forge, resume=True)
        self.assertEqual(code, 4)
        self.assertIn("--resume", err)
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(fake.calls, [])
        self.assertEqual(forge.calls, [])


class CreateRemote(unittest.TestCase):
    """Ticket 3: `--create-remote` through the forge interface."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self):
        return os.path.join(self.parent, FIXTURE)

    def repo(self, owner="operator", name=FIXTURE, empty=True, private=True):
        full = f"{owner}/{name}"
        return {
            full: {
                "private": private,
                "empty": empty,
                "url": f"https://example.com/{full}.git",
            }
        }

    def test_create_remote_creates_private_by_default_and_sets_origin(self):
        git = FakeGit(root=self.parent, status="?? README.md\n")
        forge = FakeForge(user="operator")
        code, out, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(forge.created, [("operator", FIXTURE, True)])
        self.assertEqual(git.origin_url, f"https://example.com/operator/{FIXTURE}.git")
        self.assertIn(f"created remote operator/{FIXTURE}", out)
        self.assertIn("set origin to", out)
        self.assertIn("next: corral doctor", out)
        self.assertLess(out.index("committed"), out.index("created remote"))

    def test_public_flag_creates_a_public_repo(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator")
        code, _, err = run_new(
            path=self.parent, git=git, forge=forge, create_remote=True, public=True
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(forge.created, [("operator", FIXTURE, False)])

    def test_owner_flag_wins_over_current_user(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="someone-else")
        code, _, err = run_new(
            path=self.parent, git=git, forge=forge, create_remote=True, owner="acme"
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(forge.created, [("acme", FIXTURE, True)])
        self.assertNotIn(("current_user",), forge.calls)
        self.assertIn(("repo_exists", f"acme/{FIXTURE}"), forge.calls)

    def test_owner_defaults_to_current_user(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator")
        code, _, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(("current_user",), forge.calls)
        self.assertEqual(forge.created, [("operator", FIXTURE, True)])

    def test_precheck_refuses_an_existing_remote_before_any_write(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", repos=self.repo())
        code, _, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 4)
        self.assertIn(f"operator/{FIXTURE}", err)
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(git.calls, [])
        self.assertEqual(forge.created, [])

    def test_bad_owner_flag_is_exit_4_before_any_write(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator")
        code, _, err = run_new(
            path=self.parent, git=git, forge=forge, create_remote=True, owner="not an owner"
        )
        self.assertEqual(code, 4)
        self.assertIn("owner", err)
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(forge.created, [])

    def test_unresolvable_owner_is_exit_4_before_any_write(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="")
        code, _, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 4)
        self.assertIn("owner", err)
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(forge.created, [])

    def test_unreachable_precheck_is_exit_3_and_writes_nothing(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", fail={"repo_exists"})
        code, _, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 3)
        self.assertIn("forge unreachable", err)
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(forge.created, [])

    def test_unreachable_current_user_is_exit_3_before_any_probe_or_write(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", fail={"current_user"})
        code, _, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 3)
        self.assertIn("forge unreachable", err)
        self.assertFalse(os.path.exists(self.target()))
        self.assertEqual(forge.created, [])
        self.assertNotIn("repo_exists", [call[0] for call in forge.calls])

    def test_race_between_precheck_and_create_is_exit_2(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", race={f"operator/{FIXTURE}"})
        code, _, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 2)
        self.assertIn("race", err)
        self.assertIn("may exist but be invisible to this account", err)
        self.assertIn("gh auth status", err)
        self.assertTrue(os.path.isfile(os.path.join(self.target(), "README.md")))
        self.assertEqual(forge.created, [])

    def test_unreachable_create_is_exit_3_with_the_manual_command(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", fail={"create_repo"})
        code, out, _ = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 3)
        self.assertTrue(os.path.isfile(os.path.join(self.target(), "README.md")))
        self.assertIn("not created", out)
        self.assertIn(f"gh repo create operator/{FIXTURE} --private", out)

    def test_create_and_probe_both_failing_stays_fail_closed(self):
        # The create may have succeeded before its read-back failed; with
        # the existence probe down too there is no proof either way, so
        # the report stays "not created". Deliberate: claiming a remote
        # exists that nobody can see would be worse than a redundant retry.
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", fail={"create_repo"})
        probe = mock.Mock(
            side_effect=[False, iface.ForgeError("forge unreachable during repo_exists")]
        )
        with mock.patch.object(forge, "repo_exists", probe):
            code, out, _ = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 3)
        self.assertTrue(os.path.isfile(os.path.join(self.target(), "README.md")))
        self.assertIn("not created", out)
        self.assertNotIn("but not wired", out)
        self.assertIn(f"gh repo create operator/{FIXTURE} --private", out)
        self.assertIsNone(git.origin_url)

    def test_post_create_clone_url_failure_reports_created_but_unwired(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", fail={"clone_url"})
        code, out, _ = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 3)
        self.assertTrue(os.path.isfile(os.path.join(self.target(), "README.md")))
        self.assertIn("created but not wired", out)
        self.assertIn("--resume", out)
        self.assertNotIn("gh repo create", out)
        self.assertIsNone(git.origin_url)
        self.assertEqual(len(forge.created), 1)

    def test_resume_adopts_an_empty_remote(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", repos=self.repo(empty=True))
        code, out, err = run_new(
            path=self.parent, git=git, forge=forge, create_remote=True, resume=True
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(forge.created, [])
        self.assertIn(("repo_empty", f"operator/{FIXTURE}"), forge.calls)
        self.assertEqual(git.origin_url, f"https://example.com/operator/{FIXTURE}.git")
        self.assertIn("adopted", out)

    def test_resume_refuses_a_nonempty_remote(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", repos=self.repo(empty=False))
        code, _, err = run_new(
            path=self.parent, git=git, forge=forge, create_remote=True, resume=True
        )
        self.assertEqual(code, 4)
        self.assertIn("not empty", err)
        self.assertIsNone(git.origin_url)
        self.assertEqual(forge.created, [])

    def test_failed_emptiness_check_is_exit_3_and_never_adopts(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", repos=self.repo(), fail={"repo_empty"})
        code, _, err = run_new(
            path=self.parent, git=git, forge=forge, create_remote=True, resume=True
        )
        self.assertEqual(code, 3)
        self.assertIn("forge unreachable", err)
        self.assertIsNone(git.origin_url)
        self.assertEqual(forge.created, [])

    def test_adopted_clone_url_failure_suggests_resume(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator", repos=self.repo(empty=True), fail={"clone_url"})
        code, out, _ = run_new(
            path=self.parent, git=git, forge=forge, create_remote=True, resume=True
        )
        self.assertEqual(code, 3)
        self.assertTrue(os.path.isfile(os.path.join(self.target(), "README.md")))
        self.assertIn("not wired", out)
        self.assertIn("next: re-run with --resume to adopt it", out)
        self.assertNotIn("gh repo create", out)
        self.assertIsNone(git.origin_url)
        self.assertEqual(forge.created, [])

    def test_conflicting_origin_is_exit_4(self):
        git = FakeGit(
            root=self.parent,
            status="?? README.md\n",
            origin_url="https://example.com/someone-else/other.git",
        )
        forge = FakeForge(user="operator")
        code, _, err = run_new(path=self.parent, git=git, forge=forge, create_remote=True)
        self.assertEqual(code, 4)
        self.assertIn("already points at", err)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(forge.created, [("operator", FIXTURE, True)])
        self.assertEqual(git.origin_url, "https://example.com/someone-else/other.git")

    def test_resume_rerun_after_success_converges_silently(self):
        first = FakeGit(root=self.parent, status="?? README.md\n")
        forge = FakeForge(user="operator")
        tracker = FakeTracker()
        code, _, err = run_new(
            path=self.parent, git=first, forge=forge, tracker=tracker, create_remote=True
        )
        self.assertEqual((code, err), (0, ""))
        url = f"https://example.com/operator/{FIXTURE}.git"
        self.assertEqual(first.origin_url, url)
        rerun = FakeGit(root=self.target(), hooks=".githooks", status="", origin_url=url)
        code, out, _ = run_new(
            path=self.parent,
            git=rerun,
            forge=forge,
            tracker=tracker,
            create_remote=True,
            resume=True,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("next:", out)
        self.assertEqual(len(forge.created), 1)
        self.assertEqual(rerun.commits, [])
        self.assertEqual(rerun.origin_url, url)


class LabelBootstrap(unittest.TestCase):
    """Ticket 4: `--create-remote` bootstraps the board labels."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self):
        return os.path.join(self.parent, FIXTURE)

    def test_create_remote_bootstraps_the_labels_after_the_origin(self):
        git = FakeGit(root=self.parent, status="?? README.md\n")
        forge = FakeForge(user="operator")
        tracker = FakeTracker()
        code, out, err = run_new(
            path=self.parent, git=git, forge=forge, tracker=tracker, create_remote=True
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn(("ensure_labels", f"operator/{FIXTURE}"), tracker.calls)
        self.assertIn(f"created label ready on operator/{FIXTURE}\n", out)
        self.assertIn(f"created label priority:backlog on operator/{FIXTURE}\n", out)
        self.assertLess(out.index("created remote"), out.index("created label ready"))
        self.assertLess(out.index("set origin to"), out.index("created label ready"))
        self.assertIn("next: corral doctor", out)

    def test_refused_labels_are_exit_1_with_fix_lines(self):
        git = FakeGit(root=self.parent, status="?? README.md\n")
        forge = FakeForge(user="operator")
        tracker = FakeTracker(refused={"hold"})
        code, out, err = run_new(
            path=self.parent, git=git, forge=forge, tracker=tracker, create_remote=True
        )
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn(f"created label ready on operator/{FIXTURE}\n", out)
        self.assertIn(
            f"refused label hold on operator/{FIXTURE}: ask an owner to create it",
            out,
        )
        self.assertIn("next: corral github setup", out)
        self.assertNotIn("next: corral doctor", out)

    def test_unreachable_labels_are_exit_3_with_the_manual_command(self):
        git = FakeGit(root=self.parent, status="?? README.md\n")
        forge = FakeForge(user="operator")
        tracker = FakeTracker(ensure_error=tracker_iface.TrackerError("down"))
        code, out, _ = run_new(
            path=self.parent, git=git, forge=forge, tracker=tracker, create_remote=True
        )
        self.assertEqual(code, 3)
        self.assertEqual(len(forge.created), 1)
        self.assertIn(f"labels on operator/{FIXTURE} not bootstrapped", out)
        self.assertIn("next: corral github setup", out)
        self.assertNotIn("next: corral doctor", out)

    def test_adopted_remotes_bootstrap_too(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(
            user="operator",
            repos={
                f"operator/{FIXTURE}": {
                    "private": True,
                    "empty": True,
                    "url": f"https://example.com/operator/{FIXTURE}.git",
                }
            },
        )
        tracker = FakeTracker()
        code, out, err = run_new(
            path=self.parent,
            git=git,
            forge=forge,
            tracker=tracker,
            create_remote=True,
            resume=True,
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn(("ensure_labels", f"operator/{FIXTURE}"), tracker.calls)
        self.assertIn(f"created label ready on operator/{FIXTURE}\n", out)

    def test_conflicting_origin_never_reaches_the_labels(self):
        git = FakeGit(
            root=self.parent,
            status="?? README.md\n",
            origin_url="https://example.com/someone-else/other.git",
        )
        forge = FakeForge(user="operator")
        tracker = FakeTracker()
        code, _, _ = run_new(
            path=self.parent, git=git, forge=forge, tracker=tracker, create_remote=True
        )
        self.assertEqual(code, 4)
        self.assertEqual(tracker.calls, [])


class RemotePlan(unittest.TestCase):
    """`--plan` prints the outward command and writes nothing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        isolated_config(self)

    def target(self):
        return os.path.join(self.parent, FIXTURE)

    def test_plain_run_prints_the_planned_create_command(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge()
        code, out, err = run_new(path=self.parent, git=git, forge=forge)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"gh repo create {FIXTURE} --private", out)
        self.assertNotIn(("current_user",), forge.calls)

    def test_plan_with_owner_names_the_owner(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge()
        code, out, _ = run_new(path=self.parent, git=git, forge=forge, plan=True, owner="acme")
        self.assertEqual(code, 0)
        self.assertIn(f"gh repo create acme/{FIXTURE} --private", out)
        self.assertFalse(os.path.exists(self.target()))

    def test_plan_with_create_remote_prints_would_run(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(user="operator")
        code, out, _ = run_new(
            path=self.parent, git=git, forge=forge, plan=True, create_remote=True
        )
        self.assertEqual(code, 0)
        self.assertIn(f"would run gh repo create operator/{FIXTURE} --private", out)
        self.assertIn(f"would ensure labels on operator/{FIXTURE}", out)
        self.assertFalse(os.path.exists(self.target()))

    def test_plan_with_resume_notes_adoption(self):
        git = FakeGit(root=self.parent)
        forge = FakeForge(
            user="operator",
            repos={
                f"operator/{FIXTURE}": {
                    "private": True,
                    "empty": True,
                    "url": f"https://example.com/operator/{FIXTURE}.git",
                }
            },
        )
        code, out, _ = run_new(
            path=self.parent, git=git, forge=forge, plan=True, create_remote=True, resume=True
        )
        self.assertEqual(code, 0)
        self.assertIn(f"would adopt operator/{FIXTURE} when empty", out)
        self.assertNotIn("would run gh repo create", out)
        self.assertIn(f"would ensure labels on operator/{FIXTURE}", out)
        self.assertFalse(os.path.exists(self.target()))


class CliWiring(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = tmp.name
        cfg = tempfile.mkdtemp(prefix="corral-config-")
        self.addCleanup(shutil.rmtree, cfg, True)
        xdg = tempfile.mkdtemp(prefix="corral-xdg-")
        self.addCleanup(shutil.rmtree, xdg, True)
        os.makedirs(os.path.join(xdg, "git"))
        with open(os.path.join(xdg, "git", "config"), "w", encoding="utf-8") as handle:
            handle.write("[user]\n\tname = Suite\n\temail = suite@corral.invalid\n")
        patcher = mock.patch.dict(os.environ, {"CORRAL_CONFIG": cfg, "XDG_CONFIG_HOME": xdg})
        patcher.start()
        self.addCleanup(patcher.stop)

    def target(self):
        return os.path.join(self.parent, "demo")

    def test_new_through_cli_main_converges(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["new", "demo", "--path", self.parent])
        self.assertEqual((code, err.getvalue()), (0, ""))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            again = cli.main(["new", "demo", "--path", self.parent])
        self.assertEqual(again, 1)
        with open(os.path.join(self.target(), "README.md"), encoding="utf-8") as handle:
            self.assertIn("# demo", handle.read())

    def test_initial_commit_holds_the_scaffold(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["new", "demo", "--path", self.parent])
        self.assertEqual(code, 0)
        log = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=self.target(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(log.returncode, 0, log.stderr)
        self.assertEqual(log.stdout.strip(), "chore: scaffold the demo project")
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.target(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(status.stdout.strip(), "")

    def test_init_converges_and_uninit_keeps_the_scaffold(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["new", "demo", "--path", self.parent]), 0)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["init", self.target()]), 1)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["uninit", self.target()]), 0)
        for rel in ("README.md", "Makefile", "AGENTS.md", "corral.toml", "agents.overlay.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.target(), rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(self.target(), ".githooks")))


if __name__ == "__main__":
    unittest.main()
