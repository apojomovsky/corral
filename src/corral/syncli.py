"""`corral sync`: feed the board store from the tracker's change feed.

Runs the incremental feed for the ops root's repositories (each
``[[repo]]`` entry; ``tracker.repo`` when set, else the clone's
origin), with ``--resync`` for a full rebuild and ``--audit N`` for
the rolling re-read of the N rows verified least recently. Exit codes:
0 synced, 1 nothing changed, 3 tracker unreachable, 4 usage or
configuration error. Partial failures with something synced are still 0;
with ``--push`` a sync phase that failed and synced nothing is still 3,
even when the mirror pass sent a write.
"""

import argparse
import json
import os
import subprocess
import sys
import time

from corral import board, settings, sync
from corral import tracker as iface
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


def repo_operators(root, names, git):
    """`{name: (tracker_repo, operators)}` for each named ``[[repo]]`` entry.

    The single resolver for per-repo operators: sync, mirror and the
    dispatch pass all build their tracker-repo mapping from it, so the
    three agree on which authors are internal. Returns (entries, None);
    on a configuration error returns (None, problem).
    """
    entries = {}
    for name in names:
        try:
            resolved = settings.load(root=root, repo=name)
        except settings.SettingsError as problem:
            return None, str(problem)
        tracker_repo, problem = _tracker_repo(root, name, resolved, git)
        if problem is not None:
            return None, problem
        entries[name] = (
            tracker_repo,
            _operators(resolved["tracker.operators"], tracker_repo),
        )
    return entries, None


def _has_board_reader(tracker) -> bool:
    """Whether ``tracker`` can run the board step, through the factory's
    predicate. A board-variant repo whose tracker lacks this is a
    configuration error, never a silent label-only run."""
    from corral import trackers

    return trackers.is_board_tracker(tracker)


def _board_step(store, tracker, tracker_repo, *, operators, now):
    """The board poll for one board repo, merged into the pass report.

    Only the board's own failures are caught here (a live check the
    budget or the board refused): the step records a board-outage line,
    keeps the old cursor, and the pass continues on store data. Anything
    else (a programming error, a store failure) propagates.
    """
    try:
        schema = tracker.board_schema()
    except (iface.TrackerError, settings.SettingsError) as err:
        old = store.get_cursor(tracker_repo, sync.BOARD_CURSOR_NAME)
        return sync.SyncResult(0, old, (f"{tracker_repo}: {err}",))
    return sync.sync_board_repo(
        store,
        tracker,
        schema,
        tracker_repo,
        operators=operators,
        now=now,
        max_pages=tracker.board_poll_pages,
    )


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
    push=False,
    tracker_factory=None,
) -> int:
    """Sync each named repo entry (or every entry) and report. All seams
    injected; exit codes follow the module docstring.

    ``tracker_factory`` maps a tracker repo to its tracker, built by the
    one factory from the entry's resolved settings (labels or board);
    when omitted the single ``tracker`` serves every repo. Board repos
    run the feed step then the board step; a board-variant repo whose
    tracker has no board reader is exit 4 naming the repo.
    """
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
    entries, problem = repo_operators(root, names or wanted, git)
    if problem is not None:
        _err(problem, err)
        return USAGE
    variants: dict = {}
    for name in names or wanted:
        try:
            variants[name] = settings.load(root=root, repo=name)["tracker.variant"]
        except settings.SettingsError as problem:
            _err(str(problem), err)
            return USAGE
    results: dict = {}
    mirror_ops: dict = {}
    repo_trackers: dict = {}
    for name in names or wanted:
        tracker_repo, operators = entries[name]
        if tracker_factory is not None:
            try:
                repo_tracker = tracker_factory(tracker_repo)
            except KeyError:
                _err(f"[[repo]] {name}: no tracker for {tracker_repo}", err)
                return USAGE
        else:
            repo_tracker = tracker
        repo_trackers[name] = repo_tracker
        if variants[name] == "board" and not _has_board_reader(repo_tracker):
            _err(
                f"[[repo]] {name}: tracker.variant is 'board' but the tracker"
                f" for {tracker_repo} has no board reader",
                err,
            )
            return USAGE
        try:
            if resync:
                result = sync.resync_repo(
                    store, repo_tracker, tracker_repo, operators=operators, now=now
                )
            elif audit is not None:
                result = sync.audit_repo(
                    store,
                    repo_tracker,
                    tracker_repo,
                    operators=operators,
                    now=now,
                    limit=audit,
                )
            else:
                result = sync.sync_repo(
                    store,
                    repo_tracker,
                    tracker_repo,
                    operators=operators,
                    now=now,
                    cursor=store.get_cursor(tracker_repo, sync.CURSOR_NAME),
                )
            board_result = None
            if variants[name] == "board":
                board_result = _board_step(
                    store, repo_tracker, tracker_repo, operators=operators, now=now
                )
        except board.StoreError as problem:
            _err(str(problem), err)
            return USAGE
        board_seen = board_result.items_seen if board_result is not None else 0
        board_errors = list(board_result.errors) if board_result is not None else []
        results[name] = {
            "tracker": tracker_repo,
            "seen": result.items_seen + board_seen,
            "cursor": result.cursor,
            "errors": list(result.errors) + board_errors,
            "conflicts": list(result.conflicts),
        }
        if board_result is not None:
            results[name]["board_seen"] = board_seen
            results[name]["board_cursor"] = board_result.cursor
        mirror_ops[tracker_repo] = operators
        for line in result.errors:
            _err(line, err)
        for line in board_errors:
            _err(line, err)
        for line in result.conflicts:
            _err(f"claim conflict: {line} (corral wins)", err)
    seen = sum(row["seen"] for row in results.values())
    failed = sum(len(row["errors"]) for row in results.values())
    pushed: dict | None = None
    if push:
        from corral import mirror

        try:
            if tracker_factory is not None:
                pairs = [(entries[name][0], repo_trackers[name]) for name in names or wanted]
                outcome = mirror.push_repos(store, pairs, operators=mirror_ops, now=now)
            else:
                outcome = mirror.push(store, tracker, operators=mirror_ops, now=now)
        except board.StoreError as problem:
            _err(str(problem), err)
            return USAGE
        pushed = {
            "sent": outcome.sent,
            "dropped": outcome.dropped,
            "dirty": outcome.dirty,
            "stopped": outcome.stopped,
            "errors": list(outcome.errors),
        }
        for line in outcome.errors:
            _err(line, err)
    if as_json:
        doc: dict = {"repos": results, "seen": seen}
        if pushed is not None:
            doc["mirror"] = pushed
        out.write(json.dumps(doc, indent=2, sort_keys=True))
        out.write("\n")
    else:
        for name, row in results.items():
            out.write(f"{name}: {row['seen']} applied, cursor {row['cursor'] or 'none'}")
            if "board_seen" in row:
                out.write(
                    f", board {row['board_seen']} applied,"
                    f" board cursor {row['board_cursor'] or 'none'}"
                )
            out.write("\n")
        if pushed is not None:
            out.write(
                f"mirror: {pushed['sent']} sent,"
                f" {pushed['dropped']} dropped,"
                f" {pushed['dirty']} dirty\n"
            )
            if pushed["stopped"] is not None:
                out.write(f"mirror: rate limited, retry after {pushed['stopped']:g}s\n")
    landed = seen + (pushed["sent"] if pushed is not None else 0)
    bad = failed + (len(pushed["errors"]) if pushed is not None else 0)
    if failed and not seen:
        return UNREACHABLE
    if landed:
        return OK
    return UNREACHABLE if bad else NOTHING


def cmd_sync(args: argparse.Namespace) -> int:
    from corral import trackers

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
    try:
        wanted = settings.repo_names(root)
    except settings.SettingsError as problem:
        _err(str(problem), sys.stderr)
        return USAGE
    mapping, problem = trackers.production_trackers(root, args.repo or wanted, git=_git)
    if problem is not None:
        _err(problem, sys.stderr)
        return USAGE
    by_repo = {}
    for entry in mapping.values():
        by_repo.setdefault(entry.tracker_repo, entry.tracker)
    with store:
        return sync_root(
            root,
            args.repo,
            resync=args.resync,
            audit=args.audit,
            as_json=args.json,
            tracker=None,
            store=store,
            git=_git,
            now=time.time(),
            out=sys.stdout,
            err=sys.stderr,
            push=args.push,
            tracker_factory=by_repo.__getitem__,
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
    p.add_argument(
        "--push",
        action="store_true",
        help="run a mirror pass over the outbox after the sync",
    )
    p.set_defaults(func=cmd_sync)
