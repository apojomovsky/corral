"""The reference docs: `docs/commands.md` and `docs/configuration.md`.

`commands.md` holds the full command reference (every verb, flag and exit
code); `configuration.md` holds one place per setting, with its table
generated from the settings registry. Reading the files and building the
parsers touches nothing outside the tree: no subprocess, no network, no
state dir.
"""

import argparse
import re
import unittest
from pathlib import Path

from korapy import cli, settings, snapshot, takeoff

COMMANDS = Path(__file__).resolve().parent.parent / "docs" / "commands.md"
CONFIGURATION = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"
README = Path(__file__).resolve().parent.parent / "README.md"

SPAN_RE = re.compile(r"`([^`]*)`")
COMMAND_RE = re.compile(r"\bkorapy ([a-z][a-z0-9-]*)")
TABLE_START = "<!-- settings-table:start -->"
TABLE_END = "<!-- settings-table:end -->"
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
IMG_RE = re.compile(r'<img\s[^>]*src="([^"]+)"')


def subcommands():
    """Every top-level verb, from the same parser `main` runs."""
    parser = cli.build_parser()
    subs = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return set(subs.choices)


def table_tokens(text):
    """Every `korapy <name>` token in a table's Command cell."""
    found = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = re.split(r"(?<!\\)\|", line)
        if len(cells) < 3 or "korapy " not in cells[1]:
            continue
        for span in SPAN_RE.findall(cells[1]):
            found.update(COMMAND_RE.findall(span))
    return found


def table_row(text, verb):
    """The description cell of one verb's command-table row."""
    for line in text.splitlines():
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
    raise AssertionError(f"no command-table row for {verb!r}")


def render_settings_table():
    """The settings table `docs/configuration.md` embeds, from the registry.

    One row per registered setting, sorted by key: the registry owns the
    names, defaults and meanings, so the doc never hand-copies them.
    """
    settings._load_providers()
    lines = ["| Setting | Default | Meaning |", "| --- | --- | --- |"]
    for key in sorted(settings.REGISTRY):
        setting = settings.REGISTRY[key]
        lines.append(f"| `{key}` | `{setting.default!r}` | {setting.help} |")
    return "\n".join(lines) + "\n"


def embedded_settings_table(text):
    """The generated table between the markers in `configuration.md`."""
    start = text.index(TABLE_START) + len(TABLE_START)
    end = text.index(TABLE_END)
    return text[start:end].strip("\n") + "\n"


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


def heading_anchors(text):
    """Every `#anchor` a file's ATX headings define, GitHub's way."""
    anchors = set()
    for line in text.splitlines():
        match = re.match(r"#{1,6}\s+(.*)", line)
        if not match:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        anchor = heading.lower()
        anchor = re.sub(r"[^\w\s-]", "", anchor)
        anchors.add(anchor.replace(" ", "-"))
    return anchors


def local_targets(path):
    """Every relative link and image target a doc file names."""
    text = path.read_text(encoding="utf-8")
    targets = LINK_RE.findall(text) + IMG_RE.findall(text)
    return [
        target for target in targets if "://" not in target and not target.startswith("mailto:")
    ]


class CommandsTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = COMMANDS.read_text(encoding="utf-8")

    def test_every_subcommand_has_a_row(self):
        missing = sorted(subcommands() - table_tokens(self.text))
        self.assertEqual(missing, [])

    def test_every_token_is_a_subcommand(self):
        phantoms = sorted(table_tokens(self.text) - subcommands())
        self.assertEqual(phantoms, [])

    def test_a_mention_in_a_description_is_not_a_row(self):
        section = (
            "| Command | What it does |\n"
            "| --- | --- |\n"
            "| `korapy deps` | check the host programs, and the `korapy quota` ledger |\n"
        )
        self.assertEqual(table_tokens(section), {"deps"})

    def test_a_prose_line_after_the_table_is_not_a_row(self):
        section = (
            "| Command | What it does |\n"
            "| --- | --- |\n"
            "| `korapy deps` | check the host programs |\n"
            "\n"
            "Compare a | b with `korapy nosuchverb` | x for details.\n"
        )
        self.assertEqual(table_tokens(section), {"deps"})

    def test_the_reference_uses_no_em_dashes(self):
        self.assertNotIn("\u2014", self.text)


class HeavyRow(unittest.TestCase):
    """The `heavy run` row names the lane's bypass and role flags."""

    def heavy_run_row(self):
        text = COMMANDS.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("|") and "heavy run" in line:
                return re.split(r"(?<!\\)\|", line)[2]
        raise AssertionError("no heavy run row in docs/commands.md")

    def test_heavy_run_row_names_bypass_and_role(self):
        row = self.heavy_run_row()
        for token in ("--bypass", "--role", "journal", "hand"):
            self.assertIn(token, row)


class SnapshotSections(unittest.TestCase):
    def test_document_keys_match_the_error_sections(self):
        self.assertEqual(document_sections(), set(snapshot.ERROR_SECTIONS))

    def test_help_and_reference_list_the_document_sections(self):
        sections = document_sections()
        text = COMMANDS.read_text(encoding="utf-8")
        self.assertEqual(sections_named(verb_help("snapshot")), sections)
        self.assertEqual(sections_named(table_row(text, "snapshot")), sections)


class TakeoffStatuses(unittest.TestCase):
    """The takeoff paragraph names every status the gate reports."""

    def test_takeoff_status_list_names_every_status(self):
        text = COMMANDS.read_text(encoding="utf-8")
        start = text.index("Each check reports")
        sentence = text[start:].split(";", 1)[0]
        display = {
            takeoff.OK: "`ok`",
            takeoff.FAIL: "`FAIL`",
            takeoff.SKIPPED: "`skipped`",
            takeoff.PLANNED: "`planned`",
        }
        self.assertEqual(set(display), set(takeoff.STATUSES))
        for status in takeoff.STATUSES:
            self.assertIn(display[status], sentence, f"takeoff status list omits {status}")


class SettingsTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CONFIGURATION.read_text(encoding="utf-8")

    def test_the_table_matches_the_registry(self):
        self.assertEqual(embedded_settings_table(self.text), render_settings_table())

    def test_every_registry_key_appears_exactly_once(self):
        settings._load_providers()
        table = embedded_settings_table(self.text)
        for key in sorted(settings.REGISTRY):
            with self.subTest(key=key):
                self.assertEqual(table.count(f"`{key}`"), 1, f"{key} is missing or repeated")

    def test_the_reference_uses_no_em_dashes(self):
        self.assertNotIn("\u2014", self.text)


class DocLinks(unittest.TestCase):
    """Every relative link and image in the front door and the reference
    docs resolves to a file in the repository, and every `#anchor` to a
    heading in the same file."""

    FILES = (README, COMMANDS, CONFIGURATION)

    def test_every_relative_target_resolves(self):
        for path in self.FILES:
            anchors = heading_anchors(path.read_text(encoding="utf-8"))
            for target in local_targets(path):
                with self.subTest(file=path.name, target=target):
                    if target.startswith("#"):
                        self.assertIn(target[1:], anchors, f"{path.name}: no heading for {target}")
                        continue
                    file_part, hash_mark, anchor = target.partition("#")
                    resolved = (path.parent / file_part).resolve()
                    root = Path(__file__).resolve().parent.parent
                    self.assertTrue(
                        resolved.is_file(),
                        f"{path.name}: {target} resolves outside the repository",
                    )
                    self.assertTrue(
                        resolved == root or root in resolved.parents,
                        f"{path.name}: {target} resolves outside the repository",
                    )
                    if hash_mark:
                        other = heading_anchors(resolved.read_text(encoding="utf-8"))
                        self.assertIn(anchor, other, f"{path.name}: no heading for {target}")


if __name__ == "__main__":
    unittest.main()
