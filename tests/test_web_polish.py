"""Ticket 542: dashboard polish (duplicate headings, unknown counts, layout).

Source-discipline tests over the packaged `korapy/web/` files and the
drive snapshot section, the same pattern as `test_web_readable.py`: no
JS runner, no network beyond loopback, no real state dirs. Temporary
journals live under `tempfile` and the drive seam is never touched.
"""

import json
import os
import re
import tempfile
import unittest
from importlib import resources

from korapy import drive


def web_text(name):
    return resources.files("korapy").joinpath(f"web/{name}").read_text(encoding="utf-8")


def function_body(source, name):
    """The text of `function name(...)` through its closing brace line."""
    match = re.search(r"function " + name + r"\(.*?\n\}", source, re.DOTALL)
    assert match is not None, "no function " + name
    return match.group(0)


class StripTitle(unittest.TestCase):
    """Point 1: the overview strip is `fleet`; the list stays `agents`."""

    def test_strip_card_is_titled_fleet(self):
        self.assertIn('card("fleet"', web_text("views.js"))

    def test_agents_list_keeps_its_title(self):
        self.assertIn('card("agents"', web_text("views.js"))

    def test_header_builder_names_fleet(self):
        self.assertIn('"fleet"', function_body(web_text("views.js"), "headerLine"))


class UnknownCounts(unittest.TestCase):
    """Point 2: a missing fleet count reads as not reported, never 0."""

    def test_missing_live_does_not_default_to_zero(self):
        self.assertNotIn("info.agents_live : 0", web_text("views.js"))

    def test_missing_stale_does_not_default_to_zero(self):
        self.assertNotIn("info.agents_stale : 0", web_text("views.js"))

    def test_missing_counts_render_not_reported(self):
        self.assertIn("not reported", function_body(web_text("views.js"), "headerLine"))


class FinishedGuard(unittest.TestCase):
    """Ticket 569: a non-finite finished count hides the toggle, never renders."""

    def test_finished_count_is_finite_guarded(self):
        body = function_body(web_text("views.js"), "headerLine")
        self.assertIn("Number.isFinite(info.agents_finished)", body)

    def test_finished_has_no_bare_typeof_default(self):
        self.assertNotIn(
            'typeof info.agents_finished === "number" ? info.agents_finished : 0',
            web_text("views.js"),
        )


class CapacityGuards(unittest.TestCase):
    """Point 3: capacity guards numbers and a non-array pending."""

    def test_total_capacity_is_guarded(self):
        views = web_text("views.js")
        self.assertNotIn('String(info.live) + " of " + String(info.total_cap)', views)
        self.assertIn("typeof info.live", function_body(views, "capacityCard"))
        self.assertIn("typeof info.total_cap", function_body(views, "capacityCard"))

    def test_live_now_is_guarded(self):
        body = function_body(web_text("views.js"), "capacityCard")
        self.assertIn("typeof info.live", body)
        self.assertIn("not reported", body)

    def test_non_array_pending_is_not_none(self):
        views = web_text("views.js")
        self.assertNotIn("Array.isArray(info.pending) ? info.pending : []", views)
        self.assertIn("not reported", function_body(views, "capacityCard"))


def write_journal(path, now):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "pass_start", "at": now}) + "\n")
        handle.write(json.dumps({"event": "pass_end", "at": now, "exit": 0}) + "\n")


class PauseFile(unittest.TestCase):
    """Point 4: the pause help names the file the snapshot reports."""

    def test_status_reports_the_pause_file_name(self):
        with tempfile.TemporaryDirectory(prefix="korapy-polish-pause-") as tmp:
            journal = os.path.join(tmp, "journal.jsonl")
            write_journal(journal, 1700000000)
            status = drive.drive_status(
                now=1700000000,
                max_starts=50,
                journal_path=journal,
                pause_path=os.path.join(tmp, "drive.pause"),
                day_path=os.path.join(tmp, "starts.json"),
                lock_path=os.path.join(tmp, "drive.lock"),
            )
        self.assertEqual(status["pause_file"], drive.PAUSE_NAME)

    def test_card_renders_the_snapshot_name(self):
        views = web_text("views.js")
        self.assertIn("pause_file", function_body(views, "driveCard"))

    def test_card_names_no_hardcoded_pause_file(self):
        self.assertNotIn("drive.pause", web_text("views.js"))


class WideLayout(unittest.TestCase):
    """Point 5: the page uses a wide window, up to three cards across."""

    def test_content_column_is_not_capped_at_seventy_rem(self):
        self.assertNotIn("max-width: 70rem", web_text("app.css"))

    def test_content_is_centered_with_a_wide_cap(self):
        css = web_text("app.css")
        self.assertIn("margin: 0 auto", css)
        self.assertIn("max-width: 96rem", css)

    def test_cards_fit_three_across(self):
        self.assertIn("min(22rem, 100%)", web_text("app.css"))


class FieldHelp(unittest.TestCase):
    """Point 6: numbers lead; per-field help rides as a tooltip."""

    def test_field_help_is_a_title_attribute(self):
        views = web_text("views.js")
        self.assertIn('setAttribute("title"', function_body(views, "field"))

    def test_no_visible_help_row_in_fields(self):
        views = web_text("views.js")
        self.assertNotIn(
            'el("dd", String(help), { class: "help" })',
            function_body(views, "field"),
        )
        self.assertNotIn(
            'el("dd", help, { class: "help" })',
            function_body(views, "resourceDim"),
        )

    def test_resource_help_is_a_title_attribute(self):
        self.assertIn('setAttribute("title"', function_body(web_text("views.js"), "resourceDim"))


class Meters(unittest.TestCase):
    """Point 7: meters fill the row with the percentage at the end."""

    def test_bar_geometry_scales_with_the_row(self):
        self.assertIn("viewBox", function_body(web_text("views.js"), "meter"))

    def test_meter_fills_its_row(self):
        css = web_text("app.css")
        meter = re.search(r"\.meter \{[^}]*\}", css)
        assert meter is not None, "no .meter rule"
        self.assertIn("100%", meter.group(0))

    def test_percentage_sits_at_the_row_end(self):
        self.assertIn(".meter span", web_text("app.css"))


class StaleQuota(unittest.TestCase):
    """Point 8: a stale provider looks stale, not just reads stale."""

    def test_stale_provider_carries_a_stale_class(self):
        self.assertIn("provider stale", function_body(web_text("views.js"), "quotaProvider"))

    def test_stale_provider_carries_a_badge(self):
        self.assertIn("badge", function_body(web_text("views.js"), "quotaProvider"))

    def test_stale_group_and_badge_are_styled(self):
        css = web_text("app.css")
        self.assertIn(".provider.stale", css)
        self.assertIn(".badge", css)


if __name__ == "__main__":
    unittest.main()
