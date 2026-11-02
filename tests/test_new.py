"""`corral new` (ticket 1): scaffold templates, writes, exits, idempotency.

Local only, no `init` wiring yet. Every git call goes through the
`FakeGit` harness from `tests.test_init` (reused, not redefined); every
file lands in a temporary directory. The CLI wiring test runs the real
entry point, which runs real local git inside the temp tree, the same
way `tests/test_init.py` already does for `init`.
"""

import io
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from corral import cli, new
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


def run_new(*, name=FIXTURE, path=None, template="blank", plan=False, force=False, git=None):
    """`new.run` with captured output: `(code, stdout, stderr)`."""
    opts = new.Options(name=name, path=path, template=template, plan=plan, force=force)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if git is None:
            code = new.run(opts)
        else:
            code = new.run(opts, git=git)
    return code, out.getvalue(), err.getvalue()


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
        self.assertEqual(
            fake.calls,
            [
                (["rev-parse", "--show-toplevel"], self.target()),
                (["init", "-b", new.default_branch()], self.target()),
            ],
        )
        self.assertEqual(self.snapshot(self.target()), new.render("blank", FIXTURE))
        self.assertIn("README.md", out)

    def test_rerun_is_nothing_to_do_and_changes_no_byte(self):
        fake = FakeGit(root=self.parent)
        code, _, _ = run_new(path=self.parent, git=fake)
        self.assertEqual(code, 0)
        before = self.snapshot(self.target())
        rerun = FakeGit(root=self.target())
        code, out, _ = run_new(path=self.parent, git=rerun)
        self.assertEqual(code, 1)
        self.assertEqual(rerun.calls, [(["rev-parse", "--show-toplevel"], self.target())])
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
        code, _, _ = run_new(path=self.parent, plan=True, git=FakeGit(root=self.target()))
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
        self.assertEqual(self.snapshot(target), rendered)

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
        self.assertEqual(self.snapshot(target), {**rendered, "README.md": edited})
        self.assertIn("README.md", out)

    def test_force_rerun_with_only_a_differ_is_nothing_to_do(self):
        target = self.target()
        os.makedirs(target)
        rendered = new.render("blank", FIXTURE)
        for rel, blob in rendered.items():
            if rel == "README.md":
                continue
            full = os.path.join(target, rel)
            os.makedirs(os.path.dirname(full) or target, exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(blob)
        with open(os.path.join(target, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("my own readme\n")
        code, _, _ = run_new(path=self.parent, force=True, git=FakeGit(root=target))
        self.assertEqual(code, 1)


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
    def test_new_through_cli_main_converges(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["new", "demo", "--path", tmp.name])
        self.assertEqual((code, err.getvalue()), (0, ""))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            again = cli.main(["new", "demo", "--path", tmp.name])
        self.assertEqual(again, 1)
        with open(os.path.join(tmp.name, "demo", "README.md"), encoding="utf-8") as handle:
            self.assertIn("# demo", handle.read())


if __name__ == "__main__":
    unittest.main()
