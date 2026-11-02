"""W6: board view, with `body=0` (decision D6).

Contract and slimming tests: the additive `/v1/board` parameter omits
`body` and `extra` from rows, the default reply is unchanged, paging
still honors `total`, and every field the board view reads is present
with the right types. Source-discipline tests pin the view's own
rules: named constants, the `body=0` request shape, scheme-checked
tracker links, and no ticket body in the rendered output. Offline: a
real HTTP server on a loopback ephemeral port, a scratch board store,
source reads over the packaged `corral/web/` files. No `gh`, no
network beyond loopback, no real state dirs.
"""

import json
import os
import re
import tempfile
import unittest
from importlib import resources

from corral import board, serve
from tests.test_serve import FakeBuilder, ServeCase, healthy_doc
from tests.test_snapshot import T0


def web_bytes(name):
    return resources.files("corral").joinpath(f"web/{name}").read_bytes()


def stored_board_with_text(path, *itemspec, claims=()):
    """A board store at `path` holding items with bodies and extras."""
    store = board.open_store(path)
    try:
        for spec in itemspec:
            item = board.Item(
                repo=spec.get("repo", "app"),
                number=spec["number"],
                title=spec.get("title", f"Title {spec['number']}"),
                body=spec.get("body", f"Body {spec['number']}"),
                state=spec.get("state", "open"),
                author="alice",
                labels=list(spec.get("labels", [])),
                priority=spec.get("priority", ""),
                url=spec.get("url", ""),
                fetched_at=spec.get("fetched_at", T0),
                extra=dict(spec.get("extra", {})),
            )
            store.upsert_item(item, operators=["alice"], now=spec.get("fetched_at", T0))
        for number, agent in claims:
            store.claim("app", number, agent, "host-1", now=T0)
    finally:
        store.close()


class BoardSlimParam(ServeCase):
    def open_store(self, tmp, *itemspec, claims=()):
        stored_board_with_text(os.path.join(tmp, "board.sqlite"), *itemspec, claims=claims)
        return lambda: board.open_store(os.path.join(tmp, "board.sqlite"))

    def get_json(self, server, target):
        status, headers, body = self.fetch(server, "GET", target)
        self.assertEqual(status, 200, target)
        self.assertEqual(headers.get("Content-Type"), "application/json", target)
        return json.loads(body)

    def test_body_zero_drops_exactly_body_and_extra(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-board-slim-") as tmp:
            opener = self.open_store(
                tmp,
                {"number": 3, "extra": {"sprint": "12"}},
                {"number": 7, "extra": {"sprint": "13"}},
            )
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            full = self.get_json(server, "/v1/board")
            slim = self.get_json(server, "/v1/board?body=0")
            self.assertEqual(len(full["board"]), 2)
            self.assertEqual(len(slim["board"]), 2)
            for fat, thin in zip(full["board"], slim["board"]):
                self.assertIn("body", fat)
                self.assertIn("extra", fat)
                self.assertNotIn("body", thin)
                self.assertNotIn("extra", thin)
                self.assertEqual(set(fat) - {"body", "extra"}, set(thin))
                for key in thin:
                    self.assertEqual(thin[key], fat[key], key)

    def test_default_reply_still_carries_body_and_extra(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-board-slim-") as tmp:
            opener = self.open_store(tmp, {"number": 3, "extra": {"sprint": "12"}})
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            payload = self.get_json(server, "/v1/board")
            row = payload["board"][0]
            self.assertEqual(row["body"], "Body 3")
            self.assertEqual(row["extra"], {"sprint": "12"})

    def test_body_one_reads_like_the_default(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-board-slim-") as tmp:
            opener = self.open_store(tmp, {"number": 3, "extra": {"sprint": "12"}})
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            default = self.get_json(server, "/v1/board")
            explicit = self.get_json(server, "/v1/board?body=1")
            self.assertEqual(explicit, default)

    def test_body_zero_paging_honors_total(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-board-slim-") as tmp:
            specs = [{"number": n, "fetched_at": T0 + n} for n in range(1, 4)]
            opener = self.open_store(tmp, *specs)
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            payload = self.get_json(server, "/v1/board?body=0&limit=2&offset=1")
            self.assertEqual(payload["total"], 3)
            self.assertEqual(payload["limit"], 2)
            self.assertEqual(payload["offset"], 1)
            self.assertEqual([row["number"] for row in payload["board"]], [2, 3])
            for row in payload["board"]:
                self.assertNotIn("body", row)
                self.assertNotIn("extra", row)

    def test_body_bad_value_is_a_400(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-board-slim-") as tmp:
            opener = self.open_store(tmp, {"number": 3})
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            for target in ("/v1/board?body=many", "/v1/board?body=2", "/v1/board?body="):
                with self.subTest(target=target):
                    status, _, raw = self.fetch(server, "GET", target)
                    self.assertEqual(status, 400, target)
                    self.assertIn("error", json.loads(raw))


class BoardContract(ServeCase):
    def test_board_rows_carry_the_fields_the_view_reads(self):
        with tempfile.TemporaryDirectory(prefix="corral-serve-board-view-") as tmp:
            stored_board_with_text(
                os.path.join(tmp, "board.sqlite"),
                {
                    "number": 3,
                    "title": "Fix the leak",
                    "state": "open",
                    "labels": ["ready"],
                    "priority": "high",
                    "url": "https://tracker.example.com/app/3",
                },
                claims=[(3, "agent-1")],
            )
            opener = lambda: board.open_store(os.path.join(tmp, "board.sqlite"))
            server, _ = self.start_server(FakeBuilder(healthy_doc()), board_open=opener)
            status, _, raw = self.fetch(server, "GET", "/v1/board?body=0&max_age=300")
            self.assertEqual(status, 200)
            payload = json.loads(raw)
            self.assertEqual(payload["total"], 1)
            row = payload["board"][0]
            self.assertIsInstance(row["repo"], str)
            self.assertIsInstance(row["number"], int)
            self.assertIsInstance(row["title"], str)
            self.assertIsInstance(row["state"], str)
            self.assertIsInstance(row["priority"], str)
            self.assertIsInstance(row["labels"], list)
            self.assertIsInstance(row["url"], str)
            self.assertIsInstance(row["fetched_at"], (int, float))
            self.assertIsInstance(row["confidence"], str)
            self.assertIsInstance(row["fresh"], bool)
            claim = row["claim"]
            self.assertIsInstance(claim, dict)
            self.assertIsInstance(claim["agent"], str)
            self.assertIsInstance(claim["host"], str)
            self.assertIsInstance(claim["epoch"], int)
            self.assertIsInstance(claim["claimed_at"], (int, float))


class WebBoardSources(unittest.TestCase):
    def test_board_api_entry_points_exist(self):
        api = web_bytes("api.js").decode("utf-8")
        for name in (
            "export function loadBoard(",
            "export const BOARD_POLL_MS = 15000;",
            "export const BOARD_PAGE_SIZE = 50;",
            "export const BOARD_MAX_AGE_S = 300;",
        ):
            self.assertIn(name, api)

    def test_board_requests_are_slimmed_bounded_and_paged(self):
        api = web_bytes("api.js").decode("utf-8")
        start = api.find("export function loadBoard(")
        self.assertGreater(start, -1)
        body = api[start : api.find("\n}\n", start)]
        self.assertIn('"body", "0"', body)
        self.assertIn("BOARD_MAX_AGE_S", body)
        self.assertIn("BOARD_PAGE_SIZE", body)
        self.assertIn('"limit"', body)
        self.assertIn('"offset"', body)
        self.assertIn('"max_age"', body)

    def test_board_delay_uses_the_poll_base_then_backoff(self):
        api = web_bytes("api.js").decode("utf-8")
        self.assertIn("export function nextBoardDelay(failures) {", api)
        start = api.find("export function nextBoardDelay(failures) {")
        body = api[start : api.find("\n}\n", start)]
        self.assertIn("BOARD_POLL_MS", body)
        self.assertIn("BACKOFF_MS", body)

    def test_board_view_entry_points_exist(self):
        views = web_bytes("views.js").decode("utf-8")
        for name in (
            "export function renderBoardShell(",
            "export function renderBoardRows(",
            "export function trackerLink(",
        ):
            self.assertIn(name, views)

    def test_tracker_links_allow_only_http_and_rel_noopener(self):
        views = web_bytes("views.js").decode("utf-8")
        start = views.find("export function trackerLink(")
        self.assertGreater(start, -1)
        body = views[start : views.find("\n}\n", start)]
        self.assertIn('"http:"', body)
        self.assertIn('"https:"', body)
        self.assertIn('"noopener noreferrer"', body)
        self.assertNotIn("innerHTML", body)

    def test_board_rows_render_claim_freshness_and_counts(self):
        views = web_bytes("views.js").decode("utf-8")
        start = views.find("export function renderBoardRows(")
        self.assertGreater(start, -1)
        end = views.find("export function ", start + len("export function renderBoardRows("))
        body = views[start:] if end == -1 else views[start:end]
        self.assertIn("trackerLink", body)
        self.assertIn("fresh", body)
        self.assertIn("confidence", body)
        self.assertIn("total", body)

    def test_board_view_never_names_the_ticket_body(self):
        views = web_bytes("views.js").decode("utf-8")
        for name in ("renderBoardShell(", "renderBoardRows(", "boardRow("):
            at = views.find("function " + name)
            if at == -1:
                continue
            end = views.find("\n}\n", at)
            self.assertNotIn(".body", views[at:end], name)

    def test_board_route_and_mount_exist(self):
        app = web_bytes("app.js").decode("utf-8")
        self.assertIn('"#/board"', app)
        self.assertIn("renderBoardShell", app)
        self.assertIn("renderBoardRows", app)
        self.assertIn("loadBoard", app)
        self.assertIn("nextBoardDelay", app)
        self.assertIn("BOARD_PAGE_SIZE", app)

    def test_poll_loops_retire_on_navigation(self):
        app = web_bytes("app.js").decode("utf-8")
        self.assertIn("pollGen += 1", app)
        for loop in ("schedule(config, token);", "scheduleBoard(config, token);"):
            at = app.find(loop)
            self.assertGreater(at, -1, loop)
            window = app[max(0, at - 100) : at]
            self.assertIn("gen === pollGen", window, loop)

    def test_board_poll_sits_inside_a_try(self):
        app = web_bytes("app.js").decode("utf-8")
        sites = list(re.finditer(r"await\s+loadBoard\s*\(", app))
        self.assertTrue(sites)
        for site in sites:
            start = site.start()
            self.assertGreater(app.rfind("try {", 0, start), app.rfind("catch", 0, start))

    def test_shell_has_board_tab(self):
        # The tabs render from JS into an empty nav (the log view's
        # pin reads the same way), so the pin is on the renderer:
        # the board destination sits between overview and log.
        views = web_bytes("views.js").decode("utf-8")
        self.assertIn('["#/", "Overview", "overview"]', views)
        self.assertIn('["#/board", "Board", "board"]', views)
        self.assertIn('["#/log", "Log", "log"]', views)
        html = web_bytes("index.html").decode("utf-8")
        self.assertIn('id="tabs"', html)
        self.assertIn('id="board"', html)


def app_text():
    return web_bytes("app.js").decode("utf-8")


def loop_body(text, name):
    at = text.find(name)
    assert at > -1, name
    return text[at : text.find("\n}\n", at)]


class RetiredLoopsRetire(unittest.TestCase):
    """Item 7: a retired poll loop draws nothing after navigation."""

    def test_tick_checks_gen_after_every_await(self):
        body = loop_body(app_text(), "async function tick(")
        awaits = [m.start() for m in re.finditer(r"await\s+\w+", body)]
        self.assertGreaterEqual(len(awaits), 2)
        for pos in awaits:
            guard = body.find("gen !== pollGen", pos)
            next_await = body.find("await", pos + 1)
            render = body.find("render", pos)
            failure = body.find("failures +=", pos)
            bound = min(x for x in (next_await, render, failure) if x > -1)
            self.assertGreater(guard, -1)
            self.assertLess(guard, bound)

    def test_board_tick_checks_gen_after_every_await(self):
        body = loop_body(app_text(), "async function boardTick(")
        awaits = [m.start() for m in re.finditer(r"await\s+\w+", body)]
        self.assertGreaterEqual(len(awaits), 2)
        for pos in awaits:
            guard = body.find("gen !== pollGen", pos)
            next_await = body.find("await", pos + 1)
            render = body.find("render", pos)
            failure = body.find("failures +=", pos)
            bound = min(x for x in (next_await, render, failure) if x > -1)
            self.assertGreater(guard, -1)
            self.assertLess(guard, bound)

    def test_hashchange_clears_the_timer(self):
        app = app_text()
        at = app.find('window.addEventListener("hashchange"')
        self.assertGreater(at, -1)
        window = app[at : at + 800]
        self.assertIn("pollGen += 1", window)
        self.assertIn("clearTimeout(timer)", window)


class BoardPagerClamp(unittest.TestCase):
    """Item 8: the pager stays honest when offset passes total."""

    def test_board_tick_clamps_offset_to_the_last_page(self):
        body = loop_body(app_text(), "async function boardTick(")
        self.assertIn("boardOffset", body)
        self.assertIn("Math.ceil", body)
        self.assertIn("BOARD_PAGE_SIZE", body)

    def test_render_board_rows_never_inverts_the_count(self):
        views = web_bytes("views.js").decode("utf-8")
        at = views.find("export function renderBoardRows(")
        self.assertGreater(at, -1)
        end = views.find("export function ", at + 10)
        body = views[at:end] if end > -1 else views[at:]
        self.assertIn("Math.min", body)
        count_at = body.find('+ " of " +')
        self.assertGreater(count_at, -1)
        window = body[max(0, count_at - 600) : count_at + 200]
        self.assertTrue("Math.min" in window or "Math.max" in window)


if __name__ == "__main__":
    unittest.main()
