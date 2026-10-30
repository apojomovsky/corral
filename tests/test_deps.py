"""System dependency checks, against a fake probe."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from corral import cli, deps


def fake(outputs):
    """A probe that answers from a dict keyed by argv[0]; absent means missing."""

    def probe(argv, paths):
        return outputs.get(argv[0])

    return probe


def dep(**kw):
    kw.setdefault("name", kw.get("probe", ("x",))[0])
    kw.setdefault("why", "because")
    kw.setdefault("probe", (kw["name"], "--version"))
    return deps.Dep(**kw)


class Check(unittest.TestCase):
    def test_a_present_program_passes_with_its_version(self):
        r = deps.check(dep(name="git"), fake({"git": "git version 2.43.0\n"}))
        self.assertEqual((r.ok, r.version), (True, "2.43.0"))

    def test_a_missing_program_fails(self):
        r = deps.check(dep(name="gh"), fake({}))
        self.assertEqual(r.problem, "missing")

    def test_an_old_version_fails(self):
        r = deps.check(dep(name="uv", min="0.13.5"), fake({"uv": "uv 0.9.1 (abc)"}))
        self.assertEqual(r.problem, "version 0.9.1, needs 0.13.5 or later")

    def test_versions_compare_as_numbers(self):
        r = deps.check(dep(name="uv", min="0.9.0"), fake({"uv": "uv 0.13.5"}))
        self.assertTrue(r.ok)

    def test_a_major_at_the_bound_fails(self):
        r = deps.check(dep(name="opencode", below="2"), fake({"opencode": "2.0.1"}))
        self.assertEqual(r.problem, "version 2.0.1, needs below 2")

    def test_below_the_bound_passes(self):
        r = deps.check(dep(name="opencode", below="2"), fake({"opencode": "1.18.32"}))
        self.assertTrue(r.ok)

    def test_a_pattern_without_a_version_is_a_yes_no_check(self):
        d = dep(name="loginctl", pattern="Linger=yes")
        self.assertTrue(deps.check(d, fake({"loginctl": "Linger=yes\n"})).ok)
        self.assertFalse(deps.check(d, fake({"loginctl": "Linger=no\n"})).ok)


class Fixes(unittest.TestCase):
    def results(self):
        listed = [
            dep(name="git", apt="git"),
            dep(name="make", apt="make"),
            dep(name="gh", install="see the gh docs"),
            dep(name="curl", apt="curl"),
        ]
        return deps.check_all(listed, fake({"curl": "curl 8.5.0"}))

    def test_apt_packages_merge_into_one_line_first(self):
        self.assertEqual(
            deps.fixes(self.results()),
            ["sudo apt install -y git make", "see the gh docs"],
        )

    def test_nothing_to_fix_prints_no_commands(self):
        results = deps.check_all([dep(name="git", apt="git")], fake({"git": "2.43.0"}))
        self.assertEqual(deps.fixes(results), [])


class Report(unittest.TestCase):
    def test_a_missing_optional_entry_does_not_fail(self):
        listed = [dep(name="git"), dep(name="tailscale", optional=True, install="ts")]
        text, ok = deps.report(deps.check_all(listed, fake({"git": "2.43.0"})))
        self.assertTrue(ok)
        self.assertIn("Optional:", text)
        self.assertNotIn("To fix", text)

    def test_a_missing_required_entry_fails_and_says_how(self):
        listed = [dep(name="gh", install="see the gh docs")]
        text, ok = deps.report(deps.check_all(listed, fake({})))
        self.assertFalse(ok)
        self.assertIn("MISSING", text)
        self.assertIn("  see the gh docs", text)


class MuseTripwire(unittest.TestCase):
    """The shipped muse entry pins major 1: canned 1.x passes, 2.x fails."""

    def entry(self):
        return next(d for d in deps.load() if d.name == "muse")

    def test_corral_deps_reports_muse(self):
        self.assertEqual(self.entry().probe, ("muse", "--version"))
        outputs = {d.probe[0]: f"{d.name} 1.0 Linger=yes systemd 255" for d in deps.load()}
        outputs["uv"] = "uv 0.13.5"
        outputs["muse"] = "muse 1.4.0"
        text, ok = deps.report(deps.check_all(deps.load(), fake(outputs)))
        self.assertTrue(ok)
        self.assertIn("muse", text)

    def test_canned_1x_output_passes(self):
        r = deps.check(self.entry(), fake({"muse": "muse 1.4.0\n"}))
        self.assertTrue(r.ok)

    def test_old_1x_output_fails_with_the_min_floor(self):
        entry = self.entry()
        self.assertEqual(entry.min, "1.4")
        r = deps.check(entry, fake({"muse": "muse 1.0\n"}))
        self.assertFalse(r.ok)
        self.assertIn("needs 1.4 or later", r.problem or "")

    def test_canned_2x_output_fails_with_the_cutover_hint(self):
        entry = self.entry()
        self.assertEqual(entry.below, "2")
        self.assertIn("cutover", entry.why)
        r = deps.check(entry, fake({"muse": "muse 2.0.1\n"}))
        self.assertFalse(r.ok)
        self.assertIn("below 2", r.problem or "")
        self.assertIn("Muse Code installation instructions", entry.install or "")


class ShippedList(unittest.TestCase):
    def test_it_parses_and_every_entry_says_how_to_get_it(self):
        listed = deps.load()
        self.assertIn("uv", [d.name for d in listed])
        for d in listed:
            with self.subTest(d.name):
                self.assertTrue(d.why)
                if d.name != "systemd":  # part of the OS, nothing to print
                    self.assertTrue(d.apt or d.install, d.name)

    def test_the_makefile_prints_the_same_uv_installer(self):
        # make bootstrap runs before uv exists, so it repeats the command
        uv = next(d for d in deps.load() if d.name == "uv")
        makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
        self.assertIn(uv.install, makefile)


class Command(unittest.TestCase):
    def run_cli(self, outputs):
        out = io.StringIO()
        with (
            tempfile.TemporaryDirectory(prefix="corral-deps-") as directory,
            mock.patch.dict(os.environ, {"CORRAL_CONFIG": directory}),
            mock.patch.object(deps, "run_probe", fake(outputs)),
            redirect_stdout(out),
        ):
            code = cli.main(["deps"])
        return code, out.getvalue()

    def test_all_present_exits_0(self):
        everything = {d.probe[0]: f"{d.name} 1.0 Linger=yes systemd 255" for d in deps.load()}
        everything["uv"] = "uv 0.13.5"
        everything["muse"] = "muse 1.4.0"
        self.assertEqual(self.run_cli(everything)[0], 0)

    def test_a_missing_program_exits_1(self):
        code, text = self.run_cli({})
        self.assertEqual(code, 1)
        self.assertIn("To fix, run:", text)

    def test_an_unknown_verb_is_a_usage_error(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["nope"]), 4)


if __name__ == "__main__":
    unittest.main()
