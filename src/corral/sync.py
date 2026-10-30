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
settings.register(
    "tracker.variant", str, "labels", "tracker source: labels or board (default: labels)"
)
settings.register("board.poll_pages", int, 5, "board poll page cap per pass (default: small)")
settings.register(
    "board.schema_max_age_s",
    int,
    3600,
    "seconds before the board step refreshes the cached board schema",
)
settings.register(
    "board.item_max_age_s",
    int,
    300,
    "seconds before a freshness-gated read re-reads a board row live",
)

#: The cursor name this module keeps in the store's cursor table.
CURSOR_NAME = "tracker"

#: The board cursor name: its own clock in the same cursor table, naming
#: the newest project-item timestamp applied. It is stored separately and
#: never compared with the feed cursor; each advances only on its own
#: reader's success.
BOARD_CURSOR_NAME = "board"


class SyncResult(NamedTuple):
    items_seen: int  # store rows touched: applied readings, plus absent marks on resync
    cursor: str | None  # the feed position after the run, None when untouched
    errors: tuple[str, ...]  # one line per failed tracker call; "" never appears
    conflicts: tuple[str, ...] = ()  # resync-only: "repo#N: store=A tracker=B" where corral wins


def _reading(item: iface.TrackerItem) -> board.Item:
    """One tracker item as a store reading. The tracker's ``agent`` and
    ``status`` labels ride along so the upsert can settle queued writes;
    the upsert never originates corral-owned values from them. The board
    concepts ride along in ``extra``, copied on read so no two rows share
    one dict; a missing or foreign value reads as carrying nothing."""
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
        extra=dict(item.extra) if isinstance(item.extra, dict) else {},
    )


def _store_cursor(
    store: board.Store, repo: str, cursor: str | None, *, name: str = CURSOR_NAME
) -> None:
    if cursor is not None:
        store.set_cursor(repo, name, cursor)


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


def sync_board_repo(
    store: board.Store,
    reader,
    schema,
    repo: str,
    *,
    operators,
    now: float,
    max_pages: int,
) -> SyncResult:
    """Apply one board poll for ``repo``: page the board items oldest first
    (at most ``max_pages`` pages per call), upsert every returned reading,
    and advance the board cursor to the newest item applied.

    The board cursor is its own clock: it is stored under
    ``BOARD_CURSOR_NAME``, never compared with the feed cursor, and never
    moves backwards. A failed poll keeps the old cursor and reports the
    error; an empty poll changes nothing. Field-level compare falls out of
    the upsert: each reading merges per concept, so a reading that carries
    no board concepts keeps the stored ones, and only the cleared encoding
    (``None`` for a scalar, an empty list for a list) removes one. A
    board-only edit still moves the row forward, because a board-sourced
    reading carries the maximum of the issue and board times.
    """
    old = store.get_cursor(repo, BOARD_CURSOR_NAME)
    try:
        found, _ = reader.poll(schema, max_pages=max_pages)
    except iface.TrackerError as err:
        return SyncResult(0, old, (f"{repo}: {err}",))
    for item in found:
        store.upsert_item(_reading(item), operators=operators, now=now)
    if not found:
        return SyncResult(0, old, ())
    cursor = iface.format_cursor(max(item.updated_at for item in found))
    if old is not None and iface.parse_cursor(old) > iface.parse_cursor(cursor):
        cursor = old
    _store_cursor(store, repo, cursor, name=BOARD_CURSOR_NAME)
    return SyncResult(len(found), cursor, ())


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
