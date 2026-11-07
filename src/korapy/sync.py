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

from korapy import board, boardsweep, boardstatus, settings, tracker as iface

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
    "board.add_per_pass",
    int,
    20,
    "missing board issues added per repo per pass (default: 20; 0 or less adopts nothing)",
)
settings.register(
    "board.status_per_pass",
    int,
    20,
    "board Status writes per repo per pass (default: 20; 0 or less syncs nothing)",
)
settings.register(
    "board.drift_max_age_s",
    int,
    600,
    "seconds a card may stay out of step with its issue before the doctor fails the repo",
)
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

#: Default cap on the GitHub calls the Status step spends listing open
#: pull requests per repo per pass. Mirrors `drive.DEFAULT_PULLS_MAX_CALLS`
#: (the loop owns that budget; this module sits below it, so the value is
#: repeated here and a test pins the copies).
DEFAULT_PULLS_MAX_CALLS = 20

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
    conflicts: tuple[str, ...] = ()  # resync-only: "repo#N: store=A tracker=B" where korapy wins
    store_errors: tuple[str, ...] = ()  # corrupt/unusable-row lines (exit 4, not 3)
    refused: tuple[
        str, ...
    ] = ()  # validation-rejection lines: the tracker answered (exit 1, not 3)
    adopt_errors: tuple[
        str, ...
    ] = ()  # failed board adds, retried next pass, never counted toward an exit
    status_errors: tuple[
        str, ...
    ] = ()  # failed Status writes, retried next pass, never counted toward an exit
    adopted: tuple[str, ...] = ()  # "repo#N" the adopt step added this call
    status_written: tuple[str, ...] = ()  # "repo#N" the Status step set this call


class StatusOutcome(NamedTuple):
    written: tuple[str, ...]  # "repo#N" per card the pass set, in poll order
    errors: tuple[str, ...]  # outage lines halting the step; "" never appears
    status_errors: tuple[str, ...]  # per-card failure lines, retried next pass


def _reading(item: iface.TrackerItem) -> board.Item:
    """One tracker item as a store reading. The tracker's ``agent`` and
    ``status`` labels ride along so the upsert can settle queued writes;
    the upsert never originates korapy-owned values from them. The board
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


def _adopt_after_poll(reader, schema, repo, found, *, page_after, resume, now, uncapped=False):
    """Add the tracker's missing issues to the board.

    Returns ``(added, adopt_lines, refused_lines, outage_lines)``: the
    ``repo#N`` names adopted this call, one line per failed add
    (retried next pass, never counted toward an exit), the rejection
    line when the tracker answered with one, and the outage line when
    the tracker was unreachable or rate limited. The adopt runs only
    on a complete view: the poll started at the head (no resume token)
    and reached the end, so the polled items are the whole membership
    and adding cannot duplicate. A reader without an adopt cap
    (``board_adopt_limit``) skips the adopt, unless the sweep runs
    uncapped: then the cap reads as unlimited and every missing issue
    is adopted in one run.
    """
    limit = None if uncapped else getattr(reader, "board_adopt_limit", None)
    if limit is None and not uncapped:
        return (), (), (), ()
    if resume is not None or page_after is not None:
        return (), (), (), ()
    present = {(item.repo, item.number) for item in found}
    try:
        adopted = reader.adopt_missing(schema.project_id, repo, present, limit=limit, now=now)
    except iface.TrackerRejected as err:
        return (), (), (f"{repo}: {err}",), ()
    except iface.TrackerError as err:
        return (), (), (), (f"{repo}: {err}",)
    outage: list[str] = []
    if adopted.stopped is not None:
        message, retry_after = adopted.stopped
        outage.append(
            f"{repo}: board add rate limited, retry after {retry_after:g}s"
            f" ({message}); the rest carries to the next pass"
        )
    return tuple(adopted.added), tuple(adopted.refused), (), tuple(outage)


def _status_setup(reader, schema, *, uncapped=False):
    """``(limit, write, ids, field)`` when the Status step may write, else None.

    One predicate for the step's activation: a capped reader carrying
    the Status option ids, and a schema with the Status column. Both
    the step and the pulls resolution read this, so an unmapped
    project never pays for a pulls listing it cannot use. The sweep
    runs uncapped: the cap reads as unlimited, while a missing writer,
    missing ids or missing column still read as unmapped.
    """
    limit = None if uncapped else getattr(reader, "board_status_limit", None)
    write = getattr(reader, "write_option", None)
    ids = getattr(reader, "board_status_ids", None)
    if limit is None and not uncapped:
        return None
    if write is None or not ids:
        return None
    if limit is not None and limit <= 0:
        return None
    field = schema.fields.get("board_status")
    if field is None:
        return None
    return limit, write, ids, field


def _pulls_budget(pulls_max_calls):
    """The pulls listing cap: explicit wins, else the host setting.

    An unreadable host setting falls back to the mirrored default, the
    way the drive gate falls back to its own: the gate reports the
    settings problem in the same pass, so the step stays quiet.
    """
    if pulls_max_calls is not None:
        return pulls_max_calls
    try:
        return settings.load(root=None)["drive.pulls_max_calls"]
    except settings.SettingsError:
        return DEFAULT_PULLS_MAX_CALLS


def _resolve_status_pulls(reader, repo, *, max_calls):
    """``(numbers with an open pull, "")`` for the Status step, one listing.

    Resolves the repo's open pull requests once through the tracker's
    ``pulls_index`` seam and files them by linked ticket, so the step
    spends at most ``max_calls`` listing calls per repo per pass instead
    of one read per card. A non-positive budget lists nothing and reads
    as capped. Returns ``(None, cause)`` when the listing is
    unreadable or the budget stopped it early: open cards then keep
    their Status (closed cards still derive Done, which needs no
    pulls). A rate-limit answer raises ``TrackerRateLimited``; any
    other failure raises ``TrackerError``; the caller turns both into
    lines, never a crash.
    """
    if max_calls <= 0:
        return None, f"pulls lookup limited at {max_calls} call(s)"
    try:
        index = reader.pulls_index(repo, max_calls)
    except iface.TrackerRateLimited:
        raise
    except Exception as err:
        raise iface.TrackerError(str(err) or type(err).__name__) from err
    if not index.complete:
        return None, f"pulls lookup limited at {max_calls} call(s)"
    return (
        {
            number
            for number, linked in index.by_issue.items()
            if any(getattr(pull, "kind", "pr") == "pr" for pull in linked)
        },
        "",
    )


def sync_board_status(
    store, reader, schema, repo, cards, *, now, pulls=None, pulls_error="", uncapped=False
) -> StatusOutcome:
    """Set the Status column from the derived state, up to the pass cap.

    The derived state per card is closed means done, an open linked
    pull request means in review, a live claim means claimed, otherwise
    available. Only states the board config names an option id for are
    synced; a project mapping none of them, a reader without a status
    cap, or a schema without the Status column all read as unmapped
    and change nothing. Pull-request cards are skipped like everywhere
    else: only tickets carry a Status. A card a person moved (its live
    Status differs from the recorded last set) rests until the derived
    state changes, then writes again. Writes only land when the live
    Status differs from the derived one, one write per changed card, at
    most the reader's cap per pass; the rest carries to the next pass.
    The sweep runs uncapped: every drifted card is written in one run.
    A failed card is one status line and the step moves on.

    ``pulls`` is the per-issue set of issue numbers holding an open
    linked pull, resolved once per repo by the caller through the
    tracker's ``pulls_index`` seam; ``None`` means the listing was
    unreadable or capped, with the cause in ``pulls_error``. An open
    card with unknown pulls rests with one status line naming the
    cause, while a closed card still derives Done, which needs no
    pulls.
    """
    setup = _status_setup(reader, schema, uncapped=uncapped)
    if setup is None:
        return StatusOutcome((), (), ())
    limit, write, ids, field = setup
    by_name = boardstatus.option_names(field.options)
    written: list[str] = []
    errors: list[str] = []
    status_errors: list[str] = []
    for card in cards:
        if getattr(card, "kind", "issue") != "issue":
            continue
        if card.state == "closed":
            has_open_pull = False
        elif pulls is None:
            status_errors.append(f"{card.repo}#{card.number}: {pulls_error}")
            continue
        else:
            has_open_pull = card.number in pulls
        claimed = card.state != "closed" and (store.claim_of(card.repo, card.number) is not None)
        derived = boardstatus.derive(card.state, has_open_pull, claimed)
        extra = card.extra if isinstance(card.extra, dict) else {}
        current_name = extra.get("board_status") or ""
        current = by_name.get(current_name) if current_name else ""
        record = store.status_set_of(card.repo, card.number)
        action, payload = boardstatus.decide(current, record, derived, ids)
        if action == "record":
            store.record_status_set(
                card.repo, card.number, payload.option_id, payload.derived, now=now
            )
        elif action == "write":
            if limit is not None and len(written) >= limit:
                continue
            try:
                write(card.repo, card.number, payload)
            except iface.TrackerRateLimited as err:
                errors.append(
                    f"{repo}: board status rate limited, retry after {err.retry_after:g}s"
                    f" ({err}); the rest carries to the next pass"
                )
                return StatusOutcome(tuple(written), tuple(errors), tuple(status_errors))
            except iface.TrackerError as err:
                status_errors.append(f"{card.repo}#{card.number}: {err}")
                continue
            store.record_status_set(card.repo, card.number, payload, derived, now=now)
            written.append(f"{card.repo}#{card.number}")
    return StatusOutcome(tuple(written), tuple(errors), tuple(status_errors))


def _sync_board_status(store, reader, schema, repo, cards, *, now, pulls_max_calls, uncapped=False):
    """The Status step with its pulls resolved once for the repo.

    Skips the listing when the step cannot write or no open ticket
    needs pulls, so unmapped projects and closed-only polls never
    contact the tracker. A rate-limited listing halts the step with
    the outage line; an unreadable or capped one rests the open cards
    with the cause while closed cards still derive Done. Returns the
    outcome with the resolved pulls, so the drift recording reads the
    same listing instead of resolving it twice; a halted listing
    resolves to unknown, and the recording rests the open cards too.
    """
    pulls: set[int] | None = set()
    pulls_error = ""
    if _status_setup(reader, schema, uncapped=uncapped) is not None and any(
        getattr(card, "kind", "issue") == "issue" and card.state != "closed" for card in cards
    ):
        try:
            pulls, pulls_error = _resolve_status_pulls(
                reader, repo, max_calls=_pulls_budget(pulls_max_calls)
            )
        except iface.TrackerRateLimited as err:
            return (
                StatusOutcome(
                    (),
                    (
                        f"{repo}: board status rate limited, retry after {err.retry_after:g}s"
                        f" ({err}); the rest carries to the next pass",
                    ),
                    (),
                ),
                None,
                "",
            )
        except iface.TrackerError as err:
            pulls, pulls_error = None, str(err)
    return (
        sync_board_status(
            store,
            reader,
            schema,
            repo,
            cards,
            now=now,
            pulls=pulls,
            pulls_error=pulls_error,
            uncapped=uncapped,
        ),
        pulls,
        pulls_error,
    )


def _record_board_drift(store, reader, schema, repo, found, *, pulls, pulls_error, now, complete):
    """Reconcile this poll's drift verdicts in the store.

    The one recording path: the status and missing verdicts come from
    the same ``boardsweep`` finders the sweep runs, over the same poll
    cards, pulls and store state, so the pass, the sweep, the doctor
    and the dashboard never disagree on what drifted. What counts is
    the mapping, not the caps: the sweep fixes everything mapped
    without the per-pass caps, so mapped drift is always fixable in
    one command, while an unmapped project (no Status column, no
    option ids, no writer) has no fix path and records nothing. The
    per-pass caps only pace the pass; the verdicts stay objective. A
    corrupt stored row raises ``board.StoreError``: the caller reports
    it as one store line and records nothing, the way it already
    reports rows the upsert cannot apply.
    """
    mapped = _status_setup(reader, schema, uncapped=True)
    status_active = mapped is not None
    names = boardstatus.option_names(mapped[3].options) if mapped is not None else {}
    ids = dict(mapped[2]) if mapped is not None else {}
    present = {(item.repo, item.number) for item in found}
    boardsweep.reconcile(
        store,
        repo,
        cards=boardsweep.poll_cards(found),
        open_cards=boardsweep.store_cards(store.items(repo=repo, state="open")),
        present=present,
        names=names,
        ids=ids,
        pulls=pulls,
        pulls_error=pulls_error,
        now=now,
        complete=complete,
        status_active=status_active,
        adopt_active=True,
    )


def sync_board_repo(
    store: board.Store,
    reader,
    schema,
    repo: str,
    *,
    operators,
    now: float,
    max_pages: int,
    pulls_max_calls=None,
    uncapped=False,
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

    A complete poll (started at the head, reached the end) also adopts
    the tracker repo's missing open and recently closed issues onto the
    board through the reader, up to its adopt cap; a truncated poll
    skips the adopt, since partial membership cannot prove anything
    missing. Failed adds are reported without failing the sync.

    Every poll, complete or resumed, also syncs the Status column from
    the derived state through the reader, up to its status cap: each
    card's decision is independent, so a partial view still sets the
    cards it holds. Failed writes are reported without failing the
    sync. Open pulls resolve once per repo through the reader's
    ``pulls_index`` seam, capped by ``pulls_max_calls`` (None reads
    the ``drive.pulls_max_calls`` host setting): an unreadable or
    capped listing rests the open cards with the cause while closed
    cards still derive Done, and a rate-limited one halts the step
    with an outage line. The sweep runs uncapped: both caps read as
    unlimited and every drifted card is fixed in one run.

    Every poll also reconciles the board-drift verdicts in the store
    (``adopted`` and ``status_written`` name what this call fixed):
    the doctor and the dashboard read those flags, and the sweep lists
    them. A corrupt stored row the verdict read cannot parse is one
    store line, and records nothing.
    """
    old = store.get_cursor(repo, BOARD_CURSOR_NAME)
    page_after = store.get_cursor(repo, BOARD_PAGE_NAME)
    try:
        before = iface.parse_cursor(old) if old is not None else None
        found, resume = reader.poll(schema, after=page_after, max_pages=max_pages)
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
    adopt_added, adopt_lines, adopt_refused, adopt_outage = _adopt_after_poll(
        reader,
        schema,
        repo,
        found,
        page_after=page_after,
        resume=resume,
        now=now,
        uncapped=uncapped,
    )
    status, pulls, pulls_error = _sync_board_status(
        store,
        reader,
        schema,
        repo,
        found,
        now=now,
        pulls_max_calls=pulls_max_calls,
        uncapped=uncapped,
    )
    if not store_errors:
        try:
            _record_board_drift(
                store,
                reader,
                schema,
                repo,
                found,
                pulls=pulls,
                pulls_error=pulls_error,
                now=now,
                complete=resume is None and page_after is None,
            )
        except board.StoreError as err:
            # The upserts above read clean, so this is new information
            # (a claims, status-set or drift row the verdict read could
            # not parse), worth its own line. When the upserts already
            # failed, the verdict read hits the same corruption, so it
            # is skipped instead of reporting the same row twice.
            store_errors.append(str(err))
    if resume is None:
        store.clear_cursor(repo, BOARD_PAGE_NAME)
    else:
        store.set_cursor(repo, BOARD_PAGE_NAME, resume)
    if not found:
        return SyncResult(
            0,
            old,
            tuple(adopt_outage) + tuple(status.errors),
            store_errors=tuple(store_errors),
            refused=tuple(adopt_refused),
            adopt_errors=tuple(adopt_lines),
            status_errors=tuple(status.status_errors),
            adopted=tuple(adopt_added),
            status_written=tuple(status.written),
        )
    cursor = iface.format_cursor(max(item.updated_at for item in found))
    if before is not None and before > iface.parse_cursor(cursor):
        cursor = old
    _store_cursor(store, repo, cursor, name=BOARD_CURSOR_NAME)
    return SyncResult(
        seen,
        cursor,
        tuple(adopt_outage) + tuple(status.errors),
        store_errors=tuple(store_errors),
        refused=tuple(adopt_refused),
        adopt_errors=tuple(adopt_lines),
        status_errors=tuple(status.status_errors),
        adopted=tuple(adopt_added),
        status_written=tuple(status.written),
    )


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
    # now does the outbox show which items korapy already spoke for. A
    # queued or sent agent write blocks adoption; a stored claim naming a
    # different agent is a conflict korapy wins instead. Adoptions enqueue
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
