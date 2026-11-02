"""The hub: one SQLite database behind a small loopback HTTP service.

Ticket H1 of docs/08-hub-multihost.md section 10: the hub skeleton,
the host registry and the claims table. Ticket H4 adds the operator
verbs: `remove-host` (membership epoch, claim release), per-ticket
`release`, and `rotate-token`. Later tickets add leases and renewal
(H2), drive composition (H3), the merged dashboard (H5) and hub
operations (H6).

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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from corral import paths, settings

#: API major served at `/v1/*`. Hosts compare it on every call; a
#: breaking change is a new path major with a cutover.
HUB_API = 1
#: Schema version stamped as ``PRAGMA user_version``. A database with
#: a newer or unknown version fails closed instead of migrating.
SCHEMA_VERSION = 1

#: Heartbeat every 30 seconds, lease three heartbeats: the decided D1
#: defaults from docs/08-hub-multihost.md section 12.
DEFAULT_HEARTBEAT_S = 30
DEFAULT_LEASE_TTL_S = 90

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

_BUSY_TIMEOUT_MS = 5000
_MAX_BODY = 65536

#: Host ids are operator-assigned, never raw hostnames: one path part,
#: no spaces, no slashes.
HOST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

log = logging.getLogger("corral.hub")


class HubError(Exception):
    """The hub store or request is unusable (exit 4)."""

    exit_code = 4


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


_SCHEMA = """
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


def membership_epoch(conn: sqlite3.Connection) -> int:
    """The hub's membership counter, bumped by every `remove-host`."""
    row = conn.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
    return int(row[0])


def remove_host(conn: sqlite3.Connection, host_id: str, *, now: float) -> int:
    """Remove a host: bump the epoch, release its claims, drop its token.

    The host's claim rows are cleared but their epochs are kept, so the
    next grant mints ``epoch + 1`` and fences a stale holder still acting
    on the old epoch. The returned epoch rides every register and
    heartbeat reply; a host that reads a newer epoch than the one it
    stored stops its agents and waits to be added again. An unknown id
    is ``HubError`` (exit 4).
    """
    name = str(host_id or "")
    with _txn(conn):
        row = conn.execute("SELECT host_id FROM hosts WHERE host_id=?", (name,)).fetchone()
        if row is None:
            raise HubError(f"hub host {name!r} is not registered")
        conn.execute("UPDATE meta SET value=value+1 WHERE key='membership_epoch'")
        conn.execute(
            "UPDATE claims SET agent='', host_id='', lease_expires=0, updated_at=? WHERE host_id=?",
            (now, name),
        )
        conn.execute("DELETE FROM hosts WHERE host_id=?", (name,))
        epoch = membership_epoch(conn)
    log.info("hub: removed host %s (membership epoch %d)", name, epoch)
    return epoch


def release_claim(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    *,
    now: float,
    reason: str = "",
) -> int:
    """Force-release one claim, keeping its epoch for the next mint.

    An operator override: the row is cleared but the epoch stays, so the
    next grant fences. An unclaimed or unknown ticket is ``HubError``
    (exit 4). The reason is logged; it never carries a secret.
    """
    if not isinstance(repo, str) or not repo:
        raise HubError(f"claim repo {repo!r} must be a non-empty string")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise HubError(f"claim number {number!r} must be a positive integer")
    with _txn(conn):
        row = conn.execute(
            "SELECT agent, epoch FROM claims WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        if row is None or not row[0]:
            raise HubError(f"ticket {repo}#{number} holds no hub claim to release")
        epoch = row[1]
        conn.execute(
            "UPDATE claims SET agent='', host_id='', lease_expires=0,"
            " updated_at=? WHERE repo=? AND number=?",
            (now, repo, number),
        )
    log.info("hub: released %s#%d (epoch %d): %s", repo, number, epoch, reason)
    return int(epoch)


def rotate_token(conn: sqlite3.Connection, host_id: str) -> str:
    """Revoke one host's token and mint its replacement.

    The old hash is replaced at once, so the leaked token dies on its
    next request; no other host's token changes. Returns the new token
    for the caller to print once. An unknown id is ``HubError``.
    """
    name = str(host_id or "")
    token = secrets.token_hex(32)
    salt = secrets.token_hex(16)
    with _txn(conn):
        cursor = conn.execute(
            "UPDATE hosts SET token_salt=?, token_hash=? WHERE host_id=?",
            (salt, _hash(salt, token), name),
        )
        if cursor.rowcount == 0:
            raise HubError(f"hub host {name!r} is not registered")
    log.info("hub: rotated token for host %s", name)
    return token


def _check_shape(repo: object, number: object, agent: object, host_id: object) -> None:
    if not isinstance(repo, str) or not repo:
        raise HubError(f"claim repo {repo!r} must be a non-empty string")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise HubError(f"claim number {number!r} must be a positive integer")
    if not isinstance(agent, str) or not agent:
        raise HubError(f"claim agent {agent!r} must be a non-empty string")
    if not isinstance(host_id, str) or not host_id:
        raise HubError(f"claim host_id {host_id!r} must be a non-empty string")


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
    ):
        self.db_path = db_path
        self.clock = clock or time.time
        self.lease_ttl_s = lease_ttl_s
        self.address_family = server_family(address[0])
        super().__init__(address, HubHandler)


def _server(handler: http.server.BaseHTTPRequestHandler) -> HubServer:
    server = handler.server
    assert isinstance(server, HubServer)
    return server


class HubHandler(http.server.BaseHTTPRequestHandler):
    """Routes: `GET /v1/version` (open), `POST /v1/claims` (authed).

    Anything else is 404 (unknown path) or 405 (wrong method), with no
    side effects. Error bodies are minimal JSON: no paths, no tokens,
    no tracebacks.
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

    def do_GET(self) -> None:
        if self.path == "/v1/version":
            self._send(200, {"api": HUB_API, "schema_version": SCHEMA_VERSION})
            return
        if self.path == "/v1/claims":
            self._send(405, {"error": "hub v1 takes GET and POST only"})
            return
        self._send(404, {"error": f"unknown hub route {self.path}"})

    def do_POST(self) -> None:
        if self.path == "/v1/version":
            self._send(405, {"error": "hub v1 takes GET and POST only"})
            return
        if self.path != "/v1/claims":
            self._send(404, {"error": f"unknown hub route {self.path}"})
            return
        server = _server(self)
        body = self._read_json()
        if body is None:
            self._send(400, {"error": "claim needs a JSON object with repo and number"})
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
            repo, number, agent, claim_host = (
                body.get("repo"),
                body.get("number"),
                body.get("agent"),
                body.get("host_id"),
            )
            if (
                not isinstance(repo, str)
                or not isinstance(number, int)
                or isinstance(number, bool)
                or not isinstance(agent, str)
                or not isinstance(claim_host, str)
            ):
                self._send(400, {"error": "claim needs repo, number, agent and host_id"})
                return
            try:
                receipt = grant(
                    conn,
                    repo,
                    number,
                    agent,
                    claim_host,
                    now=server.clock(),
                    lease_ttl_s=server.lease_ttl_s,
                )
            except Conflict as err:
                self._send(409, {"error": str(err), "holder": err.holder})
            except HubError as err:
                self._send(400, {"error": str(err)})
            else:
                self._send(200, receipt.as_dict())
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
) -> HubServer:
    """Bind the hub service. Fails fast when the store cannot open.

    A held port raises ``OSError`` with ``EADDRINUSE``; the verb maps
    it to exit 2 and every other bind error to exit 4.
    """
    open_db(db_path).close()
    return HubServer((bind, port), db_path, clock=clock, lease_ttl_s=lease_ttl_s)
