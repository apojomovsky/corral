"""Feed the board store from the tracker: incremental sync, resync, audit.

The store holds the rows; this module fills them. Writing back to the
tracker (the mirror pass over the outbox) lives elsewhere. Every
function tolerates a ``TrackerError`` per tracker call: partial
progress is kept, the error is reported, and the cursor never advances
past what was applied. A corrupt stored row (or an unusable reading)
never aborts the pass either: it is reported in ``store_errors`` and
the pass moves on, so callers can bucket store corruption toward exit
4 while tracker failures stay exit 3. A read the tracker answers with
a validation rejection is refused, not unreachable: it is reported in
``refused`` toward exit 1, and the repo keeps its cursor.
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

#: Agent-id namespaces never adopted from tracker labels: ids the
#: dispatch loop mints. A label carrying one stays unadopted and is
#: reported, so a foreign label can never read as a managed holder.
#: Mirrors `drive.MANAGED_PREFIX` (the loop owns that namespace; this
#: module sits below it, so the value is repeated here and a test pins
#: the copies).
RESERVED_AGENT_PREFIXES = ("drive-",)

#: The cursor name this module keeps in the store's cursor table.
CURSOR_NAME = "tracker"

#: The board cursor name: its own clock in the same cursor table, naming
#: the newest project-item timestamp applied. It is stored separately and
#: never compared with the feed cursor; each advances only on its own
#: reader's success.
BOARD_CURSOR_NAME = "board"

#: The board page token name: the opaque paging cursor to resume a board
#: poll stopped by the page cap. It lives in the same cursor table but is
#: never compared with either timestamp cursor; it is stored when a poll
#: is truncated and cleared when a poll completes.
BOARD_PAGE_NAME = "board_page"


class SyncResult(NamedTuple):
    items_seen: int  # store rows touched: applied readings, plus absent marks on resync
    cursor: str | None  # the position this step advanced (feed or board, per step)
    errors: tuple[str, ...]  # tracker/outage lines; "" never appears
    conflicts: tuple[str, ...] = ()  # resync-only: "repo#N: store=A tracker=B" where corral wins
    store_errors: tuple[str, ...] = ()  # corrupt/unusable-row lines (exit 4, not 3)
    refused: tuple[
        str, ...
    ] = ()  # validation-rejection lines: the tracker answered (exit 1, not 3)


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
    reports the error. An item whose upsert fails (a corrupt stored
    row, an unusable reading) is reported as one store error line
    naming the item and the pass moves on to the next item; the cursor
    still advances past the consumed feed, and only applied items count
    as seen.
    """
    try:
        found, nxt = tracker.changes(repo, cursor)
    except iface.TrackerRejected as err:
        return SyncResult(0, cursor, (), refused=(f"{repo}: {err}",))
    except iface.TrackerError as err:
        return SyncResult(0, cursor, (f"{repo}: {err}",))
    seen = 0
    store_errors: list[str] = []
    for item in found:
        try:
            store.upsert_item(_reading(item), operators=operators, now=now)
        except board.StoreError as err:
            store_errors.append(str(err))
            continue
        seen += 1
    _store_cursor(store, repo, nxt)
    return SyncResult(seen, nxt, (), store_errors=tuple(store_errors))


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
    moves backwards. A poll stopped by ``max_pages`` stores its resume
    token under ``BOARD_PAGE_NAME`` and the next pass resumes from it; a
    poll that completes clears the token. A failed poll keeps the old
    cursor and the old token and reports the error; an empty poll changes
    nothing. An item whose upsert fails (a corrupt stored row, an
    unusable reading) is reported as one store error line naming the
    item and the pass moves on; the page token and cursor still advance
    past the consumed poll, and only applied items count as seen.
    Field-level compare falls out of the upsert: each reading merges
    per concept, so a reading that carries no board concepts keeps the
    stored ones, and only the cleared encoding (``None`` or an empty
    list, whatever the stored type) removes one. A board-only edit
    still moves the row forward, because a board-sourced reading
    carries the maximum of the issue and board times.
    """
    old = store.get_cursor(repo, BOARD_CURSOR_NAME)
    try:
        before = iface.parse_cursor(old) if old is not None else None
        found, resume = reader.poll(
            schema, after=store.get_cursor(repo, BOARD_PAGE_NAME), max_pages=max_pages
        )
    except iface.TrackerRejected as err:
        return SyncResult(0, old, (), refused=(f"{repo}: {err}",))
    except iface.TrackerError as err:
        return SyncResult(0, old, (f"{repo}: {err}",))
    seen = 0
    store_errors: list[str] = []
    for item in found:
        try:
            store.upsert_item(_reading(item), operators=operators, now=now)
        except board.StoreError as err:
            store_errors.append(str(err))
            continue
        seen += 1
    if resume is None:
        store.clear_cursor(repo, BOARD_PAGE_NAME)
    else:
        store.set_cursor(repo, BOARD_PAGE_NAME, resume)
    if not found:
        return SyncResult(0, old, (), store_errors=tuple(store_errors))
    cursor = iface.format_cursor(max(item.updated_at for item in found))
    if before is not None and before > iface.parse_cursor(cursor):
        cursor = old
    _store_cursor(store, repo, cursor, name=BOARD_CURSOR_NAME)
    return SyncResult(seen, cursor, (), store_errors=tuple(store_errors))


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
    nothing absent: an incomplete listing proves nothing missing. An
    item whose upsert fails (a corrupt stored row, an unusable reading)
    is reported as one store error line naming the item and the walk
    moves on; like a halfway failure, nothing is then marked absent,
    and only applied items count as seen.

    This is the only path that rebuilds claims: when the tracker shows
    an agent and the store holds no claim and no queued or sent agent
    write for the item, the claim is adopted with a recovery epoch. A
    shown agent inside the reserved loop namespaces is never adopted:
    the row stays unclaimed and is reported, so a foreign label can
    never read as a managed holder. A stored claim naming a different
    agent with nothing queued is a conflict between hosts: the store
    wins, and the item is reported in ``conflicts`` instead.
    Incremental sync and audit never adopt.
    """
    seen = 0
    listed: set[int] = set()
    stamps: list[float] = []
    errors: list[str] = []
    refused: list[str] = []
    store_errors: list[str] = []
    conflicts: list[str] = []
    shown: list[tuple[str, int, str]] = []  # tracker-reported claims, decided below
    try:
        walked: Iterator[iface.TrackerItem] = tracker.items(repo)
        for item in walked:
            try:
                store.upsert_item(_reading(item), operators=operators, now=now)
            except board.StoreError as err:
                store_errors.append(str(err))
                continue
            seen += 1
            listed.add(item.number)
            stamps.append(item.updated_at)
            if item.agent:
                shown.append((item.repo, item.number, item.agent))
    except iface.TrackerRejected as err:
        refused.append(f"{repo}: {err}")
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
        if shown_agent.startswith(RESERVED_AGENT_PREFIXES):
            conflicts.append(
                f"{shown_repo}#{shown_number}: tracker shows reserved id"
                f" {shown_agent!r}, left unclaimed"
            )
            continue
        try:
            stored, _ = store.item(shown_repo, shown_number, now=now)
        except board.StoreError as err:
            # A row the walk applied but cannot be read back (its extra
            # is fine, so the upsert passed, yet another column is
            # corrupt): report it and move on. A row the walk already
            # failed on never reaches this loop, so lines stay unique.
            store_errors.append(str(err))
            continue
        holder = stored.agent if stored is not None else ""
        if not holder:
            store.adopt_claim(shown_repo, shown_number, shown_agent, epoch=int(now), now=now)
        elif holder != shown_agent:
            conflicts.append(f"{shown_repo}#{shown_number}: store={holder} tracker={shown_agent}")
    if not errors and not refused and not store_errors:
        absent = 0
        try:
            rows = store.items(repo=repo)
        except board.StoreError as err:
            # A corrupt row breaks the stored enumeration itself: like a
            # halfway listing failure, it proves nothing missing, so the
            # row is reported and nothing is marked absent.
            store_errors.append(str(err))
            rows = []
        for row in rows:
            if row.number not in listed:
                store.mark_absent(repo, row.number, now=now)
                absent += 1
        seen += absent
    cursor = iface.format_cursor(max(stamps)) if stamps else store.get_cursor(repo, CURSOR_NAME)
    _store_cursor(store, repo, cursor)
    return SyncResult(
        seen, cursor, tuple(errors), tuple(conflicts), tuple(store_errors), tuple(refused)
    )


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
    and the audit moves on, whether the live re-read (a tracker line, a
    refused line when the tracker answered with a rejection) or
    the upsert failed (a store line: a corrupt stored row, an unusable
    reading), and only applied items count as seen. A corrupt row that
    breaks the stored enumeration itself is reported as one store line
    and the audit reads nothing, rather than aborting the pass. The
    cursor is untouched.
    """
    seen = 0
    errors: list[str] = []
    refused: list[str] = []
    store_errors: list[str] = []
    try:
        rows = store.items(repo=repo)[: max(limit, 0)]
    except board.StoreError as err:
        return SyncResult(seen, store.get_cursor(repo, CURSOR_NAME), (), store_errors=(str(err),))
    for row in rows:
        try:
            item = tracker.item(row.repo, row.number)
        except iface.TrackerRejected as err:
            refused.append(f"{row.repo}#{row.number}: {err}")
            continue
        except iface.TrackerError as err:
            errors.append(f"{row.repo}#{row.number}: {err}")
            continue
        try:
            store.upsert_item(_reading(item), operators=operators, now=now)
        except board.StoreError as err:
            store_errors.append(str(err))
            continue
        seen += 1
    return SyncResult(
        seen,
        store.get_cursor(repo, CURSOR_NAME),
        tuple(errors),
        store_errors=tuple(store_errors),
        refused=tuple(refused),
    )
