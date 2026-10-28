"""`corral disk`: pressure, a reclaim plan, and an opt-in apply.

Plan-only by default. `--apply` removes only the safe `pr-body` and
`session` candidates after re-verifying each proof; worktree reclaim stays
with `corral gc --apply`.
"""

import argparse
import json
import os
import shutil
import sys

from corral import disk, gc, gccli, paths, projects, registry, settings, units, worktrees

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4


def production_remove(roots):
    """The real destruction seam. Only the CLI builds it.

    `remove(path)` deletes one file or tree inside `roots` (symlinks
    resolved, escapes refused) and raises on anything else.
    """
    real_roots = tuple(os.path.realpath(r) for r in roots)

    def remove(path):
        real = os.path.realpath(path)
        if not any(real == r or real.startswith(r + os.sep) for r in real_roots):
            raise gc.GcError(f"{path} escapes the declared roots")
        for root in real_roots:
            if real == root:
                raise gc.GcError(f"refusing to remove a declared root: {path}")
        if os.path.islink(path):
            raise gc.GcError(f"refusing to remove a symlink: {path}")
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    return remove


def production_live():
    """The live `(repo, number)` tickets, or None when unreadable (keep)."""
    prefixes = {str(settings.REGISTRY["agents.unit_prefix"].default)}
    try:
        prefixes.update(p.unit_prefix for p in projects.load_all())
    except Exception:  # noqa: BLE001 - a broken registration keeps every session
        pass
    try:
        found = set()
        for prefix in prefixes:
            found.update(units.live_tickets(prefix, units.list_units(prefix)))
    except Exception:  # noqa: BLE001 - an unreachable manager keeps every session
        return None
    return found


def production_gc_rows(root, worktrees_setting):
    """`[(path, proof, planned)]` from `gc.plan` for every repo of the ops root."""
    rows = []
    notes = []
    try:
        names = settings.repo_names(root)
    except settings.SettingsError as err:
        return rows, [str(err)]
    for name in names:
        try:
            resolved = settings.load(root=root, repo=name)
            clone = resolved["clone.path"]
        except settings.SettingsError as err:
            notes.append(f"repo {name}: {err}")
            continue
        clone = clone if os.path.isabs(clone) else os.path.normpath(os.path.join(root, clone))
        if not os.path.isdir(os.path.join(clone, ".git")):
            notes.append(f"repo {name}: {clone} is not a git clone")
            continue
        index, note = gccli.live_pr_index(clone)
        if note:
            notes.append(f"repo {name}: {note}")
        try:
            checked = gc.plan(clone, pr_index=index, root=worktrees_setting)
        except (gc.GcError, worktrees.WorktreeError) as err:
            notes.append(f"repo {name}: {err}")
            continue
        for row in checked.registered:
            if row.planned:
                rows.append((row.path, row.reason, True))
        for row in checked.dangling:
            if row.planned:
                rows.append((row.path, row.reason, True))
    return rows, notes


def production_roots(root, worktrees_setting):
    """Every declared root: state, cache, sessions, plus each clone's worktree root."""
    roots = [paths.state_dir(), paths.cache_dir(), paths.sessions_dir()]
    try:
        names = settings.repo_names(root)
    except settings.SettingsError:
        return roots
    for name in names:
        try:
            resolved = settings.load(root=root, repo=name)
            clone = resolved["clone.path"]
        except settings.SettingsError:
            continue
        clone = clone if os.path.isabs(clone) else os.path.normpath(os.path.join(root, clone))
        if not os.path.isdir(os.path.join(clone, ".git")):
            continue
        try:
            roots.append(worktrees.root(os.path.realpath(clone), worktrees_setting))
        except worktrees.WorktreeError:
            continue
    return roots


def _free_text(reading):
    if reading.free is None or reading.total is None:
        return "unknown"
    pct = reading.free / reading.total * 100
    return f"{disk.format_gb(reading.free)} free ({pct:.0f}%)"


def format_plan(plan):
    """The human-readable pressure plus candidates, largest first."""
    lines = [f"pressure: {plan.pressure}"]
    for reading in plan.readings:
        lines.append(f"  {reading.path}: {_free_text(reading)}")
    for candidate in plan.candidates:
        flag = "safe" if candidate.safe else "keep"
        lines.append(
            f"  [{flag}] {candidate.kind} {candidate.path} "
            f"({candidate.bytes} bytes): {candidate.proof}"
        )
    for note in plan.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def plan_json(plan, result=None):
    """The machine-readable plan, with the apply result nested when given."""
    doc = {
        "pressure": plan.pressure,
        "readings": [{"path": r.path, "total": r.total, "free": r.free} for r in plan.readings],
        "candidates": [
            {
                "kind": c.kind,
                "path": c.path,
                "bytes": c.bytes,
                "proof": c.proof,
                "safe": c.safe,
            }
            for c in plan.candidates
        ],
        "notes": list(plan.notes),
    }
    if result is not None:
        doc["apply"] = {
            "removed": list(result.removed),
            "kept": [{"path": p, "reason": r} for p, r in result.kept],
        }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _err(text):
    print(f"corral disk: {text}", file=sys.stderr)


def cmd_disk(args):
    try:
        cfg = settings.load(root=settings.find_root(os.getcwd()))
    except settings.SettingsError as err:
        _err(str(err))
        return USAGE
    try:
        state_dir = paths.state_dir()
        cache_dir = paths.cache_dir()
        sessions_dir = paths.sessions_dir()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    root = settings.find_root(os.getcwd())
    worktrees_setting = cfg["git.worktrees"]
    roots = production_roots(root, worktrees_setting)
    gc_rows, gc_notes = production_gc_rows(root, worktrees_setting)
    try:
        entries = registry.load_all()
    except (paths.PathError, OSError) as err:
        _err(str(err))
        return UNREACHABLE
    import time

    checked = disk.plan(
        state_dir=state_dir,
        cache_dir=cache_dir,
        sessions_dir=sessions_dir,
        roots=roots,
        min_free_pct=cfg["disk.min_free_pct"],
        min_free_gb=cfg["disk.min_free_gb"],
        session_retention_days=cfg["disk.session_retention_days"],
        usage=disk.production_usage,
        walk=disk.measure,
        now=time.time(),
        entries=entries,
        list_fn=production_live,
        gc_rows=gc_rows,
    )
    if gc_notes:
        checked = disk.Plan(
            checked.pressure,
            checked.readings,
            checked.candidates,
            tuple(list(checked.notes) + gc_notes),
        )
    result = None
    if args.apply:
        import time as _time

        result = disk.apply(
            checked.candidates,
            remove=production_remove(roots),
            roots=roots,
            session_retention_days=cfg["disk.session_retention_days"],
            now=_time.time(),
            entries=entries,
            list_fn=production_live,
        )
    if args.json:
        sys.stdout.write(plan_json(checked, result))
    else:
        text = format_plan(checked)
        sys.stdout.write((text + "\n") if text else "")
    if result is not None:
        report = sys.stderr if args.json else sys.stdout
        for path in result.removed:
            print(f"applied: removed {path}", file=report)
        for path, reason in result.kept:
            print(f"kept: {path} ({reason})", file=report)
    return OK


def add_parser(sub):
    """Register `disk` on the top-level subparsers."""
    p = sub.add_parser(
        "disk",
        help="disk pressure and the reclaim plan; --apply removes safe pr-body and session rows",
    )
    p.add_argument("--json", action="store_true", help="machine-readable plan")
    p.add_argument(
        "--apply",
        action="store_true",
        help="remove only the safe pr-body and session candidates (never worktrees)",
    )
    p.set_defaults(func=cmd_disk)
