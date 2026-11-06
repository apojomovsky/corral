"""`korapy mirror`: push the board store's outbox into the tracker.

Exit codes: 0 a write landed, 1 nothing queued, 2 a rename or a
drive pass holds the drive lock, 3 every attempted write failed or a
rate limit stopped the pass with nothing sent, 4 usage error."""

import argparse
import json
import os
import sys
import time

from korapy import board, mirror, settings, syncli

OK, NOTHING, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4


def _err(text, err):
    print(f"korapy mirror: {text}", file=err)


def mirror_root(
    root,
    names,
    *,
    limit,
    as_json,
    tracker,
    store,
    git,
    now,
    out,
    err,
    operators=None,
    tracker_factory=None,
    lock=None,
    lock_path=None,
) -> int:
    """Push the queued outbox writes into the tracker and report.

    ``operators`` maps tracker repos to their operator logins; when omitted
    it is resolved from each ``[[repo]]`` entry the same way `korapy sync`
    does. ``tracker_factory`` maps a tracker repo to its tracker, built by
    the one factory (labels or board); when omitted the single ``tracker``
    serves every repo, pushing through each repo's own tracker otherwise.
    All seams injected; exit codes follow the module docstring. ``lock``
    is the drive-lock seam (``drive.drive_lock``, taking ``lock_path``
    from the one resolver ``drive.lock_path``), required: the whole
    pass runs under it, so a mirror cannot push while a rename migrates
    the keys it writes through; a held lock refuses with exit 2 naming
    the holder, and a missing lock fails closed with exit 4 instead of
    mirroring unguarded.
    """
    if lock is None:
        _err("cannot mirror without the drive lock: refusing unguarded", err)
        return USAGE
    from korapy import drive

    try:
        with lock(lock_path):
            return _mirror_body(
                root,
                names,
                limit=limit,
                as_json=as_json,
                tracker=tracker,
                store=store,
                git=git,
                now=now,
                out=out,
                err=err,
                operators=operators,
                tracker_factory=tracker_factory,
            )
    except drive.LockHeld as held:
        _err(str(held), err)
        return RACE
    except drive.LockError as problem:
        _err(str(problem), err)
        return USAGE


def _mirror_body(
    root,
    names,
    *,
    limit,
    as_json,
    tracker,
    store,
    git,
    now,
    out,
    err,
    operators=None,
    tracker_factory=None,
) -> int:
    """The resolve and push behind `mirror_root`: run unlocked."""
    try:
        wanted = settings.repo_names(root)
    except settings.SettingsError as problem:
        _err(str(problem), err)
        return USAGE
    if not wanted:
        _err(f"{root}: no [[repo]] entries to mirror", err)
        return USAGE
    unknown = [name for name in names if name not in wanted]
    if unknown:
        _err(f"unknown repo {unknown[0]!r} (not a [[repo]] entry in {root})", err)
        return USAGE
    if operators is None:
        entries, problem = syncli.repo_operators(root, names or wanted, git)
        if problem is not None:
            _err(problem, err)
            return USAGE
        operators = {}
        for _name, (tracker_repo, ops) in entries.items():
            operators[tracker_repo] = ops
    try:
        if tracker_factory is not None:
            entries, problem = syncli.repo_operators(root, names or wanted, git)
            if problem is not None:
                _err(problem, err)
                return USAGE
            pairs = []
            for name in names or wanted:
                tracker_repo, _ = entries[name]
                try:
                    pairs.append((tracker_repo, tracker_factory(tracker_repo)))
                except KeyError:
                    _err(f"[[repo]] {name}: no tracker for {tracker_repo}", err)
                    return USAGE
            result = mirror.push_repos(store, pairs, operators=operators, now=now, limit=limit)
        else:
            result = mirror.push(store, tracker, operators=operators, now=now, limit=limit)
    except board.StoreError as problem:
        _err(str(problem), err)
        return USAGE
    if as_json:
        out.write(
            json.dumps(
                {
                    "sent": result.sent,
                    "dropped": result.dropped,
                    "dirty": result.dirty,
                    "stopped": result.stopped,
                    "errors": list(result.errors),
                },
                indent=2,
                sort_keys=True,
            )
        )
        out.write("\n")
    else:
        out.write(f"mirror: {result.sent} sent, {result.dropped} dropped, {result.dirty} dirty\n")
        if result.stopped is not None:
            out.write(f"mirror: rate limited, retry after {result.stopped:g}s\n")
    for line in result.errors:
        _err(line, err)
    if result.sent:
        return OK
    if result.stopped is not None or result.errors:
        return UNREACHABLE
    return NOTHING


def cmd_mirror(args: argparse.Namespace) -> int:
    from korapy import drive, trackers

    root = settings.find_root(os.getcwd())
    if root is None:
        _err("no korapy.toml above here", sys.stderr)
        return USAGE
    if args.limit is not None and args.limit <= 0:
        _err("--limit needs a positive integer", sys.stderr)
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
    mapping, problem = trackers.production_trackers(root, args.repo or wanted, git=syncli._git)
    if problem is not None:
        _err(problem, sys.stderr)
        return USAGE
    by_repo = {}
    for entry in mapping.values():
        by_repo.setdefault(entry.tracker_repo, entry.tracker)
    with store:
        return mirror_root(
            root,
            args.repo,
            limit=args.limit,
            as_json=args.json,
            tracker=None,
            store=store,
            git=syncli._git,
            now=time.time(),
            out=sys.stdout,
            err=sys.stderr,
            tracker_factory=by_repo.__getitem__,
            lock=drive.drive_lock,
            lock_path=drive.lock_path(),
        )


def _add_flags(p) -> None:
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="push at most N queued writes (default: every queued write)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")


def build_mirror_parser() -> argparse.ArgumentParser:
    """A standalone `korapy mirror` parser (tests; `add_parser` below)."""
    parser = argparse.ArgumentParser(prog="korapy mirror", description=__doc__)
    parser.add_argument(
        "repo",
        nargs="*",
        default=[],
        help="the [[repo]] entries to mirror (default: every entry)",
    )
    _add_flags(parser)
    return parser


def add_parser(sub) -> None:
    """Register `mirror` on the top-level subparsers."""
    p = sub.add_parser(
        "mirror",
        help="push the board store's queued writes into the tracker",
    )
    p.add_argument(
        "repo",
        nargs="*",
        default=[],
        help="the [[repo]] entries to mirror (default: every entry)",
    )
    _add_flags(p)
    p.set_defaults(func=cmd_mirror)
