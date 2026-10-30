"""The muse runtime adapter: argv, the session record, resolve and activity.

Offline: every session dir is a temp dir with fake files, and the binary is
patched. The real muse binary never runs, and no home config or home state
is read or written (resolve takes an explicit root; only `sessions_root`
names the home dir and tests patch it).
"""

import json
import os
import tempfile
import unittest
import uuid
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

    def write_config_model(self, entry):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            json.dump({"model": entry}, handle)


class WorkerArgv(SessionCase):
    def test_a_launch_argv_is_the_exec_json_form(self):
        self.write_config_model("muse/spark-1")
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID),
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
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID),
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
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, model="muse/other", session_id=SESSION_ID)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "other")
        self.assertNotIn("spark-1", argv)

    def test_a_bare_model_override_passes_through(self):
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, model="other", session_id=SESSION_ID)
        self.assertEqual(argv[argv.index("--model") + 1], "other")


class ReviewerArgv(SessionCase):
    def test_a_reviewer_argv_is_the_worker_form_plus_disable_write(self):
        self.write_config_model("muse/spark-1")
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT, reviewer=True, session_id=SESSION_ID),
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
        self.assertNotIn(
            "--disable-write", muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)
        )


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

    def test_a_resume_ignores_a_passed_session_id(self):
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID, session_id=SESSION_ID)
        self.assertEqual(argv[argv.index("--session-id") + 1], EXISTING_ID)


class FreshRunNeedsSessionId(SessionCase):
    def test_a_fresh_run_without_a_session_id_is_refused(self):
        self.write_config_model("muse/spark-1")
        for kwargs in ({}, {"reviewer": True}, {"model": "muse/other"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "session"):
                    muse.RUNTIME.argv(self.sdir, PROMPT, **kwargs)

    def test_an_empty_session_id_is_refused_like_a_missing_one(self):
        with self.assertRaisesRegex(ValueError, "session"):
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id="")

    def test_a_fresh_run_uses_the_passed_session_id(self):
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)
        self.assertEqual(argv[argv.index("--session-id") + 1], SESSION_ID)


class NewSessionId(unittest.TestCase):
    def test_new_ids_are_unique_uuid4_values(self):
        first, second = muse.new_session_id(), muse.new_session_id()
        self.assertNotEqual(first, second)
        for value in (first, second):
            self.assertEqual(str(uuid.UUID(value, version=4)), value)


class RecordAndResume(SessionCase):
    def test_record_writes_the_id_the_handle_reads_back(self):
        muse.record_session_id(self.sdir, SESSION_ID)
        with open(os.path.join(self.sdir, "muse_session_id"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), SESSION_ID + "\n")
        self.assertEqual(muse.RUNTIME.resume_handle(self.sdir), SESSION_ID)

    def test_no_record_means_no_handle(self):
        self.assertIsNone(muse.RUNTIME.resume_handle(self.sdir))

    def test_an_empty_record_means_no_handle(self):
        with open(os.path.join(self.sdir, "muse_session_id"), "w", encoding="utf-8") as handle:
            handle.write("  \n")
        self.assertIsNone(muse.RUNTIME.resume_handle(self.sdir))

    def test_undecodable_bytes_mean_no_handle(self):
        with open(os.path.join(self.sdir, "muse_session_id"), "wb") as handle:
            handle.write(b"\xff\xfe\x00bad\n")
        self.assertIsNone(muse.RUNTIME.resume_handle(self.sdir))

    def test_the_handle_needs_no_exported_session_tree(self):
        muse.record_session_id(self.sdir, SESSION_ID)
        self.assertEqual(muse.RUNTIME.resume_handle(self.sdir), SESSION_ID)


class ResolveCase(SessionCase):
    def setUp(self):
        super().setUp()
        self.export = os.path.join(self.root, "export")
        os.makedirs(self.export)

    def plant(self, day, sid, mtime):
        target = os.path.join(self.export, *day.split("/"), sid, "session.jsonl")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        os.utime(target, (mtime, mtime))
        return target

    def test_resolve_finds_the_newest_session_file(self):
        self.plant("2026/01/01", SESSION_ID, 1000)
        new = self.plant("2026/01/02", SESSION_ID, 2000)
        self.assertEqual(muse.resolve(SESSION_ID, self.export), os.path.abspath(new))

    def test_an_equal_mtime_never_depends_on_directory_order(self):
        self.plant("2026/01/01", SESSION_ID, 1000)
        second = self.plant("2026/01/02", SESSION_ID, 1000)
        self.assertEqual(muse.resolve(SESSION_ID, self.export), os.path.abspath(second))
        self.assertEqual(muse.resolve(SESSION_ID, self.export), os.path.abspath(second))

    def test_resolve_returns_none_when_absent(self):
        self.assertIsNone(muse.resolve(SESSION_ID, self.export))
        self.assertIsNone(muse.resolve(SESSION_ID, os.path.join(self.root, "missing")))
        self.plant("2026/01/01", EXISTING_ID, 1000)
        self.assertIsNone(muse.resolve(SESSION_ID, self.export))

    def test_a_session_id_that_escapes_the_root_finds_nothing(self):
        for bad in ("../x", "a/b", "", ".", "..", "*", "a[bc]"):
            with self.subTest(bad=bad):
                self.assertIsNone(muse.resolve(bad, self.export))

    def test_resolve_defaults_to_the_home_sessions_root(self):
        with mock.patch.object(muse, "sessions_root", return_value=self.export) as root:
            self.assertIsNone(muse.resolve(SESSION_ID))
            root.assert_called_once_with()
        planted = self.plant("2026/01/01", SESSION_ID, 1000)
        with mock.patch.object(muse, "sessions_root", return_value=self.export):
            self.assertEqual(muse.resolve(SESSION_ID), os.path.abspath(planted))


class ActivityFile(SessionCase):
    def test_no_out_log_means_no_activity(self):
        self.assertIsNone(muse.RUNTIME.activity_file(self.sdir))

    def test_a_present_out_log_is_the_activity(self):
        target = os.path.join(self.sdir, "out.log")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertEqual(muse.RUNTIME.activity_file(self.sdir), target)


class WriteMessage(SessionCase):
    def test_write_message_leaves_the_resume_prompt_file(self):
        muse.write_message(self.sdir, PROMPT)
        with open(os.path.join(self.sdir, "message.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), PROMPT)


class PromptAsFlag(SessionCase):
    def test_a_prompt_starting_with_a_dash_is_refused(self):
        for prompt in ("--help", "-p", "-"):
            with self.subTest(prompt=prompt):
                with self.assertRaises(ValueError):
                    muse.RUNTIME.argv(self.sdir, prompt, session_id=SESSION_ID)
                with self.assertRaises(ValueError):
                    muse.RUNTIME.argv(self.sdir, prompt, reviewer=True, session_id=SESSION_ID)
                with self.assertRaises(ValueError):
                    muse.RUNTIME.argv(self.sdir, prompt, resume=EXISTING_ID)

    def test_a_prompt_that_only_contains_a_dash_is_fine(self):
        argv = muse.RUNTIME.argv(self.sdir, "fix -x", session_id=SESSION_ID)
        self.assertIn(os.path.join(self.sdir, "prompt.md"), argv)
        self.assertEqual(argv[-1], "--parallel-tool-calls")


class MaxTime(SessionCase):
    def test_max_time_is_accepted_and_ignored(self):
        self.write_config_model("muse/spark-1")
        self.assertEqual(
            muse.RUNTIME.argv(self.sdir, PROMPT, max_time="30m", session_id=SESSION_ID),
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID),
        )


class BadConfig(SessionCase):
    def test_a_corrupt_config_raises_a_clear_error(self):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaisesRegex(ValueError, "muse.json"):
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)

    def test_a_present_but_unusable_model_raises_like_a_corrupt_file(self):
        for bad in (123, True, ["muse/spark-1"], {"id": "muse/spark-1"}):
            with self.subTest(bad=bad):
                with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
                    json.dump({"model": bad}, handle)
                with self.assertRaisesRegex(ValueError, "muse.json"):
                    muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)

    def test_a_null_model_is_present_and_raises(self):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            handle.write('{"model": null}')
        with self.assertRaisesRegex(ValueError, "muse.json"):
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)

    def test_an_empty_string_model_means_no_model_flag(self):
        self.write_config_model("")
        self.assertNotIn("--model", muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID))

    def test_a_non_object_config_raises_a_clear_error(self):
        with open(os.path.join(self.sdir, "muse.json"), "w", encoding="utf-8") as handle:
            handle.write('["muse/spark-1"]')
        with self.assertRaisesRegex(ValueError, "muse.json"):
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)

    def test_an_unreadable_config_raises_rather_than_silently_defaulting(self):
        self.write_config_model("muse/spark-1")
        path = os.path.join(self.sdir, "muse.json")
        os.chmod(path, 0)
        try:
            with self.assertRaisesRegex(ValueError, "muse.json"):
                muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)
        finally:
            os.chmod(path, 0o600)


class EmptyOverrides(SessionCase):
    def test_an_empty_model_falls_back_to_the_config_model(self):
        self.write_config_model("muse/spark-1")
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, model="", session_id=SESSION_ID)
        self.assertEqual(argv[argv.index("--model") + 1], "spark-1")

    def test_an_empty_model_with_no_config_means_no_model_flag(self):
        self.assertNotIn(
            "--model", muse.RUNTIME.argv(self.sdir, PROMPT, model="", session_id=SESSION_ID)
        )

    def test_an_empty_resume_means_a_fresh_run(self):
        self.write_config_model("muse/spark-1")
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, resume="", session_id=SESSION_ID)
        self.assertIn(os.path.join(self.sdir, "prompt.md"), argv)
        self.assertIn(SESSION_ID, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "spark-1")


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
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID),
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
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)
        self.assertIn("-c", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "token=${SECRET}")

    def test_a_partial_env_reference_stays_json(self):
        self.write_config({"name": "x-${SECRET}"})
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)
        self.assertEqual(argv[argv.index("-c") + 1], 'name="x-${SECRET}"')

    def test_a_resume_keeps_dash_c_but_takes_no_model_from_the_config(self):
        self.write_config({"model": "muse/spark-1", "tab.width": 4})
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, resume=EXISTING_ID)
        self.assertNotIn("--model", argv)
        self.assertIn("-c", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "tab.width=4")

    def test_an_unknown_key_takes_the_shape_muse_refuses(self):
        self.write_config({"bogus.key": 1})
        argv = muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID)
        self.assertEqual(argv[argv.index("-c") + 1], "bogus.key=1")


class HomeProof(SessionCase):
    def test_no_argv_entry_points_at_the_home_config_or_state(self):
        self.write_config_model("muse/spark-1")
        argvs = [
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID),
            muse.RUNTIME.argv(self.sdir, PROMPT, reviewer=True, session_id=SESSION_ID),
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
            muse.RUNTIME.argv(self.sdir, PROMPT, session_id=SESSION_ID),
            muse.RUNTIME.argv(self.sdir, PROMPT, reviewer=True, session_id=SESSION_ID),
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


#: (returncode, stderr, stream) cut from the observed exit-2 usage lines
#: in the design: a bad `--session-id`, an unknown `-c` key, `--prompt-file`
#: with positional text, and a meta-only flag under a non-meta provider.
USAGE_CASES = (
    (2, "muse: invalid --session-id 'abc': expected a UUID", ""),
    (2, "muse: refusing run: unknown key 'bogus.key'", ""),
    (2, "muse: --prompt-file cannot be used with inline prompt text", ""),
    (2, "muse: --model requires --provider meta or local", ""),
    (
        2,
        "muse: refusing run: unknown key 'bogus.key'",
        '{"payload_type": "run.lifecycle.started", "payload": {}}',
    ),
)

#: (name, returncode, stderr, stream) with no learned wording: a clean run,
#: stderr noise, a usage line under another exit, and one representative
#: failure per provider category of the design (auth, quota, rate limit,
#: network). The provider lines carry the obvious keywords on purpose: a
#: classifier that guesses from keywords must still fail these.
UNKNOWN_CASES = (
    (
        "success",
        0,
        "",
        '{"payload_type": "run.terminal.completed",'
        ' "payload": {"terminal": "completed", "reason": "done"}}',
    ),
    (
        "stderr noise on a clean run",
        0,
        "Linux sandbox notice: user namespaces are restricted\n"
        "workspace trust: trusted source=run-flag",
        "",
    ),
    (
        "usage wording under another exit",
        1,
        "muse: refusing run: unknown key 'bogus.key'",
        "",
    ),
    ("usage wording with no exit", None, "muse: --model requires --provider meta or local", ""),
    ("usage wording with no stderr", 2, None, ""),
    (
        "auth failure",
        1,
        "muse: auth failed: credentials expired, run the login flow to continue",
        "",
    ),
    (
        "quota exhaustion",
        1,
        "muse: quota exhausted: the weekly allowance is at 100 percent",
        "",
    ),
    (
        "rate limit",
        2,
        "muse: rate limited: too many requests, retry in a minute",
        "",
    ),
    (
        "network failure",
        1,
        "muse: network unreachable: connection refused",
        "",
    ),
)


class ClassifyUsage(unittest.TestCase):
    def test_each_observed_usage_line_classifies_as_usage(self):
        for returncode, stderr, stream in USAGE_CASES:
            with self.subTest(stderr=stderr):
                self.assertEqual(muse.classify(returncode, stderr, stream), "usage")

    def test_a_usage_line_with_a_trailing_newline_still_classifies(self):
        returncode, stderr, stream = USAGE_CASES[0]
        self.assertEqual(muse.classify(returncode, stderr + "\n", stream), "usage")


#: The provider failure categories of the design, one unknown case each.
_PROVIDER_NAMES = ("auth failure", "quota exhaustion", "rate limit", "network failure")


class ClassifyUnknown(unittest.TestCase):
    def test_everything_without_a_learned_wording_classifies_as_unknown(self):
        for name, returncode, stderr, stream in UNKNOWN_CASES:
            with self.subTest(name=name):
                self.assertEqual(muse.classify(returncode, stderr, stream), "unknown")

    def test_each_provider_category_classifies_as_unknown(self):
        provider = [case for case in UNKNOWN_CASES if case[0] in _PROVIDER_NAMES]
        self.assertEqual(len(provider), len(_PROVIDER_NAMES))
        for name, returncode, stderr, stream in provider:
            with self.subTest(name=name):
                self.assertEqual(muse.classify(returncode, stderr, stream), "unknown")


class ClassifyMarksNothing(unittest.TestCase):
    def test_unknown_reaches_no_mark(self):
        self.assertNotIn("unknown", muse.MARK_CATEGORIES)

    def test_no_classify_result_reaches_a_mark(self):
        results = {muse.classify(code, err, stream) for code, err, stream in USAGE_CASES}
        results |= {muse.classify(code, err, stream) for _, code, err, stream in UNKNOWN_CASES}
        self.assertLessEqual(results, {"usage", "unknown"})
        self.assertEqual(results & set(muse.MARK_CATEGORIES), set())


if __name__ == "__main__":
    unittest.main()
