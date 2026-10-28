"""The accounts file and its quota readers, against a temp config dir.

Fake readers live here, in the test tree: nothing real, and no provider
names beyond provider-a/provider-b.
"""

import os
import tempfile
import unittest
from unittest import mock

from corral import accounts, cli, deps


class OkReader:
    """A reader that reports one window."""

    def read(self):
        return accounts.Reading(
            windows=(accounts.Window(id="weekly", used_fraction=0.1, resets_at=None),)
        )


class FailingReader:
    """A reader whose read fails at call time (not a configuration error)."""

    def read(self):
        raise accounts.ReaderError("provider-a: quota endpoint refused")


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
        reading = reader.read()
        self.assertEqual(reading.windows[0].id, "weekly")

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
            reader.read()

    def test_check_imports_but_never_calls_read(self):
        self.assertEqual(accounts.check(config_with(OK_FILE)), [])
        self.assertEqual(accounts.check(config_with(FAIL_FILE)), [])

    def test_check_names_a_reader_that_does_not_import(self):
        text = OK_FILE.replace("tests.test_accounts:OkReader", "no.such.module:Reader")
        problems = accounts.check(config_with(text))
        self.assertEqual(len(problems), 1)
        self.assertIn("provider-a", problems[0])


class ExitCode(unittest.TestCase):
    def test_a_configuration_error_exits_4(self):
        with mock.patch.object(
            deps, "load", side_effect=accounts.AccountsError("accounts.toml: bad")
        ):
            with mock.patch("sys.stderr", mock.Mock()):
                self.assertEqual(cli.main(["deps"]), 4)


if __name__ == "__main__":
    unittest.main()
