"""The static dashboard shell both JSON servers read from one table.

W1 covers the asset table, the document content, the config route and
the security header values for host mode; hub mode reuses this module
unchanged. A pure core: functions over plain data, no handler imports,
so `serve` and the hub cannot drift. Bytes load through
`importlib.resources` once at server construction behind an injectable
seam (callers pass raw file bytes); an empty table means the assets
failed to load and every UI route must answer the generic 500.
"""

import hashlib
import json
from dataclasses import dataclass
from importlib import resources

#: The files that ship the dashboard, and the only names the UI routes
#: ever serve. `index.html` answers `/`; the rest answer `/ui/<name>`.
WEB_FILES = ("index.html", "app.css", "app.js", "api.js", "fmt.js", "views.js")

#: The generated backend-identity route. One constant, read by the
#: asset table check and the host handler alike, so the path cannot
#: drift between the two.
CONFIG_PATH = "/ui/config.json"

#: Exact content type per shipped file. JS rides as `text/javascript`,
#: the one type every browser module script accepts.
CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "fmt.js": "text/javascript; charset=utf-8",
    "views.js": "text/javascript; charset=utf-8",
    "config.json": "application/json",
}

#: Sent on the document and every asset: same-origin only, no plugins,
#: no framing, no outside connection target for a future bug to use.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)

#: Legacy clickjacking cover beside `frame-ancestors 'none'`.
FRAME_OPTIONS = "DENY"

#: Tracker links carry nothing out; also sent on JSON config.
REFERRER_POLICY = "no-referrer"

#: Sent on everything, UI and API alike, so a directly navigated
#: `/v1/tail` text body can never be sniffed into markup.
NO_SNIFF = "nosniff"

#: How many hex characters of the SHA-256 carry the strong ETag.
ETAG_HEX = 32


@dataclass(frozen=True)
class Asset:
    """One servable file: its bytes, exact content type and ETag."""

    data: bytes
    content_type: str
    etag: str


def etag_for(data: bytes) -> str:
    """The strong ETag for `data`: the quoted truncated content hash."""
    return '"' + hashlib.sha256(data).hexdigest()[:ETAG_HEX] + '"'


def build_assets(files: dict[str, bytes]) -> dict[str, Asset]:
    """The servable table for raw `files` keyed by bare file name.

    `index.html` maps to `/`; every other known name maps to
    `/ui/<name>`. Unknown names never enter the table, and lookup is
    by exact key, so traversal shapes can never resolve.
    """
    table = {}
    for name, data in files.items():
        if name not in CONTENT_TYPES or name in ("config.json",):
            continue
        key = "/" if name == "index.html" else f"/ui/{name}"
        table[key] = Asset(data=data, content_type=CONTENT_TYPES[name], etag=etag_for(data))
    return table


def load_assets(files: dict[str, bytes] | None = None) -> dict[str, Asset]:
    """The servable table, from packaged bytes or an injected dict.

    With no argument the bytes come from `importlib.resources`, the
    same pattern the prompts and hooks use. Tests pass a dict instead;
    an empty one yields an empty table, the degraded-UI signal.
    """
    if files is None:
        files = {
            name: resources.files("corral").joinpath(f"web/{name}").read_bytes()
            for name in WEB_FILES
        }
    return build_assets(files)


def config_body(api: int, mode: str = "host") -> bytes:
    """The `/ui/config.json` document: backend mode and API major only.

    It deliberately carries no schema number and no secrets; it tells
    identical shell bytes which backend they face before any token
    exists, so no request is spent probing for routes.
    """
    return json.dumps({"mode": mode, "api": api}).encode("utf-8")


def asset_headers(asset: Asset) -> tuple:
    """The headers for a 200 asset reply: type, ETag and the UI policy."""
    return (
        ("Content-Type", asset.content_type),
        ("Cache-Control", "no-cache"),
        ("ETag", asset.etag),
        ("Content-Security-Policy", CSP),
        ("X-Content-Type-Options", NO_SNIFF),
        ("X-Frame-Options", FRAME_OPTIONS),
        ("Referrer-Policy", REFERRER_POLICY),
    )


def config_headers() -> tuple:
    """The headers for `/ui/config.json`: generated, never cached."""
    return (
        ("Content-Type", CONTENT_TYPES["config.json"]),
        ("Cache-Control", "no-store"),
        ("Content-Security-Policy", CSP),
        ("X-Content-Type-Options", NO_SNIFF),
        ("X-Frame-Options", FRAME_OPTIONS),
        ("Referrer-Policy", REFERRER_POLICY),
    )


def is_ui_path(path: str) -> bool:
    """Whether `path` (already split from any query) is a UI route."""
    return path == "/" or path == CONFIG_PATH or path.startswith("/ui/")
