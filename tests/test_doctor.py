"""Read-only health report: `corral doctor`, over fakes at every seam."""

import io
import json
import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from typing import Any
from unittest import mock

from corral import cli, deps, doctor, registry, units


def proc(stdout="", returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def fake_run(stdout="Linger=yes\n", returncode=0, error=None):
    def run(argv):
        if error is not None:
            raise error
        return proc(stdout, returncode)

    return run


def dep(name, problem=None, version="1.0"):
    entry = deps.Dep(name=name, why="a test program", probe=(name, "--version"))
    return deps.Result(entry, None if problem else version, problem)


def live_entry(ticket=True):
    entry = {"id": "agent-1", "host": "host-1", "pid": 4242}
    if ticket:
        entry["ticket"] = {"project": "", "repo": "demo", "number": 7}
    return entry


def env(**overrides):
    fields: dict[str, Any] = {
        "dep_results": (dep("git"),),
        "account_problems": (),
        "settings_error": None,
        "state_error": None,
        "user": "tester",
        "unit_prefix": "corral-agent-",
        "run": fake_run(),
        "list_fn": lambda prefix: [],
        "reader_errors": (),
        "registry_rows": ((live_entry(), registry.LIVE),),
    }
    fields.update(overrides)
    return doctor.Env(**fields)


def names(checks):
    return [check.name for check in checks]


class AllOk(unittest.TestCase):
    def test_every_check_passes_and_the_exit_is_zero(self):
        checks = doctor.run_checks(env())
        self.assertTrue(checks)
        self.assertTrue(all(check.status == "ok" for check in checks))
        self.assertEqual(
            names(checks),
            [
                "deps",
                "accounts",
                "settings",
                "state-dir",
                "linger",
                "user-manager",
                "quota-readers",
                "registry",
                "prompts",
            ],
        )
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_warnings_alone_still_exit_zero(self):
        checks = doctor.run_checks(
            env(run=fake_run("Linger=no\n"), registry_rows=((live_entry(False), registry.LIVE),))
        )
        self.assertEqual(
            {check.name: check.status for check in checks},
            {
                "deps": "ok",
                "accounts": "ok",
                "settings": "ok",
                "state-dir": "ok",
                "linger": "warn",
                "user-manager": "ok",
                "quota-readers": "ok",
                "registry": "warn",
                "prompts": "ok",
            },
        )
        self.assertEqual(doctor.exit_code(checks), 0)


class EachCheckFailingAlone(unittest.TestCase):
    def test_a_missing_program_is_a_failing_deps_row_with_its_fix(self):
        entry = deps.Dep(
            name="git",
            why="clones",
            probe=("git", "--version"),
            apt="git",
        )
        checks = doctor.run_checks(env(dep_results=(deps.Result(entry, None, "missing"),)))
        failed = [check for check in checks if check.name.startswith("deps")]
        self.assertEqual(names(failed), ["deps:git"])
        self.assertEqual(failed[0].status, "fail")
        self.assertIn("missing", failed[0].detail)
        self.assertIn("sudo apt install -y git", failed[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_an_optional_program_missing_is_not_a_failure(self):
        entry = deps.Dep(
            name="tailscale",
            why="multi-host",
            probe=("tailscale", "version"),
            optional=True,
            install="curl -fsSL https://tailscale.com/install.sh | sh",
        )
        checks = doctor.run_checks(env(dep_results=(deps.Result(entry, None, "missing"),)))
        found = [check for check in checks if check.name == "deps"]
        self.assertEqual([check.status for check in found], ["ok"])

    def test_a_muse_major_bump_is_a_failing_deps_row_with_the_cutover_hint(self):
        entry = next(d for d in deps.load() if d.name == "muse")
        result = deps.check(entry, lambda argv, paths: "muse 2.0.1\n")
        self.assertFalse(result.ok)
        checks = doctor.run_checks(env(dep_results=(result,)))
        failed = [check for check in checks if check.name == "deps:muse"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertIn("below 2", failed[0].detail)
        self.assertIn("Muse Code installation instructions", failed[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_deps_load_error_is_a_failing_deps_row_with_exit_1(self):
        checks = doctor.run_checks(env(deps_error="system_deps.toml: bad toml"))
        failed = [check for check in checks if check.name == "deps"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertIn("bad toml", failed[0].detail)
        self.assertTrue(failed[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_account_problems_fail(self):
        checks = doctor.run_checks(env(account_problems=("accounts.toml: boom",)))
        self.assertEqual([check.status for check in checks if check.name == "accounts"], ["fail"])
        self.assertIn("boom", [c.detail for c in checks if c.name == "accounts"][0])
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_settings_error_fails(self):
        checks = doctor.run_checks(env(settings_error="/x/config.toml: bad value"))
        failed = [check for check in checks if check.name == "settings"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertIn("bad value", failed[0].detail)
        self.assertTrue(failed[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_an_unwritable_state_dir_fails(self):
        checks = doctor.run_checks(env(state_error="/x/state is not writable: denied"))
        failed = [check for check in checks if check.name == "state-dir"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertIn("denied", failed[0].detail)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_linger_off_is_a_warning_with_the_enable_command(self):
        seen = []

        def run(argv):
            seen.append(argv)
            return proc("Linger=no\n")

        checks = doctor.run_checks(env(run=run))
        failed = [check for check in checks if check.name == "linger"]
        self.assertEqual([check.status for check in failed], ["warn"])
        self.assertIn('loginctl enable-linger "$USER"', failed[0].fix)
        self.assertEqual(seen, [["loginctl", "show-user", "tester", "--property=Linger"]])
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_an_unreachable_loginctl_fails_with_exit_3(self):
        checks = doctor.run_checks(env(run=fake_run(error=OSError("no bus"))))
        failed = [check for check in checks if check.name == "linger"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 3)

    def test_a_failing_loginctl_fails_with_exit_3(self):
        checks = doctor.run_checks(env(run=fake_run("", returncode=1)))
        failed = [check for check in checks if check.name == "linger"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 3)

    def test_an_unreachable_manager_fails_with_exit_3(self):
        def list_fn(prefix):
            raise units.Unreachable("cannot list units: refused")

        checks = doctor.run_checks(env(list_fn=list_fn))
        failed = [check for check in checks if check.name == "user-manager"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertIn("refused", failed[0].detail)
        self.assertTrue(failed[0].fix)
        self.assertEqual(doctor.exit_code(checks), 3)

    def test_listed_units_are_counted(self):
        checks = doctor.run_checks(
            env(list_fn=lambda prefix: [("corral-agent-demo-7", "active", "running")])
        )
        found = [check for check in checks if check.name == "user-manager"]
        self.assertEqual([check.status for check in found], ["ok"])
        self.assertIn("1", found[0].detail)

    def test_a_reader_that_fails_to_import_fails(self):
        checks = doctor.run_checks(
            env(reader_errors=(("provider-a", "no.such:Reader", "does not import"),))
        )
        failed = [check for check in checks if "quota-readers" in check.name]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].status, "fail")
        self.assertIn("provider-a", failed[0].name)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_live_agent_without_a_ticket_warns(self):
        checks = doctor.run_checks(env(registry_rows=((live_entry(False), registry.LIVE),)))
        found = [check for check in checks if check.name == "registry"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("no ticket", found[0].detail)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_registry_counts_by_state(self):
        checks = doctor.run_checks(
            env(
                registry_rows=(
                    (live_entry(), registry.LIVE),
                    (live_entry(False), registry.LIVE),
                    (live_entry(False), registry.STALE),
                )
            )
        )
        found = [check for check in checks if check.name == "registry"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("live=2", found[0].detail)
        self.assertIn("stale=1", found[0].detail)
        self.assertIn("finished=0", found[0].detail)


class Rendering(unittest.TestCase):
    def test_json_parses_with_schema_version_1(self):
        checks = doctor.run_checks(env(account_problems=("boom",)))
        out = doctor.render(checks, json_mode=True)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(len(document["checks"]), len(checks))
        for row in document["checks"]:
            self.assertEqual(set(row), {"name", "status", "detail", "fix"})

    def test_human_output_marks_each_check_and_shows_fixes(self):
        checks = doctor.run_checks(env(account_problems=("boom",), run=fake_run("Linger=no\n")))
        out = doctor.render(checks, json_mode=False)
        lines = out.splitlines()
        self.assertEqual(len([line for line in lines if line[:1] in "✓!✗"]), len(checks))
        self.assertIn("boom", out)
        self.assertIn('loginctl enable-linger "$USER"', out)

    def test_an_all_ok_report_shows_no_fixes(self):
        out = doctor.render(doctor.run_checks(env()), json_mode=False)
        self.assertNotIn("fix:", out)


class StateDirs(unittest.TestCase):
    def test_missing_dirs_are_created_and_the_probe_is_removed(self):
        root = tempfile.mkdtemp(prefix="corral-doctor-")
        env_map = {
            "CORRAL_STATE": os.path.join(root, "state"),
            "CORRAL_CONFIG": os.path.join(root, "config"),
        }
        self.assertIsNone(doctor.check_state_dirs(env_map))
        leftovers = []
        for base in (env_map["CORRAL_STATE"], env_map["CORRAL_CONFIG"]):
            leftovers += os.listdir(base)
        self.assertEqual(leftovers, [])

    def test_a_file_where_a_dir_belongs_is_an_error(self):
        root = tempfile.mkdtemp(prefix="corral-doctor-")
        blocked = os.path.join(root, "state")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("in the way")
        error = doctor.check_state_dirs(
            {"CORRAL_STATE": blocked, "CORRAL_CONFIG": os.path.join(root, "config")}
        )
        self.assertIsNotNone(error)
        self.assertIn(blocked, error or "")

    def test_a_missing_probe_on_unlink_still_passes(self):
        root = tempfile.mkdtemp(prefix="corral-doctor-")
        env_map = {
            "CORRAL_STATE": os.path.join(root, "state"),
            "CORRAL_CONFIG": os.path.join(root, "config"),
        }
        with mock.patch.object(doctor.os, "unlink", side_effect=FileNotFoundError("gone")):
            self.assertIsNone(doctor.check_state_dirs(env_map))

    def test_probe_names_carry_the_pid(self):
        seen = []
        real = doctor.paths.atomic_write_text

        def spy(path, text):
            seen.append(path)
            return real(path, text)

        root = tempfile.mkdtemp(prefix="corral-doctor-")
        env_map = {
            "CORRAL_STATE": os.path.join(root, "state"),
            "CORRAL_CONFIG": os.path.join(root, "config"),
        }
        with mock.patch.object(doctor.paths, "atomic_write_text", side_effect=spy):
            self.assertIsNone(doctor.check_state_dirs(env_map))
        self.assertTrue(seen)
        self.assertTrue(all(str(os.getpid()) in path for path in seen))


class SettingsResolution(unittest.TestCase):
    def write(self, directory, name, text):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_broken_ops_file_is_an_error(self):
        root = tempfile.mkdtemp(prefix="corral-doctor-")
        config = tempfile.mkdtemp(prefix="corral-doctor-")
        self.write(root, "corral.toml", "agents = 42\n")
        error = doctor.check_settings(root, {"CORRAL_CONFIG": config})
        self.assertIsNotNone(error)
        self.assertIn("corral.toml", error or "")

    def test_a_clean_host_and_root_resolve(self):
        root = tempfile.mkdtemp(prefix="corral-doctor-")
        config = tempfile.mkdtemp(prefix="corral-doctor-")
        self.write(root, "corral.toml", "[models.default]\nchain = ['pool/model']\n")
        self.assertIsNone(doctor.check_settings(root, {"CORRAL_CONFIG": config}))


class ProductionGathering(unittest.TestCase):
    def test_a_corrupt_packaged_toml_degrades_to_a_deps_error(self):
        for error in (
            tomllib.TOMLDecodeError("boom", "x=", 0),
            OSError("gone"),
            TypeError("bad entry"),
            KeyError("probe"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ):
            with mock.patch.object(doctor.deps, "load", side_effect=error):
                results, deps_error = doctor.load_dep_results()
            self.assertEqual(results, [])
            self.assertIsNotNone(deps_error)

    def test_a_bad_probe_regex_degrades_to_a_deps_error(self):
        with mock.patch.object(doctor.deps, "check_all", side_effect=re.error("unbalanced")):
            results, deps_error = doctor.load_dep_results()
        self.assertEqual(results, [])
        self.assertIsNotNone(deps_error)

    def test_a_deleted_working_directory_is_a_settings_failure(self):
        here = os.getcwd()
        root = tempfile.mkdtemp(prefix="corral-doctor-")
        os.chdir(root)
        try:
            os.rmdir(root)
            resolved, error = doctor._production_settings()
        finally:
            os.chdir(here)
        self.assertIsNone(resolved)
        self.assertIsNotNone(error)
        checks = doctor.run_checks(env(settings_error=error))
        failed = [check for check in checks if check.name == "settings"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 1)


class Command(unittest.TestCase):
    def run_verb(self, argv, make_env):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = doctor.run_verb(argv, make_env)
        return code, out.getvalue(), err.getvalue()

    def test_all_ok_exits_zero(self):
        code, out, _ = self.run_verb(["doctor"], lambda: env())
        self.assertEqual(code, 0)
        self.assertIn("accounts", out)

    def test_a_failure_exits_one(self):
        code, _, _ = self.run_verb(["doctor"], lambda: env(account_problems=("boom",)))
        self.assertEqual(code, 1)

    def test_json_flag_prints_a_document(self):
        code, out, _ = self.run_verb(["doctor", "--json"], lambda: env())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["schema_version"], 1)

    def test_a_bad_flag_is_a_usage_error(self):
        code, _, _ = self.run_verb(["doctor", "--nope"], lambda: env())
        self.assertEqual(code, 4)

    def test_the_top_level_parser_knows_doctor(self):
        args = cli.build_parser().parse_args(["doctor", "--json"])
        self.assertTrue(args.json)
        self.assertIs(args.func.func, doctor.cmd_doctor)


class FailClosed(unittest.TestCase):
    def test_a_hung_loginctl_fails_with_exit_3(self):
        def run(argv):
            raise subprocess.TimeoutExpired(argv, 10)

        checks = doctor.run_checks(env(run=run))
        failed = [check for check in checks if check.name == "linger"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 3)

    def test_an_invalid_prefix_is_a_failing_row_not_a_crash(self):
        def list_fn(prefix):
            raise units.UnitsError("unit prefix 'bad prefix!' must be letters")

        checks = doctor.run_checks(env(list_fn=list_fn))
        failed = [check for check in checks if check.name == "user-manager"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertIn("bad prefix", failed[0].detail)

    def test_an_invalid_prefix_names_the_setting_and_exits_1(self):
        def list_fn(prefix):
            raise units.UnitsError("unit prefix 'bad prefix!' must be letters")

        checks = doctor.run_checks(env(list_fn=list_fn))
        failed = [check for check in checks if check.name == "user-manager"]
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertIn("agents.unit_prefix", failed[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_reader_failure_without_names_keeps_the_bare_check_name(self):
        checks = doctor.run_checks(env(reader_errors=(("", "", "boom"),)))
        failed = [check for check in checks if "quota-readers" in check.name]
        self.assertEqual([check.name for check in failed], ["quota-readers"])
        self.assertEqual([check.status for check in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_registry_listing_failure_fails(self):
        checks = doctor.run_checks(env(registry_error="state is gone"))
        found = [check for check in checks if check.name == "registry"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertIn("gone", found[0].detail)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_combined_failures_prefer_exit_3_when_the_bus_is_gone(self):
        checks = doctor.run_checks(
            env(
                account_problems=("boom",),
                run=fake_run(error=OSError("no bus")),
            )
        )
        self.assertEqual(doctor.exit_code(checks), 3)

    def test_exit_codes_agree_with_the_cli(self):
        self.assertEqual(
            (doctor.OK, doctor.UNREACHABLE, doctor.USAGE),
            (cli.OK, cli.UNREACHABLE, cli.USAGE),
        )


class DriveRows(unittest.TestCase):
    def test_no_reading_means_no_drive_rows(self):
        self.assertEqual(names(doctor.run_checks(env())), names(doctor.run_checks(env())))
        found = [check for check in doctor.run_checks(env()) if check.name.startswith("drive")]
        self.assertEqual(found, [])

    def test_an_enabled_timer_is_ok(self):
        checks = doctor.run_checks(env(timer_state="enabled"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["ok"])
        self.assertIn("corral-drive.timer", found[0].detail)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_a_missing_timer_warns_with_the_install_command(self):
        checks = doctor.run_checks(env(timer_state="missing"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("corral drive install-timer", found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_a_disabled_timer_warns_with_the_install_command(self):
        checks = doctor.run_checks(env(timer_state="disabled"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("corral drive install-timer", found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_a_masked_timer_warns_with_the_unmask_command(self):
        checks = doctor.run_checks(env(timer_state="masked"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("unmask", found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_an_unreachable_timer_fails_with_exit_3(self):
        checks = doctor.run_checks(env(timer_state="unreachable: no bus"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 3)

    def test_no_project_problems_is_an_ok_row(self):
        checks = doctor.run_checks(env(project_problems=()))
        found = [check for check in checks if check.name == "drive-projects"]
        self.assertEqual([check.status for check in found], ["ok"])
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_a_bad_registration_fails_naming_the_file(self):
        checks = doctor.run_checks(
            env(project_problems=("config/projects.d/blue.toml: cannot read the registration",))
        )
        found = [check for check in checks if check.name == "drive-projects"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertIn("blue.toml", found[0].detail)
        self.assertTrue(found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_an_unreadable_policy_module_fails_naming_the_project(self):
        checks = doctor.run_checks(
            env(
                project_problems=(
                    "project blue: eligibility 'blue_policy:eligible' does not import: boom",
                )
            )
        )
        found = [check for check in checks if check.name == "drive-projects"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertIn("blue", found[0].detail)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_drive_rows_come_after_the_manager_row(self):
        checks = doctor.run_checks(env(timer_state="enabled", project_problems=()))
        ordered = names(checks)
        self.assertLess(ordered.index("user-manager"), ordered.index("drive-timer"))
        self.assertLess(ordered.index("drive-timer"), ordered.index("drive-projects"))


if __name__ == "__main__":
    unittest.main()
