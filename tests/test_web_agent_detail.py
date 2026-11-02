"""W3: agent detail and tail (host mode), plus three W2 review fixes.

The detail resolves an agent row key against a fresh snapshot and polls
`GET /v1/tail` for the row's ticket; the tail pane is one text node,
follow/pause are memory-only, and a 404 tail reads as "nothing to
show" without breaking the poll loop. The W2 fixes: the hashchange
re-render updates its cache only after a clean draw, the overview
builds off-DOM and swaps once, and the `isErrorSection` mirror is
pinned on both sides.

Offline: a real HTTP server on a loopback ephemeral port with injected
fakes, plus source-discipline reads over the packaged `corral/web/`
files. No `gh`, no network beyond loopback, no real state dirs.
"""

import http.client
import json
import re
import unittest
from importlib import resources

from corral import snapshot
from tests.test_serve import FakeBuilder, ServeCase, healthy_doc


def web_text(name):
    return resources.files("corral").joinpath(f"web/{name}").read_text(encoding="utf-8")


def js_body(text, signature):
    start = text.find(signature)
    assert start > -1, signature
    end = text.find("\n}\n", start)
    assert end > start, signature
    return text[start:end]


class TailAuth(ServeCase):
    """The tail route the detail polls keeps the data-route auth rule."""

    def fetch_tail(self, server, token=None):
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            conn.request("GET", "/v1/tail?repo=app&number=7", headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_tail_needs_a_token_when_required(self):
        server, _ = self.start_server(
            FakeBuilder(healthy_doc()),
            tail_fn=lambda repo, number, last: "line one\n",
            token_required=True,
            token_reader=lambda: "tok-1",
        )
        status, body = self.fetch_tail(server)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        status, _ = self.fetch_tail(server, token="wrong")
        self.assertEqual(status, 401)
        status, body = self.fetch_tail(server, token="tok-1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"line one\n")


class ErrorSectionAgreement(unittest.TestCase):
    """Fix 3: the browser mirror of the server's error-section predicate."""

    def test_server_predicate_is_the_single_error_key(self):
        self.assertTrue(snapshot.is_error_section({"error": "gone"}))
        self.assertTrue(snapshot.is_error_section({"error": None}))
        self.assertTrue(snapshot.is_error_section({"error": {"nested": 1}}))
        for value in (
            {},
            {"error": "gone", "extra": 1},
            {"Error": "gone"},
            [],
            [1],
            None,
            "error",
            0,
        ):
            self.assertFalse(snapshot.is_error_section(value), repr(value))

    def test_browser_mirror_pins_the_same_shape(self):
        body = js_body(web_text("api.js"), "export function isErrorSection(")
        self.assertIn('keys.length === 1 && keys[0] === "error"', body)
        self.assertIn("Array.isArray", body)


class RenderCurrentCacheOrder(unittest.TestCase):
    """Fix 1: the hashchange re-render caches only after a clean draw."""

    def body(self):
        return js_body(web_text("app.js"), "function renderCurrent(")

    def test_filter_cache_updates_only_after_a_clean_overview_draw(self):
        body = self.body()
        draw = body.find("renderOverview(lastDoc")
        self.assertGreater(draw, -1)
        for name in ("lastProject = route.project", "lastNotice = route.notice"):
            self.assertGreater(body.find(name), draw, name)

    def test_a_thrown_draw_returns_false_for_the_tick_fallthrough(self):
        body = self.body()
        draw = body.find("renderOverview(lastDoc")
        self.assertGreater(draw, -1)
        caught = body.find("} catch", draw)
        self.assertGreater(caught, draw)
        self.assertGreater(body.find("return false", caught), caught)


class OverviewSwapsOnce(unittest.TestCase):
    """Fix 2: the overview builds off-DOM and swaps once per section."""

    def test_cards_swap_once_after_an_off_dom_build(self):
        body = js_body(web_text("views.js"), "export function renderOverview(")
        self.assertGreaterEqual(body.count("replaceChildren"), 3)
        self.assertNotIn("appendChild", body)
        self.assertNotIn('getElementById("cards").textContent', body)
        self.assertNotIn('getElementById("agents").textContent', body)


class DetailDiscipline(unittest.TestCase):
    def test_detail_entry_points_exist(self):
        api = web_text("api.js")
        for name in (
            "export const TAIL_POLL_MS = 3000;",
            "export async function fetchText(",
            "export function loadTail(",
            "export function hasTicket(",
            "export function agentKey(",
            "export function detailHash(",
            "export function resolveAgent(",
        ):
            self.assertIn(name, api)
        views = web_text("views.js")
        for name in (
            "export function renderDetail(",
            "export function renderDetailMissing(",
            "export function renderTail(",
            "export function renderTailMissing(",
            "export function renderTailFailed(",
            "export function renderTailLoading(",
            "export function renderTailButtons(",
        ):
            self.assertIn(name, views)
        app = web_text("app.js")
        for name in (
            "TAIL_POLL_MS",
            "loadTail(",
            "resolveAgent(",
            "hasTicket(",
            "renderDetail(",
            "renderDetailMissing(",
            "renderTail(",
        ):
            self.assertIn(name, app)

    def test_tail_renders_as_a_single_text_node(self):
        body = js_body(web_text("views.js"), "export function renderTail(")
        self.assertIn('getElementById("tail")', body)
        self.assertIn(".textContent =", body)
        self.assertNotIn("appendChild", body)
        self.assertNotIn("createElement", body)
        html = web_text("index.html")
        self.assertEqual(html.count("<pre"), 1)
        self.assertIn('id="tail"', html)

    def test_tail_404_says_nothing_to_show(self):
        views = web_text("views.js")
        missing = js_body(views, "export function renderTailMissing(")
        self.assertIn('"nothing to show"', missing)
        app = web_text("app.js")
        self.assertIn("status === 404", app)
        self.assertIn("renderTailMissing(", app)

    def test_tail_fetch_maps_network_failure_to_the_failure_branch(self):
        body = js_body(web_text("api.js"), "export async function fetchText(")
        self.assertIn("return { status: 0, text: null };", body)

    def test_ticketless_agents_state_no_ticket_no_tail(self):
        self.assertIn('"no ticket, no tail"', web_text("views.js"))

    def test_gone_agents_are_stated_never_guessed(self):
        views = web_text("views.js")
        self.assertIn('"this agent is no longer listed"', views)
        body = js_body(web_text("api.js"), "export function resolveAgent(")
        self.assertIn("return null", body)

    def test_agent_rows_link_by_row_key(self):
        # The hash shape is owned by api.js detailHash; views only
        # keys the row and hands the key over.
        api = web_text("api.js")
        self.assertIn('"#/agent?id=" + encodeURIComponent', api)
        self.assertIn('"#/agent?repo=" + encodeURIComponent', api)
        views = web_text("views.js")
        self.assertIn("agentKey(", views)
        self.assertIn("detailHash(", views)

    def test_detail_route_parses_in_app(self):
        app = web_text("app.js")
        self.assertIn('"#/agent"', app)
        self.assertIn("resolveAgent(", app)

    def test_detail_shell_anchors_exist(self):
        html = web_text("index.html")
        for anchor in (
            'id="detail"',
            'id="detail-back"',
            'id="detail-fields"',
            'id="tail-controls"',
            'id="tail-pause"',
            'id="tail-follow"',
            'id="tail"',
            'id="tail-state"',
        ):
            self.assertIn(anchor, html)

    def test_detail_fields_cover_the_whole_row(self):
        body = js_body(web_text("views.js"), "export function renderDetail(")
        for label in ('"id: "', '"project: "', '"profile: "'):
            self.assertIn(label, body)

    def test_detail_buttons_name_both_states(self):
        body = js_body(web_text("views.js"), "export function renderTailButtons(")
        for label in ('"tail: pause"', '"tail: resume"', '"follow: on"', '"follow: off"'):
            self.assertIn(label, body)

    def test_every_tail_await_sits_inside_a_try(self):
        app = web_text("app.js")
        sites = list(re.finditer(r"await\s+loadTail\s*\(", app))
        self.assertTrue(sites)
        for site in sites:
            start = site.start()
            self.assertGreater(app.rfind("try {", 0, start), app.rfind("catch", 0, start))

    def test_tail_schedule_uses_shared_delays(self):
        app = web_text("app.js")
        self.assertIn("TAIL_POLL_MS", app)
        self.assertIn("nextDelay(tailFailures)", app)

    def test_stopped_state_clears_the_detail(self):
        body = js_body(web_text("views.js"), "export function renderStopped(")
        self.assertIn('"detail"', body)

    def test_tail_pane_scrolls_in_css(self):
        self.assertIn("#tail", web_text("app.css"))


if __name__ == "__main__":
    unittest.main()
