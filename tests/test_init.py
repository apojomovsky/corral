"""`corral init` for the inline layout: detection, `corral.toml`, exits."""

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

from corral import cli, detect, init, settings
from corral.gitenv import clean_env


def proc(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class FakeGit:
    """An injectable git seam over a fixed directory."""

    def __init__(self, root, *, origin_url=None, head=None, current="main", hooks=""):
        self.root = root
        self.origin_url = origin_url
        self.head = head
        self.current = current
        self.hooks = hooks
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        if args == ["rev-parse", "--show-toplevel"]:
            return proc(0, self.root + "\n")
        if args == ["remote", "get-url", "origin"]:
            if self.origin_url is None:
                return proc(1, "")
            return proc(0, self.origin_url + "\n")
        if args == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
            if self.head is None:
                return proc(1, "")
            return proc(0, f"refs/remotes/origin/{self.head}\n")
        if args == ["branch", "--show-current"]:
            return proc(0, self.current + "\n")
        if args == ["config", "core.hooksPath"]:
            if not self.hooks:
                return proc(1, "")
            return proc(0, self.hooks + "\n")
        raise AssertionError(f"unexpected git call {args}")


class DetectCase(unittest.TestCase):
    def test_remote_slug_accepts_github_shapes_only(self):
        cases = {
            "git@github.com:owner/name.git": "owner/name",
            "git@github.com:owner/name": "owner/name",
            "https://github.com/owner/name": "owner/name",
            "https://github.com/owner/name.git": "owner/name",
            "ssh://git@github.com/owner/name.git": "owner/name",
            "git@example.com:owner/name.git": None,
            "https://example.com/owner/name": None,
            "https://github.com/owner": None,
            "https://github.com/a/b/c": None,
            "/srv/repos/name": None,
            "": None,
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect.remote_slug(url), want)

    def test_origin_slug_reads_the_origin_remote(self):
        git = FakeGit("/repo", origin_url="git@github.com:owner/name.git")
        self.assertEqual(detect.origin_slug("/repo", git), "owner/name")
        git = FakeGit("/repo")
        self.assertIsNone(detect.origin_slug("/repo", git))

    def test_default_branch_prefers_remote_head(self):
        git = FakeGit("/repo", head="trunk", current="feature/1-x")
        self.assertEqual(detect.default_branch("/repo", git), "trunk")

    def test_default_branch_falls_back_to_current(self):
        git = FakeGit("/repo", current="feature/1-x")
        self.assertEqual(detect.default_branch("/repo", git), "feature/1-x")

    def test_default_branch_is_none_when_detached_or_unknown(self):
        self.assertIsNone(detect.default_branch("/repo", FakeGit("/repo", current="")))
        git = FakeGit("/repo", current="")
        git.calls.clear()

        def failing(args, cwd):
            return proc(1, "")

        self.assertIsNone(detect.default_branch("/repo", failing))

    def test_detect_registers_checks_test(self):
        self.assertIn("corral.detect", settings.PROVIDERS)
        self.assertEqual(settings.REGISTRY["checks.test"].default, "")


class TestCommandCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, rel, text=""):
        full = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_empty_dir_detects_nothing(self):
        self.assertIsNone(detect.test_command(self.dir))

    def test_makefile_wins_over_everything(self):
        self.write("Makefile", "test:\n\ttrue\n")
        self.write("pyproject.toml", "")
        self.write("package.json", '{"scripts": {"test": "node --test"}}')
        self.write("Cargo.toml", "")
        self.write("go.mod", "")
        self.assertEqual(detect.test_command(self.dir), "make test")

    def test_makefile_without_a_test_target_is_skipped(self):
        self.write("Makefile", ".PHONY: check\ncheck:\n\ttrue\n")
        self.write("Cargo.toml", "")
        self.assertEqual(detect.test_command(self.dir), "cargo test")

    def test_a_test_variable_is_not_a_test_target(self):
        for line in ("test := unit\n", "test ::= unit\n", "test= unit\n"):
            with self.subTest(line=line):
                self.write("Makefile", line)
                self.write("Cargo.toml", "")
                self.assertEqual(detect.test_command(self.dir), "cargo test")

    def test_a_double_colon_test_rule_is_a_target(self):
        self.write("Makefile", "test::\n\ttrue\n")
        self.assertEqual(detect.test_command(self.dir), "make test")

    def test_pyproject_with_tests_uses_unittest(self):
        self.write("pyproject.toml", "")
        self.write("tests/test_app.py", "")
        self.assertEqual(detect.test_command(self.dir), "uv run python -m unittest")

    def test_pyproject_without_tests_uses_pytest(self):
        self.write("pyproject.toml", "")
        self.assertEqual(detect.test_command(self.dir), "uv run pytest")

    def test_pyproject_with_empty_tests_dir_uses_pytest(self):
        self.write("pyproject.toml", "")
        os.makedirs(os.path.join(self.dir, "tests"))
        self.assertEqual(detect.test_command(self.dir), "uv run pytest")

    def test_package_json_with_a_test_script(self):
        self.write("package.json", '{"scripts": {"test": "node --test"}}')
        self.assertEqual(detect.test_command(self.dir), "npm test")

    def test_package_json_without_a_test_script_falls_through(self):
        self.write("package.json", '{"scripts": {"lint": "true"}}')
        self.write("go.mod", "")
        self.assertEqual(detect.test_command(self.dir), "go test ./...")

    def test_broken_package_json_falls_through(self):
        self.write("package.json", "{nope")
        self.write("Cargo.toml", "")
        self.assertEqual(detect.test_command(self.dir), "cargo test")

    def test_cargo_and_go(self):
        self.write("Cargo.toml", "")
        self.assertEqual(detect.test_command(self.dir), "cargo test")
        os.remove(os.path.join(self.dir, "Cargo.toml"))
        self.write("go.mod", "")
        self.assertEqual(detect.test_command(self.dir), "go test ./...")


class ExistingCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, rel, text=""):
        full = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_bare_dir_finds_nothing(self):
        found = detect.existing(self.dir, FakeGit(self.dir))
        self.assertEqual(found, detect.Existing(False, False, False))

    def test_agent_files_are_found(self):
        self.write("AGENTS.md", "")
        self.write("CLAUDE.md", "")
        found = detect.existing(self.dir, FakeGit(self.dir))
        self.assertEqual((found.agents_md, found.claude_md), (True, True))

    def test_hooks_path_counts_as_hooks(self):
        found = detect.existing(self.dir, FakeGit(self.dir, hooks="/srv/hooks"))
        self.assertTrue(found.hooks)

    def test_a_real_hook_counts_samples_do_not(self):
        self.write(".git/hooks/pre-commit", "")
        found = detect.existing(self.dir, FakeGit(self.dir))
        self.assertTrue(found.hooks)
        os.remove(os.path.join(self.dir, ".git", "hooks", "pre-commit"))
        self.write(".git/hooks/pre-commit.sample", "")
        found = detect.existing(self.dir, FakeGit(self.dir))
        self.assertFalse(found.hooks)


TAIL = """# Defaults: uncomment to override.
#
# [admission]
# admission.repo_cap = 0  # live plus pending agents per repo (0 means none)
# admission.total_cap = 4  # live plus pending agents the host admits in total
#
# [agents]
# agents.cap = 1  # this project's ceiling of live agents on the host
# agents.runtime = "omp"  # agent runtime: omp or opencode
# agents.unit_prefix = "corral-agent-"  # prefix of transient agent unit names
#
# [board]
# board.hold_label = "hold"  # label that holds a ticket for a human
# board.ready = "ready"  # label that makes an issue eligible
#
# [git]
# git.branch = "{type}/{issue}-{slug}"  # branch name template
# git.commits.conventional = true  # enforce Conventional Commits
# git.worktrees = ".worktrees/"  # worktree root, relative to the clone
#
# [quota]
# quota.low_pct = 90  # usage percent at which a provider reads low
# quota.unknown_blocks = false  # an unknown provider blocks its roles instead of passing them
#
# [registry]
# registry.count_toward_cap = true  # live registered ticket workers take cap slots
# registry.gone_s = 3600  # seconds without a heartbeat before an agent is dropped
# registry.stale_s = 600  # seconds without a heartbeat before an agent reads stale
"""

HEAD = """# corral.toml, written by `corral init`. Settings that differ
# from the defaults live in [[repo]]; everything else is
# commented out below with its default.
[[repo]]
name = "%s"

[repo.checks]
test = "%s"

[repo.clone]
default_branch = "main"
path = "."

"""

GOLDEN_EMPTY = HEAD % ("empty", "make check") + TAIL
GOLDEN_PYUV = HEAD % ("pyuv", "uv run python -m unittest") + TAIL
GOLDEN_PYPLAIN = HEAD % ("pyplain", "uv run pytest") + TAIL
GOLDEN_NODE = HEAD % ("node", "npm test") + TAIL
GOLDEN_RUSTY = HEAD % ("rusty", "cargo test") + TAIL
GOLDEN_GOPHER = HEAD % ("gopher", "go test ./...") + TAIL
GOLDEN_DOCSY = HEAD % ("docsy", "make test") + TAIL

GOLDEN_FLAGS = """# corral.toml, written by `corral init`. Settings that differ
# from the defaults live in [[repo]]; everything else is
# commented out below with its default.
[[repo]]
name = "empty"

[repo.agents]
cap = 3
runtime = "opencode"

[repo.board]
ready = "triage"

[repo.checks]
test = "make check"

[repo.clone]
default_branch = "main"
path = "."

[repo.git]
commits.conventional = false

# Defaults: uncomment to override.
#
# [admission]
# admission.repo_cap = 0  # live plus pending agents per repo (0 means none)
# admission.total_cap = 4  # live plus pending agents the host admits in total
#
# [agents]
# agents.unit_prefix = "corral-agent-"  # prefix of transient agent unit names
#
# [board]
# board.hold_label = "hold"  # label that holds a ticket for a human
#
# [git]
# git.branch = "{type}/{issue}-{slug}"  # branch name template
# git.worktrees = ".worktrees/"  # worktree root, relative to the clone
#
# [quota]
# quota.low_pct = 90  # usage percent at which a provider reads low
# quota.unknown_blocks = false  # an unknown provider blocks its roles instead of passing them
#
# [registry]
# registry.count_toward_cap = true  # live registered ticket workers take cap slots
# registry.gone_s = 3600  # seconds without a heartbeat before an agent is dropped
# registry.stale_s = 600  # seconds without a heartbeat before an agent reads stale
"""


class Fixture(unittest.TestCase):
    """A real local git repository under a fixed name (goldens embed it)."""

    name = "repo"
    files: dict = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, self.name)
        os.makedirs(self.root)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        for rel, text in self.files.items():
            self.write(rel, text)
        self.write("seed.txt", "seed\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "seed")

    def git(self, *args, cwd=None):
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root if cwd is None else cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def write(self, rel, text=""):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["init", *argv])
        return code, out.getvalue(), err.getvalue()

    def target(self):
        return os.path.join(self.root, "corral.toml")


class GoldenCase(Fixture):
    name = "empty"
    golden = GOLDEN_EMPTY
    extra = ("--test-cmd", "make check")

    def test_plan_prints_the_golden_and_writes_nothing(self):
        code, out, _ = self.run_cli("--plan", *self.extra, self.root)
        self.assertEqual(code, 0)
        self.assertIn('+name = "%s"\n' % self.name, out)
        self.assertFalse(os.path.exists(self.target()))
        _, text = self.plan_inputs()
        self.assertEqual(text, self.golden)

    def plan_inputs(self):
        root, inputs = init.collect(self.root, git=init.gc.git, test_cmd=self.flag_cmd(), sets=())
        return root, init.render(inputs)

    def flag_cmd(self):
        return "make check" if "--test-cmd" in self.extra else None

    def test_write_then_rerun_is_nothing_to_do(self):
        code, out, _ = self.run_cli(*self.extra, self.root)
        self.assertEqual(code, 0, out)
        with open(self.target(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.golden)
        code, out, _ = self.run_cli(*self.extra, self.root)
        self.assertEqual(code, 1)
        self.assertIn("is up to date", out)

    def test_plan_after_write_is_nothing_to_do(self):
        self.assertEqual(self.run_cli(*self.extra, self.root)[0], 0)
        code, out, _ = self.run_cli("--plan", *self.extra, self.root)
        self.assertEqual(code, 1)
        self.assertNotIn("+++", out)

    def test_round_trip_through_settings_load(self):
        self.assertEqual(self.run_cli(*self.extra, self.root)[0], 0)
        env = {"CORRAL_CONFIG": os.path.join(self.root, "none")}
        resolved = settings.load(root=self.root, repo=self.name, env=env)
        self.assertEqual(resolved["checks.test"], self.want_test())
        self.assertEqual(resolved["clone.path"], ".")
        self.assertEqual(resolved["clone.default_branch"], "main")

    def want_test(self):
        return self.extra[self.extra.index("--test-cmd") + 1]


class PyUvCase(GoldenCase):
    name = "pyuv"
    files = {"pyproject.toml": "", "tests/test_app.py": ""}
    golden = GOLDEN_PYUV
    extra = ()

    def want_test(self):
        return "uv run python -m unittest"


class PyPlainCase(GoldenCase):
    name = "pyplain"
    files = {"pyproject.toml": ""}
    golden = GOLDEN_PYPLAIN
    extra = ()

    def want_test(self):
        return "uv run pytest"


class NodeCase(GoldenCase):
    name = "node"
    files = {"package.json": '{"name": "n", "scripts": {"test": "node --test"}}'}
    golden = GOLDEN_NODE
    extra = ()

    def want_test(self):
        return "npm test"


class RustCase(GoldenCase):
    name = "rusty"
    files = {"Cargo.toml": ""}
    golden = GOLDEN_RUSTY
    extra = ()

    def want_test(self):
        return "cargo test"


class GoCase(GoldenCase):
    name = "gopher"
    files = {"go.mod": ""}
    golden = GOLDEN_GOPHER
    extra = ()

    def want_test(self):
        return "go test ./..."


class DocsyCase(GoldenCase):
    name = "docsy"
    files = {
        "Makefile": "test:\n\ttrue\n",
        "AGENTS.md": "build: make test\n",
        "CLAUDE.md": "same\n",
    }
    golden = GOLDEN_DOCSY
    extra = ()

    def setUp(self):
        super().setUp()
        hooks = os.path.join(self.root, ".git", "hooks")
        with open(os.path.join(hooks, "pre-commit"), "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")

    def want_test(self):
        return "make test"

    def test_existing_files_are_never_touched(self):
        before = {}
        for here, _, names in os.walk(self.root):
            for entry in names:
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    before[os.path.relpath(full, self.root)] = handle.read()
        code, _, _ = self.run_cli(self.root)
        self.assertEqual(code, 0)
        after = {}
        for here, _, names in os.walk(self.root):
            for entry in names:
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    after[os.path.relpath(full, self.root)] = handle.read()
        added = set(after) - set(before)
        self.assertEqual(added, {"corral.toml"})
        for rel in before:
            self.assertEqual(after[rel], before[rel], rel)


class FlagsCase(Fixture):
    name = "empty"

    def test_flags_land_in_the_repo_entry(self):
        code, _, _ = self.run_cli(
            "--test-cmd",
            "make check",
            "--cap",
            "3",
            "--runtime",
            "opencode",
            "--set",
            "board.ready=triage",
            "--set",
            "git.commits.conventional=false",
            self.root,
        )
        self.assertEqual(code, 0)
        with open(self.target(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), GOLDEN_FLAGS)
        env = {"CORRAL_CONFIG": os.path.join(self.root, "none")}
        resolved = settings.load(root=self.root, repo=self.name, env=env)
        self.assertEqual(resolved["agents.cap"], 3)
        self.assertEqual(resolved["agents.runtime"], "opencode")
        self.assertEqual(resolved["board.ready"], "triage")
        self.assertIs(resolved["git.commits.conventional"], False)

    def test_yes_is_a_no_op(self):
        code, _, _ = self.run_cli("--yes", "--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        with open(self.target(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), GOLDEN_EMPTY)

    def test_every_registered_setting_appears_in_the_output(self):
        _, inputs = init.collect(self.root, git=init.gc.git, test_cmd="make check", sets=())
        rendered = init.render(inputs)
        active = {"clone.path", "clone.default_branch", "checks.test"}
        active.update(key for key, _ in inputs.extra)
        for key in settings.REGISTRY:
            prefix, leaf = key.split(".", 1)
            with self.subTest(key=key):
                if key in active:
                    self.assertIn(f"[repo.{prefix}]\n", rendered)
                    self.assertIn(f"{leaf} = ", rendered)
                else:
                    setting = settings.REGISTRY[key]
                    self.assertIn(f"# {key} = {init._toml(setting.default)}", rendered)


class ErrorsCase(Fixture):
    name = "empty"

    def test_missing_test_command_names_the_flag(self):
        code, _, err = self.run_cli(self.root)
        self.assertEqual(code, 4)
        self.assertIn("--test-cmd", err)

    def test_unknown_set_key_names_the_key(self):
        code, _, err = self.run_cli("--set", "nope.x=1", self.root)
        self.assertEqual(code, 4)
        self.assertIn("nope.x", err)

    def test_wrong_type_set_names_the_key(self):
        for raw in ("agents.cap=many", "git.commits.conventional=maybe"):
            with self.subTest(raw=raw):
                code, _, err = self.run_cli("--test-cmd", "make check", "--set", raw, self.root)
                self.assertEqual(code, 4)
                self.assertIn(raw.split("=")[0], err)

    def test_malformed_set_is_rejected(self):
        code, _, err = self.run_cli("--test-cmd", "make check", "--set", "bare", self.root)
        self.assertEqual(code, 4)
        self.assertIn("bare", err)

    def test_path_outside_git_is_rejected(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        code, _, err = self.run_cli(other.name)
        self.assertEqual(code, 4)
        self.assertIn(other.name, err)

    def test_missing_and_file_paths_are_rejected(self):
        missing = os.path.join(self.root, "no-such-dir")
        code, _, err = self.run_cli("--test-cmd", "make check", missing)
        self.assertEqual(code, 4)
        self.assertIn(missing, err)
        code, _, err = self.run_cli("--test-cmd", "make check", __file__)
        self.assertEqual(code, 4)
        self.assertIn(__file__, err)

    def test_set_trio_keys_are_honored(self):
        code, _, _ = self.run_cli(
            "--test-cmd",
            "make check",
            "--set",
            "clone.default_branch=zzz",
            "--set",
            "clone.path=sub",
            self.root,
        )
        self.assertEqual(code, 0)
        with open(self.target(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('default_branch = "zzz"', text)
        self.assertIn('path = "sub"', text)
        env = {"CORRAL_CONFIG": os.path.join(self.root, "none")}
        resolved = settings.load(root=self.root, repo=self.name, env=env)
        self.assertEqual(resolved["clone.default_branch"], "zzz")
        self.assertEqual(resolved["clone.path"], "sub")

    def test_unknown_runtime_names_the_flag(self):
        code, _, err = self.run_cli("--test-cmd", "make check", "--runtime", "bogus", self.root)
        self.assertEqual(code, 4)
        self.assertIn("--runtime", err)

    def test_differing_rerun_prints_diff_and_needs_force(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        code, out, err = self.run_cli("--test-cmd", "other", self.root)
        self.assertEqual(code, 4)
        self.assertIn('-test = "make check"', out)
        self.assertIn('+test = "other"', out)
        self.assertIn("--force", err)
        with open(self.target(), encoding="utf-8") as handle:
            before = handle.read()
        code, _, _ = self.run_cli("--test-cmd", "other", "--force", self.root)
        self.assertEqual(code, 0)
        with open(self.target(), encoding="utf-8") as handle:
            self.assertNotEqual(handle.read(), before)

    def test_path_may_be_a_subdirectory(self):
        sub = os.path.join(self.root, "pkg")
        os.makedirs(sub)
        code, out, _ = self.run_cli("--test-cmd", "make check", sub)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(self.target()))
        self.assertFalse(os.path.exists(os.path.join(sub, "corral.toml")))

    def test_staging_branch_is_detected(self):
        self.git("checkout", "-qb", "staging")
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        env = {"CORRAL_CONFIG": os.path.join(self.root, "none")}
        resolved = settings.load(root=self.root, repo=self.name, env=env)
        self.assertEqual(resolved["clone.default_branch"], "staging")


class SlugCase(unittest.TestCase):
    def test_origin_slug_names_the_repo_entry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "checkout")
        os.makedirs(root)
        git = FakeGit(root, origin_url="https://github.com/owner/name.git")
        got, inputs = init.collect(root, git=git, test_cmd="make check")
        self.assertEqual((got, inputs.name), (root, "name"))
        self.assertTrue(inputs.test_command)


if __name__ == "__main__":
    unittest.main()
