"""`corral serve` S1: routes and the snapshot.

Offline: a real HTTP server on a loopback ephemeral port, spoken to
with `http.client`. Snapshot builders are fakes or `snapshot.build`
over canned views. No `gh`, no systemd, no network beyond loopback,
no real state dirs.
"""

import ast
import copy
import http.client
import json
import shutil
import socket
import threading
import unittest
from unittest import mock

from corral import serve, snapshot
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

    def start_server(self, build):
        now = [T0]
        server = serve.make_server(build=build, clock=lambda: now[0], quiet=True)
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
            actual = serve.collect()
        self.assertEqual(actual, expected)


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
