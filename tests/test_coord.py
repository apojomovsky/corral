"""Coordinator lease with pid and heartbeat expiry.

Offline: every test gets its own state dir, and API tests use a
fake process table plus a fake clock (never the real state dir, the
``/proc`` of real agents, systemd, the network or the board). CLI tests
only read the test process's own ``/proc`` entries.
"""

import datetime
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

from corral import cli
from corral import coord

T0 = datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc)


class FakeProc:
    """Injectable process table: {pid: {comm, argv0, ppid, start}}."""

    def __init__(self, table):
        self.table = dict(table)

    def comm(self, pid):
        row = self.table.get(pid)
        return row.get("comm") if row else None

    def argv0(self, pid):
        row = self.table.get(pid)
        return row.get("argv0") if row else None

    def ppid(self, pid):
        row = self.table.get(pid)
        return row.get("ppid") if row else None

    def start(self, pid):
        row = self.table.get(pid)
        return row.get("start") if row else None


def live_table():
    """Self pid 100 under an omp ancestor at pid 50."""
    return FakeProc(
        {
            100: {"comm": "python3", "argv0": "python3", "ppid": 50, "start": "1000"},
            50: {"comm": "omp", "argv0": "/usr/local/bin/omp", "ppid": 1, "start": "500"},
            1: {"comm": "sleep", "argv0": "sleep", "ppid": 0, "start": "10"},
        }
    )


class CoordApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.proc = live_table()

    def test_acquire_on_free_sets_generation_one(self):
        lease = coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        self.assertEqual(lease["generation"], 1)
        self.assertEqual(lease["holder"], "h1")
        self.assertEqual(lease["runtime"], "omp")
        self.assertEqual(lease["pid"], 50)
        self.assertEqual(lease["pid_start"], "500")

    def test_second_acquire_refused_while_holder_alive_and_fresh(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        with self.assertRaises(coord.LeaseHeldError) as ctx:
            coord.acquire(holder="h2", root=self.root, now=T0, proc=self.proc, self_pid=100)
        self.assertIn("h1", str(ctx.exception))

    def test_freed_by_dead_pid(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        dead = live_table()
        del dead.table[50]
        info = coord.describe(root=self.root, now=T0, proc=dead)
        self.assertTrue(info["free"])
        self.assertEqual(info["reason"], "dead-pid")
        lease = coord.acquire(holder="h2", root=self.root, now=T0, proc=dead, self_pid=100)
        self.assertEqual(lease["generation"], 2)

    def test_unreadable_holder_start_is_dead_not_held(self):
        nostart = live_table()
        row = dict(nostart.table[50])
        del row["start"]
        nostart.table[50] = row
        coord.acquire(holder="h1", root=self.root, now=T0, proc=nostart, self_pid=100)
        info = coord.describe(root=self.root, now=T0, proc=nostart)
        self.assertTrue(info["free"])
        self.assertEqual(info["reason"], "dead-pid")
        lease = coord.acquire(holder="h2", root=self.root, now=T0, proc=nostart, self_pid=100)
        self.assertEqual(lease["generation"], 2)

    def test_freed_by_reused_pid(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        reused = live_table()
        reused.table[50] = dict(reused.table[50], start="501")
        info = coord.describe(root=self.root, now=T0, proc=reused)
        self.assertTrue(info["free"])
        self.assertEqual(info["reason"], "pid-reused")
        lease = coord.acquire(holder="h2", root=self.root, now=T0, proc=reused, self_pid=100)
        self.assertEqual(lease["generation"], 2)

    def test_freed_by_stale_heartbeat(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        late = T0 + datetime.timedelta(seconds=coord.COORD_TTL_S + 1)
        info = coord.describe(root=self.root, now=late, proc=self.proc)
        self.assertTrue(info["free"])
        self.assertEqual(info["reason"], "stale-heartbeat")
        self.assertEqual(info["heartbeat_age_s"], coord.COORD_TTL_S + 1)
        lease = coord.acquire(holder="h2", root=self.root, now=late, proc=self.proc, self_pid=100)
        self.assertEqual(lease["generation"], 2)

    def test_fresh_heartbeat_keeps_lease(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        soon = T0 + datetime.timedelta(seconds=coord.COORD_TTL_S - 1)
        info = coord.describe(root=self.root, now=soon, proc=self.proc)
        self.assertFalse(info["free"])
        self.assertEqual(info["reason"], "held")

    def test_heartbeat_and_release_refuse_stale_generation(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        with self.assertRaises(coord.StaleLeaseError) as ctx:
            coord.heartbeat(99, root=self.root, now=T0)
        self.assertIn("generation 1 is current", str(ctx.exception))
        with self.assertRaises(coord.StaleLeaseError):
            coord.release(99, root=self.root)

    def test_heartbeat_refreshes_and_release_keeps_generation(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        later = T0 + datetime.timedelta(seconds=60)
        lease = coord.heartbeat(1, root=self.root, now=later)
        self.assertEqual(lease["heartbeat_at"], later.isoformat())
        kept = coord.release(1, root=self.root)
        self.assertIsNone(kept["holder"])
        self.assertEqual(kept["generation"], 1)
        info = coord.describe(root=self.root, now=later, proc=self.proc)
        self.assertTrue(info["free"])
        self.assertEqual(info["reason"], "released")
        again = coord.acquire(holder="h2", root=self.root, now=later, proc=self.proc, self_pid=100)
        self.assertEqual(again["generation"], 2)

    def test_takeover_bumps_generation(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        lease = coord.takeover(
            holder="h2", force=True, root=self.root, now=T0, proc=self.proc, self_pid=100
        )
        self.assertEqual(lease["generation"], 2)
        self.assertEqual(lease["holder"], "h2")

    def test_takeover_needs_force(self):
        with self.assertRaises(coord.UsageError):
            coord.takeover(root=self.root, now=T0, proc=self.proc, self_pid=100)

    def test_require_current(self):
        coord.acquire(holder="h1", root=self.root, now=T0, proc=self.proc, self_pid=100)
        self.assertEqual(coord.require_current(1, root=self.root), 1)
        with self.assertRaises(coord.StaleLeaseError) as ctx:
            coord.require_current(7, root=self.root)
        self.assertIn("generation 1 is current", str(ctx.exception))
        self.assertEqual(ctx.exception.current, 1)

    def test_concurrent_acquires_have_exactly_one_winner(self):
        wins, losses = [], []
        start = threading.Barrier(2)

        def run():
            start.wait()
            try:
                coord.acquire(holder="h", root=self.root, now=T0, proc=self.proc, self_pid=100)
                wins.append(1)
            except coord.LeaseHeldError:
                losses.append(1)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), 1)

    def test_holder_defaults_to_runtime_and_pid(self):
        lease = coord.acquire(root=self.root, now=T0, proc=self.proc, self_pid=100)
        self.assertEqual(lease["holder"], "omp:50")

    def test_find_holder_prefers_nearest_claude_over_omp(self):
        table = FakeProc(
            {
                100: {"comm": "x", "argv0": "x", "ppid": 60, "start": "1"},
                60: {"comm": "claude", "argv0": "claude", "ppid": 50, "start": "2"},
                50: {"comm": "omp", "argv0": "omp", "ppid": 1, "start": "3"},
                1: {"comm": "init", "argv0": "init", "ppid": 0, "start": "4"},
            }
        )
        self.assertEqual(coord.find_holder(self_pid=100, proc=table), (60, "claude-code"))

    def test_find_holder_matches_rewritten_process_title(self):
        # A Claude Code background session's comm is its version
        # and its argv is one rewritten title string.
        table = FakeProc(
            {
                100: {"comm": "python3", "argv0": "python3", "ppid": 90, "start": "1"},
                90: {"comm": "bash", "argv0": "/bin/bash", "ppid": 60, "start": "2"},
                60: {"comm": "2.1.299", "argv0": "claude bg-spare", "ppid": 1, "start": "3"},
                1: {"comm": "init", "argv0": "init", "ppid": 0, "start": "4"},
            }
        )
        self.assertEqual(coord.find_holder(self_pid=100, proc=table), (60, "claude-code"))

    def test_runtime_of_ignores_names_merely_containing_claude(self):
        self.assertIsNone(coord._runtime_of("x", "claude-helper run"))


class CoordCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_STATE": tmp.name, "CORRAL_PROJECT": "demo"})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_acquire_then_second_acquire_refused(self):
        code, out, _ = self._run(["coord", "acquire", "--holder", "cli-one"])
        self.assertEqual(code, 0)
        self.assertIn("generation 1", out)
        code, _, err = self._run(["coord", "acquire", "--holder", "cli-two"])
        self.assertEqual(code, cli.RACE)
        self.assertIn("cli-one", err)

    def test_status_json_keys(self):
        self._run(["coord", "acquire", "--holder", "cli-one"])
        code, out, _ = self._run(["coord", "status", "--json"])
        self.assertEqual(code, 0)
        info = json.loads(out)
        self.assertEqual(
            set(info),
            {
                "free",
                "reason",
                "holder",
                "runtime",
                "pid",
                "pid_alive",
                "heartbeat_age_s",
                "generation",
            },
        )
        self.assertFalse(info["free"])
        self.assertEqual(info["holder"], "cli-one")
        self.assertTrue(info["pid_alive"])

    def test_heartbeat_and_release_refuse_stale_generation(self):
        self._run(["coord", "acquire", "--holder", "cli-one"])
        code, _, err = self._run(["coord", "heartbeat", "--generation", "99"])
        self.assertEqual(code, cli.RACE)
        self.assertIn("generation 1 is current", err)
        code, _, err = self._run(["coord", "release", "--generation", "99"])
        self.assertEqual(code, cli.RACE)
        self.assertIn("generation 1 is current", err)
        code, out, _ = self._run(["coord", "heartbeat", "--generation", "1"])
        self.assertEqual(code, 0)
        self.assertIn("generation 1", out)
        code, out, _ = self._run(["coord", "release", "--generation", "1"])
        self.assertEqual(code, 0)
        self.assertIn("generation 1", out)

    def test_takeover_needs_force_then_bumps(self):
        self._run(["coord", "acquire", "--holder", "cli-one"])
        code, _, err = self._run(["coord", "takeover"])
        self.assertEqual(code, cli.USAGE)
        self.assertIn("--force", err)
        code, out, _ = self._run(["coord", "takeover", "--force", "--holder", "cli-two"])
        self.assertEqual(code, 0)
        self.assertIn("generation 2", out)

    def test_heartbeat_needs_generation(self):
        code, _, _ = self._run(["coord", "heartbeat"])
        self.assertEqual(code, cli.USAGE)

    def test_generation_never_resets_across_release(self):
        self._run(["coord", "acquire", "--holder", "cli-one"])
        self._run(["coord", "release", "--generation", "1"])
        code, out, _ = self._run(["coord", "acquire", "--holder", "cli-two"])
        self.assertEqual(code, 0)
        self.assertIn("generation 2", out)


if __name__ == "__main__":
    unittest.main()


class PerProjectTest(unittest.TestCase):
    """Each project has its own lease, logbook and events."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name
        env = mock.patch.dict(os.environ, {"CORRAL_STATE": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CORRAL_PROJECT", None)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_two_projects_hold_their_leases_at_once(self):
        code, out, _ = self._run(["coord", "acquire", "--project", "app", "--holder", "a"])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["coord", "acquire", "--project", "lib", "--holder", "b"])
        self.assertEqual(code, 0, out)
        code, _, err = self._run(["coord", "acquire", "--project", "app", "--holder", "c"])
        self.assertEqual(code, cli.RACE)
        self.assertIn("a", err)

    def test_files_live_under_the_project_root(self):
        self._run(["coord", "acquire", "--project", "app", "--holder", "a"])
        self._run(["coord", "log", "note", "x", "--project", "app"])
        root = os.path.join(self.state, "projects", "app", "coord")
        self.assertTrue(os.path.isfile(os.path.join(root, "lease.json")))
        self.assertTrue(os.path.isfile(os.path.join(root, "journal.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "coord")))

    def test_the_env_names_the_project_and_the_flag_wins(self):
        with mock.patch.dict(os.environ, {"CORRAL_PROJECT": "app"}):
            self._run(["coord", "acquire", "--holder", "a"])
            code, out, _ = self._run(["coord", "status", "--project", "lib", "--json"])
        self.assertTrue(json.loads(out)["free"])
        self.assertEqual(coord.project_root("app"), os.path.join(self.state, "projects", "app"))

    def test_no_project_is_a_usage_error(self):
        code, _, err = self._run(["coord", "status"])
        self.assertEqual(code, cli.USAGE)
        self.assertIn("CORRAL_PROJECT", err)
        self.assertFalse(os.path.exists(os.path.join(self.state, "projects")))

    def test_a_project_name_must_be_one_path_segment(self):
        for bad in ("../x", "a/b", ".", "..", "-x", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(coord.UsageError):
                    coord.project_root(bad, env={"CORRAL_STATE": self.state})
