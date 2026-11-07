"""The README is the front door, not the reference.

It stays short: no command, settings or exit-code tables (those live in
`docs/commands.md` and `docs/configuration.md`), no wide tables, no
em-dashes. The repository's own owner and name appear only in the
README's CI badge and clone URL: the address tests derive both from
the clone URL and scan the rest of the tracked tree for the login.
Local git reads only (the tracked file list); no network, no state.
"""

import re
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from korapy import drive, eligibility, runtimes

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# The clone URL is the one place the repository's own public path is
# read from; the badge tests below reuse whatever it names. Tracked
# files the login may appear in: the copyright notice and the README's
# two allowed URL forms.
_CLONE_RE = re.compile(
    r"git clone https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
)
_EXEMPT = frozenset({"LICENSE", "README.md"})


def _tracked_files() -> list[Path]:
    """Every tracked file, via the local index, else a tree walk."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        return [ROOT / entry for entry in proc.stdout.split("\0") if entry]
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(ROOT).parts
    )


def readme_text():
    return README.read_text(encoding="utf-8")


def _contains_login(data: bytes, login: str) -> bool:
    """Case-insensitive login match, mirroring the coordinator's grep -i gate."""
    return login.lower().encode("ascii") in data.lower()


def _ci_workflow_file() -> str:
    """The ci workflow file name, read from the workflows directory."""
    names = sorted(
        path.name for path in (ROOT / ".github" / "workflows").glob("ci.*") if path.is_file()
    )
    assert names, "no ci workflow in .github/workflows"
    return names[0]


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
            "License-MIT",
            "3.11",
            "linux",
            "dependencies-none",
            "alpha",
        ):
            with self.subTest(badge=badge):
                self.assertIn(badge, head)

    def test_the_old_name_stays_out_of_the_readme(self):
        self.assertNotIn("corral", self.text.lower())

    def test_the_opinions_link_to_the_design(self):
        opinions = self.text.split("## Opinions", 1)[1].split("## ", 1)[0]
        self.assertIn("docs/00-design.md", opinions)
        bullets = [line for line in opinions.splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(bullets), 5, f"Opinions has {len(bullets)} bullets")
        for bullet in bullets:
            self.assertLessEqual(len(bullet.splitlines()), 1)


class RepositoryAddress(unittest.TestCase):
    """The one approved exception: the README names the repo's own path."""

    @classmethod
    def setUpClass(cls):
        cls.text = readme_text()
        match = _CLONE_RE.search(cls.text)
        assert match is not None, "no clone URL in README.md"
        cls.owner = match.group("owner")
        cls.repo = match.group("repo")
        cls.workflow_file = _ci_workflow_file()
        cls.badge_image = (
            f"https://github.com/{cls.owner}/{cls.repo}"
            f"/actions/workflows/{cls.workflow_file}/badge.svg"
        )
        cls.badge_link = (
            f"https://github.com/{cls.owner}/{cls.repo}/actions/workflows/{cls.workflow_file}"
        )
        cls.clone_line = f"git clone https://github.com/{cls.owner}/{cls.repo}"

    def test_the_ci_badge_points_at_this_repository(self):
        head = self.text.split("## ", 1)[0]
        self.assertNotIn("CI badge goes here", head)
        self.assertIn(f"[![CI]({self.badge_image})]({self.badge_link})", head)
        self.assertTrue(
            self.badge_link.endswith(f"/actions/workflows/{self.workflow_file}"),
            f"badge link misses the workflow file: {self.badge_link}",
        )

    def test_the_install_section_names_the_clone_url(self):
        install = self.text.split("## Requirements and install", 1)[1].split("## ", 1)[0]
        self.assertIn(self.clone_line, install)

    def test_the_owner_path_appears_only_in_the_allowed_forms(self):
        cases = (
            (
                "allowed forms only",
                f"{self.badge_image}\n{self.badge_link}\n{self.clone_line}\n",
                True,
            ),
            (
                "bare login in prose",
                f"{self.clone_line}\nThanks to {self.owner} for the reviews.\n",
                False,
            ),
            (
                "differently cased login in prose",
                f"{self.clone_line}\nThanks to {self.owner.upper()} for the reviews.\n",
                False,
            ),
            (
                "another repository URL",
                "See https://github.com/someone/else for details.\n",
                False,
            ),
            ("the readme itself", self.text, True),
        )
        for name, text, expected_clean in cases:
            with self.subTest(case=name):
                rest = text
                for allowed in (self.badge_image, self.badge_link, self.clone_line):
                    rest = rest.replace(allowed, "")
                is_clean = "github.com/" not in rest and not _contains_login(
                    rest.encode("utf-8"), self.owner
                )
                self.assertEqual(is_clean, expected_clean)

    def test_the_owner_login_stays_out_of_the_rest_of_the_tree(self):
        offenders = [
            str(path.relative_to(ROOT))
            for path in _tracked_files()
            if str(path.relative_to(ROOT)) not in _EXEMPT
            and _contains_login(path.read_bytes(), self.owner)
        ]
        self.assertEqual(offenders, [])

    def test_the_login_scan_ignores_case(self):
        marked = self.owner.encode("ascii")
        cases = (
            (b"nothing to see here", False),
            (b"prefix " + marked + b" suffix", True),
            (marked.upper(), True),
            (marked.capitalize(), True),
        )
        for data, expected in cases:
            with self.subTest(data=data[:20]):
                self.assertEqual(_contains_login(data, self.owner), expected)


class QuickstartOutputs(unittest.TestCase):
    """The quickstart quotes the real pass outputs, not approximations."""

    def test_the_drive_summaries_match_the_renderer(self):
        text = readme_text()
        self.assertIn(drive.render([], label="plan", plan=True).strip(), text)
        self.assertIn(drive.render([], label="pass", plan=False).strip(), text)

    def test_the_skipped_line_matches_the_eligibility_reason(self):
        project = SimpleNamespace(name="NAME", eligibility=None, root=".")
        outcome = eligibility.for_project(project, [], now=0.0)
        assert outcome.skipped is not None
        self.assertIn(outcome.skipped, readme_text())


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
