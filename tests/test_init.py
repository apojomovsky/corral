"""`corral init` and `corral uninit`: files written, exits, idempotency."""

import inspect
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from corral import cli, detect, init, projects, settings
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

    def test_plain_tests_dir_suggests_unittest_discover(self):
        self.write("tests/test_app.py", "")
        self.assertIsNone(detect.test_command(self.dir))
        self.assertEqual(
            detect.suggested_test_command(self.dir),
            "python3 -m unittest discover -s tests -t .",
        )

    def test_detected_runner_suppresses_the_hint(self):
        self.write("Makefile", "test:\n\ttrue\n")
        self.write("tests/test_app.py", "")
        self.assertEqual(detect.test_command(self.dir), "make test")
        self.assertIsNone(detect.suggested_test_command(self.dir))

    def test_empty_dir_suggests_nothing(self):
        self.assertIsNone(detect.suggested_test_command(self.dir))

    def test_tests_dir_without_test_files_suggests_nothing(self):
        self.write("tests/helpers.py", "")
        os.makedirs(os.path.join(self.dir, "tests", "data"), exist_ok=True)
        self.assertIsNone(detect.test_command(self.dir))
        self.assertIsNone(detect.suggested_test_command(self.dir))


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
        found = detect.existing(self.dir)
        self.assertEqual(found, detect.Existing(False, False))

    def test_agent_files_are_found(self):
        self.write("AGENTS.md", "")
        self.write("CLAUDE.md", "")
        found = detect.existing(self.dir)
        self.assertEqual((found.agents_md, found.claude_md), (True, True))


TAIL = """# Defaults: uncomment to override.
#
# [admission]
# admission.repo_cap = 0  # live plus pending agents per repo (0 means none)
# admission.total_cap = 4  # live plus pending agents the host admits in total
#
# [agents]
# agents.cap = 1  # this project's ceiling of live agents on the host
# agents.runtime = "omp"  # agent runtime: omp or opencode or muse
# agents.unit_prefix = "corral-agent-"  # prefix of transient agent unit names
#
# [board]
# board.hold_label = "hold"  # label that holds a ticket for a human
# board.item_max_age_s = 300  # seconds before a freshness-gated read re-reads a board row live
# board.poll_pages = 5  # board poll page cap per pass (default: small)
# board.ready = "ready"  # label that makes an issue eligible
# board.schema_max_age_s = 3600  # seconds before the board step refreshes the cached board schema
#
# [checks]
# checks.no_em_dash = true  # takeoff refuses added lines and bodies with an em-dash
# checks.suite_lock_timeout_s = 3600  # seconds a second takeoff waits for the full-suite lock before giving up (exit 75)
# checks.takeoff = []  # takeoff gate commands, run in order in the worktree
# checks.timeout_s = 1800  # seconds before a takeoff command is killed
#
# [disk]
# disk.min_free_gb = 5  # free gigabytes under which the disk reads low
# disk.min_free_pct = 10  # free-space percent under which the disk reads low
# disk.session_retention_days = 14  # days before a finished ticket's session dir is a reclaim candidate
#
# [drive]
# drive.agent_host = ""  # display name for the host segment of minted agent ids (empty hashes the hostname)
# drive.max_starts_per_day = 50  # real starts one dispatch pass may launch per calendar day (plans never count)
#
# [git]
# git.branch = "{type}/{issue}-{slug}"  # branch name template
# git.commits.conventional = true  # enforce Conventional Commits
# git.worktrees = ".worktrees/"  # worktree root, relative to the clone
#
# [prompts]
# prompts.reviewer = ""  # override file for the reviewer prompt (relative to the ops root)
# prompts.triage = ""  # override file for the triage prompt (relative to the ops root)
# prompts.worker = ""  # override file for the worker prompt (relative to the ops root)
#
# [quota]
# quota.low_pct = 90  # usage percent at which a provider reads low
# quota.unknown_blocks = false  # an unknown provider blocks its roles instead of passing them
#
# [registry]
# registry.count_toward_cap = true  # live registered ticket workers take cap slots
# registry.gone_s = 3600  # seconds without a heartbeat before an agent is dropped
# registry.stale_s = 600  # seconds without a heartbeat before an agent reads stale
#
# [review]
# review.required = true  # takeoff needs an approving review record for the head
#
# [tracker]
# tracker.operators = []  # tracker logins treated as operators (default: the repository owner only)
# tracker.repo = ""  # tracker repository as owner/name (default: the clone's origin)
# tracker.variant = "labels"  # tracker source: labels or board (default: labels)
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
# board.item_max_age_s = 300  # seconds before a freshness-gated read re-reads a board row live
# board.poll_pages = 5  # board poll page cap per pass (default: small)
# board.schema_max_age_s = 3600  # seconds before the board step refreshes the cached board schema
#
# [checks]
# checks.no_em_dash = true  # takeoff refuses added lines and bodies with an em-dash
# checks.suite_lock_timeout_s = 3600  # seconds a second takeoff waits for the full-suite lock before giving up (exit 75)
# checks.takeoff = []  # takeoff gate commands, run in order in the worktree
# checks.timeout_s = 1800  # seconds before a takeoff command is killed
#
# [disk]
# disk.min_free_gb = 5  # free gigabytes under which the disk reads low
# disk.min_free_pct = 10  # free-space percent under which the disk reads low
# disk.session_retention_days = 14  # days before a finished ticket's session dir is a reclaim candidate
#
# [drive]
# drive.agent_host = ""  # display name for the host segment of minted agent ids (empty hashes the hostname)
# drive.max_starts_per_day = 50  # real starts one dispatch pass may launch per calendar day (plans never count)
#
# [git]
# git.branch = "{type}/{issue}-{slug}"  # branch name template
# git.worktrees = ".worktrees/"  # worktree root, relative to the clone
#
# [prompts]
# prompts.reviewer = ""  # override file for the reviewer prompt (relative to the ops root)
# prompts.triage = ""  # override file for the triage prompt (relative to the ops root)
# prompts.worker = ""  # override file for the worker prompt (relative to the ops root)
#
# [quota]
# quota.low_pct = 90  # usage percent at which a provider reads low
# quota.unknown_blocks = false  # an unknown provider blocks its roles instead of passing them
#
# [registry]
# registry.count_toward_cap = true  # live registered ticket workers take cap slots
# registry.gone_s = 3600  # seconds without a heartbeat before an agent is dropped
# registry.stale_s = 600  # seconds without a heartbeat before an agent reads stale
#
# [review]
# review.required = true  # takeoff needs an approving review record for the head
#
# [tracker]
# tracker.operators = []  # tracker logins treated as operators (default: the repository owner only)
# tracker.repo = ""  # tracker repository as owner/name (default: the clone's origin)
# tracker.variant = "labels"  # tracker source: labels or board (default: labels)
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
        cfg = tempfile.mkdtemp(prefix="corral-config-")
        self.addCleanup(shutil.rmtree, cfg, True)
        patcher = mock.patch.dict(os.environ, {"CORRAL_CONFIG": cfg})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_dir = cfg
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

    def run_uninit(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["uninit", *argv])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        """`relpath -> bytes` of every file under the root, `.git` skipped."""
        git = os.path.join(self.root, ".git") + os.sep
        found = {}
        for here, _, names in os.walk(self.root):
            if here == git[:-1] or here.startswith(git):
                continue
            for entry in names:
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    found[os.path.relpath(full, self.root)] = handle.read()
        return found

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
        for here, dirnames, names in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for entry in names:
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    before[os.path.relpath(full, self.root)] = handle.read()
        code, _, _ = self.run_cli(self.root)
        self.assertEqual(code, 0)
        after = {}
        for here, dirnames, names in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for entry in names:
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    after[os.path.relpath(full, self.root)] = handle.read()
        added = set(after) - set(before)
        self.assertEqual(
            added,
            {
                "corral.toml",
                "agents.overlay.md",
                os.path.join(".githooks", "commit-msg"),
                os.path.join(".githooks", "pre-push"),
            },
        )
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

    def test_plain_unittest_tree_names_the_usual_command(self):
        self.write("tests/test_app.py", "")
        code, _, err = self.run_cli(self.root)
        self.assertEqual(code, 4)
        self.assertIn("--test-cmd", err)
        self.assertIn('--test-cmd "python3 -m unittest discover -s tests -t ."', err)

    def test_missing_test_command_without_hint_stays_short(self):
        code, _, err = self.run_cli(self.root)
        self.assertEqual(code, 4)
        self.assertIn("no test command detected: pass --test-cmd CMD", err)
        self.assertNotIn("python3 -m unittest", err)

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


class DirectoryNameCase(Fixture):
    name = "mydir"

    def add_origin(self, url="https://github.com/org/other.git"):
        self.git("remote", "add", "origin", url)

    def test_origin_slug_does_not_name_the_registration(self):
        self.add_origin()
        code, _, err = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0, err)
        reg = os.path.join(self.config_dir, "projects.d", "mydir.toml")
        self.assertTrue(os.path.isfile(reg))
        with open(reg, encoding="utf-8") as handle:
            self.assertIn('name = "mydir"', handle.read())
        with open(self.target(), encoding="utf-8") as handle:
            self.assertIn('name = "other"', handle.read())
        self.assertEqual([p.name for p in projects.load_all()], ["mydir"])

    def test_two_clones_of_one_origin_register_as_two_projects(self):
        url = "https://github.com/org/other.git"
        self.add_origin(url)
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        sibling = os.path.join(os.path.dirname(self.root), "sibling")
        os.makedirs(sibling)
        env = clean_env()
        for args in (["init", "-q", "-b", "main"], ["remote", "add", "origin", url]):
            proc = subprocess.run(
                ["git", *args], cwd=sibling, capture_output=True, text=True, env=env
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        code, _, err = self.run_cli("--test-cmd", "make check", sibling)
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in projects.load_all()), ["mydir", "sibling"])


OVERLAY_GOLDEN = """# Agent overlay

How corral's agents are driven. The project's `AGENTS.md` speaks to
everyone, including contributors and their own agents. This file speaks only
to the agents corral launches: worktrees, the review gate, takeoff, the
decision protocol, house preferences, what is private.

This file is yours to edit after `corral init`: a re-run never overwrites
it. In the inline layout it is a tracked file in the project, but still
separate from `AGENTS.md`, so contributors never read corral-only rules.

## Where a new rule goes

The first yes decides:

1. It mentions anything private (the ops side, a client, people, quota, accounts, internal priorities): it goes in the overlay. The leak guard enforces this.
2. It only makes sense because corral runs the agent: it goes in the overlay.
3. A contributor's PR would be rejected for not following it: it goes in the project `AGENTS.md`.
4. Otherwise, it is a preference for these agents only: it goes in the overlay.

When in doubt, put it in the overlay first. A rule that turns out to hold for
everyone is promoted with a PR to the project. If the overlay would
contradict the project's file, one of them is wrong: drop the overlay rule,
or change the project's `AGENTS.md` in a PR.
"""


def registration_golden(name, root, cap=1):
    """The registration text `init` writes, as an independent copy."""
    return (
        "# The host registration for this project, written by `corral init`.\n"
        "# `corral uninit` removes this file while its content still matches\n"
        "# what `corral init` would write; edit it and `uninit` leaves it alone.\n"
        f'name = "{name}"\n'
        f'unit_prefix = "{name}-agent"\n'
        f'root = "{root}"\n'
        "floor = 0\n"
        f"ceiling = {cap}\n"
    )


class OverlayCase(Fixture):
    name = "empty"

    def test_overlay_written_from_the_template(self):
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        with open(os.path.join(self.root, "agents.overlay.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), OVERLAY_GOLDEN)
        self.assertEqual(init.overlay_text(), OVERLAY_GOLDEN)

    def test_overlay_states_the_four_step_test(self):
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        with open(os.path.join(self.root, "agents.overlay.md"), encoding="utf-8") as handle:
            text = handle.read()
        for rule in (
            "anything private",
            "only makes sense because corral runs the agent",
            "rejected for not following it",
            "a preference for these agents only",
        ):
            self.assertIn(rule, text)

    def test_rerun_never_touches_the_overlay(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        overlay = os.path.join(self.root, "agents.overlay.md")
        with open(overlay, "a", encoding="utf-8") as handle:
            handle.write("\nHouse rule: tea first.\n")
        with open(overlay, encoding="utf-8") as handle:
            edited = handle.read()
        code, _, _ = self.run_cli("--test-cmd", "make check", "--force", self.root)
        self.assertEqual(code, 1)
        with open(overlay, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), edited)


class HooksCase(Fixture):
    name = "empty"

    def test_hooks_installed_executable_and_configured(self):
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        for name in ("commit-msg", "pre-push"):
            target = os.path.join(self.root, ".githooks", name)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), init.hook_template(name))
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
        proc = self.git("config", "core.hooksPath")
        self.assertEqual(proc.stdout.strip(), ".githooks")

    def test_rerun_writes_nothing_and_stays_quiet(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        before = self.snapshot()
        code, out, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 1)
        self.assertIn("is up to date", out)
        self.assertNotIn("exec", out)
        self.assertEqual(self.snapshot(), before)

    def test_plan_writes_nothing(self):
        before = self.snapshot()
        code, out, _ = self.run_cli("--plan", "--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        for want in (
            "would write",
            ".githooks/commit-msg",
            ".githooks/pre-push",
            "would set core.hooksPath",
            "agents.overlay.md",
            "projects.d/empty.toml",
        ):
            self.assertIn(want, out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(os.path.join(self.config_dir, "projects.d")))

    def test_rerun_repairs_hook_modes(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        target = os.path.join(self.root, ".githooks", "pre-push")
        os.chmod(target, 0o644)
        before = self.snapshot()
        code, out, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0, out)
        self.assertIn(f"fixed the mode of {target}", out)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
        self.assertEqual(self.snapshot(), before)
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 1)

    def test_a_symlinked_hook_is_blocked_and_never_chmodded(self):
        outside = os.path.join(os.path.dirname(self.root), "outside-hook")
        from corral import init as corral_init

        with open(outside, "wb") as handle:
            handle.write(corral_init.hook_template("pre-push"))
        os.chmod(outside, 0o644)
        os.makedirs(os.path.join(self.root, ".githooks"))
        os.symlink(outside, os.path.join(self.root, ".githooks", "pre-push"))
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 4)
        self.assertEqual(stat.S_IMODE(os.stat(outside).st_mode), 0o644)

    def test_an_unwritable_registration_dir_is_exit_4(self):
        blocker = os.path.join(self.config_dir, "projects.d")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        code, _, err = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 4, err)


class HooksExistingCase(Fixture):
    name = "old"

    def setUp(self):
        super().setUp()
        self.git("config", "core.hooksPath", ".git/hooks")

    def test_foreign_hooks_path_get_chain_instructions(self):
        code, out, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".githooks")))
        for want in ("exec", "commit-msg", "pre-push"):
            self.assertIn(want, out)
        self.assertTrue(os.path.isfile(self.target()))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "agents.overlay.md")))

    def test_rerun_repeats_the_chain_instructions(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        code, out, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 1)
        self.assertIn("exec", out)

    def test_legacy_git_hooks_still_converge(self):
        self.git("config", "--unset", "core.hooksPath")
        hooks = os.path.join(self.root, ".git", "hooks")
        with open(os.path.join(hooks, "pre-commit"), "w") as handle:
            handle.write("#!/bin/sh\n")
        code, out, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".githooks", "pre-push")))
        self.assertNotIn("exec", out)

    def test_partial_install_converges_on_rerun(self):
        self.git("config", "--unset", "core.hooksPath")
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        missing = os.path.join(self.root, ".githooks", "commit-msg")
        os.remove(missing)
        code, out, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0, out)
        with open(missing, "rb") as handle:
            self.assertEqual(handle.read(), init.hook_template("commit-msg"))
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 1)

    def test_blocked_hook_is_reported_never_overwritten(self):
        self.git("config", "--unset", "core.hooksPath")
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        target = os.path.join(self.root, ".githooks", "pre-push")
        with open(target, "ab") as handle:
            handle.write(b"# edited\n")
        code, _, err = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 4)
        self.assertIn(target, err)
        with open(target, "rb") as handle:
            self.assertTrue(handle.read().endswith(b"# edited\n"))

    def test_plan_previews_a_conflicting_hook_file(self):
        self.git("config", "--unset", "core.hooksPath")
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        target = os.path.join(self.root, ".githooks", "pre-push")
        with open(target, "ab") as handle:
            handle.write(b"# edited\n")
        before = self.snapshot()
        with open(os.path.join(self.config_dir, "projects.d", "old.toml"), "rb") as handle:
            reg_before = handle.read()
        code, out, _ = self.run_cli("--plan", "--test-cmd", "make check", self.root)
        self.assertEqual(code, 0, out)
        self.assertIn(f"would keep {target}", out)
        self.assertEqual(self.snapshot(), before)
        with open(os.path.join(self.config_dir, "projects.d", "old.toml"), "rb") as handle:
            self.assertEqual(handle.read(), reg_before)


class ConventionalCase(Fixture):
    name = "empty"
    extra = ("--set", "git.commits.conventional=false")

    def test_false_skips_commit_msg(self):
        code, _, _ = self.run_cli("--test-cmd", "make check", *self.extra, self.root)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".githooks", "pre-push")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".githooks", "commit-msg")))
        code, _, _ = self.run_cli("--test-cmd", "make check", *self.extra, self.root)
        self.assertEqual(code, 1)

    def test_false_chains_only_pre_push(self):
        self.git("config", "core.hooksPath", ".git/hooks")
        code, out, _ = self.run_cli("--test-cmd", "make check", *self.extra, self.root)
        self.assertEqual(code, 0)
        self.assertIn("pre-push", out)
        self.assertNotIn("commit-msg", out)

    def test_false_then_default_installs_commit_msg(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", *self.extra, self.root)[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".githooks", "commit-msg")))
        code, _, _ = self.run_cli("--test-cmd", "make check", "--force", self.root)
        self.assertEqual(code, 0)
        with open(os.path.join(self.root, ".githooks", "commit-msg"), "rb") as handle:
            self.assertEqual(handle.read(), init.hook_template("commit-msg"))
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 1)

    def test_default_then_false_keeps_commit_msg(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        target = os.path.join(self.root, ".githooks", "commit-msg")
        code, out, _ = self.run_cli("--test-cmd", "make check", "--force", *self.extra, self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("kept", out)
        self.assertIn("git.commits.conventional is false", out)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), init.hook_template("commit-msg"))


class RegistrationCase(Fixture):
    name = "empty"

    def reg_target(self):
        return os.path.join(self.config_dir, "projects.d", f"{self.name}.toml")

    def test_registration_golden_and_valid(self):
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        with open(self.reg_target(), encoding="utf-8") as handle:
            self.assertEqual(
                handle.read(), registration_golden(self.name, os.path.realpath(self.root))
            )
        found = projects.load_all()
        self.assertEqual(len(found), 1)
        self.assertEqual(
            (found[0].name, found[0].unit_prefix, found[0].root, found[0].floor, found[0].ceiling),
            (self.name, f"{self.name}-agent-", os.path.realpath(self.root), 0, 1),
        )

    def test_cap_lands_in_the_ceiling(self):
        code, _, _ = self.run_cli("--test-cmd", "make check", "--cap", "3", self.root)
        self.assertEqual(code, 0)
        with open(self.reg_target(), encoding="utf-8") as handle:
            self.assertEqual(
                handle.read(), registration_golden(self.name, os.path.realpath(self.root), 3)
            )

    def test_no_register_skips_with_a_note(self):
        code, out, _ = self.run_cli("--test-cmd", "make check", "--no-register", self.root)
        self.assertEqual(code, 0)
        self.assertIn("skipped the host registration (--no-register)", out)
        self.assertFalse(os.path.exists(self.reg_target()))
        code, out, _ = self.run_cli("--test-cmd", "make check", "--no-register", self.root)
        self.assertEqual(code, 1)
        self.assertIn("skipped the host registration (--no-register)", out)

    def test_same_name_different_content_needs_force(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        with open(self.reg_target(), "a", encoding="utf-8") as handle:
            handle.write("# edited\n")
        code, _, err = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 4)
        self.assertIn(self.reg_target(), err)
        code, _, _ = self.run_cli("--test-cmd", "make check", "--force", self.root)
        self.assertEqual(code, 0)
        with open(self.reg_target(), encoding="utf-8") as handle:
            self.assertEqual(
                handle.read(), registration_golden(self.name, os.path.realpath(self.root))
            )

    def test_conflicting_prefix_is_always_4(self):
        other = os.path.join(self.config_dir, "projects.d", "zeta.toml")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w", encoding="utf-8") as handle:
            handle.write(
                registration_golden("zeta", os.path.realpath(self.root) + "-elsewhere").replace(
                    'unit_prefix = "zeta-agent"', f'unit_prefix = "{self.name}-agent"'
                )
            )
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 4)
        code, _, _ = self.run_cli("--test-cmd", "make check", "--force", self.root)
        self.assertEqual(code, 4)

    def test_conflicting_root_is_always_4(self):
        other = os.path.join(self.config_dir, "projects.d", "zeta.toml")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w", encoding="utf-8") as handle:
            handle.write(registration_golden("zeta", os.path.realpath(self.root)))
        code, _, _ = self.run_cli("--test-cmd", "make check", self.root)
        self.assertEqual(code, 4)
        code, _, _ = self.run_cli("--test-cmd", "make check", "--force", self.root)
        self.assertEqual(code, 4)

    def test_plan_writes_no_registration(self):
        code, _, _ = self.run_cli("--plan", "--test-cmd", "make check", self.root)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.reg_target()))

    def test_plan_previews_a_differing_registration(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        with open(self.reg_target(), "a", encoding="utf-8") as handle:
            handle.write("# edited\n")
        with open(self.reg_target(), encoding="utf-8") as handle:
            edited = handle.read()
        before = self.snapshot()
        code, out, _ = self.run_cli("--plan", "--test-cmd", "make check", self.root)
        self.assertEqual(code, 0, out)
        self.assertIn(f"would write {self.reg_target()}", out)
        self.assertIn("-# edited", out)
        self.assertIn(f"a/{os.path.basename(self.reg_target())}", out)
        self.assertNotIn("a/corral.toml", out)
        self.assertEqual(self.snapshot(), before)
        with open(self.reg_target(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), edited)


class UninitCase(Fixture):
    name = "empty"

    def reg_target(self):
        return os.path.join(self.config_dir, "projects.d", f"{self.name}.toml")

    def hooks_path(self):
        proc = subprocess.run(
            ["git", "config", "core.hooksPath"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def test_init_then_uninit_restores_the_tree(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        with open(self.target(), "rb") as handle:
            toml = handle.read()
        with open(os.path.join(self.root, "agents.overlay.md"), "rb") as handle:
            overlay = handle.read()
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 0, out)
        for rel in (".githooks/commit-msg", ".githooks/pre-push", ".githooks"):
            self.assertFalse(os.path.exists(os.path.join(self.root, rel)), rel)
        self.assertFalse(os.path.exists(self.reg_target()))
        self.assertEqual(self.hooks_path(), "")
        with open(self.target(), "rb") as handle:
            self.assertEqual(handle.read(), toml)
        with open(os.path.join(self.root, "agents.overlay.md"), "rb") as handle:
            self.assertEqual(handle.read(), overlay)
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 1)
        self.assertIn("nothing to remove", out)

    def test_uninit_plan_changes_nothing(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        before = self.snapshot()
        code, out, _ = self.run_uninit("--plan", self.root)
        self.assertEqual(code, 0)
        for want in (
            "would remove",
            ".githooks/commit-msg",
            ".githooks/pre-push",
            "would unset core.hooksPath",
            ".githooks",
            "projects.d/empty.toml",
        ):
            self.assertIn(want, out)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(os.path.isfile(self.reg_target()))
        self.assertEqual(self.hooks_path(), ".githooks")

    def test_uninit_plan_when_clean_is_nothing_to_do(self):
        code, out, _ = self.run_uninit("--plan", self.root)
        self.assertEqual(code, 1)
        self.assertNotIn("would remove", out)

    def test_modified_hook_is_kept(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        with open(os.path.join(self.root, ".githooks", "commit-msg"), "ab") as handle:
            handle.write(b"# edited\n")
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("kept", out)
        self.assertIn("modified since `corral init` wrote it", out)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".githooks", "commit-msg")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".githooks", "pre-push")))
        self.assertFalse(os.path.exists(self.reg_target()))
        self.assertEqual(self.hooks_path(), ".githooks")

    def test_changed_registration_is_kept(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        with open(self.reg_target(), "a", encoding="utf-8") as handle:
            handle.write("# edited\n")
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("differs from what `corral init` would write now", out)
        self.assertTrue(os.path.isfile(self.reg_target()))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".githooks")))
        self.assertEqual(self.hooks_path(), "")

    def test_cap_three_registration_round_trips_through_uninit(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", "--cap", "3", self.root)[0], 0)
        self.assertTrue(os.path.isfile(self.reg_target()))
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.reg_target()))

    def test_a_no_origin_multi_entry_toml_matches_the_directory_named_entry(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", "--cap", "3", self.root)[0], 0)
        directory = os.path.basename(self.root)
        with open(self.target(), "w", encoding="utf-8") as handle:
            handle.write(
                '[[repo]]\nname = "other"\nagents.cap = 1\n\n'
                f'[[repo]]\nname = "{directory}"\nagents.cap = 3\n'
            )
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.reg_target()))

    def test_uninit_without_corral_toml_falls_back_to_the_default(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        os.remove(self.target())
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.reg_target()))

    def test_foreign_hooks_are_kept(self):
        os.makedirs(os.path.join(self.root, ".githooks"), exist_ok=True)
        with open(os.path.join(self.root, ".githooks", "my-hook"), "w") as handle:
            handle.write("#!/bin/sh\n")
        self.git("config", "core.hooksPath", ".githooks")
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 1)
        self.assertIn("holds files `corral init` did not write", out)
        self.assertIn("hooks remain", out)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".githooks", "my-hook")))
        self.assertEqual(self.hooks_path(), ".githooks")

    def test_symlinked_hook_is_kept_not_followed(self):
        self.assertEqual(self.run_cli("--test-cmd", "make check", self.root)[0], 0)
        target = os.path.join(self.root, ".githooks", "commit-msg")
        outside = os.path.join(self.config_dir, "sentinel.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("sentinel\n")
        os.remove(target)
        os.symlink(outside, target)
        code, out, _ = self.run_uninit(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("is a symlink", out)
        self.assertTrue(os.path.islink(target))
        with open(outside, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "sentinel\n")
        remove = init.production_remove(
            os.path.realpath(self.root), os.path.realpath(self.config_dir)
        )
        with self.assertRaises(init.InitError):
            remove(target)
        self.assertTrue(os.path.islink(target))

    def test_remove_seam_is_a_required_argument(self):
        params = inspect.signature(init.run_un).parameters
        self.assertIs(params["remove"].default, inspect.Parameter.empty)

    def test_real_production_remove_contained_to_temp_dir(self):
        remove = init.production_remove(
            os.path.realpath(self.root), os.path.realpath(self.config_dir)
        )
        target = os.path.join(self.root, ".githooks")
        os.makedirs(target)
        remove(target)
        self.assertFalse(os.path.exists(target))
        inner = os.path.join(self.config_dir, "projects.d", "gone.toml")
        os.makedirs(os.path.dirname(inner), exist_ok=True)
        with open(inner, "w") as handle:
            handle.write("x")
        remove(inner)
        self.assertFalse(os.path.exists(inner))
        with self.assertRaises(init.InitError):
            remove(os.path.join(self.root, "..", "outside"))
        with self.assertRaises(init.InitError):
            remove(self.root)


if __name__ == "__main__":
    unittest.main()
