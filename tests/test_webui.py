"""W1: static serving and the shell (host mode).

Offline: a real HTTP server on a loopback ephemeral port, spoken to
with `http.client`, plus source-discipline reads over the packaged
`korapy/web/` files. No `gh`, no network beyond loopback, no real
state dirs.
"""

import hashlib
import http.client
import io
import json
import re
import threading
import unittest
from contextlib import redirect_stderr
from importlib import resources

from korapy import serve

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)

WEB_FILES = ("index.html", "app.css", "app.js", "api.js", "fmt.js", "views.js")

CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "fmt.js": "text/javascript; charset=utf-8",
    "views.js": "text/javascript; charset=utf-8",
}


def web_bytes(name):
    return resources.files("korapy").joinpath(f"web/{name}").read_bytes()


def trivial_doc():
    return {"schema_version": 1}


class WebCase(unittest.TestCase):
    """A live loopback server per test, shut down on cleanup."""

    def start_server(self, build, **kwargs):
        kwargs.setdefault("quiet", True)
        server = serve.make_server(build=build, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.daemon = True
        thread.start()
        self.addCleanup(self._stop, server, thread)
        return server

    def _stop(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    def fetch(self, server, method, target, headers=None):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, target, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()


class AssetTable(unittest.TestCase):
    def test_every_expected_file_loads_non_empty_utf8(self):
        from korapy import webui

        assets = webui.load_assets()
        want = ["/"] + [f"/ui/{name}" for name in WEB_FILES if name != "index.html"]
        self.assertEqual(sorted(assets), sorted(want))
        for path, asset in assets.items():
            self.assertTrue(asset.data, path)
            asset.data.decode("utf-8")

    def test_content_types_are_exact(self):
        from korapy import webui

        assets = webui.load_assets()
        for name, want in CONTENT_TYPES.items():
            key = "/" if name == "index.html" else f"/ui/{name}"
            self.assertEqual(assets[key].content_type, want, key)

    def test_etag_is_the_truncated_content_hash(self):
        from korapy import webui

        assets = webui.load_assets()
        for path, asset in assets.items():
            want = '"' + hashlib.sha256(asset.data).hexdigest()[:32] + '"'
            self.assertEqual(asset.etag, want, path)

    def test_config_body_carries_mode_and_api_only(self):
        from korapy import webui

        body = json.loads(webui.config_body(serve.API_VERSION))
        self.assertEqual(body, {"mode": "host", "api": serve.API_VERSION})

    def test_config_path_is_one_constant(self):
        import pathlib
        from importlib.util import find_spec

        from korapy import webui

        self.assertEqual(webui.CONFIG_PATH, "/ui/config.json")
        self.assertTrue(webui.is_ui_path(webui.CONFIG_PATH))
        spec = find_spec("korapy.serve")
        assert spec is not None and spec.origin is not None
        text = pathlib.Path(spec.origin).read_text()
        self.assertIn("webui.CONFIG_PATH", text)
        self.assertNotIn('"/ui/config.json"', text)


class DocumentServing(WebCase):
    def test_root_returns_index_bytes_with_security_headers(self):
        server = self.start_server(trivial_doc)
        status, headers, body = self.fetch(server, "GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, web_bytes("index.html"))
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(headers.get("Content-Security-Policy"), CSP)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        self.assertIn("ETag", headers)

    def test_each_asset_returns_table_bytes_with_security_headers(self):
        server = self.start_server(trivial_doc)
        for name in ("app.css", "app.js", "api.js", "fmt.js", "views.js"):
            status, headers, body = self.fetch(server, "GET", f"/ui/{name}")
            self.assertEqual(status, 200, name)
            self.assertEqual(body, web_bytes(name), name)
            self.assertEqual(headers.get("Content-Type"), CONTENT_TYPES[name], name)
            self.assertEqual(headers.get("Content-Security-Policy"), CSP, name)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", name)
            self.assertEqual(headers.get("Cache-Control"), "no-cache", name)
            self.assertIn("ETag", headers, name)

    def test_conditional_get_yields_304_with_empty_body(self):
        server = self.start_server(trivial_doc)
        _, headers, _ = self.fetch(server, "GET", "/")
        etag = headers["ETag"]
        status, _, body = self.fetch(server, "GET", "/", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        status, _, body = self.fetch(
            server, "GET", "/ui/app.js", headers={"If-None-Match": '"no-such-etag"'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body)

    def test_unknown_and_traversal_paths_under_ui_are_404(self):
        server = self.start_server(trivial_doc)
        for target in (
            "/ui/nope.js",
            "/ui/../serve.py",
            "/ui/%2e%2e/serve.py",
            "/ui//app.js",
            "/ui\\.app.js",
            "/ui/app.js/",
        ):
            status, _, body = self.fetch(server, "GET", target)
            self.assertEqual(status, 404, target)
            self.assertEqual(json.loads(body), {"error": "not found"})

    def test_config_returns_mode_and_the_served_api_version(self):
        server = self.start_server(trivial_doc)
        status, headers, body = self.fetch(server, "GET", "/ui/config.json")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload, {"mode": "host", "api": serve.API_VERSION})
        self.assertNotIn("schema_version", payload)
        _, _, version_body = self.fetch(server, "GET", "/v1/version")
        self.assertEqual(payload["api"], json.loads(version_body)["api"])
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")


class MethodPrecedence(WebCase):
    def test_non_get_on_ui_paths_is_405_with_or_without_a_token(self):
        for token in (None, "tok-1"):
            server = self.start_server(
                trivial_doc,
                token_required=True,
                token_reader=lambda: "tok-1",
            )
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                status, _, body = self.fetch(server, method, "/", headers=headers)
                self.assertEqual(status, 405, (method, token))
                if method != "HEAD":  # HEAD answers carry no body by HTTP rule
                    self.assertIn("error", json.loads(body))
            status, _, _ = self.fetch(server, "POST", "/ui/app.js", headers=headers)
            self.assertEqual(status, 405)

    def test_options_on_ui_paths_is_405_even_as_a_preflight(self):
        origin = "https://dash.example.com"
        server = self.start_server(trivial_doc, allowed_origins=(origin,))
        headers = {"Origin": origin, "Access-Control-Request-Method": "GET"}
        for target in ("/", "/ui/app.js", "/ui/config.json"):
            status, resp_headers, body = self.fetch(server, "OPTIONS", target, headers=headers)
            self.assertEqual(status, 405, target)
            self.assertNotIn("Access-Control-Allow-Methods", resp_headers)
            self.assertIn("error", json.loads(body))


class ShellExemption(WebCase):
    def test_assets_and_config_skip_auth_while_data_needs_it(self):
        server = self.start_server(
            trivial_doc,
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        for target in ("/", "/ui/app.js", "/ui/config.json"):
            status, _, _ = self.fetch(server, "GET", target)
            self.assertEqual(status, 200, target)
        status, _, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        status, _, _ = self.fetch(
            server, "GET", "/v1/snapshot", headers={"Authorization": "Bearer tok-1"}
        )
        self.assertEqual(status, 200)


class JsonHardening(WebCase):
    def test_api_responses_carry_nosniff(self):
        server = self.start_server(trivial_doc)
        for target in ("/v1/version", "/v1/snapshot", "/v1/health", "/nope"):
            status, headers, _ = self.fetch(server, "GET", target)
            self.assertIn(status, (200, 404))
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", target)


class DegradedUi(WebCase):
    def test_empty_asset_table_is_500_on_ui_routes_only(self):
        server = self.start_server(trivial_doc, web_files={})
        err = io.StringIO()
        with redirect_stderr(err):
            for target in ("/", "/ui/app.js", "/ui/config.json"):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 500, target)
                self.assertEqual(json.loads(body), {"error": serve.UI_UNAVAILABLE})
        # A missing dashboard must not send the operator to snapshot
        # debugging: the two generic bodies stay distinct.
        self.assertNotEqual(serve.UI_UNAVAILABLE, serve.UNAVAILABLE)
        self.assertIn("dashboard", err.getvalue())
        status, _, _ = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 200)


class VersionContract(WebCase):
    def test_version_carries_the_numbers_the_shell_reads(self):
        server = self.start_server(trivial_doc)
        status, _, body = self.fetch(server, "GET", "/v1/version")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsInstance(payload["api"], int)
        self.assertIsInstance(payload["schema_version"], int)


class SourceDiscipline(unittest.TestCase):
    def test_no_dangerous_sinks_or_external_references_in_js(self):
        for name in ("app.js", "api.js", "fmt.js", "views.js"):
            text = web_bytes(name).decode("utf-8")
            self.assertNotIn("innerHTML", text, name)
            self.assertNotIn("eval(", text, name)
            self.assertNotIn("document.write", text, name)
            if name == "views.js":
                # The one allowed URL-shaped literal: the SVG element
                # namespace for the quota bars, an identifier that is
                # never fetched. Exactly one site may name it.
                self.assertEqual(text.count('"http://www.w3.org/2000/svg"'), 1)
                text = text.replace('"http://www.w3.org/2000/svg"', "")
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)

    def test_no_inline_style_vector_in_js(self):
        for name in ("app.js", "api.js", "fmt.js", "views.js"):
            text = web_bytes(name).decode("utf-8")
            self.assertNotIn(".style", text, name)
            self.assertNotIn("style=", text, name)

    def test_no_external_references_in_html_or_css(self):
        for name in ("index.html", "app.css"):
            text = web_bytes(name).decode("utf-8")
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)

    def test_shell_has_no_inline_code_or_handlers(self):
        text = web_bytes("index.html").decode("utf-8")
        scripts = re.findall(r"<script\b[^>]*>", text)
        self.assertEqual(len(scripts), 1)
        self.assertIn('src="/ui/app.js"', scripts[0])
        self.assertIn('type="module"', scripts[0])
        self.assertNotIn("<style", text)
        self.assertIsNone(re.search(r"\son\w+\s*=", text))

    def test_only_the_token_key_touches_storage(self):
        found = set()
        for name in ("app.js", "api.js", "fmt.js", "views.js"):
            text = web_bytes(name).decode("utf-8")
            found.update(re.findall(r"korapy\.[A-Za-z_]+", text))
        self.assertEqual(found, {"korapy.token"})

    def test_volatile_keys_match_the_server(self):
        text = web_bytes("fmt.js").decode("utf-8")
        # Parse the Set literal itself: collecting only the known
        # names would pass with an extra member sitting beside them.
        found = re.search(r"VOLATILE_KEYS = new Set\(\[([^\]]*)\]\)", text)
        self.assertIsNotNone(found)
        assert found is not None
        names = set(re.findall(r'"([^"]+)"', found.group(1)))
        self.assertEqual(names, set(serve.VOLATILE_KEYS))

    def test_bundle_stays_small(self):
        total = 0
        for name in WEB_FILES:
            size = len(web_bytes(name))
            self.assertLessEqual(size, 60 * 1024, name)
            total += size
        self.assertLessEqual(total, 120 * 1024)

    def test_poll_backoff_table_is_pinned(self):
        text = web_bytes("api.js").decode("utf-8")
        self.assertIn("export const POLL_MS = 5000;", text)
        self.assertIn("export const BACKOFF_MS = [5000, 15000, 30000, 60000];", text)
        self.assertIn("export function nextDelay(failures) {", text)
        self.assertIn("Math.min(failures - 1, BACKOFF_MS.length - 1)", text)

    def test_every_poll_await_sits_inside_a_try(self):
        text = web_bytes("app.js").decode("utf-8")
        for name in ("loadSnapshot", "loadConfig", "loadVersion", "loadTail"):
            sites = list(re.finditer(r"await\s+" + name + r"\s*\(", text))
            self.assertTrue(sites, name)
            for site in sites:
                start = site.start()
                self.assertGreater(
                    text.rfind("try {", 0, start), text.rfind("catch", 0, start), name
                )

    def test_schedule_uses_the_shared_next_delay(self):
        app = web_bytes("app.js").decode("utf-8")
        self.assertIn("nextDelay(failures)", app)
        # Word boundaries: the tail cadence TAIL_POLL_MS is an api.js
        # import, not a number restated in app.js.
        self.assertIsNone(re.search(r"\bPOLL_MS\b", app))
        self.assertIsNone(re.search(r"\bBACKOFF_MS\b", app))

    def test_schedule_runs_only_from_a_finally(self):
        # The poll loop is structurally unable to die: the single
        # reschedule sits in tick's finally, so any throw above it
        # still rearms the loop.
        app = web_bytes("app.js").decode("utf-8")
        sites = list(re.finditer(r"schedule\(config, token\);", app))
        self.assertEqual(len(sites), 1)
        start = sites[0].start()
        self.assertGreater(app.rfind("finally {", 0, start), app.rfind("catch", 0, start))

    def test_draw_failure_is_visible_and_counts_a_failure(self):
        # A render throw (a new server shape) must read as a failure
        # with a visible notice, never as a live page over stale data.
        app = web_bytes("app.js").decode("utf-8")
        at = app.find("Could not draw this update")
        self.assertGreater(at, -1)
        window = app[max(0, at - 400) : at]
        self.assertIn("failures += 1", window)
        self.assertIn("renderConnection({ live: false, seenMs })", window)

    def test_stopped_state_drops_the_compare_cache(self):
        # Entering the stopped gate must clear the fingerprint, or the
        # compare-and-skip path redraws nothing when the version
        # matches again and an empty page claims to be live.
        app = web_bytes("app.js").decode("utf-8")
        self.assertEqual(app.count("renderStopped("), 1)
        at = app.find("renderStopped(")
        start = app.rfind("function enterStopped", 0, at)
        self.assertGreater(start, -1)
        block = app[start:at]
        self.assertIn("lastFingerprint = null", block)
        self.assertIn("lastDoc = null", block)

    def test_usage_triple_tolerates_a_null_share(self):
        # A null by_project share on a new server shape must read as
        # unknown, not throw inside the poll loop.
        text = web_bytes("views.js").decode("utf-8")
        start = text.find("function usageTriple(")
        self.assertGreater(start, -1)
        body = text[start : text.find("\n}\n", start)]
        self.assertIn("typeof triple", body)

    def test_pressure_degrades_to_verbatim(self):
        # A non-numeric pressure reading must read as its raw value,
        # the timestamp formatters' rule, not NaN.
        text = web_bytes("views.js").decode("utf-8")
        start = text.find("function pressureValue(")
        self.assertGreater(start, -1)
        body = text[start : text.find("\n}\n", start)]
        self.assertIn("Number.isFinite", body)
        self.assertIn("String(value)", body)
        view = web_bytes("views.js").decode("utf-8")
        at = view.find("function pressureText(")
        self.assertGreater(at, -1)
        section = view[at : view.find("\n}\n", at)]
        self.assertIn("pressureValue(info.load_per_cpu, 2)", section)
        self.assertIn("pressureValue(info.mem_available_pct, 1)", section)

    def test_fetch_maps_network_failure_to_the_failure_branch(self):
        text = web_bytes("api.js").decode("utf-8")
        self.assertIn("return { status: 0, data: null };", text)

    def test_token_writes_are_guarded(self):
        text = web_bytes("api.js").decode("utf-8")
        for call in (
            "localStorage.getItem",
            "localStorage.setItem",
            "localStorage.removeItem",
        ):
            at = text.find(call)
            self.assertGreater(at, -1, call)
            self.assertGreater(text.rfind("try {", 0, at), text.rfind("catch", 0, at), call)

    def test_unstorable_token_is_said_on_screen(self):
        app = web_bytes("app.js").decode("utf-8")
        self.assertIn("cannot keep this token", app)

    def test_forget_failure_keeps_the_token_instead_of_reloading(self):
        app = web_bytes("app.js").decode("utf-8")
        self.assertIsNotNone(re.search(r"if\s*\(\s*forgetToken\(\)", app))
        self.assertIn("cannot forget this token", app)

    def test_el_warns_about_unchecked_hrefs(self):
        text = web_bytes("views.js").decode("utf-8")
        self.assertIn("W6", text)
        self.assertIn("scheme", text)

    def test_overview_entry_points_exist(self):
        api = web_bytes("api.js").decode("utf-8")
        for name in (
            "export function loadSnapshot(",
            "export function snapshotProjects(",
            "export function isErrorSection(",
            "export function isSupportedDocument(",
            "export function isSupportedApi(",
            "export const VERSION_CHECK_MS = 60000;",
        ):
            self.assertIn(name, api)
        views = web_bytes("views.js").decode("utf-8")
        for name in (
            "export function renderOverview(",
            "export function renderChips(",
            "export function renderStopped(",
        ):
            self.assertIn(name, views)
        fmt = web_bytes("fmt.js").decode("utf-8")
        self.assertIn("export function canonicalJson(", fmt)
        self.assertIn("export function formatClock(", fmt)

    def test_compare_and_skip_has_one_stable_path(self):
        app = web_bytes("app.js").decode("utf-8")
        self.assertIn("canonicalJson(stripVolatile(reply.data))", app)
        # Two render sites: the poll compare path and the hashchange
        # filter re-render over the last good payload, never a third.
        self.assertEqual(app.count("renderOverview("), 2)

    def test_shell_schema_expectation_matches_the_server(self):
        from korapy import snapshot

        app = web_bytes("app.js").decode("utf-8")
        found = re.search(r"const EXPECTED_SCHEMA_VERSION = (\d+);", app)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(int(found.group(1)), snapshot.SCHEMA_VERSION)

    def test_version_gate_says_out_of_date(self):
        api = web_bytes("api.js").decode("utf-8")
        self.assertIn("export const VERSION_CHECK_MS = 60000;", api)
        app = web_bytes("app.js").decode("utf-8")
        self.assertIn("VERSION_CHECK_MS", app)
        self.assertIn("out of date for this server", app)
        self.assertIn("Unsupported document schema", app)

    def test_error_sections_render_as_error_cards(self):
        views = web_bytes("views.js").decode("utf-8")
        self.assertIn("isErrorSection", views)
        self.assertIn('"card error"', views)

    def test_chips_are_encoded_hash_links(self):
        views = web_bytes("views.js").decode("utf-8")
        self.assertIn('"All"', views)
        self.assertIn('"#/?project=" + encodeURIComponent(name)', views)
        html = web_bytes("index.html").decode("utf-8")
        for anchor in ('id="chips"', 'id="notice"', 'id="cards"', 'id="agents"'):
            self.assertIn(anchor, html)

    def test_quota_bars_are_svg_attributes(self):
        views = web_bytes("views.js").decode("utf-8")
        self.assertIn("createElementNS", views)
        self.assertIn('setAttribute("width"', views)


if __name__ == "__main__":
    unittest.main()
