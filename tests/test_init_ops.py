"""`corral init --ops` and `corral uninit --ops`: the split layout.

The ops root holds the agent side (`corral.toml`, the overlay, the hooks,
the host registration); the project clone is touched only through local,
untracked git state (`core.hooksPath`, `.git/info/exclude`).
"""

import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, init, projects
from corral.gitenv import clean_env
from tests.test_init import OVERLAY_GOLDEN, TAIL, registration_golden


def ops_golden(name, clone_root, test_cmd="make check", branch="main"):
    """The fresh-ops `corral.toml` text, as an independent copy."""
    return (
        "# corral.toml, written by `corral init`. Settings that differ\n"
        "# from the defaults live in [[repo]]; everything else is\n"
        "# commented out below with its default.\n"
        "[[repo]]\n"
        f'name = "{name}"\n'
        "\n"
        "[repo.checks]\n"
        f'test = "{test_cmd}"\n'
        "\n"
        "[repo.clone]\n"
        f'default_branch = "{branch}"\n'
        f'path = "{clone_root}"\n'
        "\n"
    ) + TAIL


class OpsFixture(unittest.TestCase):
    """One real local clone plus a missing ops sibling."""

    clone_name = "proj"
    files = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = tmp.name
        self.clone = os.path.join(self.work, self.clone_name)
        os.makedirs(self.clone)
        cfg = tempfile.mkdtemp(prefix="corral-config-")
        self.addCleanup(shutil.rmtree, cfg, True)
        patcher = mock.patch.dict(os.environ, {"CORRAL_CONFIG": cfg})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_dir = cfg
        self.ops = os.path.join(self.work, f"{self.clone_name}_ops")
        self.make_clone(self.clone, self.files)

    def make_clone(self, root, files):
        os.makedirs(root, exist_ok=True)
        self.git("init", "-q", "-b", "main", cwd=root)
        self.git("config", "user.name", "t", cwd=root)
        self.git("config", "user.email", "t@t", cwd=root)
        for rel, text in files.items():
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(text)
        with open(os.path.join(root, "seed.txt"), "w", encoding="utf-8") as handle:
            handle.write("seed\n")
        self.git("add", "-A", cwd=root)
        self.git("commit", "-qm", "seed", cwd=root)

    def git(self, *args, cwd=None):
        proc = subprocess.run(
            ["git", *args],
            cwd=self.clone if cwd is None else cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def status(self):
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.clone,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def hooks_path(self):
        proc = subprocess.run(
            ["git", "config", "core.hooksPath"],
            cwd=self.clone,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def clone_snapshot(self):
        """`relpath -> bytes` of the working tree, `.git` skipped."""
        git = os.path.join(self.clone, ".git") + os.sep
        found = {}
        for here, dirnames, names in os.walk(self.clone):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if here == git[:-1] or here.startswith(git):
                continue
            for entry in names:
                full = os.path.join(here, entry)
                with open(full, "rb") as handle:
                    found[os.path.relpath(full, self.clone)] = handle.read()
        return found

    def ops_toml(self):
        with open(os.path.join(self.ops, "corral.toml"), encoding="utf-8") as handle:
            return handle.read()


class FreshOpsCase(OpsFixture):
    def test_golden_toml_overlay_hooks_and_registration(self):
        code, out, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ops_toml(), ops_golden("proj", os.path.realpath(self.clone)))
        with open(os.path.join(self.ops, "agents.overlay.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), OVERLAY_GOLDEN)
        self.assertEqual(init.overlay_text(), OVERLAY_GOLDEN)
        for name in ("commit-msg", "pre-push"):
            target = os.path.join(self.ops, "hooks", "proj", name)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), init.hook_template(name))
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
        self.assertEqual(self.hooks_path(), os.path.join(self.ops, "hooks", "proj"))
        with open(os.path.join(self.clone, ".git", "info", "exclude"), encoding="utf-8") as handle:
            self.assertIn(".worktrees/\n", handle.read().splitlines(keepends=True))
        reg = os.path.join(self.config_dir, "projects.d", "proj_ops.toml")
        with open(reg, encoding="utf-8") as handle:
            self.assertEqual(
                handle.read(), registration_golden("proj_ops", os.path.realpath(self.ops))
            )
        self.assertIn("next: corral github setup", out)
        found = projects.load_all()
        self.assertEqual(
            [(p.name, p.unit_prefix, p.root, p.floor, p.ceiling) for p in found],
            [("proj_ops", "proj_ops-agent", os.path.realpath(self.ops), 0, 1)],
        )

    def test_ops_root_created_with_git_init(self):
        self.assertFalse(os.path.exists(self.ops))
        code, _, _ = self.run_cli("init", "--ops", self.ops, "--test-cmd", "make check", self.clone)
        self.assertEqual(code, 0)
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.ops,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(os.path.realpath(proc.stdout.strip()), os.path.realpath(self.ops))

    def test_detection_runs_against_the_clone(self):
        self.git("remote", "add", "origin", "https://github.com/owner/name.git")
        code, _, _ = self.run_cli("init", "--ops", self.ops, "--test-cmd", "make check", self.clone)
        self.assertEqual(code, 0)
        self.assertIn('name = "name"\n', self.ops_toml())
        self.assertTrue(os.path.isfile(os.path.join(self.ops, "hooks", "name", "pre-push")))
        self.assertEqual(self.hooks_path(), os.path.join(self.ops, "hooks", "name"))

    def test_test_command_detected_from_the_clone(self):
        with open(os.path.join(self.clone, "Makefile"), "w", encoding="utf-8") as handle:
            handle.write("test:\n\ttrue\n")
        code, _, _ = self.run_cli("init", "--ops", self.ops, self.clone)
        self.assertEqual(code, 0)
        self.assertIn('test = "make test"\n', self.ops_toml())

    def test_bare_ops_uses_the_sibling(self):
        code, _, _ = self.run_cli("init", "--ops", "--test-cmd", "make check", self.clone)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.ops, "corral.toml")))

    def test_ops_name_is_a_sibling(self):
        custom = os.path.join(self.work, "custom")
        code, _, _ = self.run_cli(
            "init", "--ops-name", "custom", "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(custom, "corral.toml")))
        reg = os.path.join(self.config_dir, "projects.d", "custom.toml")
        with open(reg, encoding="utf-8") as handle:
            self.assertIn(f'root = "{os.path.realpath(custom)}"\n', handle.read())

    def test_ops_path_wins_over_ops_name(self):
        other = os.path.join(self.work, "other")
        code, _, _ = self.run_cli(
            "init",
            "--ops",
            other,
            "--ops-name",
            "custom",
            "--test-cmd",
            "make check",
            self.clone,
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(other, "corral.toml")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "custom")))

    def test_plan_writes_nothing(self):
        before = self.clone_snapshot()
        code, out, _ = self.run_cli(
            "init", "--plan", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 0, out)
        for want in (
            "would create",
            "would run git init",
            "agents.overlay.md",
            "hooks/proj/commit-msg",
            "hooks/proj/pre-push",
            "would set core.hooksPath",
            "would add .worktrees/",
            "projects.d/proj_ops.toml",
            "next: corral github setup",
        ):
            self.assertIn(want, out, want)
        self.assertFalse(os.path.exists(self.ops))
        self.assertFalse(os.path.exists(os.path.join(self.config_dir, "projects.d")))
        self.assertEqual(self.clone_snapshot(), before)
        self.assertEqual(self.status(), "")
        self.assertEqual(self.hooks_path(), "")

    def test_clone_working_tree_untouched(self):
        before = self.clone_snapshot()
        code, _, _ = self.run_cli("init", "--ops", self.ops, "--test-cmd", "make check", self.clone)
        self.assertEqual(code, 0)
        self.assertEqual(self.clone_snapshot(), before)
        self.assertEqual(self.status(), "")
        self.assertEqual(self.hooks_path(), os.path.join(self.ops, "hooks", "proj"))

    def test_rerun_is_nothing_to_do(self):
        self.assertEqual(
            self.run_cli("init", "--ops", self.ops, "--test-cmd", "make check", self.clone)[0], 0
        )
        code, out, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 1)
        self.assertIn("is up to date", out)

    def test_cap_lands_in_entry_and_registration(self):
        code, _, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", "--cap", "3", self.clone
        )
        self.assertEqual(code, 0)
        self.assertIn("cap = 3", self.ops_toml())
        reg = os.path.join(self.config_dir, "projects.d", "proj_ops.toml")
        with open(reg, encoding="utf-8") as handle:
            self.assertEqual(
                handle.read(), registration_golden("proj_ops", os.path.realpath(self.ops), 3)
            )

    def test_comments_only_ops_toml_gets_entry_appended(self):
        os.makedirs(self.ops)
        self.git("init", "-q", "-b", "main", cwd=self.ops)
        with open(os.path.join(self.ops, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write("# hand-kept header\n")
        code, out, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 0, out)
        text = self.ops_toml()
        self.assertTrue(text.startswith("# hand-kept header\n"))
        self.assertIn("\n[[repo]]\n", text)
        self.assertIn('name = "proj"\n', text)

    def test_empty_ops_dir_plan_reports_git_init(self):
        os.makedirs(self.ops)
        code, out, _ = self.run_cli(
            "init", "--plan", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 0, out)
        self.assertIn("would run git init", out)
        self.assertNotIn("would create", out)
        self.assertFalse(os.path.exists(os.path.join(self.ops, ".git")))
        self.assertEqual(self.hooks_path(), "")

    def test_empty_ops_dir_apply_initialises(self):
        os.makedirs(self.ops)
        code, out, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 0, out)
        self.assertIn("initialised", out)
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.ops,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        self.assertEqual(proc.returncode, 0)


class OpsErrorsCase(OpsFixture):
    def test_nonempty_nonrepo_ops_root_is_4(self):
        os.makedirs(self.ops)
        with open(os.path.join(self.ops, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("mine\n")
        code, _, err = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 4)
        self.assertIn(self.ops, err)

    def test_ops_path_holding_a_file_is_4(self):
        with open(self.ops, "w", encoding="utf-8") as handle:
            handle.write("mine\n")
        code, _, err = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 4)
        self.assertIn(self.ops, err)

    def test_foreign_hooks_path_prints_chain_and_writes_no_hooks(self):
        self.git("config", "core.hooksPath", ".git/hooks")
        code, out, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.ops, "hooks")))
        self.assertEqual(self.hooks_path(), ".git/hooks")
        for want in ("exec", "commit-msg", "pre-push"):
            self.assertIn(want, out)
        self.assertTrue(os.path.isfile(os.path.join(self.ops, "corral.toml")))
        self.assertTrue(os.path.isfile(os.path.join(self.ops, "agents.overlay.md")))
        code, out, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 1)
        self.assertIn("exec", out)

    def test_conflicting_entry_is_4_even_with_force(self):
        self.assertEqual(
            self.run_cli("init", "--ops", self.ops, "--test-cmd", "make check", self.clone)[0], 0
        )
        before = self.ops_toml()
        code, out, err = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "other", "--force", self.clone
        )
        self.assertEqual(code, 4)
        self.assertIn("corral.toml", err)
        self.assertIn("-test = ", out)
        self.assertIn("+test = ", out)
        self.assertEqual(self.ops_toml(), before)

    def test_dropping_cap_is_4_even_with_force(self):
        self.assertEqual(
            self.run_cli(
                "init",
                "--ops",
                self.ops,
                "--test-cmd",
                "make check",
                "--cap",
                "3",
                "--no-register",
                self.clone,
            )[0],
            0,
        )
        before = self.ops_toml()
        for extra in ([], ["--force"]):
            code, _, err = self.run_cli(
                "init",
                "--ops",
                self.ops,
                "--test-cmd",
                "make check",
                "--no-register",
                *extra,
                self.clone,
            )
            self.assertEqual(code, 4)
            self.assertIn("corral.toml", err)
        self.assertEqual(self.ops_toml(), before)

    def test_dropping_set_value_is_4(self):
        self.assertEqual(
            self.run_cli(
                "init",
                "--ops",
                self.ops,
                "--test-cmd",
                "make check",
                "--set",
                "agents.cap=3",
                "--no-register",
                self.clone,
            )[0],
            0,
        )
        code, _, err = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", "--no-register", self.clone
        )
        self.assertEqual(code, 4)
        self.assertIn("corral.toml", err)

    def test_dropping_runtime_is_4(self):
        self.assertEqual(
            self.run_cli(
                "init",
                "--ops",
                self.ops,
                "--test-cmd",
                "make check",
                "--runtime",
                "opencode",
                "--no-register",
                self.clone,
            )[0],
            0,
        )
        code, _, err = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", "--no-register", self.clone
        )
        self.assertEqual(code, 4)
        self.assertIn("corral.toml", err)

    def test_init_refuses_second_name_for_same_clone(self):
        self.assertEqual(
            self.run_cli("init", "--ops", self.ops, "--test-cmd", "make check", self.clone)[0], 0
        )
        self.git("remote", "add", "origin", "https://github.com/owner/renamed.git")
        before = self.ops_toml()
        code, _, err = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", self.clone
        )
        self.assertEqual(code, 4)
        self.assertIn("already registered as 'proj'", err)
        self.assertEqual(self.ops_toml(), before)


class SecondCloneCase(OpsFixture):
    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.work, "web")
        self.make_clone(self.other, {"package.json": '{"scripts": {"test": "node x"}}'})

    def init_both(self, *extra):
        code, _, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", *extra, self.clone
        )
        self.assertEqual(code, 0)
        with open(os.path.join(self.ops, "corral.toml"), "a", encoding="utf-8") as handle:
            handle.write("# hand-kept comment\n")
        with open(os.path.join(self.ops, "corral.toml"), encoding="utf-8") as handle:
            first = handle.read()
        code, out, _ = self.run_cli("init", "--ops", self.ops, "--test-cmd", "npm test", self.other)
        self.assertEqual(code, 0, out)
        return first, out

    def test_second_clone_appends_and_preserves_bytes(self):
        first, _ = self.init_both()
        text = self.ops_toml()
        self.assertTrue(text.startswith(first))
        self.assertIn("# hand-kept comment\n", text)
        self.assertEqual(text.count("\n[[repo]]\n"), 2)
        self.assertIn('name = "proj"\n', text)
        self.assertIn('name = "web"\n', text)
        self.assertIn(f'path = "{os.path.realpath(self.other)}"\n', text)
        self.assertIn('test = "npm test"\n', text)
        self.assertTrue(os.path.isfile(os.path.join(self.ops, "hooks", "web", "pre-push")))
        self.assertTrue(os.path.isfile(os.path.join(self.ops, "hooks", "proj", "pre-push")))
        with open(os.path.join(self.other, ".git", "info", "exclude"), encoding="utf-8") as handle:
            self.assertIn(".worktrees/", handle.read())

    def test_registration_already_identical_is_a_noop(self):
        _, out = self.init_both()
        self.assertNotIn("proj_ops.toml", out)
        reg = os.path.join(self.config_dir, "projects.d", "proj_ops.toml")
        with open(reg, encoding="utf-8") as handle:
            self.assertEqual(
                handle.read(), registration_golden("proj_ops", os.path.realpath(self.ops))
            )

    def test_identical_second_entry_is_a_noop(self):
        self.init_both()
        code, out, _ = self.run_cli("init", "--ops", self.ops, "--test-cmd", "npm test", self.other)
        self.assertEqual(code, 1)
        self.assertIn("is up to date", out)

    def test_same_name_different_clone_is_4(self):
        self.init_both()
        sibling = os.path.join(self.work, "sibling")
        self.make_clone(sibling, {})
        self.git("remote", "add", "origin", "https://github.com/owner/proj.git", cwd=self.clone)
        self.git("remote", "add", "origin", "https://github.com/owner/proj.git", cwd=sibling)
        code, _, err = self.run_cli("init", "--ops", self.ops, "--test-cmd", "make check", sibling)
        self.assertEqual(code, 4)
        self.assertIn("corral.toml", err)


class UninitOpsCase(OpsFixture):
    def init(self, *extra):
        code, out, _ = self.run_cli(
            "init", "--ops", self.ops, "--test-cmd", "make check", *extra, self.clone
        )
        self.assertEqual(code, 0, out)

    def uninit(self, *extra):
        return self.run_cli("uninit", *extra)

    def test_uninit_restores_clone_and_keeps_ops_side(self):
        self.init()
        toml = self.ops_toml()
        with open(os.path.join(self.ops, "agents.overlay.md"), encoding="utf-8") as handle:
            overlay = handle.read()
        reg_target = os.path.join(self.config_dir, "projects.d", "proj_ops.toml")
        with open(reg_target, encoding="utf-8") as handle:
            reg = handle.read()
        exclude = os.path.join(self.clone, ".git", "info", "exclude")
        with open(exclude, "a", encoding="utf-8") as handle:
            handle.write("# mine\n")
        code, out, _ = self.uninit("--ops", self.ops, self.clone)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.ops, "hooks", "proj")))
        self.assertEqual(self.hooks_path(), "")
        self.assertEqual(self.status(), "")
        self.assertEqual(self.ops_toml(), toml)
        with open(os.path.join(self.ops, "agents.overlay.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), overlay)
        with open(reg_target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), reg)
        self.assertIn("shared by the ops root", out)
        self.assertIn("left for you to delete", out)
        code, out, _ = self.uninit("--ops", self.ops, self.clone)
        self.assertEqual(code, 1)
        self.assertIn("nothing to remove", out)

    def test_uninit_plan_changes_nothing(self):
        self.init()
        code, out, _ = self.uninit("--plan", "--ops", self.ops, self.clone)
        self.assertEqual(code, 0, out)
        for want in (
            "would remove",
            "hooks/proj/commit-msg",
            "hooks/proj/pre-push",
            "would unset core.hooksPath",
            "would remove the .worktrees/ line from",
            "would keep",
            "shared by the ops root",
        ):
            self.assertIn(want, out, want)

    def test_uninit_plan_when_clean_is_nothing_to_do(self):
        self.init()
        code, _, _ = self.uninit("--ops", self.ops, self.clone)
        self.assertEqual(code, 0)
        code, out, _ = self.uninit("--plan", "--ops", self.ops, self.clone)
        self.assertEqual(code, 1, out)
        self.assertNotIn("would remove", out)
        self.assertIn("shared by the ops root", out)

    def test_modified_hook_is_kept_and_hooks_path_stays(self):
        self.init()
        target = os.path.join(self.ops, "hooks", "proj", "commit-msg")
        with open(target, "ab") as handle:
            handle.write(b"# edited\n")
        code, out, _ = self.uninit("--ops", self.ops, self.clone)
        self.assertEqual(code, 0, out)
        self.assertIn("modified since `corral init` wrote it", out)
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(os.path.join(self.ops, "hooks", "proj", "pre-push")))
        self.assertEqual(self.hooks_path(), os.path.join(self.ops, "hooks", "proj"))

    def test_symlinked_hook_is_kept_not_followed(self):
        self.init()
        target = os.path.join(self.ops, "hooks", "proj", "commit-msg")
        outside = os.path.join(self.config_dir, "sentinel.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("sentinel\n")
        os.remove(target)
        os.symlink(outside, target)
        code, out, _ = self.uninit("--ops", self.ops, self.clone)
        self.assertEqual(code, 0, out)
        self.assertIn("is a symlink", out)
        self.assertTrue(os.path.islink(target))
        with open(outside, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "sentinel\n")
        remove = init.production_remove(
            os.path.realpath(self.clone),
            os.path.realpath(self.ops),
            os.path.realpath(self.config_dir),
        )
        with self.assertRaises(init.InitError):
            remove(target)

    def test_uninit_missing_ops_root_is_4(self):
        code, _, err = self.uninit("--ops", self.ops, self.clone)
        self.assertEqual(code, 4)
        self.assertIn(self.ops, err)

    def test_uninit_finds_hooks_after_remote_rename(self):
        self.init()
        self.git("remote", "add", "origin", "https://github.com/owner/renamed.git")
        code, out, _ = self.uninit("--ops", self.ops, self.clone)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.ops, "hooks", "proj")))
        self.assertFalse(os.path.exists(os.path.join(self.ops, "hooks", "renamed")))
        self.assertEqual(self.hooks_path(), "")
        self.assertEqual(self.status(), "")


if __name__ == "__main__":
    unittest.main()
