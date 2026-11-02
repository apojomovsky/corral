"""The README Commands table names every `corral` verb, and no phantom ones.

Pure: builds the argparse parser the way `cli.main` does and reads
`README.md` off disk. Building the parser constructs the production seams
but never invokes them, so no subprocess runs and no socket opens. The
snapshot document is built from plain data with a fake disk-usage
function, so it reads no real state either.
"""

import argparse
import dataclasses
import re
import unittest
from pathlib import Path

from corral import cli, host_ledger, pressure, prompts, resources, runtimes, snapshot

SPAN_RE = re.compile(r"`([^`]*)`")
COMMAND_RE = re.compile(r"\bcorral ([a-z][a-z0-9-]*)")
ROOT = Path(__file__).resolve().parent.parent


def subcommands():
    """Every top-level verb, from the same parser `main` runs."""
    parser = cli.build_parser()
    subs = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return set(subs.choices)


def commands_section():
    """The Commands table, from its heading to the next section."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return text.split("## Commands", 1)[1].split("## ", 1)[0]


def table_tokens(section):
    """Every `corral <name>` token in a Commands-table Command cell."""
    found = set()
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = re.split(r"(?<!\\)\|", line)
        if len(cells) < 3 or "corral " not in cells[1]:
            continue
        for span in SPAN_RE.findall(cells[1]):
            found.update(COMMAND_RE.findall(span))
    return found


def verb_help(name):
    """The `help=` text of one top-level verb, from the parser `main` runs."""
    parser = cli.build_parser()
    subs = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    for choice in subs._choices_actions:
        if choice.dest == name:
            return choice.help
    raise AssertionError(f"no verb {name!r}")


def table_row(section, verb):
    """The description cell of one verb's Commands-table row."""
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = re.split(r"(?<!\\)\|", line)
        if len(cells) < 3:
            continue
        tokens = set()
        for span in SPAN_RE.findall(cells[1]):
            tokens.update(COMMAND_RE.findall(span))
        if tokens == {verb}:
            return cells[2]
    raise AssertionError(f"no Commands-table row for {verb!r}")


def sections_named(text):
    """The snapshot sections a prose list names, after its colon.

    The list runs to the first semicolon (a table description continues
    past it) or to the end of the text (a help string does not).
    """
    listed = text.split(":", 1)[1].split(";", 1)[0]
    words = listed.replace(",", " ").split()
    named = set()
    for word in words:
        if word in ("and", "the", "section"):
            continue
        named.add("disk" if word == "disks" else word)
    return named


def document_sections():
    """The snapshot document's section keys, without its envelope keys."""
    doc = snapshot.build(
        now=0.0,
        host="host-1",
        registry_view=[],
        quota_view={},
        settings={
            "admission.total_cap": 1,
            "admission.repo_cap": 1,
            "registry.count_toward_cap": True,
        },
        usage_fn=lambda path: (0, 0, 0),
        drive_view={},
        resources_view={},
    )
    return set(doc) - {"schema_version", "fleet_header"}


def readme_text():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def runtimes_bullet():
    """The Headless runtimes bullet, the line that must name every runtime."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if "Headless runtimes" in line]
    return "\n".join(lines)


class RegisteredRuntimes(unittest.TestCase):
    def test_readme_names_every_registered_runtime(self):
        bullet = runtimes_bullet()
        self.assertTrue(bullet, "no Headless runtimes bullet in README.md")
        missing = [
            name for name in runtimes.NAMES if not re.search(rf"\b{re.escape(name)}\b", bullet)
        ]
        self.assertEqual(missing, [])


class Table(unittest.TestCase):
    def test_every_subcommand_has_a_row(self):
        missing = sorted(subcommands() - table_tokens(commands_section()))
        self.assertEqual(missing, [])

    def test_every_token_is_a_subcommand(self):
        phantoms = sorted(table_tokens(commands_section()) - subcommands())
        self.assertEqual(phantoms, [])

    def test_a_mention_in_a_description_is_not_a_row(self):
        section = (
            "| Command | What it does |\n"
            "| --- | --- |\n"
            "| `corral deps` | check the host programs, and the `corral quota` ledger |\n"
        )
        self.assertEqual(table_tokens(section), {"deps"})

    def test_a_prose_line_after_the_table_is_not_a_row(self):
        section = (
            "| Command | What it does |\n"
            "| --- | --- |\n"
            "| `corral deps` | check the host programs |\n"
            "\n"
            "Compare a | b with `corral nosuchverb` | x for details.\n"
        )
        self.assertEqual(table_tokens(section), {"deps"})


class SnapshotSections(unittest.TestCase):
    def test_document_keys_match_the_error_sections(self):
        self.assertEqual(document_sections(), set(snapshot.ERROR_SECTIONS))

    def test_help_and_readme_list_the_document_sections(self):
        sections = document_sections()
        self.assertEqual(sections_named(verb_help("snapshot")), sections)
        self.assertEqual(sections_named(table_row(commands_section(), "snapshot")), sections)


class ResourceSettings(unittest.TestCase):
    """The README documents every resources key the code accepts."""

    def missing(self, tokens):
        text = readme_text()
        return sorted(token for token in tokens if token not in text)

    def test_host_budget_and_role_charges(self):
        tokens = {"[resources.host]", *resources.DIMS}
        tokens.update(f"[resources.role.{role}]" for role in prompts.ROLES)
        self.assertEqual(self.missing(tokens), [])

    def test_project_share_keys(self):
        tokens = {field.name for field in dataclasses.fields(resources.Share)}
        self.assertEqual(self.missing(tokens), [])

    def test_pressure_and_foreign_claim_keys(self):
        tokens = {field.name for field in dataclasses.fields(resources.PressureCaps)}
        tokens.update(host_ledger.CLAIM_KEYS)
        tokens.update(("resources.gone_s", host_ledger.FOREIGN_CLAIMS_DIR, "<name>.json"))
        self.assertEqual(self.missing(tokens), [])

    def test_repeated_default_numbers_match_the_code(self):
        text = readme_text()
        self.assertIn(f"(default {int(resources.DEFAULT_GONE_S)})", text)
        self.assertIn(f"(default {pressure.DEFAULT_MAX_LOAD_PER_CPU})", text)
        self.assertIn(f"(default {pressure.DEFAULT_MIN_MEM_AVAILABLE_PCT})", text)


if __name__ == "__main__":
    unittest.main()
