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

from corral import runtimes as rt
from corral import settings
from corral.runtimes import omp, opencode

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
        self.assertEqual(rt.get("opencode").name, "opencode")

    def test_anything_else_is_an_unknown_runtime(self):
        for name in ("claude-code", "", "OMP"):
            with self.assertRaises(rt.UnknownRuntime, msg=name):
                rt.get(name)
        self.assertTrue(issubclass(rt.UnknownRuntime, ValueError))


class Record(SessionCase):
    def test_an_unrecorded_session_reads_as_omp(self):
        self.assertEqual(rt.recorded(self.sdir), "omp")

    def test_the_recorded_runtime_is_read_back(self):
        rt.record(self.sdir, "opencode")
        self.assertEqual(rt.recorded(self.sdir), "opencode")
        with open(os.path.join(self.sdir, "runtime"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "opencode\n")

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
        opencode.binary.cache_clear()
        self.addCleanup(omp.binary.cache_clear)
        self.addCleanup(opencode.binary.cache_clear)

    def test_the_binary_on_path_wins(self):
        with mock.patch("corral.runtimes.omp.shutil.which", return_value="/opt/bin/omp"):
            self.assertEqual(omp.binary(), "/opt/bin/omp")

    def test_without_one_on_path_the_usual_install_location_is_used(self):
        with mock.patch("corral.runtimes.omp.shutil.which", return_value=None):
            self.assertEqual(omp.binary(), os.path.expanduser("~/.local/bin/omp"))
        with mock.patch("corral.runtimes.opencode.shutil.which", return_value=None):
            self.assertEqual(opencode.binary(), os.path.expanduser("~/.opencode/bin/opencode"))

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


class OpencodeArgv(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(opencode, "binary", return_value="/fake/opencode")
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_launch_argv_is_the_json_run_form(self):
        self.assertEqual(
            opencode.RUNTIME.argv("/s", PROMPT),
            ["/fake/opencode", "run", "--format", "json", "--auto", "--pure", PROMPT],
        )

    def test_resume_and_model_are_added_before_the_prompt(self):
        self.assertEqual(
            opencode.RUNTIME.argv("/s", PROMPT, model=MODEL, resume="ses_1"),
            [
                "/fake/opencode",
                "run",
                "--format",
                "json",
                "--auto",
                "--pure",
                "--session",
                "ses_1",
                "--model",
                MODEL,
                PROMPT,
            ],
        )

    def test_the_environment_points_at_the_session_config(self):
        self.assertEqual(
            opencode.RUNTIME.env("/s"),
            {
                "OPENCODE_CONFIG": os.path.join("/s", "opencode.json"),
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_SHARE": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": "3600000",
            },
        )


class OpencodeSessions(SessionCase):
    def test_the_config_is_written_as_json_into_the_session_dir(self):
        config = {"$schema": "https://example.com/config.json", "model": MODEL}
        opencode.RUNTIME.write_config(self.sdir, config)
        with open(os.path.join(self.sdir, "opencode.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), config)
        self.assertEqual(os.listdir(self.sdir), ["opencode.json"])

    def test_the_resume_handle_is_the_last_session_id_in_out_log(self):
        self.write(
            "out.log",
            "\n".join(
                [
                    json.dumps({"type": "step_start", "sessionID": "ses_1"}),
                    "not json",
                    json.dumps(["no", "session"]),
                    json.dumps({"type": "text", "sessionID": "ses_2"}),
                    json.dumps({"type": "step_finish"}),
                    "",
                ]
            ),
        )
        self.assertEqual(opencode.RUNTIME.resume_handle(self.sdir), "ses_2")

    def test_no_handle_without_an_out_log_or_a_session_id(self):
        self.assertIsNone(opencode.RUNTIME.resume_handle(self.sdir))
        self.write("out.log", json.dumps({"type": "step_start"}) + "\n")
        self.assertIsNone(opencode.RUNTIME.resume_handle(self.sdir))

    def test_the_activity_file_is_out_log_once_it_exists(self):
        self.assertIsNone(opencode.RUNTIME.activity_file(self.sdir))
        path = self.write("out.log", "")
        self.assertEqual(opencode.RUNTIME.activity_file(self.sdir), path)


class Adapters(unittest.TestCase):
    def test_each_adapter_names_its_runtime(self):
        self.assertEqual(omp.RUNTIME.name, "omp")
        self.assertEqual(opencode.RUNTIME.name, "opencode")

    def test_an_unrecorded_session_defaults_to_omp(self):
        self.assertEqual(rt.DEFAULT_RUNTIME, "omp")


class PromptAsFlag(unittest.TestCase):
    """The prompt is the last argument; one that starts with `-` would be read as a flag."""

    def setUp(self):
        patch = mock.patch.object(omp, "binary", return_value="/fake/omp")
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(opencode, "binary", return_value="/fake/opencode")
        patch.start()
        self.addCleanup(patch.stop)

    def test_both_runtimes_refuse_a_prompt_that_starts_with_a_dash(self):
        for runtime in (omp.RUNTIME, opencode.RUNTIME):
            for prompt in ("--help", "-p", "-"):
                with self.subTest(runtime=runtime.name, prompt=prompt):
                    with self.assertRaises(ValueError):
                        runtime.argv("/s", prompt)

    def test_a_prompt_that_only_contains_a_dash_is_fine(self):
        self.assertEqual(omp.RUNTIME.argv("/s", "fix -x")[-1], "fix -x")
        self.assertEqual(opencode.RUNTIME.argv("/s", "fix -x")[-1], "fix -x")


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


class OpencodeSessionIds(SessionCase):
    def test_a_non_string_session_id_on_the_last_line_is_skipped(self):
        self.write(
            "out.log",
            "\n".join(
                [
                    json.dumps({"sessionID": "ses_1"}),
                    json.dumps({"sessionID": ["ses_2"]}),
                ]
            )
            + "\n",
        )
        self.assertEqual(opencode.RUNTIME.resume_handle(self.sdir), "ses_1")

    def test_numeric_and_empty_session_ids_are_skipped(self):
        self.write(
            "out.log",
            "\n".join(
                [
                    json.dumps({"sessionID": "ses_1"}),
                    json.dumps({"sessionID": 7}),
                    json.dumps({"sessionID": ""}),
                ]
            )
            + "\n",
        )
        self.assertEqual(opencode.RUNTIME.resume_handle(self.sdir), "ses_1")


class RuntimeSetting(unittest.TestCase):
    def test_the_agents_runtime_setting_is_registered_by_the_runtimes_package(self):
        setting = settings.REGISTRY["agents.runtime"]
        self.assertEqual(setting.default, rt.DEFAULT_RUNTIME)
        self.assertIn("omp or opencode", setting.help)
        self.assertIn("corral.runtimes", settings.PROVIDERS)


if __name__ == "__main__":
    unittest.main()
