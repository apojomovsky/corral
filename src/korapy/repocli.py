"""`korapy repo rename`: migrate the store after a GitHub repository rename.

A renamed tracker repository keeps its issues, so GitHub redirects the
old `owner/repo` and plain reads keep working; but the board store keys
every row by the old name, and search-based calls (a label filter, a
head-branch filter) quietly return nothing for it. The key cannot be
changed in the config without orphaning every claim, so this verb moves
the keys: items, claims (agent and epoch kept), the outbox and the sync
cursors, in a single transaction, with a backup left beside the store.

Exit codes: 0 migrated (or a plan with changes), 1 nothing to do (an
unknown old name, the same name twice, a second run, or a plan with no
changes), 2 a live or stale agent entry or a live-held claim blocks
the move (named), or a drive pass holds its lock (named), 3 the
live-unit scan cannot reach systemd, 4 a bad name, a target that
already holds rows, an unreadable store or config, or a lock that
cannot be set up.
"""

import argparse
import os
import sys
import time

from korapy import board, reponame

OK, NO, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4


def _err(text, err):
    print(f"korapy repo rename: {text}", file=err)


def _held_claims(store, repo):
    """`[(repo, number, agent)]` claims on ``repo``: the liveness scan."""
    held = []
    for item in store.items(repo=repo):
        claim = store.claim_of(item.repo, item.number)
        if claim is not None and claim.agent:
            held.append((claim.repo, claim.number, claim.agent))
    return held


def _counts_text(counts):
    return (
        f"{counts['items']} items,"
        f" {counts['claims']} claims,"
        f" {counts['outbox']} outbox rows,"
        f" {counts['cursors']} cursors"
    )


def _backup_target(store_path, now):
    """A backup path beside the store that no backup holds yet."""
    target = reponame.backup_path(store_path, now)
    stem, suffix = os.path.splitext(target)
    extra = 1
    while os.path.exists(target):
        extra += 1
        target = f"{stem}-{extra}{suffix}"
    return target


def _remove_fresh_backup(path):
    """Remove the backup this run wrote after a refused move. Best effort.

    Call only when this run's `backup_into` returned cleanly: the flag
    at the call site proves the file is ours. A path alone never proves
    it, since a concurrent writer can plant the target between selection
    and removal. Earlier backups are never touched; a refused run rotates
    nothing, and a later success prunes by retention.
    """
    try:
        os.remove(path)
    except OSError:
        pass


def units_exit(problem) -> int:
    """The exit code for a units failure during the liveness scan.

    Unreachable (systemd cannot be reached) returns UNREACHABLE; any
    other units failure keeps its own code.
    """
    from korapy import units

    if isinstance(problem, units.Unreachable):
        return UNREACHABLE
    return problem.exit_code


def _gathered_liveness(gather, err):
    """Fresh ``(entries, live_units)`` from ``gather``, or ``(None, exit)``.

    ``gather`` runs wherever the caller holds exclusion, so a unit that
    starts after the verb does is still seen. A units failure keeps its
    own exit code (unreachable systemd is 3); a registry failure is a
    usage error.
    """
    from korapy import registry, units

    try:
        return gather(), None
    except units.UnitsError as problem:
        _err(str(problem), err)
        return None, units_exit(problem)
    except registry.RegistryError as problem:
        _err(str(problem), err)
        return None, USAGE


def rename_root(
    old,
    new,
    *,
    store,
    store_path,
    registry_entries=(),
    live_units=frozenset(),
    mapping=None,
    project_shorts=None,
    owners=None,
    now,
    out,
    err,
    plan=False,
    lock,
    lock_path=None,
    gather=None,
) -> int:
    """Migrate one tracker repo's store keys, or plan the move. All seams injected.

    ``registry_entries`` are ``(entry, state)`` registry rows,
    ``live_units`` are ``(project, short repo, number)`` tickets with a
    live unit, and ``mapping`` resolves short entry names to tracker
    repos. ``project_shorts`` is ``{project: [shorts]}`` for the owners
    fallback; ``owners`` names the projects owning the renamed repo
    outright. ``lock`` is the drive-lock seam (``drive.drive_lock``,
    taking ``lock_path`` from the one resolver ``drive.lock_path``),
    required: the move holds it for the whole liveness scan, backup and
    key migration, so a concurrent drive pass cannot claim between the
    scan and the move; a held lock refuses with exit 2 naming the
    holder, and a missing lock outside ``--plan`` fails closed with
    exit 4 instead of migrating unguarded. ``gather`` is a zero-arg
    callable returning fresh ``(registry_entries, live_units)``; it is
    read inside the lock (outside any lock for a plan), so a snapshot
    taken before the lock cannot miss an agent that started late. A
    plan holds no lock and writes nothing, like ``drive --plan``.
    Exit codes follow the module docstring.
    """
    if plan:
        if gather is not None:
            gathered, exit_code = _gathered_liveness(gather, err)
            if gathered is None:
                return exit_code
            registry_entries, live_units = gathered
        return _rename_body(
            old,
            new,
            store=store,
            store_path=store_path,
            registry_entries=registry_entries,
            live_units=live_units,
            mapping=mapping,
            project_shorts=project_shorts,
            owners=owners,
            now=now,
            out=out,
            err=err,
            plan=plan,
        )
    if lock is None:
        _err("cannot migrate without the drive lock: refusing unguarded", err)
        return USAGE
    from korapy import drive

    try:
        with lock(lock_path):
            if gather is not None:
                gathered, exit_code = _gathered_liveness(gather, err)
                if gathered is None:
                    return exit_code
                registry_entries, live_units = gathered
            return _rename_body(
                old,
                new,
                store=store,
                store_path=store_path,
                registry_entries=registry_entries,
                live_units=live_units,
                mapping=mapping,
                project_shorts=project_shorts,
                owners=owners,
                now=now,
                out=out,
                err=err,
                plan=plan,
            )
    except drive.LockHeld as held:
        _err(str(held), err)
        return RACE
    except drive.LockError as problem:
        _err(str(problem), err)
        return USAGE


def _rename_body(
    old,
    new,
    *,
    store,
    store_path,
    registry_entries,
    live_units,
    mapping,
    project_shorts,
    owners,
    now,
    out,
    err,
    plan,
) -> int:
    """The parse, scan, backup and move behind `rename_root`: run unlocked."""
    mapping = dict(mapping or {})
    try:
        old, new = reponame.parse_names(old, new)
    except ValueError as problem:
        _err(str(problem), err)
        return USAGE
    if old == new:
        out.write(f"nothing to do: {old} is already {new}\n")
        return NO
    if owners is None and project_shorts is not None:
        owners = reponame.project_owners(project_shorts, mapping, {old, new})
    try:
        before = store.rename_counts(old)
        after = store.rename_counts(new)
    except board.StoreError as problem:
        _err(str(problem), err)
        return USAGE
    if not sum(before.values()):
        out.write(f"nothing to do: no store rows for {old}\n")
        return NO
    if sum(after.values()):
        _err(f"{new} already holds store rows: refusing a merge of two repositories", err)
        return USAGE
    try:
        held = _held_claims(store, old)
    except board.StoreError as problem:
        _err(str(problem), err)
        return USAGE
    blockers = reponame.live_blockers(
        old,
        new,
        claims=held,
        entries=registry_entries,
        live_units=live_units,
        mapping=mapping,
        owners=owners,
    )
    if blockers:
        _err("cannot migrate while live work holds these tickets:", err)
        for line in blockers:
            _err(f"  {line}", err)
        return RACE
    backup = _backup_target(store_path, now)
    if plan:
        out.write(f"would rename {old} -> {new}: {_counts_text(before)}\n")
        out.write(f"would write backup {backup}\n")
        out.write(
            "session directories and review records are keyed by the short"
            " repository name and are not touched by this verb; see #479\n"
        )
        return OK
    backup_created = False
    try:
        store.backup_into(backup)
        backup_created = True
    except board.StoreError as problem:
        _err(str(problem), err)
        return USAGE
    try:
        counts = store.rename_repo(old, new)
    except board.RenameRefused as problem:
        if backup_created:
            _remove_fresh_backup(backup)
        _err(str(problem), err)
        return USAGE
    except board.StoreError as problem:
        if backup_created:
            _remove_fresh_backup(backup)
        _err(str(problem), err)
        return USAGE
    reponame.prune_backups(os.path.dirname(store_path))
    out.write(f"renamed {old} -> {new}: {_counts_text(counts)}\n")
    out.write(f"backup: {backup}\n")
    out.write(
        "now point the entry at the new name: update the clone's origin"
        f" remote to the new URL and set tracker.repo to {new!r},"
        " then run `korapy doctor`\n"
    )
    return OK


def cmd_repo(args: argparse.Namespace) -> int:
    from korapy import drive, projects, registry, settings, units

    if args.repo_verb != "rename":
        print(f"korapy: unknown repo verb {args.repo_verb!r}", file=sys.stderr)
        return USAGE
    root = settings.find_root(os.getcwd())
    if root is None:
        print("korapy repo rename: no korapy.toml above here", file=sys.stderr)
        return USAGE
    try:
        store = board.open_store()
    except board.StoreError as problem:
        print(f"korapy repo rename: {problem}", file=sys.stderr)
        return USAGE
    try:
        registered = projects.load_all()
        mapping = drive.production_repo_map(registered)
        project_shorts = {project.name: settings.repo_names(project.root) for project in registered}
        cfg = registry.config()
    except (settings.SettingsError, projects.ProjectsError, board.StoreError) as problem:
        print(f"korapy repo rename: {problem}", file=sys.stderr)
        return USAGE
    except registry.RegistryError as problem:
        print(f"korapy repo rename: {problem}", file=sys.stderr)
        return USAGE

    def gather():
        """Fresh liveness inputs; `rename_root` reads these inside the lock."""
        listed = registry.visible(time.time(), cfg=cfg)
        live = set()
        for project in registered:
            rows = units.list_units(project.unit_prefix)
            live.update(
                (project.name, repo, number)
                for repo, number in units.live_tickets(project.unit_prefix, rows)
            )
        return listed, live

    if args.plan:
        try:
            entries, live = gather()
        except registry.RegistryError as problem:
            print(f"korapy repo rename: {problem}", file=sys.stderr)
            return USAGE
        except units.UnitsError as problem:
            print(f"korapy repo rename: {problem}", file=sys.stderr)
            return units_exit(problem)
        with store:
            return rename_root(
                args.old,
                args.new,
                store=store,
                store_path=board.db_path(),
                registry_entries=entries,
                live_units=live,
                mapping=mapping,
                project_shorts=project_shorts,
                now=time.time(),
                out=sys.stdout,
                err=sys.stderr,
                plan=True,
                lock=drive.drive_lock,
                lock_path=drive.lock_path(),
            )
    with store:
        return rename_root(
            args.old,
            args.new,
            store=store,
            store_path=board.db_path(),
            mapping=mapping,
            project_shorts=project_shorts,
            now=time.time(),
            out=sys.stdout,
            err=sys.stderr,
            plan=False,
            lock=drive.drive_lock,
            lock_path=drive.lock_path(),
            gather=gather,
        )


def build_repo_parser() -> argparse.ArgumentParser:
    """A standalone `korapy repo` parser (tests; `add_parser` below)."""
    parser = argparse.ArgumentParser(prog="korapy repo", description=__doc__)
    verbs = parser.add_subparsers(dest="repo_verb", required=True)
    p = verbs.add_parser("rename", help="migrate the store's keys to the renamed repo")
    p.add_argument("old", help="the old owner/name the store keys rows by")
    p.add_argument("new", help="the new owner/name the tracker answers")
    p.add_argument(
        "--plan",
        action="store_true",
        help="print what would change and change nothing",
    )
    return parser


def add_parser(sub) -> None:
    """Register `repo` and its verbs on the top-level subparsers."""
    parser = sub.add_parser(
        "repo",
        help="the repositories behind the board store: migrate keys after a rename",
    )
    verbs = parser.add_subparsers(dest="repo_verb", required=True)
    p = verbs.add_parser("rename", help="migrate the store's keys to the renamed repo")
    p.add_argument("old", help="the old owner/name the store keys rows by")
    p.add_argument("new", help="the new owner/name the tracker answers")
    p.add_argument(
        "--plan",
        action="store_true",
        help="print what would change and change nothing",
    )
    parser.set_defaults(func=cmd_repo)
