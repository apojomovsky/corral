"""Directory resolvers and atomic writes."""

import json
import os
import tempfile
import unittest

from corral import paths

HOME = os.path.expanduser("~")


class Resolvers(unittest.TestCase):
    def test_defaults_follow_xdg_when_nothing_is_set(self):
        env = {}
        self.assertEqual(paths.state_dir(env), os.path.join(HOME, ".local/state/corral"))
        self.assertEqual(paths.config_dir(env), os.path.join(HOME, ".config/corral"))
        self.assertEqual(paths.cache_dir(env), os.path.join(HOME, ".cache/corral"))
        self.assertEqual(
            paths.sessions_dir(env), os.path.join(HOME, ".local/state/corral/sessions")
        )

    def test_xdg_variables_move_the_defaults(self):
        env = {
            "XDG_STATE_HOME": "/x/state",
            "XDG_CONFIG_HOME": "/x/config",
            "XDG_CACHE_HOME": "/x/cache",
        }
        self.assertEqual(paths.state_dir(env), "/x/state/corral")
        self.assertEqual(paths.config_dir(env), "/x/config/corral")
        self.assertEqual(paths.cache_dir(env), "/x/cache/corral")
        self.assertEqual(paths.sessions_dir(env), "/x/state/corral/sessions")

    def test_a_relative_xdg_value_is_ignored(self):
        env = {"XDG_STATE_HOME": "relative/state"}
        self.assertEqual(paths.state_dir(env), os.path.join(HOME, ".local/state/corral"))

    def test_overrides_win_and_are_used_as_given(self):
        env = {
            "XDG_STATE_HOME": "/x/state",
            "XDG_CONFIG_HOME": "/x/config",
            "XDG_CACHE_HOME": "/x/cache",
            "CORRAL_STATE": "/o/state",
            "CORRAL_CONFIG": "/o/config",
            "CORRAL_CACHE": "/o/cache",
        }
        self.assertEqual(paths.state_dir(env), "/o/state")
        self.assertEqual(paths.config_dir(env), "/o/config")
        self.assertEqual(paths.cache_dir(env), "/o/cache")

    def test_sessions_follow_the_state_override(self):
        self.assertEqual(paths.sessions_dir({"CORRAL_STATE": "/o/state"}), "/o/state/sessions")

    def test_the_sessions_override_wins_over_the_state_dir(self):
        env = {"CORRAL_STATE": "/o/state", "CORRAL_SESSIONS": "/o/sessions"}
        self.assertEqual(paths.sessions_dir(env), "/o/sessions")

    def test_a_tilde_override_expands(self):
        self.assertEqual(paths.state_dir({"CORRAL_STATE": "~/s"}), os.path.join(HOME, "s"))

    def test_a_relative_override_is_a_configuration_error(self):
        for name, fn in (
            ("CORRAL_STATE", paths.state_dir),
            ("CORRAL_CONFIG", paths.config_dir),
            ("CORRAL_CACHE", paths.cache_dir),
            ("CORRAL_SESSIONS", paths.sessions_dir),
        ):
            with self.subTest(name), self.assertRaises(paths.PathError):
                fn({name: "relative/dir"})

    def test_relative_xdg_config_and_cache_are_ignored(self):
        env = {"XDG_CONFIG_HOME": "rel/config", "XDG_CACHE_HOME": "rel/cache"}
        self.assertEqual(paths.config_dir(env), os.path.join(HOME, ".config/corral"))
        self.assertEqual(paths.cache_dir(env), os.path.join(HOME, ".cache/corral"))

    def test_the_sessions_override_wins_over_xdg_state(self):
        env = {"XDG_STATE_HOME": "/x/state", "CORRAL_SESSIONS": "/o/sessions"}
        self.assertEqual(paths.sessions_dir(env), "/o/sessions")

    def test_a_relative_state_override_fails_sessions_too_and_names_it(self):
        with self.assertRaisesRegex(paths.PathError, "CORRAL_STATE"):
            paths.sessions_dir({"CORRAL_STATE": "relative/state"})

    def test_an_empty_override_counts_as_unset(self):
        env = {"CORRAL_STATE": "", "CORRAL_SESSIONS": ""}
        self.assertEqual(paths.state_dir(env), os.path.join(HOME, ".local/state/corral"))
        self.assertEqual(
            paths.sessions_dir(env), os.path.join(HOME, ".local/state/corral/sessions")
        )

    def test_results_are_normalized(self):
        self.assertEqual(paths.state_dir({"CORRAL_STATE": "/o/state/"}), "/o/state")
        self.assertEqual(paths.state_dir({"CORRAL_STATE": "/o/../o/state"}), "/o/state")
        self.assertEqual(paths.sessions_dir({"CORRAL_SESSIONS": "/o/s//x/"}), "/o/s/x")
        self.assertEqual(paths.config_dir({"XDG_CONFIG_HOME": "/x/./config/"}), "/x/config/corral")

    def test_the_suite_never_resolves_the_real_dirs(self):
        # tests/__init__.py points these at scratch dirs for the whole run.
        for fn in (paths.state_dir, paths.config_dir, paths.sessions_dir, paths.cache_dir):
            with self.subTest(fn.__name__):
                self.assertFalse(fn().startswith(os.path.join(HOME, ".")), fn())


class AtomicWrite(unittest.TestCase):
    def test_replace_is_all_or_nothing_with_no_litter(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, "state.json")
        paths.atomic_write_text(target, '{"a": 1}')
        paths.atomic_write_text(target, '{"a": 2}')
        with open(target) as f:
            self.assertEqual(json.load(f), {"a": 2})
        self.assertEqual(os.listdir(tmp.name), ["state.json"])

    def test_failed_write_cleans_its_temp_and_raises(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(OSError):
            paths.atomic_write_text(os.path.join(tmp.name, "missing", "s.json"), "{}")
        self.assertEqual(os.listdir(tmp.name), [])


class Cli(unittest.TestCase):
    def test_a_bad_override_exits_4(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        from corral import cli

        def boom(args):
            raise paths.PathError("CORRAL_STATE='x' must be an absolute path")

        err = io.StringIO()
        with mock.patch.object(cli, "cmd_deps", boom), redirect_stderr(err):
            self.assertEqual(cli.main(["deps"]), 4)
        self.assertIn("must be an absolute path", err.getvalue())

    def _run(self, argv):
        import io
        from contextlib import redirect_stdout

        from corral import cli

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_paths_prints_each_resolver(self):
        code, out = self._run(["paths"])
        self.assertEqual(code, 0)
        want = "".join(
            f"{name} = {getattr(paths, name + '_dir')()}\n"
            for name in ("state", "config", "cache", "sessions")
        )
        self.assertEqual(out, want)

    def test_paths_one_name_honours_the_override(self):
        from unittest import mock

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, "nested", "cache")
        with mock.patch.dict(os.environ, {"CORRAL_CACHE": target}):
            code, out = self._run(["paths", "cache"])
            self.assertEqual((code, out), (0, target + "\n"))
            self.assertFalse(os.path.exists(target))
            code, out = self._run(["paths", "cache", "--mkdir"])
        self.assertEqual((code, out), (0, target + "\n"))
        self.assertTrue(os.path.isdir(target))

    def test_an_unknown_name_is_a_usage_error(self):
        import io
        from contextlib import redirect_stderr

        with redirect_stderr(io.StringIO()):
            self.assertEqual(self._run(["paths", "bogus"])[0], 4)

    def test_agents_md_drafts_pr_bodies_through_the_resolver(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "AGENTS.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('mktemp "$(uv run corral paths cache --mkdir)/pr-', text)
        self.assertNotIn("XDG_CACHE_HOME", text)


if __name__ == "__main__":
    unittest.main()
