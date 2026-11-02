"""W1: static serving and the shell (host mode).

Offline: a real HTTP server on a loopback ephemeral port, spoken to
with `http.client`, plus source-discipline reads over the packaged
`corral/web/` files. No `gh`, no network beyond loopback, no real
state dirs.
"""

import hashlib
import http.client
import json
import re
import threading
import unittest
from importlib import resources

from corral import serve

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
    return resources.files("corral").joinpath(f"web/{name}").read_bytes()


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
        from corral import webui

        assets = webui.load_assets()
        want = ["/"] + [f"/ui/{name}" for name in WEB_FILES if name != "index.html"]
        self.assertEqual(sorted(assets), sorted(want))
        for path, asset in assets.items():
            self.assertTrue(asset.data, path)
            asset.data.decode("utf-8")

    def test_content_types_are_exact(self):
        from corral import webui

        assets = webui.load_assets()
        for name, want in CONTENT_TYPES.items():
            key = "/" if name == "index.html" else f"/ui/{name}"
            self.assertEqual(assets[key].content_type, want, key)

    def test_etag_is_the_truncated_content_hash(self):
        from corral import webui

        assets = webui.load_assets()
        for path, asset in assets.items():
            want = '"' + hashlib.sha256(asset.data).hexdigest()[:32] + '"'
            self.assertEqual(asset.etag, want, path)

    def test_config_body_carries_mode_and_api_only(self):
        from corral import webui

        body = json.loads(webui.config_body(serve.API_VERSION))
        self.assertEqual(body, {"mode": "host", "api": serve.API_VERSION})


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
            for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD"):
                status, _, body = self.fetch(server, method, "/", headers=headers)
                self.assertEqual(status, 405, (method, token))
                if method != "HEAD":  # HEAD answers carry no body by HTTP rule
                    self.assertIn("error", json.loads(body))
            status, _, _ = self.fetch(server, "POST", "/ui/app.js", headers=headers)
            self.assertEqual(status, 405)


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
        for target in ("/", "/ui/app.js", "/ui/config.json"):
            status, _, body = self.fetch(server, "GET", target)
            self.assertEqual(status, 500, target)
            self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})
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
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)

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
            found.update(re.findall(r"corral\.[A-Za-z_]+", text))
        self.assertEqual(found, {"corral.token"})

    def test_volatile_keys_match_the_server(self):
        text = web_bytes("fmt.js").decode("utf-8")
        names = set(re.findall(r'"(now|read_at)"', text))
        self.assertEqual(names, set(serve.VOLATILE_KEYS))

    def test_bundle_stays_small(self):
        total = 0
        for name in WEB_FILES:
            size = len(web_bytes(name))
            self.assertLessEqual(size, 60 * 1024, name)
            total += size
        self.assertLessEqual(total, 120 * 1024)


if __name__ == "__main__":
    unittest.main()
