"""The drive safety pass: journal, snapshot section, pause file, day cap. Fakes only.

The store is a temporary board store; every host read (projects, live
state, limits, quotas, ledger, labels) is injected plain data; the
journal, pause file and day counter live in temporary directories,
never the real state dir. No tracker, no network, no subprocess.
"""

import errno
import fcntl
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from corral import admission, drive, snapshot
from corral import projects as projects_mod
from tests.test_drive import policy_source, stored
from tests.test_drive_claims import FakeLauncher, drive_pass

NOW = 20000.0


def safety_paths(tmp):
    """Temporary journal, pause and day-counter paths plus a max-starts cap."""
    return {
        "journal_path": os.path.join(tmp, "drive", "journal.jsonl"),
        "pause_path": os.path.join(tmp, "drive.pause"),
        "day_path": os.path.join(tmp, "drive.starts.json"),
        "max_starts": 50,
    }


def journal_lines(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle.read().splitlines() if line.strip()]


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-safety-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.paths = safety_paths(self.tmp.name)

    def passing(self, store, **fields):
        out, err = io.StringIO(), io.StringIO()
        params = dict(self.paths)
        params.update(fields)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            policy_dir=self.policies,
            launcher=FakeLauncher(),
            **params,
        )
        return code, out.getvalue(), err.getvalue()

    def test_pass_appends_well_formed_lines(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, _ = self.passing(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        entries = journal_lines(self.paths["journal_path"])
        self.assertGreaterEqual(len(entries), 2)
        for entry in entries:
            self.assertIsInstance(entry, dict)
            self.assertIn("event", entry)
            self.assertIn("at", entry)
        self.assertEqual(entries[0]["event"], "pass_start")
        self.assertEqual(entries[-1]["event"], "pass_end")
        self.assertEqual(entries[-1]["exit"], 0)

    def test_starts_and_skips_are_journaled_with_ticket_and_reason(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "closed")])
        self.addCleanup(store.close)
        code, _, _ = self.passing(
            store, found=[("blue", [("app", 7, "start"), ("app", 9, "start")])]
        )
        self.assertEqual(code, 0)
        entries = journal_lines(self.paths["journal_path"])
        starts = [e for e in entries if e["event"] == "start"]
        skips = [e for e in entries if e["event"] == "skip"]
        self.assertEqual([s["ticket"] for s in starts], ["app#7"])
        self.assertTrue(skips)
        for entry in skips:
            self.assertIn("ticket", entry)
            self.assertTrue(entry["reason"])

    def test_plan_writes_no_journal(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        params = dict(self.paths)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=True,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            **params,
        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.paths["journal_path"]))

    def test_orphan_release_is_journaled(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-1000-acme--app-7", "host-1", now=NOW - 8000.0)
        code, _, _ = self.passing(store, now=NOW)
        self.assertEqual(code, 0)
        entries = journal_lines(self.paths["journal_path"])
        releases = [e for e in entries if e["event"] == "release"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["ticket"], "app#7")

    def test_pass_end_carries_verdict_counts(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, _ = self.passing(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        entries = journal_lines(self.paths["journal_path"])
        end = entries[-1]
        self.assertEqual(end["started"], 1)
        self.assertEqual(end["released"], 0)
        self.assertEqual(end["gave_up"], 0)

    def test_no_launcher_journals_no_start_rows(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            launcher=None,
            **self.paths,
        )
        self.assertEqual(code, 0)
        entries = journal_lines(self.paths["journal_path"])
        self.assertEqual([entry for entry in entries if entry["event"] == "start"], [])
        end = entries[-1]
        self.assertEqual(end["event"], "pass_end")
        self.assertEqual(end["started"], 0)


class PauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-pause-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.paths = safety_paths(self.tmp.name)

    def passing(self, store, **fields):
        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        params = dict(self.paths)
        params.update(fields)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            policy_dir=self.policies,
            launcher=launcher,
            **params,
        )
        return code, out.getvalue(), err.getvalue(), launcher

    def test_pause_file_exits_one_with_the_files_reason_and_starts_nothing(self):
        with open(self.paths["pause_path"], "w", encoding="utf-8") as handle:
            handle.write("operator break\nsecond line\n")
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, err, launcher = self.passing(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 1)
        self.assertIn("operator break", err)
        self.assertEqual(launcher.calls, [])
        self.assertIsNone(store.claim_of("app", 7))
        self.assertFalse(os.path.exists(self.paths["journal_path"]))

    def test_pause_file_also_stops_a_plan(self):
        with open(self.paths["pause_path"], "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        params = dict(self.paths)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=True,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            **params,
        )
        self.assertEqual(code, 1)
        self.assertIn("operator break", err.getvalue())

    def test_missing_pause_file_passes_normally(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, _, launcher = self.passing(store, found=[("blue", [("app", 7, "start")])])
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)


class DayCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-cap-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.paths = safety_paths(self.tmp.name)

    def passing(self, store, **fields):
        out, err = io.StringIO(), io.StringIO()
        launcher = FakeLauncher()
        params = dict(self.paths)
        params.update(fields)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            policy_dir=self.policies,
            launcher=launcher,
            **params,
        )
        return code, out.getvalue(), err.getvalue(), launcher

    def test_cap_counts_starts_and_stops_the_pass_with_exit_one(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 8, "open")])
        self.addCleanup(store.close)
        code, out, _, launcher = self.passing(
            store,
            found=[("blue", [("app", 7, "start"), ("app", 8, "start")])],
            max_starts=1,
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(launcher.calls), 1)
        self.assertIn("day cap", out)
        with open(self.paths["day_path"], encoding="utf-8") as handle:
            counted = json.load(handle)
        self.assertEqual(counted["starts"], 1)
        entries = journal_lines(self.paths["journal_path"])
        self.assertTrue(any(e["event"] == "cap" for e in entries))

    def test_capped_pass_starts_nothing_when_nothing_remains(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, _, launcher = self.passing(
            store, found=[("blue", [("app", 7, "start")])], max_starts=0
        )
        self.assertEqual(code, 1)
        self.assertEqual(launcher.calls, [])

    def test_plan_consumes_no_day_starts(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        params = dict(self.paths)
        params["max_starts"] = 1
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=True,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            **params,
        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.paths["day_path"]))

    def test_counter_resets_on_date_change(self):
        import datetime

        yesterday = (
            (
                datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
                - datetime.timedelta(days=1)
            )
            .date()
            .isoformat()
        )
        with open(self.paths["day_path"], "w", encoding="utf-8") as handle:
            json.dump({"date": yesterday, "starts": 50}, handle)
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, _, launcher = self.passing(
            store, found=[("blue", [("app", 7, "start")])], now=NOW, max_starts=50
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(launcher.calls), 1)

    def test_negative_cap_is_a_usage_error(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, _, err, launcher = self.passing(
            store, found=[("blue", [("app", 7, "start")])], max_starts=-1
        )
        self.assertEqual(code, 4)
        self.assertEqual(launcher.calls, [])


class RotationTest(unittest.TestCase):
    def test_past_the_max_oldest_lines_move_to_a_dated_archive(self):
        tmp = tempfile.TemporaryDirectory(prefix="corral-drive-journal-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "drive", "journal.jsonl")
        for seq in range(drive.JOURNAL_MAX + 5):
            drive.log_event(path, {"event": "skip", "at": float(NOW), "seq": seq})
        with open(path, encoding="utf-8") as handle:
            live = [line for line in handle.read().splitlines() if line.strip()]
        self.assertEqual(len(live), drive.JOURNAL_MAX)
        names = os.listdir(os.path.join(tmp.name, "drive"))
        archives = [n for n in names if n.startswith("journal-") and n.endswith(".jsonl")]
        self.assertEqual(len(archives), 1)
        with open(os.path.join(tmp.name, "drive", archives[0]), encoding="utf-8") as handle:
            self.assertEqual(len(handle.read().splitlines()), 5)
        for line in live:
            json.loads(line)


class DriveShowTest(unittest.TestCase):
    def test_human_output_names_last_pass_and_starts(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        doc = snapshot.build(
            NOW,
            "host-1",
            [],
            {},
            {"admission.total_cap": 4, "admission.repo_cap": 0},
            drive_view={
                "last_pass_at": NOW,
                "last_exit": 1,
                "lock_held": False,
                "paused": True,
                "pause_reason": "operator break",
                "starts_today": 50,
                "starts_remaining": 0,
                "max_starts_per_day": 50,
                "started": 0,
                "skipped_by_reason": {"cap": 2},
                "released": 0,
                "gave_up": 0,
            },
        )
        with redirect_stdout(out), redirect_stderr(err):
            snapshot._show(doc)
        self.assertIn("drive: last pass", out.getvalue())
        self.assertIn("50/50 starts today", out.getvalue())
        self.assertIn("paused: operator break", out.getvalue())
        self.assertIn("skip cap: 2", out.getvalue())


class DriveStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-status-")
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.sqlite")
        self.lock = os.path.join(self.tmp.name, "drive.lock")
        self.policies = os.path.join(self.tmp.name, "policies")
        os.makedirs(self.policies)
        self.paths = safety_paths(self.tmp.name)

    def status(self, **over):
        params = {
            "now": NOW,
            "max_starts": 50,
            "journal_path": self.paths["journal_path"],
            "pause_path": self.paths["pause_path"],
            "day_path": self.paths["day_path"],
            "lock_path": self.lock,
        }
        params.update(over)
        return drive.drive_status(**params)

    def test_status_reports_the_section_six_fields(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        params = dict(self.paths)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            launcher=FakeLauncher(),
            **params,
        )
        self.assertEqual(code, 0)
        status = self.status()
        self.assertEqual(status["last_exit"], 0)
        self.assertIsNotNone(status["last_pass_at"])
        self.assertFalse(status["lock_held"])
        self.assertFalse(status["paused"])
        self.assertIsNone(status["pause_reason"])
        self.assertEqual(status["starts_today"], 1)
        self.assertEqual(status["starts_remaining"], 49)
        self.assertEqual(status["started"], 1)
        self.assertEqual(status["released"], 0)
        self.assertEqual(status["gave_up"], 0)

    def test_missing_journal_is_a_section_error_not_a_crash(self):
        with self.assertRaises(drive.JournalError):
            self.status()
        doc = snapshot.build(
            NOW,
            "host-1",
            [],
            {},
            {"admission.total_cap": 4, "admission.repo_cap": 0},
            drive_view=drive.JournalError("no drive passes recorded yet"),
        )
        self.assertEqual(doc["drive"], {"error": "no drive passes recorded yet"})

    def test_corrupt_journal_is_a_section_error_not_a_crash(self):
        os.makedirs(os.path.dirname(self.paths["journal_path"]), exist_ok=True)
        with open(self.paths["journal_path"], "w", encoding="utf-8") as handle:
            handle.write('{"event": "pass_start", "at": 1}\n')
            handle.write("not json at all\n")
        with self.assertRaises(drive.JournalError):
            self.status()

    def test_pass_end_without_exit_is_a_section_error_not_a_crash(self):
        os.makedirs(os.path.dirname(self.paths["journal_path"]), exist_ok=True)
        with open(self.paths["journal_path"], "w", encoding="utf-8") as handle:
            handle.write('{"event": "pass_start", "at": 1}\n')
            handle.write('{"event": "pass_end", "at": 2}\n')
        with self.assertRaises(drive.JournalError):
            self.status()

    def test_pass_end_with_non_int_exit_is_a_section_error(self):
        os.makedirs(os.path.dirname(self.paths["journal_path"]), exist_ok=True)
        with open(self.paths["journal_path"], "w", encoding="utf-8") as handle:
            handle.write('{"event": "pass_start", "at": 1}\n')
            handle.write('{"event": "pass_end", "at": 2, "exit": "zero"}\n')
        with self.assertRaises(drive.JournalError):
            self.status()
        try:
            self.status()
        except drive.JournalError as err:
            doc = snapshot.build(
                NOW,
                "host-1",
                [],
                {},
                {"admission.total_cap": 4, "admission.repo_cap": 0},
                drive_view=err,
            )
        self.assertIn("error", doc["drive"])

    def test_pause_file_shows_in_status(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        params = dict(self.paths)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            launcher=FakeLauncher(),
            **params,
        )
        self.assertEqual(code, 0)
        with open(self.paths["pause_path"], "w", encoding="utf-8") as handle:
            handle.write("operator break\n")
        status = self.status()
        self.assertTrue(status["paused"])
        self.assertEqual(status["pause_reason"], "operator break")

    def test_held_lock_shows_in_status(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        out, err = io.StringIO(), io.StringIO()
        params = dict(self.paths)
        code = drive_pass(
            store,
            self.lock,
            out,
            err,
            plan=False,
            found=[("blue", [("app", 7, "start")])],
            policy_dir=self.policies,
            launcher=FakeLauncher(),
            **params,
        )
        self.assertEqual(code, 0)
        with open(self.lock, "w") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            self.assertTrue(self.status()["lock_held"])
        self.assertFalse(self.status()["lock_held"])


class CheckLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-drive-check-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "drive.lock")
        with open(self.path, "w", encoding="utf-8"):
            pass

    def test_unexpected_flock_error_still_closes_the_fd(self):
        real_close = os.close

        def broken(_fd, _op):
            raise RuntimeError("flock exploded")

        with (
            mock.patch.object(drive, "_flock", side_effect=broken),
            mock.patch("os.close", side_effect=real_close, autospec=True) as closed,
        ):
            with self.assertRaises(RuntimeError):
                drive.check_lock(self.path)
        self.assertEqual(closed.call_count, 1)

    def test_non_contention_flock_error_closes_the_fd_and_raises(self):
        real_close = os.close

        def broken(_fd, _op):
            raise OSError(errno.EINTR, "interrupted")

        with (
            mock.patch.object(drive, "_flock", side_effect=broken),
            mock.patch("os.close", side_effect=real_close, autospec=True) as closed,
        ):
            with self.assertRaises(OSError):
                drive.check_lock(self.path)
        self.assertEqual(closed.call_count, 1)


class DriveSettingsTest(unittest.TestCase):
    def test_max_starts_per_day_defaults_to_fifty(self):
        from corral import settings

        self.assertIn("corral.drive", settings.PROVIDERS)
        resolved = settings.resolve(host={}, project={}, env={})
        self.assertEqual(resolved["drive.max_starts_per_day"], 50)


if __name__ == "__main__":
    unittest.main()
