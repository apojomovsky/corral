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
    """The datetime behind a backup name, or None for a foreign file.

    A same-stamp `-N` counter disambiguates two backups from one second;
    it carries no age, so suffixed backups prune by their stamp.
    """
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return None
    stem = name[len(BACKUP_PREFIX) : -len(BACKUP_SUFFIX)]
    base, dash, counter = stem.partition("-")
    if dash and not counter.isdigit():
        return None
    try:
        return datetime.datetime.strptime(base, "%Y%m%dT%H%M%SZ")
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


def project_owners(project_shorts, mapping, wanted):
    """`{project names}` with a short name resolving into ``wanted``.

    Pure over ``{project: [shorts]}`` and the merged short-to-tracker
    mapping: the rename's owners are the projects whose config still
    names the repo. A short-name collision across projects over-names
    rather than under-names, so the rename fails closed.
    """
    mapping = dict(mapping or {})
    wanted = set(wanted)
    return {
        project
        for project, shorts in (project_shorts or {}).items()
        for short in shorts
        if mapping.get(short) in wanted
    }


def _unit_parts(live_units):
    """`(project, short, number)` for each live unit.

    Triples carry the owning project; plain pairs predate it and read
    as project-unknown, which the blocker rule fails closed on.
    """
    for unit in live_units:
        if len(unit) == 3:
            project, short, number = unit
        else:
            short, number = unit
            project = None
        yield project, short, number


def _unmapped_blocks(*, project, owners, related):
    """Whether an unmapped short name blocks the rename.

    The one rule for both liveness joins: independent evidence
    (``related``, a ticket number matching a held claim or the entry
    holding one) blocks on its own, and otherwise only a project known
    to be unrelated lets the rename through. An unknown project,
    unknown owners, or a project owning the renamed repo fails closed.
    A bare number match across projects is not evidence: tickets from
    different projects can share a number while naming different work.
    """
    if related:
        return True
    return not project or not owners or project in owners


def live_blockers(old, new, *, claims, entries, live_units, mapping, owners=None):
    """Human lines for what forbids the rename. Empty means clear.

    ``claims`` are ``(repo, number, agent)`` store claims on either
    name; ``entries`` are ``(entry, state)`` registry rows;
    ``live_units`` are ``(project, short repo, number)`` tickets with a
    live unit (plain ``(short, number)`` pairs read as project-unknown);
    ``mapping`` resolves short entry names to tracker repos, and
    ``owners`` names the projects owning the renamed repo. A live or
    stale registry agent for either name blocks, and so does a claim
    whose ticket has a live unit: migrating under either would orphan
    in-flight work. Released claims (no holder) never block. An
    unmapped short name goes through the shared fail-closed rule: it
    blocks unless the project is known and does not own the renamed
    repo, since the mapping comes from the current config while units
    enumerate live work, and the two can disagree across a config edit.
    """
    wanted = {old, new}
    mapping = dict(mapping or {})
    owners = set(owners or ())
    claim_numbers = {number for _, number, _ in claims}
    claim_agents = {agent for _, _, agent in claims if agent}
    found = []
    for claim_repo, number, agent in claims:
        if not agent or claim_repo not in wanted:
            continue
        parts = sorted(_unit_parts(live_units), key=lambda part: (part[2], part[1], part[0] or ""))
        for project, short, live_number in parts:
            if live_number != number:
                continue
            if short in mapping:
                tracker_repo, _ = drive._tracker_key((short, live_number), mapping)
                if tracker_repo == claim_repo:
                    found.append(f"claim {claim_repo}#{number} by {agent} is held by a live unit")
                    break
            elif _unmapped_blocks(project=project, owners=owners, related=False):
                found.append(f"claim {claim_repo}#{number} by {agent} is held by a live unit")
                break
    for item_entry, state in entries:
        if state not in (registry.LIVE, registry.STALE):
            continue
        ticket = registry.ticket_of(item_entry)
        if ticket is None:
            continue
        short, number = ticket
        if short in mapping:
            tracker_repo, _ = drive._tracker_key(ticket, mapping)
            if tracker_repo in wanted:
                found.append(f"agent {item_entry['id']} is {state} for {tracker_repo}#{number}")
            continue
        project = (item_entry.get("ticket") or {}).get("project") or None
        related = number in claim_numbers or item_entry.get("id") in claim_agents
        if _unmapped_blocks(project=project, owners=owners, related=related):
            found.append(
                f"agent {item_entry['id']} is {state} for {short}#{number}"
                f" with unmapped short name {short!r}"
            )
    return sorted(found)


def canonical_matches(configured, canonical):
    """Whether the tracker's canonical name is the configured one.

    Case alone never counts as a rename: tracker lookups are
    case-insensitive, so only a genuinely different name warns. A
    missing reading is unknown, never evidence: returns None then.
    """
    if not configured or not canonical:
        return None
    return str(configured).casefold() == str(canonical).casefold()
