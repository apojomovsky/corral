"""`korapy serve` S1: routes and the snapshot.

Offline: a real HTTP server on a loopback ephemeral port, spoken to
with `http.client`. Snapshot builders are fakes or `snapshot.build`
over canned views. No `gh`, no systemd, no network beyond loopback,
no real state dirs.
"""

import argparse
import ast
import copy
import http.client
import inspect
import io
import json
import os
import pathlib
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from korapy import (
    board,
    cli,
    coord,
    coordevents,
    hub,
    launch,
    paths,
    projects,
    serve,
    settings,
    snapshot,
)
from tests.test_snapshot import (
    HOST,
    T0,
    config,
    doc_limits,
    doc_pressure,
    entry,
    quota_view,
    resolved_settings,
    usage,
    view,
)

DRIVE = {
    "last_pass_at": T0,
    "last_exit": 0,
    "started": 0,
    "skipped_by_reason": {},
    "released": 0,
    "gave_up": 0,
    "starts_today": 0,
    "max_starts_per_day": 4,
    "starts_remaining": 4,
    "paused": False,
    "pause_reason": None,
    "lock_held": False,
}


def healthy_doc():
    """A fully readable snapshot document from canned views."""
    rows = view((entry("a1"), "live"))
    resources_view = snapshot.resources_section(
        rows,
        HOST,
        doc_limits(),
        doc_pressure(),
        [],
        lambda *args: None,
        live_by_owner={"proj-a": ["a1"]},
        project_names={"proj-a"},
    )
    return snapshot.build(
        T0,
        HOST,
        rows,
        quota_view(),
        config(),
        usage,
        DRIVE,
        resources_view=resources_view,
    )


class FakeBuilder:
    """An injected snapshot builder: a canned doc, counted calls."""

    def __init__(self, doc):
        self.doc = doc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return copy.deepcopy(self.doc)


class ServeCase(unittest.TestCase):
    """A live loopback server per test, shut down on cleanup."""

    def start_server(self, build, **kwargs):
        now = [T0]
        kwargs.setdefault("clock", lambda: now[0])
        kwargs.setdefault("quiet", True)
        server = serve.make_server(build=build, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.daemon = True
        thread.start()
        self.addCleanup(self._stop, server, thread)
        return server, now

    def _stop(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    def fetch(self, server, method, target):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, target)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()


class SnapshotRoute(ServeCase):
    def test_snapshot_matches_builder_byte_for_byte(self):
        doc = healthy_doc()
        server, _ = self.start_server(FakeBuilder(doc))
        status, headers, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(body, json.dumps(doc).encode("utf-8"))
        self.assertEqual(json.loads(body), doc)

    def test_one_broken_section_stays_200(self):
        doc = healthy_doc()
        doc["quota"] = {"error": "ledger unreadable"}
        server, _ = self.start_server(FakeBuilder(doc))
        status, _, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["quota"], {"error": "ledger unreadable"})
        self.assertEqual(payload["agents"], doc["agents"])

    def test_all_broken_sections_still_200(self):
        doc = {
            "schema_version": 1,
            "fleet_header": {"error": "gone"},
            "agents": {"error": "gone"},
            "quota": {"error": "gone"},
            "capacity": {"error": "gone"},
            "disk": {"error": "gone"},
            "drive": {"error": "gone"},
        }
        server, _ = self.start_server(FakeBuilder(doc))
        status, _, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), doc)

    def test_builder_failure_is_a_json_error(self):
        def boom():
            raise OSError("/tmp/secret-path unreadable")

        server, _ = self.start_server(boom)
        status, headers, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})
        self.assertNotIn("/tmp/secret-path", body.decode("utf-8"))

    def test_none_builder_result_is_a_generic_500(self):
        server, _ = self.start_server(lambda: None)
        status, headers, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})

    def test_non_dict_builder_result_is_a_generic_500(self):
        server, _ = self.start_server(lambda: ["agents"])
        status, headers, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})

    def test_none_builder_health_is_a_generic_500(self):
        server, _ = self.start_server(lambda: None)
        status, headers, body = self.fetch(server, "GET", "/v1/health")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})


class VersionRoute(ServeCase):
    def test_version_reports_api_and_schema(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()))
        status, _, body = self.fetch(server, "GET", "/v1/version")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"api": 1, "schema_version": snapshot.SCHEMA_VERSION})
        self.assertEqual(serve.API_VERSION, 1)


class HealthRoute(ServeCase):
    def test_health_reports_host_from_document(self):
        doc = healthy_doc()
        doc["fleet_header"]["host"] = "host-9"
        server, _ = self.start_server(FakeBuilder(doc))
        _, _, body = self.fetch(server, "GET", "/v1/health")
        self.assertEqual(json.loads(body)["host"], "host-9")

    def test_health_builder_failure_is_500_without_detail(self):
        def boom():
            raise OSError("/tmp/secret-path unreadable")

        server, _ = self.start_server(boom)
        status, headers, body = self.fetch(server, "GET", "/v1/health")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})
        self.assertNotIn("/tmp/secret-path", body.decode("utf-8"))

    def test_health_reports_healthy(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()))
        status, _, body = self.fetch(server, "GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "ok": True,
                "host": HOST,
                "now": T0,
                "uptime_s": 0.0,
                "requests_total": 1,
                "requests_304": 0,
                "degraded": [],
            },
        )

    def test_health_lists_degraded_sections(self):
        doc = healthy_doc()
        doc["quota"] = {"error": "ledger unreadable"}
        doc["disk"] = {"error": "disks unreadable"}
        server, _ = self.start_server(FakeBuilder(doc))
        _, _, body = self.fetch(server, "GET", "/v1/health")
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["degraded"], ["disk", "quota"])

    def test_health_lists_a_resources_error_as_degraded(self):
        doc = healthy_doc()
        doc["resources"] = {"error": "budget unreadable"}
        server, _ = self.start_server(FakeBuilder(doc))
        _, _, body = self.fetch(server, "GET", "/v1/health")
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["degraded"], ["resources"])

    def test_health_clock_moves_now_and_uptime(self):
        server, now = self.start_server(FakeBuilder(healthy_doc()))
        now[0] = T0 + 30
        _, _, body = self.fetch(server, "GET", "/v1/health")
        payload = json.loads(body)
        self.assertEqual(payload["now"], T0 + 30)
        self.assertEqual(payload["uptime_s"], 30.0)

    def test_requests_total_counts_every_request(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_server(builder)
        self.fetch(server, "GET", "/v1/snapshot")
        self.fetch(server, "GET", "/v1/version")
        self.fetch(server, "GET", "/nope")
        self.fetch(server, "POST", "/v1/snapshot")
        _, _, body = self.fetch(server, "GET", "/v1/health")
        self.assertEqual(json.loads(body)["requests_total"], 5)

    def test_health_without_a_fleet_header_has_no_host(self):
        doc = healthy_doc()
        del doc["fleet_header"]
        server, _ = self.start_server(FakeBuilder(doc))
        status, _, body = self.fetch(server, "GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["host"])

    def test_health_with_an_errored_fleet_header_has_no_host(self):
        doc = healthy_doc()
        doc["fleet_header"] = {"error": "registry unreadable"}
        server, _ = self.start_server(FakeBuilder(doc))
        status, _, body = self.fetch(server, "GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["host"])

    def test_builder_error_without_a_message_logs_the_repr(self):
        def boom():
            raise ValueError()

        server, _ = self.start_server(boom)
        err = io.StringIO()
        with redirect_stderr(err):
            status, _, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})
        self.assertIn("ValueError()", err.getvalue())


class ErrorShapes(ServeCase):
    def test_unknown_path_is_404_without_building(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_server(builder)
        status, headers, body = self.fetch(server, "GET", "/v9/nope")
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertEqual(builder.calls, 0)

    def test_other_methods_are_405_without_building(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_server(builder)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            status, _, body = self.fetch(server, method, "/v1/snapshot")
            self.assertEqual(status, 405, method)
            self.assertIn("error", json.loads(body))
        status, _, _ = self.fetch(server, "HEAD", "/v1/snapshot")
        self.assertEqual(status, 405)
        self.assertEqual(builder.calls, 0)


class ServerBinding(ServeCase):
    def test_server_binds_loopback_only(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()))
        self.assertEqual(server.server_address[0], "127.0.0.1")


class ServerFamilyDedup(unittest.TestCase):
    def test_serve_and_hub_share_one_family_helper(self):
        from korapy import netbind

        self.assertIs(serve.server_family, netbind.server_family)
        self.assertIs(hub.server_family, netbind.server_family)


class ServerFamilyBehavior(unittest.TestCase):
    def test_serve_binds_an_ipv6_literal_as_v6(self):
        server = serve.make_server(build=FakeBuilder(healthy_doc()), bind="::1")
        self.addCleanup(server.server_close)
        import socket as _socket

        self.assertEqual(server.address_family, _socket.AF_INET6)

    def test_hub_binds_an_ipv6_literal_as_v6(self):
        import socket as _socket

        with tempfile.TemporaryDirectory(prefix="korapy-serve-hub-") as tmp:
            server = hub.make_server("::1", 0, os.path.join(tmp, "hub.sqlite"))
            self.addCleanup(server.server_close)
            self.assertEqual(server.address_family, _socket.AF_INET6)


class MethodFallback(unittest.TestCase):
    def test_unknown_do_method_answers_405(self):
        handler = object.__new__(serve._Handler)
        self.assertEqual(handler.__getattr__("do_TRACE"), handler._method_not_allowed)

    def test_non_do_attribute_still_raises(self):
        handler = object.__new__(serve._Handler)
        with self.assertRaises(AttributeError):
            handler.__getattr__("server_state_typo")


class ImportClosure(unittest.TestCase):
    def test_serve_imports_no_live_seams(self):
        with open(serve.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        forbidden = {
            "subprocess",
            "gh",
            "tracker",
            "trackers",
            "forge",
            "hub",
            "systemd",
            "docker",
            "omp",
            "muse",
            "claude",
            "requests",
        }
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module != "korapy":
                    names.append(node.module)
                if node.module == "korapy":
                    names.extend(f"korapy.{alias.name}" for alias in node.names)
        bad = [name for name in names if set(name.split(".")) & forbidden]
        self.assertEqual(bad, [])

    def test_serve_builds_from_snapshot(self):
        with open(serve.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        from_korapy = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "korapy"
            for alias in node.names
        ]
        self.assertIn("snapshot", from_korapy)


class CollectDivergence(unittest.TestCase):
    def test_collect_and_helper_cannot_diverge(self):
        from korapy import resources as R
        from korapy import snapshot as snap

        registry_view = view((entry("a1"), "live"))
        quota = quota_view()
        resolved = resolved_settings(**{"quota.low_pct": 90, "drive.max_starts_per_day": 4})
        with (
            mock.patch("korapy.snapshot._settings_root", return_value=None),
            mock.patch("korapy.settings.load", return_value=resolved),
            mock.patch("korapy.projects.load_all", return_value=[]),
            mock.patch("korapy.units.list_units", return_value=[]),
            mock.patch("korapy.registry.config", return_value={"count_toward_cap": True}),
            mock.patch("korapy.registry.visible", return_value=registry_view),
            mock.patch("korapy.accounts.providers", return_value=["prov-a"]),
            mock.patch("korapy.quota_ledger.load", return_value={"ledger": True}),
            mock.patch("korapy.quotacli.snapshot", return_value=quota),
            mock.patch("korapy.drive.drive_status", return_value=dict(DRIVE)),
            mock.patch(
                "korapy.resources.read_pressure",
                return_value=(R.Pressure(load_per_cpu=0.25), []),
            ),
            mock.patch("korapy.snapshot.default_measured", return_value=None),
            mock.patch("shutil.disk_usage", return_value=(1000, 400, 600)),
            mock.patch("time.time", return_value=T0),
            mock.patch("socket.gethostname", return_value=HOST),
        ):
            kwargs = snap.collect_kwargs()
            expected = snap.build(**kwargs)
            actual = serve.collect()
            self.assertEqual(actual, expected)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(snap.cmd_snapshot(argparse.Namespace(json=True)), 0)
            self.assertEqual(json.loads(out.getvalue()), expected)


class ServeSettingsTest(unittest.TestCase):
    def test_serve_settings_have_documented_defaults(self):
        self.assertEqual(settings.REGISTRY["serve.bind"].default, "127.0.0.1")
        self.assertEqual(settings.REGISTRY["serve.port"].default, serve.SERVE_PORT_DEFAULT)
        self.assertEqual(settings.REGISTRY["serve.origins"].default, [])
        self.assertEqual(settings.REGISTRY["serve.snapshot_ttl_s"].default, 2)
        self.assertEqual(settings.REGISTRY["serve.tail_max"].default, 200)
        self.assertEqual(settings.REGISTRY["serve.max_streams"].default, 20)

    def test_serve_registers_before_resolve(self):
        self.assertIn("korapy.serve", settings.PROVIDERS)

    def test_host_and_env_layers_reach_serve(self):
        resolved = settings.resolve(host={"serve": {"port": 4321}}, env={})
        self.assertEqual(resolved["serve.port"], 4321)
        resolved = settings.resolve(env={"KORAPY_SERVE_PORT": "4322"})
        self.assertEqual(resolved["serve.port"], 4322)

    def test_no_setting_holds_a_token(self):
        self.assertEqual([key for key in settings.REGISTRY if "token" in key], [])


class ApiTokenPathTest(unittest.TestCase):
    def test_token_lives_beside_the_host_config(self):
        self.assertEqual(paths.api_token_path({"KORAPY_CONFIG": "/x/config"}), "/x/config/token")

    def test_token_defaults_under_xdg_config(self):
        self.assertEqual(
            paths.api_token_path({"XDG_CONFIG_HOME": "/x/config"}),
            "/x/config/korapy/token",
        )


class LoopbackTest(unittest.TestCase):
    def test_loopback_addresses(self):
        for bind in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            with self.subTest(bind=bind):
                self.assertTrue(serve.is_loopback(bind))

    def test_non_loopback_addresses(self):
        for bind in ("0.0.0.0", "", "8.8.8.8", "10.0.0.5", "host-1.example.com"):
            with self.subTest(bind=bind):
                self.assertFalse(serve.is_loopback(bind))


class BindCheckTest(unittest.TestCase):
    def test_loopback_binds_are_open(self):
        for bind in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(bind=bind):
                self.assertIsNone(serve.check_bind(bind, 0, allow_remote=False, has_token=False))

    def test_port_range(self):
        for port in (-1, 65536):
            with self.subTest(port=port):
                problem = serve.check_bind("127.0.0.1", port, allow_remote=False, has_token=False)
                self.assertIsNotNone(problem)
                self.assertIn(str(port), str(problem))
        for port in (0, 4321, 65535):
            with self.subTest(port=port):
                self.assertIsNone(
                    serve.check_bind("127.0.0.1", port, allow_remote=False, has_token=False)
                )

    def test_remote_bind_needs_flag_and_token(self):
        problem = serve.check_bind("8.8.8.8", 0, allow_remote=False, has_token=True)
        self.assertIn("--allow-remote", str(problem))
        problem = serve.check_bind("8.8.8.8", 0, allow_remote=True, has_token=False)
        self.assertIn("token", str(problem))
        self.assertIsNone(serve.check_bind("8.8.8.8", 0, allow_remote=True, has_token=True))

    def test_names_are_remote_without_dns(self):
        self.assertIsNotNone(
            serve.check_bind("host-1.example.com", 0, allow_remote=False, has_token=True)
        )

    def test_origins_reject_wildcards(self):
        self.assertIsNone(serve.check_origins([]))
        self.assertIsNone(serve.check_origins(["https://dash.example.com"]))
        for origins in (["*"], ["https://*.example.com"], [""], [42]):
            with self.subTest(origins=origins):
                self.assertIsNotNone(serve.check_origins(origins))


class TokenFileTest(unittest.TestCase):
    def test_mint_creates_owner_only_hex(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-token-") as tmp:
            target = os.path.join(tmp, "sub", "token")
            serve.mint_token(target)
            self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o600")
            text = serve.read_token(target)
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", str(text)))

    def test_mint_refuses_when_present(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-token-") as tmp:
            target = os.path.join(tmp, "token")
            serve.mint_token(target)
            with self.assertRaises(serve.ServeError):
                serve.mint_token(target)

    def test_missing_token_reads_none(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-token-") as tmp:
            self.assertIsNone(serve.read_token(os.path.join(tmp, "token")))

    def test_unreadable_token_raises(self):
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaises(serve.ServeError):
                serve.read_token(os.path.join("token"))

    def test_world_readable_token_warns(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-token-") as tmp:
            target = os.path.join(tmp, "token")
            serve.mint_token(target)
            self.assertIsNone(serve.token_file_warning(target))
            self.assertIsNone(serve.token_file_warning(os.path.join(tmp, "missing")))
            os.chmod(target, 0o644)
            warning = serve.token_file_warning(target)
            self.assertIsNotNone(warning)
            self.assertIn(target, str(warning))


class TokenAuthTest(ServeCase):
    def fetch_auth(self, server, token=None):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            conn.request("GET", "/v1/snapshot", headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_open_when_not_required(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()))
        status, body = self.fetch_auth(server)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), healthy_doc())

    def test_missing_token_is_401(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        status, body = self.fetch_auth(server)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})

    def test_wrong_token_is_401_without_logging_it(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        err = io.StringIO()
        with redirect_stderr(err):
            status, body = self.fetch_auth(server, token="presented-secret-abc")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        self.assertNotIn("presented-secret-abc", body.decode("utf-8"))
        self.assertNotIn("presented-secret-abc", err.getvalue())

    def test_correct_token_serves(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        status, body = self.fetch_auth(server, token="tok-1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), healthy_doc())

    def test_unreadable_token_denies(self):
        def fail():
            raise OSError("gone")

        server, _ = self.start_server(
            FakeBuilder(healthy_doc()), token_required=True, token_reader=fail
        )
        status, _ = self.fetch_auth(server, token="tok-1")
        self.assertEqual(status, 401)

    def test_non_ascii_token_denies(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        status, _ = self.fetch_auth(server, token="t\xe9k-1")
        self.assertEqual(status, 401)


class CorsTest(ServeCase):
    ORIGIN = "https://dash.example.com"

    def fetch_origin(self, server, method, target, origin=None, preflight=False):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {}
            if origin is not None:
                headers["Origin"] = origin
            if preflight:
                headers["Access-Control-Request-Method"] = "GET"
            conn.request(method, target, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_matching_origin_is_echoed(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()), allowed_origins=(self.ORIGIN,))
        status, headers, _ = self.fetch_origin(server, "GET", "/v1/snapshot", origin=self.ORIGIN)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.ORIGIN)

    def test_unmatched_and_missing_origins_have_no_echo(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()), allowed_origins=(self.ORIGIN,))
        _, headers, _ = self.fetch_origin(
            server, "GET", "/v1/snapshot", origin="https://other.example.com"
        )
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        _, headers, _ = self.fetch_origin(server, "GET", "/v1/snapshot")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_star_is_never_echoed(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()), allowed_origins=("*",))
        _, headers, _ = self.fetch_origin(server, "GET", "/v1/snapshot", origin="*")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_preflight_answers_only_configured_origins(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()), allowed_origins=(self.ORIGIN,))
        status, headers, _ = self.fetch_origin(
            server, "OPTIONS", "/v1/snapshot", origin=self.ORIGIN, preflight=True
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.ORIGIN)
        self.assertIn("GET", headers.get("Access-Control-Allow-Methods", ""))
        status, headers, _ = self.fetch_origin(
            server,
            "OPTIONS",
            "/v1/snapshot",
            origin="https://other.example.com",
            preflight=True,
        )
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_plain_options_without_preflight_is_405(self):
        server, _ = self.start_server(FakeBuilder(healthy_doc()), allowed_origins=(self.ORIGIN,))
        status, _, _ = self.fetch_origin(server, "OPTIONS", "/v1/snapshot")
        self.assertEqual(status, 405)

    def test_preflight_needs_no_token(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            allowed_origins=(self.ORIGIN,),
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        status, headers, body = self.fetch_origin(
            server, "OPTIONS", "/v1/snapshot", origin=self.ORIGIN, preflight=True
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.ORIGIN)
        self.assertEqual(body, b"")

    def test_plain_options_still_needs_token(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            allowed_origins=(self.ORIGIN,),
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        status, _, _ = self.fetch_origin(server, "OPTIONS", "/v1/snapshot")
        self.assertEqual(status, 401)


class ServeCommandTest(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(["serve", *argv])

    def run_func(self, argv, config=None, state=None):
        config_dir = tempfile.mkdtemp(prefix="korapy-serve-config-")
        self.addCleanup(shutil.rmtree, config_dir)
        state_dir = tempfile.mkdtemp(prefix="korapy-serve-state-")
        self.addCleanup(shutil.rmtree, state_dir)
        if config is not None:
            with open(os.path.join(config_dir, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write(config)
        env = {"KORAPY_CONFIG": config_dir, "KORAPY_STATE": state_dir}
        if state == "missing":
            missing = os.path.join(state_dir, "gone")
            env["KORAPY_STATE"] = missing
        args = self.parse(*argv)
        with mock.patch.dict(os.environ, env, clear=False):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                return args.func(args)

    def test_bad_port_is_a_usage_error(self):
        for port in ("-1", "70000"):
            with self.subTest(port=port):
                self.assertEqual(self.run_func(["--port", port]), 4)

    def test_remote_bind_without_flag_refuses_before_listening(self):
        with mock.patch.object(serve, "make_server", side_effect=AssertionError("must not bind")):
            self.assertEqual(self.run_func(["--bind", "8.8.8.8"]), 4)

    def test_remote_bind_with_flag_but_no_token_refuses(self):
        with mock.patch.object(serve, "make_server", side_effect=AssertionError("must not bind")):
            self.assertEqual(self.run_func(["--bind", "8.8.8.8", "--allow-remote"]), 4)

    def test_remote_bind_with_flag_and_token_serves(self):
        class FakeServer:
            server_address = ("8.8.8.8", 4321)

            def __init__(self):
                self.served = False

            def serve_forever(self):
                self.served = True

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory(prefix="korapy-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o600)
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=FakeServer()) as made:
                    out = io.StringIO()
                    args = self.parse("--bind", "8.8.8.8", "--allow-remote")
                    with redirect_stdout(out):
                        self.assertEqual(args.func(args), 0)
        made.assert_called_once()
        self.assertIn("dashboard at http://8.8.8.8:4321/", out.getvalue())

    def test_world_readable_token_warns_on_loopback_and_serves(self):
        class FakeServer:
            server_address = ("127.0.0.1", 4321)

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory(prefix="korapy-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o644)
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=FakeServer()):
                    out, err = io.StringIO(), io.StringIO()
                    args = self.parse()
                    with redirect_stdout(out), redirect_stderr(err):
                        self.assertEqual(args.func(args), 0)
            self.assertIn("readable beyond its owner", err.getvalue())
            self.assertIn("dashboard at http://127.0.0.1:4321/", out.getvalue())

    def test_world_readable_token_refuses_remote(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o644)
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(
                    serve,
                    "make_server",
                    side_effect=AssertionError("must not bind"),
                ):
                    args = self.parse("--bind", "8.8.8.8", "--allow-remote")
                    self.assertEqual(args.func(args), 4)

    def test_held_loopback_port_is_a_race(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-cmd-") as tmp:
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                holder.bind(("127.0.0.1", 0))
                holder.listen(1)
                port = holder.getsockname()[1]
                with mock.patch.dict(
                    os.environ,
                    {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                    clear=False,
                ):
                    args = self.parse("--port", str(port))
                    self.assertEqual(args.func(args), 2)
            finally:
                holder.close()

    def test_other_bind_error_is_a_usage_error(self):
        import errno

        with mock.patch.object(
            serve,
            "make_server",
            side_effect=OSError(errno.EACCES, "permission denied"),
        ):
            self.assertEqual(self.run_func([]), 4)

    def test_unreadable_state_dir_is_unreachable(self):
        with mock.patch.object(serve, "make_server", side_effect=AssertionError("must not bind")):
            self.assertEqual(self.run_func([], state="missing"), 3)

    def test_settings_bind_reaches_the_verb(self):
        with mock.patch.object(serve, "make_server", side_effect=AssertionError("must not bind")):
            self.assertEqual(self.run_func([], config='[serve]\nbind = "8.8.8.8"\n'), 4)

    def test_new_token_mints_then_serves(self):
        class FakeServer:
            server_address = ("127.0.0.1", 4321)

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory(prefix="korapy-serve-cmd-") as tmp:
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=FakeServer()):
                    out, err = io.StringIO(), io.StringIO()
                    args = self.parse("--new-token")
                    with redirect_stdout(out), redirect_stderr(err):
                        self.assertEqual(args.func(args), 0)
            target = os.path.join(tmp, "token")
            self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o600")
            minted = serve.read_token(target)
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", str(minted)))
            self.assertNotIn(minted, out.getvalue())
            self.assertNotIn(minted, err.getvalue())

    def test_new_token_refuses_when_present(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(
                    serve,
                    "make_server",
                    side_effect=AssertionError("must not bind"),
                ):
                    args = self.parse("--new-token")
                    self.assertEqual(args.func(args), 4)


class CollectWiring(unittest.TestCase):
    def test_collect_uses_snapshot_build_with_local_readers(self):
        from korapy import resources as R

        registry_view = view((entry("a1"), "live"))
        quota = quota_view()
        resolved = resolved_settings(**{"quota.low_pct": 90, "drive.max_starts_per_day": 4})
        with (
            mock.patch("korapy.snapshot._settings_root", return_value=None),
            mock.patch("korapy.settings.load", return_value=resolved),
            mock.patch("korapy.projects.load_all", return_value=[]),
            mock.patch("korapy.units.list_units", return_value=[]),
            mock.patch("korapy.registry.config", return_value={"count_toward_cap": True}),
            mock.patch("korapy.registry.visible", return_value=registry_view),
            mock.patch("korapy.accounts.providers", return_value=["prov-a"]),
            mock.patch("korapy.quota_ledger.load", return_value={"ledger": True}),
            mock.patch("korapy.quotacli.snapshot", return_value=quota),
            mock.patch("korapy.drive.drive_status", return_value=dict(DRIVE)),
            mock.patch(
                "korapy.resources.read_pressure",
                return_value=(R.Pressure(load_per_cpu=0.25), []),
            ),
            mock.patch("korapy.snapshot.build", return_value={"ok": True}) as build,
        ):
            self.assertEqual(serve.collect(), {"ok": True})
        _, kwargs = build.call_args
        self.assertIs(kwargs["registry_view"], registry_view)
        self.assertIs(kwargs["quota_view"], quota)
        self.assertEqual(
            kwargs["settings"],
            {
                "admission.total_cap": 4,
                "admission.repo_cap": 1,
                "registry.count_toward_cap": True,
            },
        )
        self.assertEqual(kwargs["drive_view"], DRIVE)
        self.assertIsInstance(kwargs["now"], float)
        self.assertIsInstance(kwargs["host"], str)
        self.assertEqual(kwargs["resources_view"]["pressure"]["load_per_cpu"], 0.25)
        self.assertEqual(
            kwargs["resources_view"]["charged"], {"cpu_m": 0, "memory_mb": 0, "pids": 0}
        )


class SharedSections(unittest.TestCase):
    def test_serve_uses_snapshots_section_names(self):
        self.assertIs(serve.ERROR_SECTIONS, snapshot.ERROR_SECTIONS)


class UnreadableTokenFailsClosed(unittest.TestCase):
    def test_loopback_unreadable_token_is_a_usage_error(self):
        case = ServeCommandTest()
        with mock.patch.object(
            serve, "read_token", side_effect=serve.ServeError("cannot read token file: denied")
        ):
            with mock.patch.object(
                serve, "make_server", side_effect=AssertionError("must not bind")
            ):
                self.assertEqual(case.run_func([]), 4)


class MintCleanup(unittest.TestCase):
    def test_failed_mint_leaves_no_token_file(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-token-") as tmp:
            target = os.path.join(tmp, "token")
            with mock.patch.object(os, "chmod", side_effect=OSError("denied")):
                with self.assertRaises(serve.ServeError):
                    serve.mint_token(target)
            self.assertFalse(os.path.exists(target))


def stored_board(path, *itemspec, claims=()):
    """A board store at `path` holding the given items; `claims` maps number to agent."""
    store = board.open_store(path)
    try:
        for spec in itemspec:
            item = board.Item(
                repo=spec.get("repo", "app"),
                number=spec["number"],
                title=spec.get("title", f"Title {spec['number']}"),
                state=spec.get("state", "open"),
                author="alice",
                labels=list(spec.get("labels", [])),
                fetched_at=spec.get("fetched_at", T0),
            )
            store.upsert_item(item, operators=["alice"], now=spec.get("fetched_at", T0))
        for number, agent in claims:
            store.claim("app", number, agent, "host-1", now=T0)
    finally:
        store.close()


class BoardView(ServeCase):
    def open_store(self, tmp, *itemspec, claims=()):
        stored_board(os.path.join(tmp, "board.sqlite"), *itemspec, claims=claims)
        return lambda: board.open_store(os.path.join(tmp, "board.sqlite"))

    def get_json(self, server, target):
        status, headers, body = self.fetch(server, "GET", target)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        return json.loads(body)

    def test_board_lists_rows_oldest_first(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            opener = self.open_store(
                tmp,
                {"number": 7, "fetched_at": T0 + 20},
                {"number": 3, "fetched_at": T0 + 10},
            )
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            payload = self.get_json(server, "/v1/board")
            self.assertEqual(payload["now"], T0)
            self.assertEqual([row["number"] for row in payload["board"]], [3, 7])
            row = payload["board"][0]
            self.assertEqual(row["repo"], "app")
            self.assertEqual(row["confidence"], board.CONFIRMED)
            self.assertEqual(row["fetched_at"], T0 + 10)
            self.assertTrue(row["fresh"])
            self.assertIsNone(row["claim"])

    def test_board_filters_reach_the_store(self):
        seen = []

        class FakeStore:
            def items(self, repo=None, state=None, label=None):
                seen.append((repo, state, label))
                return []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=lambda: FakeStore())
        payload = self.get_json(server, "/v1/board?repo=app&state=open&label=ready")
        self.assertEqual(payload["board"], [])
        self.assertEqual(seen, [("app", "open", "ready")])

    def test_board_reports_confidence_freshness_and_claims(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            opener = self.open_store(
                tmp,
                {"number": 7, "fetched_at": T0},
                {"number": 9, "fetched_at": T0 - 100},
                claims=[(7, "agent-1")],
            )
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            payload = self.get_json(server, "/v1/board?max_age=10")
            by_number = {row["number"]: row for row in payload["board"]}
            self.assertTrue(by_number[7]["fresh"])
            self.assertFalse(by_number[9]["fresh"])
            claim = by_number[7]["claim"]
            self.assertEqual(
                claim,
                {"agent": "agent-1", "host": "host-1", "epoch": 1, "claimed_at": T0},
            )
            self.assertIsNone(by_number[9]["claim"])
            self.assertEqual(by_number[7]["agent"], "agent-1")

    def test_board_captures_one_now_per_request(self):
        ticks = [T0]

        def clock():
            ticks[0] += 1
            return float(ticks[0])

        nows = []

        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            stored_board(
                os.path.join(tmp, "board.sqlite"),
                {"number": 7, "fetched_at": T0},
                {"number": 9, "fetched_at": T0},
            )
            path = os.path.join(tmp, "board.sqlite")

            class RecordingStore:
                def items(self, repo=None, state=None, label=None):
                    return self.real.items(repo=repo, state=state, label=label)

                def item(self, repo, number, *, now, max_age=None):
                    nows.append(now)
                    return self.real.item(repo, number, now=now, max_age=max_age)

                def claim_of(self, repo, number):
                    return self.real.claim_of(repo, number)

                def __enter__(self):
                    self.real = board.open_store(path)
                    return self

                def __exit__(self, *exc):
                    self.real.close()
                    return False

            server, _ = self.start_server(
                FakeBuilder(healthy_doc()),
                board_open=RecordingStore,
                clock=clock,
            )
            payload = self.get_json(server, "/v1/board?max_age=30")
            self.assertEqual(len(nows), 2)
            self.assertEqual(nows[0], nows[1])
            self.assertEqual(payload["now"], nows[0])

    def test_board_without_a_store_is_a_json_error(self):
        def boom():
            raise board.StoreError("gone")

        server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=boom)
        status, _, body = self.fetch(server, "GET", "/v1/board")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "board unavailable"})

    def open_many(self, tmp, count):
        specs = [{"number": n, "fetched_at": T0 + n} for n in range(1, count + 1)]
        return self.open_store(tmp, *specs)

    def test_board_defaults_to_the_first_100_with_total(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            opener = self.open_many(tmp, 250)
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            payload = self.get_json(server, "/v1/board")
            self.assertEqual(payload["total"], 250)
            self.assertEqual(payload["limit"], 100)
            self.assertEqual(payload["offset"], 0)
            self.assertEqual([row["number"] for row in payload["board"]], list(range(1, 101)))

    def test_board_limit_clamps_at_the_maximum(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            opener = self.open_many(tmp, 250)
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            payload = self.get_json(server, "/v1/board?limit=5000")
            self.assertEqual(payload["limit"], 1000)
            self.assertEqual(payload["total"], 250)
            self.assertEqual(payload["offset"], 0)
            self.assertEqual(len(payload["board"]), 250)

    def test_board_pages_cover_every_row_exactly_once(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            opener = self.open_many(tmp, 250)
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            seen = []
            offset = 0
            while True:
                payload = self.get_json(server, f"/v1/board?limit=100&offset={offset}")
                self.assertEqual(payload["total"], 250)
                self.assertEqual(payload["limit"], 100)
                self.assertEqual(payload["offset"], offset)
                seen.extend(row["number"] for row in payload["board"])
                if len(payload["board"]) < 100:
                    break
                offset += 100
            self.assertEqual(sorted(seen), list(range(1, 251)))
            self.assertEqual(len(seen), 250)

    def test_board_bad_paging_is_a_400(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            opener = self.open_many(tmp, 3)
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            for target in (
                "/v1/board?limit=many",
                "/v1/board?limit=-1",
                "/v1/board?offset=many",
                "/v1/board?offset=-3",
            ):
                with self.subTest(target=target):
                    status, _, body = self.fetch(server, "GET", target)
                    self.assertEqual(status, 400, target)
                    self.assertIn("error", json.loads(body))

    def test_board_bad_max_age_is_a_400(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            opener = self.open_many(tmp, 3)
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            for target in (
                "/v1/board?max_age=many",
                "/v1/board?max_age=-1",
                "/v1/board?max_age=nan",
                "/v1/board?max_age=inf",
                "/v1/board?max_age=-inf",
                "/v1/board?max_age=Infinity",
            ):
                with self.subTest(target=target):
                    status, _, body = self.fetch(server, "GET", target)
                    self.assertEqual(status, 400, target)
                    self.assertIn("error", json.loads(body))


class FilteredViews(ServeCase):
    def test_agents_view_matches_the_snapshot_sections(self):
        doc = healthy_doc()
        server, _ = self.start_server(FakeBuilder(doc))
        status, _, body = self.fetch(server, "GET", "/v1/agents")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), {"fleet_header": doc["fleet_header"], "agents": doc["agents"]}
        )

    def test_quota_capacity_and_disks_carry_now(self):
        doc = healthy_doc()
        server, _ = self.start_server(FakeBuilder(doc))
        for target, section in (
            ("/v1/quota", "quota"),
            ("/v1/capacity", "capacity"),
            ("/v1/disks", "disk"),
        ):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"now": T0, section: doc[section]})

    def test_filtered_views_do_not_rebuild_per_section(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_server(builder, snapshot_ttl_s=0)
        self.fetch(server, "GET", "/v1/agents")
        self.fetch(server, "GET", "/v1/quota")
        self.assertEqual(builder.calls, 2)


class FakeTail:
    """An injected tail reader: canned text, recorded calls."""

    def __init__(self, text="line one\nline two\n", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def __call__(self, repo, number, last):
        self.calls.append((repo, number, last))
        if self.error is not None:
            raise self.error
        return self.text


class TailView(ServeCase):
    def start(self, tail, **kwargs):
        kwargs.setdefault("tail_fn", tail)
        kwargs.setdefault("tail_max", 5)
        return self.start_server(FakeBuilder(healthy_doc()), **kwargs)

    def get_text(self, server, target):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", target)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_tail_returns_plain_text(self):
        tail = FakeTail("line one\nline two\n")
        server, _ = self.start(tail)
        status, headers, body = self.get_text(server, "/v1/tail?repo=app&number=7")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(body, b"line one\nline two\n")
        self.assertEqual(tail.calls, [("app", 7, 5)])

    def test_tail_last_is_clamped_not_refused(self):
        tail = FakeTail()
        server, _ = self.start(tail)
        status, _, _ = self.get_text(server, "/v1/tail?repo=app&number=7&last=999")
        self.assertEqual(status, 200)
        self.assertEqual(tail.calls, [("app", 7, 5)])
        tail.calls.clear()
        status, _, _ = self.get_text(server, "/v1/tail?repo=app&number=7&last=2")
        self.assertEqual(status, 200)
        self.assertEqual(tail.calls, [("app", 7, 2)])

    def test_tail_missing_ticket_is_404(self):
        tail = FakeTail(error=launch.LaunchError("no session"))
        server, _ = self.start(tail)
        status, _, body = self.get_text(server, "/v1/tail?repo=app&number=7")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})

    def test_tail_unknown_repo_is_404(self):
        tail = FakeTail(error=settings.SettingsError("unknown repo 'nosuch'"))
        server, _ = self.start(tail)
        status, _, body = self.get_text(server, "/v1/tail?repo=nosuch&number=1")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})

    def test_tail_needs_repo_number_and_a_valid_last(self):
        tail = FakeTail()
        server, _ = self.start(tail)
        for target in (
            "/v1/tail?repo=app",
            "/v1/tail?number=7",
            "/v1/tail?repo=app&number=seven",
            "/v1/tail?repo=app&number=7&last=many",
            "/v1/tail?repo=app&number=7&last=0",
        ):
            with self.subTest(target=target):
                status, _, body = self.get_text(server, target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))
        self.assertEqual(tail.calls, [])

    def test_tail_failure_is_a_generic_500(self):
        tail = FakeTail(error=OSError("gone"))
        server, _ = self.start(tail)
        status, _, body = self.get_text(server, "/v1/tail?repo=app&number=7")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "tail unavailable"})


class ProductionTailWiring(unittest.TestCase):
    def test_production_tail_reads_through_launch_without_the_ledger(self):
        with (
            mock.patch.object(launch, "target") as target,
            mock.patch.object(launch, "tail") as tail,
        ):
            target.return_value = mock.sentinel.tgt

            def fake(tgt, number, **kwargs):
                self.assertIs(tgt, mock.sentinel.tgt)
                kwargs["out"].write("line one\n")
                self.assertEqual(kwargs["last"], 3)
                self.assertEqual(kwargs["pool_fn"](), [])

            tail.side_effect = fake
            self.assertEqual(serve.read_tail("app", 7, 3), "line one\n")
        target.assert_called_once()


class TailCapSingleSource(unittest.TestCase):
    def test_tail_cap_default_defined_once(self):
        import inspect

        self.assertEqual(serve.TAIL_MAX_DEFAULT, 200)
        self.assertEqual(settings.REGISTRY["serve.tail_max"].default, serve.TAIL_MAX_DEFAULT)
        state_default = inspect.signature(serve._State.__init__).parameters["tail_max"].default
        self.assertEqual(state_default, serve.TAIL_MAX_DEFAULT)
        server_default = inspect.signature(serve.make_server).parameters["tail_max"].default
        self.assertEqual(server_default, serve.TAIL_MAX_DEFAULT)


class BlankNumericsAreInvalid(ServeCase):
    def test_board_blank_paging_is_a_400(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-board-") as tmp:
            store = BoardView()
            opener = store.open_store(tmp, {"number": 1, "fetched_at": T0})
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            for target in (
                "/v1/board?limit=",
                "/v1/board?offset=",
                "/v1/board?max_age=",
            ):
                with self.subTest(target=target):
                    status, _, body = self.fetch(server, "GET", target)
                    self.assertEqual(status, 400, target)
                    self.assertIn("error", json.loads(body))

    def test_tail_blank_numerics_are_a_400(self):
        tail = FakeTail()
        server, _ = self.start_server(FakeBuilder(healthy_doc()), tail_fn=tail, tail_max=5)
        for target in (
            "/v1/tail?repo=app&number=",
            "/v1/tail?repo=app&number=7&last=",
        ):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))
        self.assertEqual(tail.calls, [])


def transitive_korapy_modules(start):
    """Module names reachable from `start` through `korapy` imports.

    Follows `import korapy.x` and `from korapy[.x] import ...` edges at
    every level of each visited file (a lazy import still binds the
    seam when the handler runs), cycle-safe. Only `korapy.*` edges
    are followed; anything else is a leaf.
    """
    import importlib.util

    seen = set()
    stack = [start]
    while stack:
        name = stack.pop()
        if name in seen or not name.startswith("korapy"):
            continue
        seen.add(name)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
            continue
        package = name if spec.submodule_search_locations else name.rsplit(".", 1)[0]
        with open(spec.origin, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "korapy" or alias.name.startswith("korapy."):
                        stack.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    for alias in node.names:
                        if node.module:
                            stack.append(f"{package}.{node.module}")
                        else:
                            stack.append(f"{package}.{alias.name}")
                elif node.module == "korapy":
                    for alias in node.names:
                        stack.append(f"korapy.{alias.name}")
                elif (node.module or "").startswith("korapy"):
                    stack.append(node.module)
    return seen


EVENT_ROWS = [
    {
        "id": "a1b2c3d4",
        "kind": "held-merge",
        "ref": "app#7",
        "evidence": "waiting on checks",
        "state": "9:abcdef123 checks-failing",
        "first_seen": "2026-01-01T00:00:00+00:00",
        "resolved_at": None,
    },
    {
        "id": "e5f6a7b8",
        "kind": "coordinator-review",
        "ref": "app#9",
        "evidence": "a question",
        "state": "what now?",
        "first_seen": "2026-01-01T01:00:00+00:00",
        "resolved_at": "2026-01-02T00:00:00+00:00",
    },
    {
        "id": "c9d0e1f2",
        "kind": "held-merge",
        "ref": "app#11",
        "evidence": "held again",
        "state": "4:beef done",
        "first_seen": "2026-01-03T00:00:00+00:00",
        "resolved_at": "2026-01-10T00:00:00+00:00",
    },
]


class EventsView(ServeCase):
    def start(self, rows, **kwargs):
        seen = []
        kwargs.setdefault("events_fn", lambda project: (seen.append(project), list(rows))[1])
        server, _ = self.start_server(FakeBuilder(healthy_doc()), **kwargs)
        return server, seen

    def get_json(self, server, target):
        status, headers, body = self.fetch(server, "GET", target)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        return json.loads(body)

    def test_events_lists_unresolved_oldest_first(self):
        server, seen = self.start(EVENT_ROWS)
        payload = self.get_json(server, "/v1/events?project=demo")
        self.assertEqual(seen, ["demo"])
        self.assertEqual(payload["now"], T0)
        self.assertEqual(payload["project"], "demo")
        self.assertIsNone(payload["resolved_since"])
        self.assertEqual(payload["events"], [EVENT_ROWS[0]])

    def test_events_resolved_since_includes_recently_resolved(self):
        import datetime

        since = datetime.datetime(2026, 1, 5, tzinfo=datetime.timezone.utc).timestamp()
        server, _ = self.start(EVENT_ROWS)
        payload = self.get_json(server, f"/v1/events?project=demo&resolved_since={since}")
        self.assertEqual(payload["resolved_since"], since)
        self.assertEqual(payload["events"], [EVENT_ROWS[0], EVENT_ROWS[2]])

    def test_events_needs_a_project(self):
        server, _ = self.start(EVENT_ROWS)
        for target in ("/v1/events", "/v1/events?project="):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))

    def test_events_bad_project_is_a_400(self):
        def boom(project):
            raise coord.UsageError("bad project name")

        server, _ = self.start_server(FakeBuilder(healthy_doc()), events_fn=boom)
        status, _, body = self.fetch(server, "GET", "/v1/events?project=../..")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_events_bad_since_is_a_400(self):
        server, seen = self.start(EVENT_ROWS)
        for target in (
            "/v1/events?project=demo&resolved_since=many",
            "/v1/events?project=demo&resolved_since=",
            "/v1/events?project=demo&resolved_since=-1",
            "/v1/events?project=demo&resolved_since=nan",
            "/v1/events?project=demo&resolved_since=inf",
            "/v1/events?project=demo&resolved_since=-inf",
            "/v1/events?project=demo&resolved_since=Infinity",
        ):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))
        self.assertEqual(seen, [])

    def test_events_failure_is_a_generic_500(self):
        def boom(project):
            raise OSError("gone")

        server, _ = self.start_server(FakeBuilder(healthy_doc()), events_fn=boom)
        status, _, body = self.fetch(server, "GET", "/v1/events?project=demo")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "events unavailable"})


def journal_entry(index, kind="note"):
    """One canned logbook entry with the production shape."""
    return {
        "at": f"2026-01-01T00:{index:02d}:00+00:00",
        "holder": "agent-1",
        "generation": 1,
        "kind": kind,
        "refs": [],
        "text": f"entry {index}",
    }


class JournalView(ServeCase):
    def start(self, entries, **kwargs):
        seen = []
        kwargs.setdefault("journal_fn", lambda project: (seen.append(project), list(entries))[1])
        server, _ = self.start_server(FakeBuilder(healthy_doc()), **kwargs)
        return server, seen

    def get_json(self, server, target):
        status, headers, body = self.fetch(server, "GET", target)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        return json.loads(body)

    def test_journal_pages_oldest_first_with_an_opaque_cursor(self):
        entries = [journal_entry(n) for n in range(5)]
        server, seen = self.start(entries)
        payload = self.get_json(server, "/v1/journal?project=demo")
        self.assertEqual(seen, ["demo"])
        self.assertEqual(payload["now"], T0)
        self.assertEqual(payload["project"], "demo")
        self.assertEqual(payload["entries"], entries)
        self.assertEqual(payload["total"], 5)
        self.assertEqual(payload["limit"], 100)
        self.assertEqual(payload["cursor"], "5")

    def test_journal_cursor_pages_cover_every_entry_exactly_once(self):
        entries = [journal_entry(n) for n in range(7)]
        server, _ = self.start(entries)
        seen = []
        cursor = "0"
        while True:
            payload = self.get_json(server, f"/v1/journal?project=demo&limit=3&cursor={cursor}")
            self.assertEqual(payload["total"], 7)
            self.assertEqual(payload["limit"], 3)
            seen.extend(payload["entries"])
            if len(payload["entries"]) < 3:
                break
            cursor = payload["cursor"]
        self.assertEqual(seen, entries)

    def test_journal_cursor_past_the_end_is_empty_at_the_end(self):
        entries = [journal_entry(n) for n in range(3)]
        server, _ = self.start(entries)
        payload = self.get_json(server, "/v1/journal?project=demo&cursor=99")
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["cursor"], "3")
        self.assertEqual(payload["total"], 3)

    def test_journal_filters_by_kind(self):
        entries = [journal_entry(0, "note"), journal_entry(1, "action"), journal_entry(2, "note")]
        server, _ = self.start(entries)
        payload = self.get_json(server, "/v1/journal?project=demo&kind=action")
        self.assertEqual(payload["entries"], [entries[1]])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["cursor"], "1")

    def test_journal_unknown_kind_is_a_400(self):
        server, seen = self.start([journal_entry(0)])
        status, _, body = self.fetch(server, "GET", "/v1/journal?project=demo&kind=snooze")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        self.assertEqual(seen, [])

    def test_journal_limit_defaults_and_clamps(self):
        entries = [journal_entry(n) for n in range(250)]
        server, _ = self.start(entries)
        payload = self.get_json(server, "/v1/journal?project=demo")
        self.assertEqual(payload["limit"], 100)
        self.assertEqual(len(payload["entries"]), 100)
        payload = self.get_json(server, "/v1/journal?project=demo&limit=5000")
        self.assertEqual(payload["limit"], 1000)
        self.assertEqual(len(payload["entries"]), 250)

    def test_journal_bad_paging_is_a_400(self):
        server, seen = self.start([journal_entry(0)])
        for target in (
            "/v1/journal?project=demo&limit=many",
            "/v1/journal?project=demo&limit=-1",
            "/v1/journal?project=demo&limit=",
            "/v1/journal?project=demo&cursor=many",
            "/v1/journal?project=demo&cursor=-2",
            "/v1/journal?project=demo&cursor=",
        ):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))
        self.assertEqual(seen, [])

    def test_journal_needs_a_project(self):
        server, _ = self.start([journal_entry(0)])
        for target in ("/v1/journal", "/v1/journal?project="):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))

    def test_journal_bad_project_is_a_400(self):
        def boom(project):
            raise coord.UsageError("bad project name")

        server, _ = self.start_server(FakeBuilder(healthy_doc()), journal_fn=boom)
        status, _, body = self.fetch(server, "GET", "/v1/journal?project=../..")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_journal_failure_is_a_generic_500(self):
        def boom(project):
            raise OSError("gone")

        server, _ = self.start_server(FakeBuilder(healthy_doc()), journal_fn=boom)
        status, _, body = self.fetch(server, "GET", "/v1/journal?project=demo")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "journal unavailable"})


class ProjectsRoute(ServeCase):
    def get_json(self, server, target, token=None):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "application/json")
            return json.loads(body)
        finally:
            conn.close()

    def test_projects_lists_names_sorted(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-projects-") as tmp:
            directory = os.path.join(tmp, projects.PROJECTS_DIR)
            os.makedirs(directory)
            for name, extra in (
                ("beta", 'eligibility = "policy:register"\n'),
                ("alpha", ""),
            ):
                with open(os.path.join(directory, f"{name}.toml"), "w", encoding="utf-8") as handle:
                    handle.write(
                        f'name = "{name}"\nunit_prefix = "{name}-"\nroot = "/tmp/{name}"\n{extra}'
                    )
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": tmp}):
                server, _ = self.start_server(FakeBuilder(healthy_doc()))
                self.assertEqual(
                    self.get_json(server, "/v1/projects"), {"projects": ["alpha", "beta"]}
                )

    def test_projects_missing_registry_is_an_empty_list(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-projects-") as tmp:
            missing = os.path.join(tmp, "absent")
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": missing}):
                server, _ = self.start_server(FakeBuilder(healthy_doc()))
                self.assertEqual(self.get_json(server, "/v1/projects"), {"projects": []})

    def test_projects_bad_file_is_a_generic_500(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-projects-") as tmp:
            directory = os.path.join(tmp, projects.PROJECTS_DIR)
            os.makedirs(directory)
            bad = os.path.join(directory, "broken.toml")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write('name = "broken"\nunknown_key = 1\n')
            with mock.patch.dict(os.environ, {"KORAPY_CONFIG": tmp}):
                server, _ = self.start_server(FakeBuilder(healthy_doc()))
                err = io.StringIO()
                with redirect_stderr(err):
                    status, _, body = self.fetch(server, "GET", "/v1/projects")
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(body), {"error": "projects unavailable"})
                self.assertIn("broken.toml", err.getvalue())
                self.assertNotIn(tmp, body.decode("utf-8"))

    def test_projects_needs_a_token_when_required(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            token_required=True,
            token_reader=lambda: "tok-1",
            projects_fn=lambda: [],
        )
        status, _, body = self.fetch(server, "GET", "/v1/projects")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        self.assertEqual(self.get_json(server, "/v1/projects", token="tok-1"), {"projects": []})


class ProductionProjectReaders(unittest.TestCase):
    def test_events_reader_returns_the_projects_queue_oldest_first(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-events-") as tmp:
            root = os.path.join(tmp, "projects", "demo")
            written = coordevents.record(
                [
                    {"kind": "held-merge", "ref": "app#7", "evidence": "e1", "state": "s1"},
                    {"kind": "coordinator-review", "ref": "app#9", "evidence": "e2"},
                ],
                root=root,
                now=T0,
                event_id_fn=iter(["a1b2c3d4", "e5f6a7b8"]).__next__,
            )
            with mock.patch.dict(os.environ, {"KORAPY_STATE": tmp}):
                self.assertEqual(serve.read_project_events("demo"), written)
                self.assertEqual(serve.read_project_events("demo"), coordevents.read_events(root))
            with mock.patch.dict(os.environ, {"KORAPY_STATE": tmp}):
                with self.assertRaises(coord.UsageError):
                    serve.read_project_events("../..")

    def test_journal_reader_returns_archives_then_live(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-journal-") as tmp:
            root = os.path.join(tmp, "projects", "demo")
            directory = os.path.join(root, coord.LEASE_DIRNAME)
            os.makedirs(directory)
            archived_old = {
                "at": "2026-01-01T00:00:00+00:00",
                "holder": "",
                "generation": 1,
                "kind": "note",
                "refs": [],
                "text": "archived old",
            }
            archived_new = dict(archived_old, at="2026-01-03T00:00:00+00:00", text="archived new")
            live = dict(archived_old, at="2026-01-05T00:00:00+00:00", text="live")
            with open(
                os.path.join(directory, f"{coord.JOURNAL_ARCHIVE_PREFIX}2026-01-03.jsonl"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(json.dumps(archived_new) + "\n")
                handle.write("not json\n")
                handle.write("\n")
                handle.write("[1, 2]\n")
            with open(
                os.path.join(directory, f"{coord.JOURNAL_ARCHIVE_PREFIX}2026-01-01.jsonl"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(json.dumps(archived_old) + "\n")
            with open(os.path.join(directory, coord.JOURNAL_NAME), "w", encoding="utf-8") as handle:
                handle.write(json.dumps(live) + "\n")
            with mock.patch.dict(os.environ, {"KORAPY_STATE": tmp}):
                self.assertEqual(
                    serve.read_project_journal("demo"), [archived_old, archived_new, live]
                )
                self.assertEqual(serve.read_project_journal("quiet"), [])
            with mock.patch.dict(os.environ, {"KORAPY_STATE": tmp}):
                with self.assertRaises(coord.UsageError):
                    serve.read_project_journal("../..")


def sse_field(line, prefix):
    """The value of one `prefix: value` SSE line (one space stripped)."""
    rest = line[len(prefix) :]
    return rest[1:] if rest.startswith(" ") else rest


class StreamConn:
    """An open SSE request: the connection, its response, and the live socket.

    `getresponse` moves the socket into the response, so each frame
    read sets its timeout through this handle instead of `conn.sock`.
    `close` hangs up like a browser tab closing: the response owns
    the socket, so closing the connection alone releases nothing.
    """

    def __init__(
        self,
        conn: http.client.HTTPConnection,
        resp: http.client.HTTPResponse,
        sock: socket.socket,
    ) -> None:
        self.conn = conn
        self.resp = resp
        self.sock = sock

    def close(self) -> None:
        try:
            self.resp.close()
        finally:
            self.conn.close()


class StreamCase(ServeCase):
    """A live loopback server plus an incremental SSE reader per test."""

    def start_stream_server(self, build=None, **kwargs):
        kwargs.setdefault("clock", lambda: T0)
        kwargs.setdefault("quiet", True)
        kwargs.setdefault("stream_poll_s", 0.02)
        kwargs.setdefault("stream_heartbeat_s", 60.0)
        server = serve.make_server(build=build or FakeBuilder(healthy_doc()), **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.daemon = True
        thread.start()
        self.addCleanup(self._stop, server, thread)
        return server

    def open_stream(self, server, target, last_id=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        headers = {}
        if last_id is not None:
            headers["Last-Event-ID"] = last_id
        conn.request("GET", target, headers=headers)
        # Read the socket before getresponse moves it into the response.
        sock = conn.sock
        assert sock is not None  # requested and unanswered: the socket is live
        stream = StreamConn(conn, conn.getresponse(), sock)
        self.addCleanup(stream.close)
        return stream

    def read_frame(self, stream, timeout=10):
        """One SSE frame: the lines up to the blank terminator."""
        stream.sock.settimeout(timeout)
        lines = []
        try:
            while True:
                raw = stream.resp.fp.readline()
                if not raw:
                    raise AssertionError("stream closed before a full frame")
                text = raw.decode("utf-8")
                text = text[:-1] if text.endswith("\n") else text
                text = text[:-1] if text.endswith("\r") else text
                if not text:
                    return lines
                lines.append(text)
        except socket.timeout:
            raise AssertionError(f"no stream frame within {timeout}s (got {lines!r})")

    def frame_fields(self, lines):
        fields = {}
        for line in lines:
            if line.startswith(":"):
                fields.setdefault("comments", []).append(line)
            elif line.startswith("data:"):
                fields.setdefault("data", []).append(sse_field(line, "data:"))
            else:
                name, _, value = line.partition(":")
                fields[name.strip()] = value.strip()
        return fields

    def wait_for_streams(self, server, count, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with server.state.lock:
                if server.state.live_streams == count:
                    return
            time.sleep(0.02)
        self.fail(f"live streams did not reach {count}")


class StreamHandshake(StreamCase):
    def test_stream_is_an_event_stream_with_ids(self):
        server = self.start_stream_server(events_fn=lambda project: [], journal_fn=lambda p: [])
        stream = self.open_stream(server, "/v1/stream?topics=snapshot&project=demo")
        self.assertEqual(stream.resp.status, 200)
        self.assertEqual(stream.resp.getheader("Content-Type"), "text/event-stream")
        fields = self.frame_fields(self.read_frame(stream))
        self.assertEqual(fields["event"], "snapshot")
        self.assertTrue(fields["id"].startswith("snapshot:"))
        self.assertIn("digest", json.loads(fields["data"][0]))

    def test_stream_topics_subset_suppresses_the_rest(self):
        entries = [journal_entry(0)]
        server = self.start_stream_server(
            events_fn=lambda project: list(EVENT_ROWS),
            journal_fn=lambda project: list(entries),
        )
        stream = self.open_stream(server, "/v1/stream?topics=journal&project=demo")
        self.assertEqual(stream.resp.status, 200)
        entries.append(journal_entry(1))
        fields = self.frame_fields(self.read_frame(stream))
        self.assertEqual(fields["event"], "journal")
        self.assertEqual(fields["id"], "journal:2")
        self.assertEqual([e["text"] for e in json.loads(fields["data"][0])["entries"]], ["entry 1"])

    def test_stream_unknown_topic_is_a_400(self):
        server = self.start_stream_server()
        status, _, body = self.fetch(server, "GET", "/v1/stream?topics=snapshot,bogus")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_stream_events_and_journal_need_a_project(self):
        server = self.start_stream_server()
        for target in ("/v1/stream", "/v1/stream?topics=events,journal"):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))
        stream = self.open_stream(server, "/v1/stream?topics=snapshot")
        self.assertEqual(stream.resp.status, 200)
        self.read_frame(stream)

    def test_stream_bad_cursor_is_a_400(self):
        server = self.start_stream_server(journal_fn=lambda project: [journal_entry(0)])
        for target in (
            "/v1/stream?topics=journal&project=demo&cursor=bogus",
            "/v1/stream?topics=journal&project=demo&cursor=journal:-1",
            "/v1/stream?topics=journal&project=demo&cursor=journal:",
        ):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))

    def test_stream_stray_last_event_id_restarts_at_the_end(self):
        entries = [journal_entry(0)]
        server = self.start_stream_server(journal_fn=lambda project: list(entries))
        stream = self.open_stream(server, "/v1/stream?topics=journal&project=demo", last_id="bogus")
        self.assertEqual(stream.resp.status, 200)
        entries.append(journal_entry(1))
        fields = self.frame_fields(self.read_frame(stream))
        self.assertEqual(fields["id"], "journal:2")
        self.assertEqual([e["text"] for e in json.loads(fields["data"][0])["entries"]], ["entry 1"])

    def test_stream_builder_failure_is_a_500(self):
        def boom():
            raise OSError("gone")

        server = self.start_stream_server(build=boom)
        stream = self.open_stream(server, "/v1/stream?topics=snapshot")
        self.assertEqual(stream.resp.status, 500)
        self.assertEqual(json.loads(stream.resp.read()), {"error": "stream unavailable"})

    def test_stream_bad_project_is_a_400(self):
        def boom(project):
            raise coord.UsageError("bad project name")

        server = self.start_stream_server(events_fn=boom)
        stream = self.open_stream(server, "/v1/stream?topics=events&project=../..")
        self.assertEqual(stream.resp.status, 400)
        self.assertIn("error", json.loads(stream.resp.read()))


class StreamLiveUpdates(StreamCase):
    def test_snapshot_digest_moves_on_rebuild(self):
        builder = FakeBuilder(healthy_doc())
        server = self.start_stream_server(build=builder)
        stream = self.open_stream(server, "/v1/stream?topics=snapshot")
        first = self.frame_fields(self.read_frame(stream))
        builder.doc = dict(healthy_doc(), drive={"error": "changed"})
        second = self.frame_fields(self.read_frame(stream))
        self.assertEqual(first["event"], "snapshot")
        self.assertEqual(second["event"], "snapshot")
        self.assertNotEqual(first["id"], second["id"])

    def test_events_emit_on_change_with_unresolved_only(self):
        rows = [dict(EVENT_ROWS[0])]
        server = self.start_stream_server(events_fn=lambda project: list(rows))
        stream = self.open_stream(server, "/v1/stream?topics=events&project=demo")
        first = self.frame_fields(self.read_frame(stream))
        self.assertEqual([e["id"] for e in json.loads(first["data"][0])["events"]], ["a1b2c3d4"])
        rows.append(dict(EVENT_ROWS[2], resolved_at=None))
        second = self.frame_fields(self.read_frame(stream))
        self.assertEqual(
            [e["id"] for e in json.loads(second["data"][0])["events"]],
            ["a1b2c3d4", "c9d0e1f2"],
        )
        self.assertNotEqual(first["id"], second["id"])

    def test_journal_resumes_from_last_event_id_without_gaps(self):
        entries = [journal_entry(n) for n in range(5)]
        server = self.start_stream_server(journal_fn=lambda project: list(entries))
        stream = self.open_stream(server, "/v1/stream?topics=journal&project=demo")
        entries.extend([journal_entry(5), journal_entry(6)])
        first = self.frame_fields(self.read_frame(stream))
        self.assertEqual(first["id"], "journal:7")
        stream.close()
        entries.append(journal_entry(7))
        stream2 = self.open_stream(
            server, "/v1/stream?topics=journal&project=demo", last_id=first["id"]
        )
        second = self.frame_fields(self.read_frame(stream2))
        self.assertEqual(second["id"], "journal:8")
        got = json.loads(first["data"][0])["entries"] + json.loads(second["data"][0])["entries"]
        self.assertEqual([e["text"] for e in got], ["entry 5", "entry 6", "entry 7"])
        stream2.close()

    def test_journal_cursor_param_replays_the_backlog(self):
        entries = [journal_entry(n) for n in range(5)]
        server = self.start_stream_server(journal_fn=lambda project: list(entries))
        stream = self.open_stream(server, "/v1/stream?topics=journal&project=demo&cursor=journal:3")
        fields = self.frame_fields(self.read_frame(stream))
        self.assertEqual(fields["id"], "journal:5")
        self.assertEqual(
            [e["text"] for e in json.loads(fields["data"][0])["entries"]],
            ["entry 3", "entry 4"],
        )

    def test_mixed_topics_resume_from_any_frame_without_gaps(self):
        entries = [journal_entry(n) for n in range(5)]
        server = self.start_stream_server(
            journal_fn=lambda project: list(entries), events_fn=lambda project: []
        )
        stream = self.open_stream(server, "/v1/stream?topics=snapshot,journal&project=demo")
        snap = self.frame_fields(self.read_frame(stream))
        self.assertEqual(snap["event"], "snapshot")
        self.assertTrue(snap["id"].startswith("5:snapshot:"))
        entries.extend([journal_entry(5), journal_entry(6)])
        first = self.frame_fields(self.read_frame(stream))
        self.assertEqual(first["id"], "journal:7")
        stream.close()
        entries.append(journal_entry(7))
        # Reconnect from the snapshot frame, not the journal one: the
        # prefixed cursor still resumes the log where it was.
        stream2 = self.open_stream(
            server, "/v1/stream?topics=snapshot,journal&project=demo", last_id=snap["id"]
        )
        self.frame_fields(self.read_frame(stream2))
        second = self.frame_fields(self.read_frame(stream2))
        self.assertEqual(second["id"], "journal:8")
        # At-least-once replay from the resume point: 5 and 6 repeat,
        # 7 is new, and nothing in between is missing.
        self.assertEqual(
            [e["text"] for e in json.loads(second["data"][0])["entries"]],
            ["entry 5", "entry 6", "entry 7"],
        )
        stream2.close()

    def test_prefixed_cursor_param_replays_from_its_index(self):
        entries = [journal_entry(n) for n in range(5)]
        server = self.start_stream_server(journal_fn=lambda project: list(entries))
        stream = self.open_stream(
            server, "/v1/stream?topics=journal&project=demo&cursor=2:journal:3"
        )
        fields = self.frame_fields(self.read_frame(stream))
        self.assertEqual(fields["id"], "journal:5")
        self.assertEqual(
            [e["text"] for e in json.loads(fields["data"][0])["entries"]],
            ["entry 2", "entry 3", "entry 4"],
        )

    def test_journal_matches_polling(self):
        entries = [journal_entry(n) for n in range(4)]
        server = self.start_stream_server(
            journal_fn=lambda project: list(entries),
            events_fn=lambda project: [],
        )
        stream = self.open_stream(server, "/v1/stream?topics=journal&project=demo&cursor=journal:1")
        fields = self.frame_fields(self.read_frame(stream))
        status, _, body = self.fetch(server, "GET", "/v1/journal?project=demo&cursor=1")
        self.assertEqual(status, 200)
        polled = json.loads(body)
        self.assertEqual(json.loads(fields["data"][0])["entries"], polled["entries"])
        self.assertEqual(json.loads(fields["data"][0])["cursor"], polled["cursor"])

    def test_heartbeat_arrives_when_idle(self):
        server = self.start_stream_server(
            clock=time.time,
            stream_heartbeat_s=0.05,
            events_fn=lambda project: [],
            journal_fn=lambda p: [],
        )
        stream = self.open_stream(server, "/v1/stream?topics=snapshot")
        self.frame_fields(self.read_frame(stream))
        deadline = time.time() + 5
        while True:
            fields = self.frame_fields(self.read_frame(stream, timeout=5))
            if "comments" in fields:
                self.assertIn(": heartbeat", fields["comments"])
                return
            if time.time() > deadline:
                self.fail("no heartbeat arrived while idle")


class StreamShedding(StreamCase):
    def test_past_max_streams_sheds_with_retry_after(self):
        server = self.start_stream_server(
            max_streams=2,
            stream_heartbeat_s=3600.0,
            events_fn=lambda project: [],
            journal_fn=lambda p: [],
        )
        stream1 = self.open_stream(server, "/v1/stream?topics=snapshot")
        self.assertEqual(stream1.resp.status, 200)
        self.read_frame(stream1)
        stream2 = self.open_stream(server, "/v1/stream?topics=snapshot")
        self.assertEqual(stream2.resp.status, 200)
        self.read_frame(stream2)
        status, headers, body = self.fetch(server, "GET", "/v1/stream?topics=snapshot")
        self.assertEqual(status, 503)
        self.assertIn("Retry-After", headers)
        self.assertEqual(json.loads(body), {"error": "too many streams"})
        stream1.close()
        stream2.close()
        self.wait_for_streams(server, 0)

    def test_closed_stream_releases_its_slot(self):
        server = self.start_stream_server(
            max_streams=1,
            stream_heartbeat_s=3600.0,
            events_fn=lambda project: [],
            journal_fn=lambda p: [],
        )
        stream = self.open_stream(server, "/v1/stream?topics=snapshot")
        self.assertEqual(stream.resp.status, 200)
        self.read_frame(stream)
        status, _, _ = self.fetch(server, "GET", "/v1/stream?topics=snapshot")
        self.assertEqual(status, 503)
        stream.close()
        self.wait_for_streams(server, 0)
        stream2 = self.open_stream(server, "/v1/stream?topics=snapshot")
        self.assertEqual(stream2.resp.status, 200)
        self.read_frame(stream2)
        stream2.close()
        self.wait_for_streams(server, 0)


class StreamSettings(unittest.TestCase):
    def test_max_streams_default_defined_once(self):
        import inspect

        self.assertEqual(serve.MAX_STREAMS_DEFAULT, 20)
        self.assertEqual(settings.REGISTRY["serve.max_streams"].default, serve.MAX_STREAMS_DEFAULT)
        params = inspect.signature(serve.make_server).parameters
        self.assertEqual(params["max_streams"].default, serve.MAX_STREAMS_DEFAULT)

    def test_journal_and_stream_bounds(self):
        self.assertEqual(serve.JOURNAL_LIMIT_DEFAULT, 100)
        self.assertEqual(serve.JOURNAL_LIMIT_MAX, 1000)
        self.assertEqual(serve.STREAM_TOPICS, ("snapshot", "events", "journal"))
        self.assertEqual(serve.STREAM_POLL_S, 0.2)
        self.assertEqual(serve.STREAM_HEARTBEAT_S, 15.0)

    def test_settings_max_streams_reaches_the_server(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-stream-") as tmp:
            config_dir = os.path.join(tmp, "config")
            state_dir = os.path.join(tmp, "state")
            os.makedirs(config_dir)
            os.makedirs(state_dir)
            with open(os.path.join(config_dir, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write("[serve]\nmax_streams = 3\n")
            args = cli.build_parser().parse_args(["serve"])
            seen = {}

            class FakeServer:
                server_address = ("127.0.0.1", 4321)

                def serve_forever(self):
                    pass

                def server_close(self):
                    pass

            def fake_make(**kwargs):
                seen.update(kwargs)
                return FakeServer()

            with mock.patch.dict(
                os.environ, {"KORAPY_CONFIG": config_dir, "KORAPY_STATE": state_dir}
            ):
                with mock.patch.object(serve, "make_server", side_effect=fake_make):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(args.func(args), 0)
            self.assertEqual(seen.get("max_streams"), 3)


class DigestVolatility(unittest.TestCase):
    def test_volatile_keys_constant_names_now_and_read_at(self):
        self.assertEqual(serve.VOLATILE_KEYS, frozenset({"now", "read_at"}))

    def test_digest_ignores_top_level_volatile_keys(self):
        self.assertEqual(
            serve._digest({"now": 1, "agents": []}),
            serve._digest({"now": 2, "agents": []}),
        )

    def test_digest_ignores_nested_read_at(self):
        before = {"quota": {"prov": {"read_at": 1.0, "stale": False}}}
        after = {"quota": {"prov": {"read_at": 2.0, "stale": False}}}
        self.assertEqual(serve._digest(before), serve._digest(after))

    def test_digest_moves_on_real_change(self):
        before = {"now": 1, "agents": []}
        after = {"now": 1, "agents": [{"id": "a1"}]}
        self.assertNotEqual(serve._digest(before), serve._digest(after))


class TickingBuilder:
    """A snapshot builder whose volatile stamps tick on every call."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        return {
            "schema_version": 1,
            "fleet_header": {"host": "host-1", "now": time.time()},
            "quota": {"prov": {"read_at": time.time(), "stale": False}},
            "n": self.n,
        }


class StreamDigestStability(StreamCase):
    def test_ticking_clock_emits_no_repeat_frame(self):
        builder = TickingBuilder()
        server = self.start_stream_server(
            build=builder,
            clock=time.time,
            stream_poll_s=0.02,
            stream_heartbeat_s=0.3,
            events_fn=lambda project: [],
            journal_fn=lambda project: [],
        )
        stream = self.open_stream(server, "/v1/stream?topics=snapshot")
        first = self.frame_fields(self.read_frame(stream))
        self.assertEqual(first["event"], "snapshot")
        # Nothing changed: the next frame is the heartbeat, not a snapshot.
        ticking = self.frame_fields(self.read_frame(stream, timeout=5))
        self.assertNotIn("event", ticking)
        self.assertIn(": heartbeat", ticking.get("comments", []))
        # Real content changes: a snapshot frame with a new digest.
        builder.n = 1
        changed = self.frame_fields(self.read_frame(stream, timeout=5))
        self.assertEqual(changed["event"], "snapshot")
        self.assertNotEqual(first["id"], changed["id"])
        stream.close()
        self.wait_for_streams(server, 0)


class StreamBlankTopics(StreamCase):
    def test_blank_topics_is_a_400(self):
        server = self.start_stream_server(
            events_fn=lambda project: [], journal_fn=lambda project: []
        )
        for target in (
            "/v1/stream?topics=&project=demo",
            "/v1/stream?topics=,,,&project=demo",
            "/v1/stream?topics=%20&project=demo",
        ):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))


class JournalBlankKind(ServeCase):
    def test_blank_kind_is_a_400(self):
        seen = []
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            journal_fn=lambda project: (seen.append(project), [journal_entry(0)])[1],
        )
        for target in (
            "/v1/journal?project=demo&kind=",
            "/v1/journal?project=demo&kind=,,,",
        ):
            with self.subTest(target=target):
                status, _, body = self.fetch(server, "GET", target)
                self.assertEqual(status, 400, target)
                self.assertIn("error", json.loads(body))
        self.assertEqual(seen, [])


class StreamProjectGone(StreamCase):
    def test_stream_closes_when_the_project_goes_away(self):
        for topics in ("journal", "events"):
            with self.subTest(topics=topics):
                if topics == "journal":
                    entries = [journal_entry(0)]
                    calls = []

                    def journal_fn(project):
                        calls.append(project)
                        if len(calls) > 1:
                            raise coord.UsageError("renamed mid-stream")
                        return list(entries)

                    kwargs = {"journal_fn": journal_fn, "events_fn": lambda project: []}
                else:
                    rows = [dict(EVENT_ROWS[0])]
                    calls = []

                    def events_fn(project):
                        calls.append(project)
                        if len(calls) > 1:
                            raise coord.UsageError("renamed mid-stream")
                        return list(rows)

                    kwargs = {"events_fn": events_fn, "journal_fn": lambda project: []}
                server = self.start_stream_server(**kwargs)
                stream = self.open_stream(server, f"/v1/stream?topics={topics}&project=demo")
                if topics == "events":
                    self.frame_fields(self.read_frame(stream))
                stream.sock.settimeout(5)
                self.assertEqual(stream.resp.fp.readline(), b"")
                stream.close()
                self.wait_for_streams(server, 0)


class ClientGonePeek(unittest.TestCase):
    def test_client_gone_peeks_instead_of_consuming(self):
        with open(serve.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_client_gone"
        )
        recvs = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "recv"
        ]
        self.assertEqual(len(recvs), 1)
        self.assertEqual(len(recvs[0].args), 2)
        flag = recvs[0].args[1]
        self.assertTrue(isinstance(flag, ast.Attribute) and flag.attr == "MSG_PEEK")


class TransitiveSeamClosure(unittest.TestCase):
    def test_transitive_closure_has_no_hub_or_forge(self):
        closed = transitive_korapy_modules("korapy.serve")
        self.assertIn("korapy.serve", closed)
        self.assertIn("korapy.board", closed)
        self.assertNotIn("korapy.hub", closed)
        self.assertNotIn("korapy.forge", closed)

    def test_transitive_closure_seams_match_allowlist(self):
        import importlib.util

        closed = transitive_korapy_modules("korapy.serve")
        found = set()
        for name in sorted(closed):
            spec = importlib.util.find_spec(name)
            if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
                continue
            with open(spec.origin, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in ("subprocess", "socket"):
                            found.add((name, top))
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    if top in ("subprocess", "socket"):
                        found.add((name, top))
        allowlist = {
            (
                "korapy.drive",
                "socket",
            ): "socket.gethostname names the agent host segment; no packets",
            (
                "korapy.drive",
                "subprocess",
            ): "timer helpers other verbs call; no request handler reaches them",
            (
                "korapy.github.gh",
                "subprocess",
            ): "the gh seam other verbs call; reads inject fakes instead",
            (
                "korapy.launch",
                "socket",
            ): "socket.gethostname names launch records; the tail read never shells out",
            ("korapy.netbind", "socket"): "address-family constants for the loopback bind",
            ("korapy.registry", "socket"): "socket.gethostname tags live-agent rows; local only",
            ("korapy.serve", "socket"): "MSG_PEEK flag for the client-close probe; opens nothing",
            ("korapy.snapshot", "socket"): "socket.gethostname fills the fleet header; local only",
            (
                "korapy.syncli",
                "subprocess",
            ): "sync subprocess seam other verbs call; reads never reach it",
            (
                "korapy.units",
                "subprocess",
            ): "unit control seam other verbs call; reads never reach it",
            (
                "korapy.worktrees",
                "subprocess",
            ): "git worktree seam other verbs call; reads never reach it",
        }
        for reason in allowlist.values():
            self.assertTrue(reason and "\n" not in reason, reason)
        self.assertEqual(found, set(allowlist))

    def test_journal_reader_skips_undated_archives(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-journal-") as tmp:
            root = os.path.join(tmp, "projects", "demo")
            directory = os.path.join(root, coord.LEASE_DIRNAME)
            os.makedirs(directory)
            live = {
                "at": "2026-01-05T00:00:00+00:00",
                "holder": "",
                "generation": 1,
                "kind": "note",
                "refs": [],
                "text": "live",
            }
            dated = dict(live, at="2026-01-01T00:00:00+00:00", text="dated")
            stray = dict(live, at="2026-01-02T00:00:00+00:00", text="stray")
            with open(
                os.path.join(directory, f"{coord.JOURNAL_ARCHIVE_PREFIX}2026-01-01.jsonl"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(json.dumps(dated) + "\n")
            with open(
                os.path.join(directory, "journal-draft.jsonl"), "w", encoding="utf-8"
            ) as handle:
                handle.write(json.dumps(stray) + "\n")
            with open(os.path.join(directory, coord.JOURNAL_NAME), "w", encoding="utf-8") as handle:
                handle.write(json.dumps(live) + "\n")
            with mock.patch.dict(os.environ, {"KORAPY_STATE": tmp}):
                self.assertEqual(serve.read_project_journal("demo"), [dated, live])


class SnapshotCache(ServeCase):
    """S5: the snapshot TTL cache, the generation ETag, and 304s.

    Offline: a live loopback server with a counting builder fake and a
    controllable clock. No `gh`, no network beyond loopback.
    """

    def start_cached(self, builder, ttl=10, **kwargs):
        return self.start_server(builder, snapshot_ttl_s=ttl, **kwargs)

    def get(self, server, target, etag=None):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {} if etag is None else {"If-None-Match": etag}
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_repeated_polls_within_ttl_share_one_build(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder)
        for _ in range(3):
            status, _, body = self.get(server, "/v1/snapshot")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), healthy_doc())
        self.assertEqual(builder.calls, 1)

    def test_poll_past_ttl_rebuilds_with_a_new_etag(self):
        builder = FakeBuilder(healthy_doc())
        server, now = self.start_cached(builder)
        _, headers, _ = self.get(server, "/v1/snapshot")
        first = headers.get("ETag")
        self.assertTrue(first)
        now[0] = T0 + 11
        status, headers, _ = self.get(server, "/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(builder.calls, 2)
        self.assertIsNotNone(headers.get("ETag"))
        self.assertNotEqual(headers.get("ETag"), first)

    def test_zero_ttl_rebuilds_every_time(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder, ttl=0)
        self.get(server, "/v1/snapshot")
        self.get(server, "/v1/snapshot")
        self.assertEqual(builder.calls, 2)

    def test_views_share_the_cached_build(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder)
        self.get(server, "/v1/snapshot")
        self.get(server, "/v1/agents")
        self.get(server, "/v1/quota")
        self.assertEqual(builder.calls, 1)

    def test_failed_build_never_populates_the_cache(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("gone")
            return healthy_doc()

        server, _ = self.start_cached(flaky)
        status, _, _ = self.get(server, "/v1/snapshot")
        self.assertEqual(status, 500)
        status, _, body = self.get(server, "/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), healthy_doc())
        self.assertEqual(len(calls), 2)

    def test_non_dict_build_never_populates_the_cache(self):
        # Mirrors the flaky-builder test above for the non-dict path: a
        # builder that returns a list once and then a valid document must
        # read as a 500 followed by a 200, proving the bad result was not
        # cached (a cached list would answer the second request from one
        # build, and with the wrong body).
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                return ["agents"]
            return healthy_doc()

        server, _ = self.start_cached(flaky)
        status, _, body = self.get(server, "/v1/snapshot")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "snapshot unavailable"})
        status, _, body = self.get(server, "/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), healthy_doc())
        self.assertEqual(len(calls), 2)

    def test_snapshot_carries_etag_and_last_modified(self):
        server, _ = self.start_cached(FakeBuilder(healthy_doc()))
        status, headers, _ = self.get(server, "/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("ETag", "").startswith('"'))
        self.assertIn("Last-Modified", headers)

    def test_matching_etag_is_a_304_counted_in_health(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder)
        _, headers, _ = self.get(server, "/v1/snapshot")
        etag = headers.get("ETag")
        status, again, body = self.get(server, "/v1/snapshot", etag=etag)
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(again.get("ETag"), etag)
        self.assertEqual(builder.calls, 1)
        _, _, health = self.get(server, "/v1/health")
        payload = json.loads(health)
        self.assertEqual(payload["requests_total"], 3)
        self.assertEqual(payload["requests_304"], 1)

    def test_stale_etag_after_rebuild_is_200_with_a_new_etag(self):
        builder = FakeBuilder(healthy_doc())
        server, now = self.start_cached(builder)
        _, headers, _ = self.get(server, "/v1/snapshot")
        old = headers.get("ETag")
        now[0] = T0 + 11
        status, headers, _ = self.get(server, "/v1/snapshot", etag=old)
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("ETag"), old)

    def test_wrong_etag_is_200(self):
        server, _ = self.start_cached(FakeBuilder(healthy_doc()))
        status, _, body = self.get(server, "/v1/snapshot", etag='"no-such-generation"')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), healthy_doc())

    def test_wildcard_etag_matches(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder)
        status, _, body = self.get(server, "/v1/snapshot", etag="*")
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(builder.calls, 1)

    def test_etag_is_shared_across_snapshot_views(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder)
        _, headers, _ = self.get(server, "/v1/snapshot")
        etag = headers.get("ETag")
        for target in ("/v1/agents", "/v1/quota", "/v1/capacity", "/v1/disks"):
            with self.subTest(target=target):
                status, _, body = self.get(server, target, etag=etag)
                self.assertEqual(status, 304, target)
                self.assertEqual(body, b"")
        self.assertEqual(builder.calls, 1)

    def test_health_reports_both_counters(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder)
        _, headers, _ = self.get(server, "/v1/snapshot")
        self.get(server, "/v1/snapshot", etag=headers.get("ETag"))
        _, _, body = self.get(server, "/v1/health")
        payload = json.loads(body)
        self.assertEqual(payload["requests_total"], 3)
        self.assertEqual(payload["requests_304"], 1)
        self.assertTrue(payload["ok"])

    def test_health_matching_etag_is_a_304_counted(self):
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder)
        _, headers, _ = self.get(server, "/v1/health")
        etag = headers.get("ETag")
        self.assertTrue(etag)
        status, again, body = self.get(server, "/v1/health", etag=etag)
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(again.get("ETag"), etag)
        self.assertEqual(builder.calls, 1)
        _, _, fresh = self.get(server, "/v1/health")
        payload = json.loads(fresh)
        self.assertEqual(payload["requests_total"], 3)
        self.assertEqual(payload["requests_304"], 1)

    def get_with_origin(self, server, target, etag=None, origin=None):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {}
            if etag is not None:
                headers["If-None-Match"] = etag
            if origin is not None:
                headers["Origin"] = origin
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_304_echoes_matching_origin(self):
        origin = "https://dash.example.com"
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder, allowed_origins=(origin,))
        _, headers, _ = self.get_with_origin(server, "/v1/snapshot", origin=origin)
        etag = headers.get("ETag")
        self.assertTrue(etag)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)
        status, again, body = self.get_with_origin(server, "/v1/snapshot", etag=etag, origin=origin)
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(again.get("ETag"), etag)
        self.assertEqual(again.get("Access-Control-Allow-Origin"), origin)

    def test_304_without_matching_origin_has_no_echo(self):
        origin = "https://dash.example.com"
        builder = FakeBuilder(healthy_doc())
        server, _ = self.start_cached(builder, allowed_origins=(origin,))
        _, headers, _ = self.get_with_origin(server, "/v1/snapshot")
        etag = headers.get("ETag")
        status, again, body = self.get_with_origin(
            server, "/v1/snapshot", etag=etag, origin="https://other.example.com"
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(again.get("ETag"), etag)
        self.assertNotIn("Access-Control-Allow-Origin", again)

    def test_preflight_emits_origin_exactly_once(self):
        origin = "https://dash.example.com"
        server, _ = self.start_cached(FakeBuilder(healthy_doc()), allowed_origins=(origin,))
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(
                "OPTIONS",
                "/v1/snapshot",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                },
            )
            resp = conn.getresponse()
            raw = resp.getheaders()
            body = resp.read()
            status = resp.status
        finally:
            conn.close()
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        echoed = [v for k, v in raw if k.lower() == "access-control-allow-origin"]
        self.assertEqual(echoed, [origin])


class SnapshotTtlWiring(ServeCommandTest):
    def test_snapshot_ttl_defaults_to_two_seconds(self):
        class FakeServer:
            server_address = ("127.0.0.1", 4321)

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory(prefix="korapy-serve-ttl-") as tmp:
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=FakeServer()) as made:
                    args = self.parse()
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(args.func(args), 0)
        self.assertEqual(made.call_args.kwargs["snapshot_ttl_s"], 2)

    def test_snapshot_ttl_default_is_one_fact(self):
        self.assertEqual(
            settings.REGISTRY["serve.snapshot_ttl_s"].default,
            serve.SNAPSHOT_TTL_S_DEFAULT,
        )
        state_default = (
            inspect.signature(serve._State.__init__).parameters["snapshot_ttl_s"].default
        )
        make_default = inspect.signature(serve.make_server).parameters["snapshot_ttl_s"].default
        self.assertEqual(state_default, serve.SNAPSHOT_TTL_S_DEFAULT)
        self.assertEqual(make_default, serve.SNAPSHOT_TTL_S_DEFAULT)

    def test_snapshot_ttl_setting_reaches_make_server(self):
        class FakeServer:
            server_address = ("127.0.0.1", 4321)

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        config = "[serve]\nsnapshot_ttl_s = 9\n"
        with tempfile.TemporaryDirectory(prefix="korapy-serve-ttl-") as tmp:
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with open(os.path.join(tmp, "config.toml"), "w", encoding="utf-8") as handle:
                    handle.write(config)
                with mock.patch.object(serve, "make_server", return_value=FakeServer()) as made:
                    args = self.parse()
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(args.func(args), 0)
        self.assertEqual(made.call_args.kwargs["snapshot_ttl_s"], 9)


def _serve_subparser():
    """The `serve` subparser, so help-text tests read what the CLI shows."""
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices["serve"]
    raise AssertionError("no serve subparser")


def _port_help():
    """The `--port` help text of the `serve` verb."""
    for action in _serve_subparser()._actions:
        if "--port" in action.option_strings:
            return action.help or ""
    raise AssertionError("no --port flag on serve")


class ServePortDefaultTest(unittest.TestCase):
    def test_default_port_is_fixed_and_unprivileged(self):
        self.assertEqual(serve.SERVE_PORT_DEFAULT, 8420)
        self.assertGreaterEqual(serve.SERVE_PORT_DEFAULT, 1024)
        self.assertLessEqual(serve.SERVE_PORT_DEFAULT, 65535)
        self.assertNotEqual(serve.SERVE_PORT_DEFAULT, 0)

    def test_setting_default_is_the_constant(self):
        self.assertEqual(settings.REGISTRY["serve.port"].default, serve.SERVE_PORT_DEFAULT)

    def test_setting_help_names_the_choice_and_zero(self):
        text = str(settings.REGISTRY["serve.port"].help)
        self.assertIn(str(serve.SERVE_PORT_DEFAULT), text)
        self.assertIn("0", text)

    def test_port_help_quotes_the_default_and_zero(self):
        text = _port_help()
        self.assertIn(str(serve.SERVE_PORT_DEFAULT), text)
        self.assertIn("0", text)

    def test_docs_quote_the_default_port(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        files = (
            root / "README.md",
            root / "docs" / "guide.md",
            root / "docs" / "12-web-dashboard.md",
            root / "docs" / "05-dashboard-api.md",
        )
        for path in files:
            with self.subTest(path=str(path)):
                self.assertIn(str(serve.SERVE_PORT_DEFAULT), path.read_text(encoding="utf-8"))


class StartupLinesTest(unittest.TestCase):
    def test_loopback_is_a_clickable_url(self):
        self.assertEqual(
            serve.startup_lines("127.0.0.1", 4321),
            ["korapy serve: dashboard at http://127.0.0.1:4321/"],
        )

    def test_ipv6_url_carries_brackets(self):
        self.assertEqual(
            serve.startup_lines("::1", 4321),
            ["korapy serve: dashboard at http://[::1]:4321/"],
        )

    def test_wildcard_names_the_bind_and_the_client_address(self):
        for bind in ("0.0.0.0", "::"):
            with self.subTest(bind=bind):
                text = "\n".join(serve.startup_lines(bind, 8420))
                self.assertIn(f"{bind}:8420", text)
                self.assertIn("LAN address", text)

    def test_token_line_names_the_file_and_the_one_time_prompt(self):
        lines = serve.startup_lines(
            "0.0.0.0", 8420, token_required=True, token_path="/x/config/token"
        )
        text = "\n".join(lines)
        self.assertIn("/x/config/token", text)
        self.assertIn("asks for it once", text)

    def test_no_token_line_on_loopback_without_a_token(self):
        self.assertEqual(len(serve.startup_lines("127.0.0.1", 8420)), 1)


class ConfigurableServer:
    """A `make_server` stand-in with a chosen bound address."""

    address = ("127.0.0.1", 4321)

    def serve_forever(self):
        pass

    def server_close(self):
        pass

    @property
    def server_address(self):
        return type(self).address


class ServeStartupTest(ServeCommandTest):
    def run_capture(self, argv, config=None, address=("127.0.0.1", 4321)):
        """The exit code, stdout, stderr, and `make_server` call of one run."""
        config_dir = tempfile.mkdtemp(prefix="korapy-serve-startup-config-")
        self.addCleanup(shutil.rmtree, config_dir)
        state_dir = tempfile.mkdtemp(prefix="korapy-serve-startup-state-")
        self.addCleanup(shutil.rmtree, state_dir)
        if config is not None:
            with open(os.path.join(config_dir, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write(config)
        ConfigurableServer.address = address
        env = {"KORAPY_CONFIG": config_dir, "KORAPY_STATE": state_dir}
        args = self.parse(*argv)
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(serve, "make_server", return_value=ConfigurableServer()) as made:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = args.func(args)
        return code, out.getvalue(), err.getvalue(), made

    def test_default_port_reaches_the_server(self):
        code, out, _, made = self.run_capture([], address=("127.0.0.1", 8420))
        self.assertEqual(code, 0)
        self.assertEqual(made.call_args.kwargs["port"], serve.SERVE_PORT_DEFAULT)
        self.assertIn("korapy serve: dashboard at http://127.0.0.1:8420/", out)

    def test_flag_beats_config(self):
        code, _, _, made = self.run_capture(["--port", "4322"], config="[serve]\nport = 4321\n")
        self.assertEqual(code, 0)
        self.assertEqual(made.call_args.kwargs["port"], 4322)

    def test_config_sets_the_port(self):
        code, _, _, made = self.run_capture([], config="[serve]\nport = 4321\n")
        self.assertEqual(code, 0)
        self.assertEqual(made.call_args.kwargs["port"], 4321)

    def test_explicit_zero_prints_the_bound_port(self):
        code, out, _, made = self.run_capture(["--port", "0"], address=("127.0.0.1", 54321))
        self.assertEqual(code, 0)
        self.assertEqual(made.call_args.kwargs["port"], 0)
        self.assertIn("korapy serve: dashboard at http://127.0.0.1:54321/", out)

    def test_held_port_suggests_another_or_a_free_one(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            held = holder.getsockname()[1]
            config_dir = tempfile.mkdtemp(prefix="korapy-serve-held-config-")
            self.addCleanup(shutil.rmtree, config_dir)
            state_dir = tempfile.mkdtemp(prefix="korapy-serve-held-state-")
            self.addCleanup(shutil.rmtree, state_dir)
            env = {"KORAPY_CONFIG": config_dir, "KORAPY_STATE": state_dir}
            args = self.parse("--port", str(held))
            with mock.patch.dict(os.environ, env, clear=False):
                err = io.StringIO()
                with redirect_stderr(err):
                    with redirect_stdout(io.StringIO()):
                        code = args.func(args)
        finally:
            holder.close()
        self.assertEqual(code, 2)
        self.assertIn("--port 0", err.getvalue())

    def test_remote_startup_names_the_token_file_never_its_content(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-startup-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-secret-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o600)
            config_dir = tempfile.mkdtemp(prefix="korapy-serve-token-config-")
            self.addCleanup(shutil.rmtree, config_dir)
            args = self.parse("--bind", "8.8.8.8", "--allow-remote")
            ConfigurableServer.address = ("8.8.8.8", 4321)
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=ConfigurableServer()):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(args.func(args), 0)
        token_path = os.path.join(tmp, "token")
        self.assertIn(token_path, out.getvalue())
        self.assertNotIn("tok-secret-1", out.getvalue())

    def test_wildcard_startup_guides_the_client(self):
        with tempfile.TemporaryDirectory(prefix="korapy-serve-wild-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o600)
            args = self.parse("--bind", "0.0.0.0", "--allow-remote")
            ConfigurableServer.address = ("0.0.0.0", 8420)
            with mock.patch.dict(
                os.environ,
                {"KORAPY_CONFIG": tmp, "KORAPY_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=ConfigurableServer()):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(args.func(args), 0)
        self.assertIn("0.0.0.0:8420", out.getvalue())
        self.assertIn("LAN address", out.getvalue())


if __name__ == "__main__":
    unittest.main()
