"""The operator's guide (`docs/guide.md`) and its parser conformance.

The guide is docs only, but every `korapy ...` invocation it shows in a
shell code block must resolve through the real parsers, so the guide
cannot drift from the CLI the way prose can. Reading the file and
building the parsers touches nothing outside the tree: no subprocess,
no network, no state dir.
"""

import argparse
import pathlib
import re
import shlex
import unittest

from korapy import cli

GUIDE = pathlib.Path(__file__).resolve().parents[1] / "docs" / "guide.md"

SHELL_LANGUAGES = ("sh", "bash", "console")
OPERATOR_RE = re.compile(r"&&|\|\||;|\|")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

EXPECTED_SECTIONS = (
    "what korapy is",
    "first run",
    "registering",
    "loop",
    "coordinator",
    "troubleshooting",
    "upgrading",
)


def _subparsers(parser):
    """The name-to-parser map of a parser's subcommands, or {} when none."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _options(parser):
    """Every long and short flag the parser accepts, to its action."""
    found = {}
    for action in parser._actions:
        for option in action.option_strings:
            found[option] = action
    return found


def _positional_choices(parser):
    """Every literal a positional argument accepts via `choices`, if any."""
    found = set()
    for action in parser._actions:
        if action.option_strings:
            continue
        if action.choices and isinstance(action.choices, (list, tuple, set)):
            found.update(str(choice) for choice in action.choices)
    return found


def shell_commands(text):
    """Every `korapy` shell segment the guide's shell blocks show.

    Only fenced sh/bash/console blocks are read. Backslash continuations
    are joined, comments are stripped, and each line is split on shell
    operators, so pipelines and `&&` chains yield one segment each.
    Redirections are cut: filenames after `>` are not CLI tokens.
    """
    commands = []
    in_block = False
    continued = ""
    for line in text.splitlines():
        if line.startswith("```"):
            language = line[3:].strip()
            in_block = language in SHELL_LANGUAGES if language else False
            continued = ""
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped.endswith("\\"):
            continued += stripped[:-1] + " "
            continue
        logical, continued = continued + line, ""
        code = re.split(r"(?:^|\s)#", logical, maxsplit=1)[0]
        for piece in OPERATOR_RE.split(code):
            piece = re.split(r"[<>]", piece, maxsplit=1)[0].strip()
            while piece.startswith("$"):
                piece = piece[1:].strip()
            if not piece:
                continue
            try:
                tokens = shlex.split(piece, posix=True)
            except ValueError as err:
                raise AssertionError(f"cannot split shell line: {logical!r}: {err}")
            while tokens and ENV_ASSIGN_RE.match(tokens[0]):
                tokens.pop(0)
            if tokens and tokens[0] == "korapy":
                commands.append(" ".join(tokens))
    return commands


def check_command(test, command, parser):
    """Walk one guide command down the parsers: verbs descend, flags exist."""
    tokens = command.split()
    test.assertEqual(tokens[0], "korapy", command)
    test.assertFalse(
        any("|" in token for token in tokens),
        f"{command}: shell alternations do not name one invocation",
    )
    current = parser
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return
        if token.startswith("-"):
            test.assertTrue(
                token.startswith("--"),
                f"{command}: {token} is not a --flag",
            )
            name, eq, _ = token.partition("=")
            options = _options(current)
            test.assertIn(name, options, f"{command}: unknown flag {name}")
            index += 1
            if eq:
                continue
            action = options[name]
            if action.nargs == 0:
                continue
            if action.nargs == "?" and (index >= len(tokens) or tokens[index].startswith("--")):
                continue
            test.assertLess(index, len(tokens), f"{command}: {name} needs a value")
            index += 1
        else:
            choices = _subparsers(current)
            if token in choices:
                current = choices[token]
            else:
                test.assertTrue(
                    token.isupper() or token in _positional_choices(current),
                    f"{command}: {token} is neither a verb, a choice, nor a VALUE placeholder",
                )
            index += 1


def section_headings(text):
    """The `## ` headings of the guide, in file order."""
    return [
        line[3:].strip()
        for line in text.splitlines()
        if line.startswith("## ") and not line.startswith("### ")
    ]


class GuideTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GUIDE.read_text(encoding="utf-8")
        cls.commands = shell_commands(cls.text)
        assert cls.commands, "the guide shows no korapy commands"
        cls.parser = cli.build_parser()

    def test_the_sections_arrive_in_the_ticket_order(self):
        headings = [heading.lower() for heading in section_headings(self.text)]
        self.assertGreaterEqual(len(headings), len(EXPECTED_SECTIONS))
        cursor = 0
        for expected in EXPECTED_SECTIONS:
            found = next(
                (i for i in range(cursor, len(headings)) if expected in headings[i]),
                None,
            )
            self.assertIsNotNone(found, f"no ## heading names {expected!r}: {headings}")
            assert found is not None
            cursor = found + 1

    def test_every_shown_verb_and_flag_exists(self):
        for command in self.commands:
            with self.subTest(command=command):
                check_command(self, command, self.parser)

    def test_the_guide_uses_no_em_dashes(self):
        self.assertNotIn("\u2014", self.text)

    def test_the_guide_states_the_exit_codes(self):
        for snippet in ("75", "free slot"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.text)


if __name__ == "__main__":
    unittest.main()
