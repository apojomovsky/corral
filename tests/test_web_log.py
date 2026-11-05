"""W5: log view (host mode).

Contract tests: every events and journal field the log view reads,
served at the production seam with fakes, asserted present with the
right types and nullability. A failing test here means an API change
that would silently blank or garble the view. Source-discipline tests
read the packaged `korapy/web/` files: the log entry points, caps and
routing exist, since no JS runner exists. Offline: a real HTTP server
on a loopback ephemeral port with fake readers. No `gh`, no network
beyond loopback, no real state dirs.
"""

import json
import re
import unittest
from importlib import resources

from korapy import coord
from tests.test_serve import EVENT_ROWS, ServeCase, healthy_doc, journal_entry
from tests.test_serve import FakeBuilder


def web_text(name):
    return resources.files("korapy").joinpath(f"web/{name}").read_text(encoding="utf-8")


class EventsContract(ServeCase):
    def get_json(self, server, target):
        status, _, body = self.fetch(server, "GET", target)
        self.assertEqual(status, 200)
        return json.loads(body)

    def start(self, rows, **kwargs):
        seen = []
        kwargs.setdefault("events_fn", lambda project: (seen.append(project), list(rows))[1])
        server, _ = self.start_server(FakeBuilder(healthy_doc()), **kwargs)
        return server, seen

    def test_event_rows_carry_the_log_key_set(self):
        server, _ = self.start(EVENT_ROWS)
        payload = self.get_json(server, "/v1/events?project=demo&resolved_since=0")
        self.assertTrue(payload["events"])
        for row in payload["events"]:
            self.assertEqual(
                set(row),
                {"id", "kind", "ref", "evidence", "state", "first_seen", "resolved_at"},
                row.get("id"),
            )

    def test_event_first_seen_is_an_iso_string(self):
        server, _ = self.start(EVENT_ROWS)
        payload = self.get_json(server, "/v1/events?project=demo&resolved_since=0")
        for row in payload["events"]:
            self.assertIsInstance(row["first_seen"], str, row.get("id"))

    def test_default_call_lists_unresolved_rows_only(self):
        server, seen = self.start(EVENT_ROWS)
        payload = self.get_json(server, "/v1/events?project=demo")
        self.assertEqual(seen, ["demo"])
        self.assertEqual(payload["project"], "demo")
        for row in payload["events"]:
            self.assertFalse(row["resolved_at"], row.get("id"))

    def test_unknown_project_is_a_400_the_view_shows_as_text(self):
        def boom(project):
            raise coord.UsageError("bad project name")

        server, _ = self.start_server(FakeBuilder(healthy_doc()), events_fn=boom)
        status, _, body = self.fetch(server, "GET", "/v1/events?project=../..")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))


class JournalContract(ServeCase):
    def get_json(self, server, target):
        status, _, body = self.fetch(server, "GET", target)
        self.assertEqual(status, 200)
        return json.loads(body)

    def start(self, entries, **kwargs):
        seen = []
        kwargs.setdefault("journal_fn", lambda project: (seen.append(project), list(entries))[1])
        server, _ = self.start_server(FakeBuilder(healthy_doc()), **kwargs)
        return server, seen

    def test_entries_carry_the_log_key_set(self):
        entries = [journal_entry(n) for n in range(3)]
        server, _ = self.start(entries)
        payload = self.get_json(server, "/v1/journal?project=demo")
        self.assertEqual(payload["entries"], entries)
        for row in payload["entries"]:
            self.assertEqual(
                set(row),
                {"at", "holder", "generation", "kind", "refs", "text"},
                row.get("text"),
            )

    def test_entry_at_is_an_iso_string_and_refs_is_a_list(self):
        entries = [journal_entry(n) for n in range(2)]
        server, _ = self.start(entries)
        payload = self.get_json(server, "/v1/journal?project=demo")
        for row in payload["entries"]:
            self.assertIsInstance(row["at"], str, row.get("text"))
            self.assertIsInstance(row["refs"], list, row.get("text"))
            self.assertIsInstance(row["text"], str, row.get("text"))

    def test_entry_kinds_are_within_the_route_kinds(self):
        entries = [journal_entry(0, "note"), journal_entry(1, "action")]
        server, _ = self.start(entries)
        payload = self.get_json(server, "/v1/journal?project=demo")
        for row in payload["entries"]:
            self.assertIn(row["kind"], coord.KINDS, row.get("text"))

    def test_every_route_kind_filters_cleanly(self):
        entries = [journal_entry(n, coord.KINDS[n % len(coord.KINDS)]) for n in range(7)]
        server, seen = self.start(entries)
        for kind in coord.KINDS:
            payload = self.get_json(server, f"/v1/journal?project=demo&kind={kind}")
            for row in payload["entries"]:
                self.assertEqual(row["kind"], kind)
        self.assertTrue(seen)

    def test_cursor_is_a_decimal_entry_index(self):
        # The accepted deviation of section 2: the log view learns
        # `total` from a `limit=1` reply and requests the last window
        # by index. A future cursor reminting breaks here first, not
        # in a browser.
        entries = [journal_entry(n) for n in range(5)]
        server, _ = self.start(entries)
        first = self.get_json(server, "/v1/journal?project=demo&limit=1")
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["cursor"], "1")
        window = self.get_json(
            server, "/v1/journal?project=demo&limit=100&cursor=%d" % (first["total"] - 1)
        )
        self.assertEqual(window["entries"], entries[4:])
        self.assertEqual(window["cursor"], "5")

    def test_unknown_project_is_a_400_the_view_shows_as_text(self):
        def boom(project):
            raise coord.UsageError("bad project name")

        server, _ = self.start_server(FakeBuilder(healthy_doc()), journal_fn=boom)
        status, _, body = self.fetch(server, "GET", "/v1/journal?project=demo")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))


class LogSourceDiscipline(unittest.TestCase):
    def test_data_entry_points_exist(self):
        api = web_text("api.js")
        for name in (
            "export function loadEvents(",
            "export function loadJournal(",
            "export async function loadJournalLast(",
            "export function loadJournalOlder(",
            "export function loadProjects(",
            "export function projectNames(",
            "export function nextLogDelay(",
        ):
            self.assertIn(name, api)

    def test_log_cadence_and_caps_are_pinned(self):
        api = web_text("api.js")
        self.assertIn("export const LOG_POLL_MS = 15000;", api)
        self.assertIn("export const LOG_JOURNAL_PAGE = 100;", api)
        self.assertIn("export const LOG_JOURNAL_CAP = 300;", api)
        self.assertIn("export const LOG_EVENTS_CAP = 100;", api)

    def test_kind_list_matches_the_route(self):
        from korapy import coord as coord_module

        api = web_text("api.js")
        found = re.search(r"export const LOG_KINDS = \[([^\]]*)\];", api)
        self.assertIsNotNone(found)
        assert found is not None
        names = re.findall(r'"([^"]+)"', found.group(1))
        self.assertEqual(tuple(names), coord_module.KINDS)

    def test_last_window_learns_total_first(self):
        # The journal pages oldest first, so the newest window is
        # found by learning `total` from a one-row reply and then
        # requesting the trailing window by index.
        api = web_text("api.js")
        at = api.find("export async function loadJournalLast(")
        self.assertGreater(at, -1)
        body = api[at : api.find("\n}\n", at)]
        self.assertIn("limit=1", body)
        self.assertIn("cursor=", body)

    def test_log_view_entry_points_exist(self):
        views = web_text("views.js")
        for name in (
            "export function renderLog(",
            "export function renderTabs(",
        ):
            self.assertIn(name, views)

    def test_log_states_render_as_text(self):
        views = web_text("views.js")
        self.assertIn("Choose a project", views)
        self.assertIn("Unknown project", views)
        self.assertIn(" of ", views)

    def test_log_chips_link_the_log_view(self):
        views = web_text("views.js")
        self.assertIn('"#/log?project=" + encodeURIComponent(name)', views)

    def test_log_routing_exists(self):
        app = web_text("app.js")
        self.assertIn('"#/log"', app)
        self.assertIn('"#/log?"', app)
        self.assertIn("renderLog(", app)
        self.assertIn("nextLogDelay(failures)", app)
        self.assertNotIn("LOG_POLL_MS", app)
        self.assertNotIn("LOG_JOURNAL_PAGE", app)

    def test_log_schedule_runs_only_from_a_finally(self):
        # One shared reschedule in the poll loop's finally: the log
        # route picks the 15s cadence there instead of growing a
        # second loop that could die on its own.
        app = web_text("app.js")
        self.assertIn('route.name === "log" ? nextLogDelay(failures) : nextDelay(failures)', app)
        sites = list(re.finditer(r"schedule\(config, token\);", app))
        self.assertEqual(len(sites), 1)
        start = sites[0].start()
        self.assertGreater(app.rfind("finally {", 0, start), app.rfind("catch", 0, start))

    def test_every_log_poll_await_sits_inside_a_try(self):
        app = web_text("app.js")
        for name in ("loadEvents", "loadJournalLast", "loadProjects"):
            sites = list(re.finditer(r"await\s+" + name + r"\s*\(", app))
            self.assertTrue(sites, name)
            for site in sites:
                start = site.start()
                self.assertGreater(app.rfind("try {", 0, start), app.rfind("catch", 0, start), name)

    def test_tabs_link_the_log_view(self):
        # The tabs render from JS into an empty nav, so the pin is on
        # the renderer: both destinations with the overview first.
        views = web_text("views.js")
        self.assertIn('["#/", "Overview", "overview"]', views)
        self.assertIn('["#/log", "Log", "log"]', views)
        html = web_text("index.html")
        self.assertIn('id="tabs"', html)


def app_body(name):
    app = web_text("app.js")
    at = app.find(name)
    assert at > -1, name
    return app[at : app.find("\n}\n", at)]


class LogFingerprintIncludesKind(unittest.TestCase):
    """Item 4: the tickLog render-skip fingerprint covers the kind."""

    def test_fingerprint_names_log_kind(self):
        body = app_body("async function tickLog(")
        at = body.find("const fingerprint =")
        self.assertGreater(at, -1)
        window = body[at : at + 800]
        self.assertIn("logKind", window)


class OlderFailureStaysInCard(unittest.TestCase):
    """Item 5: a failed load-older stays in the journal card."""

    def test_older_failure_touches_no_global_failure_state(self):
        body = app_body("async function pollOlder(")
        at = body.find("if (!(reply")
        self.assertGreater(at, -1)
        failure = body[at : at + 900]
        self.assertNotIn("failures += 1", failure)
        self.assertNotIn("renderConnection(", failure)

    def test_older_failure_shows_the_journal_retry_text(self):
        body = app_body("async function pollOlder(")
        at = body.find("if (!(reply")
        self.assertGreater(at, -1)
        window = body[at : at + 1200]
        self.assertIn("journalState", window)
        self.assertIn("renderLog(", window)


class JournalWindowRecomputesStart(unittest.TestCase):
    """Item 6: the newest window recomputes start from its own reply."""

    def test_load_journal_last_recomputes_from_the_window_reply(self):
        api = web_text("api.js")
        at = api.find("export async function loadJournalLast(")
        self.assertGreater(at, -1)
        body = api[at : api.find("\n}\n", at)]
        fetches = [m.start() for m in re.finditer(r"await fetchJson\(", body)]
        self.assertGreaterEqual(len(fetches), 2)
        second = body[fetches[1] :]
        self.assertTrue(
            "windowReply.data.total" in second or "windowReply.data.cursor" in second,
            second[:600],
        )

    def test_tick_log_derives_start_from_the_window_cursor(self):
        body = app_body("async function tickLog(")
        self.assertIn("parsedCursor", body)
        self.assertIn("journalReply.data.cursor", body)


if __name__ == "__main__":
    unittest.main()
