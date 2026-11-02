"""Agent profiles, ticket 1: declarations and validation.

Offline: profiles are plain data checked against the account pool and each
runtime adapter's options schema. No files, no subprocesses, no network.
"""

import unittest
from unittest import mock

from corral import models, profiles, runtimes, settings


def raw_profile(runtime="muse", **overrides):
    table = {"runtime": runtime}
    table.update(overrides)
    return table


def fake_adapter(schema):
    adapter = mock.Mock()
    adapter.options_schema.return_value = dict(schema)
    return adapter


STR_SCHEMA = {"reasoning_effort": (str, "how hard the model thinks")}


class DocExample(unittest.TestCase):
    def test_the_section_1_shape_loads(self):
        declarations = {
            "default": {
                "runtime": "muse",
                "models": {
                    "default": {"chain": ["provider-a/model-x", "provider-b/model-y"]},
                    "reviewer": {"chain": ["provider-a/model-x"]},
                },
                "options": {"reasoning_effort": "high"},
            }
        }
        with mock.patch.object(runtimes, "get", return_value=fake_adapter(STR_SCHEMA)):
            parsed = profiles.validate(declarations, ["provider-a", "provider-b"])
        profile = parsed["default"]
        self.assertEqual(profile.name, "default")
        self.assertEqual(profile.runtime, "muse")
        self.assertEqual(
            profile.models,
            {
                "default": ["provider-a/model-x", "provider-b/model-y"],
                "reviewer": ["provider-a/model-x"],
            },
        )
        self.assertEqual(profile.options, {"reasoning_effort": "high"})

    def test_a_runtime_only_profile_loads_with_empty_models_and_options(self):
        parsed = profiles.validate({"slim": raw_profile("omp")}, ["provider-a"])
        self.assertEqual(parsed["slim"].models, {})
        self.assertEqual(parsed["slim"].options, {})

    def test_option_scalars_cover_strings_numbers_and_booleans(self):
        schema = {
            "a": (str, "a string"),
            "b": (int, "a count"),
            "c": (float, "a ratio"),
            "d": (bool, "a switch"),
        }
        declarations = {"p": raw_profile("omp", options={"a": "x", "b": 2, "c": 1.5, "d": True})}
        with mock.patch.object(runtimes, "get", return_value=fake_adapter(schema)):
            parsed = profiles.validate(declarations, ["provider-a"])
        self.assertEqual(parsed["p"].options, {"a": "x", "b": 2, "c": 1.5, "d": True})

    def test_an_int_option_also_satisfies_a_float_schema(self):
        declarations = {"p": raw_profile("omp", options={"c": 2})}
        with mock.patch.object(
            runtimes, "get", return_value=fake_adapter({"c": (float, "a ratio")})
        ):
            parsed = profiles.validate(declarations, ["provider-a"])
        self.assertEqual(parsed["p"].options, {"c": 2})

    def test_a_bool_is_not_a_number_for_option_types(self):
        declarations = {"p": raw_profile("omp", options={"b": True})}
        with mock.patch.object(runtimes, "get", return_value=fake_adapter({"b": (int, "a count")})):
            with self.assertRaises(settings.SettingsError):
                profiles.validate(declarations, ["provider-a"])


class BadNames(unittest.TestCase):
    def test_names_outside_lowercase_digits_and_dashes_are_refused(self):
        for name in ("", "Default", "FAST", "a_b", "a b", "a.b", "a/b", "x!"):
            with self.subTest(name=name), self.assertRaises(settings.SettingsError) as ctx:
                profiles.validate({name: raw_profile("omp")}, ["provider-a"])
            self.assertIn(name, str(ctx.exception))

    def test_a_non_table_declarations_value_is_refused(self):
        for declarations in (None, [], [("default", {})], "default"):
            with self.subTest(declarations=declarations), self.assertRaises(settings.SettingsError):
                profiles.validate(declarations, ["provider-a"])

    def test_a_non_table_profile_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            profiles.validate({"default": "muse"}, ["provider-a"])


class BadShapes(unittest.TestCase):
    def test_a_missing_runtime_is_refused_naming_the_profile(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.validate({"default": {}}, ["provider-a"])
        self.assertIn("default", str(ctx.exception))

    def test_a_non_string_runtime_is_refused(self):
        for runtime in ("", "OMP", "claude-code", 123, None):
            with self.subTest(runtime=runtime), self.assertRaises(settings.SettingsError) as ctx:
                profiles.validate({"default": raw_profile(runtime)}, ["provider-a"])
            self.assertIn("default", str(ctx.exception))
            self.assertIn(repr(runtime), str(ctx.exception))

    def test_an_unknown_key_beside_runtime_models_options_is_refused(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.validate(
                {"default": raw_profile("omp", model="provider-a/model-x")},
                ["provider-a"],
            )
        self.assertIn("model", str(ctx.exception))

    def test_bad_chain_shapes_are_refused(self):
        bad = [
            {"chain": "provider-a/model-x"},
            {"chain": []},
            {"chain": [1]},
            {"chain": [""]},
            {"chain": ["provider-a/model-x"], "other": 1},
            {},
        ]
        for models_table in bad:
            with self.subTest(models_table=models_table), self.assertRaises(settings.SettingsError):
                profiles.validate(
                    {"default": raw_profile("omp", models={"default": models_table})},
                    ["provider-a"],
                )

    def test_an_empty_role_name_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            profiles.validate(
                {"default": raw_profile("omp", models={"": {"chain": ["provider-a/model-x"]}})},
                ["provider-a"],
            )


class ChainPool(unittest.TestCase):
    def test_an_unknown_provider_is_exit_4_naming_profile_role_and_entry(self):
        with self.assertRaises(models.ModelsError) as ctx:
            profiles.validate(
                {
                    "default": raw_profile(
                        "omp",
                        models={"reviewer": {"chain": ["provider-b/model-x"]}},
                    )
                },
                ["provider-a"],
            )
        self.assertIsInstance(ctx.exception, settings.SettingsError)
        self.assertIn("default", str(ctx.exception))
        self.assertIn("reviewer", str(ctx.exception))
        self.assertIn("provider-b/model-x", str(ctx.exception))

    def test_a_model_id_starting_with_a_dash_is_refused(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.validate(
                {"default": raw_profile("omp", models={"default": {"chain": ["-provider-a/x"]}})},
                ["provider-a"],
            )
        self.assertIn("-provider-a/x", str(ctx.exception))


class OptionKeys(unittest.TestCase):
    def test_an_unknown_option_key_is_exit_4_naming_runtime_and_key(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.validate(
                {"default": raw_profile("muse", options={"bogus_key": "high"})},
                ["provider-a"],
            )
        self.assertIn("muse", str(ctx.exception))
        self.assertIn("bogus_key", str(ctx.exception))

    def test_a_non_table_options_value_is_refused(self):
        for options in ("high", [("reasoning_effort", "high")], None):
            with self.subTest(options=options), self.assertRaises(settings.SettingsError):
                profiles.validate({"default": raw_profile("omp", options=options)}, ["provider-a"])

    def test_nested_option_values_are_refused(self):
        for value in ({"nested": True}, ["high"], None):
            with self.subTest(value=value), self.assertRaises(settings.SettingsError) as ctx:
                profiles.validate(
                    {"default": raw_profile("omp", options={"k": value})},
                    ["provider-a"],
                )
            self.assertIn("k", str(ctx.exception))

    def test_a_wrong_option_type_is_refused_naming_the_key(self):
        declarations = {"p": raw_profile("omp", options={"a": 2})}
        with mock.patch.object(runtimes, "get", return_value=fake_adapter(STR_SCHEMA)):
            with self.assertRaises(settings.SettingsError) as ctx:
                profiles.validate(declarations, ["provider-a"])
        self.assertIn("a", str(ctx.exception))


class NoSilentFallback(unittest.TestCase):
    def test_validate_returns_no_default_profile_unless_declared(self):
        self.assertEqual(profiles.validate({}, ["provider-a"]), {})
        parsed = profiles.validate({"slim": raw_profile("omp")}, ["provider-a"])
        self.assertEqual(set(parsed), {"slim"})

    def test_looking_up_an_undeclared_name_fails_instead_of_substituting(self):
        parsed = profiles.validate({"slim": raw_profile("omp")}, ["provider-a"])
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.lookup(parsed, "default")
        self.assertIn("default", str(ctx.exception))

    def test_looking_up_anything_in_empty_declarations_fails(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.lookup({}, "default")
        self.assertIn("default", str(ctx.exception))


class BuiltinDefault(unittest.TestCase):
    def test_the_builtin_default_is_the_shipped_muse_setup(self):
        self.assertEqual(profiles.DEFAULT_PROFILE_NAME, "default")
        self.assertEqual(profiles.BUILTIN_DEFAULT.name, "default")
        self.assertEqual(profiles.BUILTIN_DEFAULT.runtime, "muse")
        self.assertIn(profiles.BUILTIN_DEFAULT.runtime, runtimes.NAMES)

    def test_the_builtin_default_validates_cleanly(self):
        raw = {
            profiles.BUILTIN_DEFAULT.name: {
                "runtime": profiles.BUILTIN_DEFAULT.runtime,
                "models": {
                    role: {"chain": list(chain)}
                    for role, chain in profiles.BUILTIN_DEFAULT.models.items()
                },
                "options": dict(profiles.BUILTIN_DEFAULT.options),
            }
        }
        parsed = profiles.validate(raw, ["provider-a", "provider-b"])
        self.assertEqual(parsed["default"], profiles.BUILTIN_DEFAULT)


class AdapterSchemas(unittest.TestCase):
    def test_every_runtime_adapter_declares_a_read_only_options_schema(self):
        for name in runtimes.NAMES:
            with self.subTest(runtime=name):
                adapter = runtimes.get(name)
                self.assertTrue(callable(adapter.options_schema))
                schema = adapter.options_schema()
                self.assertIsInstance(schema, dict)
                self.assertTrue(schema, f"{name} options schema must not be a stub")
                for key, entry in schema.items():
                    self.assertIsInstance(key, str)
                    self.assertTrue(key)
                    want = entry[0] if isinstance(entry, tuple) else entry
                    self.assertIn(want, (str, int, float, bool))
                    if isinstance(entry, tuple):
                        self.assertEqual(len(entry), 2)
                        self.assertIsInstance(entry[1], str)
                        self.assertTrue(entry[1])

    def test_an_unknown_option_key_fails_for_every_real_runtime(self):
        for name in runtimes.NAMES:
            with self.subTest(runtime=name):
                with self.assertRaises(settings.SettingsError) as ctx:
                    profiles.validate(
                        {name: raw_profile(name, options={"bogus_key": "x"})},
                        ["provider-a"],
                    )
                self.assertIn(name, str(ctx.exception))
                self.assertIn("bogus_key", str(ctx.exception))

    def test_each_real_schema_accepts_its_own_keys_with_the_right_types(self):
        good = {
            "omp": {"auto_approve": False},
            "opencode": {"bash_timeout_ms": 60000},
            "muse": {"reasoning_effort": "high", "parallel_tool_calls": True},
        }
        for name, options in good.items():
            with self.subTest(runtime=name):
                parsed = profiles.validate(
                    {name: raw_profile(name, options=options)}, ["provider-a"]
                )
                self.assertEqual(parsed[name].options, options)

    def test_each_real_schema_refuses_a_wrong_option_type(self):
        bad = {
            "omp": {"auto_approve": "yes"},
            "opencode": {"bash_timeout_ms": True},
            "muse": {"reasoning_effort": 2},
        }
        for name, options in bad.items():
            with self.subTest(runtime=name):
                with self.assertRaises(settings.SettingsError) as ctx:
                    profiles.validate({name: raw_profile(name, options=options)}, ["provider-a"])
                self.assertIn(next(iter(options)), str(ctx.exception))
