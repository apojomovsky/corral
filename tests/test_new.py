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
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, init, new, settings
from tests.test_init import FakeGit

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
    default_branch=None,
    sets=(),
    test_cmd=None,
    runtime=None,
    cap=None,
    ops=None,
    ops_name=None,
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
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if git is None:
            code = new.run(opts)
        else:
            code = new.run(opts, git=git)
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
