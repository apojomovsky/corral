"""The layered settings resolver and `corral config show`."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, settings

S = settings


def resolve(**kw):
    kw.setdefault("env", {})
    return S.resolve(**kw)


class Layers(unittest.TestCase):
    def test_defaults_come_from_the_registry(self):
        r = resolve()
        self.assertEqual(r["agents.cap"], 1)
        self.assertEqual(r.values["agents.cap"].origin, S.DEFAULT)
        self.assertEqual(r["git.branch"], "{type}/{issue}-{slug}")

    def test_each_layer_beats_the_one_before(self):
        host = {"agents": {"cap": 2}}
        project = {"agents": {"cap": 3}, "repo": [{"name": "app", "agents": {"cap": 4}}]}
        env = {"CORRAL_AGENTS_CAP": "5"}
        cases = [
            (dict(host=host), 2, S.HOST),
            (dict(host=host, project=project), 3, S.PROJECT),
            (dict(host=host, project=project, repo="app"), 4, S.REPO),
            (dict(host=host, project=project, repo="app", env=env), 5, S.ENV),
            (
                dict(host=host, project=project, repo="app", env=env, flags={"agents.cap": 6}),
                6,
                S.FLAG,
            ),
        ]
        for kw, value, origin in cases:
            with self.subTest(origin=origin):
                item = resolve(**kw).values["agents.cap"]
                self.assertEqual((item.value, item.origin), (value, origin))

    def test_a_repo_entry_applies_only_to_its_repo(self):
        project = {"repo": [{"name": "app", "agents": {"runtime": "opencode"}}, {"name": "lib"}]}
        self.assertEqual(resolve(project=project, repo="app")["agents.runtime"], "opencode")
        self.assertEqual(resolve(project=project, repo="lib")["agents.runtime"], "omp")
        self.assertEqual(resolve(project=project)["agents.runtime"], "omp")

    def test_env_values_are_parsed_by_type(self):
        r = resolve(env={"CORRAL_GIT_COMMITS_CONVENTIONAL": "no", "CORRAL_AGENTS_CAP": "3"})
        self.assertIs(r["git.commits.conventional"], False)
        self.assertEqual(r["agents.cap"], 3)

    def test_an_empty_env_value_counts_as_unset(self):
        self.assertEqual(
            resolve(env={"CORRAL_AGENTS_CAP": ""}).values["agents.cap"].origin, S.DEFAULT
        )

    def test_a_none_flag_counts_as_unset(self):
        self.assertEqual(resolve(flags={"agents.cap": None})["agents.cap"], 1)


class Limits(unittest.TestCase):
    def test_a_value_above_the_host_limit_is_clamped_and_marked(self):
        r = resolve(host={"limits": {"agents": {"cap": 3}}}, project={"agents": {"cap": 5}})
        item = r.values["agents.cap"]
        self.assertEqual((item.value, item.origin, item.clamped_from), (3, S.PROJECT, 5))

    def test_the_limit_holds_against_env_and_flags_too(self):
        host = {"limits": {"agents": {"cap": 2}}}
        self.assertEqual(resolve(host=host, env={"CORRAL_AGENTS_CAP": "9"})["agents.cap"], 2)
        self.assertEqual(resolve(host=host, flags={"agents.cap": 9})["agents.cap"], 2)

    def test_a_value_within_the_limit_is_untouched(self):
        item = resolve(
            host={"limits": {"agents": {"cap": 3}}}, project={"agents": {"cap": 2}}
        ).values["agents.cap"]
        self.assertEqual((item.value, item.clamped_from), (2, None))

    def test_a_limit_on_a_non_numeric_setting_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, "not a numeric setting"):
            resolve(host={"limits": {"agents": {"runtime": "omp"}}})


class Errors(unittest.TestCase):
    def test_an_unknown_key_names_the_file_and_key(self):
        with self.assertRaisesRegex(S.SettingsError, r"corral.toml: unknown setting 'agents.cpa'"):
            resolve(project={"agents": {"cpa": 2}})

    def test_a_wrong_type_is_an_error(self):
        for value in ("2", True, 2.0):
            with self.subTest(value=value), self.assertRaises(S.SettingsError):
                resolve(project={"agents": {"cap": value}})

    def test_bad_env_values_are_errors(self):
        for env in ({"CORRAL_AGENTS_CAP": "two"}, {"CORRAL_GIT_COMMITS_CONVENTIONAL": "maybe"}):
            with self.subTest(env=env), self.assertRaises(S.SettingsError):
                resolve(env=env)

    def test_repo_entries_are_validated(self):
        bad = [
            ({"repo": [{"agents": {"cap": 2}}]}, "needs a name"),
            ({"repo": [{"name": "a"}, {"name": "a"}]}, "appears twice"),
            ({"repo": {"name": "a"}}, "array of tables"),
        ]
        for project, message in bad:
            with self.subTest(message=message), self.assertRaisesRegex(S.SettingsError, message):
                resolve(project=project)

    def test_an_unknown_repo_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, "no \\[\\[repo\\]\\] named 'x'"):
            resolve(project={"repo": [{"name": "app"}]}, repo="x")

    def test_repo_names_lists_entries_in_order(self):
        with tempfile.TemporaryDirectory(prefix="corral-settings-") as root:
            with open(os.path.join(root, "corral.toml"), "w", encoding="utf-8") as handle:
                handle.write('[[repo]]\nname = "b"\n[[repo]]\nname = "a"\n')
            self.assertEqual(S.repo_names(root), ["b", "a"])

    def test_repo_names_is_empty_without_root_or_file(self):
        self.assertEqual(S.repo_names(None), [])
        with tempfile.TemporaryDirectory(prefix="corral-settings-") as root:
            self.assertEqual(S.repo_names(root), [])

    def test_repo_names_validates_like_resolve(self):
        with tempfile.TemporaryDirectory(prefix="corral-settings-") as root:
            with open(os.path.join(root, "corral.toml"), "w", encoding="utf-8") as handle:
                handle.write('[[repo]]\nname = "a"\n[[repo]]\nname = "a"\n')
            with self.assertRaisesRegex(S.SettingsError, "appears twice"):
                S.repo_names(root)

    def test_a_setting_cannot_be_registered_twice_or_with_a_bad_default(self):
        with self.assertRaises(ValueError):
            S.register("agents.cap", int, 1, "again")
        with self.assertRaises(ValueError):
            S.register("x.y", int, "1", "bad default")
        self.assertNotIn("x.y", S.REGISTRY)


class Strictness(unittest.TestCase):
    def test_every_repo_entry_is_checked_not_only_the_selected_one(self):
        for entry in ({"name": "a", "bogus": 1}, {"name": "a", "agents": {"cap": "x"}}):
            with self.subTest(entry=entry), self.assertRaisesRegex(S.SettingsError, "repo"):
                resolve(project={"repo": [entry, {"name": "b"}]}, repo="b")

    def test_repo_errors_name_the_entry(self):
        with self.assertRaisesRegex(S.SettingsError, "number 2 needs a name"):
            resolve(project={"repo": [{"name": "a"}, {}]})
        with self.assertRaisesRegex(S.SettingsError, "'a' appears twice"):
            resolve(project={"repo": [{"name": "a"}, {"name": "a"}]})

    def test_a_key_set_twice_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, "agents.cap is set twice"):
            resolve(project={"agents.cap": 3, "agents": {"cap": 5}})

    def test_an_empty_table_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, "empty table"):
            resolve(project={"agents": {"cap": {}}})

    def test_env_ints_are_plain_digits(self):
        for raw in ("1_0", "\u0663", "+3", "0x3"):
            with self.subTest(raw=raw), self.assertRaises(S.SettingsError):
                resolve(env={"CORRAL_AGENTS_CAP": raw})
        self.assertEqual(resolve(env={"CORRAL_AGENTS_CAP": " 3 "})["agents.cap"], 3)

    def test_a_ceiling_must_not_be_negative(self):
        with self.assertRaisesRegex(S.SettingsError, "0 or more"):
            resolve(host={"limits": {"agents": {"cap": -1}}})


class Files(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config = os.path.join(tmp.name, "config")
        os.makedirs(self.config)
        self.root = os.path.join(tmp.name, "ops")
        os.makedirs(os.path.join(self.root, "sub", "deeper"))

    def write(self, path, text):
        with open(path, "w") as handle:
            handle.write(text)

    def test_load_reads_both_files(self):
        self.write(os.path.join(self.config, "config.toml"), "[limits.agents]\ncap = 2\n")
        self.write(os.path.join(self.root, "corral.toml"), "[agents]\ncap = 4\n")
        r = S.load(root=self.root, env={"CORRAL_CONFIG": self.config})
        self.assertEqual((r["agents.cap"], r.values["agents.cap"].clamped_from), (2, 4))

    def test_missing_files_mean_defaults(self):
        r = S.load(root=self.root, env={"CORRAL_CONFIG": self.config})
        self.assertEqual(r["agents.cap"], 1)

    def test_a_malformed_file_names_its_path(self):
        path = os.path.join(self.root, "corral.toml")
        self.write(path, "[agents\n")
        with self.assertRaisesRegex(S.SettingsError, "corral.toml"):
            S.load(root=self.root, env={"CORRAL_CONFIG": self.config})

    def test_a_file_that_is_not_utf8_is_a_settings_error(self):
        with open(os.path.join(self.root, "corral.toml"), "wb") as handle:
            handle.write(b"[agents]\ncap = 2 # \xff\n")
        with self.assertRaisesRegex(S.SettingsError, "corral.toml"):
            S.load(root=self.root, env={"CORRAL_CONFIG": self.config})

    def test_find_root_walks_up_to_the_nearest_corral_toml(self):
        self.write(os.path.join(self.root, "corral.toml"), "")
        self.assertEqual(S.find_root(os.path.join(self.root, "sub", "deeper")), self.root)
        self.assertIsNone(S.find_root(self.tmp))


class ConfigShow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        with open(os.path.join(self.root, "corral.toml"), "w") as handle:
            handle.write('[agents]\ncap = 3\n\n[[repo]]\nname = "app"\n[repo.agents]\ncap = 2\n')
        env = mock.patch.dict(os.environ, {"CORRAL_CONFIG": os.path.join(tmp.name, "none")})
        env.start()
        self.addCleanup(env.stop)
        for setting in S.REGISTRY.values():
            os.environ.pop(setting.env, None)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["config", "show", "--root", self.root, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_show_lists_every_setting(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("agents.cap = 3\n", out)
        self.assertEqual(len(out.splitlines()), len(S.REGISTRY))

    def test_origin_and_repo(self):
        code, out, _ = self.run_cli("--origin", "--repo", "app")
        self.assertEqual(code, 0)
        self.assertIn("agents.cap = 2  # repo\n", out)
        self.assertIn("agents.runtime = 'omp'  # default\n", out)

    def test_a_settings_error_exits_4(self):
        code, _, err = self.run_cli("--repo", "nope")
        self.assertEqual(code, 4)
        self.assertIn("no [[repo]] named 'nope'", err)

    def test_a_root_without_corral_toml_exits_4(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["config", "show", "--root", os.path.join(self.root, "typo")])
        self.assertEqual(code, 4)
        self.assertIn("no corral.toml", err.getvalue())

    def test_repo_without_a_root_exits_4(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch.object(S, "find_root", return_value=None),
        ):
            code = cli.main(["config", "show", "--repo", "app"])
        self.assertEqual(code, 4)
        self.assertIn("needs an ops root", err.getvalue())


if __name__ == "__main__":
    unittest.main()
