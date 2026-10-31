"""Tests for corral.disk: pressure, the reclaim plan, `--apply`, and the gate.

All seams are fakes. The real removal seam is parked behind
`_REAL_PRODUCTION_REMOVE` and replaced at module level with
`_forbidden_remove`, so any test reaching a real deletion by accident
fails; `_apply` patches in a contained fake, and exactly one test named
`test_real_production_remove_contained_to_temp_dir` exercises the real
seam, only against temp dirs.
"""

import argparse
import inspect
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from corral import disk, diskcli, doctor, gc, launch, settings
from tests.test_doctor import env as doctor_env

GB = 1024**3

_REAL_PRODUCTION_REMOVE = diskcli.production_remove


def _forbidden_remove(roots):
    def remove(path):
        raise AssertionError("tests must not reach the real removal seam")

    return remove


diskcli.production_remove = _forbidden_remove  # ty: ignore[invalid-assignment]

T0 = 1_700_000_000


def usage_free(free_gb, total_gb=100):
    def usage(path):
        return (total_gb * GB, (total_gb - free_gb) * GB, free_gb * GB)

    return usage


def roomy(path):
    return (100 * GB, 10 * GB, 90 * GB)


def finished_entry(repo, number, ended_ago=100):
    return {
        "id": f"agent-{repo}-{number}",
        "started_at": T0 - 2000,
        "last_seen": T0 - 2000,
        "ended_at": T0 - ended_ago,
        "ticket": {"project": "", "repo": repo, "number": number},
    }


def live_entry(repo, number):
    return {
        "id": f"agent-{repo}-{number}",
        "started_at": T0 - 10,
        "last_seen": T0,
        "ticket": {"project": "", "repo": repo, "number": number},
    }


class DiskCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-disk-")
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.cache = os.path.join(self.tmp.name, "cache")
        self.sessions = os.path.join(self.tmp.name, "sessions")
        for path in (self.state, self.cache, self.sessions):
            os.makedirs(path)
        self.roots = (self.state, self.cache, self.sessions)

    def plan(self, **overrides):
        args = dict(
            state_dir=self.state,
            cache_dir=self.cache,
            sessions_dir=self.sessions,
            roots=self.roots,
            min_free_pct=10,
            min_free_gb=5,
            session_retention_days=14,
            usage=roomy,
            walk=lambda path: 0,
            now=T0,
            entries=[],
            list_fn=lambda: set(),
            gc_rows=[],
        )
        args.update(overrides)
        return disk.plan(**args)

    def kinds(self, planned):
        return [(c.kind, c.path, c.safe) for c in planned.candidates]


class PressureTest(DiskCase):
    def reading(self, free_gb, total_gb=100, path="/x"):
        return disk.Reading(path, total_gb * GB, free_gb * GB)

    def test_ok_when_everything_fits(self):
        self.assertEqual(disk.pressure([self.reading(20)], min_free_pct=10, min_free_gb=5), "ok")

    def test_low_under_either_threshold(self):
        by_pct = disk.pressure([self.reading(9, total_gb=100)], min_free_pct=10, min_free_gb=5)
        by_gb = disk.pressure([self.reading(4, total_gb=30)], min_free_pct=10, min_free_gb=5)
        self.assertEqual(by_pct, "low")
        self.assertEqual(by_gb, "low")

    def test_critical_under_half_of_either_threshold(self):
        by_pct = disk.pressure([self.reading(4, total_gb=100)], min_free_pct=10, min_free_gb=5)
        by_gb = disk.pressure([self.reading(2, total_gb=30)], min_free_pct=10, min_free_gb=5)
        self.assertEqual(by_pct, "critical")
        self.assertEqual(by_gb, "critical")

    def test_critical_wins_over_low_on_another_filesystem(self):
        readings = [self.reading(9, path="/a"), self.reading(1, path="/b")]
        self.assertEqual(disk.pressure(readings, min_free_pct=10, min_free_gb=5), "critical")

    def test_unknown_when_a_path_cannot_be_read(self):
        def failing(path):
            raise OSError("no such device")

        readings = disk.read(["/a", "/b"], usage=failing)
        self.assertTrue(all(r.free is None for r in readings))
        self.assertEqual(disk.pressure(readings, min_free_pct=10, min_free_gb=5), "unknown")

    def test_low_outranks_unknown(self):
        readings = [
            self.reading(9, path="/a"),
            disk.Reading("/b", None, None),
        ]
        self.assertEqual(disk.pressure(readings, min_free_pct=10, min_free_gb=5), "low")

    def test_nonsense_readings_are_unknown(self):
        readings = disk.read(["/a"], usage=lambda path: (-1, 0, -5))
        self.assertEqual(disk.pressure(readings, min_free_pct=10, min_free_gb=5), "unknown")


class MeasureTest(DiskCase):
    def test_production_walk_counts_files_not_symlinks(self):
        outside = os.path.join(self.tmp.name, "outside.bin")
        with open(outside, "wb") as handle:
            handle.write(b"x" * 4096)
        target = os.path.join(self.state, "real.bin")
        with open(target, "wb") as handle:
            handle.write(b"y" * 100)
        os.symlink(outside, os.path.join(self.state, "link.bin"))
        os.symlink(target, os.path.join(self.state, "inner-link.bin"))
        size = disk.measure(self.state)
        self.assertGreaterEqual(size, 100)
        self.assertLess(size, 4096)

    def test_production_walk_skips_unreadable_dirs(self):
        self.assertEqual(disk.measure(os.path.join(self.tmp.name, "missing")), 0)


class PrBodyTest(DiskCase):
    def write_pr_body(self, name, age_s, size=10):
        path = os.path.join(self.cache, name)
        with open(path, "wb") as handle:
            handle.write(b"z" * size)
        os.utime(path, (T0 - age_s, T0 - age_s))
        return path

    def test_old_pr_body_is_a_safe_candidate(self):
        old = 8 * 24 * 3600
        path = self.write_pr_body("pr-12-abcdef.md", old, size=30)
        planned = self.plan(walk=lambda p: 30)
        rows = [c for c in planned.candidates if c.kind == "pr-body"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].path, path)
        self.assertTrue(rows[0].safe)
        self.assertIn("8d", rows[0].proof)

    def test_young_pr_body_is_not_planned(self):
        self.write_pr_body("pr-12-abcdef.md", 3600)
        planned = self.plan()
        self.assertEqual([c for c in planned.candidates if c.kind == "pr-body"], [])

    def test_non_matching_names_are_ignored(self):
        self.write_pr_body("notes.md", 30 * 24 * 3600)
        self.write_pr_body("pr-12", 30 * 24 * 3600)
        planned = self.plan()
        self.assertEqual([c for c in planned.candidates if c.kind == "pr-body"], [])

    def test_symlinked_pr_body_is_dropped_with_a_note(self):
        outside = os.path.join(self.tmp.name, "outside.md")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("x")
        link = os.path.join(self.cache, "pr-99-abcdef.md")
        os.symlink(outside, link)
        planned = self.plan()
        self.assertEqual([c for c in planned.candidates if c.kind == "pr-body"], [])
        self.assertTrue(any("is a symlink" in n for n in planned.notes))

    def test_a_symlink_inside_the_roots_is_a_symlink_not_an_escape(self):
        inside = os.path.join(self.cache, "real.md")
        with open(inside, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.symlink(inside, os.path.join(self.cache, "pr-98-abcdef.md"))
        planned = self.plan()
        notes = [n for n in planned.notes if "pr-98-abcdef.md" in n]
        self.assertEqual(len(notes), 1)
        self.assertIn("is a symlink", notes[0])
        self.assertNotIn("outside", notes[0])


class SessionTest(DiskCase):
    def make_session(self, repo, number, age_s=20 * 24 * 3600, size=0):
        path = os.path.join(self.sessions, f"{repo}-{number}")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "activity.jsonl"), "wb") as handle:
            handle.write(b"q" * size)
        old = T0 - age_s
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                os.utime(os.path.join(root, name), (old, old))
            os.utime(root, (old, old))
        return path

    def test_done_old_idle_session_is_safe(self):
        path = self.make_session("demo", 7)
        entries = [finished_entry("demo", 7)]
        planned = self.plan(entries=entries, walk=lambda p: 41)
        rows = [c for c in planned.candidates if c.path == path]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "session")
        self.assertTrue(rows[0].safe)
        self.assertEqual(rows[0].bytes, 41)

    def test_session_with_a_live_unit_is_kept(self):
        self.make_session("demo", 7)
        planned = self.plan(entries=[finished_entry("demo", 7)], list_fn=lambda: {("demo", 7)})
        rows = [c for c in planned.candidates if c.kind == "session"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].safe)
        self.assertIn("live unit", rows[0].proof)

    def test_session_with_no_registry_entry_is_unproven(self):
        path = self.make_session("demo", 7)
        planned = self.plan(entries=[], walk=lambda p: 3)
        rows = [c for c in planned.candidates if c.path == path]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "unproven")
        self.assertFalse(rows[0].safe)
        self.assertIn("no registry entry", rows[0].proof)

    def test_session_with_an_open_entry_is_kept(self):
        self.make_session("demo", 7)
        planned = self.plan(entries=[live_entry("demo", 7)])
        rows = [c for c in planned.candidates if c.kind == "session"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].safe)
        self.assertIn("still open", rows[0].proof)

    def test_young_session_is_kept(self):
        self.make_session("demo", 7, age_s=3600)
        planned = self.plan(entries=[finished_entry("demo", 7)])
        rows = [c for c in planned.candidates if c.kind == "session"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].safe)
        self.assertIn("retention", rows[0].proof)

    def test_unreadable_listing_keeps_every_session(self):
        def down():
            raise RuntimeError("bus gone")

        path = self.make_session("demo", 7)
        planned = self.plan(entries=[finished_entry("demo", 7)], list_fn=down)
        rows = [c for c in planned.candidates if c.path == path]
        self.assertFalse(rows[0].safe)
        self.assertTrue(any("unreadable" in n for n in planned.notes))

    def make_leaf(self, repo, number, role, gen, age_s=20 * 24 * 3600):
        leaf = os.path.join(self.sessions, f"{repo}-{number}", f"{role}-{gen}")
        os.makedirs(leaf, exist_ok=True)
        with open(os.path.join(leaf, "prompt.md"), "w", encoding="utf-8") as handle:
            handle.write("do the thing")
        old = T0 - age_s
        for root, dirs, files in os.walk(leaf):
            for name in dirs + files:
                os.utime(os.path.join(root, name), (old, old))
            os.utime(root, (old, old))
        return leaf

    def test_role_leaves_are_separate_safe_candidates(self):
        worker = self.make_leaf("demo", 7, "worker", 0)
        reviewer = self.make_leaf("demo", 7, "reviewer", 0)
        planned = self.plan(entries=[finished_entry("demo", 7)], walk=lambda p: 6)
        rows = {c.path: c for c in planned.candidates if c.kind == "session"}
        self.assertEqual(set(rows), {worker, reviewer})
        self.assertTrue(all(c.safe for c in rows.values()))
        self.assertTrue(all(c.bytes == 6 for c in rows.values()))

    def test_a_live_ticket_keeps_every_leaf(self):
        self.make_leaf("demo", 7, "worker", 0)
        self.make_leaf("demo", 7, "reviewer", 0)
        planned = self.plan(entries=[finished_entry("demo", 7)], list_fn=lambda: {("demo", 7)})
        rows = [c for c in planned.candidates if c.kind == "session"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not c.safe for c in rows))
        self.assertTrue(all("live unit" in c.proof for c in rows))

    def test_a_ticket_dir_with_only_reviews_is_never_safe(self):
        ticket = os.path.join(self.sessions, "demo-7")
        os.makedirs(os.path.join(ticket, "reviews"), exist_ok=True)
        with open(os.path.join(ticket, "reviews", "review-abc.json"), "w") as handle:
            handle.write("{}")
        planned = self.plan(entries=[finished_entry("demo", 7)], walk=lambda p: 1)
        rows = [c for c in planned.candidates if ticket in c.path]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "unproven")
        self.assertFalse(rows[0].safe)

    def test_mixed_ticket_reclaims_each_leaf_once_with_exact_bytes(self):
        ticket = self.make_session("demo", 7)
        worker = self.make_leaf("demo", 7, "worker", 0)
        reviewer = self.make_leaf("demo", 7, "reviewer", 0)
        old = T0 - 20 * 24 * 3600
        os.utime(ticket, (old, old))
        sizes = {worker: 100, reviewer: 200, ticket: 999}
        planned = self.plan(entries=[finished_entry("demo", 7)], walk=lambda p: sizes.get(p, 0))
        rows = {c.path: c for c in planned.candidates if c.kind == "session"}
        self.assertEqual(set(rows), {worker, reviewer})
        self.assertEqual(rows[worker].bytes, 100)
        self.assertEqual(rows[reviewer].bytes, 200)
        self.assertEqual(sum(c.bytes for c in rows.values()), 300)
        self.assertTrue(all(c.safe for c in rows.values()))


class WorktreeTest(DiskCase):
    def wt_plan(self, **overrides):
        wt = os.path.join(self.tmp.name, "wt")
        os.makedirs(wt, exist_ok=True)
        args = dict(roots=self.roots + (wt,))
        args.update(overrides)
        return wt, self.plan(**args)

    def test_gc_rows_carry_over_unchanged(self):
        wt = os.path.join(self.tmp.name, "wt")
        os.makedirs(wt, exist_ok=True)
        _wt, planned = self.wt_plan(
            gc_rows=[(os.path.join(wt, "one"), "landed on main", True)],
            walk=lambda p: 9,
        )
        rows = [c for c in planned.candidates if c.kind == "worktree"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].proof, "landed on main")
        self.assertTrue(rows[0].safe)

    def test_unplanned_gc_rows_are_not_candidates(self):
        planned = self.plan(gc_rows=[("/wt/one", "main checkout", False)])
        self.assertEqual([c for c in planned.candidates if c.kind == "worktree"], [])

    def test_gc_row_outside_roots_is_dropped(self):
        planned = self.plan(gc_rows=[("/elsewhere", "landed", True)])
        self.assertEqual([c for c in planned.candidates if c.kind == "worktree"], [])
        self.assertTrue(any("outside a declared root" in n for n in planned.notes))


class UnprovenTest(DiskCase):
    def test_big_directory_with_no_rule_is_listed_never_deleted(self):
        big = os.path.join(self.state, "blobs")
        os.makedirs(big)
        planned = self.plan(walk=lambda p: 2 * GB if p == big else 0)
        rows = [c for c in planned.candidates if c.path == big]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "unproven")
        self.assertFalse(rows[0].safe)

    def test_small_directories_stay_out_of_the_plan(self):
        small = os.path.join(self.state, "blobs")
        os.makedirs(small)
        planned = self.plan(walk=lambda p: 3)
        self.assertEqual(planned.candidates, ())

    def test_candidates_sort_largest_first(self):
        for name in ("a", "b"):
            os.makedirs(os.path.join(self.state, name))
        planned = self.plan(walk=lambda p: 5 * GB if p.endswith("a") else 2 * GB)
        self.assertGreater(planned.candidates[0].bytes, planned.candidates[1].bytes)


class ApplyTest(DiskCase):
    def write_pr_body(self, name, age_s):
        path = os.path.join(self.cache, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("draft")
        os.utime(path, (T0 - age_s, T0 - age_s))
        return path

    def apply(self, candidates, **overrides):
        removed = []
        args = dict(
            remove=removed.append,
            roots=self.roots,
            session_retention_days=14,
            now=T0,
            entries=[],
            list_fn=lambda: set(),
        )
        args.update(overrides)
        result = disk.apply(candidates, **args)
        return result, removed

    def test_apply_removes_only_safe_pr_body_and_session(self):
        old = 8 * 24 * 3600
        pr = self.write_pr_body("pr-1-abcdef.md", old)
        session = os.path.join(self.sessions, "demo-3")
        os.makedirs(session)
        os.utime(session, (T0 - 20 * 24 * 3600, T0 - 20 * 24 * 3600))
        entries = [finished_entry("demo", 3)]
        big = os.path.join(self.state, "blobs")
        os.makedirs(big)
        candidates = self.plan(
            entries=entries, walk=lambda p: 10 if p != big else 2 * GB
        ).candidates
        removed = []

        def take(path):
            removed.append(path)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

        result = disk.apply(
            candidates,
            remove=take,
            roots=self.roots,
            session_retention_days=14,
            now=T0,
            entries=entries,
            list_fn=lambda: set(),
        )
        self.assertEqual(sorted(result.removed), sorted([pr, session]))
        self.assertFalse(os.path.exists(pr))
        self.assertFalse(os.path.exists(session))
        kept_paths = [p for p, _ in result.kept]
        self.assertIn(big, kept_paths)
        self.assertTrue(os.path.isdir(big))

    def test_apply_keeps_a_file_touched_since_the_plan(self):
        old = 8 * 24 * 3600
        pr = self.write_pr_body("pr-1-abcdef.md", old)
        planned = self.plan(walk=lambda p: 5).candidates
        os.utime(pr, (T0, T0))
        result, removed = self.apply(planned)
        self.assertEqual(result.removed, [])
        self.assertEqual([p for p, _ in result.kept], [pr])
        self.assertTrue(os.path.isfile(pr))

    def test_apply_keeps_a_session_with_a_new_live_unit(self):
        old = 20 * 24 * 3600
        session = os.path.join(self.sessions, "demo-3")
        os.makedirs(session)
        os.utime(session, (T0 - old, T0 - old))
        entries = [finished_entry("demo", 3)]
        planned = self.plan(entries=entries, walk=lambda p: 5).candidates
        result, _ = self.apply(planned, entries=entries, list_fn=lambda: {("demo", 3)})
        self.assertEqual(result.removed, [])
        self.assertTrue(os.path.isdir(session))

    def test_apply_keeps_a_session_whose_entry_reopened(self):
        old = 20 * 24 * 3600
        session = os.path.join(self.sessions, "demo-3")
        os.makedirs(session)
        os.utime(session, (T0 - old, T0 - old))
        planned = self.plan(entries=[finished_entry("demo", 3)], walk=lambda p: 5).candidates
        result, _ = self.apply(planned, entries=[live_entry("demo", 3)])
        self.assertEqual(result.removed, [])
        self.assertTrue(os.path.isdir(session))

    def test_removal_failures_are_kept_with_their_reason(self):
        old = 8 * 24 * 3600
        pr = self.write_pr_body("pr-1-abcdef.md", old)
        planned = self.plan(walk=lambda p: 5).candidates

        def fail(path):
            raise OSError("read-only")

        result = disk.apply(
            planned,
            remove=fail,
            roots=self.roots,
            session_retention_days=14,
            now=T0,
            entries=[],
            list_fn=lambda: set(),
        )
        self.assertEqual(result.removed, [])
        self.assertEqual(result.kept, [(pr, "read-only")])

    # -- seams have no default ------------------------------------------------

    def test_removal_seam_is_required(self):
        for func in (disk.read, disk.plan, disk.apply):
            with self.subTest(func=func.__name__):
                params = inspect.signature(func).parameters
                for name in ("usage", "walk", "remove", "list_fn"):
                    if name in params:
                        self.assertEqual(
                            params[name].default,
                            inspect.Parameter.empty,
                            f"{func.__name__}.{name} must stay required",
                        )
        self.assertEqual(
            inspect.signature(disk.refusal_text).parameters["usage"].default,
            inspect.Parameter.empty,
        )

    def test_no_test_reaches_a_real_removal(self):
        self.assertIs(diskcli.production_remove, _forbidden_remove)

    def test_real_production_remove_contained_to_temp_dir(self):
        remove = _REAL_PRODUCTION_REMOVE(self.roots)
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        with self.assertRaises(gc.GcError):
            remove(outside)
        with self.assertRaises(gc.GcError):
            remove(self.tmp.name)
        target = os.path.join(self.cache, "gone.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
        remove(target)
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.isdir(outside))


class RefusalTest(DiskCase):
    def resolved(self, pct=10, gb=5):
        return {"disk.min_free_pct": pct, "disk.min_free_gb": gb}

    def test_ok_and_low_and_unknown_never_refuse(self):
        self.assertIsNone(disk.refusal_text(self.resolved(), usage=roomy))
        self.assertIsNone(disk.refusal_text(self.resolved(), usage=usage_free(9)))
        self.assertIsNone(
            disk.refusal_text(self.resolved(), usage=lambda path: (_ for _ in ()).throw(OSError()))
        )

    def test_critical_refuses_naming_the_filesystem_and_free_space(self):
        refusal = disk.refusal_text(self.resolved(), usage=usage_free(1))
        self.assertIsNotNone(refusal)
        self.assertIn("disk critically low, try later", refusal)
        self.assertIn("free", refusal)


class LaunchGateTest(DiskCase):
    def target(self):
        return SimpleNamespace(
            repo="demo",
            prefix="corral-agent-",
            root="",
            cap=1,
            resolved=settings.resolve(env={}),
        )

    def dirs(self):
        return mock.patch.dict(
            os.environ,
            {
                "CORRAL_STATE": self.state,
                "CORRAL_CACHE": self.cache,
                "CORRAL_SESSIONS": self.sessions,
            },
        )

    def test_admit_refuses_at_critical(self):
        with self.dirs():
            with self.assertRaises(launch.CapFull) as ctx:
                launch.admit(self.target(), 5, list_fn=lambda prefix: [], usage=usage_free(1))
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertIn("disk critically low", str(ctx.exception))

    def test_admit_passes_at_low(self):
        with self.dirs():
            launch.admit(self.target(), 5, list_fn=lambda prefix: [], usage=usage_free(9))

    def test_launch_and_resume_require_the_usage_seam(self):
        for func in (launch.admit, launch.launch, launch.resume):
            self.assertEqual(
                inspect.signature(func).parameters["usage"].default,
                inspect.Parameter.empty,
            )


class DoctorTest(unittest.TestCase):
    def test_disk_row_shapes(self):
        self.assertNotIn("disk", [c.name for c in doctor.run_checks(_env(None))])
        self.assertEqual(doctor.run_checks(_env("ok"))[-1].status, "ok")
        self.assertEqual(doctor.run_checks(_env("low"))[-1].status, "warn")
        self.assertEqual(doctor.run_checks(_env("critical"))[-1].status, "fail")
        self.assertEqual(doctor.run_checks(_env("unknown"))[-1].status, "warn")
        self.assertEqual(doctor.run_checks(_env("low"))[-1].name, "disk")

    def test_critical_disk_is_exit_1_not_3(self):
        self.assertEqual(doctor.exit_code(doctor.run_checks(_env("critical"))), 1)
        self.assertEqual(doctor.exit_code(doctor.run_checks(_env("low"))), 0)


def _env(pressure):
    return doctor_env(disk=pressure)


class CliTest(DiskCase):
    def run_disk(self, args, live=None, gc_rows=None):
        with mock.patch.dict(
            os.environ,
            {
                "CORRAL_STATE": self.state,
                "CORRAL_CACHE": self.cache,
                "CORRAL_SESSIONS": self.sessions,
                "CORRAL_CONFIG": os.path.join(self.tmp.name, "config"),
            },
        ):
            with mock.patch.object(diskcli.settings, "find_root", return_value=None):
                with mock.patch.object(
                    diskcli, "production_live", return_value=set() if live is None else live
                ):
                    with mock.patch.object(
                        diskcli,
                        "production_gc_rows",
                        return_value=([] if gc_rows is None else gc_rows, []),
                    ):
                        with mock.patch.object(diskcli, "production_remove", _forbidden_remove):
                            out, err = io.StringIO(), io.StringIO()
                            with redirect_stdout(out), redirect_stderr(err):
                                code = diskcli.cmd_disk(args)
        return code, out.getvalue(), err.getvalue()

    def test_disk_prints_pressure_and_plan(self):
        code, out, _ = self.run_disk(argparse.Namespace(json=False, apply=False))
        self.assertEqual(code, 0)
        self.assertIn("pressure:", out)

    def test_disk_json_carries_a_pressure_field(self):
        code, out, _ = self.run_disk(argparse.Namespace(json=True, apply=False))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertIn(doc["pressure"], ("ok", "low", "critical", "unknown"))
        self.assertIn("candidates", doc)

    def test_disk_exit_is_zero_whatever_the_pressure(self):
        with mock.patch.object(disk, "read", return_value=[disk.Reading("/x", 100 * GB, GB)]):
            code, out, _ = self.run_disk(argparse.Namespace(json=False, apply=False))
        self.assertEqual(code, 0)
        self.assertIn("pressure: critical", out)

    def test_settings_register_disk_keys(self):
        from corral import settings

        self.assertEqual(settings.REGISTRY["disk.min_free_pct"].default, 10)
        self.assertEqual(settings.REGISTRY["disk.min_free_gb"].default, 5)
        self.assertEqual(settings.REGISTRY["disk.session_retention_days"].default, 14)
        self.assertIn("corral.disk", settings.PROVIDERS)


if __name__ == "__main__":
    unittest.main()
