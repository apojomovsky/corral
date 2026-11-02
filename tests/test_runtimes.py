"""Runtime adapters: argv, env, per-launch configs, sessions and the record.

Offline: every session dir is a temp dir with fake files, and the binaries
are fixed by patching `binary()`. No runtime binary is ever run, and no
home config is read or written.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from corral import models, profiles, runtimes as rt
from corral import settings
from corral.runtimes import muse, omp

PROMPT = "do the thing"
MODEL = "provider-a/model-1"


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-runtimes-"))
        self.sdir = os.path.join(self.root, "session")
        os.makedirs(self.sdir)

    def write(self, name, text):
        path = os.path.join(self.sdir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class Get(unittest.TestCase):
    def test_the_two_runtimes_resolve_by_name(self):
        self.assertEqual(rt.get("omp").name, "omp")
        self.assertEqual(rt.get("muse").name, "muse")

    def test_anything_else_is_an_unknown_runtime(self):
        for name in ("claude-code", "", "OMP"):
            with self.assertRaises(rt.UnknownRuntime, msg=name):
                rt.get(name)
        self.assertTrue(issubclass(rt.UnknownRuntime, ValueError))


class RemovedOpencode(unittest.TestCase):
    """Every config that names opencode is refused with the removal message (exit 4)."""

    def test_opencode_is_refused_naming_its_removal(self):
        with self.assertRaises(rt.UnknownRuntime) as ctx:
            rt.get("opencode")
        self.assertEqual(str(ctx.exception), rt.REMOVED_MESSAGE)
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.validate(
                {"slim": {"runtime": "opencode", "models": {}, "options": {}}}, ["provider-a"]
            )
        self.assertIn(rt.REMOVED_MESSAGE, str(ctx.exception))
        for pool in (["provider-a"], ["provider-a", "opencode"]):
            with self.subTest(pool=pool), self.assertRaises(models.ModelsError) as ctx:
                models.validate({"default": ["opencode/model-x"]}, pool)
            self.assertIn(rt.REMOVED_MESSAGE, str(ctx.exception))


class Record(SessionCase):
    def test_an_unrecorded_session_reads_as_omp(self):
        self.assertEqual(rt.recorded(self.sdir), "omp")

    def test_the_recorded_runtime_is_read_back(self):
        rt.record(self.sdir, "muse")
        self.assertEqual(rt.recorded(self.sdir), "muse")
        with open(os.path.join(self.sdir, "runtime"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "muse\n")

    def test_an_empty_record_reads_as_omp(self):
        self.write("runtime", "\n")
        self.assertEqual(rt.recorded(self.sdir), "omp")

    def test_an_unknown_runtime_is_refused_and_nothing_is_written(self):
        fresh = os.path.join(self.root, "fresh")
        with self.assertRaises(rt.UnknownRuntime):
            rt.record(fresh, "claude-code")
        self.assertFalse(os.path.exists(fresh))


class BinaryResolution(unittest.TestCase):
    def setUp(self):
        omp.binary.cache_clear()
        muse.binary.cache_clear()
        self.addCleanup(omp.binary.cache_clear)
        self.addCleanup(muse.binary.cache_clear)

    def test_the_binary_on_path_wins(self):
        with mock.patch("corral.runtimes.omp.shutil.which", return_value="/opt/bin/omp"):
            self.assertEqual(omp.binary(), "/opt/bin/omp")

    def test_without_one_on_path_the_usual_install_location_is_used(self):
        with mock.patch("corral.runtimes.omp.shutil.which", return_value=None):
            self.assertEqual(omp.binary(), os.path.expanduser("~/.local/bin/omp"))
        with mock.patch("corral.runtimes.muse.shutil.which", return_value=None):
            self.assertEqual(muse.binary(), os.path.expanduser("~/.local/bin/muse"))

    def test_each_binary_is_found_once(self):
        with mock.patch("corral.runtimes.omp.shutil.which", return_value="/opt/bin/omp") as which:
            omp.binary()
            omp.binary()
        self.assertEqual(which.call_count, 1)


class OmpArgv(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(omp, "binary", return_value="/fake/omp")
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_launch_argv_is_the_headless_form(self):
        self.assertEqual(
            omp.RUNTIME.argv("/s", PROMPT),
            [
                "/fake/omp",
                "-p",
                "--session-dir",
                "/s",
                "--max-time",
                rt.DEFAULT_MAX_TIME,
                "--config",
                os.path.join("/s", "overlay.yml"),
                "--auto-approve",
                PROMPT,
            ],
        )

    def test_resume_and_model_come_before_the_prompt(self):
        self.assertEqual(
            omp.RUNTIME.argv("/s", PROMPT, model=MODEL, resume="/s/a.jsonl", max_time="30m"),
            [
                "/fake/omp",
                "-p",
                "--session-dir",
                "/s",
                "--max-time",
                "30m",
                "--config",
                os.path.join("/s", "overlay.yml"),
                "--auto-approve",
                "-r",
                "/s/a.jsonl",
                "--model",
                MODEL,
                PROMPT,
            ],
        )

    def test_omp_needs_no_extra_environment(self):
        self.assertEqual(omp.RUNTIME.env("/s"), {})

    def test_reviewer_is_a_documented_no_op(self):
        self.assertEqual(
            omp.RUNTIME.argv("/s", PROMPT, reviewer=True),
            omp.RUNTIME.argv("/s", PROMPT),
        )


class OmpOverlay(SessionCase):
    CONFIG = {
        "modelRoles": {"default": MODEL, "judge": "provider-b/model-2"},
        "retry": {"fallbackChains": {"default": ["provider-b/model-2"]}},
        "task": {"agentModelOverrides": {"reviewer": "provider-b/model-2"}},
    }

    def read_overlay(self):
        with open(os.path.join(self.sdir, "overlay.yml"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_overlay_is_written_by_hand_with_headless_keys_forced(self):
        omp.RUNTIME.write_config(self.sdir, self.CONFIG)
        self.assertEqual(
            self.read_overlay(),
            "modelRoles:\n"
            '  default: "provider-a/model-1"\n'
            '  judge: "provider-b/model-2"\n'
            "retry:\n"
            "  fallbackChains:\n"
            "    default:\n"
            '      - "provider-b/model-2"\n'
            "task:\n"
            "  agentModelOverrides:\n"
            '    reviewer: "provider-b/model-2"\n'
            "bash:\n"
            "  autoBackground:\n"
            "    enabled: false\n"
            "async:\n"
            "  enabled: false\n",
        )

    def test_a_caller_cannot_turn_background_jobs_back_on(self):
        omp.RUNTIME.write_config(
            self.sdir, {"bash": {"autoBackground": {"enabled": True}, "keep": 1}}
        )
        text = self.read_overlay()
        self.assertIn("    enabled: false\n", text)
        self.assertNotIn("true", text)
        self.assertIn("  keep: 1\n", text)

    def test_strings_are_quoted_so_model_ids_read_back_as_strings(self):
        omp.RUNTIME.write_config(self.sdir, {"modelRoles": {"default": 'a:"b"'}})
        self.assertIn('  default: "a:\\"b\\""\n', self.read_overlay())

    def test_a_value_yaml_cannot_express_is_refused(self):
        with self.assertRaises(TypeError):
            omp.overlay_text({"modelRoles": {"default": object()}})

    def test_the_config_goes_into_the_session_dir_only(self):
        omp.RUNTIME.write_config(self.sdir, self.CONFIG)
        self.assertEqual(os.listdir(self.sdir), ["overlay.yml"])


class OmpSessions(SessionCase):
    def test_there_is_no_resume_handle_without_a_session(self):
        self.assertIsNone(omp.RUNTIME.resume_handle(self.sdir))
        self.assertIsNone(omp.RUNTIME.resume_handle(os.path.join(self.root, "missing")))
        self.assertIsNone(omp.RUNTIME.activity_file(self.sdir))

    def test_the_newest_jsonl_is_the_handle_and_the_activity_file(self):
        old = self.write("old.jsonl", "{}\n")
        new = self.write("new.jsonl", "{}\n")
        self.write("notes.txt", "x")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.assertEqual(omp.RUNTIME.resume_handle(self.sdir), os.path.abspath(new))
        self.assertEqual(omp.RUNTIME.activity_file(self.sdir), os.path.abspath(new))


class Adapters(unittest.TestCase):
    def test_each_adapter_names_its_runtime(self):
        self.assertEqual(omp.RUNTIME.name, "omp")
        self.assertEqual(muse.RUNTIME.name, "muse")

    def test_an_unrecorded_session_defaults_to_omp(self):
        self.assertEqual(rt.DEFAULT_RUNTIME, "omp")

    def test_each_runtime_covers_the_section_6_checklist_shape(self):
        for adapter in (omp.RUNTIME, muse.RUNTIME):
            with self.subTest(runtime=adapter.name):
                for method in (
                    "argv",
                    "env",
                    "write_config",
                    "with_models",
                    "resume_handle",
                    "activity_file",
                    "options_schema",
                ):
                    self.assertTrue(callable(getattr(adapter, method)), method)


class OmpOptionsSchema(unittest.TestCase):
    def test_the_schema_is_pinned(self):
        self.assertEqual(
            omp.RUNTIME.options_schema(),
            {"auto_approve": (bool, "whether to pass --auto-approve for the headless run")},
        )

    def test_a_second_call_returns_the_same_shape(self):
        first = omp.RUNTIME.options_schema()
        first["auto_approve"] = "mutated"
        self.assertEqual(
            omp.RUNTIME.options_schema(),
            {"auto_approve": (bool, "whether to pass --auto-approve for the headless run")},
        )


class OmpApprovalOption(SessionCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(omp, "binary", return_value="/fake/omp")
        patch.start()
        self.addCleanup(patch.stop)

    def test_auto_approve_defaults_on(self):
        self.assertIn("--auto-approve", omp.RUNTIME.argv(self.sdir, PROMPT))

    def test_setting_auto_approve_keeps_the_flag_and_the_overlay(self):
        omp.RUNTIME.write_config(self.sdir, {"auto_approve": True})
        with open(os.path.join(self.sdir, "overlay.yml"), encoding="utf-8") as handle:
            self.assertNotIn("auto_approve", handle.read())
        with open(os.path.join(self.sdir, "options.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"auto_approve": True})
        self.assertIn("--auto-approve", omp.RUNTIME.argv(self.sdir, PROMPT))

    def test_clearing_auto_approve_drops_the_flag(self):
        omp.RUNTIME.write_config(self.sdir, {"auto_approve": False})
        argv = omp.RUNTIME.argv(self.sdir, PROMPT)
        self.assertNotIn("--auto-approve", argv)
        self.assertEqual(argv[-1], PROMPT)

    def test_a_non_boolean_auto_approve_refuses_the_run(self):
        omp.RUNTIME.write_config(self.sdir, {"auto_approve": "yes"})
        with self.assertRaises(ValueError):
            omp.RUNTIME.argv(self.sdir, PROMPT)

    def test_a_corrupt_options_file_refuses_the_run(self):
        with open(os.path.join(self.sdir, "options.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(ValueError):
            omp.RUNTIME.argv(self.sdir, PROMPT)


class OmpHomeProof(SessionCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(omp, "binary", return_value="/fake/omp")
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_argv_entry_points_at_a_home_config_or_state(self):
        omp.RUNTIME.write_config(self.sdir, {"auto_approve": True})
        argvs = [
            omp.RUNTIME.argv(self.sdir, PROMPT),
            omp.RUNTIME.argv(self.sdir, PROMPT, model=MODEL, resume="/s/a.jsonl"),
        ]
        for argv in argvs:
            for entry in argv:
                self.assertNotIn(".config/", entry)
                self.assertNotIn(".local/share/", entry)

    def test_no_argv_entry_points_at_the_session_options_file(self):
        omp.RUNTIME.write_config(self.sdir, {"auto_approve": False})
        for entry in omp.RUNTIME.argv(self.sdir, PROMPT):
            self.assertNotIn("options.json", entry)

    def test_the_environment_carries_no_home_paths(self):
        env = omp.RUNTIME.env(self.sdir)
        self.assertEqual(env, {})
        for value in env.values():
            self.assertNotIn(".config/", value)
            self.assertNotIn(".local/share/", value)


class PromptAsFlag(unittest.TestCase):
    """The prompt is the last argument; one that starts with `-` would be read as a flag."""

    SESSION_ID = "12345678-1234-5678-1234-567812345678"

    def setUp(self):
        patch = mock.patch.object(omp, "binary", return_value="/fake/omp")
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(muse, "binary", return_value="/fake/muse")
        patch.start()
        self.addCleanup(patch.stop)

    def argv(self, runtime, prompt):
        if runtime is muse.RUNTIME:
            return runtime.argv("/s", prompt, session_id=self.SESSION_ID)
        return runtime.argv("/s", prompt)

    def test_both_runtimes_refuse_a_prompt_that_starts_with_a_dash(self):
        for runtime in (omp.RUNTIME, muse.RUNTIME):
            for prompt in ("--help", "-p", "-"):
                with self.subTest(runtime=runtime.name, prompt=prompt):
                    with self.assertRaises(ValueError):
                        self.argv(runtime, prompt)

    def test_a_prompt_that_only_contains_a_dash_is_fine(self):
        self.assertEqual(omp.RUNTIME.argv("/s", "fix -x")[-1], "fix -x")
        argv = muse.RUNTIME.argv("/s", "fix -x", session_id=self.SESSION_ID)
        self.assertIn(os.path.join("/s", "prompt.md"), argv)


class DottedAndReservedKeys(SessionCase):
    def test_a_dotted_key_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            omp.RUNTIME.write_config(self.sdir, {"bash.autoBackground.enabled": True})
        self.assertFalse(os.path.exists(os.path.join(self.sdir, "overlay.yml")))

    def test_a_dotted_key_deeper_in_the_mapping_is_refused_too(self):
        with self.assertRaises(ValueError):
            omp.overlay_text({"bash": {"autoBackground.enabled": False}})

    def test_keys_that_yaml_reads_as_booleans_or_null_are_quoted(self):
        text = omp.overlay_text({"modelRoles": {"yes": "x", "Off": "x", "NULL": "x", "n": "x"}})
        for key in ("yes", "Off", "NULL", "n"):
            self.assertIn(f'  "{key}": "x"\n', text)

    def test_string_values_that_yaml_reads_as_booleans_or_null_stay_quoted(self):
        text = omp.overlay_text({"modelRoles": {"default": "off", "judge": "null"}})
        self.assertIn('  default: "off"\n', text)
        self.assertIn('  judge: "null"\n', text)

    def test_a_non_finite_float_is_refused(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    omp.overlay_text({"retry": {"limit": value}})

    def test_a_finite_number_is_written_as_a_number(self):
        self.assertIn("  limit: 2.5\n", omp.overlay_text({"retry": {"limit": 2.5}}))


class OmpSessionFiles(SessionCase):
    def test_a_directory_named_like_a_session_is_not_a_session(self):
        os.makedirs(os.path.join(self.sdir, "dir.jsonl"))
        self.assertIsNone(omp.RUNTIME.resume_handle(self.sdir))
        real = self.write("real.jsonl", "{}\n")
        self.assertEqual(omp.RUNTIME.resume_handle(self.sdir), os.path.abspath(real))

    def test_equal_mtimes_go_to_the_name_that_sorts_last(self):
        for name in ("b.jsonl", "a.jsonl"):
            os.utime(self.write(name, "{}\n"), (1000, 1000))
        self.assertEqual(
            omp.RUNTIME.resume_handle(self.sdir),
            os.path.join(os.path.abspath(self.sdir), "b.jsonl"),
        )


class RuntimeSetting(unittest.TestCase):
    def test_the_agents_runtime_setting_is_registered_by_the_runtimes_package(self):
        setting = settings.REGISTRY["agents.runtime"]
        self.assertEqual(setting.default, rt.DEFAULT_RUNTIME)
        for name in rt.NAMES:
            self.assertIn(name, setting.help)
        self.assertIn("corral.runtimes", settings.PROVIDERS)


if __name__ == "__main__":
    unittest.main()
