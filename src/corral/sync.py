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
    """
    seen = 0
    listed: set[int] = set()
    stamps: list[float] = []
    errors: list[str] = []
    try:
        walked: Iterator[iface.TrackerItem] = tracker.items(repo)
        for item in walked:
            store.upsert_item(_reading(item), operators=operators, now=now)
            seen += 1
            listed.add(item.number)
            stamps.append(item.updated_at)
    except iface.TrackerError as err:
        errors.append(f"{repo}: {err}")
    if not errors:
        absent = 0
        for row in store.items(repo=repo):
            if row.number not in listed:
                store.mark_absent(repo, row.number, now=now)
                absent += 1
        seen += absent
    cursor = iface.format_cursor(max(stamps)) if stamps else store.get_cursor(repo, CURSOR_NAME)
    _store_cursor(store, repo, cursor)
    return SyncResult(seen, cursor, tuple(errors))


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
