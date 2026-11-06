"""Read-only health report: `korapy doctor`, over fakes at every seam."""

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

from korapy import cli, deps, doctor, registry, units


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
        "unit_prefix": "korapy-agent-",
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
            env(list_fn=lambda prefix: [("korapy-agent-demo-7", "active", "running")])
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
        root = tempfile.mkdtemp(prefix="korapy-doctor-")
        env_map = {
            "KORAPY_STATE": os.path.join(root, "state"),
            "KORAPY_CONFIG": os.path.join(root, "config"),
        }
        self.assertIsNone(doctor.check_state_dirs(env_map))
        leftovers = []
        for base in (env_map["KORAPY_STATE"], env_map["KORAPY_CONFIG"]):
            leftovers += os.listdir(base)
        self.assertEqual(leftovers, [])

    def test_a_file_where_a_dir_belongs_is_an_error(self):
        root = tempfile.mkdtemp(prefix="korapy-doctor-")
        blocked = os.path.join(root, "state")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("in the way")
        error = doctor.check_state_dirs(
            {"KORAPY_STATE": blocked, "KORAPY_CONFIG": os.path.join(root, "config")}
        )
        self.assertIsNotNone(error)
        self.assertIn(blocked, error or "")

    def test_a_missing_probe_on_unlink_still_passes(self):
        root = tempfile.mkdtemp(prefix="korapy-doctor-")
        env_map = {
            "KORAPY_STATE": os.path.join(root, "state"),
            "KORAPY_CONFIG": os.path.join(root, "config"),
        }
        with mock.patch.object(doctor.os, "unlink", side_effect=FileNotFoundError("gone")):
            self.assertIsNone(doctor.check_state_dirs(env_map))

    def test_probe_names_carry_the_pid(self):
        seen = []
        real = doctor.paths.atomic_write_text

        def spy(path, text):
            seen.append(path)
            return real(path, text)

        root = tempfile.mkdtemp(prefix="korapy-doctor-")
        env_map = {
            "KORAPY_STATE": os.path.join(root, "state"),
            "KORAPY_CONFIG": os.path.join(root, "config"),
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
        root = tempfile.mkdtemp(prefix="korapy-doctor-")
        config = tempfile.mkdtemp(prefix="korapy-doctor-")
        self.write(root, "korapy.toml", "agents = 42\n")
        error = doctor.check_settings(root, {"KORAPY_CONFIG": config})
        self.assertIsNotNone(error)
        self.assertIn("korapy.toml", error or "")

    def test_a_clean_host_and_root_resolve(self):
        root = tempfile.mkdtemp(prefix="korapy-doctor-")
        config = tempfile.mkdtemp(prefix="korapy-doctor-")
        self.write(
            root,
            "korapy.toml",
            '[profiles.default]\nruntime = "omp"\n'
            "[profiles.default.models.default]\nchain = ['pool/model']\n",
        )
        self.assertIsNone(doctor.check_settings(root, {"KORAPY_CONFIG": config}))

    def test_old_models_and_runtime_keys_are_errors_naming_profiles(self):
        root = tempfile.mkdtemp(prefix="korapy-doctor-")
        config = tempfile.mkdtemp(prefix="korapy-doctor-")
        self.write(root, "korapy.toml", "[models.default]\nchain = ['pool/model']\n")
        error = doctor.check_settings(root, {"KORAPY_CONFIG": config})
        self.assertIsNotNone(error)
        self.assertIn("profiles", error or "")
        self.write(root, "korapy.toml", '[agents]\nruntime = "omp"\n')
        error = doctor.check_settings(root, {"KORAPY_CONFIG": config})
        self.assertIsNotNone(error)
        self.assertIn("profiles", error or "")


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
        root = tempfile.mkdtemp(prefix="korapy-doctor-")
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
        self.assertEqual(
            names(doctor.run_checks(env())),
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

    def test_an_enabled_timer_is_ok(self):
        checks = doctor.run_checks(env(timer_state="enabled"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["ok"])
        self.assertIn("korapy-drive.timer", found[0].detail)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_a_missing_timer_warns_with_the_install_command(self):
        checks = doctor.run_checks(env(timer_state="missing"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("korapy drive install-timer", found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_a_disabled_timer_warns_with_the_install_command(self):
        checks = doctor.run_checks(env(timer_state="disabled"))
        found = [check for check in checks if check.name == "drive-timer"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("korapy drive install-timer", found[0].fix)
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


HUB_URL = "http://host-1:8080"


def hub_env(**overrides):
    fields = {
        "hub_url": HUB_URL,
        "hub_auth": doctor.HubAuth("ok", "the token is present"),
        "hub_probe": doctor.HubProbe("ok", "the hub answers", hub_api=1, hub_schema=1),
    }
    fields.update(overrides)
    return env(**fields)


class HubRows(unittest.TestCase):
    def test_single_host_carries_no_hub_rows(self):
        checks = doctor.run_checks(env())
        ordered = names(checks)
        self.assertNotIn("hub", ordered)
        self.assertNotIn("hub-token", ordered)

    def test_configured_hub_with_no_readings_carries_no_hub_rows(self):
        checks = doctor.run_checks(env(hub_url=HUB_URL))
        ordered = names(checks)
        self.assertNotIn("hub", ordered)
        self.assertNotIn("hub-token", ordered)

    def test_healthy_hub_is_two_ok_rows(self):
        checks = doctor.run_checks(hub_env())
        rows = {check.name: check for check in checks if check.name.startswith("hub")}
        self.assertEqual(
            {name: row.status for name, row in rows.items()},
            {"hub": "ok", "hub-token": "ok"},
        )
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_unreachable_hub_warns_and_keeps_exit_zero(self):
        checks = doctor.run_checks(
            hub_env(hub_probe=doctor.HubProbe("unreachable", "connection refused"))
        )
        found = [check for check in checks if check.name == "hub"]
        self.assertEqual([check.status for check in found], ["warn"])
        self.assertIn("refused", found[0].detail)
        self.assertTrue(found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_refused_token_fails(self):
        checks = doctor.run_checks(
            hub_env(hub_probe=doctor.HubProbe("unauthorized", "hub refused the token"))
        )
        found = [check for check in checks if check.name == "hub"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertTrue(found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_version_mismatch_fails_naming_both_versions(self):
        checks = doctor.run_checks(
            hub_env(
                hub_probe=doctor.HubProbe(
                    "version-mismatch",
                    "hub speaks api 0, this host speaks api 1",
                    hub_api=0,
                    hub_schema=1,
                )
            )
        )
        found = [check for check in checks if check.name == "hub"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertIn("api 0", found[0].detail)
        self.assertIn("api 1", found[0].detail)
        self.assertTrue(found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_missing_token_fails_naming_the_fix(self):
        checks = doctor.run_checks(hub_env(hub_auth=doctor.HubAuth("missing", "no token")))
        found = [check for check in checks if check.name == "hub-token"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertTrue(found[0].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_bad_token_fails(self):
        checks = doctor.run_checks(hub_env(hub_auth=doctor.HubAuth("bad", "the token is empty")))
        found = [check for check in checks if check.name == "hub-token"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_hub_rows_come_last(self):
        checks = doctor.run_checks(hub_env(disk="ok"))
        ordered = names(checks)
        self.assertLess(ordered.index("disk"), ordered.index("hub-token"))
        self.assertLess(ordered.index("hub-token"), ordered.index("hub"))

    def test_hub_probe_is_data_never_a_callable(self):
        probe = doctor.HubProbe("ok", "the hub answers", hub_api=1, hub_schema=1)
        self.assertFalse(callable(probe))
        checks = doctor.run_checks(hub_env(hub_probe=probe))
        self.assertTrue(any(check.name == "hub" for check in checks))


class ProductionHubGathering(unittest.TestCase):
    def test_no_url_means_no_readings(self):
        self.assertIsNone(doctor.load_hub_auth(None))
        self.assertIsNone(doctor.load_hub_auth(""))
        self.assertIsNone(doctor.load_hub_probe(None, None))
        self.assertIsNone(doctor.load_hub_probe("", None))
        with tempfile.TemporaryDirectory(prefix="korapy-hub-auth-") as config:
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config}):
                self.assertIsNone(doctor.read_hub_token())

    def test_missing_token_file_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="korapy-hub-auth-") as config:
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config}):
                auth = doctor.load_hub_auth(HUB_URL)
                self.assertIsNotNone(auth)
                assert auth is not None
                self.assertEqual(auth.status, "missing")
                self.assertIn("hub-token", auth.detail)
                self.assertIsNone(doctor.read_hub_token())

    def test_owner_only_token_file_is_ok(self):
        with tempfile.TemporaryDirectory(prefix="korapy-hub-auth-") as config:
            path = os.path.join(config, "hub-token")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("s3cret\n")
            os.chmod(path, 0o600)
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config}):
                auth = doctor.load_hub_auth(HUB_URL)
                self.assertIsNotNone(auth)
                assert auth is not None
                self.assertEqual(auth.status, "ok")
                self.assertEqual(doctor.read_hub_token(), "s3cret")

    def test_group_readable_token_file_is_bad(self):
        with tempfile.TemporaryDirectory(prefix="korapy-hub-auth-") as config:
            path = os.path.join(config, "hub-token")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("s3cret\n")
            os.chmod(path, 0o640)
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config}):
                auth = doctor.load_hub_auth(HUB_URL)
                self.assertIsNotNone(auth)
                assert auth is not None
                self.assertEqual(auth.status, "bad")

    def test_empty_token_file_is_bad(self):
        with tempfile.TemporaryDirectory(prefix="korapy-hub-auth-") as config:
            path = os.path.join(config, "hub-token")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n")
            os.chmod(path, 0o600)
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": config}):
                auth = doctor.load_hub_auth(HUB_URL)
                self.assertIsNotNone(auth)
                assert auth is not None
                self.assertEqual(auth.status, "bad")
                self.assertIsNone(doctor.read_hub_token())

    def test_probe_of_a_live_loopback_hub_is_ok_with_versions(self):
        import threading

        from korapy import hub

        with tempfile.TemporaryDirectory(prefix="korapy-hub-probe-") as state:
            db = os.path.join(state, "hub.sqlite")
            store = hub.open_db(db)
            store.close()
            server = hub.make_server("127.0.0.1", 0, db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(thread.join, 10)
            self.addCleanup(server.server_close)
            url = f"http://127.0.0.1:{server.server_address[1]}"
            probe = doctor.load_hub_probe(url, None)
            self.assertIsNotNone(probe)
            assert probe is not None
            self.assertEqual(probe.status, "ok")
            self.assertEqual((probe.hub_api, probe.hub_schema), (hub.HUB_API, hub.SCHEMA_VERSION))

    def test_probe_of_a_closed_loopback_port_is_unreachable(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            port = holder.getsockname()[1]
        probe = doctor.load_hub_probe(f"http://127.0.0.1:{port}", None)
        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertEqual(probe.status, "unreachable")
        self.assertTrue(probe.detail)

    def test_typed_404_refusal_maps_to_version_mismatch(self):
        import urllib.error
        from email.message import Message

        from korapy import hub

        body = json.dumps(
            {
                "api": hub.HUB_API - 1,
                "schema_version": hub.SCHEMA_VERSION,
                "error": f"unsupported hub api {hub.HUB_API - 1}:"
                f" this hub serves api {hub.HUB_API}",
            }
        ).encode("utf-8")
        refused = urllib.error.HTTPError(
            HUB_URL + "/v1/version", 404, "Not Found", Message(), io.BytesIO(body)
        )
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            probe = doctor.load_hub_probe(HUB_URL, None)
        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertEqual(probe.status, "version-mismatch")
        self.assertIn(str(hub.HUB_API - 1), probe.detail)
        self.assertIn(str(hub.HUB_API), probe.detail)
        self.assertEqual((probe.hub_api, probe.hub_schema), (hub.HUB_API - 1, hub.SCHEMA_VERSION))
        checks = doctor.run_checks(hub_env(hub_probe=probe))
        found = [check for check in checks if check.name == "hub"]
        self.assertEqual([check.status for check in found], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_bare_404_without_a_typed_body_stays_unreachable(self):
        import urllib.error
        from email.message import Message

        for raw in (b"", b"not json", None):
            with self.subTest(raw=raw):
                fp = None if raw is None else io.BytesIO(raw)
                bare = urllib.error.HTTPError(
                    HUB_URL + "/v1/version", 404, "Not Found", Message(), fp
                )
                with mock.patch("urllib.request.urlopen", side_effect=bare):
                    probe = doctor.load_hub_probe(HUB_URL, None)
                self.assertIsNotNone(probe)
                assert probe is not None
                self.assertEqual(probe.status, "unreachable")
                self.assertIn("404", probe.detail)

    def test_a_refused_probe_closes_the_error_response(self):
        """An `HTTPError` owns a temp-file wrapper: leaking it warns at GC."""
        import urllib.error
        from email.message import Message

        for code in (401, 404, 500):
            with self.subTest(code=code):
                fp = io.BytesIO(b"{}")
                refused = urllib.error.HTTPError(
                    HUB_URL + "/v1/version", code, "Not Found", Message(), fp
                )
                with mock.patch("urllib.request.urlopen", side_effect=refused):
                    doctor.load_hub_probe(HUB_URL, None)
                self.assertTrue(fp.closed, f"HTTP {code} response left open")

    def test_a_probe_survives_an_error_that_cannot_close(self):
        """A close failure stays report data, never a traceback."""
        import urllib.error
        from email.message import Message

        refused = urllib.error.HTTPError(
            HUB_URL + "/v1/version", 500, "Not Found", Message(), io.BytesIO(b"{}")
        )
        refused.close = mock.Mock(side_effect=OSError("no fd to close"))
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            probe = doctor.load_hub_probe(HUB_URL, None)
        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertEqual(probe.status, "unreachable")
        self.assertIn("500", probe.detail)

    def test_probe_never_logs_the_token(self):
        import threading

        from korapy import hub

        with tempfile.TemporaryDirectory(prefix="korapy-hub-probe-") as state:
            db = os.path.join(state, "hub.sqlite")
            store = hub.open_db(db)
            token = hub.add_host(store, "host-1")
            store.close()
            server = hub.make_server("127.0.0.1", 0, db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(thread.join, 10)
            self.addCleanup(server.server_close)
            url = f"http://127.0.0.1:{server.server_address[1]}"
            import logging

            records: list[str] = []

            class Capture(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    records.append(record.getMessage())

            logger = logging.getLogger("korapy.hub")
            capture = Capture()
            logger.addHandler(capture)
            level = logger.level
            logger.setLevel(logging.DEBUG)
            try:
                probe = doctor.load_hub_probe(url, token)
            finally:
                logger.setLevel(level)
                logger.removeHandler(capture)
            self.assertIsNotNone(probe)
            assert probe is not None
            self.assertEqual(probe.status, "ok")
            self.assertFalse(any(token in line for line in records))
            self.assertNotIn(token, probe.detail)


class QuotaFreshness(unittest.TestCase):
    T0 = 1_800_000_000.0

    def view(self, providers):
        return {"now": self.T0, "providers": providers}

    def entry(self, **fields):
        values = {
            "state": "ok",
            "windows": {"weekly": {"used_pct": 10.0, "resets_at": None}},
            "read_at": self.T0,
            "stale": False,
            "stale_reason": None,
            "mark": None,
            "reader": "fake:provider-a",
            "error": None,
        }
        values.update(fields)
        return values

    def test_no_quota_view_means_no_quota_rows(self):
        checks = doctor.run_checks(env())
        self.assertEqual([check.name for check in checks if check.name.startswith("quota:")], [])

    def test_a_fresh_reading_is_an_ok_row_with_its_age(self):
        checks = doctor.run_checks(env(quota_view=self.view({"provider-a": self.entry()})))
        rows = [check for check in checks if check.name == "quota:provider-a"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "ok")
        self.assertIn("just now", rows[0].detail)

    def test_a_too_old_reading_warns_with_its_reason(self):
        entry = self.entry(
            state="unknown", stale=True, stale_reason="too old", read_at=self.T0 - 10
        )
        checks = doctor.run_checks(env(quota_view=self.view({"provider-a": entry})))
        rows = [check for check in checks if check.name == "quota:provider-a"]
        self.assertEqual(rows[0].status, "warn")
        self.assertIn("too old", rows[0].detail)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_a_missing_reading_warns_that_it_never_ran(self):
        entry = self.entry(state="unknown", stale=True, stale_reason="never ran", read_at=None)
        checks = doctor.run_checks(env(quota_view=self.view({"provider-a": entry})))
        rows = [check for check in checks if check.name == "quota:provider-a"]
        self.assertEqual(rows[0].status, "warn")
        self.assertIn("never ran", rows[0].detail)

    def test_a_failed_reading_warns_with_the_error(self):
        entry = self.entry(
            state="unknown",
            stale=True,
            stale_reason="reader failed",
            read_at=self.T0 - 60,
            error="boom",
        )
        checks = doctor.run_checks(env(quota_view=self.view({"provider-a": entry})))
        rows = [check for check in checks if check.name == "quota:provider-a"]
        self.assertEqual(rows[0].status, "warn")
        self.assertIn("reader failed", rows[0].detail)
        self.assertIn("boom", rows[0].detail)

    def test_an_unreadable_ledger_is_one_warn_row(self):
        checks = doctor.run_checks(env(quota_view=ValueError("ledger gone")))
        rows = [check for check in checks if check.name == "quota"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "warn")
        self.assertIn("ledger gone", rows[0].detail)
        self.assertEqual(doctor.exit_code(checks), 0)


if __name__ == "__main__":
    unittest.main()
