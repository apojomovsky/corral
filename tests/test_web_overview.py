"""W2: overview and polling core (host mode).

Contract tests: every snapshot field the overview reads, served at the
production seam with fakes, asserted present with the right types and
nullability. A failing test here means an API change that would
silently blank or garble a view. Offline: a real HTTP server on a
loopback ephemeral port, `snapshot.build` over canned views. No `gh`,
no network beyond loopback, no real state dirs.
"""

import copy
import json
import unittest

from korapy import snapshot
from tests.test_serve import DRIVE, ServeCase, healthy_doc
from tests.test_snapshot import (
    HOST,
    T0,
    config,
    doc_limits,
    doc_pressure,
    entry,
    quota_view,
    usage,
    view,
)

AGENT_KEYS = {
    "id",
    "project",
    "repo",
    "number",
    "runtime",
    "role",
    "profile",
    "model",
    "state",
    "started_at",
    "last_seen",
    "note",
}


def nullish_quota():
    """Providers covering every null shape the overview must tolerate."""
    found = quota_view()
    found["providers"]["provider-b"] = {
        "state": "unknown",
        "windows": None,
        "read_at": None,
        "stale": False,
        "mark": None,
        "reader": "",
        "error": "reader failed",
    }
    found["providers"]["provider-c"] = {
        "state": "ok",
        "windows": {"daily": None, "weekly": {"used_pct": None, "resets_at": T0 + 3600}},
        "read_at": T0,
        "stale": True,
        "mark": None,
        "reader": "",
        "error": None,
    }
    return found


def build_doc(quota=None, resources_view="healthy", registry="healthy"):
    """A snapshot document through `snapshot.build`, like the server builds it."""
    rows = view((entry("a1"), "live"))
    if registry == "broken":
        # `build` errors the agents, capacity and resources sections
        # itself here, so no resources section is collected.
        return snapshot.build(
            T0,
            HOST,
            ValueError("registry unreadable"),
            quota_view(),
            config(),
            usage,
            copy.deepcopy(DRIVE),
            resources_view=ValueError("registry unreadable"),
        )
    if resources_view == "healthy":
        resources = snapshot.resources_section(
            rows if not isinstance(rows, Exception) else rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            lambda *args: None,
            live_by_owner={"proj-a": ["a1"]},
            project_names={"proj-a"},
        )
    elif resources_view == "broken":
        resources = ValueError("limits unreadable")
    else:
        resources = resources_view
    return snapshot.build(
        T0,
        HOST,
        rows,
        nullish_quota() if quota == "nullish" else quota_view(),
        config(),
        usage,
        copy.deepcopy(DRIVE),
        resources_view=resources,
    )


class SnapshotContract(ServeCase):
    def snapshot_body(self, doc):
        server, _ = self.start_server(lambda: copy.deepcopy(doc))
        status, _, body = self.fetch(server, "GET", "/v1/snapshot")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_schema_version_matches_the_snapshot_module(self):
        self.assertEqual(
            self.snapshot_body(healthy_doc())["schema_version"], snapshot.SCHEMA_VERSION
        )

    def test_agent_rows_carry_the_overview_key_set(self):
        doc = self.snapshot_body(build_doc())
        self.assertIsInstance(doc["agents"], list)
        self.assertTrue(doc["agents"])
        for row in doc["agents"]:
            self.assertEqual(set(row), AGENT_KEYS, row.get("id"))
            for field in ("started_at", "last_seen"):
                self.assertTrue(row[field] is None or isinstance(row[field], (int, float)), field)
            for field in ("repo", "number"):
                self.assertTrue(row[field] is None or isinstance(row[field], (str, int)), field)

    def test_ticketless_agents_read_null_repo_and_number(self):
        rows = view((entry("a9", repo=None), "live"))
        resources = snapshot.resources_section(
            rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            lambda *args: None,
            live_by_owner={},
            project_names=set(),
        )
        doc = snapshot.build(
            T0,
            HOST,
            rows,
            quota_view(),
            config(),
            usage,
            copy.deepcopy(DRIVE),
            resources_view=resources,
        )
        served = self.snapshot_body(doc)
        row = [item for item in served["agents"] if item["id"] == "a9"][0]
        self.assertIsNone(row["repo"])
        self.assertIsNone(row["number"])

    def test_fleet_header_counts_live_stale_finished(self):
        rows = view(
            (entry("a1"), "live"),
            (entry("a2", state="stale", repo="repo-b", number=2, age=700), "stale"),
            (entry("a3", state="finished", repo=None), "finished"),
        )
        resources = snapshot.resources_section(
            rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            lambda *args: None,
            live_by_owner={"proj-a": ["a1"]},
            project_names={"proj-a"},
        )
        doc = snapshot.build(
            T0,
            HOST,
            rows,
            quota_view(),
            config(),
            usage,
            copy.deepcopy(DRIVE),
            resources_view=resources,
        )
        header = self.snapshot_body(doc)["fleet_header"]
        self.assertEqual(header["host"], HOST)
        self.assertIsInstance(header["now"], (int, float))
        self.assertEqual(header["agents_live"], 1)
        self.assertEqual(header["agents_stale"], 1)
        self.assertEqual(header["agents_finished"], 1)

    def test_quota_windows_may_be_null_at_every_level(self):
        providers = self.snapshot_body(build_doc(quota="nullish"))["quota"]["providers"]
        self.assertEqual(set(providers), {"provider-a", "provider-b", "provider-c"})
        for name, info in providers.items():
            self.assertIsInstance(info["state"], str, name)
            self.assertIsInstance(info["stale"], bool, name)
            self.assertTrue(
                info["read_at"] is None or isinstance(info["read_at"], (int, float)), name
            )
            self.assertTrue(info["error"] is None or isinstance(info["error"], str), name)
            windows = info["windows"]
            self.assertTrue(windows is None or isinstance(windows, dict), name)
            for window_id, window in (windows or {}).items():
                self.assertTrue(window is None or isinstance(window, dict), (name, window_id))
                if window is not None:
                    used = window["used_pct"]
                    self.assertTrue(
                        used is None or isinstance(used, (int, float)), (name, window_id)
                    )
                    resets = window["resets_at"]
                    self.assertTrue(
                        resets is None or isinstance(resets, (int, float)), (name, window_id)
                    )

    def test_capacity_shape(self):
        capacity = self.snapshot_body(build_doc())["capacity"]
        self.assertIsInstance(capacity["total_cap"], int)
        self.assertIsInstance(capacity["repo_cap"], int)
        self.assertIsInstance(capacity["live"], int)
        self.assertIsInstance(capacity["by_project"], dict)
        self.assertIsInstance(capacity["pending"], list)

    def test_disk_paths_shape(self):
        paths = self.snapshot_body(build_doc())["disk"]["paths"]
        self.assertIsInstance(paths, list)
        self.assertTrue(paths)
        for item in paths:
            self.assertIsInstance(item["path"], str)
            if "error" in item:
                self.assertIsInstance(item["error"], str)
            else:
                for field in ("total", "used", "free"):
                    self.assertIsInstance(item[field], (int, float), field)

    def test_drive_shape(self):
        drive = self.snapshot_body(build_doc())["drive"]
        self.assertIsInstance(drive["paused"], bool)
        self.assertTrue(drive["pause_reason"] is None or isinstance(drive["pause_reason"], str))
        for field in ("last_pass_at", "last_exit"):
            self.assertTrue(drive[field] is None or isinstance(drive[field], (int, float)), field)
        self.assertIsInstance(drive["starts_today"], int)
        self.assertIsInstance(drive["max_starts_per_day"], int)

    def test_resources_shape(self):
        resources = self.snapshot_body(build_doc())["resources"]
        self.assertIsInstance(resources["schema_version"], int)
        for field in ("host_budget", "charged"):
            triple = resources[field]
            self.assertTrue(triple is None or set(triple) == {"cpu_m", "memory_mb", "pids"}, field)
        self.assertIsInstance(resources["by_project"], dict)
        for name, triple in resources["by_project"].items():
            self.assertEqual(set(triple), {"cpu_m", "memory_mb", "pids"}, name)
        self.assertIsInstance(resources["measured_by_project"], dict)
        for name, measured in resources["measured_by_project"].items():
            self.assertTrue(measured is None or isinstance(measured, dict), name)
        pressure = resources["pressure"]
        self.assertIsInstance(pressure["saturated"], bool)
        for field in ("load_per_cpu", "mem_available_pct"):
            self.assertTrue(
                pressure[field] is None or isinstance(pressure[field], (int, float)), field
            )

    def test_failed_resources_is_a_bare_error_body(self):
        resources = self.snapshot_body(build_doc(resources_view="broken"))["resources"]
        self.assertEqual(set(resources), {"error"})
        self.assertIsInstance(resources["error"], str)
        self.assertTrue(resources["error"])
        self.assertTrue(snapshot.is_error_section(resources))

    def test_broken_registry_errors_agents_capacity_and_resources_only(self):
        doc = self.snapshot_body(build_doc(registry="broken"))
        for section in ("agents", "capacity", "resources"):
            self.assertTrue(snapshot.is_error_section(doc[section]), section)
            self.assertEqual(set(doc[section]), {"error"}, section)
        for section in ("quota", "disk", "drive"):
            self.assertFalse(snapshot.is_error_section(doc[section]), section)


if __name__ == "__main__":
    unittest.main()
