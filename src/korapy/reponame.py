"""The pure core behind `korapy repo rename`: names, backups, blockers.

A renamed tracker repository keeps its issues, so the store's
`owner/repo` keys must move with it: items, claims, the outbox and the
sync cursors (the store owns those tables; the CLI moves them through
`Store.rename_repo`). Everything here is pure over injected data;
only the CLI touches the registry, the units, the settings and git.
"""

import datetime
import os

from korapy import drive, registry
from korapy.github import forge as ghforge

#: Backup files the rename verb leaves beside the store.
BACKUP_PREFIX = "board-backup-"
BACKUP_SUFFIX = ".sqlite"

#: How many backups the verb keeps: the newest ones stay, older prune.
BACKUP_KEEP = 5


def parse_names(old, new):
    """`(old, new)` stripped, else ``ValueError``.

    Both must be `owner/name` strings the forge can look up; anything
    else is an operator typo, never a guess.
    """
    kept = []
    for name in (old, new):
        text = str(name or "").strip()
        if not ghforge.is_repo_name(text):
            raise ValueError(f"{name!r} is not an owner/name repository")
        kept.append(text)
    return kept[0], kept[1]


def _stamp(now):
    """The UTC stamp one backup name carries, sortable and chronological."""
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_name(now):
    """The backup file name for ``now``: prefixed, stamped, sortable."""
    return f"{BACKUP_PREFIX}{_stamp(now)}{BACKUP_SUFFIX}"


def backup_path(store_path, now):
    """The backup path for ``now``: beside the store file."""
    return os.path.join(os.path.dirname(store_path), backup_name(now))


def _backup_stamp(name):
    """The datetime behind a backup name, or None for a foreign file."""
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return None
    try:
        return datetime.datetime.strptime(
            name[len(BACKUP_PREFIX) : -len(BACKUP_SUFFIX)], "%Y%m%dT%H%M%SZ"
        )
    except ValueError:
        return None


def prune_backups(directory, keep=BACKUP_KEEP):
    """Drop all but the newest ``keep`` backups in ``directory``.

    Only names with the backup shape are candidates, so a foreign file
    is never touched; an unreadable directory prunes nothing. Returns
    the removed basenames, oldest first.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    dated = sorted((stamp, name) for name in names if (stamp := _backup_stamp(name)) is not None)
    removed = []
    for _, name in dated[: max(0, len(dated) - keep)]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            continue
        removed.append(name)
    return removed


def live_blockers(old, new, *, claims, entries, live_units, mapping):
    """Human lines for what forbids the rename. Empty means clear.

    ``claims`` are ``(repo, number, agent)`` store claims on either
    name; ``entries`` are ``(entry, state)`` registry rows;
    ``live_units`` are ``(short repo, number)`` tickets with a live
    unit; ``mapping`` resolves short entry names to tracker repos. A
    live or stale registry agent for either name blocks, and so does a
    claim whose ticket has a live unit: migrating under either would
    orphan in-flight work. Released claims (no holder) never block.
    """
    wanted = {old, new}
    found = []
    for claim_repo, number, agent in claims:
        if not agent or claim_repo not in wanted:
            continue
        for short, live_number in sorted(live_units):
            if live_number != number:
                continue
            if mapping.get(short, short) == claim_repo:
                found.append(f"claim {claim_repo}#{number} by {agent} is held by a live unit")
                break
    for item_entry, state in entries:
        if state not in (registry.LIVE, registry.STALE):
            continue
        ticket = registry.ticket_of(item_entry)
        if ticket is None:
            continue
        tracker_repo, number = drive._tracker_key(ticket, mapping)
        if tracker_repo in wanted:
            found.append(f"agent {item_entry['id']} is {state} for {tracker_repo}#{number}")
    return sorted(found)


def canonical_matches(configured, canonical):
    """Whether the tracker's canonical name is the configured one.

    Case alone never counts as a rename: tracker lookups are
    case-insensitive, so only a genuinely different name warns. A
    missing reading matches nothing.
    """
    if not configured or not canonical:
        return False
    return str(configured).casefold() == str(canonical).casefold()
