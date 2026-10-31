"""Built-in role prompts, their overrides, and `corral launch --role`.

Core tests use temp ops roots with the environment pointed at temp dirs;
no test touches the real state, config or session dirs, the network, or a
real runtime. Launch tests reuse the fake service manager and real local
clones from test_launch.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral import board, doctor, launchcli, paths, prompts, promptscli, settings
from tests.test_board import reading
from tests.test_doctor import env as doctor_env
from tests.test_launch import OpsCase

FULL = {
    "issue": "7",
    "title": "Add the thing",
    "branch": "feat/7-add-the-thing",
    "worktree": "/tmp/wt/7-add-the-thing",
    "checks.test": "make test",
    "overlay": "Be kind.",
    "project_agents_md": "/tmp/clone/AGENTS.md",
}


def temp_env(tmp):
    """The environment with every corral dir pointed at temp dirs."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORRAL_")}
    env.update(
        CORRAL_STATE=os.path.join(tmp, "state"),
        CORRAL_SESSIONS=os.path.join(tmp, "sessions"),
        CORRAL_CONFIG=os.path.join(tmp, "config"),
        CORRAL_CACHE=os.path.join(tmp, "cache"),
    )
    return env


class PromptsCase(unittest.TestCase):
    """A temp ops root with the environment pointed at temp dirs."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-prompts-"))
        self.root = os.path.join(self.tmp, "ops")
        os.makedirs(self.root)
        self.enterContext(mock.patch.dict(os.environ, temp_env(self.tmp), clear=True))

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_prompts(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = promptscli.run_verb(["prompts", *argv, "--ops", self.root])
        return code, out.getvalue(), err.getvalue()


class RolesTest(PromptsCase):
    def test_roles_outside_worker_reviewer_triage_are_refused(self):
        for fn in (
            lambda: prompts.effective("bogus", self.root),
            lambda: prompts.render("bogus", FULL, ops_root=self.root),
            lambda: prompts.eject("bogus", self.root),
            lambda: prompts.stale("bogus", self.root),
        ):
            with self.assertRaises(prompts.PromptsError) as ctx:
                fn()
            self.assertIn("bogus", str(ctx.exception))
        self.assertEqual(prompts.PromptsError("x").exit_code, 4)

    def test_the_settings_and_the_provider_are_registered(self):
        for role in prompts.ROLES:
            self.assertEqual(settings.REGISTRY[f"prompts.{role}"].default, "")
        self.assertIn("corral.prompts", settings.PROVIDERS)


class PlaceholdersTest(PromptsCase):
    def setUp(self):
        super().setUp()
        self.write("corral.toml", '[prompts]\nworker = "custom.md"\n')
        self.write("custom.md", "ticket {issue}: {title}\n")

    def test_an_unknown_placeholder_names_the_placeholder_and_the_role(self):
        self.write("custom.md", "ticket {issue}: {bogus}\n")
        with self.assertRaisesRegex(prompts.PromptsError, r"\{bogus\}.*worker|worker.*\{bogus\}"):
            prompts.render("worker", FULL, ops_root=self.root)

    def test_a_missing_value_names_the_placeholder_and_the_role(self):
        values = dict(FULL)
        del values["title"]
        with self.assertRaisesRegex(prompts.PromptsError, r"\{title\}.*worker|worker.*\{title\}"):
            prompts.render("worker", values, ops_root=self.root)

    def test_double_braces_are_literal_and_unused_values_are_fine(self):
        self.write("custom.md", "{{issue}} {title} {checks.test}\n")
        out = prompts.render("worker", FULL, ops_root=self.root)
        self.assertEqual(out, "{issue} Add the thing make test\n")
        self.assertEqual(prompts.known_placeholders("{{issue}} {title}"), {"title"})

    def test_a_template_without_placeholders_renders_unchanged(self):
        self.write("custom.md", "no placeholders here\n")
        self.assertEqual(prompts.render("worker", {}, ops_root=self.root), "no placeholders here\n")


class PrecedenceTest(PromptsCase):
    def test_builtin_is_the_default(self):
        text, source = prompts.effective("worker", self.root)
        self.assertEqual(source, "builtin")
        self.assertEqual(text, prompts._builtin_text("worker"))

    def test_file_beats_builtin_and_setting_beats_file(self):
        self.write(os.path.join(".corral", "prompts", "worker.md"), "file text {title}\n")
        text, source = prompts.effective("worker", self.root)
        self.assertEqual((text, source), ("file text {title}\n", "file"))
        self.write("corral.toml", '[prompts]\nworker = "custom.md"\n')
        self.write("custom.md", "setting text {title}\n")
        text, source = prompts.effective("worker", self.root)
        self.assertEqual((text, source), ("setting text {title}\n", "setting"))

    def test_a_setting_path_is_relative_to_the_ops_root(self):
        self.write("corral.toml", '[prompts]\nworker = "sub/custom.md"\n')
        self.write(os.path.join("sub", "custom.md"), "nested {title}\n")
        text, source = prompts.effective("worker", self.root)
        self.assertEqual((text, source), ("nested {title}\n", "setting"))

    def test_a_setting_naming_a_missing_file_is_a_configuration_error(self):
        self.write("corral.toml", '[prompts]\nworker = "missing.md"\n')
        with self.assertRaises(prompts.PromptsError):
            prompts.effective("worker", self.root)

    def test_a_directory_at_the_ejected_path_is_a_configuration_error(self):
        os.makedirs(os.path.join(self.root, ".corral", "prompts", "worker.md"))
        with self.assertRaises(prompts.PromptsError):
            prompts.effective("worker", self.root)


class EjectTest(PromptsCase):
    def test_eject_writes_the_hash_line_then_the_builtin(self):
        path = prompts.eject("worker", self.root)
        self.assertEqual(path, os.path.join(self.root, ".corral", "prompts", "worker.md"))
        with open(path, encoding="utf-8") as handle:
            first, rest = handle.readline().rstrip("\n"), handle.read()
        import hashlib

        digest = hashlib.sha256(prompts._builtin_text("worker").encode("utf-8")).hexdigest()
        self.assertEqual(first, f"<!-- corral-prompt sha256:{digest} -->")
        self.assertEqual(rest, prompts._builtin_text("worker"))
        self.assertFalse(prompts.stale("worker", self.root))

    def test_eject_refuses_to_overwrite_without_force(self):
        prompts.eject("worker", self.root)
        with self.assertRaisesRegex(prompts.PromptsError, "force"):
            prompts.eject("worker", self.root)
        prompts.eject("worker", self.root, force=True)

    def test_stale_is_none_when_not_ejected_or_unmarked(self):
        self.assertIsNone(prompts.stale("worker", self.root))
        self.assertIsNone(prompts.stale("worker", None))
        self.write(os.path.join(".corral", "prompts", "worker.md"), "no marker {title}\n")
        self.assertIsNone(prompts.stale("worker", self.root))

    def test_stale_flips_when_the_builtin_hash_changes(self):
        prompts.eject("worker", self.root)
        with mock.patch.object(prompts, "_builtin_text", return_value="changed\n"):
            self.assertTrue(prompts.stale("worker", self.root))
        self.assertFalse(prompts.stale("worker", self.root))


class CheckTest(PromptsCase):
    def test_clean_when_nothing_is_overridden(self):
        result = prompts.check(self.root)
        self.assertTrue(result.clean)
        code, out, _ = self.run_prompts(["check"])
        self.assertEqual(code, 0)
        self.assertIn("prompts ok", out)

    def test_an_unknown_placeholder_in_an_override_is_invalid(self):
        self.write(os.path.join(".corral", "prompts", "worker.md"), "bad {bogus}\n")
        result = prompts.check(self.root)
        self.assertFalse(result.clean)
        self.assertIn("worker", result.invalid)
        self.assertIn("{bogus}", result.invalid["worker"])
        code, out, _ = self.run_prompts(["check"])
        self.assertEqual(code, 1)
        self.assertIn("worker", out)

    def test_a_stale_ejection_is_reported(self):
        prompts.eject("reviewer", self.root)
        with mock.patch.object(prompts, "_builtin_text", return_value="changed\n"):
            result = prompts.check(self.root)
            self.assertEqual(result.stale, ["reviewer"])
            code, out, _ = self.run_prompts(["check"])
            self.assertEqual(code, 1)
            self.assertIn("reviewer", out)
            self.assertIn("stale", out)

    def test_a_missing_setting_file_is_a_configuration_error(self):
        self.write("corral.toml", '[prompts]\nworker = "missing.md"\n')
        with self.assertRaises(prompts.PromptsError):
            prompts.check(self.root)
        code, _, err = self.run_prompts(["check"])
        self.assertEqual(code, 4)
        self.assertIn("corral:", err)

    def test_show_prints_the_effective_template_unrendered(self):
        code, out, _ = self.run_prompts(["show", "worker"])
        self.assertEqual(code, 0)
        self.assertIn("{title}", out)
        self.assertIn("{overlay}", out)

    def test_eject_prints_the_path_and_needs_force_the_second_time(self):
        code, out, _ = self.run_prompts(["eject", "triage"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), os.path.join(self.root, ".corral", "prompts", "triage.md"))
        code, _, err = self.run_prompts(["eject", "triage"])
        self.assertEqual(code, 4)
        self.assertIn("corral:", err)
        code, _, _ = self.run_prompts(["eject", "triage", "--force"])
        self.assertEqual(code, 0)

    def test_an_unknown_role_is_a_usage_error(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = promptscli.run_verb(["prompts", "show", "bogus", "--ops", self.root])
        self.assertEqual(code, 4)


class BuiltinsTest(PromptsCase):
    def test_each_builtin_renders_with_a_full_value_set(self):
        for role in prompts.ROLES:
            with self.subTest(role=role):
                out = prompts.render(role, FULL, ops_root=self.root)
                self.assertNotIn("{", out)
                self.assertNotIn("}", out)
                self.assertTrue(out.strip())

    def test_the_builtins_contain_no_forbidden_words(self):
        for role in prompts.ROLES:
            with self.subTest(role=role):
                text = prompts._builtin_text(role)
                for bad in ("/" + "home" + "/", "@", "http://", "https://"):
                    self.assertNotIn(bad, text)

    def test_worker_states_the_ticket_rules(self):
        text = prompts._builtin_text("worker")
        for snippet in (
            "{issue}",
            "{title}",
            "{branch}",
            "{worktree}",
            "{checks.test}",
            "{overlay}",
            "{project_agents_md}",
            "Conventional Commits",
            "no trailers",
            "force push",
            "skip hooks",
            "short summary",
            "tests first",
        ):
            self.assertIn(snippet, text)

    def test_reviewer_asks_one_question_with_a_time_budget(self):
        text = prompts._builtin_text("reviewer")
        for snippet in (
            "ONE question",
            "hard limit",
            "three times",
            "30 minutes",
            "read-only",
            "PARTIAL",
            "never edit",
            "{overlay}",
        ):
            self.assertIn(snippet, text)

    def test_triage_proposes_labels_and_never_closes(self):
        text = prompts._builtin_text("triage")
        for snippet in (
            "{issue}",
            "{title}",
            "labels",
            "priority",
            "one question",
            "Never close",
            "never edit its body",
            "{overlay}",
        ):
            self.assertIn(snippet, text)


class DoctorPromptsTest(PromptsCase):
    def rows(self, **overrides):
        return doctor.run_checks(doctor_env(**overrides))

    def prompts_row(self, **overrides):
        return [c for c in self.rows(**overrides) if c.name == "prompts"]

    def test_clean_is_an_ok_row_named_prompts(self):
        (row,) = self.prompts_row()
        self.assertEqual(row.status, "ok")
        self.assertEqual(doctor.exit_code(self.rows()), 0)

    def test_stale_is_a_warn_row(self):
        (row,) = self.prompts_row(prompts_stale=("worker",))
        self.assertEqual(row.status, "warn")
        self.assertIn("worker", row.detail)
        self.assertEqual(doctor.exit_code(self.rows(prompts_stale=("worker",))), 0)

    def test_invalid_is_a_fail_row(self):
        (row,) = self.prompts_row(prompts_invalid=("worker: unknown placeholder {bogus}",))
        self.assertEqual(row.status, "fail")
        self.assertEqual(doctor.exit_code(self.rows(prompts_invalid=("x",))), 1)

    def test_a_prompts_error_is_a_fail_row(self):
        (row,) = self.prompts_row(prompts_error="cannot read the prompts override")
        self.assertEqual(row.status, "fail")


class DoctorGatheringTest(unittest.TestCase):
    def test_lookup_failures_degrade_to_an_error_string(self):
        for error in (
            prompts.PromptsError("bad override"),
            settings.SettingsError("bad settings"),
            doctor.paths.PathError("CORRAL_CONFIG must be an absolute path"),
            OSError("no cwd"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(doctor.prompts, "check", side_effect=error):
                    invalid, stale, message = doctor.load_prompts_state()
                self.assertEqual((invalid, stale), ((), ()))
                self.assertIn(str(error), message or "")


class LaunchRoleTest(OpsCase):
    def run_launch(self, argv, board_fn=None):
        out, err = io.StringIO(), io.StringIO()
        seams = self.system.seams()
        if board_fn is not None:
            seams = seams._replace(board_fn=board_fn)
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = launchcli.run_verb([*argv, "--root", self.root], seams)
        return code, out.getvalue(), err.getvalue()

    def prompt_of(self, repo, number, role="worker"):
        with open(
            os.path.join(paths.newest_session(repo, number, role), "prompt.md"), encoding="utf-8"
        ) as handle:
            return handle.read()

    def plant(self, repo, number, title):
        with board.open_store() as store:
            store.upsert_item(
                reading(repo=repo, number=number, title=title),
                operators=["alice"],
                now=1000.0,
            )

    def test_role_renders_the_board_title_into_prompt_md(self):
        self.plant("lib", 5, "Board title")
        code, out, err = self.run_launch(["launch", "lib", "5", "--role", "worker"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("launched corral-agent-lib-5", out)
        prompt = self.prompt_of("lib", 5)
        workdir = os.path.realpath(
            os.path.join(self.root, "clones", "lib", ".worktrees", "5-board-title")
        )
        self.assertIn("5: Board title", prompt)
        self.assertIn("feat/5-board-title", prompt)
        self.assertIn(workdir, prompt)
        self.assertIn("AGENTS.md (none found)", prompt)
        self.assertNotIn("{", prompt)
        self.assertNotIn("}", prompt)

    def test_a_bad_override_fails_before_any_worktree_exists(self):
        override = os.path.join(self.root, ".corral", "prompts", "worker.md")
        os.makedirs(os.path.dirname(override))
        with open(override, "w", encoding="utf-8") as handle:
            handle.write("{bogus}\n")
        code, _, err = self.run_launch(["launch", "lib", "7", "--role", "worker", "--title", "x"])
        self.assertEqual(code, 4, err)
        worktrees_dir = os.path.join(self.root, "clones", "lib", ".worktrees")
        self.assertFalse(os.path.isdir(worktrees_dir) and os.listdir(worktrees_dir))

    def test_role_falls_back_to_the_title_flag(self):
        code, _, err = self.run_launch(
            ["launch", "lib", "6", "--role", "worker", "--title", "feat: flag title"]
        )
        self.assertEqual((code, err), (0, ""))
        prompt = self.prompt_of("lib", 6)
        self.assertIn("6: feat: flag title", prompt)
        self.assertIn("feat/6-flag-title", prompt)

    def test_role_uses_a_board_item_passed_through_the_seam(self):
        item = reading(repo="lib", number=8, title="Seam title")
        code, _, err = self.run_launch(
            ["launch", "lib", "8", "--role", "reviewer"],
            board_fn=lambda repo, number: item,
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Seam title", self.prompt_of("lib", 8, role="reviewer"))

    def test_role_without_a_title_names_the_flag(self):
        code, _, err = self.run_launch(["launch", "lib", "7", "--role", "worker"])
        self.assertEqual(code, 4)
        self.assertIn("--title", err)

    def test_role_and_prompt_file_are_mutually_exclusive(self):
        prompt_file = os.path.join(self.tmp, "prompt.md")
        with open(prompt_file, "w", encoding="utf-8") as handle:
            handle.write("do the thing")
        argv = ["launch", "lib", "9", "--role", "worker", "--prompt-file", prompt_file]
        code, _, err = self.run_launch(argv)
        self.assertEqual(code, 4)
        self.assertIn("--role", err)
        self.assertIn("--prompt-file", err)

    def test_neither_role_nor_prompt_file_is_a_usage_error(self):
        code, _, err = self.run_launch(["launch", "lib", "9", "--title", "feat: x"])
        self.assertEqual(code, 4)

    def test_the_launch_verb_is_registered_on_the_corral_command(self):
        from corral import cli

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["launch", "--help"]), 0)
            for verb in ("prompts",):
                self.assertEqual(cli.main([verb, "--help"]), 0, verb)


if __name__ == "__main__":
    unittest.main()
