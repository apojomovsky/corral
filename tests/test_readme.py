"""The README Commands table names every `corral` verb, and no phantom ones.

Pure: builds the argparse parser the way `cli.main` does and reads
`README.md` off disk. Building the parser constructs the production seams
but never invokes them, so no subprocess runs and no socket opens.
"""

import argparse
import re
import unittest
from pathlib import Path

from corral import cli

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


if __name__ == "__main__":
    unittest.main()
