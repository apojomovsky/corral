"""`corral gc`: the plan-only hygiene audit for one clone.

Prints the plan (branches by verdict with `git branch -d` commands for
the safe ones, worktree targets, refused escapes, stale prune state)
and, only with `--apply`, performs the safe bookkeeping: `git worktree
prune` plus removing a planned worktree or unregistered directory.
Branch deletion is never performed. Exit codes: 0 plan printed (or
applied), 1 nothing to do, 3 git unreachable, 4 usage error.
"""

import argparse
import json
import os
import shutil
import sys

from corral import gc, settings, worktrees

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4

#: No PR adapter exists behind the forge interface today, so the verb
#: passes an index that says nothing (merged-pr never fires) with this
#: note in the plan.
EMPTY_FORGE_NOTE = "no forge data wired: merged-pr never fires"


def empty_pr_index():
    """The forge seam when no adapter provides one: nothing is merged."""
    return {}


def plan_json(plan):
    """The machine-readable plan."""
    return json.dumps(gc.plan_dict(plan), indent=2, sort_keys=True) + "\n"


def production_remove(repo, root_real):
    """The real destruction seam. Only the CLI builds it; tests never call this."""

    def remove(path):
        real = os.path.realpath(path)
        if os.path.commonpath([real, root_real]) != root_real:
            raise gc.GcError(f"refusing to remove {path}: escapes the worktree root")
        proc = gc.git(["worktree", "remove", path], cwd=repo)
        if proc.returncode == 0:
            return
        if "not a working tree" in (proc.stderr or ""):
            shutil.rmtree(path)
            return
        lines = (proc.stderr or "").strip().splitlines()
        raise gc.GcError(f"cannot remove worktree {path}: {lines[-1] if lines else 'git failed'}")

    return remove


def _err(text):
    print(f"corral gc: {text}", file=sys.stderr)


def cmd_gc(args: argparse.Namespace) -> int:
    repo = os.path.abspath(args.repo or os.getcwd())
    try:
        cfg = settings.load(root=settings.find_root(repo))
        root_setting = cfg["git.worktrees"]
    except settings.SettingsError as err:
        _err(str(err))
        return USAGE
    try:
        if args.apply:
            plan, result = gc.apply(
                repo,
                pr_index=empty_pr_index(),
                root=root_setting,
                remove=production_remove(repo, worktrees.root(repo, root_setting)),
                forge_note=EMPTY_FORGE_NOTE,
            )
        else:
            plan = gc.plan(
                repo,
                pr_index=empty_pr_index(),
                root=root_setting,
                forge_note=EMPTY_FORGE_NOTE,
            )
            result = None
    except worktrees.WorktreeError as err:
        _err(str(err))
        return USAGE
    except gc.GcError as err:
        _err(str(err))
        return UNREACHABLE
    if args.json:
        sys.stdout.write(plan_json(plan))
    else:
        text = gc.format_plan(plan)
        sys.stdout.write((text + "\n") if text else "")
    if not gc.actionable(plan):
        if not args.json:
            print("nothing to do")
        return NOTHING
    if result is not None:
        if result.pruned:
            print("applied: pruned stale worktree entries")
        elif result.prune_error is not None:
            print(f"kept: prune failed ({result.prune_error})")
        for path in result.removed:
            print(f"applied: removed {path}")
        for path, reason in result.kept:
            print(f"kept: {path} ({reason})")
    return OK


def add_parser(sub) -> None:
    """Register `gc` on the top-level subparsers."""
    p = sub.add_parser(
        "gc",
        help="plan-only hygiene audit: landed, superseded and merged branches, worktree cleanup",
    )
    p.add_argument("--repo", default=None, help="the clone to audit (default: cwd)")
    p.add_argument("--json", action="store_true", help="machine-readable plan")
    p.add_argument(
        "--apply",
        action="store_true",
        help="also prune stale entries and remove planned worktree paths (never branches)",
    )
    p.set_defaults(func=cmd_gc)
