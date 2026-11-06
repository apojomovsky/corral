"""Ticket 525: the dashboard reads plainly (content, clarity, visual pass).

Rendering-only change: the snapshot document keeps its stable schema
(pinned below); only the page text and styling move. Source-discipline
tests over the packaged `korapy/web/` files, the same pattern as
`test_webui.py`: no JS runner, no network beyond loopback, no real state
dirs. One test class per ticket point, then the visual-pass pins.
"""

import copy
import unittest
from importlib import resources

from korapy import snapshot
from tests.test_serve import DRIVE, ServeCase
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


def web_text(name):
    return resources.files("korapy").joinpath(f"web/{name}").read_text(encoding="utf-8")


def build_doc():
    rows = view((entry("a1"), "live"))
    made = snapshot.resources_section(
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
        T0, HOST, rows, quota_view(), config(), usage, copy.deepcopy(DRIVE), resources_view=made
    )


class SnapshotSchemaUnchanged(ServeCase):
    """The page may reword everything; the document may change nothing."""

    def test_top_level_and_header_keys_are_stable(self):
        self.assertEqual(
            set(build_doc()),
            {
                "schema_version",
                "fleet_header",
                "agents",
                "quota",
                "capacity",
                "resources",
                "disk",
                "drive",
            },
        )

    def test_fleet_header_keys_are_stable(self):
        self.assertEqual(
            set(build_doc()["fleet_header"]),
            {"host", "now", "agents_live", "agents_stale", "agents_finished"},
        )


class AgentCount(unittest.TestCase):
    """Point 1: live and stale are the headline; finished is collapsed history."""

    def test_headline_and_collapsed_finished(self):
        views = web_text("views.js")
        self.assertIn("headline", views)
        self.assertIn("finished (history)", views)
        self.assertIn("isFinishedRow", views)
        self.assertIn('"details"', views)
        self.assertIn('"summary"', views)

    def test_only_finished_rows_sit_behind_the_toggle(self):
        # An unknown future state must stay visible, never wear the
        # finished label: only an explicit finished state collapses.
        views = web_text("views.js")
        self.assertIn('row.state === "finished"', views)

    def test_finished_toggle_survives_rerenders(self):
        views = web_text("views.js")
        self.assertIn('addEventListener("toggle"', views)

    def test_strip_says_finished_is_history(self):
        views = web_text("views.js")
        self.assertIn("Finished entries are history, not agents.", views)


class QuotaAge(unittest.TestCase):
    """Point 2: the reading's age in words, stale marked and explained."""

    def test_quota_read_format_helper_exists(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatQuotaRead(", fmt)
        self.assertIn("export function formatCountdown(", fmt)
        self.assertIn("export function formatResetLine(", fmt)

    def test_quota_card_explains_stale_and_refresh(self):
        views = web_text("views.js")
        self.assertIn("older than its freshness bound counts as stale", views)
        self.assertIn("korapy quota refresh", views)

    def test_window_reset_reads_as_local_time_with_countdown(self):
        # The reset line is composed, not pasted: the local clock plus
        # the countdown, so the suite pins the composition itself.
        fmt = web_text("fmt.js")
        self.assertIn(" local, ", fmt)
        self.assertIn("formatCountdown(target, nowMs)", fmt)
        self.assertIn("reset time not reported", fmt)
        self.assertIn("due now", fmt)
        views = web_text("views.js")
        self.assertIn("formatResetLine(", views)


class CapacityLabels(unittest.TestCase):
    """Point 3: every capacity field labelled in plain words."""

    def test_capacity_fields_are_labelled(self):
        views = web_text("views.js")
        for label in ("Total capacity", "Per-repo cap", "Live now", "Pending", "By project"):
            self.assertIn(label, views)

    def test_capacity_explains_each_field(self):
        views = web_text("views.js")
        self.assertIn("How many agents the host admits in total.", views)
        self.assertIn("no per-repo limit", views)
        self.assertIn("The host does not track a queue yet.", views)


class ResourceBars(unittest.TestCase):
    """Point 4: labelled budget/charged/measured bars with spelled-out units."""

    def test_unit_format_helpers_exist(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function formatCpu(", fmt)
        self.assertIn("export function formatMem(", fmt)
        self.assertIn("millicores", fmt)
        self.assertIn("cores", fmt)

    def test_resources_card_explains_charged_vs_measured(self):
        views = web_text("views.js")
        self.assertIn("Charged is what admission reserved", views)
        self.assertIn("measured is what", views)

    def test_bare_field_names_are_replaced_by_labels(self):
        views = web_text("views.js")
        self.assertIn('"abbr"', views)
        self.assertIn("central processing unit", views)


class DriveSection(unittest.TestCase):
    """Point 5: the dispatch loop names itself and explains every field."""

    def test_drive_is_named_the_dispatch_loop(self):
        views = web_text("views.js")
        self.assertIn("dispatch loop", views)

    def test_drive_fields_are_labelled_with_help(self):
        views = web_text("views.js")
        for label in ("Status", "Last pass", "Last result", "Daily budget"):
            self.assertIn(label, views)
        self.assertIn("starts today", views)
        self.assertIn("Resumes and plans never count.", views)

    def test_last_exit_reads_as_words(self):
        fmt = web_text("fmt.js")
        self.assertIn("export function exitWord(", fmt)
        self.assertIn("finished cleanly", fmt)

    def test_pause_help_names_the_pause_file(self):
        views = web_text("views.js")
        self.assertIn("drive.pause", views)


class SectionHelp(unittest.TestCase):
    """Point 6: every section describes itself; errors say what to do."""

    def test_every_card_carries_a_one_line_description(self):
        views = web_text("views.js")
        for desc in (
            "Agents on this host right now.",
            "What the providers last reported.",
            "How many agents this host may run.",
            "Charged is what admission reserved for live agents",
            "Free space on the state and cache disks.",
            "The drive pass: sync boards, claim tickets, start agents.",
        ):
            self.assertIn(desc, views)

    def test_abbreviations_carry_tooltips(self):
        views = web_text("views.js")
        self.assertIn('"abbr"', views)
        self.assertIn("title:", views)

    def test_error_cards_say_what_to_do(self):
        views = web_text("views.js")
        self.assertIn("korapy doctor", views)

    def test_empty_agents_says_how_to_start_one(self):
        views = web_text("views.js")
        self.assertIn("korapy launch", views)


class VisualPass(unittest.TestCase):
    """The visual pass: grid, meters, table, themes, small screens."""

    def test_cards_lay_out_as_a_grid_with_a_full_width_strip(self):
        css = web_text("app.css")
        self.assertIn("grid-template-columns", css)
        self.assertIn("grid-column: 1 / -1", css)

    def test_meters_have_ok_low_stale_states(self):
        css = web_text("app.css")
        for state in (".meter.ok", ".meter.low", ".meter.stale"):
            self.assertIn(state, css)
        views = web_text("views.js")
        for state in ('"meter ok"', '"meter low"', '"meter stale"'):
            self.assertIn(state, views)

    def test_agents_render_as_a_table_with_stacking_labels(self):
        views = web_text("views.js")
        for tag in ('"table"', '"thead"', '"tbody"', '"details"'):
            self.assertIn(tag, views)
        self.assertIn("data-th", views)
        css = web_text("app.css")
        self.assertIn("attr(data-th)", css)

    def test_themes_and_figures_stay(self):
        css = web_text("app.css")
        self.assertIn("prefers-color-scheme", css)
        self.assertIn("tabular-nums", css)

    def test_narrow_layout_stays_single_column(self):
        css = web_text("app.css")
        self.assertIn("@media (max-width: 700px)", css)

    def test_full_disk_reads_high_like_full_budget(self):
        # A disk past 90% full and a budget past 90% charged share one
        # threshold and one word, so full never reads as low usage.
        views = web_text("views.js")
        self.assertIn('>= 0.9 ? "high"', views)

    def test_no_second_poll_loop_appears(self):
        app = web_text("app.js")
        self.assertNotIn("setInterval", app)

    def test_shipped_bytes_stay_under_the_maintainer_cap(self):
        total = sum(
            len(resources.files("korapy").joinpath(f"web/{name}").read_bytes())
            for name in ("index.html", "app.css", "app.js", "api.js", "fmt.js", "views.js")
        )
        self.assertLessEqual(total, 150 * 1024)


if __name__ == "__main__":
    unittest.main()
