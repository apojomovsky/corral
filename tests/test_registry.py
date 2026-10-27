"""The agent registry and `corral agent`: temp dirs and fake pid probes only."""

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, registry as reg, settings

T0 = 1_800_000_000.0
HOST = "host-1"
CFG = reg.default_config()


def dead(_pid):
    return False


def alive(_pid):
    return True


def ticket(repo="lib", number=5, project=""):
    return reg.make_ticket(repo, number, project)


class RegistryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "agents")

    def register(self, agent_id="a1", runtime="claude-code", **kw):
        kw.setdefault("now", T0)
        kw.setdefault("host", HOST)
        kw.setdefault("cfg", CFG)
        return reg.register(agent_id, runtime, path=self.path, **kw)

    def states(self, now, **kw):
        kw.setdefault("alive", alive)
        kw.setdefault("host", HOST)
        return {e["id"]: s for e, s in reg.visible(now, CFG, path=self.path, **kw)}

    def entry(self, agent_id="a1"):
        (found,) = [e for e in reg.load_all(self.path) if e["id"] == agent_id]
        return found


class LifecycleTest(RegistryCase):
    def test_register_heartbeat_done(self):
        self.register(ticket=ticket(), note="start")
        reg.heartbeat("a1", note="phase 2", now=T0 + 30, path=self.path)
        found = self.entry()
        self.assertEqual(
            (found["note"], found["last_seen"], found["started_at"]), ("phase 2", T0 + 30, T0)
        )
        reg.done(
            "a1", outcome="failed", pr="https://example.com/pull/1", now=T0 + 60, path=self.path
        )
        self.assertEqual(self.states(T0 + 61), {"a1": reg.FINISHED})
        found = self.entry()
        self.assertEqual((found["outcome"], found["pr"]), ("failed", "https://example.com/pull/1"))
        with self.assertRaises(reg.RegistryError):
            reg.heartbeat("a1", now=T0 + 62, path=self.path)

    def test_reregister_keeps_start_until_done(self):
        self.register(now=T0)
        self.register(now=T0 + 100)
        self.assertEqual(self.entry()["started_at"], T0)
        reg.done("a1", now=T0 + 110, path=self.path)
        self.register(now=T0 + 200)
        found = self.entry()
        self.assertEqual((found["started_at"], found["ended_at"]), (T0 + 200, None))

    def test_bad_input_and_unknown_id(self):
        for bad in (
            {"runtime": "bogus"},
            {"ticket": {"repo": "lib"}},
            {"ticket": {"repo": "../x", "number": 1}},
            {"ticket": {"repo": "lib", "number": 0}},
            {"ticket": {"repo": "lib", "number": 1, "extra": 1}},
            {"ticket": "lib#1"},
        ):
            with self.subTest(bad=bad), self.assertRaises(reg.RegistryError):
                self.register(**bad)
        with self.assertRaises(reg.RegistryError):
            self.register(agent_id=" ")
        with self.assertRaises(reg.RegistryError):
            reg.heartbeat("nope", path=self.path)
        with self.assertRaises(reg.RegistryError):
            reg.done("nope", path=self.path)
        self.register()
        with self.assertRaises(reg.RegistryError):
            reg.done("a1", outcome="weird", path=self.path)

    def test_make_ticket(self):
        self.assertIsNone(reg.make_ticket(None, None, None))
        self.assertEqual(
            reg.make_ticket("lib", 3, "app"), {"project": "app", "repo": "lib", "number": 3}
        )
        for args in (("lib", None, None), (None, 3, None), (None, None, "app"), ("lib", 3, "a/b")):
            with self.subTest(args=args), self.assertRaises(reg.RegistryError):
                reg.make_ticket(*args)

    def test_awkward_ids_do_not_collide(self):
        self.register("a/b")
        self.register("a_b")
        self.register("." * 3)
        self.assertEqual(len(reg.load_all(self.path)), 3)

    def test_a_second_done_is_refused_and_keeps_the_first_outcome(self):
        self.register()
        reg.done("a1", outcome="ok", now=T0 + 1, path=self.path)
        with self.assertRaises(reg.RegistryError):
            reg.done("a1", outcome="failed", now=T0 + 2, path=self.path)
        self.assertEqual(self.entry()["outcome"], "ok")

    def test_a_pid_below_one_is_refused(self):
        for pid in (0, -1, True):
            with self.subTest(pid=pid), self.assertRaises(reg.RegistryError):
                self.register(pid=pid)

    def test_entries_with_wrong_types_are_skipped(self):
        self.register()
        for name, bad in (
            ("num.json", {"id": 5, "ended_at": 1.0}),
            ("when.json", {"id": "w", "started_at": "yesterday"}),
            ("pid.json", {"id": "p", "pid": "12"}),
        ):
            with open(os.path.join(self.path, name), "w", encoding="utf-8") as handle:
                json.dump(bad, handle)
        self.assertEqual([e["id"] for e in reg.load_all(self.path)], ["a1"])
        self.register("b1", now=T0 + 1)
        self.assertEqual(sorted(e["id"] for e in reg.load_all(self.path)), ["a1", "b1"])

    def test_gc_removes_the_file_it_read_not_the_one_its_id_names(self):
        self.register("live", now=T0)
        old = dict(self.entry("live"), ended_at=T0 - 3 * reg.RETAIN_S)
        with open(os.path.join(self.path, "copy.json"), "w", encoding="utf-8") as handle:
            json.dump(old, handle)
        self.register("other", now=T0 + 1)
        names = sorted(os.listdir(self.path))
        self.assertEqual(names, [".lock", "live.json", "other.json"])

    def test_corrupt_files_are_skipped(self):
        self.register()
        with open(os.path.join(self.path, "junk.json"), "w", encoding="utf-8") as handle:
            handle.write("{nope")
        self.assertEqual([e["id"] for e in reg.load_all(self.path)], ["a1"])


class LivenessTest(RegistryCase):
    def test_stale_then_gone(self):
        self.register()
        self.assertEqual(self.states(T0 + 599), {"a1": reg.LIVE})
        self.assertEqual(self.states(T0 + 600), {"a1": reg.STALE})
        self.assertEqual(self.states(T0 + 3600), {})

    def test_heartbeat_revives_stale(self):
        self.register()
        reg.heartbeat("a1", now=T0 + 700, path=self.path)
        self.assertEqual(self.states(T0 + 701), {"a1": reg.LIVE})

    def test_dead_pid_is_gone_at_once(self):
        self.register(pid=4242)
        self.assertEqual(self.states(T0 + 1, alive=alive), {"a1": reg.LIVE})
        self.assertEqual(self.states(T0 + 1, alive=dead), {})

    def test_pid_of_another_host_is_not_probed(self):
        self.register(pid=4242, host="host-2")
        self.assertEqual(self.states(T0 + 1, alive=dead), {"a1": reg.LIVE})

    def test_done_beats_dead_pid_and_expires(self):
        self.register(pid=4242)
        reg.done("a1", now=T0 + 5, path=self.path)
        self.assertEqual(self.states(T0 + 6, alive=dead), {"a1": reg.FINISHED})
        self.assertEqual(self.states(T0 + 5 + reg.RETAIN_S + 1), {})

    def test_gone_files_are_collected_on_next_register(self):
        self.register("old", now=T0)
        self.register("new", now=T0 + 7200)
        self.assertEqual([e["id"] for e in reg.load_all(self.path)], ["new"])
        self.assertEqual(sorted(os.listdir(self.path)), [".lock", "new.json"])

    def test_a_real_pid_probe_never_signals(self):
        with mock.patch.object(reg.os, "kill") as kill:
            kill.side_effect = ProcessLookupError
            self.assertFalse(reg.pid_alive(4242))
        kill.assert_called_once_with(4242, 0)
        self.assertTrue(reg.pid_alive(os.getpid()))
        self.assertFalse(reg.pid_alive("x"))


class ConfigTest(unittest.TestCase):
    def resolved(self, **values):
        env = {f"CORRAL_REGISTRY_{k.upper()}": str(v) for k, v in values.items()}
        return settings.resolve(env=env)

    def test_defaults(self):
        self.assertEqual(
            reg.config(self.resolved()), {"stale_s": 600, "gone_s": 3600, "count_toward_cap": True}
        )
        self.assertEqual(reg.config(self.resolved()), CFG)

    def test_out_of_range_values_are_refused(self):
        for bad in ({"stale_s": 0}, {"gone_s": -1}, {"stale_s": 100, "gone_s": 50}):
            with self.subTest(bad=bad), self.assertRaises(settings.SettingsError):
                reg.config(self.resolved(**bad))

    def test_a_project_file_may_set_registry_keys(self):
        resolved = settings.resolve(project={"registry": {"stale_s": 30}}, env={})
        self.assertEqual(resolved["registry.stale_s"], 30)

    def test_every_settings_provider_is_listed(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        found = set()
        for here, _, names in os.walk(os.path.join(src, "corral")):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(here, name)
                with open(path, encoding="utf-8") as handle:
                    if re.search(r"^settings\.register\(", handle.read(), re.MULTILINE):
                        module = os.path.relpath(path, src)[: -len(".py")].replace(os.sep, ".")
                        found.add(module)
        self.assertEqual(found, set(settings.PROVIDERS))


class CapTest(RegistryCase):
    """Only live ticket workers count; one ticket is one slot."""

    def counted(self, now=T0 + 1, cfg=CFG):
        return reg.counted_tickets(now, cfg, self.path, alive, HOST)

    def test_a_ticket_counts_and_no_ticket_never_does(self):
        self.register("worker", ticket=ticket())
        self.register("reviewer")
        self.register("coordinator", runtime="human")
        self.assertEqual(self.counted(), {("lib", 5)})

    def test_two_agents_on_one_ticket_are_one_slot(self):
        self.register("w1", ticket=ticket())
        self.register("w2", runtime="omp", ticket=ticket())
        self.register("w3", ticket=ticket(number=6))
        self.register("w4", ticket=ticket(project="other"))
        self.register("w5", ticket=ticket(repo="app"))
        self.assertEqual(self.counted(), {("lib", 5), ("lib", 6), ("app", 5)})

    def test_an_explicit_and_an_inferred_ticket_are_one_slot(self):
        self.register("w1", ticket=ticket(project="proj"))
        self.register("w2")
        reg.heartbeat("w2", ticket=ticket(), now=T0, path=self.path)
        self.assertEqual(self.counted(), {("lib", 5)})

    def test_another_hosts_agents_never_count(self):
        self.register("w1", ticket=ticket(), host="host-2")
        self.assertEqual(self.counted(), set())

    def test_stale_finished_and_disabled_do_not_count(self):
        self.register("w1", ticket=ticket(number=1))
        self.register("w2", ticket=ticket(number=2))
        reg.done("w2", now=T0 + 1, path=self.path)
        self.assertEqual(self.counted(), {("lib", 1)})
        self.assertEqual(self.counted(now=T0 + 601), set())
        self.assertEqual(self.counted(cfg=dict(CFG, count_toward_cap=False)), set())


class TicketSourceTest(RegistryCase):
    def test_a_heartbeat_ticket_fills_an_empty_slot_as_inferred(self):
        self.register()
        reg.heartbeat("a1", ticket=ticket(), now=T0 + 1, path=self.path)
        self.assertEqual(
            (self.entry()["ticket"], self.entry()["ticket_source"]), (ticket(), reg.INFERRED)
        )
        self.assertEqual(reg.counted_tickets(T0 + 2, CFG, self.path, alive, HOST), {("lib", 5)})

    def test_a_heartbeat_ticket_corrects_an_older_inference(self):
        self.register()
        reg.heartbeat("a1", ticket=ticket(number=1), now=T0 + 1, path=self.path)
        reg.heartbeat("a1", ticket=ticket(number=2), now=T0 + 2, path=self.path)
        self.assertEqual(self.entry()["ticket"]["number"], 2)

    def test_a_heartbeat_ticket_never_overrides_an_explicit_register(self):
        self.register(ticket=ticket())
        reg.heartbeat("a1", ticket=ticket(number=6), now=T0 + 1, path=self.path)
        self.assertEqual(
            (self.entry()["ticket"], self.entry()["ticket_source"]), (ticket(), reg.EXPLICIT)
        )

    def test_a_bad_heartbeat_ticket_is_rejected(self):
        self.register()
        with self.assertRaises(reg.RegistryError):
            reg.heartbeat("a1", ticket={"repo": "lib"}, path=self.path)

    def test_reregister_without_a_ticket_keeps_it_and_its_source(self):
        self.register(ticket=ticket())
        self.register(now=T0 + 10)
        self.assertEqual(
            (self.entry()["ticket"], self.entry()["ticket_source"]), (ticket(), reg.EXPLICIT)
        )
        self.register("b", now=T0)
        reg.heartbeat("b", ticket=ticket(number=6), now=T0 + 1, path=self.path)
        self.register("b", now=T0 + 10)
        self.assertEqual(self.entry("b")["ticket_source"], reg.INFERRED)

    def test_reregister_with_a_ticket_is_explicit_again(self):
        self.register()
        reg.heartbeat("a1", ticket=ticket(number=6), now=T0 + 1, path=self.path)
        self.register(ticket=ticket(), now=T0 + 10)
        self.assertEqual(
            (self.entry()["ticket"], self.entry()["ticket_source"]), (ticket(), reg.EXPLICIT)
        )


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"CORRAL_STATE": tmp.name, "CORRAL_CONFIG": os.path.join(tmp.name, "none")}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("CORRAL_REGISTRY_")]:
            os.environ.pop(key)
        self.dir = os.path.join(tmp.name, "agents")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["agent", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_register_list_heartbeat_done(self):
        code, out, _ = self.run_cli(
            "register", "w1", "--runtime", "omp", "--repo", "lib", "--number", "5", "--note", "go"
        )
        self.assertEqual((code, out), (0, "registered w1\n"))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "w1.json")))
        code, out, _ = self.run_cli("list")
        self.assertEqual((code, out), (0, "w1  omp  live  lib 5  go\n"))
        self.assertEqual(self.run_cli("heartbeat", "w1", "--note", "tests")[0], 0)
        code, out, _ = self.run_cli("list", "--json")
        (row,) = json.loads(out)
        self.assertEqual((row["note"], row["status"]), ("tests", "live"))
        code, out, _ = self.run_cli("done", "w1", "--outcome", "stopped")
        self.assertEqual((code, out), (0, "w1 done (stopped)\n"))
        self.assertIn("finished", self.run_cli("list")[1])

    def test_an_empty_registry_lists_nothing(self):
        self.assertEqual(self.run_cli("list")[:2], (0, "no registered agents\n"))

    def test_usage_errors_exit_4(self):
        self.assertEqual(self.run_cli("heartbeat", "nobody")[0], 4)
        self.assertEqual(self.run_cli("register", "w1", "--runtime", "omp", "--repo", "lib")[0], 4)
        self.assertEqual(self.run_cli("register", "w1", "--runtime", "bogus")[0], 4)
        with mock.patch.dict(os.environ, {"CORRAL_REGISTRY_GONE_S": "1"}):
            code, _, err = self.run_cli("list")
        self.assertEqual(code, 4)
        self.assertIn("gone_s", err)


if __name__ == "__main__":
    unittest.main()
