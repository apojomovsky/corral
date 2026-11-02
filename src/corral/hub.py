"""The hub: one SQLite database behind a small loopback HTTP service.

Tickets H1 through H3 of docs/08-hub-multihost.md section 10: the
hub skeleton, the host registry, the claims table, the lease cycle
(renew and release with fencing epochs), the claims list for the
reconnect fetch, and the host-side client the drive pass proposes
through. Later tickets add operator verbs (H4), the merged dashboard
(H5) and hub operations (H6).

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
settings.register(
    "hub.host_id",
    str,
    "",
    "this host's operator-assigned hub id (empty means unregistered, hub mode refused)",
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
    """The hub could not be reached at all (exit 3).

    Refused, timed out, reset, or answered 500: the hub host is down,
    so the drive keeps held work, skips new claims, and retries later.
    A 401 or a malformed reply is configuration, not outage, and stays
    ``HubError`` instead.
    """

    exit_code = 3


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


def list_claims(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every hub claim row: holder, epoch, lease and clocks.

    The reconnect fetch from docs/08-hub-multihost.md section 3 reads
    this table and compares it ticket by ticket against local claims.
    Released rows stay listed with an empty holder (their epoch is
    kept for the next grant); rows never appear for tickets the hub
    never granted.
    """
    rows = conn.execute(
        "SELECT repo, number, host_id, agent, epoch, lease_expires, updated_at"
        " FROM claims ORDER BY repo, number"
    ).fetchall()
    return [
        {
            "repo": repo,
            "number": number,
            "host_id": host_id,
            "agent": agent,
            "epoch": epoch,
            "lease_expires": lease_expires,
            "updated_at": updated_at,
        }
        for repo, number, host_id, agent, epoch, lease_expires, updated_at in rows
    ]


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


#: Authed claim routes: propose, renew a live lease, release a hold.
CLAIM_PATHS = ("/v1/claims", "/v1/claims/renew", "/v1/claims/release")


class HubHandler(http.server.BaseHTTPRequestHandler):
    """Routes: `GET /v1/version` (open), `GET /v1/claims` and the claim routes (authed).

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

    def _get_claims(self, server: HubServer) -> None:
        try:
            conn = open_db(server.db_path)
        except HubError as err:
            log.warning("hub: store unavailable: %s", err)
            self._send(500, {"error": "hub store unavailable"})
            return
        try:
            if self._authed_host(conn) is None:
                self._reject("unknown hub host")
                return
            self._send(200, {"claims": list_claims(conn)})
        finally:
            conn.close()

    def do_GET(self) -> None:
        if self.path == "/v1/version":
            self._send(200, {"api": HUB_API, "schema_version": SCHEMA_VERSION})
            return
        if self.path == "/v1/claims":
            self._get_claims(_server(self))
            return
        if self.path in CLAIM_PATHS:
            self._send(405, {"error": "hub v1 takes GET and POST only"})
            return
        self._send(404, {"error": f"unknown hub route {self.path}"})

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

    def do_POST(self) -> None:
        if self.path == "/v1/version":
            self._send(405, {"error": "hub v1 takes GET and POST only"})
            return
        if self.path not in CLAIM_PATHS:
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
            if self.path == "/v1/claims/renew":
                self._post_renew(server, conn, body)
            elif self.path == "/v1/claims/release":
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


class HubClient:
    """The host side of the hub claim protocol: plain HTTP, standard library only.

    One seam for every hub network call, so drive tests inject a fake
    here and never touch the network. The token is read from its file
    at call time, never stored: each request re-reads
    ``token_path``, and the token never appears in an error. A refused,
    timed-out, reset, or 500 answer raises ``HubUnreachable`` (exit 3,
    retry later); a 401, a 400, or a malformed reply raises
    ``HubError`` (exit 4, configuration); a 409 raises ``Conflict``
    naming the holder.
    """

    def __init__(self, url: str, host_id: str, token_path: str, *, timeout_s: float = 10) -> None:
        self.url = url
        self.host_id = host_id
        self.token_path = token_path
        self.timeout_s = timeout_s

    def _endpoint(self) -> tuple[str, int]:
        try:
            parts = urllib.parse.urlsplit(self.url)
        except ValueError as err:
            raise HubError(f"hub url {self.url!r} does not parse: {err}") from None
        if parts.scheme != "http" or not parts.hostname:
            raise HubError(f"hub url {self.url!r} must be plain http://host:port")
        return parts.hostname, parts.port or 80

    def _token(self) -> str:
        try:
            with open(self.token_path, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as err:
            raise HubError(f"cannot read hub token {self.token_path}: {err}") from None
        if not token:
            raise HubError(f"hub token {self.token_path} is empty")
        return token

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        host, port = self._endpoint()
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._token()}"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout_s)
            try:
                conn.request(method, path, body=raw, headers=headers)
                response = conn.getresponse()
                payload = response.read()
                return response.status, payload
            finally:
                conn.close()
        except (OSError, http.client.HTTPException) as err:
            raise HubUnreachable(f"hub at {self.url} is unreachable: {err}") from None

    def _decode(self, payload: bytes) -> dict[str, Any]:
        try:
            body = json.loads(payload.decode("utf-8")) if payload else None
        except (ValueError, UnicodeDecodeError) as err:
            raise HubError(f"hub at {self.url} answered garbage: {err}") from None
        if not isinstance(body, dict):
            raise HubError(f"hub at {self.url} answered a non-object")
        return body

    def _receipt(self, repo: str, number: int, body: dict[str, Any]) -> Receipt:
        try:
            epoch = body["epoch"]
            lease = body["lease_expires"]
            agent = body["agent"]
            holder = body["host_id"]
        except KeyError as err:
            raise HubError(f"hub receipt for {repo}#{number} is missing {err}") from None
        if (
            not isinstance(agent, str)
            or not agent
            or not isinstance(holder, str)
            or not holder
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 1
            or isinstance(lease, bool)
            or not isinstance(lease, (int, float))
        ):
            raise HubError(f"hub receipt for {repo}#{number} is malformed")
        return Receipt(repo, number, agent, holder, epoch, float(lease))

    def _checked(self, what: str, repo: str, number: int, status: int, payload: bytes):
        """The decoded body for a 200, else the typed error the status means."""
        if status == 500:
            raise HubUnreachable(f"hub at {self.url} answered 500: store unavailable")
        decoded = self._decode(payload)
        if status == 409:
            holder = decoded.get("holder") if isinstance(decoded, dict) else None
            if (
                not isinstance(holder, dict)
                or not isinstance(holder.get("agent"), str)
                or not isinstance(holder.get("host_id"), str)
                or not isinstance(holder.get("epoch"), int)
            ):
                raise HubError(f"hub conflict for {repo}#{number} names no holder")
            raise Conflict(repo, number, holder["agent"], holder["host_id"], holder["epoch"])
        if status == 401:
            raise HubError(f"hub at {self.url} refused this host: check hub.host_id and its token")
        if status != 200:
            raise HubError(f"hub at {self.url} answered {status} for {what}")
        return decoded

    def _post(self, path: str, repo: str, number: int, agent: str, epoch: int | None) -> Receipt:
        body: dict[str, Any] = {
            "repo": repo,
            "number": number,
            "agent": agent,
            "host_id": self.host_id,
        }
        if epoch is not None:
            body["epoch"] = epoch
        status, payload = self._request("POST", path, body)
        decoded = self._checked(f"{repo}#{number}", repo, number, status, payload)
        return self._receipt(repo, number, decoded)

    def propose(self, repo: str, number: int, agent: str) -> Receipt:
        """Grant or refuse one claim: the receipt, or ``Conflict`` naming the holder."""
        return self._post("/v1/claims", repo, number, agent, None)

    def renew(self, repo: str, number: int, agent: str, epoch: int) -> Receipt:
        """Extend a live lease without changing its epoch, or ``Conflict``."""
        return self._post("/v1/claims/renew", repo, number, agent, epoch)

    def release(self, repo: str, number: int, agent: str, epoch: int) -> int:
        """Release a held claim, keeping the epoch. Returns the kept epoch."""
        body: dict[str, Any] = {
            "repo": repo,
            "number": number,
            "agent": agent,
            "host_id": self.host_id,
            "epoch": epoch,
        }
        status, payload = self._request("POST", "/v1/claims/release", body)
        decoded = self._checked(f"{repo}#{number}", repo, number, status, payload)
        kept = decoded.get("epoch")
        if isinstance(kept, bool) or not isinstance(kept, int) or kept < 1:
            raise HubError(f"hub release for {repo}#{number} kept no epoch")
        return kept

    def claims(self) -> list[dict[str, Any]]:
        """Every hub claim row, for the reconnect compare. Raises ``HubUnreachable``."""
        status, payload = self._request("GET", "/v1/claims", None)
        decoded = self._checked("the claims list", "", 0, status, payload)
        rows = decoded.get("claims")
        if not isinstance(rows, list):
            raise HubError(f"hub at {self.url} answered claims without a list")
        found = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("repo"), str)
                or isinstance(row.get("number"), bool)
                or not isinstance(row.get("number"), int)
                or not isinstance(row.get("host_id"), str)
                or not isinstance(row.get("agent"), str)
                or isinstance(row.get("epoch"), bool)
                or not isinstance(row.get("epoch"), int)
                or isinstance(row.get("lease_expires"), bool)
                or not isinstance(row.get("lease_expires"), (int, float))
            ):
                raise HubError(f"hub at {self.url} answered a malformed claim row")
            found.append(row)
        return found


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
