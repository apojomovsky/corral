"""The hub: one SQLite database behind a small loopback HTTP service.

Tickets H1, H2 and H5 of docs/08-hub-multihost.md section 10: the hub
skeleton, the host registry, the claims table, the lease cycle (renew
and release with fencing epochs), and the heartbeat carrying lease
renewals beside the snapshot document with the merged dashboard
(`GET /v1/hosts`, `GET /v1/snapshot?project=`). Later tickets add
drive composition (H3), operator verbs (H4) and hub operations (H6).

Every grant is decided by one SQLite transaction: the row is
unclaimed, or its lease has lapsed, and the new owner takes a strictly
higher fencing epoch, or the holder names the conflict. The server is
`http.server.ThreadingHTTPServer` from the standard library, loopback
by default, with one bearer token per host. Tokens are minted by
`add_host`, printed once, and stored only as salted hashes.
"""

import contextlib
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from corral import paths, settings

#: API major served at `/v1/*`. Hosts compare it on every call; a
#: breaking change is a new path major with a cutover.
HUB_API = 1
#: Schema version stamped as ``PRAGMA user_version``. A database with
#: a newer or unknown version fails closed instead of migrating.
#: Version 2 adds the snapshots and projects tables for the merged
#: dashboard; a version 1 store migrates forward, nothing older exists.
SCHEMA_VERSION = 2

#: Heartbeat every 30 seconds, lease three heartbeats: the decided D1
#: defaults from docs/08-hub-multihost.md section 12.
DEFAULT_HEARTBEAT_S = 30
DEFAULT_LEASE_TTL_S = 90
#: A host with no contact inside three heartbeats reads stale.
DEFAULT_STALE_AFTER_S = 3 * DEFAULT_HEARTBEAT_S

settings.register(
    "hub.url",
    str,
    "",
    "address of the hub, for example http://host-1:8000 (empty means single-host)",
)
settings.register("hub.heartbeat_s", int, DEFAULT_HEARTBEAT_S, "seconds between hub heartbeats")
settings.register(
    "hub.lease_ttl_s", int, DEFAULT_LEASE_TTL_S, "seconds a hub claim grant stays valid"
)
settings.register(
    "hub.stale_after_s",
    int,
    DEFAULT_STALE_AFTER_S,
    "seconds after the last heartbeat a host reads stale on the merged dashboard",
)

_BUSY_TIMEOUT_MS = 5000
_MAX_BODY = 65536

#: Host ids are operator-assigned, never raw hostnames: one path part,
#: no spaces, no slashes.
HOST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

log = logging.getLogger("corral.hub")


class HubError(Exception):
    """The hub store or request is unusable (exit 4)."""

    exit_code = 4


class HubUnreachable(Exception):
    """The hub cannot be reached: no new claims until it is back (exit 3)."""

    exit_code = 3


class UnknownProject(Exception):
    """The dashboard filter names no project the hub has seen."""

    def __init__(self, project: str):
        super().__init__(f"unknown project {project!r}")
        self.project = project


class Conflict(Exception):
    """Another holder owns the ticket. The message names the holder."""

    def __init__(self, repo: str, number: int, agent: str, host_id: str, epoch: int):
        self.holder = {"agent": agent, "host_id": host_id, "epoch": epoch}
        super().__init__(f"ticket {repo}#{number} is held by {agent} on {host_id}")


@dataclass(frozen=True)
class Receipt:
    """A decided grant: the holder plus the fencing epoch and the lease."""

    repo: str
    number: int
    agent: str
    host_id: str
    epoch: int
    lease_expires: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "number": self.number,
            "agent": self.agent,
            "host_id": self.host_id,
            "epoch": self.epoch,
            "lease_expires": self.lease_expires,
        }


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS hosts(
    host_id TEXT PRIMARY KEY,
    token_salt TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    capacity TEXT NOT NULL DEFAULT '{}',
    constraints TEXT NOT NULL DEFAULT '{}',
    last_seen REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claims(
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    host_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    lease_expires REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(repo, number)
);
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES('membership_epoch', 0);
"""

#: The version 2 tables: the last pushed host document per host, and
#: the project names each host's document carries. The hub never
#: decides from snapshots; they are data for the merged dashboard.
_SCHEMA_V2_TABLES = """
CREATE TABLE IF NOT EXISTS snapshots(
    host_id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    host_now REAL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
    name TEXT NOT NULL,
    host_id TEXT NOT NULL,
    PRIMARY KEY(name, host_id)
);
"""

_SCHEMA = _SCHEMA_V1 + _SCHEMA_V2_TABLES


@contextlib.contextmanager
def _txn(conn: sqlite3.Connection):
    """One decision, one transaction: `BEGIN IMMEDIATE` decides the race."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def open_db(path: str | None = None) -> sqlite3.Connection:
    """Open the hub database, creating it owner-only when missing.

    A newer or unknown schema version fails closed with ``HubError``
    instead of being migrated. WAL mode keeps concurrent claim
    requests serializable through one transaction each.
    """
    target = path if path is not None else paths.hub_db_path()
    parent = os.path.dirname(target)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise HubError(f"cannot create hub state dir {parent}: {err}") from None
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    try:
        os.chmod(target, 0o600)
    except OSError as err:
        raise HubError(f"cannot secure hub store {target}: {err}") from None
    try:
        conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as err:
        raise HubError(f"cannot open hub store {target}: {err}") from None
    conn.isolation_level = None
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as err:
            raise HubError(f"hub store {target} is not a database: {err}") from None
        if version == 0:
            # No explicit transaction: executescript commits implicitly.
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif version == 1:
            conn.executescript(_SCHEMA_V2_TABLES)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise HubError(
                f"hub store {target} has schema version {version},"
                f" supported {SCHEMA_VERSION}: failing closed"
            )
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if not mode or str(mode[0]).lower() != "wal":
            raise HubError(f"hub store {target}: WAL mode is not available here: failing closed")
    except Exception:
        conn.close()
        raise
    return conn


def _hash(salt: str, token: str) -> str:
    return hashlib.sha256((salt + token).encode("utf-8")).hexdigest()


def add_host(conn: sqlite3.Connection, host_id: str) -> str:
    """Mint a host id and its bearer token, storing only a salted hash.

    Prints nothing and returns the token: the caller shows it once.
    A duplicate id or a bad shape is ``HubError`` (exit 4).
    """
    name = str(host_id or "")
    if not HOST_ID_RE.fullmatch(name):
        raise HubError(f"hub host id {host_id!r} must match {HOST_ID_RE.pattern}")
    token = secrets.token_hex(32)
    salt = secrets.token_hex(16)
    try:
        with _txn(conn):
            conn.execute(
                "INSERT INTO hosts(host_id, token_salt, token_hash) VALUES(?, ?, ?)",
                (name, salt, _hash(salt, token)),
            )
    except sqlite3.IntegrityError:
        raise HubError(f"hub host {name!r} is already registered") from None
    return token


def verify_token(conn: sqlite3.Connection, presented: str | None) -> str | None:
    """The host id for a presented token, or None. Constant-time compare."""
    if not presented or not isinstance(presented, str):
        return None
    rows = conn.execute("SELECT host_id, token_salt, token_hash FROM hosts").fetchall()
    for host_id, salt, digest in rows:
        if hmac.compare_digest(_hash(salt, presented), digest):
            return str(host_id)
    return None


def _check_identity(repo: object, number: object, agent: object) -> None:
    if not isinstance(repo, str) or not repo:
        raise HubError(f"claim repo {repo!r} must be a non-empty string")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise HubError(f"claim number {number!r} must be a positive integer")
    if not isinstance(agent, str) or not agent:
        raise HubError(f"claim agent {agent!r} must be a non-empty string")


def _check_shape(repo: object, number: object, agent: object, host_id: object) -> None:
    _check_identity(repo, number, agent)
    if not isinstance(host_id, str) or not host_id:
        raise HubError(f"claim host_id {host_id!r} must be a non-empty string")


def _check_epoch(epoch: object) -> None:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise HubError(f"claim epoch {epoch!r} must be a positive integer")


def grant(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    agent: str,
    host_id: str,
    *,
    now: float,
    lease_ttl_s: int,
) -> Receipt:
    """Decide one claim in a single transaction.

    Unclaimed, or lease lapsed past ``lease_expires``, grants with a
    strictly higher epoch: ``kept epoch + 1``, so a re-grant after a
    partition fences in-flight duplicates even for the same agent. A
    live lease held by someone else raises ``Conflict`` naming the
    holder; the same agent re-proposing under a live lease is
    idempotent and keeps its epoch and lease.
    """
    _check_shape(repo, number, agent, host_id)
    if lease_ttl_s <= 0:
        raise HubError(f"lease_ttl_s {lease_ttl_s!r} must be positive")
    with _txn(conn):
        row = conn.execute(
            "SELECT agent, host_id, epoch, lease_expires FROM claims WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        if row is not None and row[0] and row[3] > now:
            if row[0] == agent and row[1] == host_id:
                return Receipt(repo, number, row[0], row[1], row[2], row[3])
            raise Conflict(repo, number, row[0], row[1], row[2])
        epoch = (row[2] if row is not None else 0) + 1
        expires = now + lease_ttl_s
        conn.execute(
            "INSERT INTO claims(repo, number, host_id, agent, epoch,"
            " lease_expires, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(repo, number) DO UPDATE SET host_id=excluded.host_id,"
            " agent=excluded.agent, epoch=excluded.epoch,"
            " lease_expires=excluded.lease_expires, updated_at=excluded.updated_at",
            (repo, number, host_id, agent, epoch, expires, now),
        )
        return Receipt(repo, number, agent, host_id, epoch, expires)


def renew(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    agent: str,
    epoch: int,
    *,
    now: float,
    lease_ttl_s: int,
) -> Receipt:
    """Extend a live lease without changing the fencing epoch.

    Only the holder with the matching agent plus epoch renews, and
    only before the lease lapses: anything else raises ``Conflict``
    naming the current holder (empty when no live holder exists), so
    a partitioned holder re-grants with a bumped epoch instead of
    reviving a stale one. A renew can never fence, because the epoch
    never changes here. One transaction, like the grant.
    """
    _check_identity(repo, number, agent)
    _check_epoch(epoch)
    if lease_ttl_s <= 0:
        raise HubError(f"lease_ttl_s {lease_ttl_s!r} must be positive")
    with _txn(conn):
        row = conn.execute(
            "SELECT agent, host_id, epoch, lease_expires FROM claims WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        if row is None or not row[0]:
            raise Conflict(repo, number, "", "", 0)
        if row[0] != agent or row[2] != epoch:
            raise Conflict(repo, number, row[0], row[1], row[2])
        if row[3] <= now:
            raise Conflict(repo, number, row[0], row[1], row[2])
        expires = now + lease_ttl_s
        conn.execute(
            "UPDATE claims SET lease_expires=?, updated_at=? WHERE repo=? AND number=?",
            (expires, now, repo, number),
        )
        return Receipt(repo, number, row[0], row[1], row[2], expires)


def release(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    agent: str,
    epoch: int,
    *,
    now: float,
) -> int:
    """Release a held claim, keeping the epoch for the next grant.

    Only the holder with the matching agent plus epoch releases;
    anyone else gets ``Conflict`` naming the holder. The row is
    cleared but the epoch is kept, so the next grant mints
    ``epoch+1`` and fences a stale holder still acting on the old
    epoch. Returns the kept epoch. One transaction, like the grant.
    """
    _check_identity(repo, number, agent)
    _check_epoch(epoch)
    with _txn(conn):
        row = conn.execute(
            "SELECT agent, host_id, epoch FROM claims WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        if row is None or not row[0]:
            raise Conflict(repo, number, "", "", 0)
        if row[0] != agent or row[2] != epoch:
            raise Conflict(repo, number, row[0], row[1], row[2])
        kept = row[2]
        conn.execute(
            "UPDATE claims SET agent='', host_id='', lease_expires=0, updated_at=?"
            " WHERE repo=? AND number=?",
            (now, repo, number),
        )
        return kept


def membership_epoch(conn: sqlite3.Connection) -> int:
    """The hub's membership counter, bumped by every host removal."""
    row = conn.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
    return int(row[0]) if row is not None else 0


def _check_mapping(name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HubError(f"hub {name} must be an object, got {type(value).__name__}")
    return dict(value)


def _check_address(address: object) -> str:
    if not isinstance(address, str):
        raise HubError(f"hub address {address!r} must be a string")
    return address


def register_host(
    conn: sqlite3.Connection,
    host_id: str,
    *,
    now: float,
    address: str = "",
    capacity: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
) -> int:
    """Record a host's check-in: address, capacity, constraints, last sighting.

    The host row is minted by ``add_host``; checking in under an
    unknown id is ``HubError`` (exit 4), never an implicit mint.
    Returns the current membership epoch so the host learns it on
    every contact. One transaction.
    """
    name = _check_address(host_id) if isinstance(host_id, str) else host_id
    if not isinstance(name, str) or not name:
        raise HubError(f"hub host_id {host_id!r} must be a non-empty string")
    address = _check_address(address)
    stored_capacity = _check_mapping("capacity", capacity if capacity is not None else {})
    stored_constraints = _check_mapping(
        "constraints", constraints if constraints is not None else {}
    )
    with _txn(conn):
        row = conn.execute("SELECT host_id FROM hosts WHERE host_id=?", (name,)).fetchone()
        if row is None:
            raise HubError(f"hub host {name!r} is not registered: add it first")
        conn.execute(
            "UPDATE hosts SET address=?, capacity=?, constraints=?, last_seen=? WHERE host_id=?",
            (
                address,
                json.dumps(stored_capacity, sort_keys=True),
                json.dumps(stored_constraints, sort_keys=True),
                now,
                name,
            ),
        )
        return membership_epoch(conn)


def _project_names(document: dict[str, Any]) -> set[str]:
    """The project names one host document carries.

    Agent rows name their project and the capacity section counts
    tickets by project; both feed the hub's projects table, which is
    the union the filter values come from. Anything misshapen reads
    as no names, never an error: snapshots are data, not decisions.
    """
    names: set[str] = set()
    agents = document.get("agents")
    if isinstance(agents, list):
        for row in agents:
            if isinstance(row, dict):
                project = row.get("project")
                if isinstance(project, str) and project:
                    names.add(project)
    capacity = document.get("capacity")
    if isinstance(capacity, dict):
        by_project = capacity.get("by_project")
        if isinstance(by_project, dict):
            for project in by_project:
                if isinstance(project, str) and project:
                    names.add(project)
    return names


def store_snapshot(
    conn: sqlite3.Connection,
    host_id: str,
    document: dict[str, Any],
    *,
    now: float,
) -> None:
    """Keep a host's last pushed document for the merged dashboard.

    The document is stored semantically identical, keys re-sorted:
    the hub never reorders merged rows by time and never compares
    timestamps across hosts. ``host_now`` is the serving host's
    clock from the document's fleet header when it is a number, else
    None; ``received_at`` is the hub clock. The host's project rows
    are refreshed from the document, so the filter table is always
    the union of the last push per host. One transaction.
    """
    if not isinstance(document, dict):
        raise HubError(f"hub snapshot must be an object, got {type(document).__name__}")
    host_now = document.get("fleet_header")
    if isinstance(host_now, dict):
        host_now = host_now.get("now")
    if isinstance(host_now, bool) or not isinstance(host_now, (int, float)):
        host_now = None
    raw = json.dumps(document, sort_keys=True)
    names = _project_names(document)
    with _txn(conn):
        row = conn.execute("SELECT host_id FROM hosts WHERE host_id=?", (host_id,)).fetchone()
        if row is None:
            raise HubError(f"hub host {host_id!r} is not registered: add it first")
        conn.execute(
            "INSERT INTO snapshots(host_id, document, host_now, received_at)"
            " VALUES(?, ?, ?, ?)"
            " ON CONFLICT(host_id) DO UPDATE SET document=excluded.document,"
            " host_now=excluded.host_now, received_at=excluded.received_at",
            (host_id, raw, host_now, now),
        )
        conn.execute("DELETE FROM projects WHERE host_id=?", (host_id,))
        conn.executemany(
            "INSERT INTO projects(name, host_id) VALUES(?, ?)",
            [(name, host_id) for name in sorted(names)],
        )


def _parse_mapping(raw: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_hosts(conn: sqlite3.Connection, *, now: float, stale_after_s: int) -> list[dict[str, Any]]:
    """One row per registered host: identity, load, and reachability.

    Every registered host stays listed, including silent ones: a host
    that stops pushing keeps its last row visibly marked unreachable
    instead of dropping silently. ``reachable`` is derived from the
    hub clock only, never by comparing timestamps across hosts.
    """
    rows = conn.execute(
        "SELECT host_id, address, capacity, constraints, last_seen FROM hosts ORDER BY host_id"
    ).fetchall()
    return [
        {
            "host_id": host_id,
            "address": address,
            "capacity": _parse_mapping(capacity),
            "constraints": _parse_mapping(constraints),
            "last_seen": last_seen,
            "reachable": (now - last_seen) <= stale_after_s,
        }
        for host_id, address, capacity, constraints, last_seen in rows
    ]


def known_projects(conn: sqlite3.Connection) -> list[str]:
    """The project names the hub has seen, sorted: the filter values."""
    return [
        row[0]
        for row in conn.execute("SELECT DISTINCT name FROM projects ORDER BY name").fetchall()
    ]


def _slice_project(document: dict[str, Any], project: str) -> dict[str, Any]:
    """One host document filtered to a project: agents only.

    Quota, capacity, disk and drive stay host-wide: they describe the
    host, and slicing them by project would invent per-project
    numbers the host never reported. The agents list is the only
    per-project section the document carries. A host with no agents
    for the named project still appears with an empty agents list: a
    host is visible to the dashboard even when idle.
    """
    agents = document.get("agents")
    if not isinstance(agents, list):
        return document
    sliced = dict(document)
    sliced["agents"] = [
        row for row in agents if isinstance(row, dict) and row.get("project") == project
    ]
    return sliced


def read_snapshot(
    conn: sqlite3.Connection,
    *,
    project: str = "all",
    now: float,
    stale_after_s: int,
) -> dict[str, Any]:
    """The merged dashboard: every host's last document plus staleness.

    Each section keeps its host's clocks untouched; the hub adds only
    ``received_at`` (hub clock) and a ``stale`` flag per host when the
    last push is older than ``stale_after_s``. A named project filters
    agents per host; an unknown name raises ``UnknownProject`` (the
    route answers 404) instead of merging an empty view that hides a
    typo.
    """
    if project != "all" and project not in known_projects(conn):
        raise UnknownProject(project)
    hosts = []
    rows = conn.execute(
        "SELECT host_id, document, received_at FROM snapshots ORDER BY host_id"
    ).fetchall()
    for host_id, raw, received_at in rows:
        document = json.loads(raw)
        if project != "all":
            document = _slice_project(document, project)
        hosts.append(
            {
                "host_id": host_id,
                "received_at": received_at,
                "stale": (now - received_at) > stale_after_s,
                "snapshot": document,
            }
        )
    return {"project": project, "now": now, "hosts": hosts}


def renewal_entry(repo: str, number: int, agent: str, epoch: int) -> dict[str, Any]:
    """One entry of the heartbeat ``renewals`` list.

    The heartbeat carries one entry per held claim beside the
    snapshot document, so one call renews every lease; the reply
    answers each entry with granted or ``409``. The entry shape is
    fixed here so the heartbeat client and the hub read one fact in
    one place.
    """
    _check_identity(repo, number, agent)
    _check_epoch(epoch)
    return {"repo": repo, "number": number, "agent": agent, "epoch": epoch}


def bind_warning(bind: str) -> str | None:
    """Why a hub bind address is unsafe to serve on, or None when quiet.

    Loopback and private addresses stay quiet. A wildcard, a
    non-loopback multicast address, or a public address warns, and so
    does a name that is not a literal IP: without resolving it (no DNS
    from this check) privacy cannot be proved. Warning never refuses:
    the operator knows best, and serving continues.
    """
    host = str(bind or "")
    if host == "localhost":
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return (
            f"hub bind {host!r} is not a literal IP address: cannot prove it is"
            " private, serving plain HTTP anyway"
        )
    if address.is_loopback:
        return None
    if address.is_unspecified:
        return (
            f"hub bind {host} listens on every interface: the hub speaks plain HTTP,"
            " serve it only over a private network"
        )
    if address.is_multicast or address.is_global:
        return (
            f"hub bind {host} looks public: the hub speaks plain HTTP,"
            " serve it only over a private network"
        )
    return None


def server_family(bind: str) -> socket.AddressFamily:
    """The socket family for a hub bind: IPv6 for IPv6 literals, else IPv4.

    Names stay on IPv4: they resolve at bind time, and the default
    family keeps today's behavior for them.
    """
    try:
        address = ipaddress.ip_address(str(bind or ""))
    except ValueError:
        return socket.AF_INET
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


class HubServer(http.server.ThreadingHTTPServer):
    """The hub service: one thread per connection, one transaction per write."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        db_path: str,
        *,
        clock: Callable[[], float] | None = None,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
        stale_after_s: int = DEFAULT_STALE_AFTER_S,
    ):
        self.db_path = db_path
        self.clock = clock or time.time
        self.lease_ttl_s = lease_ttl_s
        self.stale_after_s = stale_after_s
        self.address_family = server_family(address[0])
        super().__init__(address, HubHandler)


def _server(handler: http.server.BaseHTTPRequestHandler) -> HubServer:
    server = handler.server
    assert isinstance(server, HubServer)
    return server


#: Authed claim routes: propose, renew a live lease, release a hold.
CLAIM_PATHS = ("/v1/claims", "/v1/claims/renew", "/v1/claims/release")
#: The heartbeat check-in: renewals beside the snapshot document.
HEARTBEAT_PATH = "/v1/heartbeat"
#: The host check-in: address, capacity, constraints.
REGISTER_PATH = "/v1/register"
#: The merged read-only dashboard: membership and the last push per host.
HOSTS_PATH = "/v1/hosts"
SNAPSHOT_PATH = "/v1/snapshot"
#: Every route that takes a body: register, heartbeat, and the claims.
POST_PATHS = (REGISTER_PATH, HEARTBEAT_PATH) + CLAIM_PATHS


class HubHandler(http.server.BaseHTTPRequestHandler):
    """Routes: `GET /v1/version` (open), everything else authed.

    `GET /v1/hosts` lists the membership with reachability;
    `GET /v1/snapshot` merges the last pushed document per host, with
    `?project=` filtering agents to one known project (`all` merges
    every host, an unknown name is 404). Anything else is 404
    (unknown path) or 405 (wrong method), with no side effects.
    Error bodies are minimal JSON: no paths, no tokens, no tracebacks.
    """

    server_version = "corral-hub/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("hub: %s", format % args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > _MAX_BODY:
            return None
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _authed_host(self, conn: sqlite3.Connection) -> str | None:
        presented = self.headers.get("Authorization")
        if not presented or not presented.startswith("Bearer "):
            return None
        return verify_token(conn, presented[len("Bearer ") :])

    def _reject(self, text: str) -> None:
        log.warning("hub: rejected request from %s: %s", self.client_address[0], text)
        self._send(401, {"error": text})

    def _reader(self, conn: sqlite3.Connection) -> str | None:
        """The calling host for a read route: any registered token reads."""
        return self._authed_host(conn)

    def do_GET(self) -> None:
        split = urllib.parse.urlsplit(self.path)
        if split.path == "/v1/version" and not split.query:
            self._send(200, {"api": HUB_API, "schema_version": SCHEMA_VERSION})
            return
        if split.path in POST_PATHS:
            self._send(405, {"error": "hub v1 takes GET and POST only"})
            return
        if split.path not in (HOSTS_PATH, SNAPSHOT_PATH):
            self._send(404, {"error": f"unknown hub route {split.path}"})
            return
        server = _server(self)
        try:
            conn = open_db(server.db_path)
        except HubError as err:
            log.warning("hub: store unavailable: %s", err)
            self._send(500, {"error": "hub store unavailable"})
            return
        try:
            if self._reader(conn) is None:
                self._reject("unknown hub host")
                return
            now = server.clock()
            if split.path == HOSTS_PATH:
                self._send(
                    200,
                    {
                        "membership_epoch": membership_epoch(conn),
                        "now": now,
                        "hosts": read_hosts(conn, now=now, stale_after_s=server.stale_after_s),
                    },
                )
                return
            query = urllib.parse.parse_qs(split.query)
            project = (query.get("project") or ["all"])[0] or "all"
            try:
                merged = read_snapshot(
                    conn,
                    project=project,
                    now=now,
                    stale_after_s=server.stale_after_s,
                )
            except UnknownProject as err:
                self._send(404, {"error": str(err)})
                return
            merged["membership_epoch"] = membership_epoch(conn)
            self._send(200, merged)
        finally:
            conn.close()

    def _valid_claim_body(self, body: dict[str, Any], *, want_epoch: bool) -> bool:
        repo, number, agent, claim_host, epoch = (
            body.get("repo"),
            body.get("number"),
            body.get("agent"),
            body.get("host_id"),
            body.get("epoch"),
        )
        if (
            not isinstance(repo, str)
            or not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(agent, str)
            or not isinstance(claim_host, str)
        ):
            return False
        if want_epoch and (not isinstance(epoch, int) or isinstance(epoch, bool)):
            return False
        return True

    def _post_claim(
        self, server: HubServer, conn: sqlite3.Connection, body: dict[str, Any]
    ) -> None:
        if not self._valid_claim_body(body, want_epoch=False):
            self._send(400, {"error": "claim needs repo, number, agent and host_id"})
            return
        try:
            receipt = grant(
                conn,
                str(body["repo"]),
                int(body["number"]),
                str(body["agent"]),
                str(body["host_id"]),
                now=server.clock(),
                lease_ttl_s=server.lease_ttl_s,
            )
        except Conflict as err:
            self._send(409, {"error": str(err), "holder": err.holder})
        except HubError as err:
            self._send(400, {"error": str(err)})
        else:
            self._send(200, receipt.as_dict())

    def _post_renew(
        self, server: HubServer, conn: sqlite3.Connection, body: dict[str, Any]
    ) -> None:
        if not self._valid_claim_body(body, want_epoch=True):
            self._send(400, {"error": "renew needs repo, number, agent, epoch and host_id"})
            return
        try:
            receipt = renew(
                conn,
                str(body["repo"]),
                int(body["number"]),
                str(body["agent"]),
                int(body["epoch"]),
                now=server.clock(),
                lease_ttl_s=server.lease_ttl_s,
            )
        except Conflict as err:
            self._send(409, {"error": str(err), "holder": err.holder})
        except HubError as err:
            self._send(400, {"error": str(err)})
        else:
            self._send(200, receipt.as_dict())

    def _post_release(self, conn: sqlite3.Connection, body: dict[str, Any]) -> None:
        if not self._valid_claim_body(body, want_epoch=True):
            self._send(400, {"error": "release needs repo, number, agent, epoch and host_id"})
            return
        try:
            kept = release(
                conn,
                str(body["repo"]),
                int(body["number"]),
                str(body["agent"]),
                int(body["epoch"]),
                now=_server(self).clock(),
            )
        except Conflict as err:
            self._send(409, {"error": str(err), "holder": err.holder})
        except HubError as err:
            self._send(400, {"error": str(err)})
        else:
            self._send(200, {"repo": body["repo"], "number": body["number"], "epoch": kept})

    def _post_register(
        self, server: HubServer, conn: sqlite3.Connection, body: dict[str, Any]
    ) -> None:
        if not isinstance(body.get("host_id"), str) or not body["host_id"]:
            self._send(400, {"error": "register needs host_id"})
            return
        try:
            epoch = register_host(
                conn,
                body["host_id"],
                now=server.clock(),
                address=body.get("address", ""),
                capacity=body.get("capacity"),
                constraints=body.get("constraints"),
            )
        except HubError as err:
            self._send(400, {"error": str(err)})
            return
        self._send(200, {"host_id": body["host_id"], "membership_epoch": epoch})

    def _held_by_other(self, conn: sqlite3.Connection, repo: str, number: int, caller: str) -> bool:
        """Whether the claim row names a live holder other than the caller.

        A missing or cleared row is nobody's: only a live row held by
        another host refuses, so a re-grant after a crash still renews.
        """
        row = conn.execute(
            "SELECT agent, host_id FROM claims WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        return row is not None and bool(row[0]) and row[1] != caller

    def _refuse_foreign(
        self, identity: dict[str, Any], repo: str, number: int, caller: str
    ) -> dict[str, Any]:
        """The per-entry refusal for a renewal held by another host.

        Same shape as every other rejected entry, but with no holder:
        the caller learns its renewal failed, never who holds the
        ticket.
        """
        return {
            **identity,
            "ok": False,
            "status": 409,
            "error": f"ticket {repo}#{number} cannot be renewed by {caller}",
        }

    def _renewal_answer(
        self,
        server: HubServer,
        conn: sqlite3.Connection,
        entry: object,
        caller: str,
    ) -> dict[str, Any]:
        """One granted-or-409 answer for a heartbeat renewal entry.

        A malformed entry is answered per entry (never a whole-body
        refusal), so one bad row cannot sink the heartbeat's other
        renewals or its snapshot. A renewal is bound to the
        authenticated caller: an entry for a claim another host holds
        is refused per entry without naming the holder, so no host
        can extend another host's lease.
        """
        if not isinstance(entry, dict):
            return {"ok": False, "status": 400, "error": "renewal must be an object"}
        repo, number, agent, epoch = (
            entry.get("repo"),
            entry.get("number"),
            entry.get("agent"),
            entry.get("epoch"),
        )
        identity = {"repo": repo, "number": number, "agent": agent, "epoch": epoch}
        if (
            not isinstance(repo, str)
            or not repo
            or isinstance(number, bool)
            or not isinstance(number, int)
            or not isinstance(agent, str)
            or not agent
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
        ):
            return {
                **identity,
                "ok": False,
                "status": 400,
                "error": "renewal needs repo, number, agent and epoch",
            }
        if self._held_by_other(conn, repo, number, caller):
            return self._refuse_foreign(identity, repo, number, caller)
        try:
            receipt = renew(
                conn,
                repo,
                number,
                agent,
                epoch,
                now=server.clock(),
                lease_ttl_s=server.lease_ttl_s,
            )
        except Conflict as err:
            if self._held_by_other(conn, repo, number, caller):
                return self._refuse_foreign(identity, repo, number, caller)
            return {
                **identity,
                "ok": False,
                "status": 409,
                "error": str(err),
                "holder": err.holder,
            }
        except HubError as err:
            return {**identity, "ok": False, "status": 400, "error": str(err)}
        return {**identity, "ok": True, "receipt": receipt.as_dict()}

    def _post_heartbeat(
        self, server: HubServer, conn: sqlite3.Connection, body: dict[str, Any], caller: str
    ) -> None:
        if not isinstance(body.get("host_id"), str) or not body["host_id"]:
            self._send(400, {"error": "heartbeat needs host_id"})
            return
        renewals = body.get("renewals", [])
        if not isinstance(renewals, list):
            self._send(400, {"error": "heartbeat renewals must be a list"})
            return
        if "snapshot" in body and not isinstance(body["snapshot"], dict):
            self._send(400, {"error": "heartbeat snapshot must be an object"})
            return
        try:
            epoch = register_host(
                conn,
                body["host_id"],
                now=server.clock(),
                **{key: body[key] for key in ("address", "capacity", "constraints") if key in body},
            )
        except HubError as err:
            self._send(400, {"error": str(err)})
            return
        answers = [self._renewal_answer(server, conn, entry, caller) for entry in renewals]
        if "snapshot" in body and isinstance(body["snapshot"], dict):
            try:
                store_snapshot(conn, body["host_id"], body["snapshot"], now=server.clock())
            except HubError as err:
                self._send(400, {"error": str(err)})
                return
        self._send(200, {"membership_epoch": epoch, "renewals": answers})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/v1/version":
            self._send(405, {"error": "hub v1 takes GET and POST only"})
            return
        if path not in POST_PATHS:
            self._send(404, {"error": f"unknown hub route {path}"})
            return
        server = _server(self)
        body = self._read_json()
        if body is None:
            self._send(400, {"error": "hub needs a JSON object body"})
            return
        try:
            conn = open_db(server.db_path)
        except HubError as err:
            log.warning("hub: store unavailable: %s", err)
            self._send(500, {"error": "hub store unavailable"})
            return
        try:
            host = self._authed_host(conn)
            if host is None or host != body.get("host_id"):
                self._reject("unknown hub host")
                return
            if path == REGISTER_PATH:
                self._post_register(server, conn, body)
            elif path == HEARTBEAT_PATH:
                self._post_heartbeat(server, conn, body, host)
            elif path == "/v1/claims/renew":
                self._post_renew(server, conn, body)
            elif path == "/v1/claims/release":
                self._post_release(conn, body)
            else:
                self._post_claim(server, conn, body)
        finally:
            conn.close()

    def do_PUT(self) -> None:
        self._send(405, {"error": "hub v1 takes GET and POST only"})

    def do_DELETE(self) -> None:
        self._send(405, {"error": "hub v1 takes GET and POST only"})

    def do_PATCH(self) -> None:
        self._send(405, {"error": "hub v1 takes GET and POST only"})

    def do_HEAD(self) -> None:
        self._send(405, {"error": "hub v1 takes GET and POST only"})

    def do_OPTIONS(self) -> None:
        self._send(405, {"error": "hub v1 takes GET and POST only"})


def make_server(
    bind: str,
    port: int,
    db_path: str,
    *,
    clock: Callable[[], float] | None = None,
    lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
) -> HubServer:
    """Bind the hub service. Fails fast when the store cannot open.

    A held port raises ``OSError`` with ``EADDRINUSE``; the verb maps
    it to exit 2 and every other bind error to exit 4.
    """
    open_db(db_path).close()
    return HubServer(
        (bind, port), db_path, clock=clock, lease_ttl_s=lease_ttl_s, stale_after_s=stale_after_s
    )


def _split_url(url: str) -> tuple[str, int]:
    """The `(host, port)` for a hub URL. The hub speaks plain HTTP only."""
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError as err:
        raise HubError(f"hub url {url!r} is not usable: {err}") from None
    if split.scheme != "http" or not split.hostname or not split.port:
        raise HubError(f"hub url {url!r} must be http://host:port")
    return split.hostname, split.port


def _post_json(
    url: str, path: str, token: str, payload: dict[str, Any], *, timeout: float = 10
) -> dict[str, Any]:
    """POST one JSON body to the hub and return the decoded reply.

    A refused connection (or any I/O failure) is ``HubUnreachable``
    (exit 3): the caller keeps running held work and claims nothing
    new. A decided reply (401, 400, 404) is ``HubError`` naming the
    server's reason without echoing the token.
    """
    host, port = _split_url(url)
    raw = json.dumps(payload).encode("utf-8")
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        try:
            conn.request(
                "POST",
                path,
                body=raw,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw)),
                    "Authorization": f"Bearer {token}",
                },
            )
            response = conn.getresponse()
            body = response.read()
        except OSError as err:
            raise HubUnreachable(f"hub at {url} is unreachable: {err}") from None
    finally:
        conn.close()
    try:
        decoded = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        decoded = None
    if response.status == 200 and isinstance(decoded, dict):
        return decoded
    reason = decoded.get("error") if isinstance(decoded, dict) else None
    raise HubError(f"hub at {url} refused {path}: {response.status} {reason or ''}".rstrip())


def push_register(
    url: str,
    token: str,
    host_id: str,
    *,
    address: str = "",
    capacity: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    """Check a host in: report address, capacity and constraints.

    Returns the hub reply, which carries the current membership
    epoch. The document the host pushes later rides the heartbeat,
    not this call.
    """
    return _post_json(
        url,
        REGISTER_PATH,
        token,
        {
            "host_id": host_id,
            "address": address,
            "capacity": capacity if capacity is not None else {},
            "constraints": constraints if constraints is not None else {},
        },
        timeout=timeout,
    )


def push_heartbeat(
    url: str,
    token: str,
    host_id: str,
    *,
    renewals: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    snapshot: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    """One call per heartbeat interval: renewals beside the snapshot.

    Returns the hub reply: the membership epoch plus one
    granted-or-refused answer per renewal entry. A refused renewal
    (``ok`` false) never raises here: the caller stops the ticket's
    units and drops its local claim, exactly like the reconnect rule.
    """
    payload: dict[str, Any] = {"host_id": host_id, "renewals": list(renewals)}
    if snapshot is not None:
        payload["snapshot"] = snapshot
    if capacity is not None:
        payload["capacity"] = capacity
    if constraints is not None:
        payload["constraints"] = constraints
    return _post_json(url, HEARTBEAT_PATH, token, payload, timeout=timeout)
