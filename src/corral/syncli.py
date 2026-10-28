"""`corral sync`: feed the board store from the tracker's change feed.

Runs the incremental feed for the ops root's repositories (each
``[[repo]]`` entry; ``tracker.repo`` when set, else the clone's
origin), with ``--resync`` for a full rebuild and ``--audit N`` for
the rolling re-read of the N rows verified least recently. Exit codes:
0 synced, 1 nothing changed, 3 tracker unreachable, 4 usage or
configuration error.
"""

import argparse
import json
import os
import subprocess
import sys
import time

from corral import board, settings, sync
from corral.gitenv import clean_env
from corral.github import forge as ghforge

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4


def _err(text, err):
    print(f"corral sync: {text}", file=err)


def _git(args, cwd):
    """A local git seam for the origin lookup. None when git cannot run."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _production_tracker():
    """The live tracker over the production `gh` runner."""
    from corral.github import tracker as ghtracker
    from corral.github.gh import paced_run

    return ghtracker.GithubTracker(paced_run)


def _operators(configured, tracker_repo):
    """Effective operators: the configured logins, else the repo owner.

    An env-provided string reads as comma-separated; anything else that
    is not a login list reads as unconfigured. The repository owner is
    always the fallback, never an empty operator set.
    """
    if isinstance(configured, str):
        names = configured.split(",")
    elif isinstance(configured, list):
        names = configured
    else:
        names = []
    kept = [name.strip() for name in names if isinstance(name, str) and name.strip()]
    return kept or [tracker_repo.split("/")[0]]


def _tracker_repo(root, name, resolved, git):
    """`(owner/name, None)` for one repo entry, or `(None, problem)`."""
    explicit = resolved["tracker.repo"]
    if explicit:
        if not ghforge.is_repo_name(explicit):
            return None, f"[[repo]] {name}: tracker.repo {explicit!r} is not owner/name"
        return explicit, None
    clone = resolved["clone.path"]
    if not clone:
        return None, f"[[repo]] {name} sets neither tracker.repo nor clone.path"
    path = clone if os.path.isabs(clone) else os.path.join(root, clone)
    repo = ghforge.origin_repo(path, git)
    if repo is None:
        return None, f"[[repo]] {name}: no owner/name in the origin remote of {path}"
    return repo, None


def sync_root(
    root,
    names,
    *,
    resync,
    audit,
    as_json,
    tracker,
    store,
    git,
    now,
    out,
    err,
) -> int:
    """Sync each named repo entry (or every entry) and report. All seams
    injected; exit codes follow the module docstring."""
    try:
        wanted = settings.repo_names(root)
    except settings.SettingsError as problem:
        _err(str(problem), err)
        return USAGE
    if not wanted:
        _err(f"{root}: no [[repo]] entries to sync", err)
        return USAGE
    unknown = [name for name in names if name not in wanted]
    if unknown:
        _err(f"unknown repo {unknown[0]!r} (not a [[repo]] entry in {root})", err)
        return USAGE
    results: dict = {}
    for name in names or wanted:
        try:
            resolved = settings.load(root=root, repo=name)
        except settings.SettingsError as problem:
            _err(str(problem), err)
            return USAGE
        tracker_repo, problem = _tracker_repo(root, name, resolved, git)
        if problem is not None:
            _err(problem, err)
            return USAGE
        operators = _operators(resolved["tracker.operators"], tracker_repo)
        try:
            if resync:
                result = sync.resync_repo(
                    store, tracker, tracker_repo, operators=operators, now=now
                )
            elif audit is not None:
                result = sync.audit_repo(
                    store,
                    tracker,
                    tracker_repo,
                    operators=operators,
                    now=now,
                    limit=audit,
                )
            else:
                result = sync.sync_repo(
                    store,
                    tracker,
                    tracker_repo,
                    operators=operators,
                    now=now,
                    cursor=store.get_cursor(tracker_repo, sync.CURSOR_NAME),
                )
        except board.StoreError as problem:
            _err(str(problem), err)
            return USAGE
        results[name] = {
            "tracker": tracker_repo,
            "seen": result.items_seen,
            "cursor": result.cursor,
            "errors": list(result.errors),
        }
        for line in result.errors:
            _err(line, err)
    seen = sum(row["seen"] for row in results.values())
    failed = sum(len(row["errors"]) for row in results.values())
    if as_json:
        out.write(json.dumps({"repos": results, "seen": seen}, indent=2, sort_keys=True))
        out.write("\n")
    else:
        for name, row in results.items():
            out.write(f"{name}: {row['seen']} applied, cursor {row['cursor'] or 'none'}\n")
    if seen:
        return OK
    return UNREACHABLE if failed else NOTHING


def cmd_sync(args: argparse.Namespace) -> int:
    root = settings.find_root(os.getcwd())
    if root is None:
        _err("no corral.toml above here", sys.stderr)
        return USAGE
    if args.audit is not None and args.audit <= 0:
        _err("--audit needs a positive integer", sys.stderr)
        return USAGE
    try:
        store = board.open_store()
    except board.StoreError as problem:
        _err(str(problem), sys.stderr)
        return USAGE
    with store:
        return sync_root(
            root,
            args.repo,
            resync=args.resync,
            audit=args.audit,
            as_json=args.json,
            tracker=_production_tracker(),
            store=store,
            git=_git,
            now=time.time(),
            out=sys.stdout,
            err=sys.stderr,
        )


def add_parser(sub) -> None:
    """Register `sync` on the top-level subparsers."""
    p = sub.add_parser(
        "sync",
        help="feed the board store from the tracker's change feed",
    )
    p.add_argument(
        "repo",
        nargs="*",
        default=[],
        help="the [[repo]] entries to sync (default: every entry)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--resync",
        action="store_true",
        help="rebuild from the full listing instead of the change feed",
    )
    group.add_argument(
        "--audit",
        type=int,
        default=None,
        metavar="N",
        help="re-read the N rows verified least recently instead of the feed",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_sync)
