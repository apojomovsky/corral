"""Read-only loopback HTTP API over the fleet state (`corral serve`).

S1 covers the request router, `GET /v1/snapshot` from `snapshot.build`,
`/v1/version`, `/v1/health`, the JSON error shape and the 405/404 rules.
S2 adds the `serve.*` settings, the token file, the bind rules, CORS and
the `serve` verb. S3 adds the filtered views: `/v1/agents`,
`/v1/quota`, `/v1/capacity`, `/v1/disks`, `/v1/board` and `/v1/tail`.
S5 adds the snapshot TTL cache, the generation counter with `ETag`/`304`,
and the request counters in `/v1/health`.
Every endpoint reads local files through injected readers, so the API
can never disagree with the CLI. No `gh`, no network, no service
control in this module.
"""

import argparse
import errno
import hmac
import io
import ipaddress
import json
import os
import secrets
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from corral import board, launch, settings, snapshot
from corral.netbind import server_family

OK, RACE, UNREACHABLE, USAGE = 0, 2, 3, 4

#: Seconds one built snapshot is shared between requests. The one owner
#: for the default used by the setting, `_State`, and `make_server`.
SNAPSHOT_TTL_S_DEFAULT = 2

settings.register("serve.bind", str, "127.0.0.1", "address to bind (default 127.0.0.1, loopback)")
settings.register("serve.port", int, 0, "port to bind (default 0, an ephemeral port)")
settings.register(
    "serve.origins",
    list,
    [],
    "dashboard origins allowed cross-origin (exact match, never *)",
)
settings.register(
    "serve.snapshot_ttl_s",
    int,
    SNAPSHOT_TTL_S_DEFAULT,
    "seconds one built snapshot is shared between requests",
)
settings.register("serve.tail_max", int, 200, "maximum activity lines one tail read returns")
settings.register(
    "serve.max_streams",
    int,
    20,
    "concurrent stream connections before the server sheds with 503",
)


class ServeError(Exception):
    """A serve option or token file is unusable (exit 4)."""


#: The API major version, carried in the path (`/v1/`) and `/v1/version`.
API_VERSION = 1

#: Snapshot sections whose `{"error"}` body marks the server degraded.
#: Read through `snapshot`, the one owner of the section names.
ERROR_SECTIONS = snapshot.ERROR_SECTIONS

#: Board rows one listing returns unless `?limit=` says otherwise.
BOARD_LIMIT_DEFAULT = 100

#: Board rows one listing never returns more than; a larger `?limit=` clamps.
BOARD_LIMIT_MAX = 1000

#: The generic 500 body: the detail goes to stderr, never the wire.
UNAVAILABLE = "snapshot unavailable"

#: The generic 401 body: the presented value is never logged.
UNAUTHORIZED = "unauthorized"


def _err(text: str) -> None:
    print(f"corral serve: {text}", file=sys.stderr)


def is_loopback(bind: str) -> bool:
    """Whether `bind` stays on this host: a loopback literal or `localhost`.

    Any other name reads as remote: without resolving it (no DNS from this
    check) privacy cannot be proved, so it takes the token rules.
    """
    if str(bind or "") == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(bind or "")).is_loopback
    except ValueError:
        return False


def check_bind(bind: str, port: int, *, allow_remote: bool, has_token: bool) -> str | None:
    """Why the server must refuse to start, or None when it may listen.

    Section 3 refusal order: a bad port, then a non-loopback bind without
    `--allow-remote`, then a non-loopback bind without a token file.
    """
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        return f"serve port {port!r} must be 0-65535"
    if not is_loopback(bind) and not allow_remote:
        return f"non-loopback bind {bind!r} needs --allow-remote and a token file"
    if not is_loopback(bind) and not has_token:
        return f"non-loopback bind {bind!r} needs a readable token file (mint one with --new-token)"
    return None


def check_origins(origins) -> str | None:
    """Why the configured origins must refuse startup, or None.

    Exact matches only: a wildcard entry could be echoed back as `*`,
    so any entry holding one refuses instead.
    """
    for origin in origins or ():
        if not isinstance(origin, str) or not origin or "*" in origin:
            return f"serve origin {origin!r} is not an exact match (no wildcards)"
    return None


def read_token(path: str) -> str | None:
    """The bearer token in `path`, stripped; None when there is no file.

    Read at call time, so a minted or replaced file applies without a
    restart. An unreadable file raises: the caller fails closed.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise ServeError(f"cannot read token file {path}: {err}") from None
    return text.strip() or None


def mint_token(path: str) -> None:
    """Write a fresh bearer token to `path`, owner-only.

    The only write the verb ever does, to its own file. Refuses when the
    file already exists: replacing a live credential must be explicit.
    The token is returned to no one; the caller never prints it.
    """
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise ServeError(f"cannot create token dir {parent}: {err}") from None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ServeError(f"token file {path} already exists") from None
    except OSError as err:
        raise ServeError(f"cannot mint token file {path}: {err}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32) + "\n")
        os.chmod(path, 0o600)
    except OSError as err:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise ServeError(f"cannot mint token file {path}: {err}") from None


def token_file_warning(path: str) -> str | None:
    """Why `path` warns on stderr, or None when it is private or missing."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & 0o077:
        return f"token file {path} is readable beyond its owner (chmod 600)"
    return None


def read_tail(repo: str, number: int, last: int) -> str:
    """The ticket's rendered activity lines, through `launch.tail`.

    The production tail reader: resolves the repo target and returns the
    same text `corral tail` prints. The quota pool is empty on purpose, so
    a read never touches the quota ledger and the read-only promise holds.
    Raises `launch.LaunchError` when the ticket has nothing to show.
    """
    root = settings.find_root(os.getcwd())
    tgt = launch.target(root, repo)
    out = io.StringIO()
    launch.tail(tgt, number, last=last, out=out, pool_fn=lambda: [])
    return out.getvalue()


def _page_int(raw: str | None, name: str, default: int) -> tuple[int, str | None]:
    """A validated paging parameter, or the 400 body when it is unusable.

    Missing reads as `default`; above any maximum clamps at the call
    site, never here. A non-integer or a negative is an error, never a
    silent default: the poller must know its page is not what it asked.
    """
    if raw is None:
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, f"{name} {raw!r} is not an integer"
    if value < 0:
        return default, f"{name} {raw!r} must be 0 or more"
    return value, None


def _board_row(item: board.Item, fresh: bool, claim: board.Claim | None) -> dict:
    """One board row: the stored item, its freshness, and its holder."""
    row = asdict(item)
    row["fresh"] = fresh
    if claim is None:
        row["claim"] = None
    else:
        row["claim"] = {
            "agent": claim.agent,
            "host": claim.host,
            "epoch": claim.epoch,
            "claimed_at": claim.claimed_at,
        }
    return row


def _etag(generation: int) -> str:
    """The ETag for a snapshot generation: the counter, quoted."""
    return f'"{generation}"'


def _fresh_headers(etag: str, now: float) -> tuple:
    """The caching headers for a fresh snapshot-derived reply."""
    return (("ETag", etag), ("Last-Modified", formatdate(now, usegmt=True)))


class _State:
    """One server's injected builder, board, tail, clock, auth, CORS, cache, and counters."""

    def __init__(
        self,
        build: Callable[[], dict],
        clock: Callable[[], float],
        quiet: bool,
        allowed_origins: tuple[str, ...] = (),
        token_required: bool = False,
        token_reader: Callable[[], str | None] | None = None,
        board_open: Callable[[], board.Store] | None = None,
        tail_fn: Callable[[str, int, int], str] | None = None,
        tail_max: int = 200,
        snapshot_ttl_s: float = SNAPSHOT_TTL_S_DEFAULT,
    ) -> None:
        self.build = build
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.total = 0
        self.not_modified = 0
        self.quiet = quiet
        self.allowed_origins = tuple(allowed_origins)
        self.token_required = token_required
        self.token_reader = token_reader
        self.board_open = board_open
        self.tail_fn = tail_fn
        self.tail_max = tail_max
        self.snapshot_ttl_s = snapshot_ttl_s
        self.cached_doc: dict | None = None
        self.cached_at: float | None = None
        self.generation = 0


class _Handler(BaseHTTPRequestHandler):
    """Routes GET to the versioned endpoints; anything else is an error."""

    #: Bound per server by `make_server`: the state this handler answers from.
    server_state: _State

    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib override keeps the name
        if self.server_state.quiet:
            return
        super().log_message(format, *args)

    def _count(self) -> int:
        state = self.server_state
        with state.lock:
            state.total += 1
            return state.total

    def _echo_origin(self) -> str | None:
        """The request origin when it is configured, else None, never `*`."""
        origin = self.headers.get("Origin")
        if origin and origin != "*" and origin in self.server_state.allowed_origins:
            return origin
        return None

    def _send_json(self, code: int, obj: dict, headers: tuple = ()) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        echo = self._echo_origin()
        if echo is not None:
            self.send_header("Access-Control-Allow-Origin", echo)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, code: int, headers: tuple = ()) -> None:
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        echo = self._echo_origin()
        if echo is not None:
            self.send_header("Access-Control-Allow-Origin", echo)
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        return dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))

    def _authorized(self) -> bool:
        state = self.server_state
        if not state.token_required:
            return True
        expected = None
        try:
            if state.token_reader is not None:
                expected = state.token_reader()
        except Exception:  # noqa: BLE001 - an unreadable token denies, never breaks the request
            expected = None
        presented = self.headers.get("Authorization")
        ok = False
        if expected and presented and presented.startswith("Bearer "):
            try:
                ok = hmac.compare_digest(presented[len("Bearer ") :], expected)
            except TypeError:
                ok = False
        if not ok:
            print("corral serve: rejected unauthorized request", file=sys.stderr)
            self._send_json(401, {"error": UNAUTHORIZED})
        return ok

    def _cached_doc(self) -> tuple[dict | None, int | None, float, str | None]:
        """The snapshot for this request: a TTL cache hit or a fresh build.

        Returns the document, its generation, the request `now`, or the
        generic 500 body when the build failed. A failed build never
        populates the cache, so the next request retries. A TTL of zero
        or less rebuilds on every request.
        """
        state = self.server_state
        with state.lock:
            now = state.clock()
            if state.cached_at is not None and now - state.cached_at < state.snapshot_ttl_s:
                return state.cached_doc, state.generation, now, None
            try:
                doc = state.build()
            except Exception as exc:  # noqa: BLE001 - a request never drops the connection
                print(f"corral serve: {snapshot.error_text(exc)}", file=sys.stderr)
                return None, None, now, UNAVAILABLE
            if not isinstance(doc, dict):
                print(
                    "corral serve: snapshot builder returned a non-dict document",
                    file=sys.stderr,
                )
                return None, None, now, UNAVAILABLE
            state.generation += 1
            state.cached_doc = doc
            state.cached_at = now
            return doc, state.generation, now, None

    def _is_fresh(self, etag: str) -> bool:
        """Whether the client's copy (If-None-Match) is still current."""
        presented = self.headers.get("If-None-Match")
        if presented is None:
            return False
        return any(tag.strip() in (etag, "*") for tag in presented.split(","))

    def _send_not_modified(self, etag: str) -> None:
        """Answer 304 and count it; the client reuses its cached copy.

        Echoes the exact-match origin like `_send_json` does, passed
        through here (not inside `_send_empty`): the 204 preflight path
        supplies its own header, and a blanket echo would emit it twice.
        """
        state = self.server_state
        with state.lock:
            state.not_modified += 1
        echo = self._echo_origin()
        if echo is None:
            self._send_empty(304, (("ETag", etag),))
        else:
            self._send_empty(304, (("ETag", etag), ("Access-Control-Allow-Origin", echo)))

    def _serve_snapshot(self) -> None:
        doc, generation, now, error = self._cached_doc()
        if error is not None:
            self._send_json(500, {"error": error})
            return
        assert doc is not None  # _cached_doc pairs a missing doc with an error
        assert generation is not None  # _cached_doc pairs a missing generation with an error
        etag = _etag(generation)
        if self._is_fresh(etag):
            self._send_not_modified(etag)
        else:
            self._send_json(200, doc, _fresh_headers(etag, now))

    def _serve_health(self) -> None:
        state = self.server_state
        doc, generation, now, error = self._cached_doc()
        if error is not None:
            self._send_json(500, {"error": error})
            return
        assert doc is not None  # _cached_doc pairs a missing doc with an error
        assert generation is not None  # _cached_doc pairs a missing generation with an error
        degraded = sorted(
            name for name in ERROR_SECTIONS if snapshot.is_error_section(doc.get(name))
        )
        header = doc.get("fleet_header")
        host = header.get("host") if isinstance(header, dict) else None
        with state.lock:
            total = state.total
            not_modified = state.not_modified
        etag = _etag(generation)
        if self._is_fresh(etag):
            self._send_not_modified(etag)
            return
        self._send_json(
            200,
            {
                "ok": not degraded,
                "host": host,
                "now": now,
                "uptime_s": now - state.started,
                "requests_total": total,
                "requests_304": not_modified,
                "degraded": degraded,
            },
            _fresh_headers(etag, now),
        )

    def _serve_agents(self) -> None:
        doc, generation, now, error = self._cached_doc()
        if error is not None:
            self._send_json(500, {"error": error})
            return
        assert doc is not None  # _cached_doc pairs a missing doc with an error
        assert generation is not None  # _cached_doc pairs a missing generation with an error
        etag = _etag(generation)
        if self._is_fresh(etag):
            self._send_not_modified(etag)
        else:
            self._send_json(
                200,
                {"fleet_header": doc.get("fleet_header"), "agents": doc.get("agents")},
                _fresh_headers(etag, now),
            )

    def _serve_section(self, section: str) -> None:
        doc, generation, now, error = self._cached_doc()
        if error is not None:
            self._send_json(500, {"error": error})
            return
        assert doc is not None  # _cached_doc pairs a missing doc with an error
        assert generation is not None  # _cached_doc pairs a missing generation with an error
        etag = _etag(generation)
        if self._is_fresh(etag):
            self._send_not_modified(etag)
        else:
            self._send_json(200, {"now": now, section: doc.get(section)}, _fresh_headers(etag, now))

    def _serve_board(self) -> None:
        query = self._query()
        repo = query.get("repo") or None
        state = query.get("state") or None
        label = query.get("label") or None
        raw_age = query.get("max_age") or None
        max_age = None
        if raw_age is not None:
            try:
                max_age = float(raw_age)
            except ValueError:
                self._send_json(400, {"error": f"max_age {raw_age!r} is not seconds"})
                return
        limit, problem = _page_int(query.get("limit") or None, "limit", BOARD_LIMIT_DEFAULT)
        if problem is None:
            offset, problem = _page_int(query.get("offset") or None, "offset", 0)
        else:
            offset = 0
        if problem is not None:
            self._send_json(400, {"error": problem})
            return
        limit = min(limit, BOARD_LIMIT_MAX)
        now = self.server_state.clock()
        opener = self.server_state.board_open or board.open_store
        try:
            with opener() as store:
                listed = store.items(repo=repo, state=state, label=label)
                rows = []
                for got in listed:
                    item, fresh = store.item(got.repo, got.number, now=now, max_age=max_age)
                    if item is None:
                        continue
                    rows.append(_board_row(item, fresh, store.claim_of(item.repo, item.number)))
        except Exception as exc:  # noqa: BLE001 - a request never drops the connection
            print(f"corral serve: {snapshot.error_text(exc)}", file=sys.stderr)
            self._send_json(500, {"error": "board unavailable"})
            return
        self._send_json(
            200,
            {
                "now": now,
                "board": rows[offset : offset + limit],
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            },
        )

    def _serve_tail(self) -> None:
        query = self._query()
        repo = query.get("repo") or None
        raw_number = query.get("number") or None
        if repo is None or raw_number is None:
            self._send_json(400, {"error": "tail needs ?repo= and ?number="})
            return
        try:
            number = int(raw_number)
        except ValueError:
            self._send_json(400, {"error": f"number {raw_number!r} is not an integer"})
            return
        limit = self.server_state.tail_max
        raw_last = query.get("last") or None
        if raw_last is not None:
            try:
                limit = int(raw_last)
            except ValueError:
                self._send_json(400, {"error": f"last {raw_last!r} is not an integer"})
                return
            if limit < 1:
                self._send_json(400, {"error": f"last {raw_last!r} must be 1 or more"})
                return
        limit = min(limit, self.server_state.tail_max)
        reader = self.server_state.tail_fn or read_tail
        try:
            text = reader(repo, number, limit)
        except (launch.LaunchError, settings.SettingsError):
            self._send_json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - a request never drops the connection
            print(f"corral serve: {snapshot.error_text(exc)}", file=sys.stderr)
            self._send_json(500, {"error": "tail unavailable"})
        else:
            self._send_text(200, text)

    def do_GET(self) -> None:
        self._count()
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        if path == "/v1/snapshot":
            self._serve_snapshot()
        elif path == "/v1/agents":
            self._serve_agents()
        elif path == "/v1/quota":
            self._serve_section("quota")
        elif path == "/v1/capacity":
            self._serve_section("capacity")
        elif path == "/v1/disks":
            self._serve_section("disk")
        elif path == "/v1/board":
            self._serve_board()
        elif path == "/v1/tail":
            self._serve_tail()
        elif path == "/v1/version":
            self._send_json(200, {"api": API_VERSION, "schema_version": snapshot.SCHEMA_VERSION})
        elif path == "/v1/health":
            self._serve_health()
        else:
            self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        self._count()
        origin = self.headers.get("Origin")
        wanted = self.headers.get("Access-Control-Request-Method")
        if origin is not None and wanted is not None and self._echo_origin() is not None:
            self._send_empty(
                204,
                (
                    ("Access-Control-Allow-Origin", origin),
                    ("Access-Control-Allow-Methods", "GET"),
                    ("Access-Control-Allow-Headers", "Authorization"),
                ),
            )
            return
        if not self._authorized():
            return
        self._send_json(405, {"error": f"method {self.command} not allowed"})

    def _method_not_allowed(self) -> None:
        self._count()
        if not self._authorized():
            return
        self._send_json(405, {"error": f"method {self.command} not allowed"})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_HEAD = _method_not_allowed

    def __getattr__(self, name: str):
        # Any other `do_*` method answers 405: v1 registers GET only.
        # Scoped to `do_` so only method handlers resolve this way.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)


def make_server(
    build: Callable[[], dict] | None = None,
    port: int = 0,
    clock: Callable[[], float] | None = None,
    quiet: bool = False,
    bind: str = "127.0.0.1",
    allowed_origins: tuple[str, ...] = (),
    token_required: bool = False,
    token_reader: Callable[[], str | None] | None = None,
    board_open: Callable[[], board.Store] | None = None,
    tail_fn: Callable[[str, int, int], str] | None = None,
    tail_max: int = 200,
    snapshot_ttl_s: float = SNAPSHOT_TTL_S_DEFAULT,
) -> ThreadingHTTPServer:
    """An HTTP server answering the v1 routes.

    `build` returns the snapshot document (`collect` by default, the
    same readers the `snapshot` verb uses). Health reports the host the
    built document names. `bind` defaults to loopback and `port` to an
    ephemeral one; the bound address reads as `server.server_address`.
    `board_open` opens the board store per board request (`board.open_store`
    by default); `tail_fn` renders tail output (`read_tail` by default),
    capped at `tail_max` lines. Snapshot builds are shared between
    requests inside `snapshot_ttl_s` seconds, and each rebuild bumps a
    generation counter served as the `ETag`.
    The caller enforces the bind rules; `token_required` with a
    `token_reader` enforces the bearer token per request.
    """
    state = _State(
        build=build if build is not None else collect,
        clock=clock if clock is not None else time.time,
        quiet=quiet,
        allowed_origins=allowed_origins,
        token_required=token_required,
        token_reader=token_reader,
        board_open=board_open,
        tail_fn=tail_fn,
        tail_max=tail_max,
        snapshot_ttl_s=snapshot_ttl_s,
    )

    class _BoundHandler(_Handler):
        server_state = state

    class _BoundServer(ThreadingHTTPServer):
        address_family = server_family(bind)

    server = _BoundServer((bind, port), _BoundHandler)
    server.daemon_threads = True
    return server


def collect() -> dict:
    """Build the snapshot document from the host's local readers.

    The production builder: the shared `snapshot.collect_kwargs`
    helper, so the API and the CLI can never disagree. Reads files
    only: no `gh`, no network, no service control.
    """
    return snapshot.build(**snapshot.collect_kwargs())


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the `serve` verb."""
    from corral import paths

    try:
        resolved = settings.load()
    except (settings.SettingsError, paths.PathError) as err:
        _err(str(err))
        return USAGE
    try:
        token_path = paths.api_token_path()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    bind = args.bind or resolved["serve.bind"]
    port = args.port if args.port is not None else resolved["serve.port"]
    origins = list(resolved["serve.origins"]) + list(args.allow_origin or ())
    remote = not is_loopback(bind)
    if args.new_token:
        try:
            mint_token(token_path)
        except ServeError as err:
            _err(str(err))
            return USAGE
    try:
        have_token = read_token(token_path) is not None
    except ServeError as err:
        _err(str(err))
        return USAGE
    problem = check_origins(origins)
    if problem is None:
        problem = check_bind(bind, port, allow_remote=args.allow_remote, has_token=have_token)
    if problem is not None:
        _err(problem)
        return USAGE
    warning = token_file_warning(token_path)
    if warning is not None:
        _err(warning)
        if remote:
            return USAGE
    try:
        state_dir = paths.state_dir()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    try:
        os.listdir(state_dir)
    except OSError as err:
        _err(f"cannot read state dir {state_dir}: {err}")
        return UNREACHABLE
    try:
        server = make_server(
            bind=bind,
            port=port,
            allowed_origins=tuple(origins),
            token_required=remote,
            token_reader=lambda: read_token(token_path),
            tail_max=resolved["serve.tail_max"],
            snapshot_ttl_s=resolved["serve.snapshot_ttl_s"],
        )
    except OSError as err:
        if err.errno == errno.EADDRINUSE:
            _err(f"serve bind {bind}:{port} is already in use")
            return RACE
        _err(f"cannot bind serve to {bind}:{port}: {err.strerror or err}")
        return USAGE
    host, bound, *_ = server.server_address
    print(f"listening on {host}:{bound}")

    def _stop(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return OK


def add_parser(sub) -> None:
    """Register `serve` on the top-level subparsers."""
    p = sub.add_parser(
        "serve",
        help="run the read-only dashboard API on loopback (a wider bind needs --allow-remote and a token)",
    )
    p.add_argument("--bind", default=None, help="address to bind (default serve.bind, 127.0.0.1)")
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (default serve.port, 0 for an ephemeral port)",
    )
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback bind (needs a token file)",
    )
    p.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        help="dashboard origin allowed cross-origin (repeatable, exact match, no wildcards)",
    )
    p.add_argument(
        "--new-token",
        action="store_true",
        help="mint the API token file, then serve",
    )
    p.set_defaults(func=cmd_serve)
