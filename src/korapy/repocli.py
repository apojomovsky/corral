"""`korapy repo rename`: migrate the store after a GitHub repository rename.

A renamed tracker repository keeps its issues, so GitHub redirects the
old `owner/repo` and plain reads keep working; but the board store keys
every row by the old name, and search-based calls (a label filter, a
head-branch filter) quietly return nothing for it. The key cannot be
changed in the config without orphaning every claim, so this verb moves
the keys: items, claims (agent and epoch kept), the outbox and the sync
cursors, in a single transaction, with a backup left beside the store.

Exit codes: 0 migrated (or a plan with changes), 1 nothing to do (an
unknown old name, the same name twice, or a plan with no changes), 2 a
live agent or a live-held claim blocks the move (named), 4 a bad name,
a target that already holds rows, or an unreadable store or config.
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


def rename_root(
    old,
    new,
    *,
    store,
    store_path,
    registry_entries=(),
    live_units=frozenset(),
    mapping=None,
    now,
    out,
    err,
    plan=False,
) -> int:
    """Migrate one tracker repo's store keys, or plan the move. All seams injected.

    ``registry_entries`` are ``(entry, state)`` registry rows,
    ``live_units`` are ``(short repo, number)`` tickets with a live
    unit, and ``mapping`` resolves short entry names to tracker repos.
    Exit codes follow the module docstring.
    """
    mapping = dict(mapping or {})
    try:
        old, new = reponame.parse_names(old, new)
    except ValueError as problem:
        _err(str(problem), err)
        return USAGE
    if old == new:
        out.write(f"nothing to do: {old} is already {new}\n")
        return NO
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
        return OK
    try:
        store.backup_into(backup)
    except board.StoreError as problem:
        _err(str(problem), err)
        return USAGE
    try:
        counts = store.rename_repo(old, new)
    except board.RenameRefused as problem:
        _err(str(problem), err)
        return USAGE
    except board.StoreError as problem:
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
    from korapy import drive, projects, registry, settings, syncli, units

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
        mapping = drive._tracker_repo_map(registered, git=syncli._git)
        cfg = registry.config()
        listed = registry.visible(time.time(), cfg=cfg)
        live = set()
        for project in registered:
            rows = units.list_units(project.unit_prefix)
            live.update(units.live_tickets(project.unit_prefix, rows))
    except (settings.SettingsError, projects.ProjectsError, board.StoreError) as problem:
        print(f"korapy repo rename: {problem}", file=sys.stderr)
        return USAGE
    except registry.RegistryError as problem:
        print(f"korapy repo rename: {problem}", file=sys.stderr)
        return USAGE
    except units.UnitsError as problem:
        print(f"korapy repo rename: {problem}", file=sys.stderr)
        return problem.exit_code
    with store:
        return rename_root(
            args.old,
            args.new,
            store=store,
            store_path=board.db_path(),
            registry_entries=listed,
            live_units=live,
            mapping=mapping,
            now=time.time(),
            out=sys.stdout,
            err=sys.stderr,
            plan=args.plan,
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
