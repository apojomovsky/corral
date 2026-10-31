"""The user timer for `corral drive`: unit text, state reads and the install step.

Everything runs against fakes at the single seams: a fake `run` for the
`systemctl` calls and a temporary `XDG_CONFIG_HOME` for the unit files,
so no test touches the manager, the network or the real home.
"""

import argparse
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from corral import cli, drive, units


def proc(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(["systemctl"], returncode, stdout, stderr)


def home_env():
    """An env mapping whose systemd user dir is a fresh temp dir."""
    tmp = tempfile.mkdtemp(prefix="corral-timer-home-")
    return {"XDG_CONFIG_HOME": os.path.join(tmp, "config")}


class UnitText(unittest.TestCase):
    def test_service_runs_a_oneshot_pass(self):
        text = drive.service_text()
        self.assertIn("Type=oneshot", text)
        self.assertIn("corral drive", text)
        self.assertNotIn("--plan", text)

    def test_service_exec_uses_the_shipped_binary_by_default(self):
        text = drive.service_text()
        self.assertIn(drive.TIMER_CORRAL_BIN, text)
        self.assertIn("%h", text)

    def test_service_names_an_explicit_binary_when_given(self):
        text = drive.service_text("/opt/fleet/bin/corral")
        self.assertIn("ExecStart=/opt/fleet/bin/corral drive", text)

    def test_timer_wakes_a_pass_every_five_minutes_and_catches_up(self):
        text = drive.timer_text()
        self.assertIn("OnCalendar=", text)
        self.assertIn("0/5", text)
        self.assertIn("Persistent=true", text)

    def test_timer_and_service_share_one_basename(self):
        # systemd pairs foo.timer with foo.service by default, so the
        # timer text carries no Unit line: the shared basename is the
        # pairing, pinned here.
        self.assertEqual(drive.SERVICE_NAME, drive.TIMER_NAME.replace(".timer", ".service"))
        self.assertNotIn("Unit=", drive.timer_text())


class TimerDir(unittest.TestCase):
    def test_unit_files_live_in_the_systemd_user_dir(self):
        env = home_env()
        service_path, timer_path = drive.unit_paths(env)
        self.assertEqual(service_path, os.path.join(drive.timer_dir(env), drive.SERVICE_NAME))
        self.assertEqual(timer_path, os.path.join(drive.timer_dir(env), drive.TIMER_NAME))
        self.assertTrue(drive.timer_dir(env).endswith(os.path.join("systemd", "user")))

    def test_home_config_is_the_default_base(self):
        with mock.patch.object(os.path, "expanduser", return_value="/home/someone") as home:
            directory = drive.timer_dir({})
        home.assert_called_once_with("~/.config")
        self.assertEqual(directory, os.path.join("/home/someone", "systemd", "user"))

    def test_a_relative_config_base_is_ignored(self):
        with mock.patch.object(os.path, "expanduser", return_value="/home/someone"):
            directory = drive.timer_dir({"XDG_CONFIG_HOME": "relative/path"})
        self.assertEqual(directory, os.path.join("/home/someone", "systemd", "user"))


class TimerState(unittest.TestCase):
    def test_enabled_reads_as_enabled(self):
        self.assertEqual(drive.timer_state(run=lambda argv, timeout: proc("enabled\n")), "enabled")

    def test_enabled_runtime_reads_as_enabled(self):
        self.assertEqual(
            drive.timer_state(run=lambda argv, timeout: proc("enabled-runtime\n")),
            "enabled",
        )

    def test_disabled_reads_as_disabled(self):
        state = drive.timer_state(run=lambda argv, timeout: proc("disabled\n", returncode=1))
        self.assertEqual(state, "disabled")

    def test_masked_reads_as_masked(self):
        state = drive.timer_state(run=lambda argv, timeout: proc("masked\n", returncode=1))
        self.assertEqual(state, "masked")

    def test_a_missing_unit_file_reads_as_missing(self):
        def run(argv, timeout):
            return proc("", "Failed to get unit file state for corral-drive.timer.", 1)

        self.assertEqual(drive.timer_state(run=run), "missing")

    def test_an_unreachable_bus_raises_with_exit_3(self):
        def run(argv, timeout):
            return proc("", "Failed to connect to bus: no medium found.", 1)

        with self.assertRaises(units.Unreachable) as ctx:
            drive.timer_state(run=run)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_a_missing_systemctl_binary_raises_with_exit_3(self):
        def run(argv, timeout):
            raise OSError("no such file")

        with self.assertRaises(units.Unreachable):
            drive.timer_state(run=run)

    def test_the_query_names_the_shipped_timer(self):
        seen = []

        def run(argv, timeout):
            seen.append(argv)
            return proc("enabled\n")

        drive.timer_state(run=run)
        self.assertEqual(seen, [["systemctl", "--user", "is-enabled", drive.TIMER_NAME]])


class InstallTimer(unittest.TestCase):
    def test_install_writes_both_units_then_reloads_and_enables(self):
        env = home_env()
        seen = []

        def run(argv, timeout):
            seen.append(argv)
            return proc("")

        service_path, timer_path = drive.install_timer(env=env, run=run)
        for path in (service_path, timer_path):
            with open(path, encoding="utf-8") as handle:
                self.assertTrue(handle.read().strip())
        with open(service_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), drive.service_text())
        with open(timer_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), drive.timer_text())
        self.assertEqual(
            seen,
            [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", drive.TIMER_NAME],
            ],
        )

    def test_install_is_idempotent_over_an_existing_install(self):
        env = home_env()

        def run(argv, timeout):
            return proc("")

        first = drive.install_timer(env=env, run=run)
        second = drive.install_timer(env=env, run=run)
        self.assertEqual(first, second)

    def test_a_reload_failure_is_exit_4_naming_the_step(self):
        def run(argv, timeout):
            if argv[-1] == "daemon-reload":
                return proc("", "boom", 1)
            return proc("")

        with self.assertRaises(drive.TimerError) as ctx:
            drive.install_timer(env=home_env(), run=run)
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("daemon-reload", str(ctx.exception))

    def test_an_enable_failure_is_exit_4(self):
        def run(argv, timeout):
            if argv[0:2] == ["systemctl", "--user"] and argv[-1] == drive.TIMER_NAME:
                return proc("", "boom", 1)
            return proc("")

        with self.assertRaises(drive.TimerError) as ctx:
            drive.install_timer(env=home_env(), run=run)
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_an_unreachable_bus_is_exit_3(self):
        def run(argv, timeout):
            return proc("", "Failed to connect to bus: refused.", 1)

        with self.assertRaises(units.Unreachable) as ctx:
            drive.install_timer(env=home_env(), run=run)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_an_unwritable_home_is_exit_4(self):
        # A file where the unit dir belongs pins the exit code without
        # depending on who runs the suite.
        blocked = os.path.join(tempfile.mkdtemp(prefix="corral-timer-blocked-"), "file")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("in the way")
        with self.assertRaises(drive.TimerError) as ctx2:
            drive.install_timer(
                env={"XDG_CONFIG_HOME": blocked}, run=lambda argv, timeout: proc("")
            )
        self.assertEqual(ctx2.exception.exit_code, 4)


class InstallVerb(unittest.TestCase):
    def test_the_parser_knows_install_timer(self):
        args = cli.build_parser().parse_args(["drive", "install-timer"])
        self.assertEqual(args.drive_verb, "install-timer")
        self.assertFalse(args.plan)

    def test_a_bare_drive_still_means_a_pass(self):
        args = cli.build_parser().parse_args(["drive"])
        self.assertIsNone(args.drive_verb)

    def test_a_plan_still_parses(self):
        args = cli.build_parser().parse_args(["drive", "--plan"])
        self.assertTrue(args.plan)
        self.assertIsNone(args.drive_verb)

    def test_install_runs_only_through_the_verb(self):
        import io
        from contextlib import redirect_stdout

        with mock.patch.object(drive, "install_timer", return_value=("/s", "/t")) as installed:
            with redirect_stdout(io.StringIO()):
                code = drive.cmd_drive(argparse.Namespace(plan=False, drive_verb="install-timer"))
        installed.assert_called_once_with()
        self.assertEqual(code, 0)

    def test_a_pass_never_reaches_the_install_step(self):
        with mock.patch.object(
            drive.board, "open_store", side_effect=drive.board.StoreError("no store")
        ):
            with mock.patch.object(
                drive, "install_timer", side_effect=AssertionError("must not install")
            ):
                code = drive.cmd_drive(argparse.Namespace(plan=True, drive_verb=None))
        self.assertEqual(code, 4)

    def test_plan_combined_with_install_is_a_usage_error(self):
        with mock.patch.object(
            drive, "install_timer", side_effect=AssertionError("must not install")
        ):
            code = drive.cmd_drive(argparse.Namespace(plan=True, drive_verb="install-timer"))
        self.assertEqual(code, 4)

    def test_install_reports_the_unit_paths(self):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with mock.patch.object(drive, "install_timer", return_value=("/u/svc", "/u/tmr")):
            with redirect_stdout(out):
                code = drive.cmd_drive(argparse.Namespace(plan=False, drive_verb="install-timer"))
        self.assertEqual(code, 0)
        self.assertIn("/u/svc", out.getvalue())
        self.assertIn(drive.TIMER_NAME, out.getvalue())

    def test_install_failure_maps_to_the_public_exit_codes(self):
        import io
        from contextlib import redirect_stderr

        for error, want in (
            (drive.TimerError("cannot write"), 4),
            (units.Unreachable("no bus"), 3),
        ):
            with mock.patch.object(drive, "install_timer", side_effect=error):
                with redirect_stderr(io.StringIO()):
                    code = drive.cmd_drive(
                        argparse.Namespace(plan=False, drive_verb="install-timer")
                    )
            self.assertEqual(code, want)


if __name__ == "__main__":
    unittest.main()
