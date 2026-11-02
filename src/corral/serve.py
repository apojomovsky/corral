"""Read-only loopback HTTP API over the fleet state (`corral serve`).

S1 covers the request router, `GET /v1/snapshot` from `snapshot.build`,
`/v1/version`, `/v1/health`, the JSON error shape and the 405/404 rules.
Every endpoint reads local files through one injected builder, so the
API can never disagree with the CLI. No `gh`, no network, no service
control in this module.
"""

import json
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from corral import snapshot

#: The API major version, carried in the path (`/v1/`) and `/v1/version`.
API_VERSION = 1

#: Snapshot sections whose `{"error"}` body marks the server degraded.
ERROR_SECTIONS = ("agents", "quota", "capacity", "disk", "drive")

#: The generic 500 body: the detail goes to stderr, never the wire.
UNAVAILABLE = "snapshot unavailable"


class _State:
    """One server's injected builder, clock, and counters."""

    def __init__(
        self,
        build: Callable[[], dict],
        clock: Callable[[], float],
        quiet: bool,
    ) -> None:
        self.build = build
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.total = 0
        self.not_modified = 0
        self.quiet = quiet


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

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_doc(self) -> tuple[dict | None, str | None]:
        try:
            return self.server_state.build(), None
        except Exception as exc:  # noqa: BLE001 - a request never drops the connection
            text = str(exc) if str(exc) else repr(exc)
            print(f"corral serve: {text}", file=sys.stderr)
            return None, UNAVAILABLE

    def _serve_snapshot(self) -> None:
        doc, error = self._read_doc()
        if error is not None:
            self._send_json(500, {"error": error})
        else:
            assert doc is not None
            self._send_json(200, doc)

    def _serve_health(self) -> None:
        state = self.server_state
        now = state.clock()
        doc, error = self._read_doc()
        if error is not None:
            self._send_json(500, {"error": error})
            return
        assert doc is not None
        degraded = sorted(
            name
            for name in ERROR_SECTIONS
            if isinstance(doc.get(name), dict) and set(doc[name]) == {"error"}
        )
        header = doc.get("fleet_header")
        host = header.get("host") if isinstance(header, dict) else None
        with state.lock:
            total = state.total
            not_modified = state.not_modified
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
        )

    def do_GET(self) -> None:
        self._count()
        path = urlsplit(self.path).path
        if path == "/v1/snapshot":
            self._serve_snapshot()
        elif path == "/v1/version":
            self._send_json(200, {"api": API_VERSION, "schema_version": snapshot.SCHEMA_VERSION})
        elif path == "/v1/health":
            self._serve_health()
        else:
            self._send_json(404, {"error": "not found"})

    def _method_not_allowed(self) -> None:
        self._count()
        self._send_json(405, {"error": f"method {self.command} not allowed"})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed

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
) -> ThreadingHTTPServer:
    """A loopback-only `ThreadingHTTPServer` answering the v1 routes.

    `build` returns the snapshot document (`collect` by default, the
    same readers the `snapshot` verb uses). Health reports the host the
    built document names. `port` defaults to an ephemeral one; the
    bound port reads as `server.server_address[1]`.
    """
    state = _State(
        build=build if build is not None else collect,
        clock=clock if clock is not None else time.time,
        quiet=quiet,
    )

    class _BoundHandler(_Handler):
        server_state = state

    server = ThreadingHTTPServer(("127.0.0.1", port), _BoundHandler)
    server.daemon_threads = True
    return server


def collect() -> dict:
    """Build the snapshot document from the host's local readers.

    The production builder: the shared `snapshot.collect_kwargs`
    helper, so the API and the CLI can never disagree. Reads files
    only: no `gh`, no network, no service control.
    """
    return snapshot.build(**snapshot.collect_kwargs())
