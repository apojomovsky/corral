"""The README is the front door, not the reference.

It stays short: no command, settings or exit-code tables (those live in
`docs/commands.md` and `docs/configuration.md`), no wide tables, no
em-dashes. Reading the file touches nothing outside the tree.
"""

import re
import unittest
from pathlib import Path

from korapy import runtimes

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def readme_text():
    return README.read_text(encoding="utf-8")


def runtimes_bullet():
    """The runtimes line, the one that must name every runtime."""
    text = readme_text()
    lines = [line for line in text.splitlines() if "Headless runtimes" in line]
    return "\n".join(lines)


class FrontDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = readme_text()
        cls.lines = cls.text.splitlines()

    def test_the_readme_stays_short(self):
        self.assertLess(len(self.lines), 150, f"README.md is {len(self.lines)} lines")

    def test_no_table_is_wider_than_three_columns(self):
        for line in self.lines:
            with self.subTest(line=line[:60]):
                if not line.startswith("|"):
                    continue
                cells = re.split(r"(?<!\\)\|", line)
                self.assertLessEqual(
                    len(cells) - 2, 3, f"table row has more than three columns: {line}"
                )

    def test_the_readme_uses_no_em_dashes(self):
        self.assertNotIn("\u2014", self.text)

    def test_the_readme_holds_no_tables(self):
        tables = [line for line in self.lines if line.startswith("|")]
        self.assertEqual(tables, [])

    def test_the_reference_tables_stayed_in_docs(self):
        self.assertNotIn("## Commands", self.text)
        for heading in ("## Exit codes", "## Settings", "## Configuration"):
            with self.subTest(heading=heading):
                self.assertNotIn(heading, self.text)

    def test_the_sections_arrive_in_front_door_order(self):
        headings = [
            line for line in self.lines if line.startswith("#") and not line.startswith("##")
        ]
        self.assertTrue(headings and headings[0].startswith("# korapy"))
        order = (
            "What it does",
            "Features",
            "Requirements",
            "Quickstart",
            "Opinions",
            "Documentation",
            "Status",
            "License",
        )
        found = [line[3:].strip() for line in self.lines if line.startswith("## ")]
        cursor = 0
        for expected in order:
            hit = next(
                (i for i in range(cursor, len(found)) if found[i].startswith(expected)),
                None,
            )
            self.assertIsNotNone(hit, f"no ## heading for {expected!r}: {found}")
            assert hit is not None
            cursor = hit + 1

    def test_the_logo_and_badges_come_first(self):
        self.assertIn("docs/assets/korapy-logo.svg", self.text.split("## ", 1)[0])
        head = self.text.split("## ", 1)[0]
        for badge in (
            "actions/workflow/status",
            "License-MIT",
            "3.11",
            "linux",
            "alpha",
        ):
            with self.subTest(badge=badge):
                self.assertIn(badge, head)

    def test_the_opinions_link_to_the_design(self):
        opinions = self.text.split("## Opinions", 1)[1].split("## ", 1)[0]
        self.assertIn("docs/00-design.md", opinions)
        bullets = [line for line in opinions.splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(bullets), 5, f"Opinions has {len(bullets)} bullets")
        for bullet in bullets:
            self.assertLessEqual(len(bullet.splitlines()), 1)


class RegisteredRuntimes(unittest.TestCase):
    def test_readme_names_every_registered_runtime(self):
        bullet = runtimes_bullet()
        self.assertTrue(bullet, "no Headless runtimes line in README.md")
        missing = [
            name for name in runtimes.NAMES if not re.search(rf"\b{re.escape(name)}\b", bullet)
        ]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
