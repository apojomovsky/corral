"""`corral serve` S1: routes and the snapshot.

Offline: a real HTTP server on a loopback ephemeral port, spoken to
with `http.client`. Snapshot builders are fakes or `snapshot.build`
over canned views. No `gh`, no systemd, no network beyond loopback,
no real state dirs.
"""

import argparse
import ast
import copy
import http.client
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, paths, serve, settings, snapshot
from tests.test_snapshot import HOST, T0, config, entry, quota_view, usage, view

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
    return snapshot.build(
        T0, HOST, view((entry("a1"), "live")), quota_view(), config(), usage, DRIVE
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
            "systemd",
            "docker",
            "omp",
            "opencode",
            "claude",
            "requests",
        }
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module != "corral":
                    names.append(node.module)
                if node.module == "corral":
                    names.extend(f"corral.{alias.name}" for alias in node.names)
        bad = [name for name in names if set(name.split(".")) & forbidden]
        self.assertEqual(bad, [])

    def test_serve_builds_from_snapshot(self):
        with open(serve.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        from_corral = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "corral"
            for alias in node.names
        ]
        self.assertIn("snapshot", from_corral)


class CollectDivergence(unittest.TestCase):
    def test_collect_and_helper_cannot_diverge(self):
        from corral import snapshot as snap

        registry_view = view((entry("a1"), "live"))
        quota = quota_view()
        resolved = {
            "admission.total_cap": 4,
            "admission.repo_cap": 1,
            "quota.low_pct": 90,
            "drive.max_starts_per_day": 4,
        }
        with (
            mock.patch("corral.snapshot._settings_root", return_value=None),
            mock.patch("corral.settings.load", return_value=resolved),
            mock.patch("corral.registry.config", return_value={"count_toward_cap": True}),
            mock.patch("corral.registry.visible", return_value=registry_view),
            mock.patch("corral.accounts.providers", return_value=["prov-a"]),
            mock.patch("corral.quota_ledger.load", return_value={"ledger": True}),
            mock.patch("corral.quotacli.snapshot", return_value=quota),
            mock.patch("corral.drive.drive_status", return_value=dict(DRIVE)),
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
        self.assertEqual(settings.REGISTRY["serve.port"].default, 0)
        self.assertEqual(settings.REGISTRY["serve.origins"].default, [])
        self.assertEqual(settings.REGISTRY["serve.snapshot_ttl_s"].default, 2)
        self.assertEqual(settings.REGISTRY["serve.tail_max"].default, 200)
        self.assertEqual(settings.REGISTRY["serve.max_streams"].default, 20)

    def test_serve_registers_before_resolve(self):
        self.assertIn("corral.serve", settings.PROVIDERS)

    def test_host_and_env_layers_reach_serve(self):
        resolved = settings.resolve(host={"serve": {"port": 4321}}, env={})
        self.assertEqual(resolved["serve.port"], 4321)
        resolved = settings.resolve(env={"CORRAL_SERVE_PORT": "4322"})
        self.assertEqual(resolved["serve.port"], 4322)

    def test_no_setting_holds_a_token(self):
        self.assertEqual([key for key in settings.REGISTRY if "token" in key], [])


class ApiTokenPathTest(unittest.TestCase):
    def test_token_lives_beside_the_host_config(self):
        self.assertEqual(paths.api_token_path({"CORRAL_CONFIG": "/x/config"}), "/x/config/token")

    def test_token_defaults_under_xdg_config(self):
        self.assertEqual(
            paths.api_token_path({"XDG_CONFIG_HOME": "/x/config"}),
            "/x/config/corral/token",
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
        with tempfile.TemporaryDirectory(prefix="corral-serve-token-") as tmp:
            target = os.path.join(tmp, "sub", "token")
            serve.mint_token(target)
            self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o600")
            text = serve.read_token(target)
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", str(text)))

    def test_mint_refuses_when_present(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-token-") as tmp:
            target = os.path.join(tmp, "token")
            serve.mint_token(target)
            with self.assertRaises(serve.ServeError):
                serve.mint_token(target)

    def test_missing_token_reads_none(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-token-") as tmp:
            self.assertIsNone(serve.read_token(os.path.join(tmp, "token")))

    def test_unreadable_token_raises(self):
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaises(serve.ServeError):
                serve.read_token(os.path.join("token"))

    def test_world_readable_token_warns(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-token-") as tmp:
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


class ServeCommandTest(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(["serve", *argv])

    def run_func(self, argv, config=None, state=None):
        config_dir = tempfile.mkdtemp(prefix="corral-serve-config-")
        self.addCleanup(shutil.rmtree, config_dir)
        state_dir = tempfile.mkdtemp(prefix="corral-serve-state-")
        self.addCleanup(shutil.rmtree, state_dir)
        if config is not None:
            with open(os.path.join(config_dir, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write(config)
        env = {"CORRAL_CONFIG": config_dir, "CORRAL_STATE": state_dir}
        if state == "missing":
            missing = os.path.join(state_dir, "gone")
            env["CORRAL_STATE"] = missing
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

        with tempfile.TemporaryDirectory(prefix="corral-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o600)
            with mock.patch.dict(
                os.environ,
                {"CORRAL_CONFIG": tmp, "CORRAL_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=FakeServer()) as made:
                    out = io.StringIO()
                    args = self.parse("--bind", "8.8.8.8", "--allow-remote")
                    with redirect_stdout(out):
                        self.assertEqual(args.func(args), 0)
        made.assert_called_once()
        self.assertIn("listening on 8.8.8.8:4321", out.getvalue())

    def test_world_readable_token_warns_on_loopback_and_serves(self):
        class FakeServer:
            server_address = ("127.0.0.1", 4321)

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory(prefix="corral-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o644)
            with mock.patch.dict(
                os.environ,
                {"CORRAL_CONFIG": tmp, "CORRAL_STATE": tmp},
                clear=False,
            ):
                with mock.patch.object(serve, "make_server", return_value=FakeServer()):
                    out, err = io.StringIO(), io.StringIO()
                    args = self.parse()
                    with redirect_stdout(out), redirect_stderr(err):
                        self.assertEqual(args.func(args), 0)
            self.assertIn("readable beyond its owner", err.getvalue())
            self.assertIn("listening on 127.0.0.1:4321", out.getvalue())

    def test_world_readable_token_refuses_remote(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            os.chmod(os.path.join(tmp, "token"), 0o644)
            with mock.patch.dict(
                os.environ,
                {"CORRAL_CONFIG": tmp, "CORRAL_STATE": tmp},
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
        with tempfile.TemporaryDirectory(prefix="corral-serve-cmd-") as tmp:
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                holder.bind(("127.0.0.1", 0))
                holder.listen(1)
                port = holder.getsockname()[1]
                with mock.patch.dict(
                    os.environ,
                    {"CORRAL_CONFIG": tmp, "CORRAL_STATE": tmp},
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

        with tempfile.TemporaryDirectory(prefix="corral-serve-cmd-") as tmp:
            with mock.patch.dict(
                os.environ,
                {"CORRAL_CONFIG": tmp, "CORRAL_STATE": tmp},
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
        with tempfile.TemporaryDirectory(prefix="corral-serve-cmd-") as tmp:
            with open(os.path.join(tmp, "token"), "w", encoding="utf-8") as handle:
                handle.write("tok-1\n")
            with mock.patch.dict(
                os.environ,
                {"CORRAL_CONFIG": tmp, "CORRAL_STATE": tmp},
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
        registry_view = view((entry("a1"), "live"))
        quota = quota_view()
        resolved = {
            "admission.total_cap": 4,
            "admission.repo_cap": 1,
            "quota.low_pct": 90,
            "drive.max_starts_per_day": 4,
        }
        with (
            mock.patch("corral.snapshot._settings_root", return_value=None),
            mock.patch("corral.settings.load", return_value=resolved),
            mock.patch("corral.registry.config", return_value={"count_toward_cap": True}),
            mock.patch("corral.registry.visible", return_value=registry_view),
            mock.patch("corral.accounts.providers", return_value=["prov-a"]),
            mock.patch("corral.quota_ledger.load", return_value={"ledger": True}),
            mock.patch("corral.quotacli.snapshot", return_value=quota),
            mock.patch("corral.drive.drive_status", return_value=dict(DRIVE)),
            mock.patch("corral.snapshot.build", return_value={"ok": True}) as build,
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


if __name__ == "__main__":
    unittest.main()
