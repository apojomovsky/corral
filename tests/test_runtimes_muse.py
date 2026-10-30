"""The muse runtime adapter: argv, registration and the model id strip.

Offline: every session dir is a temp dir with fake files, and the binary
and session id generation are patched. The real muse binary never runs,
and no home config is read or written.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from corral import runtimes as rt
from corral.runtimes import muse

PROMPT = "do the thing"
SESSION_ID = "12345678-1234-5678-1234-567812345678"
EXISTING_ID = "87654321-4321-8765-4321-876543218765"


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-muse-"))
        self.sdir = os.path.join(self.root, "session")
        os.makedirs(self.sdir)
        binary = mock.patch.object(muse, "binary", return_value="/fake/muse")
        binary.start()
        self.addCleanup(binary.stop)
        fresh = mock.patch.object(muse, "new_session_id", return_value=SESSION_ID)
        fresh.start()
        self.addCleanup(fresh.stop)

    def write_config_model(self, entry):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            json.dump({"model": entry}, handle)


class WorkerArgv(SessionCase):
    def test_a_launch_argv_is_the_exec_json_form(self):
        self.write_config_model("muse/spark-1")
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT),
            [
                "/fake/muse",
                "exec",
                "--json",
                "--prompt-file",
                os.path.join(self.sdir, "prompt.md"),
                "--model",
                "spark-1",
                "--contributor-mode",
                "on",
                "--session-id",
                SESSION_ID,
                "--yolo",
                "--no-foreign-personal-context",
                "--disable-request-user-input",
                "--reasoning-effort",
                "high",
                "--parallel-tool-calls",
            ],
        )

    def test_without_a_configured_model_there_is_no_model_flag(self):
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT),
            [
                "/fake/muse",
                "exec",
                "--json",
                "--prompt-file",
                os.path.join(self.sdir, "prompt.md"),
                "--contributor-mode",
                "on",
                "--session-id",
                SESSION_ID,
                "--yolo",
                "--no-foreign-personal-context",
                "--disable-request-user-input",
                "--reasoning-effort",
                "high",
                "--parallel-tool-calls",
            ],
        )

    def test_a_model_override_beats_the_config_model(self):
        self.write_config_model("muse/spark-1")
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, model="muse/other")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "other")
        self.assertNotIn("spark-1", argv)

    def test_a_bare_model_override_passes_through(self):
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, model="other")
        self.assertEqual(argv[argv.index("--model") + 1], "other")


class ReviewerArgv(SessionCase):
    def test_a_reviewer_argv_is_the_worker_form_plus_disable_write(self):
        self.write_config_model("muse/spark-1")
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT, reviewer=True),
            [
                "/fake/muse",
                "exec",
                "--json",
                "--prompt-file",
                os.path.join(self.sdir, "prompt.md"),
                "--model",
                "spark-1",
                "--contributor-mode",
                "on",
                "--session-id",
                SESSION_ID,
                "--yolo",
                "--no-foreign-personal-context",
                "--disable-request-user-input",
                "--reasoning-effort",
                "high",
                "--parallel-tool-calls",
                "--disable-write",
            ],
        )

    def test_a_worker_run_writes_nothing(self):
        self.write_config_model("muse/spark-1")
        self.assertNotIn("--disable-write", muse.RUNTIME.argv(self.sdir, PROMPT))


class ResumeArgv(SessionCase):
    def test_a_resume_argv_reuses_the_session_with_the_message_file(self):
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID),
            [
                "/fake/muse",
                "exec",
                "--json",
                "--prompt-file",
                os.path.join(self.sdir, "message.md"),
                "--contributor-mode",
                "on",
                "--session-id",
                EXISTING_ID,
                "--yolo",
                "--no-foreign-personal-context",
                "--disable-request-user-input",
                "--reasoning-effort",
                "high",
                "--parallel-tool-calls",
            ],
        )

    def test_a_resume_takes_no_model_from_the_config(self):
        self.write_config_model("muse/spark-1")
        self.assertNotIn("--model", muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID))

    def test_a_resume_model_override_takes_the_same_stripped_path(self):
        for override in ("muse/other", "other"):
            with self.subTest(override=override):
                argv = muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID, model=override)
                self.assertEqual(argv[argv.index("--model") + 1], "other")

    def test_a_resumed_reviewer_keeps_disable_write(self):
        argv = muse.RUNTIME.argv(
            self.sdir, PROMPT, resume=EXISTING_ID, model="muse/other", reviewer=True
        )
        self.assertEqual(argv[argv.index("--model") + 1], "other")
        self.assertIn("--disable-write", argv)


class PromptAsFlag(SessionCase):
    def test_a_prompt_starting_with_a_dash_is_refused(self):
        for prompt in ("--help", "-p", "-"):
            with self.subTest(prompt=prompt):
                with self.assertRaises(ValueError):
                    muse.RUNTIME.argv(self.sdir, prompt)
                with self.assertRaises(ValueError):
                    muse.RUNTIME.argv(self.sdir, prompt, reviewer=True)
                with self.assertRaises(ValueError):
                    muse.RUNTIME.argv(self.sdir, prompt, resume=EXISTING_ID)

    def test_a_prompt_that_only_contains_a_dash_is_fine(self):
        argv = muse.RUNTIME.argv(self.sdir, "fix -x")
        self.assertIn(os.path.join(self.sdir, "prompt.md"), argv)
        self.assertEqual(argv[-1], "--parallel-tool-calls")


class MaxTime(SessionCase):
    def test_max_time_is_accepted_and_ignored(self):
        self.write_config_model("muse/spark-1")
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT, max_time="30m"),
            muse.RUNTIME.argv(self.sdir, PROMPT),
        )


class BadConfig(SessionCase):
    def test_a_corrupt_config_raises_a_clear_error(self):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaisesRegex(ValueError, "muse.json"):
            muse.RUNTIME.argv(self.sdir, PROMPT)

    def test_a_present_but_unusable_model_raises_like_a_corrupt_file(self):
        for bad in (123, True, ["muse/spark-1"], {"id": "muse/spark-1"}):
            with self.subTest(bad=bad):
                with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
                    json.dump({"model": bad}, handle)
                with self.assertRaisesRegex(ValueError, "muse.json"):
                    muse.RUNTIME.argv(self.sdir, PROMPT)

    def test_a_null_model_is_present_and_raises(self):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            handle.write('{"model": null}')
        with self.assertRaisesRegex(ValueError, "muse.json"):
            muse.RUNTIME.argv(self.sdir, PROMPT)

    def test_an_empty_string_model_means_no_model_flag(self):
        self.write_config_model("")
        self.assertNotIn("--model", muse.RUNTIME.argv(self.sdir, PROMPT))

    def test_a_non_object_config_raises_a_clear_error(self):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            handle.write('["muse/spark-1"]')
        with self.assertRaisesRegex(ValueError, "muse.json"):
            muse.RUNTIME.argv(self.sdir, PROMPT)

    def test_an_unreadable_config_raises_rather_than_silently_defaulting(self):
        self.write_config_model("muse/spark-1")
        path = os.path.join(self.sdir, "muse.json")
        os.chmod(path, 0)
        try:
            with self.assertRaisesRegex(ValueError, "muse.json"):
                muse.RUNTIME.argv(self.sdir, PROMPT)
        finally:
            os.chmod(path, 0o600)


class EmptyOverrides(SessionCase):
    def test_an_empty_model_falls_back_to_the_config_model(self):
        self.write_config_model("muse/spark-1")
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, model="")
        self.assertEqual(argv[argv.index("--model") + 1], "spark-1")

    def test_an_empty_model_with_no_config_means_no_model_flag(self):
        self.assertNotIn("--model", muse.RUNTIME.argv(self.sdir, PROMPT, model=""))

    def test_an_empty_resume_means_a_fresh_run(self):
        self.write_config_model("muse/spark-1")
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, resume="")
        self.assertIn(os.path.join(self.sdir, "prompt.md"), argv)
        self.assertIn(SESSION_ID, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "spark-1")


class ActivityFile(SessionCase):
    def test_activity_file_is_deferred_to_the_resume_ticket(self):
        with self.assertRaisesRegex(NotImplementedError, "T3"):
            muse.RUNTIME.activity_file(self.sdir)


class WriteConfig(SessionCase):
    def test_write_config_round_trips_through_the_session_file(self):
        config = {"model": "muse/spark-1", "tab.width": 4}
        muse.RUNTIME.write_config(self.sdir, config)
        with open(os.path.join(self.sdir, "muse.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), config)

    def test_write_config_keeps_every_key(self):
        config = {"model": "muse/spark-1", "flag": True, "token": "${SECRET}"}
        muse.RUNTIME.write_config(self.sdir, config)
        with open(os.path.join(self.sdir, "muse.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), config)


class WithModels(SessionCase):
    def test_with_models_sets_the_default_head_and_keeps_other_keys(self):
        self.assertEqual(
            muse.RUNTIME.with_models({"tab.width": 4}, {"default": ["muse/spark-1", "muse/other"]}),
            {"tab.width": 4, "model": "muse/spark-1"},
        )

    def test_with_models_replaces_a_stale_model(self):
        self.assertEqual(
            muse.RUNTIME.with_models(
                {"model": "muse/old"},
                {"default": ["muse/spark-1"]},
            ),
            {"model": "muse/spark-1"},
        )


class Env(SessionCase):
    def test_env_points_at_no_home_config(self):
        env = muse.RUNTIME.env(self.sdir)
        self.assertEqual(env, {})
        for value in env.values():
            self.assertNotIn(".config/muse", value)
            self.assertNotIn(".local/share/muse", value)


class DashC(SessionCase):
    def write_config(self, config):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)

    def test_other_entries_become_dash_c_pairs_after_the_model(self):
        self.write_config({"model": "muse/spark-1", "tab.width": 4, "flag": True, "name": "x"})
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT),
            [
                "/fake/muse",
                "exec",
                "--json",
                "--prompt-file",
                os.path.join(self.sdir, "prompt.md"),
                "--model",
                "spark-1",
                "-c",
                "flag=true",
                "-c",
                'name="x"',
                "-c",
                "tab.width=4",
                "--contributor-mode",
                "on",
                "--session-id",
                SESSION_ID,
                "--yolo",
                "--no-foreign-personal-context",
                "--disable-request-user-input",
                "--reasoning-effort",
                "high",
                "--parallel-tool-calls",
            ],
        )

    def test_a_whole_value_env_reference_passes_through_unquoted(self):
        self.write_config({"token": "${SECRET}"})
        argv = muse.RUNTIME.argv(self.sdir, PROMPT)
        self.assertIn("-c", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "token=${SECRET}")

    def test_a_partial_env_reference_stays_json(self):
        self.write_config({"name": "x-${SECRET}"})
        argv = muse.RUNTIME.argv(self.sdir, PROMPT)
        self.assertEqual(argv[argv.index("-c") + 1], 'name="x-${SECRET}"')

    def test_a_resume_keeps_dash_c_but_takes_no_model_from_the_config(self):
        self.write_config({"model": "muse/spark-1", "tab.width": 4})
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID)
        self.assertNotIn("--model", argv)
        self.assertIn("-c", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "tab.width=4")

    def test_an_unknown_key_takes_the_shape_muse_refuses(self):
        self.write_config({"bogus.key": 1})
        argv = muse.RUNTIME.argv(self.sdir, PROMPT)
        self.assertEqual(argv[argv.index("-c") + 1], "bogus.key=1")


class HomeProof(SessionCase):
    def test_no_argv_entry_points_at_the_home_config_or_state(self):
        self.write_config_model("muse/spark-1")
        argvs = [
            muse.RUNTIME.argv(self.sdir, PROMPT),
            muse.RUNTIME.argv(self.sdir, PROMPT, reviewer=True),
            muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID, model="muse/other"),
        ]
        for argv in argvs:
            for entry in argv:
                self.assertNotIn(".config/muse", entry)
                self.assertNotIn(".local/share/muse", entry)

    def test_the_full_flag_allowlist_is_pinned(self):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            json.dump({"model": "muse/spark-1", "tab.width": 4, "token": "${S}"}, handle)
        argvs = [
            muse.RUNTIME.argv(self.sdir, PROMPT),
            muse.RUNTIME.argv(self.sdir, PROMPT, reviewer=True),
            muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID, model="muse/other"),
        ]
        seen = {token for argv in argvs for token in argv if token.startswith("-")}
        self.assertEqual(
            seen,
            {
                "--json",
                "--prompt-file",
                "--model",
                "--contributor-mode",
                "--session-id",
                "--yolo",
                "--no-foreign-personal-context",
                "--disable-request-user-input",
                "--reasoning-effort",
                "--parallel-tool-calls",
                "--disable-write",
                "-c",
            },
        )


class BinaryResolution(unittest.TestCase):
    def setUp(self):
        muse.binary.cache_clear()
        self.addCleanup(muse.binary.cache_clear)

    def test_the_binary_on_path_wins(self):
        with mock.patch("corral.runtimes.muse.shutil.which", return_value="/opt/bin/muse"):
            self.assertEqual(muse.binary(), "/opt/bin/muse")

    def test_without_one_on_path_the_usual_install_location_is_used(self):
        with mock.patch("corral.runtimes.muse.shutil.which", return_value=None):
            self.assertEqual(muse.binary(), os.path.expanduser("~/.local/bin/muse"))

    def test_the_binary_is_found_once(self):
        with mock.patch("corral.runtimes.muse.shutil.which", return_value="/opt/bin/muse") as which:
            muse.binary()
            muse.binary()
        self.assertEqual(which.call_count, 1)


class Registration(unittest.TestCase):
    def test_muse_resolves_by_name(self):
        self.assertEqual(rt.get("muse").name, "muse")

    def test_muse_is_a_known_runtime_and_omp_stays_the_default(self):
        self.assertIn("muse", rt.NAMES)
        self.assertEqual(rt.DEFAULT_RUNTIME, "omp")


if __name__ == "__main__":
    unittest.main()
