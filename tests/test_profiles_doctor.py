"""Doctor rows for agent profiles: declarations, runtimes, references.

Offline: validated profiles, selections and dep results are plain data
carried in `doctor.ProfileState`; tests inject them. No subprocesses, no
network, no real state or config dirs.
"""

import unittest

from korapy import deps, doctor, profiles

T0 = 1_800_000_000.0


def declared(extra=None):
    tables = {
        "default": {
            "runtime": "muse",
            "models": {"default": {"chain": ["provider-a/model-x"]}},
        },
        "fast": {"runtime": "omp"},
    }
    tables.update(extra or {})
    return profiles.validate(tables, ["provider-a"])


def dep(name, problem=None, install=None):
    entry = deps.Dep(name=name, why="agent runtime", probe=(name, "--version"), install=install)
    return deps.Result(entry, None if problem else "1.0", problem)


def state(parsed=None, error=None, **over):
    fields: dict = {
        "parsed": parsed if parsed is not None else {},
        "error": error,
        "sources": {},
        "shadowed": {},
        "selections": profiles.empty_selections(),
        "selections_error": None,
        "committed": {"default": None, "repos": {}, "roles": {}},
        "now": T0,
    }
    fields.update(over)
    return doctor.ProfileState(**fields)


def check(*args, **kwargs):
    return doctor.check_profiles(*args, **kwargs)


def by_name(checks):
    return {check.name: check for check in checks}


class Declarations(unittest.TestCase):
    def test_no_profiles_read_ok(self):
        checks = check(state(), [dep("muse"), dep("omp")])
        rows = by_name(checks)
        self.assertEqual(rows["profiles"].status, "ok")
        self.assertIn("no profiles declared", rows["profiles"].detail)

    def test_declared_profiles_validate_ok(self):
        checks = check(state(parsed=declared()), [dep("muse"), dep("omp")])
        self.assertEqual(by_name(checks)["profiles"].status, "ok")

    def test_a_broken_declaration_fails_naming_the_value(self):
        checks = check(state(error="profile 'fast': unknown runtime 'nope'"), [])
        rows = by_name(checks)
        self.assertEqual(rows["profiles"].status, "fail")
        self.assertIn("nope", rows["profiles"].detail)
        self.assertTrue(rows["profiles"].fix)

    def test_an_unreadable_selections_file_fails_naming_it(self):
        checks = check(
            state(selections=None, selections_error="/cfg/profiles.toml: not a table"),
            [],
        )
        rows = by_name(checks)
        self.assertEqual(rows["profiles"].status, "fail")
        self.assertIn("profiles.toml", rows["profiles"].detail)


class DanglingReferences(unittest.TestCase):
    def test_a_selection_naming_no_profile_fails_naming_it(self):
        selections = profiles.Selections(default="ghost")
        checks = check(state(parsed=declared(), selections=selections), [])
        rows = by_name(checks)
        self.assertEqual(rows["profiles"].status, "fail")
        self.assertIn("ghost", rows["profiles"].detail)

    def test_a_pin_and_an_override_name_their_places(self):
        selections = profiles.Selections(
            pins={"myrepo#7": "ghost"},
            override=profiles.Override(scope="global", profile="gone", until=T0 + 60),
        )
        checks = check(state(parsed=declared(), selections=selections), [])
        rows = by_name(checks)
        self.assertEqual(rows["profiles"].status, "fail")
        self.assertIn("ghost", rows["profiles"].detail)
        self.assertIn("gone", rows["profiles"].detail)

    def test_a_dangling_committed_default_fails(self):
        checks = check(
            state(
                parsed=declared(),
                committed={"default": "ghost", "repos": {}, "roles": {}},
            ),
            [],
        )
        self.assertEqual(by_name(checks)["profiles"].status, "fail")

    def test_a_dangling_committed_repo_default_fails(self):
        checks = check(
            state(
                parsed=declared(),
                committed={"default": None, "repos": {"myrepo": "ghost"}, "roles": {}},
            ),
            [],
        )
        self.assertEqual(by_name(checks)["profiles"].status, "fail")


class RuntimeBinaries(unittest.TestCase):
    def test_every_referenced_runtime_resolving_reads_ok(self):
        checks = check(state(parsed=declared()), [dep("muse"), dep("omp")])
        rows = by_name(checks)
        self.assertEqual(rows["profile-runtimes"].status, "ok")

    def test_a_missing_binary_fails_with_the_install_command(self):
        install = "curl -fsSL https://example.invalid/omp/install | sh"
        checks = check(state(parsed=declared()), [dep("muse"), dep("omp", "missing", install)])
        rows = by_name(checks)
        failed = rows["profile-runtimes:omp"]
        self.assertEqual(failed.status, "fail")
        self.assertIn("fast", failed.detail)
        self.assertIn(install, failed.fix)

    def test_an_unreferenced_missing_runtime_is_not_a_profile_failure(self):
        checks = check(state(parsed=declared()), [dep("muse"), dep("omp"), dep("ghost", "missing")])
        rows = by_name(checks)
        self.assertEqual(rows["profile-runtimes"].status, "ok")
        self.assertNotIn("profile-runtimes:ghost", rows)

    def test_a_runtime_with_no_probe_fails_naming_it(self):
        checks = check(state(parsed=declared()), [dep("muse")])
        rows = by_name(checks)
        self.assertEqual(rows["profile-runtimes:omp"].status, "fail")


class ExpiredOverride(unittest.TestCase):
    def test_an_expired_override_fails_until_reset(self):
        selections = profiles.Selections(
            override=profiles.Override(scope="global", profile="fast", until=T0 - 1)
        )
        checks = check(
            state(parsed=declared(), selections=selections, now=T0),
            [dep("muse"), dep("omp")],
        )
        rows = by_name(checks)
        self.assertEqual(rows["profile-override"].status, "fail")
        self.assertIn("reset --temp", rows["profile-override"].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_live_override_reads_ok(self):
        selections = profiles.Selections(
            override=profiles.Override(scope="global", profile="fast", until=T0 + 60)
        )
        checks = check(
            state(parsed=declared(), selections=selections, now=T0),
            [dep("muse"), dep("omp")],
        )
        self.assertEqual(by_name(checks)["profile-override"].status, "ok")

    def test_no_override_carries_no_override_row(self):
        checks = check(state(parsed=declared()), [dep("muse"), dep("omp")])
        self.assertNotIn("profile-override", by_name(checks))


class ShadowedNames(unittest.TestCase):
    def test_a_shadowed_name_is_a_note_with_both_sources(self):
        checks = check(
            state(
                parsed=declared(),
                sources={"fast": "/host/config.toml"},
                shadowed={"fast": "/ops/korapy.toml"},
            ),
            [dep("muse"), dep("omp")],
        )
        rows = by_name(checks)
        note = rows["profiles:fast"]
        self.assertEqual(note.status, "warn")
        self.assertIn("/host/config.toml", note.detail)
        self.assertIn("/ops/korapy.toml", note.detail)
        self.assertTrue(note.fix)
        self.assertEqual(rows["profiles"].status, "ok")
        self.assertEqual(doctor.exit_code(checks), 0)


class ExitCode(unittest.TestCase):
    def test_a_failing_profile_row_exits_1(self):
        selections = profiles.Selections(default="ghost")
        checks = check(state(parsed=declared(), selections=selections), [])
        self.assertEqual(doctor.exit_code(checks), 1)


if __name__ == "__main__":
    unittest.main()
