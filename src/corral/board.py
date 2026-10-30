"""The board store: a local SQLite cache of the tracker, claims as transactions.

Everything that reads tickets reads this store instead of the tracker.
Slice 1 is the store only: no tracker adapter, no network, no ``gh``,
no CLI verb. Later slices add the tracker interface, the sync
(feed, audit, resync) and the mirror pass that drains the outbox.

Confidence: each row is ``confirmed``, ``pending`` (we wrote it and the
tracker has not shown it back yet) or ``dirty`` (a write failed).
Coordination writes go to an outbox the mirror pass pushes later.
Claims are store transactions: one ``BEGIN IMMEDIATE`` decides the race
with zero tracker calls.

Field ownership decides conflicts. People own their fields
(title, body, labels, priority, closing, their own assignments): a
tracker reading always overwrites those. corral owns the agent fields
(the claim, in-flight status, the agent): a tracker reading never
originates a value for those. It only settles queued writes: a reading
that echoes a queued value marks it ``sent`` and the row turns
``confirmed``; a fresher reading showing a different value drops the
queued write with reason ``overridden`` (a person won); a stale reading
(older than the queued write) settles nothing. A lost store rebuilds its
claims in the full resync path only: ``Store.adopt_claim`` takes the
tracker's agent into an empty row, with an unknown host and a recovery
epoch. The tracker's in-flight status is never recovered: the schema has
no place to store it.

External items are read-only. An item is external when its author is
not in ``operators`` or it comes from a fork, and an item stays
external once set. Every write to an external item is refused.

Exit codes for the callers later: ``ExternalItem`` is 1 (nothing to do),
``Claimed`` is 2 (the race was lost), ``StoreError`` is 4 (configuration
or usage error). ``README`` needs no change: no verb ships here.
"""

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass, field

from corral import paths

#: Schema version stamped as ``PRAGMA user_version``. A database with a
#: newer or unknown version fails closed instead of migrating.
SCHEMA_VERSION = 2

#: The store file inside the state dir.
DB_NAME = "board.sqlite"

CONFIRMED = "confirmed"
PENDING = "pending"
DIRTY = "dirty"

QUEUED = "queued"
SENT = "sent"
DROPPED = "dropped"

#: Corral-owned fields: the only field names ``enqueue`` accepts. A tracker
#: reading never originates a value for these; it only settles queued writes.
AGENT_FIELDS = ("agent", "status")

_BUSY_TIMEOUT_MS = 5000


class StoreError(Exception):
    """The store is unusable: missing item, newer schema, corrupt file (exit 4)."""

    exit_code = 4


class Claimed(Exception):
    """Another agent holds the claim, or the epoch does not match (exit 2)."""

    exit_code = 2


class ExternalItem(Exception):
    """The item is external: every write to it is refused (exit 1)."""

    exit_code = 1


@dataclass
class Item:
    """One cached tracker item. ``agent``/``status`` are virtual for reads:
    the claims table and the queued outbox rows are their authority. On a
    tracker reading they carry the tracker's reported values instead, the
    same way ``from_fork`` is input-only for the external rule: the upsert
    settles queued writes against them but never originates values."""

    repo: str
    number: int
    kind: str = "issue"
    title: str = ""
    body: str = ""
    state: str = "open"
    author: str = ""
    labels: list = field(default_factory=list)
    priority: str = ""
    assignees: list = field(default_factory=list)
    url: str = ""
    tracker_updated_at: float = 0.0
    fetched_at: float = 0.0
    confidence: str = CONFIRMED
    external: bool = False
    extra: dict = field(default_factory=dict)
    agent: str = ""
    status: str = ""
    from_fork: bool = False


@dataclass
class Claim:
    """The decided claim on an item. ``epoch`` is the fencing token:
    strictly higher for each new owner after a release, never reused."""

    repo: str
    number: int
    agent: str
    host: str
    epoch: int
    claimed_at: float


@dataclass
class OutboxRow:
    """One queued coordination write awaiting the mirror pass."""

    id: int
    repo: str
    number: int
    field: str
    value: str
    created_at: float
    attempts: int
    state: str
    reason: str


def is_external(author, operators, from_fork):
    """True when the item is external: its author is not an operator, or
    it comes from a fork. A missing author counts as external. Pure."""
    if from_fork:
        return True
    names = {str(name).lower() for name in operators or ()}
    return not author or str(author).lower() not in names


def db_path():
    """The store file: inside the state dir. The only path join for it."""
    return os.path.join(paths.state_dir(), DB_NAME)


#: The tracker cursor table, created by a fresh database and by the v1 to v2
#: migration alike: one definition.
_CURSORS_DDL = """
    CREATE TABLE IF NOT EXISTS cursors(
        repo TEXT NOT NULL, name TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(repo, name))
"""


def _migrate(conn):
    """Create the schema forward from an empty database, in one place."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS items(
            repo TEXT NOT NULL, number INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'issue',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open',
            author TEXT NOT NULL DEFAULT '',
            labels TEXT NOT NULL DEFAULT '[]',
            priority TEXT NOT NULL DEFAULT '',
            assignees TEXT NOT NULL DEFAULT '[]',
            url TEXT NOT NULL DEFAULT '',
            tracker_updated_at REAL NOT NULL DEFAULT 0,
            fetched_at REAL NOT NULL DEFAULT 0,
            confidence TEXT NOT NULL DEFAULT 'confirmed',
            external INTEGER NOT NULL DEFAULT 0,
            extra TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(repo, number));
        CREATE TABLE IF NOT EXISTS claims(
            repo TEXT NOT NULL, number INTEGER NOT NULL,
            agent TEXT NOT NULL, host TEXT NOT NULL,
            epoch INTEGER NOT NULL, claimed_at REAL NOT NULL,
            PRIMARY KEY(repo, number));
        CREATE TABLE IF NOT EXISTS outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL, number INTEGER NOT NULL,
            field TEXT NOT NULL, value TEXT NOT NULL,
            created_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'queued',
            reason TEXT NOT NULL DEFAULT '');
        """
    )
    conn.execute(_CURSORS_DDL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _migrate_1_to_2(conn):
    """Forward a version-1 database: add the tracker cursor table."""
    conn.execute(_CURSORS_DDL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextlib.contextmanager
def _txn(conn):
    """One atomic store transaction; rolls back instead of half-writing."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _canon(value):
    """Comparable text for one outbox value or tracker reading."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _reading_value(item, field):
    """The tracker's value for ``field`` from one reading: an attribute,
    else a project board field from ``extra``."""
    value = getattr(item, field, None)
    if value is None and isinstance(item.extra, dict):
        value = item.extra.get(field)
    return _canon(value)


def effective_priority(label_priority, extra):
    """The rank eligibility reads: the board rank wins when present,
    else the label-derived rank. Pure, and the only place that decides
    between the two sources."""
    if isinstance(extra, dict):
        board_priority = extra.get("priority")
        if board_priority:
            return board_priority
    return label_priority or ""


def merge_extra(stored, reading):
    """The stored board concepts plus one reading's carried keys.

    A reading overwrites only the concepts it carries: an absent key
    keeps the stored value, while the cleared encoding removes it (None
    clears a scalar key, an empty list clears a list key). A reading
    that is not a dict carries nothing. Pure."""
    merged = dict(stored)
    if not isinstance(reading, dict):
        return merged
    for key, value in reading.items():
        if value is None or (isinstance(value, list) and not value):
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


class Store:
    """One open board store. One connection per thread; methods commit
    per call, and claim/release decide inside a single transaction."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # -- reads ---------------------------------------------------------

    def _row(self, repo, number):
        return self._conn.execute(
            "SELECT repo, number, kind, title, body, state, author, labels,"
            " priority, assignees, url, tracker_updated_at, fetched_at,"
            " confidence, external, extra FROM items WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()

    def _holder(self, repo, number):
        row = self._conn.execute(
            "SELECT agent, host, epoch, claimed_at FROM claims WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        if row is None or not row[0]:
            return None
        return Claim(repo, number, row[0], row[1], row[2], row[3])

    def _queued_value(self, repo, number, field):
        row = self._conn.execute(
            "SELECT value FROM outbox WHERE repo=? AND number=? AND field=?"
            " AND state=? ORDER BY id ASC LIMIT 1",
            (repo, number, field, QUEUED),
        ).fetchone()
        return row[0] if row else None

    def _queued(self, repo, number):
        return self._conn.execute(
            "SELECT id, repo, number, field, value, created_at, attempts,"
            " state, reason FROM outbox WHERE repo=? AND number=?"
            " AND state=? ORDER BY id ASC",
            (repo, number, QUEUED),
        ).fetchall()

    def _get(self, repo, number):
        """The stored item with read-your-writes overlays, or None."""
        row = self._row(repo, number)
        if row is None:
            return None
        holder = self._holder(repo, number)
        queued_agent = self._queued_value(repo, number, "agent")
        queued_status = self._queued_value(repo, number, "status")
        try:
            labels = json.loads(row[7])
            assignees = json.loads(row[9])
            extra = json.loads(row[15])
        except ValueError as err:
            raise StoreError(f"unusable item {repo}#{number}: corrupt JSON: {err}")
        if not isinstance(labels, list) or not isinstance(assignees, list):
            raise StoreError(f"unusable item {repo}#{number}: labels and assignees must be lists")
        if not isinstance(extra, dict):
            raise StoreError(f"unusable item {repo}#{number}: extra must be an object")
        return Item(
            repo=row[0],
            number=row[1],
            kind=row[2],
            title=row[3],
            body=row[4],
            state=row[5],
            author=row[6],
            labels=labels,
            priority=row[8],
            assignees=assignees,
            url=row[10],
            tracker_updated_at=row[11],
            fetched_at=row[12],
            confidence=row[13],
            external=bool(row[14]),
            extra=extra,
            agent=holder.agent if holder else (queued_agent or ""),
            status=queued_status or "",
        )

    def item(self, repo, number, *, now, max_age=None):
        """The cached item and whether it is fresh. Fresh is false for a
        missing item, and when ``max_age`` is given and the row is older
        than it."""
        found = self._get(repo, number)
        if found is None:
            return None, False
        if max_age is not None and now - found.fetched_at > max_age:
            return found, False
        return found, True

    def items(self, repo=None, state=None, label=None):
        """Cached items, oldest first, filtered by repo, state and label."""
        query = "SELECT repo, number FROM items ORDER BY fetched_at ASC, repo ASC, number ASC"
        args: list = []
        clauses = []
        if repo is not None:
            clauses.append("repo=?")
            args.append(repo)
        if state is not None:
            clauses.append("state=?")
            args.append(state)
        if clauses:
            query = (
                "SELECT repo, number FROM items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY fetched_at ASC, repo ASC, number ASC"
            )
        out = []
        for key_repo, key_number in self._conn.execute(query, args).fetchall():
            found = self._get(key_repo, key_number)
            if found is None:
                continue
            if label is not None and label not in (found.labels or []):
                continue
            out.append(found)
        return out

    def claim_of(self, repo, number):
        """The decided claim holding (``repo``, ``number``), or None.

        Read-only: the agent, host, epoch and claimed time straight from
        the claim row, so a pass can release with the stored epoch.
        """
        return self._holder(repo, number)

    def get_cursor(self, repo, name):
        """The stored tracker cursor for ``repo`` and ``name``, or None."""
        row = self._conn.execute(
            "SELECT value FROM cursors WHERE repo=? AND name=?", (repo, name)
        ).fetchone()
        return row[0] if row else None

    def set_cursor(self, repo, name, value):
        """Store the tracker cursor for ``repo`` and ``name``."""
        self._conn.execute(
            "INSERT INTO cursors(repo, name, value) VALUES(?, ?, ?)"
            " ON CONFLICT(repo, name) DO UPDATE SET value=excluded.value",
            (repo, name, value),
        )

    def clear_cursor(self, repo, name):
        """Drop the stored cursor for ``repo`` and ``name``, if any."""
        self._conn.execute("DELETE FROM cursors WHERE repo=? AND name=?", (repo, name))

    def queued_writes(self, limit=None):
        """Queued outbox rows, oldest first, at most ``limit``."""
        query = (
            "SELECT id, repo, number, field, value, created_at, attempts,"
            " state, reason FROM outbox WHERE state=? ORDER BY id ASC"
        )
        args: list = [QUEUED]
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        return [OutboxRow(*row) for row in self._conn.execute(query, args).fetchall()]

    def outbox_rows(self, state=None):
        """Outbox rows for inspection, oldest first, optionally by state."""
        query = (
            "SELECT id, repo, number, field, value, created_at, attempts, state, reason FROM outbox"
        )
        args: list = []
        if state is not None:
            query += " WHERE state=?"
            args.append(state)
        query += " ORDER BY id ASC"
        return [OutboxRow(*row) for row in self._conn.execute(query, args).fetchall()]

    # -- writes --------------------------------------------------------

    def _set_confidence(self, repo, number):
        """Pending while a write is queued, dirty while a write failed,
        confirmed once the queue drains."""
        remaining = self._queued(repo, number)
        current = self._conn.execute(
            "SELECT confidence FROM items WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        if not remaining:
            confidence = CONFIRMED
        elif current and current[0] == DIRTY:
            confidence = DIRTY
        else:
            confidence = PENDING
        self._conn.execute(
            "UPDATE items SET confidence=? WHERE repo=? AND number=?",
            (confidence, repo, number),
        )

    def upsert_item(self, item, *, operators, now):
        """Apply a tracker reading. People-owned fields always take the
        tracker's value, while board concepts in ``extra`` merge by key:
        a reading overwrites only the concepts it carries, and only the
        cleared encoding (None for a scalar, an empty list for a list)
        removes one. Corral-owned fields are never originated here:
        the reading only settles queued writes. A fresh tracker value
        echoing a queued write marks it sent, a fresh different value
        drops it as overridden, and a reading that says nothing (an empty
        value, e.g. a title-only edit that left no label behind) settles
        nothing. A stale reading settles nothing either and keeps the
        stored concepts."""
        with _txn(self._conn):
            stored = self._row(item.repo, item.number)
            external = bool(
                (stored and stored[14]) or is_external(item.author, operators, item.from_fork)
            )
            try:
                labels = json.dumps(list(item.labels or []))
                assignees = json.dumps(list(item.assignees or []))
            except (TypeError, ValueError) as err:
                raise StoreError(f"unusable item {item.repo}#{item.number}: {err}")
            try:
                base = json.loads(stored[15]) if stored is not None else {}
            except ValueError as err:
                raise StoreError(f"unusable item {item.repo}#{item.number}: corrupt extra: {err}")
            if not isinstance(base, dict):
                raise StoreError(
                    f"unusable item {item.repo}#{item.number}: extra must be an object"
                )
            try:
                extra = json.dumps(merge_extra(base, item.extra))
            except (TypeError, ValueError) as err:
                raise StoreError(f"unusable item {item.repo}#{item.number}: {err}")
            if stored is None:
                self._conn.execute(
                    "INSERT INTO items(repo, number, kind, title, body, state,"
                    " author, labels, priority, assignees, url,"
                    " tracker_updated_at, fetched_at, confidence, external, extra)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.repo,
                        item.number,
                        item.kind,
                        item.title,
                        item.body,
                        item.state,
                        item.author,
                        labels,
                        item.priority,
                        assignees,
                        item.url,
                        item.tracker_updated_at,
                        now,
                        CONFIRMED,
                        int(external),
                        extra,
                    ),
                )
            else:
                if item.tracker_updated_at >= stored[11]:
                    self._conn.execute(
                        "UPDATE items SET kind=?, title=?, body=?, state=?,"
                        " author=?, labels=?, priority=?, assignees=?, url=?,"
                        " tracker_updated_at=?, fetched_at=?, external=?, extra=?"
                        " WHERE repo=? AND number=?",
                        (
                            item.kind,
                            item.title,
                            item.body,
                            item.state,
                            item.author,
                            labels,
                            item.priority,
                            assignees,
                            item.url,
                            item.tracker_updated_at,
                            now,
                            int(external),
                            extra,
                            item.repo,
                            item.number,
                        ),
                    )
                else:
                    self._conn.execute(
                        "UPDATE items SET fetched_at=?, external=? WHERE repo=? AND number=?",
                        (now, int(external), item.repo, item.number),
                    )
            for row in self._queued(item.repo, item.number):
                _id, _, _, queued_field, queued_value, created_at, _, _, _ = row
                if item.tracker_updated_at <= created_at:
                    continue
                seen = _reading_value(item, queued_field)
                if seen == queued_value:
                    self._conn.execute("UPDATE outbox SET state=? WHERE id=?", (SENT, _id))
                elif seen == "":
                    continue
                else:
                    self._conn.execute(
                        "UPDATE outbox SET state=?, reason=? WHERE id=?",
                        (DROPPED, "overridden", _id),
                    )
            self._set_confidence(item.repo, item.number)

    def mark_absent(self, repo, number, *, now):
        """Flag a store row the tracker's full listing no longer holds.

        The row stays: the item was deleted or transferred, and only the
        tracker knows which. Confidence turns dirty with no outbox reason
        behind it; ``fetched_at`` moves to now, since absence was just
        verified. Unknown rows are a caller bug."""
        with _txn(self._conn):
            if self._row(repo, number) is None:
                raise StoreError(f"unknown item {repo}#{number}")
            self._conn.execute(
                "UPDATE items SET confidence=?, fetched_at=? WHERE repo=? AND number=?",
                (DIRTY, now, repo, number),
            )

    def _require_writable(self, repo, number):
        row = self._row(repo, number)
        if row is None:
            raise StoreError(f"unknown item {repo}#{number}")
        if row[5] != "open":
            raise StoreError(f"item {repo}#{number} is not open")
        if row[14]:
            raise ExternalItem(f"item {repo}#{number} is external: read-only")
        return row

    def claim(self, repo, number, agent, host, *, now):
        """Decide one claim in a single transaction, zero tracker calls.
        The item must exist, be open and not external. A second claimant
        gets ``Claimed`` naming the holder; the same agent claiming again
        is idempotent. Each new owner after a release gets a strictly
        higher epoch."""
        with _txn(self._conn):
            self._require_writable(repo, number)
            row = self._conn.execute(
                "SELECT agent, host, epoch, claimed_at FROM claims WHERE repo=? AND number=?",
                (repo, number),
            ).fetchone()
            if row is not None and row[0] and row[0] != agent:
                raise Claimed(f"item {repo}#{number} is claimed by {row[0]}")
            if row is not None and row[0] == agent:
                return Claim(repo, number, row[0], row[1], row[2], row[3])
            epoch = (row[2] if row is not None else 0) + 1
            self._conn.execute(
                "INSERT INTO claims(repo, number, agent, host, epoch, claimed_at)"
                " VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(repo, number) DO UPDATE SET agent=excluded.agent,"
                " host=excluded.host, epoch=excluded.epoch,"
                " claimed_at=excluded.claimed_at",
                (repo, number, agent, host, epoch, now),
            )
            self._conn.execute(
                "INSERT INTO outbox(repo, number, field, value, created_at,"
                " attempts, state, reason) VALUES(?, ?, ?, ?, ?, 0, ?, '')",
                (repo, number, "agent", _canon(agent), now, QUEUED),
            )
            self._conn.execute(
                "UPDATE items SET confidence=? WHERE repo=? AND number=?",
                (PENDING, repo, number),
            )
            return Claim(repo, number, agent, host, epoch, now)

    def release(self, repo, number, agent, epoch, *, now):
        """Release a held claim. Only the holder with the matching epoch
        can release; anyone else gets ``Claimed``. The claim row is
        always cleared so claims never strand; on a closed or external
        item nothing is enqueued and the item is not marked pending."""
        with _txn(self._conn):
            row = self._row(repo, number)
            if row is None:
                raise StoreError(f"unknown item {repo}#{number}")
            held = self._conn.execute(
                "SELECT agent, host, epoch, claimed_at FROM claims WHERE repo=? AND number=?",
                (repo, number),
            ).fetchone()
            if held is None or not held[0] or held[0] != agent or held[2] != epoch:
                holder = held[0] if held and held[0] else "nobody"
                raise Claimed(f"item {repo}#{number} is held by {holder}")
            self._conn.execute(
                "UPDATE claims SET agent='', host='', claimed_at=? WHERE repo=? AND number=?",
                (now, repo, number),
            )
            if row[5] != "open" or row[14]:
                self._set_confidence(repo, number)
                return
            self._conn.execute(
                "INSERT INTO outbox(repo, number, field, value, created_at,"
                " attempts, state, reason) VALUES(?, ?, ?, ?, ?, 0, ?, '')",
                (repo, number, "agent", "", now, QUEUED),
            )
            self._conn.execute(
                "UPDATE items SET confidence=? WHERE repo=? AND number=?",
                (PENDING, repo, number),
            )

    def adopt_claim(self, repo, number, agent, *, epoch, now):
        """Adopt the tracker's claim into a row with no local claim state.
        One transaction: the insert happens only when no claim row exists
        and no agent write is queued or sent for the item. The host is
        unknown after a lost store, so it stays empty; the caller passes a
        recovery epoch (``int(now)``), strictly higher than any
        counter-based epoch a previous store handed out, so a stale
        holder's release is refused. Nothing is enqueued: the tracker
        already shows the claim, and confidence stays as the upsert left
        it. Returns False, never raises, when a claim row exists, the
        item is unknown, external or closed, or an agent write is already
        queued or sent."""
        with _txn(self._conn):
            row = self._row(repo, number)
            if row is None or row[5] != "open" or row[14] or not agent:
                return False
            held = self._conn.execute(
                "SELECT agent FROM claims WHERE repo=? AND number=?",
                (repo, number),
            ).fetchone()
            if held is not None:
                return False
            busy = self._conn.execute(
                "SELECT 1 FROM outbox WHERE repo=? AND number=? AND field='agent'"
                " AND state IN (?, ?) LIMIT 1",
                (repo, number, QUEUED, SENT),
            ).fetchone()
            if busy is not None:
                return False
            self._conn.execute(
                "INSERT INTO claims(repo, number, agent, host, epoch, claimed_at)"
                " VALUES(?, ?, ?, '', ?, ?)",
                (repo, number, agent, epoch, now),
            )
            return True

    def enqueue(self, repo, number, field, value, *, now):
        """Queue one coordination write and mark the item pending. Only
        corral-owned fields (``AGENT_FIELDS``) are queued; anything else
        is a caller bug. Refused for external items."""
        with _txn(self._conn):
            if field not in AGENT_FIELDS:
                raise StoreError(f"unknown coordination field {field!r}")
            self._require_writable(repo, number)
            cur = self._conn.execute(
                "INSERT INTO outbox(repo, number, field, value, created_at,"
                " attempts, state, reason) VALUES(?, ?, ?, ?, ?, 0, ?, '')",
                (repo, number, field, _canon(value), now, QUEUED),
            )
            self._conn.execute(
                "UPDATE items SET confidence=? WHERE repo=? AND number=?",
                (PENDING, repo, number),
            )
            return OutboxRow(
                cur.lastrowid,
                repo,
                number,
                field,
                _canon(value),
                now,
                0,
                QUEUED,
                "",
            )

    def mark_sent(self, _id):
        """The mirror pass pushed the write: settle it and refresh confidence."""
        with _txn(self._conn):
            row = self._conn.execute(
                "SELECT repo, number FROM outbox WHERE id=?", (_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown outbox row {_id}")
            self._conn.execute("UPDATE outbox SET state=? WHERE id=?", (SENT, _id))
            self._set_confidence(row[0], row[1])

    def mark_dropped(self, _id, reason):
        """Drop a queued write and refresh the item's confidence."""
        with _txn(self._conn):
            row = self._conn.execute(
                "SELECT repo, number FROM outbox WHERE id=?", (_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown outbox row {_id}")
            self._conn.execute(
                "UPDATE outbox SET state=?, reason=? WHERE id=?",
                (DROPPED, reason, _id),
            )
            self._set_confidence(row[0], row[1])

    def mark_dirty(self, _id, reason):
        """A push failed: count the attempt and flag the item dirty. The
        write stays queued for the next mirror pass."""
        with _txn(self._conn):
            row = self._conn.execute(
                "SELECT repo, number FROM outbox WHERE id=?", (_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown outbox row {_id}")
            self._conn.execute(
                "UPDATE outbox SET attempts=attempts+1, reason=? WHERE id=?",
                (reason, _id),
            )
            self._conn.execute(
                "UPDATE items SET confidence=? WHERE repo=? AND number=?",
                (DIRTY, row[0], row[1]),
            )


def open_store(path=None):
    """Open the board store, creating it owner-only when missing, and
    tightening a pre-existing file to owner-only on every open. A newer
    or unknown schema version, or a corrupt file, fails closed with
    ``StoreError`` instead of being recreated."""
    target = path if path is not None else db_path()
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(target):
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(target, 0o600)
    try:
        conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as err:
        raise StoreError(f"cannot open board store {target}: {err}")
    conn.isolation_level = None
    store = Store(conn)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as err:
            raise StoreError(f"board store {target} is not a database: {err}")
        if version == 0:
            with _txn(conn):
                _migrate(conn)
        elif version == 1:
            with _txn(conn):
                _migrate_1_to_2(conn)
        elif version != SCHEMA_VERSION:
            raise StoreError(
                f"board store {target} has schema version {version},"
                f" supported {SCHEMA_VERSION}: failing closed"
            )
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if not mode or str(mode[0]).lower() != "wal":
            raise StoreError(
                f"board store {target}: WAL mode is not available here: failing closed"
            )
    except Exception:
        store.close()
        raise
    return store
