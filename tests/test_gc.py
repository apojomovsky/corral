"""Tests for corral.gc: the plan-only hygiene audit.

Every fixture is a real temp git repo (local only, no network, no real
clones, no gh). The forge and removal seams are fakes; a guard test pins
that both stay required arguments with no default. The real removal seam
is parked behind `_REAL_PRODUCTION_REMOVE` and replaced at module level
with `_forbidden_remove`, so any test reaching it by accident fails;
`_run_gc` patches in a contained fake, and exactly one test named
`test_real_production_remove_contained_to_temp_dir` exercises the real
seam, only against temp dirs.
"""

import argparse
import dataclasses
import inspect
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from unittest import mock

from corral import diffscope, gc, gccli, worktrees
from corral.gitenv import clean_env

BASE = "master"

_REAL_PRODUCTION_REMOVE = gccli.production_remove


def _forbidden_remove(repo: str, root_real: str) -> Callable[[str], None]:
    def remove(path: str) -> None:
        raise AssertionError("tests must not reach the real removal seam")

    return remove


gccli.production_remove = _forbidden_remove  # ty: ignore[invalid-assignment]


def _scrub_git_env(test):
    env = mock.patch.dict(os.environ)
    env.start()
    test.addCleanup(env.stop)
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        del os.environ[name]


class Repo:
    """A real temp git repo plus a file-path bare remote."""

    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name
        self.git("init", "-q", "-b", BASE)
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        self.write("base.txt", "base\n")
        self.git("add", "-A")
        self.commit("base commit")
        self.bare = os.path.join(self.tmp.name + "-bare")
        os.makedirs(self.bare)
        self.git("init", "-q", "--bare", cwd=self.bare)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=self.path if cwd is None else cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
        )

    def commit(self, msg):
        proc = self.git("commit", "-q", "-m", msg)
        assert proc.returncode == 0, proc.stderr

    def write(self, rel, text):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def checkout(self, branch, new=False):
        args = ["checkout", "-q"]
        if new:
            args.append("-b")
        args.append(branch)
        proc = self.git(*args)
        assert proc.returncode == 0, proc.stderr

    def add_origin(self):
        proc = self.git("remote", "add", "origin", self.bare)
        assert proc.returncode == 0, proc.stderr

    def push(self, branch, delete=False):
        if delete:
            proc = self.git("push", "-q", "origin", "--delete", branch)
        else:
            proc = self.git("push", "-q", "-u", "origin", branch)
        assert proc.returncode == 0, proc.stderr

    def worktrees_root(self):
        return os.path.join(self.path, ".worktrees")

    def categories(self, **kwargs):
        kwargs.setdefault("base", BASE)
        kwargs.setdefault("pr_index", {})
        return {a.branch.name: a.category for a in gc.audit_branches(self.path, **kwargs)}


class GcTest(unittest.TestCase):
    def setUp(self):
        _scrub_git_env(self)
        self.repo = Repo(self)

    # -- branch categories ------------------------------------------------

    def test_merged_branch_is_landed(self):
        self.repo.checkout("done", new=True)
        self.repo.write("done.txt", "done\n")
        self.repo.git("add", "-A")
        self.repo.commit("ship done")
        self.repo.checkout(BASE)
        self.assertEqual(self.repo.git("merge", "-q", "done").returncode, 0)
        self.assertEqual(self.repo.categories()["done"], gc.LANDED)

    def test_squash_merged_branch_is_superseded(self):
        self.repo.checkout("sq", new=True)
        self.repo.write("sq.txt", "sq\n")
        self.repo.git("add", "-A")
        self.repo.commit("add widget")
        self.repo.checkout(BASE)
        self.assertEqual(self.repo.git("merge", "--squash", "sq").returncode, 0)
        # Same subject, different body: same-second commits must not collapse
        # to one hash, or the branch would read as landed instead of superseded.
        self.assertEqual(
            self.repo.git("commit", "-q", "-m", "add widget", "-m", "squashed").returncode, 0
        )
        cats = self.repo.categories()
        self.assertEqual(cats["sq"], gc.SUPERSEDED)

    def test_merged_pr_beats_no_upstream(self):
        self.repo.checkout("shipped", new=True)
        self.repo.write("s.txt", "s\n")
        self.repo.git("add", "-A")
        self.repo.commit("unique shipped work")
        self.repo.checkout(BASE)
        cats = self.repo.categories(pr_index={"shipped": "merged"})
        self.assertEqual(cats["shipped"], gc.MERGED_PR)

    def test_open_pr_is_not_merged(self):
        self.repo.checkout("openwork", new=True)
        self.repo.write("o.txt", "o\n")
        self.repo.git("add", "-A")
        self.repo.commit("open work")
        self.repo.checkout(BASE)
        cats = self.repo.categories(pr_index={"openwork": "open"})
        self.assertEqual(cats["openwork"], gc.NO_UPSTREAM)

    def test_pushed_branch_is_live_remote(self):
        self.repo.add_origin()
        self.repo.checkout("live", new=True)
        self.repo.write("l.txt", "l\n")
        self.repo.git("add", "-A")
        self.repo.commit("live work")
        self.repo.push("live")
        self.repo.checkout(BASE)
        self.repo.git("fetch", "-q", "origin")
        self.assertEqual(self.repo.categories()["live"], gc.LIVE_REMOTE)

    def test_never_pushed_branch_is_no_upstream(self):
        self.repo.checkout("local", new=True)
        self.repo.write("x.txt", "x\n")
        self.repo.git("add", "-A")
        self.repo.commit("local only")
        self.repo.checkout(BASE)
        self.assertEqual(self.repo.categories()["local"], gc.NO_UPSTREAM)

    def test_deleted_remote_without_evidence_is_unknown(self):
        self.repo.add_origin()
        self.repo.checkout("vanished", new=True)
        self.repo.write("v.txt", "v\n")
        self.repo.git("add", "-A")
        self.repo.commit("vanished work")
        self.repo.push("vanished")
        self.repo.push("vanished", delete=True)
        self.repo.checkout(BASE)
        self.assertEqual(self.repo.categories()["vanished"], gc.UNKNOWN)

    def test_current_branch_is_never_audited(self):
        self.repo.checkout("here", new=True)
        self.repo.write("h.txt", "h\n")
        self.repo.git("add", "-A")
        self.repo.commit("here")
        names = [a.branch.name for a in gc.audit_branches(self.repo.path, base=BASE, pr_index={})]
        self.assertNotIn("here", names)

    def test_delete_command_uses_dash_lowercase(self):
        self.repo.checkout("done", new=True)
        self.repo.write("done.txt", "d\n")
        self.repo.git("add", "-A")
        self.repo.commit("ship done")
        self.repo.checkout(BASE)
        self.repo.git("merge", "-q", "done")
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        text = gc.format_plan(plan)
        self.assertIn("git branch -d done", text)
        self.assertNotIn("git branch -D", text)

    def test_no_delete_command_for_do_not_touch(self):
        self.repo.checkout("local", new=True)
        self.repo.write("x.txt", "x\n")
        self.repo.git("add", "-A")
        self.repo.commit("local only")
        self.repo.checkout(BASE)
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        text = gc.format_plan(plan)
        self.assertIn("local", text)
        self.assertNotIn("git branch -d local", text)

    # -- base resolver -----------------------------------------------------

    def test_default_base_is_diffscopes(self):
        self.assertEqual(diffscope.default_base(), "origin/master")
        plan = gc.plan(self.repo.path, pr_index={}, root=".worktrees/")
        self.assertEqual(plan.base, diffscope.default_base())

    def test_missing_base_fails_closed_with_a_note(self):
        plan = gc.plan(self.repo.path, pr_index={}, root=".worktrees/")
        self.assertFalse(plan.base_ok)
        self.assertIn("origin/master", gc.format_plan(plan))

    # -- worktree root ------------------------------------------------------

    def test_root_goes_through_worktrees(self):
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        main = self.repo.git("rev-parse", "--show-toplevel").stdout.strip()
        self.assertEqual(plan.root, worktrees.root(main, ".worktrees/"))

    # -- registered worktrees ----------------------------------------------

    def _add_worktree(self, name, branch):
        dest = os.path.join(".worktrees", name)
        proc = self.repo.git("worktree", "add", dest, "-b", branch)
        assert proc.returncode == 0, proc.stderr
        return os.path.join(self.repo.path, dest)

    def _land(self, branch):
        self.repo.checkout(branch, new=True)
        self.repo.write(f"{branch}.txt", f"{branch}\n")
        self.repo.git("add", "-A")
        self.repo.commit(f"ship {branch}")
        self.repo.checkout(BASE)
        self.assertEqual(self.repo.git("merge", "-q", branch).returncode, 0)

    def test_clean_landed_worktree_is_planned(self):
        self._land("wt1")
        self._add_worktree("1-wt", "on-wt1")
        plan = gc.plan(self.repo.path, base=BASE, pr_index={"on-wt1": "merged"}, root=".worktrees/")
        rows = [r for r in plan.registered if r.planned]
        self.assertEqual([(os.path.basename(r.path), r.branch) for r in rows], [("1-wt", "on-wt1")])

    def test_dirty_worktree_is_kept(self):
        self._land("wt2")
        path = self._add_worktree("2-wt", "on-wt2")
        with open(os.path.join(path, "dirty.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted\n")
        plan = gc.plan(self.repo.path, base=BASE, pr_index={"on-wt2": "merged"}, root=".worktrees/")
        rows = {os.path.basename(r.path): r for r in plan.registered}
        self.assertFalse(rows["2-wt"].planned)
        self.assertIn("dirty", rows["2-wt"].reason)

    def test_worktree_with_unpushed_commits_is_kept(self):
        self.repo.checkout("ahead", new=True)
        self.repo.write("a.txt", "a\n")
        self.repo.git("add", "-A")
        self.repo.commit("ahead of base")
        self.repo.checkout(BASE)
        self._add_worktree("3-wt", "on-ahead")
        self.repo.git(
            "checkout", "-q", "on-ahead", cwd=os.path.join(self.repo.path, ".worktrees", "3-wt")
        )
        proc = self.repo.git(
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "extra local",
            cwd=os.path.join(self.repo.path, ".worktrees", "3-wt"),
        )
        assert proc.returncode == 0, proc.stderr
        plan = gc.plan(
            self.repo.path, base=BASE, pr_index={"on-ahead": "merged"}, root=".worktrees/"
        )
        rows = {os.path.basename(r.path): r for r in plan.registered}
        self.assertFalse(rows["3-wt"].planned)
        self.assertIn("unpushed", rows["3-wt"].reason)

    def test_symlink_escaping_root_is_refused(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        marker = os.path.join(outside, "keep.txt")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("outside\n")
        os.makedirs(self.repo.worktrees_root(), exist_ok=True)
        os.symlink(outside, os.path.join(self.repo.worktrees_root(), "evil"))
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        self.assertEqual(len(plan.dangling), 0)
        self.assertEqual(len(plan.refused), 1)
        removed = []
        _, result = gc.apply(
            self.repo.path, base=BASE, pr_index={}, root=".worktrees/", remove=removed.append
        )
        self.assertEqual(removed, [])
        self.assertTrue(os.path.isfile(marker))

    # -- dangling dirs and prune --------------------------------------------

    def _dangling(self, name="stale"):
        os.makedirs(self.repo.worktrees_root(), exist_ok=True)
        path = os.path.join(self.repo.worktrees_root(), name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: /nonexistent/admin/worktrees/stale\n")
        return path

    def test_dangling_dir_planned_but_untouched_without_apply(self):
        path = self._dangling()
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        self.assertIn(path, [d.path for d in plan.dangling if d.planned])
        self.assertTrue(os.path.isfile(os.path.join(path, ".git")))

    def test_dangling_dir_removed_only_with_apply(self):
        path = self._dangling()
        removed = []
        gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        self.assertTrue(os.path.isdir(path))
        _, result = gc.apply(
            self.repo.path, base=BASE, pr_index={}, root=".worktrees/", remove=removed.append
        )
        self.assertEqual(removed, [path])
        for entry in removed:
            shutil.rmtree(entry, ignore_errors=True)
        self.assertFalse(os.path.exists(path))
        self.assertIn(path, result.removed)

    def test_nonempty_dangling_dir_without_git_proof_is_kept(self):
        path = self._dangling()
        with open(os.path.join(path, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("someone left this\n")
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        rows = {d.path: d for d in plan.dangling}
        self.assertFalse(rows[path].planned)

    def test_stale_worktree_entry_is_pruned(self):
        self.repo.checkout("tmpbr", new=True)
        self.repo.write("t.txt", "t\n")
        self.repo.git("add", "-A")
        self.repo.commit("tmp")
        self.repo.checkout(BASE)
        dest = os.path.join(self.repo.path, ".worktrees", "prune-me")
        proc = self.repo.git("worktree", "add", dest, "tmpbr")
        assert proc.returncode == 0, proc.stderr
        shutil.rmtree(dest)
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        self.assertTrue(plan.prune)
        _, result = gc.apply(
            self.repo.path, base=BASE, pr_index={}, root=".worktrees/", remove=lambda p: None
        )
        self.assertTrue(result.pruned)
        rerun = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        self.assertFalse(rerun.prune)

    def test_plan_changes_nothing(self):
        self._land("keepme")
        self._dangling()
        before_branches = self.repo.git("branch", "--format=%(refname:short)").stdout
        before_trees = self.repo.git("worktree", "list", "--porcelain").stdout
        before_dirs = sorted(os.listdir(self.repo.worktrees_root()))
        gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        self.assertEqual(
            self.repo.git("branch", "--format=%(refname:short)").stdout, before_branches
        )
        self.assertEqual(self.repo.git("worktree", "list", "--porcelain").stdout, before_trees)
        self.assertEqual(sorted(os.listdir(self.repo.worktrees_root())), before_dirs)

    # -- seams have no default ------------------------------------------------

    def test_forge_and_removal_seams_are_required(self):
        for func in (gc.audit_branches, gc.plan):
            params = inspect.signature(func).parameters
            self.assertIn("pr_index", params)
            self.assertIs(params["pr_index"].default, inspect.Parameter.empty)
        params = inspect.signature(gc.apply).parameters
        for name in ("pr_index", "remove"):
            self.assertIn(name, params)
            self.assertIs(params[name].default, inspect.Parameter.empty)

    def test_empty_forge_says_so_and_never_fires_merged(self):
        self.repo.checkout("shipped", new=True)
        self.repo.write("s.txt", "s\n")
        self.repo.git("add", "-A")
        self.repo.commit("shipped work")
        self.repo.checkout(BASE)
        plan = gc.plan(
            self.repo.path,
            base=BASE,
            pr_index=gccli.empty_pr_index(),
            root=".worktrees/",
            forge_note=gccli.EMPTY_FORGE_NOTE,
        )
        cats = {a.branch.name: a.category for a in plan.audits}
        self.assertNotEqual(cats["shipped"], gc.MERGED_PR)
        self.assertIn("merged-pr never fires", gc.format_plan(plan))
        body = json.loads(gccli.plan_json(plan))
        self.assertIn("merged-pr never fires", body["forge_note"])

    # -- exit codes via the verb ----------------------------------------------

    def _args(self, **overrides):
        base = {"repo": self.repo.path, "json": False, "apply": False}
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run_gc(self, args, removed=None):
        if removed is None:
            removed = []
        root = os.path.realpath(self.repo.worktrees_root())

        def fake_factory(repo, root_real):
            def remove(path):
                real = os.path.realpath(path)
                assert real != root and real.startswith(root + os.sep), (
                    f"fake removal escapes the root: {path}"
                )
                removed.append(path)
                shutil.rmtree(path, ignore_errors=True)

            return remove

        out = io.StringIO()
        with redirect_stdout(out):
            with mock.patch.dict(os.environ, {"BASE_REF": BASE}):
                with mock.patch.object(
                    gccli.settings, "load", return_value={"git.worktrees": ".worktrees/"}
                ):
                    with mock.patch.object(gccli.settings, "find_root", return_value=None):
                        with mock.patch.object(gccli, "production_remove", new=fake_factory):
                            code = gccli.cmd_gc(args)
        return code, out.getvalue()

    def test_nothing_to_do_on_a_fresh_repo(self):
        code, out = self._run_gc(self._args())
        self.assertEqual(code, 1)
        self.assertIn("nothing to do", out)

    def test_plan_prints_and_exits_zero(self):
        self._land("done")
        code, out = self._run_gc(self._args())
        self.assertEqual(code, 0)
        self.assertIn("git branch -d done", out)

    def test_json_plan_parses(self):
        self._land("done")
        code, out = self._run_gc(self._args(json=True))
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body["base"], BASE)
        cats = {b["name"]: b["category"] for b in body["branches"]}
        self.assertEqual(cats["done"], gc.LANDED)

    def test_not_a_repo_is_unreachable(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.object(
            gccli.settings, "load", return_value={"git.worktrees": ".worktrees/"}
        ):
            with mock.patch.object(gccli.settings, "find_root", return_value=None):
                code = gccli.cmd_gc(argparse.Namespace(repo=empty, json=False, apply=False))
        self.assertEqual(code, 3)

    def test_apply_removes_dangling_through_the_verb(self):
        path = self._dangling()
        removed = []
        code, _ = self._run_gc(self._args(apply=True), removed=removed)
        self.assertEqual(code, 0)
        self.assertEqual(removed, [path])
        self.assertFalse(os.path.exists(path))

    def test_registered_escape_between_plan_and_apply_is_kept(self):
        self._land("wt-esc")
        path = self._add_worktree("esc-wt", "on-esc")
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        real_row = gc._registered_row

        def swapping(repo, base, wt_path, branch, categories, run):
            row = real_row(repo, base, wt_path, branch, categories, run)
            if wt_path == path and row.planned:
                shutil.rmtree(wt_path)
                os.symlink(outside, wt_path)
                return gc.RegisteredRow(wt_path, branch, row.category, True, row.reason)
            return row

        removed = []
        with mock.patch.object(gc, "_registered_row", side_effect=swapping):
            _, result = gc.apply(
                self.repo.path,
                base=BASE,
                pr_index={"on-esc": "merged"},
                root=".worktrees/",
                remove=removed.append,
            )
        self.assertEqual(removed, [])
        kept = dict(result.kept)
        self.assertIn(path, kept)
        self.assertIn("escapes", kept[path])
        self.assertTrue(os.path.isdir(outside))

    def test_json_apply_stdout_is_one_document(self):
        path = self._dangling()
        removed = []
        code, out = self._run_gc(self._args(json=True, apply=True), removed=removed)
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body["applied"]["removed"], [path])
        self.assertEqual(removed, [path])
        self.assertFalse(os.path.exists(path))

    def test_repo_subdir_resolves_to_toplevel(self):
        path = self._dangling()
        sub = os.path.join(self.repo.path, "sub")
        os.makedirs(sub)
        removed = []
        code, _ = self._run_gc(self._args(repo=sub, apply=True), removed=removed)
        self.assertEqual(code, 0)
        self.assertEqual(removed, [path])
        self.assertFalse(os.path.exists(path))

    def test_no_test_reaches_a_real_removal(self):
        self.assertIs(gccli.production_remove, _forbidden_remove)

    def test_real_production_remove_contained_to_temp_dir(self):
        root_real = worktrees.root(self.repo.path, ".worktrees/")
        os.makedirs(root_real, exist_ok=True)
        remove = _REAL_PRODUCTION_REMOVE(self.repo.path, root_real)
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        with self.assertRaises(gc.GcError):
            remove(outside)
        with self.assertRaises(gc.GcError):
            remove(root_real)
        self.assertTrue(os.path.isdir(outside))
        target = os.path.join(root_real, "gone")
        os.makedirs(target)
        with open(os.path.join(target, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: /nonexistent/admin/worktrees/gone\n")
        remove(target)
        self.assertFalse(os.path.exists(target))

    def test_candidate_equal_to_root_is_kept(self):
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        row = gc.DanglingRow(plan.root, True, "pretend planned")
        with mock.patch.object(gc, "plan", return_value=dataclasses.replace(plan, dangling=[row])):
            removed = []
            _, result = gc.apply(
                self.repo.path,
                base=BASE,
                pr_index={},
                root=".worktrees/",
                remove=removed.append,
            )
        self.assertEqual(removed, [])
        kept = dict(result.kept)
        self.assertIn(plan.root, kept)
        self.assertIn("escapes", kept[plan.root])

    def _standalone(self, name="solo"):
        """A standalone checkout under the worktree root, unknown to git."""
        path = os.path.join(self.repo.worktrees_root(), name)
        os.makedirs(path, exist_ok=True)
        for args in (
            ("init", "-q", "-b", BASE),
            ("config", "user.name", "t"),
            ("config", "user.email", "t@t"),
        ):
            proc = self.repo.git(*args, cwd=path)
            assert proc.returncode == 0, proc.stderr
        with open(os.path.join(path, "solo.txt"), "w", encoding="utf-8") as handle:
            handle.write("solo\n")
        self.assertEqual(self.repo.git("add", "-A", cwd=path).returncode, 0)
        self.assertEqual(self.repo.git("commit", "-q", "-m", "solo base", cwd=path).returncode, 0)
        return path

    def test_standalone_checkout_with_unique_branch_is_kept(self):
        path = self._standalone()
        self.repo.git("checkout", "-q", "-b", "unique-work", cwd=path)
        with open(os.path.join(path, "unique.txt"), "w", encoding="utf-8") as handle:
            handle.write("unique\n")
        self.repo.git("add", "-A", cwd=path)
        self.repo.git("commit", "-q", "-m", "unique work", cwd=path)
        self.repo.git("checkout", "-q", BASE, cwd=path)
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        rows = {d.path: d for d in plan.dangling}
        self.assertIn(path, rows)
        self.assertFalse(rows[path].planned)
        self.assertIn("outside the base", rows[path].reason)
        removed = []
        gc.apply(self.repo.path, base=BASE, pr_index={}, root=".worktrees/", remove=removed.append)
        self.assertEqual(removed, [])
        proc = self.repo.git("rev-parse", "--verify", "-q", "unique-work", cwd=path)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_standalone_checkout_with_stash_is_kept(self):
        path = self._standalone()
        with open(os.path.join(path, "solo.txt"), "a", encoding="utf-8") as handle:
            handle.write("more\n")
        proc = self.repo.git("stash", "push", "-q", "-m", "wink", cwd=path)
        assert proc.returncode == 0, proc.stderr
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        rows = {d.path: d for d in plan.dangling}
        self.assertIn(path, rows)
        self.assertFalse(rows[path].planned)
        self.assertIn("stash", rows[path].reason)
        removed = []
        gc.apply(self.repo.path, base=BASE, pr_index={}, root=".worktrees/", remove=removed.append)
        proc = self.repo.git("stash", "list", cwd=path)
        self.assertIn("wink", proc.stdout)

    def test_standalone_clean_checkout_is_still_planned(self):
        path = self._standalone()
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        rows = {d.path: d for d in plan.dangling}
        self.assertTrue(rows[path].planned)
        removed = []
        _, result = gc.apply(
            self.repo.path, base=BASE, pr_index={}, root=".worktrees/", remove=removed.append
        )
        self.assertEqual(removed, [path])
        self.assertIn(path, result.removed)
        for entry in removed:
            shutil.rmtree(entry, ignore_errors=True)
        self.assertFalse(os.path.exists(path))

    def test_bad_settings_is_usage_error(self):
        with mock.patch.object(
            gccli.settings, "load", side_effect=gccli.settings.SettingsError("bad config")
        ):
            with mock.patch.object(gccli.settings, "find_root", return_value=None):
                code = gccli.cmd_gc(self._args())
        self.assertEqual(code, 4)

    def test_superseded_worktree_is_not_planned(self):
        self.repo.checkout("sq-wt", new=True)
        self.repo.write("sq-wt.txt", "sq\n")
        self.repo.git("add", "-A")
        self.repo.commit("add widget for worktree")
        self.repo.checkout(BASE)
        self.assertEqual(self.repo.git("merge", "--squash", "sq-wt").returncode, 0)
        self.assertEqual(
            self.repo.git(
                "commit", "-q", "-m", "add widget for worktree", "-m", "squashed"
            ).returncode,
            0,
        )
        os.makedirs(self.repo.worktrees_root(), exist_ok=True)
        proc = self.repo.git(
            "worktree", "add", os.path.join(self.repo.path, ".worktrees", "sq"), "sq-wt"
        )
        assert proc.returncode == 0, proc.stderr
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        rows = {os.path.basename(r.path): r for r in plan.registered}
        self.assertFalse(rows["sq"].planned)
        self.assertIn("superseded", rows["sq"].reason)
        self.assertFalse(any(r.planned for r in plan.registered))

    def test_hostile_branch_name_is_quoted(self):
        name = "evil$(touch-pwned)"
        self.repo.checkout(name, new=True)
        self.repo.write("evil.txt", "evil\n")
        self.repo.git("add", "-A")
        self.repo.commit("evil work")
        self.repo.checkout(BASE)
        self.repo.git("merge", "-q", name)
        plan = gc.plan(self.repo.path, base=BASE, pr_index={}, root=".worktrees/")
        quoted = shlex.quote(name)
        self.assertIn(f"git branch -d {quoted}", gc.format_plan(plan))
        body = json.loads(gccli.plan_json(plan))
        cmds = {b["name"]: b["delete_command"] for b in body["branches"]}
        self.assertEqual(cmds[name], f"git branch -d {quoted}")


if __name__ == "__main__":
    unittest.main()
