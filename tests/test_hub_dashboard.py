"""The hub heartbeat and the aggregated dashboard. Fakes and loopback only.

Covers ticket H5 of docs/08-hub-multihost.md section 10: `POST
/v1/register`, `POST /v1/heartbeat` carrying lease renewals beside the
snapshot document, the stored snapshots table, the projects table that
filters `GET /v1/snapshot?project=`, per-host staleness under
`hub.stale_after_s`, clocks shown as data, and the host-side push
client. Every server test binds 127.0.0.1 on an ephemeral port and
speaks to it in-process; no test touches the network, a real state
dir, or another project.
"""

import ast
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from corral import hub, settings
from tests.test_hub import temp_db


def canned_doc(host, now, projects=()):
    """A minimal `snapshot.build`-shaped document for one host."""
    agents = [
        {
            "id": f"agent-{index}",
            "project": project,
            "repo": "app",
            "number": index,
            "runtime": "omp",
            "role": "worker",
            "model": None,
            "state": "live",
            "started_at": now - 10,
            "last_seen": now,
            "note": None,
        }
        for index, project in enumerate(projects, start=1)
    ]
    return {
        "schema_version": 1,
        "fleet_header": {"host": host, "now": now, "agents_live": len(agents)},
        "agents": agents,
        "quota": {"read_at": now, "stale": False},
        "capacity": {"total_cap": 4, "live": len(agents), "by_project": {}},
        "disk": {"paths": []},
        "drive": {"last_exit": 0},
    }


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        self.store = hub.open_db(self.db)
        self.addCleanup(self.store.close)
        self.token = hub.add_host(self.store, "host-1")

    def projects(self):
        return {
            row[0] for row in self.store.execute("SELECT DISTINCT name FROM projects").fetchall()
        }


class SchemaTest(StoreCase):
    def test_schema_has_snapshots_and_projects_tables(self):
        names = {
            row[0]
            for row in self.store.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertEqual(names, {"hosts", "claims", "meta", "snapshots", "projects"})

    def test_version_one_database_migrates_preserving_hosts_and_claims(self):
        self.store.close()
        fresh = sqlite3.connect(self.db)
        try:
            fresh.execute("DROP TABLE snapshots")
            fresh.execute("DROP TABLE projects")
            fresh.execute("PRAGMA user_version=1")
            fresh.commit()
        finally:
            fresh.close()
        migrated = hub.open_db(self.db)
        self.addCleanup(migrated.close)
        self.assertEqual(hub.verify_token(migrated, self.token), "host-1")
        names = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("snapshots", names)
        self.assertIn("projects", names)


class RegisterStoreTest(StoreCase):
    def test_register_reports_capacity_and_returns_the_epoch(self):
        epoch = hub.register_host(
            self.store,
            "host-1",
            now=1000.0,
            address="10.0.0.5:8000",
            capacity={"cpus": 8},
            constraints={"docker": True},
        )
        self.assertEqual(epoch, 0)
        row = self.store.execute(
            "SELECT address, capacity, constraints, last_seen FROM hosts WHERE host_id=?",
            ("host-1",),
        ).fetchone()
        self.assertEqual(row[0], "10.0.0.5:8000")
        self.assertEqual(json.loads(row[1]), {"cpus": 8})
        self.assertEqual(json.loads(row[2]), {"docker": True})
        self.assertEqual(row[3], 1000.0)

    def test_unknown_host_is_refused(self):
        with self.assertRaises(hub.HubError):
            hub.register_host(self.store, "host-9", now=1000.0)

    def test_capacity_must_be_an_object(self):
        with self.assertRaises(hub.HubError):
            hub.register_host(
                self.store,
                "host-1",
                now=1000.0,
                capacity=["cpus"],  # ty: ignore[invalid-argument-type]
            )


class SnapshotStoreTest(StoreCase):
    def test_store_keeps_the_document_semantically_identical(self):
        doc = canned_doc("h1", 1000.0, ["alpha"])
        hub.register_host(self.store, "host-1", now=1000.0)
        hub.store_snapshot(self.store, "host-1", doc, now=1005.0)
        row = self.store.execute(
            "SELECT document, host_now, received_at FROM snapshots WHERE host_id=?",
            ("host-1",),
        ).fetchone()
        self.assertEqual(json.loads(row[0]), doc)
        self.assertEqual(row[0], json.dumps(doc, sort_keys=True))
        self.assertEqual(row[1], 1000.0)
        self.assertEqual(row[2], 1005.0)

    def test_repush_replaces_the_row_and_refreshes_projects(self):
        hub.register_host(self.store, "host-1", now=1000.0)
        hub.store_snapshot(self.store, "host-1", canned_doc("h1", 1000.0, ["alpha"]), now=1001.0)
        hub.store_snapshot(self.store, "host-1", canned_doc("h1", 1010.0, ["beta"]), now=1011.0)
        count = self.store.execute(
            "SELECT COUNT(*) FROM snapshots WHERE host_id=?", ("host-1",)
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.projects(), {"beta"})

    def test_project_union_covers_agents_and_capacity(self):
        doc = canned_doc("h1", 1000.0, ["alpha"])
        doc["capacity"]["by_project"] = {"gamma": 1}
        hub.register_host(self.store, "host-1", now=1000.0)
        hub.store_snapshot(self.store, "host-1", doc, now=1001.0)
        self.assertEqual(self.projects(), {"alpha", "gamma"})

    def test_document_must_be_a_dict(self):
        hub.register_host(self.store, "host-1", now=1000.0)
        with self.assertRaises(hub.HubError):
            hub.store_snapshot(
                self.store,
                "host-1",
                ["not", "a", "dict"],  # ty: ignore[invalid-argument-type]
                now=1001.0,
            )

    def test_snapshot_for_an_unknown_host_is_refused(self):
        with self.assertRaises(hub.HubError):
            hub.store_snapshot(self.store, "host-9", canned_doc("h9", 1.0), now=2.0)


class DashboardCase(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        store = hub.open_db(self.db)
        self.token_a = hub.add_host(store, "host-1")
        self.token_b = hub.add_host(store, "host-2")
        store.close()
        self.clock = [1000.0]
        self.server = hub.make_server("127.0.0.1", 0, self.db, clock=lambda: self.clock[0])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.port = self.server.server_address[1]

    def shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()

    def call(self, method, path, body=None, token="unset"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if token != "unset":
            headers["Authorization"] = f"Bearer {token}" if token else ""
        if body is not None:
            headers["Content-Type"] = "application/json"
            conn.request(method, path, body=json.dumps(body), headers=headers)
        else:
            conn.request(method, path, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            payload = None
        return response.status, payload

    def heartbeat(
        self,
        host="host-1",
        token=None,
        renewals=(),
        snapshot=None,
        capacity=None,
        constraints=None,
    ):
        body = {"host_id": host, "renewals": list(renewals)}
        if snapshot is not None:
            body["snapshot"] = snapshot
        if capacity is not None:
            body["capacity"] = capacity
        if constraints is not None:
            body["constraints"] = constraints
        return self.call(
            "POST",
            "/v1/heartbeat",
            body,
            token=self.token_a if token is None else token,
        )


class RegisterRouteTest(DashboardCase):
    def test_register_updates_the_host_and_returns_the_epoch(self):
        status, payload = self.call(
            "POST",
            "/v1/register",
            {
                "host_id": "host-1",
                "address": "10.0.0.5:8000",
                "capacity": {"cpus": 8},
                "constraints": {"docker": True},
            },
            token=self.token_a,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"host_id": "host-1", "membership_epoch": 0})
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(by_id["host-1"]["address"], "10.0.0.5:8000")
        self.assertEqual(by_id["host-1"]["capacity"], {"cpus": 8})
        self.assertEqual(by_id["host-1"]["constraints"], {"docker": True})
        self.assertTrue(by_id["host-1"]["reachable"])

    def test_register_needs_its_own_token(self):
        status, _ = self.call("POST", "/v1/register", {"host_id": "host-1"}, token=self.token_b)
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/v1/register", {"host_id": "host-1"}, token=None)
        self.assertEqual(status, 401)

    def test_register_rejects_a_bad_body(self):
        status, payload = self.call(
            "POST", "/v1/register", {"host_id": "host-1", "capacity": [1]}, token=self.token_a
        )
        self.assertEqual(status, 400)
        self.assertTrue(payload["error"])


class HeartbeatRouteTest(DashboardCase):
    def grant(self, agent="drive-aaa", host="host-1", token=None):
        return self.call(
            "POST",
            "/v1/claims",
            {"repo": "app", "number": 7, "agent": agent, "host_id": host},
            token=self.token_a if token is None else token,
        )

    def test_heartbeat_renews_and_stores_the_snapshot(self):
        status, granted = self.grant()
        self.assertEqual(status, 200)
        entry = hub.renewal_entry("app", 7, "drive-aaa", granted["epoch"])
        doc = canned_doc("h1", 1000.0, ["alpha"])
        self.clock[0] = 1030.0
        status, payload = self.heartbeat(renewals=[entry], snapshot=doc)
        self.assertEqual(status, 200)
        self.assertEqual(payload["membership_epoch"], 0)
        (answer,) = payload["renewals"]
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["receipt"]["epoch"], 1)
        self.assertEqual(answer["receipt"]["lease_expires"], 1120.0)
        store = hub.open_db(self.db)
        try:
            row = store.execute(
                "SELECT host_now, received_at FROM snapshots WHERE host_id=?", ("host-1",)
            ).fetchone()
        finally:
            store.close()
        self.assertEqual(tuple(row), (1000.0, 1030.0))

    def test_heartbeat_reports_a_lost_race_per_entry(self):
        _, granted = self.grant()
        self.clock[0] = 1001.0
        good = hub.renewal_entry("app", 7, "drive-aaa", granted["epoch"])
        bad = {"repo": "app", "number": 7, "agent": "drive-zzz", "host_id": "host-1", "epoch": 9}
        status, payload = self.heartbeat(renewals=[good, bad])
        self.assertEqual(status, 200)
        self.assertTrue(payload["renewals"][0]["ok"])
        loser = payload["renewals"][1]
        self.assertFalse(loser["ok"])
        self.assertEqual(loser["status"], 409)
        self.assertEqual(loser["holder"]["agent"], "drive-aaa")

    def test_heartbeat_renewal_is_bound_to_the_calling_host(self):
        _, foreign = self.call(
            "POST",
            "/v1/claims",
            {"repo": "app", "number": 7, "agent": "drive-bbb", "host_id": "host-2"},
            token=self.token_b,
        )
        _, own = self.call(
            "POST",
            "/v1/claims",
            {"repo": "app", "number": 8, "agent": "drive-aaa", "host_id": "host-1"},
            token=self.token_a,
        )
        before = foreign["lease_expires"]
        self.clock[0] = before - 10.0
        foreign_entry = hub.renewal_entry("app", 7, "drive-bbb", foreign["epoch"])
        own_entry = hub.renewal_entry("app", 8, "drive-aaa", own["epoch"])
        status, payload = self.heartbeat(renewals=[foreign_entry, own_entry])
        self.assertEqual(status, 200)
        refused, accepted = payload["renewals"]
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], 409)
        self.assertNotIn("holder", refused)
        self.assertNotIn("host-2", refused.get("error", ""))
        self.assertNotIn("drive-bbb", refused.get("error", ""))
        self.assertTrue(accepted["ok"])
        store = hub.open_db(self.db)
        try:
            row = store.execute(
                "SELECT agent, lease_expires FROM claims WHERE repo=? AND number=?",
                ("app", 7),
            ).fetchone()
        finally:
            store.close()
        self.assertEqual(tuple(row), ("drive-bbb", before))

    def test_heartbeat_without_a_snapshot_still_checks_in(self):
        status, payload = self.heartbeat()
        self.assertEqual(status, 200)
        self.assertEqual(payload["renewals"], [])
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(by_id["host-1"]["last_seen"], 1000.0)

    def test_heartbeat_carries_capacity_and_constraints(self):
        status, payload = self.heartbeat(capacity={"cpus": 2}, constraints={"docker": False})
        self.assertEqual(status, 200)
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(by_id["host-1"]["capacity"], {"cpus": 2})
        self.assertEqual(by_id["host-1"]["constraints"], {"docker": False})

    def test_renewals_only_heartbeat_keeps_the_stored_host_fields(self):
        status, _ = self.call(
            "POST",
            "/v1/register",
            {
                "host_id": "host-1",
                "address": "10.0.0.5:8000",
                "capacity": {"cpus": 8},
                "constraints": {"docker": True},
            },
            token=self.token_a,
        )
        self.assertEqual(status, 200)
        self.clock[0] = 1030.0
        status, payload = self.heartbeat()
        self.assertEqual(status, 200)
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(by_id["host-1"]["address"], "10.0.0.5:8000")
        self.assertEqual(by_id["host-1"]["capacity"], {"cpus": 8})
        self.assertEqual(by_id["host-1"]["constraints"], {"docker": True})
        self.assertEqual(by_id["host-1"]["last_seen"], 1030.0)

    def test_heartbeat_needs_its_own_token(self):
        status, _ = self.heartbeat(host="host-2", token=self.token_a)
        self.assertEqual(status, 401)

    def test_heartbeat_rejects_a_snapshot_that_is_not_a_document(self):
        status, payload = self.heartbeat(snapshot=["nope"])
        self.assertEqual(status, 400)
        self.assertTrue(payload["error"])

    def test_post_routes_ignore_the_query_string(self):
        status, payload = self.call(
            "POST", "/v1/heartbeat?x=1", {"host_id": "host-1", "renewals": []}, token=self.token_a
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["renewals"], [])

    def test_empty_body_error_names_no_operation(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            "/v1/heartbeat",
            body="",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token_a}",
            },
        )
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(response.status, 400)
        self.assertIn("JSON", payload["error"])
        for word in ("claim", "renew", "heartbeat", "register", "release", "repo"):
            self.assertNotIn(word, payload["error"])


class HostsRouteTest(DashboardCase):
    def test_hosts_lists_capacity_and_reachability(self):
        self.call(
            "POST",
            "/v1/register",
            {"host_id": "host-1", "capacity": {"cpus": 8}},
            token=self.token_a,
        )
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        self.assertEqual(status, 200)
        self.assertEqual(payload["membership_epoch"], 0)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(set(by_id), {"host-1", "host-2"})
        self.assertEqual(by_id["host-1"]["capacity"], {"cpus": 8})
        self.assertTrue(by_id["host-1"]["reachable"])
        self.assertFalse(by_id["host-2"]["reachable"])

    def test_a_silent_host_stays_listed_but_unreachable(self):
        self.heartbeat()
        self.clock[0] += hub.DEFAULT_STALE_AFTER_S + 1
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(status, 200)
        self.assertIn("host-1", by_id)
        self.assertFalse(by_id["host-1"]["reachable"])

    def test_hosts_needs_a_token(self):
        status, _ = self.call("GET", "/v1/hosts", token=None)
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/v1/hosts", token="wrong")
        self.assertEqual(status, 401)


class MergeRouteTest(DashboardCase):
    def push(self, host, token, now, projects):
        self.clock[0] = now
        return self.heartbeat(host=host, token=token, snapshot=canned_doc(host, now, projects))

    def test_merge_serves_every_host_document_untouched(self):
        self.push("host-1", self.token_a, 1000.0, ["alpha"])
        self.push("host-2", self.token_b, 1005.0, ["beta"])
        self.clock[0] = 1010.0
        status, payload = self.call("GET", "/v1/snapshot", token=self.token_a)
        self.assertEqual(status, 200)
        self.assertEqual(payload["project"], "all")
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(set(by_id), {"host-1", "host-2"})
        self.assertEqual(by_id["host-1"]["snapshot"], canned_doc("host-1", 1000.0, ["alpha"]))
        self.assertEqual(by_id["host-2"]["snapshot"], canned_doc("host-2", 1005.0, ["beta"]))
        self.assertEqual(by_id["host-1"]["received_at"], 1000.0)
        self.assertFalse(by_id["host-1"]["stale"])
        self.assertFalse(by_id["host-2"]["stale"])

    def test_project_slice_filters_agents_and_keeps_host_sections(self):
        self.push("host-1", self.token_a, 1000.0, ["alpha", "beta"])
        self.push("host-2", self.token_b, 1000.0, ["beta"])
        status, payload = self.call("GET", "/v1/snapshot?project=beta", token=self.token_a)
        self.assertEqual(status, 200)
        self.assertEqual(payload["project"], "beta")
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(
            [row["project"] for row in by_id["host-1"]["snapshot"]["agents"]], ["beta"]
        )
        self.assertEqual(
            [row["project"] for row in by_id["host-2"]["snapshot"]["agents"]], ["beta"]
        )
        full = canned_doc("host-1", 1000.0, ["alpha", "beta"])
        self.assertEqual(by_id["host-1"]["snapshot"]["fleet_header"], full["fleet_header"])
        self.assertEqual(by_id["host-1"]["snapshot"]["quota"], full["quota"])

    def test_idle_host_keeps_an_empty_agents_list_in_a_slice(self):
        self.push("host-1", self.token_a, 1000.0, ["alpha"])
        self.push("host-2", self.token_b, 1000.0, ["beta"])
        status, payload = self.call("GET", "/v1/snapshot?project=beta", token=self.token_a)
        self.assertEqual(status, 200)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(set(by_id), {"host-1", "host-2"})
        self.assertEqual(by_id["host-1"]["snapshot"]["agents"], [])

    def test_unknown_project_is_404_not_an_empty_merge(self):
        self.push("host-1", self.token_a, 1000.0, ["alpha"])
        status, payload = self.call("GET", "/v1/snapshot?project=typo", token=self.token_a)
        self.assertEqual(status, 404)
        self.assertIn("typo", payload["error"])

    def test_stale_hosts_keep_last_rows_with_a_growing_age(self):
        self.push("host-1", self.token_a, 1000.0, ["alpha"])
        self.clock[0] = 1000.0 + hub.DEFAULT_STALE_AFTER_S + 5
        status, payload = self.call("GET", "/v1/snapshot", token=self.token_a)
        self.assertEqual(status, 200)
        (row,) = payload["hosts"]
        self.assertEqual(row["host_id"], "host-1")
        self.assertTrue(row["stale"])
        self.assertEqual(row["received_at"], 1000.0)
        self.assertEqual(row["snapshot"], canned_doc("host-1", 1000.0, ["alpha"]))

    def test_clock_skew_is_shown_never_compared(self):
        first = canned_doc("host-1", 1000.0, ["alpha"])
        second = canned_doc("host-2", 9000.0, ["alpha"])
        self.push("host-1", self.token_a, 1000.0, ["alpha"])
        self.push("host-2", self.token_b, 1000.0, ["alpha"])
        store = hub.open_db(self.db)
        try:
            hub.store_snapshot(store, "host-1", first, now=1000.0)
            hub.store_snapshot(store, "host-2", second, now=1000.0)
        finally:
            store.close()
        status, payload = self.call("GET", "/v1/snapshot", token=self.token_a)
        self.assertEqual(status, 200)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(by_id["host-1"]["snapshot"], first)
        self.assertEqual(by_id["host-2"]["snapshot"], second)
        self.assertEqual(by_id["host-2"]["snapshot"]["fleet_header"]["now"], 9000.0)

    def test_snapshot_needs_a_token(self):
        status, _ = self.call("GET", "/v1/snapshot", token=None)
        self.assertEqual(status, 401)


class PushClientTest(DashboardCase):
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def test_push_registers_and_heartbeats_through_the_client(self):
        self.assertEqual(
            hub.push_register(self.url(), self.token_a, "host-1", capacity={"cpus": 4}),
            {"host_id": "host-1", "membership_epoch": 0},
        )
        doc = canned_doc("host-1", 1000.0, ["alpha"])
        reply = hub.push_heartbeat(self.url(), self.token_a, "host-1", renewals=[], snapshot=doc)
        self.assertEqual(reply["membership_epoch"], 0)
        self.assertEqual(reply["renewals"], [])
        status, payload = self.call("GET", "/v1/snapshot", token=self.token_a)
        self.assertEqual(status, 200)
        self.assertEqual(payload["hosts"][0]["snapshot"], doc)

    def test_push_to_a_dark_port_is_unreachable(self):
        with tempfile.TemporaryDirectory(prefix="corral-hub-dark-") as state:
            dark = os.path.join(state, "hub.sqlite")
        closed = hub.make_server("127.0.0.1", 0, dark)
        port = closed.server_address[1]
        closed.server_close()
        with self.assertRaises(hub.HubUnreachable):
            hub.push_heartbeat(f"http://127.0.0.1:{port}", self.token_a, "host-1")

    def test_push_with_a_wrong_token_is_refused(self):
        with self.assertRaises(hub.HubError):
            hub.push_heartbeat(self.url(), "wrong", "host-1")

    def test_heartbeat_client_forwards_address_and_omits_absent_fields(self):
        hub.push_register(
            self.url(),
            self.token_a,
            "host-1",
            address="10.0.0.5:8000",
            capacity={"cpus": 8},
            constraints={"docker": True},
        )
        hub.push_heartbeat(self.url(), self.token_a, "host-1", renewals=[])
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(by_id["host-1"]["address"], "10.0.0.5:8000")
        self.assertEqual(by_id["host-1"]["capacity"], {"cpus": 8})
        self.assertEqual(by_id["host-1"]["constraints"], {"docker": True})
        hub.push_heartbeat(self.url(), self.token_a, "host-1", renewals=[], address="10.0.0.9:8000")
        status, payload = self.call("GET", "/v1/hosts", token=self.token_a)
        by_id = {row["host_id"]: row for row in payload["hosts"]}
        self.assertEqual(by_id["host-1"]["address"], "10.0.0.9:8000")

    def test_malformed_reply_is_unreachable(self):
        class Broken:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                raise http.client.BadStatusLine("bad line")

            def close(self):
                pass

        with mock.patch("http.client.HTTPConnection", Broken):
            with self.assertRaises(hub.HubUnreachable) as caught:
                hub.push_heartbeat(self.url(), self.token_a, "host-1")
        self.assertEqual(caught.exception.exit_code, 3)


class SplitUrlTest(unittest.TestCase):
    def test_non_http_scheme_is_refused(self):
        with self.assertRaises(hub.HubError) as caught:
            hub._split_url("https://host-1:8000")
        self.assertEqual(caught.exception.exit_code, 4)

    def test_missing_port_is_refused(self):
        with self.assertRaises(hub.HubError) as caught:
            hub._split_url("http://host-1")
        self.assertEqual(caught.exception.exit_code, 4)

    def test_non_numeric_port_is_refused(self):
        with self.assertRaises(hub.HubError) as caught:
            hub._split_url("http://host-1:abc")
        self.assertEqual(caught.exception.exit_code, 4)

    def test_plain_http_url_splits(self):
        self.assertEqual(hub._split_url("http://host-1:8000"), ("host-1", 8000))


class SliceProjectTest(unittest.TestCase):
    def test_agents_that_are_not_a_list_pass_through(self):
        doc = {"agents": "nope", "quota": {}}
        self.assertEqual(hub._slice_project(doc, "alpha"), doc)
        self.assertEqual(hub._slice_project({"quota": {}}, "alpha"), {"quota": {}})


class HostReadPathClosure(unittest.TestCase):
    def test_serve_snapshot_and_drive_import_no_hub(self):
        from corral import drive, serve, snapshot

        bad = []
        for module in (serve, snapshot, drive):
            with open(module.__file__, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                    if any(
                        name == "corral.hub" or name.startswith("corral.hub.") for name in names
                    ):
                        bad.append(f"{module.__name__}: import {names}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "corral.hub" or (node.module or "").startswith("corral.hub."):
                        bad.append(f"{module.__name__}: from {node.module} import ...")
                    elif node.module == "corral" and any(
                        alias.name == "hub" for alias in node.names
                    ):
                        bad.append(f"{module.__name__}: from corral import hub")
        self.assertEqual(bad, [])


class HubStaleSettingTest(unittest.TestCase):
    def test_stale_default_is_three_heartbeats(self):
        resolved = settings.resolve(project={}, env={})
        self.assertEqual(resolved["hub.stale_after_s"], 90)
        self.assertEqual(resolved["hub.stale_after_s"], 3 * resolved["hub.heartbeat_s"])

    def test_env_overrides_the_stale_horizon(self):
        resolved = settings.resolve(project={}, env={"CORRAL_HUB_STALE_AFTER_S": "30"})
        self.assertEqual(resolved["hub.stale_after_s"], 30)


if __name__ == "__main__":
    unittest.main()
