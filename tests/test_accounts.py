"""The accounts file and its quota readers, against a temp config dir.

Fake readers live here, in the test tree: nothing real, and no provider
names beyond provider-a/provider-b.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral import accounts, cli, deps


class OkReader:
    """A reader that reports one window."""

    def read(self, now):
        return accounts.Reading(windows={"weekly": accounts.Window(used_pct=10.0, resets_at=None)})

    def parse_limit_error(self, text, now):
        return None


class FailingReader:
    """A reader whose read fails at call time (not a configuration error)."""

    def read(self, now):
        raise accounts.ReaderError("provider-a: quota endpoint refused")

    def parse_limit_error(self, text, now):
        return None


class BrokenInitReader:
    """A reader that cannot even be built (a configuration error)."""

    def __init__(self):
        raise RuntimeError("missing argument")


def write_config(directory, text):
    with open(os.path.join(directory, "accounts.toml"), "w", encoding="utf-8") as handle:
        handle.write(text)
    return directory


def config_with(text):
    directory = tempfile.mkdtemp(prefix="corral-accounts-")
    return write_config(directory, text)


OK_FILE = """\
[[provider]]
name = "provider-a"
reader = "tests.test_accounts:OkReader"
"""

FAIL_FILE = """\
[[provider]]
name = "provider-a"
reader = "tests.test_accounts:FailingReader"
"""


class MissingFile(unittest.TestCase):
    def test_a_missing_file_is_an_empty_pool(self):
        with tempfile.TemporaryDirectory(prefix="corral-accounts-") as directory:
            self.assertEqual(accounts.providers(directory), [])
            self.assertEqual(accounts.check(directory), [])

    def test_providers_defaults_to_the_config_dir(self):
        with tempfile.TemporaryDirectory(prefix="corral-accounts-") as directory:
            write_config(directory, OK_FILE)
            with mock.patch.dict(os.environ, {"CORRAL_CONFIG": directory}):
                found = accounts.providers()
        self.assertEqual([provider.name for provider in found], ["provider-a"])


class Loading(unittest.TestCase):
    def test_minimal_entries_take_the_defaults(self):
        found = accounts.providers(config_with(OK_FILE))
        self.assertEqual(len(found), 1)
        provider = found[0]
        self.assertEqual(provider.name, "provider-a")
        self.assertEqual(provider.reader, "tests.test_accounts:OkReader")
        self.assertEqual(provider.cooldown_s, 3600.0)
        self.assertEqual(provider.stale_after_s, 900.0)

    def test_durations_take_other_units(self):
        text = """\
[[provider]]
name = "provider-a"
reader = "tests.test_accounts:OkReader"
cooldown = "90m"
stale_after = "2h"
"""
        (provider,) = accounts.providers(config_with(text))
        self.assertEqual(provider.cooldown_s, 90 * 60.0)
        self.assertEqual(provider.stale_after_s, 2 * 3600.0)

    def test_durations_take_bare_seconds(self):
        text = """\
[[provider]]
name = "provider-a"
reader = "tests.test_accounts:OkReader"
cooldown = 30
stale_after = 45
"""
        (provider,) = accounts.providers(config_with(text))
        self.assertEqual(provider.cooldown_s, 30.0)
        self.assertEqual(provider.stale_after_s, 45.0)

    def test_two_providers_load_in_order(self):
        text = """\
[[provider]]
name = "provider-a"
reader = "tests.test_accounts:OkReader"

[[provider]]
name = "provider-b"
reader = "tests.test_accounts:OkReader"
cooldown = "2h"
"""
        found = accounts.providers(config_with(text))
        self.assertEqual([provider.name for provider in found], ["provider-a", "provider-b"])
        self.assertEqual(found[1].cooldown_s, 2 * 3600.0)


class BadFiles(unittest.TestCase):
    def test_bad_toml_is_a_configuration_error(self):
        directory = config_with("[[provider]\nname = \n")
        with self.assertRaises(accounts.AccountsError):
            accounts.providers(directory)

    def test_a_singular_provider_table_is_a_configuration_error(self):
        directory = config_with('[provider]\nname = "provider-a"\n')
        with self.assertRaises(accounts.AccountsError):
            accounts.providers(directory)
        self.assertTrue(accounts.check(directory))

    def test_duplicate_names_are_a_configuration_error(self):
        text = """\
[[provider]]
name = "provider-a"
reader = "tests.test_accounts:OkReader"

[[provider]]
name = "provider-a"
reader = "tests.test_accounts:OkReader"
"""
        directory = config_with(text)
        with self.assertRaises(accounts.AccountsError):
            accounts.providers(directory)
        problems = accounts.check(directory)
        self.assertTrue(problems)
        self.assertIn("provider-a", problems[0])

    def test_a_bad_duration_is_a_configuration_error(self):
        text = OK_FILE + 'cooldown = "soon"\n'
        with self.assertRaises(accounts.AccountsError):
            accounts.providers(config_with(text))

    def test_a_negative_duration_is_a_configuration_error(self):
        text = OK_FILE + 'cooldown = "-1h"\n'
        with self.assertRaises(accounts.AccountsError):
            accounts.providers(config_with(text))

    def test_a_missing_name_is_a_configuration_error(self):
        text = '[[provider]]\nreader = "tests.test_accounts:OkReader"\n'
        with self.assertRaises(accounts.AccountsError):
            accounts.providers(config_with(text))

    def test_an_unknown_key_is_a_configuration_error(self):
        text = OK_FILE + "cooldown_s = 60\n"
        with self.assertRaises(accounts.AccountsError):
            accounts.providers(config_with(text))


class Readers(unittest.TestCase):
    def test_load_reader_builds_the_named_class(self):
        reader = accounts.load_reader("tests.test_accounts:OkReader")
        reading = reader.read(1700000000.0)
        self.assertEqual(reading.windows["weekly"].used_pct, 10.0)
        self.assertIsNone(reader.parse_limit_error("nope", 1700000000.0))

    def test_load_reader_rejects_an_entry_without_a_colon(self):
        with self.assertRaises(accounts.AccountsError):
            accounts.load_reader("tests.test_accounts")

    def test_load_reader_rejects_a_missing_module(self):
        with self.assertRaises(accounts.AccountsError):
            accounts.load_reader("no.such.module:Reader")

    def test_load_reader_rejects_a_missing_class(self):
        with self.assertRaises(accounts.AccountsError):
            accounts.load_reader("tests.test_accounts:NoSuchReader")

    def test_load_reader_rejects_a_failing_constructor(self):
        with self.assertRaises(accounts.AccountsError):
            accounts.load_reader("tests.test_accounts:BrokenInitReader")

    def test_a_reader_error_is_not_a_configuration_error(self):
        reader = accounts.load_reader("tests.test_accounts:FailingReader")
        with self.assertRaises(accounts.ReaderError):
            reader.read(1700000000.0)

    def test_check_imports_but_never_calls_read(self):
        self.assertEqual(accounts.check(config_with(OK_FILE)), [])
        self.assertEqual(accounts.check(config_with(FAIL_FILE)), [])

    def test_check_names_a_reader_that_does_not_import(self):
        text = OK_FILE.replace("tests.test_accounts:OkReader", "no.such.module:Reader")
        problems = accounts.check(config_with(text))
        self.assertEqual(len(problems), 1)
        self.assertIn("provider-a", problems[0])

    def test_a_module_that_raises_at_import_is_a_configuration_error(self):
        name = "corral_boom_reader_for_test"
        directory = tempfile.mkdtemp(prefix="corral-accounts-")
        with open(os.path.join(directory, f"{name}.py"), "w", encoding="utf-8") as handle:
            handle.write("raise RuntimeError('boom at import')\n")
        sys.path.insert(0, directory)
        try:
            with self.assertRaises(accounts.AccountsError):
                accounts.load_reader(f"{name}:Reader")
            text = OK_FILE.replace("tests.test_accounts:OkReader", f"{name}:Reader")
            problems = accounts.check(config_with(text))
            self.assertEqual(len(problems), 1)
            self.assertIn("provider-a", problems[0])
        finally:
            sys.path.remove(directory)
            sys.modules.pop(name, None)


class ExitCode(unittest.TestCase):
    def run_deps(self, config_text):
        outputs = {d.probe[0]: f"{d.name} 1.0 Linger=yes systemd 255" for d in deps.load()}
        outputs["uv"] = "uv 0.13.5"

        def probe(argv, paths):
            return outputs.get(argv[0])

        with tempfile.TemporaryDirectory(prefix="corral-accounts-") as directory:
            write_config(directory, config_text)
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, {"CORRAL_CONFIG": directory}),
                mock.patch.object(deps, "run_probe", probe),
                redirect_stdout(out),
                mock.patch("sys.stderr", err),
            ):
                code = cli.main(["deps"])
        return code, out.getvalue(), err.getvalue()

    def test_a_broken_accounts_file_fails_deps_with_exit_4(self):
        text = OK_FILE + OK_FILE.replace("tests.test_accounts:OkReader", "x:y")
        code, out, err = self.run_deps(text)
        self.assertEqual(code, 4)
        self.assertIn("provider-a", err)

    def test_a_reader_that_does_not_import_fails_deps_with_exit_4(self):
        text = OK_FILE.replace("tests.test_accounts:OkReader", "no.such.module:Reader")
        code, out, err = self.run_deps(text)
        self.assertEqual(code, 4)
        self.assertIn("provider-a", err)

    def test_a_good_accounts_file_keeps_deps_at_0(self):
        code, out, err = self.run_deps(OK_FILE)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
