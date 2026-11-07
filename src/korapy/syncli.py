"""`korapy sync`: feed the board store from the tracker's change feed.

Runs the incremental feed for the ops root's repositories (each
``[[repo]]`` entry; ``tracker.repo`` when set, else the clone's
origin), with ``--resync`` for a full rebuild and ``--audit N`` for
the rolling re-read of the N rows verified least recently. Exit codes:
0 synced, 1 nothing changed, 2 a rename or a drive pass holds the
drive lock, 3 tracker unreachable, 4 usage or configuration error.
A read the tracker answers with a validation
rejection is refused, not unreachable: its line is reported and the
repo keeps its cursor, and a pass whose only failures are refused
reads exits 1. Corrupt stored rows are usage errors: a sync that
failed only on those (and synced nothing) exits 4, while tracker
failures with nothing synced exit 3. When one pass has both tracker
failures and corrupt rows with nothing synced, the store check runs
first and the pass exits 4; both lines still print to stderr, so the
report names the outage the code masks. Partial failures with something
synced are still 0; with ``--push`` a sync phase that failed and synced
nothing is still 3 (or 4 for corrupt rows), even when the mirror pass
sent a write.
"""

import argparse
import json
import os
import subprocess
import sys
import time

from korapy import board, paths, settings, sync
from korapy import tracker as iface
from korapy.gitenv import clean_env
from korapy.github import forge as ghforge

OK, NOTHING, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4


def _err(text, err):
    print(f"korapy sync: {text}", file=err)


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


def repo_operators(root, names, git, *, project=None, host=None, env=None):
    """`{name: (tracker_repo, operators)}` for each named ``[[repo]]`` entry.

    The single resolver for per-repo operators: sync, mirror and the
    dispatch pass all build their tracker-repo mapping from it, so the
    three agree on which authors are internal. Returns (entries, None);
    on a configuration error returns (None, problem). When `project` is
    given it is the pre-read project table (see `settings.read_project`,
    with `host` the pre-read host table): every entry resolves from
    that single parse instead of reading the file once per entry, so
    one pass never mixes entries from a concurrent edit. `env` selects
    the environment for both layers, defaulting as in `settings.load`.
    """
    if project is not None and env is None:
        env = os.environ
    entries = {}
    for name in names:
        try:
            if project is None:
                resolved = settings.load(root=root, repo=name, env=env)
            else:
                resolved = settings.resolve(
                    host=host if host is not None else {},
                    project=project,
                    repo=name,
                    env=env,
                    host_name=os.path.join(paths.config_dir(env), settings.HOST_CONFIG_NAME),
                    project_name=os.path.join(root, settings.PROJECT_FILE)
                    if root
                    else settings.PROJECT_FILE,
                )
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


def root_short_map(root, git):
    """`{short [[repo]] name: tracker repo}` for one ops root.

    The one shared single-root projection of `repo_operators`: both
    the dispatch pass (`drive.production_repo_map`, across registered
    roots) and the fail-soft `short_to_tracker` below build from here,
    so the two builders cannot drift. A configuration problem raises
    `settings.SettingsError`, so strict callers refuse instead of
    joining against a guess.
    """
    names = settings.repo_names(root)
    entries, problem = repo_operators(root, names, git)
    if problem is not None or entries is None:
        raise settings.SettingsError(problem or f"{root}: cannot read the [[repo]] entries")
    return {name: tracker for name, (tracker, _ops) in entries.items()}


def short_to_tracker(root, git):
    """`{short [[repo]] name: tracker repo}` for one ops root.

    The single-root projection of the shared `repo_operators` map:
    the review-record callers (record, show, takeoff, merge) resolve
    their ticket repo through it, so one map feeds every spelling
    decision. No root reads as no entries; a configuration problem
    reads as no entries too, so short names (the on-disk truth every
    writer uses) keep resolving: only a tracker spelling the map
    cannot translate fails, loudly, at the caller.
    """
    if not root:
        return {}
    try:
        return root_short_map(root, git)
    except settings.SettingsError:
        return {}


def _has_board_reader(tracker) -> bool:
    """Whether ``tracker`` can run the board step, through the factory's
    predicate. A board-variant repo whose tracker lacks this is a
    configuration error, never a silent label-only run."""
    from korapy import trackers

    return trackers.is_board_tracker(tracker)


def _board_step(store, tracker, tracker_repo, *, operators, now):
    """The board poll for one board repo, merged into the pass report.

    Only the board's own failures are caught here (a live check the
    budget or the board refused): the step records a board-outage line,
    keeps the old cursor, and the pass continues on store data. A row
    the poll cannot apply is already a line in the board result, not a
    raise. A complete poll also adopts the tracker's missing issues
    through the same reader; failed adds ride along as adopt lines
    without failing the sync. Anything else (a programming error, any
    other store failure) propagates.
    """
    try:
        schema = tracker.board_schema()
    except iface.TrackerRejected as err:
        old = store.get_cursor(tracker_repo, sync.BOARD_CURSOR_NAME)
        return sync.SyncResult(0, old, (), refused=(f"{tracker_repo}: {err}",))
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
    lock=None,
    lock_path=None,
) -> int:
    """Sync each named repo entry (or every entry) and report. All seams
    injected; exit codes follow the module docstring.

    ``tracker_factory`` maps a tracker repo to its tracker, built by the
    one factory from the entry's resolved settings (labels or board);
    when omitted the single ``tracker`` serves every repo. Board repos
    run the feed step then the board step; a board-variant repo whose
    tracker has no board reader is exit 4 naming the repo. ``lock`` is
    the drive-lock seam (``drive.drive_lock``, taking ``lock_path``
    from the one resolver ``drive.lock_path``), required: the whole
    pass runs under it, so a sync of the old name cannot land rows
    under the old key while a rename migrates them; a held lock
    refuses with exit 2 naming the holder, and a missing lock fails
    closed with exit 4 instead of syncing unguarded.
    """
    if lock is None:
        _err("cannot sync without the drive lock: refusing unguarded", err)
        return USAGE
    from korapy import drive

    try:
        with lock(lock_path):
            return _sync_body(
                root,
                names,
                resync=resync,
                audit=audit,
                as_json=as_json,
                tracker=tracker,
                store=store,
                git=git,
                now=now,
                out=out,
                err=err,
                push=push,
                tracker_factory=tracker_factory,
            )
    except drive.LockHeld as held:
        _err(str(held), err)
        return RACE
    except drive.LockError as problem:
        _err(str(problem), err)
        return USAGE


def _sync_body(
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
    """The resolve, sync and mirror behind `sync_root`: run unlocked."""
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
        board_refused = list(board_result.refused) if board_result is not None else []
        board_store_errors = list(board_result.store_errors) if board_result is not None else []
        board_adopt_errors = list(board_result.adopt_errors) if board_result is not None else []
        board_status_errors = list(board_result.status_errors) if board_result is not None else []
        results[name] = {
            "tracker": tracker_repo,
            "seen": result.items_seen + board_seen,
            "cursor": result.cursor,
            "errors": list(result.errors) + board_errors,
            "refused": list(result.refused) + board_refused,
            "store_errors": list(result.store_errors) + board_store_errors,
            "conflicts": list(result.conflicts),
            "adopt_errors": list(board_adopt_errors),
            "status_errors": list(board_status_errors),
        }
        if board_result is not None:
            results[name]["board_seen"] = board_seen
            results[name]["board_cursor"] = board_result.cursor
        mirror_ops[tracker_repo] = operators
        for line in result.errors:
            _err(line, err)
        for line in board_errors:
            _err(line, err)
        for line in result.refused:
            _err(line, err)
        for line in board_refused:
            _err(line, err)
        for line in result.store_errors:
            _err(line, err)
        for line in board_store_errors:
            _err(line, err)
        for line in board_adopt_errors:
            _err(line, err)
        for line in board_status_errors:
            _err(line, err)
        for line in result.conflicts:
            _err(f"claim conflict: {line} (korapy wins)", err)
    seen = sum(row["seen"] for row in results.values())
    failed = sum(len(row["errors"]) for row in results.values())
    store_failed = sum(len(row["store_errors"]) for row in results.values())
    pushed: dict | None = None
    if push:
        from korapy import mirror

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
    if store_failed and not seen:
        return USAGE
    if failed and not seen:
        return UNREACHABLE
    if landed:
        return OK
    return UNREACHABLE if bad else NOTHING


def cmd_sync(args: argparse.Namespace) -> int:
    from korapy import drive, trackers

    root = settings.find_root(os.getcwd())
    if root is None:
        _err("no korapy.toml above here", sys.stderr)
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
            lock=drive.drive_lock,
            lock_path=drive.lock_path(),
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
