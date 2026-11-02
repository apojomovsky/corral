"""The hub: one SQLite database behind a small loopback HTTP service.

Tickets H1, H2 and H6 of docs/08-hub-multihost.md section 10: the
hub skeleton, the host registry and the claims table (H1), the lease
cycle with renew and release (H2), and hub operations (H6): backup,
export and import with fencing epochs preserved, and the version
exchange hosts check on every call. Later tickets add drive
composition (H3), operator verbs (H4) and the merged dashboard (H5).

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
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from corral import paths, settings
from corral.netbind import server_family

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


#: Export document version written by `export_hub`. Import refuses a
#: newer document instead of guessing: fencing continuity is too
#: important to reinterpret.
EXPORT_VERSION = 1


class HubVersionError(Exception):
    """This host and the hub speak different API majors (exit 4)."""

    exit_code = 4


def check_version(hub_api: int, hub_schema: int) -> None:
    """Fail closed when the hub's API major differs from this host's.

    Both versions are named in the error: the operator upgrades the hub
    and the hosts in lockstep. The schema version floats within a
    major: additive-only changes never break the exchange.
    """
    _ = hub_schema
    if hub_api != HUB_API:
        raise HubVersionError(
            f"hub speaks api {hub_api}, this host speaks api {HUB_API}:"
            " upgrade the hub and the hosts in lockstep"
        )


def export_hub(conn: sqlite3.Connection) -> dict[str, Any]:
    """The hub's movable state as JSON-ready data.

    Hosts carry ids and reported facts only, never token material, so
    no secret crosses the export. Claims carry their fencing epochs, so
    fencing continuity survives the move. Snapshots are excluded by
    design: hosts re-push on their next heartbeat. The projects list is
    empty until the dashboard ticket grows a projects table; the key
    stays so the shape never changes under readers.
    """
    try:
        hosts = [
            {
                "host_id": host_id,
                "address": address,
                "capacity": capacity,
                "constraints": constraints,
            }
            for host_id, address, capacity, constraints in conn.execute(
                "SELECT host_id, address, capacity, constraints FROM hosts ORDER BY host_id"
            ).fetchall()
        ]
        claims = [
            {
                "repo": repo,
                "number": number,
                "host_id": host_id,
                "agent": agent,
                "epoch": epoch,
                "lease_expires": lease_expires,
                "updated_at": updated_at,
            }
            for repo, number, host_id, agent, epoch, lease_expires, updated_at in conn.execute(
                "SELECT repo, number, host_id, agent, epoch,"
                " lease_expires, updated_at FROM claims ORDER BY repo, number"
            ).fetchall()
        ]
        epoch_row = conn.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
    except sqlite3.Error as err:
        raise HubError(f"cannot export the hub store: {err}") from None
    return {
        "export_version": EXPORT_VERSION,
        "hosts": hosts,
        "projects": [],
        "claims": claims,
        "membership_epoch": epoch_row[0] if epoch_row is not None else 0,
    }


def _checked_claim(entry: Any) -> dict[str, Any]:
    """One validated claim entry, or HubError naming what is wrong."""
    if not isinstance(entry, dict):
        raise HubError(f"hub claim {entry!r} must be an object")
    for key in ("repo", "agent", "host_id"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise HubError(f"hub claim {key} {entry.get(key)!r} must be a non-empty string")
    for key in ("number", "epoch"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise HubError(f"hub claim {key} {value!r} must be a positive integer")
    for key in ("lease_expires", "updated_at"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HubError(f"hub claim {key} {value!r} must be a number")
    return {
        "repo": entry["repo"],
        "number": entry["number"],
        "host_id": entry["host_id"],
        "agent": entry["agent"],
        "epoch": entry["epoch"],
        "lease_expires": float(entry["lease_expires"]),
        "updated_at": float(entry["updated_at"]),
    }


def import_hub(conn: sqlite3.Connection, document: object) -> tuple[int, list[str]]:
    """Load an `export_hub` document into a fresh hub, keeping every epoch.

    Returns the claims loaded and the host ids to re-add: hosts are
    validated but never stored, because token hashes never cross the
    export and `add_host` stays the only minter, so the operator
    re-runs `add_host --id` for each listed id and installs the minted
    tokens. A hub that already holds hosts or claims, a newer export
    version, a non-empty projects list (this hub stores no projects
    yet), and any bad shape are HubError, checked before anything is
    written, so a refused import leaves the target untouched.
    """
    if not isinstance(document, Mapping):
        raise HubError("hub import needs the JSON object `corral hub export` wrote")
    version = document.get("export_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != EXPORT_VERSION:
        raise HubError(
            f"hub export version {version!r} is not supported here"
            f" (this hub reads {EXPORT_VERSION}): failing closed"
        )
    hosts = document.get("hosts")
    if not isinstance(hosts, list):
        raise HubError("hub import needs a hosts list")
    host_ids: list[str] = []
    for entry in hosts:
        if not isinstance(entry, dict):
            raise HubError(f"hub host {entry!r} must be an object")
        host_id = entry.get("host_id")
        if not isinstance(host_id, str) or not HOST_ID_RE.fullmatch(host_id):
            raise HubError(f"hub host id {host_id!r} must match {HOST_ID_RE.pattern}")
        host_ids.append(host_id)
    projects = document.get("projects")
    if not isinstance(projects, list):
        raise HubError("hub import needs a projects list")
    if projects:
        raise HubError(
            "hub import holds projects this hub version cannot store:"
            " upgrade the hub first, then import again"
        )
    claims_raw = document.get("claims")
    if not isinstance(claims_raw, list):
        raise HubError("hub import needs a claims list")
    claims = [_checked_claim(entry) for entry in claims_raw]
    seen = set()
    for claim in claims:
        key = (claim["repo"], claim["number"])
        if key in seen:
            raise HubError(f"hub import lists {claim['repo']}#{claim['number']} twice")
        seen.add(key)
    epoch = document.get("membership_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise HubError(f"hub membership epoch {epoch!r} must be an integer of 0 or more")
    try:
        held = conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0]
        held += conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        stored = conn.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
    except sqlite3.Error as err:
        raise HubError(f"cannot read the hub store: {err}") from None
    if held or (stored is not None and stored[0] != 0):
        raise HubError("hub import needs a fresh hub: this one already holds state")
    try:
        with _txn(conn):
            conn.executemany(
                "INSERT INTO claims(repo, number, host_id, agent, epoch,"
                " lease_expires, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        claim["repo"],
                        claim["number"],
                        claim["host_id"],
                        claim["agent"],
                        claim["epoch"],
                        claim["lease_expires"],
                        claim["updated_at"],
                    )
                    for claim in claims
                ],
            )
            conn.execute("UPDATE meta SET value=? WHERE key='membership_epoch'", (epoch,))
    except sqlite3.Error as err:
        raise HubError(f"cannot import into the hub store: {err}") from None
    return len(claims), host_ids


def backup_hub(source: str, dest: str) -> str:
    """Copy the hub database to `dest` and return it.

    The stdlib online backup API copies a running hub safely; the copy
    is owner-only like the live store. `dest` is created exclusively
    before anything connects, so an existing file is refused instead of
    clobbered and a failed copy leaves nothing behind. Restore is a
    file replacement while the hub is stopped.
    """
    parent = os.path.dirname(dest)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise HubError(f"cannot create backup dir {parent}: {err}") from None
    try:
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise HubError(f"backup destination {dest} exists: remove it first") from None
    except OSError as err:
        raise HubError(f"cannot write backup {dest}: {err}") from None
    os.close(fd)
    try:
        try:
            src = sqlite3.connect(
                f"file:{source}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000
            )
        except sqlite3.Error as err:
            raise HubError(f"cannot read hub store {source}: {err}") from None
        try:
            try:
                dst = sqlite3.connect(dest, timeout=_BUSY_TIMEOUT_MS / 1000)
            except sqlite3.Error as err:
                raise HubError(f"cannot write backup {dest}: {err}") from None
            try:
                os.chmod(dest, 0o600)
            except OSError as err:
                raise HubError(f"cannot secure backup {dest}: {err}") from None
            try:
                src.backup(dst)
            except sqlite3.Error as err:
                raise HubError(f"cannot copy hub store {source} to {dest}: {err}") from None
            finally:
                dst.close()
        finally:
            src.close()
    except (HubError, OSError, sqlite3.Error):
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return dest


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


#: A versioned hub path: `/v<major>` plus the rest, or None.
_VERSIONED_PATH_RE = re.compile(r"^/v(\d+)(?:/|$)")

#: Authed claim routes: propose, renew a live lease, release a hold.
CLAIM_PATHS = ("/v1/claims", "/v1/claims/renew", "/v1/claims/release")


def _path_major(path: str) -> int | None:
    """The API major a request path asks for, or None when unversioned."""
    match = _VERSIONED_PATH_RE.match(path)
    return int(match.group(1)) if match else None


class HubHandler(http.server.BaseHTTPRequestHandler):
    """Routes: `GET /v1/version` (open), the claim routes (authed).

    Anything else is 404 (unknown path) or 405 (wrong method), with no
    side effects. A path naming another API major gets a typed 404
    refusal carrying the served versions, so an older host learns to
    upgrade instead of guessing. Error bodies are minimal JSON: no
    paths, no tokens, no tracebacks.
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

    def _refuse_old_major(self) -> bool:
        """A typed refusal for a path naming another API major, or False."""
        major = _path_major(self.path)
        if major is None or major == HUB_API:
            return False
        self._send(
            404,
            {
                "api": HUB_API,
                "schema_version": SCHEMA_VERSION,
                "error": f"unsupported hub api {major}: this hub serves api {HUB_API}",
            },
        )
        return True

    def do_GET(self) -> None:
        if self._refuse_old_major():
            return
        if self.path == "/v1/version":
            self._send(200, {"api": HUB_API, "schema_version": SCHEMA_VERSION})
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
        if self._refuse_old_major():
            return
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
