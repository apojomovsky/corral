"""Feed the board store from the tracker: incremental sync, resync, audit.

The store holds the rows; this module fills them. Writing back to the
tracker (the mirror pass over the outbox) lives elsewhere. Every
function tolerates a ``TrackerError`` per tracker call: partial
progress is kept, the error is reported, and the cursor never advances
past what was applied.
"""

from typing import Iterator, NamedTuple

from corral import board, settings, tracker as iface

settings.register(
    "tracker.repo", str, "", "tracker repository as owner/name (default: the clone's origin)"
)
settings.register(
    "tracker.operators",
    list,
    [],
    "tracker logins treated as operators (default: the repository owner only)",
)

#: The cursor name this module keeps in the store's cursor table.
CURSOR_NAME = "tracker"


class SyncResult(NamedTuple):
    items_seen: int  # store rows touched: applied readings, plus absent marks on resync
    cursor: str | None  # the feed position after the run, None when untouched
    errors: tuple[str, ...]  # one line per failed tracker call; "" never appears
    conflicts: tuple[str, ...] = ()  # resync-only: "repo#N: store=A tracker=B" where corral wins


def _reading(item: iface.TrackerItem) -> board.Item:
    """One tracker item as a store reading. The tracker's ``agent`` and
    ``status`` labels ride along so the upsert can settle queued writes;
    the upsert never originates corral-owned values from them."""
    return board.Item(
        repo=item.repo,
        number=item.number,
        kind=item.kind,
        title=item.title,
        body=item.body,
        state=item.state,
        author=item.author,
        labels=list(item.labels),
        priority=item.priority,
        assignees=list(item.assignees),
        url=item.url,
        tracker_updated_at=item.updated_at,
        from_fork=item.from_fork,
        agent=item.agent,
        status=item.status,
    )


def _store_cursor(store: board.Store, repo: str, cursor: str | None) -> None:
    if cursor is not None:
        store.set_cursor(repo, CURSOR_NAME, cursor)


def sync_repo(
    store: board.Store,
    tracker: iface.Tracker,
    repo: str,
    *,
    operators,
    now: float,
    cursor: str | None,
) -> SyncResult:
    """Apply the tracker's change feed for ``repo`` since ``cursor``.

    Every returned item is upserted; the stored cursor moves to the
    next one. A feed failure keeps prior rows and the old cursor, and
    reports the error.
    """
    try:
        found, nxt = tracker.changes(repo, cursor)
    except iface.TrackerError as err:
        return SyncResult(0, cursor, (f"{repo}: {err}",))
    for item in found:
        store.upsert_item(_reading(item), operators=operators, now=now)
    _store_cursor(store, repo, nxt)
    return SyncResult(len(found), nxt, ())


def resync_repo(
    store: board.Store,
    tracker: iface.Tracker,
    repo: str,
    *,
    operators,
    now: float,
) -> SyncResult:
    """Upsert the tracker's full listing for ``repo``, then mark stored
    rows the listing no longer holds as dirty. Deleted or transferred
    rows stay: only the tracker knows which they are.

    A listing that fails halfway keeps what was applied and marks
    nothing absent: an incomplete listing proves nothing missing.

    This is the only path that rebuilds claims: when the tracker shows
    an agent and the store holds no claim and no queued or sent agent
    write for the item, the claim is adopted with a recovery epoch. A
    stored claim naming a different agent with nothing queued is a
    conflict between hosts: the store wins, and the item is reported in
    ``conflicts`` instead. Incremental sync and audit never adopt.
    """
    seen = 0
    listed: set[int] = set()
    stamps: list[float] = []
    errors: list[str] = []
    conflicts: list[str] = []
    shown: list[tuple[str, int, str]] = []  # tracker-reported claims, decided below
    try:
        walked: Iterator[iface.TrackerItem] = tracker.items(repo)
        for item in walked:
            store.upsert_item(_reading(item), operators=operators, now=now)
            seen += 1
            listed.add(item.number)
            stamps.append(item.updated_at)
            if item.agent:
                shown.append((item.repo, item.number, item.agent))
    except iface.TrackerError as err:
        errors.append(f"{repo}: {err}")
    # Adoptions are decided after the walk: each upsert settles queued
    # writes (a fresher tracker value drops an overridden queue), so only
    # now does the outbox show which items corral already spoke for. A
    # queued or sent agent write blocks adoption; a stored claim naming a
    # different agent is a conflict corral wins instead. Adoptions enqueue
    # nothing, so one snapshot covers every item.
    spoken = {
        (row.repo, row.number)
        for row in store.outbox_rows()
        if row.field == "agent" and row.state in (board.QUEUED, board.SENT)
    }
    for shown_repo, shown_number, shown_agent in shown:
        if (shown_repo, shown_number) in spoken:
            continue
        stored, _ = store.item(shown_repo, shown_number, now=now)
        holder = stored.agent if stored is not None else ""
        if not holder:
            store.adopt_claim(shown_repo, shown_number, shown_agent, epoch=int(now), now=now)
        elif holder != shown_agent:
            conflicts.append(f"{shown_repo}#{shown_number}: store={holder} tracker={shown_agent}")
    if not errors:
        absent = 0
        for row in store.items(repo=repo):
            if row.number not in listed:
                store.mark_absent(repo, row.number, now=now)
                absent += 1
        seen += absent
    cursor = iface.format_cursor(max(stamps)) if stamps else store.get_cursor(repo, CURSOR_NAME)
    _store_cursor(store, repo, cursor)
    return SyncResult(seen, cursor, tuple(errors), tuple(conflicts))


def audit_repo(
    store: board.Store,
    tracker: iface.Tracker,
    repo: str,
    *,
    operators,
    now: float,
    limit: int,
) -> SyncResult:
    """Re-read the ``limit`` rows verified least recently, one by one.

    One failing item does not stop the others: each failure is reported
    and the audit moves on. The cursor is untouched.
    """
    seen = 0
    errors: list[str] = []
    for row in store.items(repo=repo)[: max(limit, 0)]:
        try:
            item = tracker.item(row.repo, row.number)
        except iface.TrackerError as err:
            errors.append(f"{row.repo}#{row.number}: {err}")
            continue
        store.upsert_item(_reading(item), operators=operators, now=now)
        seen += 1
    return SyncResult(seen, store.get_cursor(repo, CURSOR_NAME), tuple(errors))
