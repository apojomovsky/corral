"""The hub skeleton: hosts, the claims table, atomic grants. Fakes and loopback only.

Covers ticket H1 of docs/08-hub-multihost.md section 10: the SQLite
store behind one transaction, the Bearer token check, the loopback
HTTP service with grant and conflict paths, `corral hub add-host`,
the `hub.*` settings, and the non-private bind warning. Every server
test binds 127.0.0.1 on an ephemeral port and speaks to it in-process;
no test touches the network, a real state dir, or another project.
"""

import http.client
import json
import logging
import os
import re
import socket
import sqlite3
import tempfile
import threading
import unittest

from corral import hub, paths, settings


def temp_db():
    tmp = tempfile.TemporaryDirectory(prefix="corral-hub-")
    return tmp, os.path.join(tmp.name, "hub.sqlite")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        self.store = hub.open_db(self.db)
        self.addCleanup(self.store.close)

    def add_host(self, host_id="host-1"):
        token = hub.add_host(self.store, host_id)
        return token


class OpenDbTest(StoreCase):
    def test_creates_wal_database_owner_only(self):
        mode = oct(os.stat(self.db).st_mode & 0o777)
        self.assertEqual(mode, "0o600")
        journal = self.store.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(journal.lower(), "wal")

    def test_schema_has_hosts_claims_meta_snapshots_and_projects(self):
        names = {
            row[0]
            for row in self.store.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertEqual(names, {"hosts", "claims", "meta", "snapshots", "projects"})
        epoch = self.store.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
        self.assertEqual(tuple(epoch), (0,))

    def test_reopen_keeps_hosts_and_epochs(self):
        token = self.add_host()
        receipt = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        self.store.close()
        reopened = hub.open_db(self.db)
        self.addCleanup(reopened.close)
        self.assertEqual(hub.verify_token(reopened, token), "host-1")
        row = reopened.execute(
            "SELECT agent, epoch FROM claims WHERE repo=? AND number=?", ("app", 7)
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("drive-aaa", receipt.epoch))

    def test_newer_schema_fails_closed(self):
        self.store.execute("PRAGMA user_version=9999")
        self.store.close()
        with self.assertRaises(hub.HubError):
            hub.open_db(self.db)

    def test_preexisting_file_from_a_lost_create_race_opens(self):
        self.store.close()
        with open(self.db, "wb") as handle:
            handle.write(b"")
        reopened = hub.open_db(self.db)
        self.addCleanup(reopened.close)
        epoch = reopened.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
        self.assertEqual(tuple(epoch), (0,))

    def test_unwritable_state_parent_is_hub_error(self):
        from unittest import mock

        self.store.close()
        with mock.patch.object(os, "makedirs", side_effect=OSError("denied")):
            with self.assertRaises(hub.HubError):
                hub.open_db(os.path.join(self.tmp.name, "nested", "hub.sqlite"))

    def test_chmod_failure_is_a_hub_error(self):
        from unittest import mock

        self.store.close()
        with mock.patch.object(os, "chmod", side_effect=OSError("denied")):
            with self.assertRaises(hub.HubError):
                hub.open_db(os.path.join(self.tmp.name, "chmod-fail.sqlite"))

    def test_connect_failure_is_a_hub_error(self):
        from unittest import mock

        self.store.close()
        with mock.patch("sqlite3.connect", side_effect=sqlite3.Error("locked")):
            with self.assertRaises(hub.HubError):
                hub.open_db(os.path.join(self.tmp.name, "connect-fail.sqlite"))

    def test_wal_unavailable_is_a_hub_error(self):
        from unittest import mock

        self.store.close()
        real_connect = sqlite3.connect

        def fake_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            fake = mock.Mock(wraps=conn)
            real_execute = conn.execute

            def fake_execute(sql, *exec_args, **exec_kwargs):
                if isinstance(sql, str) and "journal_mode" in sql:
                    cursor = mock.Mock()
                    cursor.fetchone.return_value = ("delete",)
                    return cursor
                return real_execute(sql, *exec_args, **exec_kwargs)

            fake.execute = fake_execute
            return fake

        with mock.patch("sqlite3.connect", side_effect=fake_connect):
            with self.assertRaises(hub.HubError) as caught:
                hub.open_db(os.path.join(self.tmp.name, "wal-fail.sqlite"))
        self.assertIn("WAL", str(caught.exception))


class AddHostTest(StoreCase):
    def test_mint_stores_a_salted_hash_never_the_token(self):
        token = self.add_host()
        self.assertTrue(token)
        row = self.store.execute(
            "SELECT token_salt, token_hash FROM hosts WHERE host_id=?", ("host-1",)
        ).fetchone()
        self.assertTrue(row[0])
        self.assertTrue(row[1])
        dump = "\n".join(
            str(value)
            for (table,) in self.store.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            for value in self.store.execute(f"SELECT * FROM {table}").fetchall()
        )
        self.assertNotIn(token, dump)
        self.assertEqual(hub.verify_token(self.store, token), "host-1")

    def test_two_hosts_get_different_tokens(self):
        first = self.add_host("host-1")
        second = self.add_host("host-2")
        self.assertNotEqual(first, second)
        self.assertEqual(hub.verify_token(self.store, second), "host-2")

    def test_duplicate_id_is_refused(self):
        self.add_host()
        with self.assertRaises(hub.HubError):
            hub.add_host(self.store, "host-1")

    def test_bad_ids_are_refused(self):
        for bad in ("", "host 1", "host/1", "-host", "host!1", "../x"):
            with self.subTest(bad=bad), self.assertRaises(hub.HubError):
                hub.add_host(self.store, bad)

    def test_wrong_or_missing_token_verifies_to_nobody(self):
        token = self.add_host()
        self.assertIsNone(hub.verify_token(self.store, token + "x"))
        self.assertIsNone(hub.verify_token(self.store, ""))
        self.assertIsNone(hub.verify_token(self.store, None))


class GrantTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.add_host()

    def test_first_grant_mints_epoch_one_with_a_lease(self):
        receipt = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 1)
        self.assertEqual(receipt.lease_expires, 1090.0)

    def test_second_claimant_gets_409_naming_the_holder(self):
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        with self.assertRaises(hub.Conflict) as caught:
            hub.grant(self.store, "app", 7, "drive-bbb", "host-2", now=1001.0, lease_ttl_s=90)
        self.assertIn("drive-aaa", str(caught.exception))
        self.assertIn("host-1", str(caught.exception))
        self.assertEqual(caught.exception.holder["agent"], "drive-aaa")

    def test_same_agent_reproposing_is_idempotent(self):
        first = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        second = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1001.0, lease_ttl_s=90)
        self.assertEqual((second.epoch, second.lease_expires), (first.epoch, first.lease_expires))

    def test_lapsed_lease_regrants_with_a_bumped_epoch(self):
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        receipt = hub.grant(self.store, "app", 7, "drive-bbb", "host-2", now=1091.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 2)

    def test_lapsed_lease_regrant_bumps_even_for_the_same_agent(self):
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        receipt = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1091.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 2)

    def test_propose_at_exact_expiry_regrants_with_a_bumped_epoch(self):
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        with self.assertRaises(hub.Conflict):
            hub.grant(self.store, "app", 7, "drive-bbb", "host-2", now=1089.0, lease_ttl_s=90)
        receipt = hub.grant(self.store, "app", 7, "drive-bbb", "host-2", now=1090.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 2)

    def test_bad_shapes_are_refused(self):
        with self.assertRaises(hub.HubError):
            hub.grant(self.store, "", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        with self.assertRaises(hub.HubError):
            hub.grant(self.store, "app", 0, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        with self.assertRaises(hub.HubError):
            hub.grant(self.store, "app", 7, "", "host-1", now=1000.0, lease_ttl_s=90)


class ServerCase(unittest.TestCase):
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
            raw = json.dumps(body)
            headers["Content-Type"] = "application/json"
            conn.request(method, path, body=raw, headers=headers)
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

    def propose(self, agent="drive-aaa", host="host-1", token=None, **over):
        body = {"repo": "app", "number": 7, "agent": agent, "host_id": host}
        body.update(over)
        return self.call("POST", "/v1/claims", body, token=self.token_a if token is None else token)

    def raw_call(self, raw, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Authorization": f"Bearer {self.token_a if token is None else token}"}
        conn.request("POST", "/v1/claims", body=raw, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        try:
            body = json.loads(payload.decode("utf-8")) if payload else None
        except ValueError:
            body = None
        return response.status, body


class ClaimRouteTest(ServerCase):
    def test_grant_returns_the_receipt_with_epoch_one(self):
        status, payload = self.propose()
        self.assertEqual(status, 200)
        self.assertEqual(
            (payload["agent"], payload["host_id"], payload["epoch"]), ("drive-aaa", "host-1", 1)
        )
        self.assertEqual(payload["lease_expires"], 1090.0)

    def test_conflict_names_the_holder(self):
        self.propose()
        status, payload = self.propose(agent="drive-bbb", host="host-2", token=self.token_b)
        self.assertEqual(status, 409)
        self.assertIn("drive-aaa", payload["error"])
        self.assertEqual(payload["holder"]["agent"], "drive-aaa")
        self.assertEqual(payload["holder"]["host_id"], "host-1")

    def test_racing_proposes_grant_exactly_one_epoch_one(self):
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def race(index):
            barrier.wait()
            status, payload = self.call(
                "POST",
                "/v1/claims",
                {"repo": "app", "number": 7, "agent": f"drive-{index}", "host_id": "host-1"},
                token=self.token_a,
            )
            with lock:
                results.append((status, payload))

        threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 8)
        winners = [payload for status, payload in results if status == 200]
        losers = [(status, payload) for status, payload in results if status != 200]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["epoch"], 1)
        self.assertEqual(len(losers), 7)
        for status, payload in losers:
            self.assertEqual(status, 409)
            self.assertIn(winners[0]["agent"], payload["error"])

    def test_missing_or_wrong_token_is_401_without_a_leak(self):
        status, payload = self.call("POST", "/v1/claims", {"repo": "app"}, token=None)
        self.assertEqual(status, 401)
        status, payload = self.call("POST", "/v1/claims", {"repo": "app"}, token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": payload["error"]})
        with self.assertLogs("corral.hub", level="WARNING") as logs:
            self.call("POST", "/v1/claims", {"repo": "app"}, token="s3cret-token")
        self.assertTrue(any("401" in line or "rejected" in line for line in logs.output))
        self.assertFalse(any("s3cret-token" in line for line in logs.output))

    def test_token_for_another_host_is_refused(self):
        status, _ = self.propose(host="host-2", token=self.token_a)
        self.assertEqual(status, 401)

    def test_unknown_host_is_refused(self):
        status, _ = self.call(
            "POST",
            "/v1/claims",
            {"repo": "app", "number": 7, "agent": "drive-aaa", "host_id": "host-9"},
            token=self.token_a,
        )
        self.assertEqual(status, 401)

    def test_bad_body_is_400(self):
        status, payload = self.call(
            "POST",
            "/v1/claims",
            {"repo": "app", "number": 0, "agent": "drive-aaa", "host_id": "host-1"},
            token=self.token_a,
        )
        self.assertEqual(status, 400)
        self.assertTrue(payload["error"])

    def test_malformed_bodies_are_400(self):
        cases = {
            "empty": b"",
            "non-json": b"this is not json",
            "oversize": b"x" * (hub._MAX_BODY + 1),
            "non-dict": b"[1, 2]",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                status, payload = self.raw_call(raw)
                self.assertEqual(status, 400)
                self.assertTrue(payload["error"])

    def test_unknown_path_is_404_and_wrong_method_is_405(self):
        status, _ = self.call("GET", "/v1/nope", token=self.token_a)
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/v1/claims/renew", token=self.token_a)
        self.assertEqual(status, 405)

    def test_version_is_open_and_names_both_versions(self):
        status, payload = self.call("GET", "/v1/version")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"api": hub.HUB_API, "schema_version": hub.SCHEMA_VERSION})


class BindWarningTest(unittest.TestCase):
    def test_loopback_and_private_binds_stay_quiet(self):
        for bind in ("127.0.0.1", "127.0.0.2", "::1", "localhost", "10.0.0.5", "192.168.1.9"):
            with self.subTest(bind=bind):
                self.assertIsNone(hub.bind_warning(bind))

    def test_wildcard_and_multicast_binds_warn_and_keep_serving(self):
        for bind in ("0.0.0.0", "::", "224.0.0.1", "ff02::1"):
            with self.subTest(bind=bind):
                warning = hub.bind_warning(bind)
                self.assertTrue(warning)
                self.assertIn(bind, str(warning))

    def test_public_bind_warns(self):
        warning = hub.bind_warning("8.8.8.8")
        self.assertTrue(warning)
        self.assertIn("8.8.8.8", str(warning))

    def test_shared_and_carrier_grade_space_warns(self):
        for bind in ("100.64.0.1", "100.127.255.254"):
            with self.subTest(bind=bind):
                warning = hub.bind_warning(bind)
                self.assertTrue(warning)
                self.assertIn(bind, str(warning))

    def test_unresolvable_name_warns_without_dns(self):
        warning = hub.bind_warning("hub-1.example.com")
        self.assertTrue(warning)
        self.assertIn("hub-1.example.com", str(warning))


class ServerFamilyTest(unittest.TestCase):
    def test_ipv6_literals_select_the_ipv6_family(self):
        import socket as socket_module

        for bind in ("::1", "::"):
            with self.subTest(bind=bind):
                self.assertEqual(hub.server_family(bind), socket_module.AF_INET6)

    def test_ipv4_literals_and_names_stay_on_ipv4(self):
        import socket as socket_module

        for bind in ("127.0.0.1", "0.0.0.0", "10.0.0.5", "localhost", "hub-1.example.com"):
            with self.subTest(bind=bind):
                self.assertEqual(hub.server_family(bind), socket_module.AF_INET)


class IPv6LoopbackTest(unittest.TestCase):
    def test_ipv6_loopback_serves_the_version_route(self):
        import socket as socket_module

        if not socket_module.has_ipv6:
            self.skipTest("this host has no IPv6")
        tmp = tempfile.TemporaryDirectory(prefix="corral-hub-ipv6-")
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "hub.sqlite")
        store = hub.open_db(db)
        hub.add_host(store, "host-1")
        store.close()
        try:
            server = hub.make_server("::1", 0, db)
        except OSError as err:
            self.skipTest(f"this host cannot bind ::1: {err}")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.server_close)
        conn = http.client.HTTPConnection("::1", port, timeout=10)
        try:
            conn.request("GET", "/v1/version")
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"api": hub.HUB_API, "schema_version": hub.SCHEMA_VERSION})


class HubPathsTest(unittest.TestCase):
    def test_defaults_follow_the_state_and_config_dirs(self):
        env = {}
        self.assertEqual(
            paths.hub_db_path(env),
            os.path.join(paths.state_dir(env), "hub", "hub.sqlite"),
        )
        self.assertEqual(
            paths.hub_token_path(env), os.path.join(paths.config_dir(env), "hub-token")
        )

    def test_hub_state_override_wins(self):
        env = {"CORRAL_HUB_STATE": "/o/hub"}
        self.assertEqual(paths.hub_db_path(env), os.path.join("/o/hub", "hub.sqlite"))

    def test_a_relative_hub_state_is_refused(self):
        with self.assertRaises(paths.PathError):
            paths.hub_db_path({"CORRAL_HUB_STATE": "relative/hub"})

    def test_no_second_resolver_for_the_file_name(self):
        self.assertFalse(hasattr(hub, "DB_NAME"))


class HubSettingsTest(unittest.TestCase):
    def test_defaults_follow_the_decided_heartbeat_and_lease(self):
        resolved = settings.resolve(project={}, env={})
        self.assertEqual(resolved["hub.url"], "")
        self.assertEqual(resolved["hub.heartbeat_s"], 30)
        self.assertEqual(resolved["hub.lease_ttl_s"], 90)
        self.assertEqual(resolved["hub.lease_ttl_s"], 3 * resolved["hub.heartbeat_s"])

    def test_env_overrides_and_provider_is_listed(self):
        resolved = settings.resolve(project={}, env={"CORRAL_HUB_URL": "http://host-1:8080"})
        self.assertEqual(resolved["hub.url"], "http://host-1:8080")
        self.assertIn("corral.hub", settings.PROVIDERS)


class AddHostCommandTest(unittest.TestCase):
    def run_hub(self, *argv):
        from unittest import mock

        from corral import cli

        args = cli.build_parser().parse_args(["hub", *argv])
        with tempfile.TemporaryDirectory(prefix="corral-hub-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                return args.func(args), state

    def test_add_host_mints_and_prints_the_token_once(self):
        from unittest import mock

        from corral import cli

        with tempfile.TemporaryDirectory(prefix="corral-hub-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                args = cli.build_parser().parse_args(["hub", "add-host", "--id", "host-1"])
                self.assertEqual(args.func(args), 0)
                store = hub.open_db(paths.hub_db_path())
                try:
                    row = store.execute(
                        "SELECT host_id FROM hosts WHERE host_id=?", ("host-1",)
                    ).fetchone()
                finally:
                    store.close()
                self.assertEqual(tuple(row), ("host-1",))

    def test_add_host_twice_is_a_usage_error(self):
        from unittest import mock

        from corral import cli

        with tempfile.TemporaryDirectory(prefix="corral-hub-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                first = cli.build_parser().parse_args(["hub", "add-host", "--id", "host-1"])
                self.assertEqual(first.func(first), 0)
                second = cli.build_parser().parse_args(["hub", "add-host", "--id", "host-1"])
                self.assertEqual(second.func(second), 4)

    def test_add_host_with_a_bad_id_is_a_usage_error(self):
        code, _ = self.run_hub("add-host", "--id", "host 1")
        self.assertEqual(code, 4)

    def test_printed_token_authenticates(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        from corral import cli

        with tempfile.TemporaryDirectory(prefix="corral-hub-cli-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                args = cli.build_parser().parse_args(["hub", "add-host", "--id", "host-1"])
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(args.func(args), 0)
                token = out.getvalue().strip()
                self.assertTrue(token)
                store = hub.open_db(paths.hub_db_path({"CORRAL_HUB_STATE": state}))
                try:
                    self.assertEqual(hub.verify_token(store, token), "host-1")
                finally:
                    store.close()


class ServeCommandTest(unittest.TestCase):
    def parse(self, *argv):
        from corral import cli

        return cli.build_parser().parse_args(["hub", "serve", *argv])

    def test_bad_port_is_a_usage_error(self):
        for port in ("-1", "70000"):
            with self.subTest(port=port):
                args = self.parse("--port", port)
                self.assertEqual(args.func(args), 4)

    def test_held_loopback_port_is_a_race(self):
        import socket
        from unittest import mock

        from corral import settings

        with tempfile.TemporaryDirectory(prefix="corral-hub-serve-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                with mock.patch.object(
                    settings, "load", return_value={"hub.lease_ttl_s": 90, "hub.stale_after_s": 90}
                ):
                    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        holder.bind(("127.0.0.1", 0))
                        holder.listen(1)
                        port = holder.getsockname()[1]
                        args = self.parse("--bind", "127.0.0.1", "--port", str(port))
                        self.assertEqual(args.func(args), 2)
                    finally:
                        holder.close()

    def test_bind_warning_goes_to_stderr_and_listening_to_stdout(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        from corral import hub as hub_module
        from corral import settings

        class FakeServer:
            server_address = ("0.0.0.0", 4321)

            def __init__(self):
                self.served = False

            def serve_forever(self):
                self.served = True

            def server_close(self):
                pass

        fake = FakeServer()
        with tempfile.TemporaryDirectory(prefix="corral-hub-serve-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                with mock.patch.object(
                    settings, "load", return_value={"hub.lease_ttl_s": 90, "hub.stale_after_s": 90}
                ):
                    with mock.patch.object(hub_module, "make_server", return_value=fake) as made:
                        args = self.parse("--bind", "0.0.0.0", "--port", "4321")
                        out, err = io.StringIO(), io.StringIO()
                        with redirect_stdout(out), redirect_stderr(err):
                            self.assertEqual(args.func(args), 0)
        made.assert_called_once()
        self.assertIn("0.0.0.0", err.getvalue())
        self.assertIn("listening on 0.0.0.0:4321", out.getvalue())
        self.assertTrue(fake.served)

    def test_corrupt_store_is_a_usage_error_without_a_traceback(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        from corral import paths
        from corral import settings

        with tempfile.TemporaryDirectory(prefix="corral-hub-serve-") as state:
            db_path = paths.hub_db_path({"CORRAL_HUB_STATE": state})
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            with open(db_path, "wb") as handle:
                handle.write(b"this is not a database")
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                with mock.patch.object(
                    settings, "load", return_value={"hub.lease_ttl_s": 90, "hub.stale_after_s": 90}
                ):
                    args = self.parse("--bind", "127.0.0.1", "--port", "0")
                    err = io.StringIO()
                    with redirect_stderr(err):
                        self.assertEqual(args.func(args), 4)
        self.assertIn("corral:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_nonpositive_lease_ttl_is_a_usage_error_naming_the_setting(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        from corral import hub as hub_module
        from corral import settings

        for ttl in (0, -5):
            with self.subTest(ttl=ttl):
                with tempfile.TemporaryDirectory(prefix="corral-hub-serve-") as state:
                    with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                        with mock.patch.object(
                            settings,
                            "load",
                            return_value={"hub.lease_ttl_s": ttl, "hub.stale_after_s": 90},
                        ):
                            with mock.patch.object(hub_module, "make_server") as made:
                                args = self.parse()
                                err = io.StringIO()
                                with redirect_stderr(err):
                                    self.assertEqual(args.func(args), 4)
                                self.assertIn("hub.lease_ttl_s", err.getvalue())
                made.assert_not_called()

    def test_nonpositive_stale_after_is_a_usage_error_naming_the_setting(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        from corral import hub as hub_module
        from corral import settings

        for horizon in (0, -5):
            with self.subTest(horizon=horizon):
                with tempfile.TemporaryDirectory(prefix="corral-hub-serve-") as state:
                    with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                        with mock.patch.object(
                            settings,
                            "load",
                            return_value={"hub.lease_ttl_s": 90, "hub.stale_after_s": horizon},
                        ):
                            with mock.patch.object(hub_module, "make_server") as made:
                                args = self.parse()
                                err = io.StringIO()
                                with redirect_stderr(err):
                                    self.assertEqual(args.func(args), 4)
                                self.assertIn("hub.stale_after_s", err.getvalue())
                made.assert_not_called()


class RenewTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.add_host()
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)

    def test_renew_moves_the_lease_and_keeps_the_epoch(self):
        renewed = hub.renew(self.store, "app", 7, "drive-aaa", 1, now=1030.0, lease_ttl_s=90)
        self.assertEqual(
            (renewed.agent, renewed.host_id, renewed.epoch), ("drive-aaa", "host-1", 1)
        )
        self.assertEqual(renewed.lease_expires, 1120.0)
        row = self.store.execute(
            "SELECT epoch, lease_expires FROM claims WHERE repo=? AND number=?", ("app", 7)
        ).fetchone()
        self.assertEqual((row[0], row[1]), (1, 1120.0))

    def test_renew_with_a_wrong_epoch_conflicts_naming_the_holder(self):
        with self.assertRaises(hub.Conflict) as caught:
            hub.renew(self.store, "app", 7, "drive-aaa", 2, now=1030.0, lease_ttl_s=90)
        self.assertEqual(caught.exception.holder["agent"], "drive-aaa")
        self.assertEqual(caught.exception.holder["epoch"], 1)

    def test_renew_with_a_wrong_agent_conflicts(self):
        with self.assertRaises(hub.Conflict) as caught:
            hub.renew(self.store, "app", 7, "drive-bbb", 1, now=1030.0, lease_ttl_s=90)
        self.assertIn("drive-aaa", str(caught.exception))

    def test_renew_after_the_lease_lapsed_conflicts_then_regrant_bumps(self):
        with self.assertRaises(hub.Conflict):
            hub.renew(self.store, "app", 7, "drive-aaa", 1, now=1091.0, lease_ttl_s=90)
        receipt = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1091.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 2)

    def test_renew_at_exact_expiry_conflicts(self):
        with self.assertRaises(hub.Conflict):
            hub.renew(self.store, "app", 7, "drive-aaa", 1, now=1090.0, lease_ttl_s=90)

    def test_renew_of_an_unclaimed_ticket_conflicts(self):
        with self.assertRaises(hub.Conflict):
            hub.renew(self.store, "other", 9, "drive-aaa", 1, now=1001.0, lease_ttl_s=90)

    def test_renew_of_a_released_ticket_conflicts(self):
        hub.release(self.store, "app", 7, "drive-aaa", 1, now=1030.0)
        with self.assertRaises(hub.Conflict):
            hub.renew(self.store, "app", 7, "drive-aaa", 1, now=1031.0, lease_ttl_s=90)

    def test_renew_leaves_a_conflicting_row_untouched(self):
        with self.assertRaises(hub.Conflict):
            hub.renew(self.store, "app", 7, "drive-bbb", 1, now=1030.0, lease_ttl_s=90)
        row = self.store.execute(
            "SELECT agent, epoch, lease_expires FROM claims WHERE repo=? AND number=?",
            ("app", 7),
        ).fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("drive-aaa", 1, 1090.0))

    def test_bad_shapes_are_refused(self):
        with self.assertRaises(hub.HubError):
            hub.renew(self.store, "", 7, "drive-aaa", 1, now=1030.0, lease_ttl_s=90)
        with self.assertRaises(hub.HubError):
            hub.renew(self.store, "app", 0, "drive-aaa", 1, now=1030.0, lease_ttl_s=90)
        with self.assertRaises(hub.HubError):
            hub.renew(self.store, "app", 7, "", 1, now=1030.0, lease_ttl_s=90)
        for bad_epoch in (0, -1, True):
            with self.subTest(bad_epoch=bad_epoch), self.assertRaises(hub.HubError):
                hub.renew(self.store, "app", 7, "drive-aaa", bad_epoch, now=1030.0, lease_ttl_s=90)
        with self.assertRaises(hub.HubError):
            hub.renew(self.store, "app", 7, "drive-aaa", 1, now=1030.0, lease_ttl_s=0)


class ReleaseTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.add_host()
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)

    def test_release_clears_the_holder_and_keeps_the_epoch(self):
        kept = hub.release(self.store, "app", 7, "drive-aaa", 1, now=1030.0)
        self.assertEqual(kept, 1)
        row = self.store.execute(
            "SELECT agent, host_id, epoch, lease_expires FROM claims WHERE repo=? AND number=?",
            ("app", 7),
        ).fetchone()
        self.assertEqual(row[0], "")
        self.assertEqual(row[1], "")
        self.assertEqual(row[2], 1)
        self.assertLessEqual(row[3], 1030.0)

    def test_next_grant_after_release_fences_with_epoch_plus_one(self):
        hub.release(self.store, "app", 7, "drive-aaa", 1, now=1030.0)
        receipt = hub.grant(self.store, "app", 7, "drive-bbb", "host-2", now=1031.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 2)
        with self.assertRaises(hub.Conflict) as caught:
            hub.renew(self.store, "app", 7, "drive-aaa", 1, now=1032.0, lease_ttl_s=90)
        self.assertEqual(caught.exception.holder["agent"], "drive-bbb")

    def test_release_with_a_wrong_agent_or_epoch_conflicts(self):
        with self.assertRaises(hub.Conflict):
            hub.release(self.store, "app", 7, "drive-bbb", 1, now=1030.0)
        with self.assertRaises(hub.Conflict):
            hub.release(self.store, "app", 7, "drive-aaa", 2, now=1030.0)
        row = self.store.execute(
            "SELECT agent, epoch FROM claims WHERE repo=? AND number=?", ("app", 7)
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("drive-aaa", 1))

    def test_release_twice_conflicts_the_second_time(self):
        hub.release(self.store, "app", 7, "drive-aaa", 1, now=1030.0)
        with self.assertRaises(hub.Conflict):
            hub.release(self.store, "app", 7, "drive-aaa", 1, now=1031.0)

    def test_release_of_an_unclaimed_ticket_conflicts(self):
        with self.assertRaises(hub.Conflict):
            hub.release(self.store, "other", 9, "drive-aaa", 1, now=1030.0)

    def test_bad_shapes_are_refused(self):
        with self.assertRaises(hub.HubError):
            hub.release(self.store, "", 7, "drive-aaa", 1, now=1030.0)
        with self.assertRaises(hub.HubError):
            hub.release(self.store, "app", 7, "drive-aaa", 0, now=1030.0)


class RenewalEntryTest(unittest.TestCase):
    def test_entry_carries_repo_number_agent_and_epoch(self):
        self.assertEqual(
            hub.renewal_entry("app", 7, "drive-aaa", 3),
            {"repo": "app", "number": 7, "agent": "drive-aaa", "epoch": 3},
        )

    def test_bad_shapes_are_refused(self):
        with self.assertRaises(hub.HubError):
            hub.renewal_entry("", 7, "drive-aaa", 3)
        with self.assertRaises(hub.HubError):
            hub.renewal_entry("app", 0, "drive-aaa", 3)
        with self.assertRaises(hub.HubError):
            hub.renewal_entry("app", 7, "", 3)
        with self.assertRaises(hub.HubError):
            hub.renewal_entry("app", 7, "drive-aaa", 0)


class RenewReleaseRouteTest(ServerCase):
    def renew(self, agent="drive-aaa", host="host-1", epoch=1, token=None, **over):
        body = {"repo": "app", "number": 7, "agent": agent, "host_id": host, "epoch": epoch}
        body.update(over)
        return self.call(
            "POST",
            "/v1/claims/renew",
            body,
            token=self.token_a if token is None else token,
        )

    def release(self, agent="drive-aaa", host="host-1", epoch=1, token=None, **over):
        body = {"repo": "app", "number": 7, "agent": agent, "host_id": host, "epoch": epoch}
        body.update(over)
        return self.call(
            "POST",
            "/v1/claims/release",
            body,
            token=self.token_a if token is None else token,
        )

    def test_renew_keeps_the_epoch_and_moves_the_lease(self):
        self.propose()
        status, payload = self.renew()
        self.assertEqual(status, 200)
        self.assertEqual(
            (payload["agent"], payload["host_id"], payload["epoch"]), ("drive-aaa", "host-1", 1)
        )
        self.assertEqual(payload["lease_expires"], 1090.0)
        self.clock[0] = 1030.0
        status, payload = self.renew()
        self.assertEqual(status, 200)
        self.assertEqual(payload["epoch"], 1)
        self.assertEqual(payload["lease_expires"], 1120.0)

    def test_renew_with_a_wrong_epoch_is_409_naming_the_holder(self):
        self.propose()
        status, payload = self.renew(epoch=2)
        self.assertEqual(status, 409)
        self.assertIn("drive-aaa", payload["error"])
        self.assertEqual(payload["holder"]["epoch"], 1)

    def test_renew_after_release_is_409(self):
        self.propose()
        status, _ = self.release()
        self.assertEqual(status, 200)
        status, _ = self.renew()
        self.assertEqual(status, 409)

    def test_release_returns_the_kept_epoch_and_fences_the_next_grant(self):
        self.propose()
        status, payload = self.release()
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"repo": "app", "number": 7, "epoch": 1})
        status, granted = self.propose(agent="drive-bbb", host="host-2", token=self.token_b)
        self.assertEqual(status, 200)
        self.assertEqual(granted["epoch"], 2)
        status, payload = self.renew(agent="drive-bbb", host="host-2", epoch=1, token=self.token_b)
        self.assertEqual(status, 409)
        self.assertEqual(payload["holder"]["agent"], "drive-bbb")

    def test_release_twice_conflicts_the_second_time(self):
        self.propose()
        status, _ = self.release()
        self.assertEqual(status, 200)
        status, payload = self.release()
        self.assertEqual(status, 409)
        self.assertTrue(payload["error"])

    def test_release_by_another_holder_is_409(self):
        self.propose()
        status, payload = self.release(agent="drive-bbb", host="host-2", token=self.token_b)
        self.assertEqual(status, 409)
        self.assertIn("drive-aaa", payload["error"])

    def test_missing_or_wrong_token_is_401_without_a_leak(self):
        self.propose()
        status, _ = self.call("POST", "/v1/claims/renew", {"repo": "app"}, token=None)
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/v1/claims/release", {"repo": "app"}, token="wrong")
        self.assertEqual(status, 401)
        with self.assertLogs("corral.hub", level="WARNING") as logs:
            self.call("POST", "/v1/claims/renew", {"repo": "app"}, token="s3cret-token")
        self.assertFalse(any("s3cret-token" in line for line in logs.output))

    def test_token_for_another_host_is_refused(self):
        self.propose()
        status, _ = self.renew(host="host-2", token=self.token_a)
        self.assertEqual(status, 401)
        status, _ = self.release(host="host-2", token=self.token_a)
        self.assertEqual(status, 401)

    def test_renew_of_another_hosts_claim_is_409_without_a_holder(self):
        _, granted = self.propose(agent="drive-bbb", host="host-2", token=self.token_b)
        before = granted["lease_expires"]
        status, payload = self.renew(
            agent="drive-bbb", host="host-1", epoch=granted["epoch"], token=self.token_a
        )
        self.assertEqual(status, 409)
        self.assertNotIn("holder", payload)
        self.assertNotIn("host-2", payload.get("error", ""))
        self.assertNotIn("drive-bbb", payload.get("error", ""))
        store = hub.open_db(self.db)
        try:
            row = store.execute(
                "SELECT agent, lease_expires FROM claims WHERE repo=? AND number=?",
                ("app", 7),
            ).fetchone()
        finally:
            store.close()
        self.assertEqual(tuple(row), ("drive-bbb", before))

    def test_bad_bodies_are_400(self):
        self.propose()
        for path, make in (
            ("/v1/claims/renew", self.renew),
            ("/v1/claims/release", self.release),
        ):
            with self.subTest(path=path):
                status, payload = make(number=0)
                self.assertEqual(status, 400)
                status, payload = make(epoch=True)
                self.assertEqual(status, 400)
                status, payload = make(epoch="1")
                self.assertEqual(status, 400)
                status, payload = self.call(
                    "POST",
                    path,
                    {"repo": "app", "number": 7, "agent": "drive-aaa", "host_id": "host-1"},
                    token=self.token_a,
                )
                self.assertEqual(status, 400)
                self.assertTrue(payload["error"])

    def test_wrong_method_on_renew_and_release_is_405(self):
        for path in ("/v1/claims/renew", "/v1/claims/release"):
            with self.subTest(path=path):
                status, _ = self.call("GET", path, token=self.token_a)
                self.assertEqual(status, 405)


class ListClaimsTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.add_host()

    def test_an_empty_table_lists_nothing(self):
        self.assertEqual(hub.list_claims(self.store), [])

    def test_lists_rows_with_holder_lease_and_clocks(self):
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        rows = hub.list_claims(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0],
            {
                "repo": "app",
                "number": 7,
                "host_id": "host-1",
                "agent": "drive-aaa",
                "epoch": 1,
                "lease_expires": 1090.0,
                "updated_at": 1000.0,
            },
        )

    def test_released_rows_stay_listed_with_an_empty_holder(self):
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.release(self.store, "app", 7, "drive-aaa", 1, now=1030.0)
        rows = hub.list_claims(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["agent"], rows[0]["host_id"], rows[0]["epoch"]), ("", "", 1))


class ClaimsRouteTest(ServerCase):
    def test_lists_the_claims_table(self):
        self.propose()
        status, payload = self.call("GET", "/v1/claims", token=self.token_a)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["claims"]), 1)
        row = payload["claims"][0]
        self.assertEqual(
            (row["repo"], row["number"], row["host_id"], row["agent"], row["epoch"]),
            ("app", 7, "host-1", "drive-aaa", 1),
        )
        self.assertIn("lease_expires", row)

    def test_missing_or_wrong_token_is_401_without_a_leak(self):
        self.propose()
        for bad in ("unset", "", "wrong-token"):
            status, payload = self.call("GET", "/v1/claims", token=bad)
            self.assertEqual(status, 401)
            self.assertNotIn(self.token_a, json.dumps(payload))

    def test_a_token_for_another_host_still_lists(self):
        self.propose()
        status, payload = self.call("GET", "/v1/claims", token=self.token_b)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["claims"]), 1)

    def test_renew_and_release_stay_post_only(self):
        for path in ("/v1/claims/renew", "/v1/claims/release"):
            status, _ = self.call("GET", path, token=self.token_a)
            self.assertEqual(status, 405)


class HubClientTest(ServerCase):
    def make_client(self, host="host-1", token=None, port=None, **over):
        token_path = os.path.join(self.tmp.name, f"{host}.token")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write(token if token is not None else self.token_a)
        os.chmod(token_path, 0o600)
        url = f"http://127.0.0.1:{self.port if port is None else port}"
        return hub.HubClient(url, host, token_path, **over)

    def test_propose_grants_epoch_one(self):
        client = self.make_client()
        receipt = client.propose("app", 7, "drive-aaa")
        self.assertEqual(
            (receipt.agent, receipt.host_id, receipt.epoch), ("drive-aaa", "host-1", 1)
        )

    def test_propose_conflict_names_the_holder(self):
        client = self.make_client()
        client.propose("app", 7, "drive-aaa")
        with self.assertRaises(hub.Conflict) as caught:
            client.propose("app", 7, "drive-bbb")
        self.assertEqual(caught.exception.holder["agent"], "drive-aaa")

    def test_renew_moves_the_lease_and_keeps_the_epoch(self):
        client = self.make_client()
        first = client.propose("app", 7, "drive-aaa")
        renewed = client.renew("app", 7, "drive-aaa", first.epoch)
        self.assertEqual(renewed.epoch, first.epoch)
        self.assertGreaterEqual(renewed.lease_expires, first.lease_expires)

    def test_renew_conflict_names_the_holder(self):
        client = self.make_client()
        client.propose("app", 7, "drive-aaa")
        with self.assertRaises(hub.Conflict):
            client.renew("app", 7, "drive-bbb", 1)

    def test_release_returns_the_kept_epoch_and_fences(self):
        client = self.make_client()
        first = client.propose("app", 7, "drive-aaa")
        self.assertEqual(client.release("app", 7, "drive-aaa", first.epoch), first.epoch)
        second = client.propose("app", 7, "drive-bbb")
        self.assertEqual(second.epoch, first.epoch + 1)

    def test_claims_lists_rows(self):
        client = self.make_client()
        client.propose("app", 7, "drive-aaa")
        rows = client.claims()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["repo"], rows[0]["number"], rows[0]["agent"], rows[0]["epoch"]),
            ("app", 7, "drive-aaa", 1),
        )

    def test_an_unreachable_hub_is_hub_unreachable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
        probe.close()
        client = self.make_client(port=free)
        for call in (
            lambda: client.propose("app", 7, "drive-aaa"),
            lambda: client.renew("app", 7, "drive-aaa", 1),
            lambda: client.release("app", 7, "drive-aaa", 1),
            lambda: client.claims(),
        ):
            with self.assertRaises(hub.HubUnreachable):
                call()

    def test_a_wrong_token_is_a_hub_error(self):
        client = self.make_client(token="wrong-token")
        with self.assertRaises(hub.HubError):
            client.propose("app", 7, "drive-aaa")

    def test_a_missing_token_file_is_a_hub_error(self):
        client = hub.HubClient(
            f"http://127.0.0.1:{self.port}", "host-1", os.path.join(self.tmp.name, "absent")
        )
        with self.assertRaises(hub.HubError):
            client.propose("app", 7, "drive-aaa")

    def test_a_non_http_url_is_a_hub_error(self):
        client = self.make_client()
        client.url = "https://127.0.0.1:1/"
        with self.assertRaises(hub.HubError):
            client.propose("app", 7, "drive-aaa")


class ImportClosureTest(unittest.TestCase):
    def test_hub_reaches_no_live_seam(self):
        with open(hub.__file__, encoding="utf-8") as handle:
            source = handle.read()
        imports = set()
        for line in source.splitlines():
            match = re.match(r"\s*(?:import|from)\s+([a-z0-9_.]+)", line)
            if match:
                imports.add(match.group(1).split(".")[0])
        for banned in ("subprocess", "gh", "tracker", "forge", "systemd", "docker"):
            self.assertNotIn(banned, imports)

    def test_hub_imports_only_stdlib_paths_and_settings(self):
        with open(hub.__file__, encoding="utf-8") as handle:
            source = handle.read()
        reached = set()
        for line in source.splitlines():
            match = re.match(r"\s*from\s+(corral[a-z0-9_.]*)\s+import\s+(.+)", line)
            if not match:
                continue
            module, names = match.group(1), match.group(2)
            if module == "corral":
                reached.update(f"corral.{name.strip()}" for name in names.split(","))
            else:
                reached.add(module)
        self.assertEqual(reached, {"corral.paths", "corral.settings", "corral.netbind"})


class ReadPathClosureTest(unittest.TestCase):
    def test_host_read_paths_do_not_reach_the_hub_client(self):
        import pathlib

        # Snapshot builds the dashboard document: it must never import the
        # hub client, so a read can never turn into a hub call. The doctor
        # is not a read path: its hub rows come from injected probe data.
        source = (pathlib.Path(paths.__file__).parent / "snapshot.py").read_text(encoding="utf-8")
        reached = set()
        for line in source.splitlines():
            match = re.match(r"\s*(?:import|from)\s+([a-z0-9_.]+)", line)
            if match:
                reached.add(match.group(1))
        self.assertNotIn("corral.hub", reached)


class ExportImportTest(StoreCase):
    def test_export_carries_hosts_claims_and_epochs_but_no_secrets(self):
        self.add_host("host-1")
        first = hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        second = hub.grant(self.store, "app", 7, "drive-bbb", "host-1", now=1091.0, lease_ttl_s=90)
        self.assertEqual((first.epoch, second.epoch), (1, 2))
        document = hub.export_hub(self.store)
        self.assertEqual(document["export_version"], hub.EXPORT_VERSION)
        self.assertEqual(document["projects"], [])
        self.assertEqual(document["membership_epoch"], 0)
        self.assertNotIn("snapshots", document)
        self.assertEqual(
            [host["host_id"] for host in document["hosts"]],
            ["host-1"],
        )
        for host in document["hosts"]:
            self.assertEqual(
                sorted(host),
                ["address", "capacity", "constraints", "host_id"],
            )
        self.assertEqual(len(document["claims"]), 1)
        claim = document["claims"][0]
        self.assertEqual(
            sorted(claim),
            ["agent", "epoch", "host_id", "lease_expires", "number", "repo", "updated_at"],
        )
        self.assertEqual(
            (claim["repo"], claim["number"], claim["agent"], claim["epoch"]),
            ("app", 7, "drive-bbb", 2),
        )
        raw = json.dumps(document)
        secrets = self.store.execute(
            "SELECT token_salt, token_hash FROM hosts WHERE host_id=?", ("host-1",)
        ).fetchone()
        self.assertTrue(secrets[0])
        self.assertTrue(secrets[1])
        for token_key in ("token_salt", "token_hash"):
            self.assertNotIn(token_key, raw)
        for secret in secrets:
            self.assertNotIn(secret, raw)

    def test_export_is_json_round_trippable(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        document = hub.export_hub(self.store)
        self.assertEqual(json.loads(json.dumps(document)), document)

    def test_import_into_a_fresh_hub_preserves_epochs_for_fencing(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        hub.grant(self.store, "app", 7, "drive-bbb", "host-1", now=1091.0, lease_ttl_s=90)
        document = hub.export_hub(self.store)
        fresh_tmp, fresh_db = temp_db()
        self.addCleanup(fresh_tmp.cleanup)
        fresh = hub.open_db(fresh_db)
        self.addCleanup(fresh.close)
        claims_loaded, host_ids = hub.import_hub(fresh, json.loads(json.dumps(document)))
        self.assertEqual(claims_loaded, 1)
        self.assertEqual(host_ids, ["host-1"])
        row = fresh.execute(
            "SELECT agent, epoch FROM claims WHERE repo=? AND number=?", ("app", 7)
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("drive-bbb", 2))
        receipt = hub.grant(fresh, "app", 9, "drive-ccc", "host-1", now=2000.0, lease_ttl_s=90)
        self.assertEqual(receipt.epoch, 1)
        regrant = hub.grant(fresh, "app", 7, "drive-ccc", "host-1", now=2091.0, lease_ttl_s=90)
        self.assertEqual(regrant.epoch, 3)

    def test_import_stores_no_hosts_so_add_host_stays_the_minter(self):
        self.add_host("host-1")
        document = hub.export_hub(self.store)
        fresh_tmp, fresh_db = temp_db()
        self.addCleanup(fresh_tmp.cleanup)
        fresh = hub.open_db(fresh_db)
        self.addCleanup(fresh.close)
        hub.import_hub(fresh, document)
        self.assertEqual(fresh.execute("SELECT COUNT(*) FROM hosts").fetchone()[0], 0)
        token = hub.add_host(fresh, "host-1")
        self.assertEqual(hub.verify_token(fresh, token), "host-1")

    def test_import_carries_the_membership_epoch(self):
        self.add_host("host-1")
        document = hub.export_hub(self.store)
        document["membership_epoch"] = 4
        fresh_tmp, fresh_db = temp_db()
        self.addCleanup(fresh_tmp.cleanup)
        fresh = hub.open_db(fresh_db)
        self.addCleanup(fresh.close)
        hub.import_hub(fresh, document)
        epoch = fresh.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
        self.assertEqual(tuple(epoch), (4,))

    def test_import_upserts_the_epoch_when_the_meta_row_is_missing(self):
        document = hub.export_hub(self.store)
        document["membership_epoch"] = 4
        self.store.execute("DELETE FROM meta")
        claims_loaded, host_ids = hub.import_hub(self.store, document)
        self.assertEqual((claims_loaded, host_ids), (0, []))
        row = self.store.execute("SELECT value FROM meta WHERE key='membership_epoch'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(tuple(row), (4,))

    def test_import_ignores_snapshots_by_design(self):
        self.add_host("host-1")
        document = hub.export_hub(self.store)
        document["snapshots"] = [{"host_id": "host-1", "now": 1.0}]
        fresh_tmp, fresh_db = temp_db()
        self.addCleanup(fresh_tmp.cleanup)
        fresh = hub.open_db(fresh_db)
        self.addCleanup(fresh.close)
        claims_loaded, _ = hub.import_hub(fresh, document)
        self.assertEqual(claims_loaded, 0)


class ImportRefusalTest(StoreCase):
    def fresh(self):
        fresh_tmp, fresh_db = temp_db()
        self.addCleanup(fresh_tmp.cleanup)
        fresh = hub.open_db(fresh_db)
        self.addCleanup(fresh.close)
        return fresh

    def test_import_refuses_a_hub_that_already_holds_state(self):
        self.add_host("host-1")
        document = hub.export_hub(self.store)
        with self.assertRaises(hub.HubError):
            hub.import_hub(self.store, document)
        target = self.fresh()
        hub.add_host(target, "host-9")
        with self.assertRaises(hub.HubError):
            hub.import_hub(target, document)
        claimed = self.fresh()
        hub.add_host(claimed, "host-1")
        hub.grant(claimed, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        with self.assertRaises(hub.HubError):
            hub.import_hub(claimed, document)

    def test_import_refuses_bad_shapes(self):
        target = self.fresh()
        for bad in (
            [],
            "hub",
            {},
            {"export_version": 1},
            {
                "export_version": 1,
                "hosts": [],
                "claims": [],
                "membership_epoch": 0,
                "projects": [{"name": "a project"}],
            },
            {
                "export_version": 9999,
                "hosts": [],
                "claims": [],
                "membership_epoch": 0,
                "projects": [],
            },
            {
                "export_version": 1,
                "hosts": [{"host_id": "host 1"}],
                "claims": [],
                "membership_epoch": 0,
                "projects": [],
            },
            {
                "export_version": 1,
                "hosts": [],
                "membership_epoch": 0,
                "projects": [],
                "claims": [{"repo": "app"}],
            },
        ):
            with self.subTest(bad=bad), self.assertRaises(hub.HubError):
                hub.import_hub(self.fresh(), bad)


class BackupTest(StoreCase):
    def test_backup_copy_opens_with_rows_and_epochs_intact(self):
        self.add_host("host-1")
        hub.grant(self.store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
        dest = os.path.join(self.tmp.name, "copy", "hub-backup.sqlite")
        self.assertEqual(hub.backup_hub(self.db, dest), dest)
        mode = oct(os.stat(dest).st_mode & 0o777)
        self.assertEqual(mode, "0o600")
        copied = hub.open_db(dest)
        self.addCleanup(copied.close)
        row = copied.execute(
            "SELECT agent, epoch FROM claims WHERE repo=? AND number=?", ("app", 7)
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("drive-aaa", 1))
        self.assertEqual(hub.verify_token(copied, "nope"), None)

    def test_backup_refuses_to_clobber_and_a_missing_source(self):
        dest = os.path.join(self.tmp.name, "hub-backup.sqlite")
        hub.backup_hub(self.db, dest)
        with self.assertRaises(hub.HubError):
            hub.backup_hub(self.db, dest)
        with self.assertRaises(hub.HubError):
            hub.backup_hub(os.path.join(self.tmp.name, "absent.sqlite"), dest + ".new")

    def test_a_failing_backup_leaves_no_destination_behind(self):
        junk = os.path.join(self.tmp.name, "junk.sqlite")
        with open(junk, "w", encoding="utf-8") as handle:
            handle.write("not a database\n")
        dest = os.path.join(self.tmp.name, "copy", "hub-backup.sqlite")
        with self.assertRaises(hub.HubError):
            hub.backup_hub(junk, dest)
        self.assertFalse(os.path.exists(dest))
        missing_dest = os.path.join(self.tmp.name, "missing-backup.sqlite")
        with self.assertRaises(hub.HubError):
            hub.backup_hub(os.path.join(self.tmp.name, "absent.sqlite"), missing_dest)
        self.assertFalse(os.path.exists(missing_dest))

    def test_backup_destination_is_created_owner_only(self):
        dest = os.path.join(self.tmp.name, "hub-backup.sqlite")
        self.assertFalse(os.path.exists(dest))
        hub.backup_hub(self.db, dest)
        self.assertEqual(oct(os.stat(dest).st_mode & 0o777), "0o600")


class VersionCheckTest(unittest.TestCase):
    def test_matching_versions_pass(self):
        self.assertIsNone(hub.check_version(hub.HUB_API, hub.SCHEMA_VERSION))

    def test_older_hub_major_is_exit_4_naming_both_versions(self):
        with self.assertRaises(hub.HubVersionError) as caught:
            hub.check_version(hub.HUB_API - 1, hub.SCHEMA_VERSION)
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertIn(str(hub.HUB_API - 1), str(caught.exception))
        self.assertIn(str(hub.HUB_API), str(caught.exception))

    def test_newer_hub_major_fails_closed_naming_both_versions(self):
        with self.assertRaises(hub.HubVersionError) as caught:
            hub.check_version(hub.HUB_API + 1, hub.SCHEMA_VERSION)
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertIn(str(hub.HUB_API + 1), str(caught.exception))
        self.assertIn(str(hub.HUB_API), str(caught.exception))

    def test_schema_versions_float_within_a_major(self):
        self.assertIsNone(hub.check_version(hub.HUB_API, hub.SCHEMA_VERSION + 1))

    def test_canned_older_reply_maps_to_exit_4(self):
        reply = {"api": hub.HUB_API - 1, "schema_version": hub.SCHEMA_VERSION}
        with self.assertRaises(hub.HubVersionError) as caught:
            hub.check_version(reply["api"], reply["schema_version"])
        self.assertEqual(caught.exception.exit_code, 4)


class OlderMajorRouteTest(ServerCase):
    def test_older_major_gets_a_typed_refusal_naming_the_served_major(self):
        status, payload = self.call("GET", "/v0/version", token=self.token_a)
        self.assertEqual(status, 404)
        self.assertIn(str(hub.HUB_API), payload["error"])
        self.assertEqual(payload["api"], hub.HUB_API)
        self.assertEqual(payload["schema_version"], hub.SCHEMA_VERSION)

    def test_older_major_post_gets_a_typed_refusal(self):
        status, payload = self.call(
            "POST",
            "/v0/claims",
            {"repo": "app", "number": 7, "agent": "drive-aaa", "host_id": "host-1"},
            token=self.token_a,
        )
        self.assertEqual(status, 404)
        self.assertIn(str(hub.HUB_API), payload["error"])

    def test_older_major_on_any_405_method_gets_the_typed_refusal(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, payload = self.call(method, "/v0/claims", token=self.token_a)
                self.assertEqual(status, 404)
                self.assertIn(str(hub.HUB_API), payload["error"])
                self.assertEqual(payload["api"], hub.HUB_API)
                self.assertEqual(payload["schema_version"], hub.SCHEMA_VERSION)

    def test_older_major_head_gets_the_typed_status(self):
        status, _ = self.call("HEAD", "/v0/claims", token=self.token_a)
        self.assertEqual(status, 404)

    def test_current_major_405_methods_stay_bare(self):
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                status, _ = self.call(method, "/v1/claims", token=self.token_a)
                self.assertEqual(status, 405)

    def test_version_ignores_a_query_string(self):
        status, payload = self.call("GET", "/v1/version?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"api": hub.HUB_API, "schema_version": hub.SCHEMA_VERSION})


class OversizeBodyTest(ServerCase):
    def raw_exchange(self, request):
        """One raw request: the response head and body, then whether the server closed."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(request)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            while len(rest) < length:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                rest += chunk
            sock.settimeout(5)
            try:
                tail = sock.recv(1024)
            except ConnectionResetError:
                return head, rest, b""
            except socket.timeout:
                return head, rest, None
            return head, rest, tail
        finally:
            sock.close()

    def post(self, body, content_length=None):
        length = len(body) if content_length is None else content_length
        request = (
            b"POST /v1/claims HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {self.token_a}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {length}\r\n".encode()
            + b"Connection: keep-alive\r\n\r\n"
            + body
        )
        return self.raw_exchange(request)

    def test_oversize_body_is_a_400_that_closes_the_connection(self):
        head, rest, tail = self.post(b"x" * (hub._MAX_BODY + 1))
        self.assertIn(b" 400 ", head.split(b"\r\n")[0])
        self.assertTrue(json.loads(rest)["error"])
        # The server drops the connection instead of parsing the
        # unread bytes as the next request: an empty read, not another
        # response and not a hang.
        self.assertEqual(tail, b"")

    def test_invalid_content_length_is_a_400_that_closes_the_connection(self):
        head, rest, tail = self.post(b"{}", content_length="many")
        self.assertIn(b" 400 ", head.split(b"\r\n")[0])
        self.assertTrue(json.loads(rest)["error"])
        self.assertEqual(tail, b"")


class StoreUnavailableTest(ServerCase):
    def test_unopenable_store_is_a_500_without_a_leak(self):
        from unittest import mock

        with mock.patch.object(hub, "open_db", side_effect=hub.HubError("disk gone")):
            status, payload = self.call("GET", "/v1/claims", token=self.token_a)
            self.assertEqual(status, 500)
            self.assertEqual(payload, {"error": "hub store unavailable"})
            status, payload = self.propose()
            self.assertEqual(status, 500)
            self.assertEqual(payload, {"error": "hub store unavailable"})


class HubOperationsCommandTest(unittest.TestCase):
    def parse(self, *argv):
        from corral import cli

        return cli.build_parser().parse_args(["hub", *argv])

    def test_export_prints_json_with_epochs(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                add = self.parse("add-host", "--id", "host-1")
                self.assertEqual(add.func(add), 0)
                store = hub.open_db(paths.hub_db_path())
                try:
                    hub.grant(store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
                finally:
                    store.close()
                args = self.parse("export")
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(args.func(args), 0)
                document = json.loads(out.getvalue())
                self.assertEqual(document["claims"][0]["epoch"], 1)
                self.assertEqual(document["hosts"][0]["host_id"], "host-1")

    def test_export_import_roundtrip_through_files(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-") as first:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": first}):
                add = self.parse("add-host", "--id", "host-1")
                self.assertEqual(add.func(add), 0)
                store = hub.open_db(paths.hub_db_path())
                try:
                    hub.grant(store, "app", 7, "drive-aaa", "host-1", now=1000.0, lease_ttl_s=90)
                finally:
                    store.close()
                args = self.parse("export")
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(args.func(args), 0)
                exported = out.getvalue()
        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-") as second:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": second}):
                export_file = os.path.join(second, "hub-export.json")
                with open(export_file, "w", encoding="utf-8") as handle:
                    handle.write(exported)
                args = self.parse("import", "--file", export_file)
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(args.func(args), 0)
                self.assertIn("1", out.getvalue())
                self.assertIn("host-1", out.getvalue())
                store = hub.open_db(paths.hub_db_path())
                try:
                    row = store.execute(
                        "SELECT epoch FROM claims WHERE repo=? AND number=?", ("app", 7)
                    ).fetchone()
                finally:
                    store.close()
                self.assertEqual(tuple(row), (1,))

    def test_import_into_a_live_hub_is_a_usage_error(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                add = self.parse("add-host", "--id", "host-1")
                self.assertEqual(add.func(add), 0)
                store = hub.open_db(paths.hub_db_path())
                try:
                    document = hub.export_hub(store)
                finally:
                    store.close()
                export_file = os.path.join(state, "hub-export.json")
                with open(export_file, "w", encoding="utf-8") as handle:
                    json.dump(document, handle)
                args = self.parse("import", "--file", export_file)
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 4)
                self.assertIn("corral:", err.getvalue())

    def test_import_of_a_missing_or_broken_file_is_a_usage_error(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                args = self.parse("import", "--file", os.path.join(state, "absent.json"))
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 4)
                broken = os.path.join(state, "broken.json")
                with open(broken, "w", encoding="utf-8") as handle:
                    handle.write("this is not json")
                args = self.parse("import", "--file", broken)
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 4)

    def test_backup_writes_a_copy_and_refuses_to_clobber(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="corral-hub-ops-") as state:
            with mock.patch.dict(os.environ, {"CORRAL_HUB_STATE": state}):
                add = self.parse("add-host", "--id", "host-1")
                self.assertEqual(add.func(add), 0)
                dest = os.path.join(state, "hub-backup.sqlite")
                args = self.parse("backup", "--to", dest)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 0)
                self.assertTrue(os.path.isfile(dest))
                args = self.parse("backup", "--to", dest)
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 4)
                self.assertIn("corral:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
