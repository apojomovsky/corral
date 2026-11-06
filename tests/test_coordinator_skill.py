"""The coordinator skill (`skills/coordinator.md`) and its parser conformance.

The skill is a thin file over the CLI: every verb and flag it names must
exist in the real parsers, and its verb table must cover the coordinator
verbs, so the two cannot drift silently. Reading the file and building the
parsers touches nothing outside the tree: no subprocess, no network, no
state dir.
"""

import argparse
import pathlib
import re
import unittest

from korapy import cli

SKILL = pathlib.Path(__file__).resolve().parents[1] / "skills" / "coordinator.md"

COMMAND_RE = re.compile(r"`(korapy(?=\s)[^`]*)`")

EXPECTED_VERBS = frozenset(
    {
        "coord wait",
        "coord events",
        "coord ack",
        "review show",
        "takeoff",
        "merge",
        "drive",
        "push",
        "agent list",
        "snapshot",
        "serve",
    }
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


def check_command(test, command, parser):
    """Walk one skill command down the parsers: verbs descend, flags exist."""
    tokens = command.replace("[", " ").replace("]", " ").split()
    test.assertEqual(tokens[0], "korapy", command)
    test.assertFalse(
        any("|" in token or token == "--" for token in tokens),
        f"{command}: shell alternations do not name one invocation",
    )
    current = parser
    index = 1
    while index < len(tokens):
        token = tokens[index]
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
            if options[name].nargs != 0:
                test.assertLess(index, len(tokens), f"{command}: {name} needs a value")
                index += 1
        else:
            choices = _subparsers(current)
            if token in choices:
                current = choices[token]
            else:
                test.assertTrue(
                    token.isupper(),
                    f"{command}: {token} is neither a verb nor a VALUE placeholder",
                )
            index += 1


def verb_path(command):
    """The verb path of a skill command: subcommands only, no flags or values."""
    tokens = command.replace("[", " ").replace("]", " ").split()
    parser = cli.build_parser()
    path = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            name, eq, _ = token.partition("=")
            action = _options(parser)[name]
            index += 1
            if not eq and action.nargs != 0:
                index += 1
            continue
        choices = _subparsers(parser)
        if token in choices:
            parser = choices[token]
            path.append(token)
            index += 1
        else:
            index += 1
    return " ".join(path)


class CoordinatorSkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")
        cls.commands = COMMAND_RE.findall(cls.text)
        assert cls.commands, "the skill names no korapy commands"
        cls.parser = cli.build_parser()

    def test_every_named_verb_and_flag_exists(self):
        for command in self.commands:
            with self.subTest(command=command):
                check_command(self, command, self.parser)

    def test_the_verb_table_covers_the_coordinator_verbs(self):
        found = {verb_path(command) for command in self.commands}
        for verb in sorted(EXPECTED_VERBS):
            with self.subTest(verb=verb):
                self.assertIn(verb, found, f"the skill never names `korapy {verb}`")

    def test_the_skill_states_the_exit_codes_and_the_review_gate(self):
        for snippet in ("75", "free slot", "reviewer", "author", "session"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.text)


if __name__ == "__main__":
    unittest.main()
