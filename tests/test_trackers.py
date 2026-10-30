"""The one tracker factory: labels or board-backed, from resolved settings.

Pure construction over the injected ``gh`` seam: tests pass fakes, so no
network, no subprocess, no real state dir. Production passes the real
runners through ``build_tracker`` defaults.
"""

import unittest

from corral import settings
from corral import tracker as iface
from corral import trackers
from tests.test_board_fields import FakePacer
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    rest_issue,
    schema_payload,
    select_value,
    text_value,
)
from tests.test_tracker import item_payload


class VariantsTest(unittest.TestCase):
    def test_labels_is_the_default(self):
        self.assertEqual(settings.REGISTRY["tracker.variant"].default, "labels")
        self.assertIn(settings.REGISTRY["tracker.variant"].default, trackers.VARIANTS)

    def test_exactly_labels_and_board(self):
        self.assertEqual(set(trackers.VARIANTS), {"labels", "board"})

    def test_settings_imports_the_canonical_set(self):
        self.assertIs(settings.VARIANTS, trackers.VARIANTS)

    def test_sync_help_names_every_variant(self):
        """The prose in ``sync.py`` lists the same set: a reader of the
        help sees every value ``settings`` accepts."""
        help_text = settings.REGISTRY["tracker.variant"].help
        for variant in trackers.VARIANTS:
            self.assertIn(variant, help_text)


class BuildTrackerTest(unittest.TestCase):
    def test_labels_builds_the_rest_tracker(self):
        from corral.github.tracker import GithubTracker

        tracker = trackers.build_tracker(
            variant="labels", board=None, max_pages=5, run=lambda args: "{}"
        )
        self.assertIsInstance(tracker, GithubTracker)

    def test_board_builds_the_backed_tracker(self):
        from tests.test_board_reader import config

        tracker = trackers.build_tracker(
            variant="board", board=config(), max_pages=5, run=lambda args: "{}"
        )
        self.assertIsInstance(tracker, trackers.BoardBackedTracker)

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(ValueError):
            trackers.build_tracker(
                variant="carrier-pigeon", board=None, max_pages=5, run=lambda args: "{}"
            )

    def test_board_without_a_table_is_refused(self):
        with self.assertRaisesRegex(ValueError, "board"):
            trackers.build_tracker(variant="board", board=None, max_pages=5, run=lambda args: "{}")


REPO = "example/demo"
T1 = "2026-10-28T13:00:00Z"
SINGLE_MARKER = "BoardItemByNumber"


def backed_tracker(run, **over):
    return trackers.BoardBackedTracker(config(**over), run, pacer=FakePacer(), max_pages=5)


def single_node(project_id, number, repo=REPO, updated=T1, values=()):
    return {
        "id": f"PVTI-{number}",
        "updatedAt": updated,
        "project": {"id": project_id},
        "fieldValues": {"nodes": list(values)},
    }


def single_payload(nodes, end=None, more=False):
    return {
        "repository": {
            "issue": {
                "projectItems": {
                    "pageInfo": {"hasNextPage": more, "endCursor": end},
                    "nodes": list(nodes),
                }
            }
        }
    }


def script_item(run, number, values=(), project_id="PVT_1", count=1):
    run.script(
        SINGLE_MARKER,
        [single_payload([single_node(project_id, number, values=list(values))])] * count,
    )
    return run


def script_schema(run, project_id="PVT_1"):
    run.script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
    return run


def graphql_calls(run, marker):
    return run.graphql_variables(marker)


class BackedItemTest(unittest.TestCase):
    def test_item_merges_board_concepts_over_the_rest_issue(self):
        run = BoardFakeRun()
        script_schema(run)
        script_item(
            run,
            7,
            [
                text_value("F_agent", "Agent", "agent-1"),
                text_value("F_status", "Agent state", "working"),
                select_value("F_wf", "Status", "opt-s1", "In progress"),
                select_value("F_prio", "Priority", "opt-p0-id", "P0"),
            ],
        )
        rest_issue(run, 7, repo=REPO)
        found = backed_tracker(run).item(REPO, 7)
        self.assertEqual(found.repo, REPO)
        self.assertEqual(found.number, 7)
        self.assertEqual(found.agent, "agent-1")
        self.assertEqual(found.status, "working")
        self.assertEqual(found.extra["board_status"], "In progress")
        self.assertEqual(found.extra["priority"], "urgent")
        self.assertEqual(found.priority, "high")
        self.assertEqual(found.updated_at, iface.parse_cursor(T1))

    def test_schema_is_fetched_once_per_instance(self):
        run = BoardFakeRun()
        script_schema(run)
        script_item(run, 7, count=2)
        rest_issue(run, 7, repo=REPO)
        tracker = backed_tracker(run)
        tracker.item(REPO, 7)
        tracker.item(REPO, 7)
        tracker.board_schema()
        self.assertEqual(len(graphql_calls(run, "BoardSchemaByNumber")), 1)
        self.assertEqual(len(graphql_calls(run, SINGLE_MARKER)), 2)

    def test_reads_and_writes_share_the_one_fetch(self):
        from tests.test_board_writes import script_item as script_poll_item
        from tests.test_board_writes import script_mutation

        run = BoardFakeRun()
        script_schema(run)
        script_item(run, 7)
        script_poll_item(run, 7)
        script_mutation(run)
        rest_issue(run, 7, repo=REPO)
        tracker = backed_tracker(run)
        tracker.item(REPO, 7)
        tracker.write(REPO, 7, "agent", "agent-1")
        self.assertEqual(len(graphql_calls(run, "BoardSchemaByNumber")), 1)

    def test_item_needs_no_board_poll(self):
        run = BoardFakeRun()
        script_schema(run)
        script_item(run, 7)
        rest_issue(run, 7, repo=REPO)
        backed_tracker(run).item(REPO, 7)
        self.assertEqual(graphql_calls(run, "BoardItems"), [])

    def test_item_missing_from_the_board_is_fail_closed(self):
        run = BoardFakeRun()
        script_schema(run)
        run.script(SINGLE_MARKER, [single_payload([])])
        rest_issue(run, 7, repo=REPO)
        with self.assertRaises(iface.TrackerError):
            backed_tracker(run).item(REPO, 7)

    def test_item_with_no_live_issue_is_fail_closed(self):
        run = BoardFakeRun()
        script_schema(run)
        run.script(SINGLE_MARKER, [{"repository": {"issue": None}}])
        with self.assertRaises(iface.TrackerError):
            backed_tracker(run).item(REPO, 7)

    def test_malformed_single_page_is_fail_closed(self):
        run = BoardFakeRun()
        script_schema(run)
        run.script(SINGLE_MARKER, [{"repository": {"issue": {"projectItems": []}}}])
        rest_issue(run, 7, repo=REPO)
        with self.assertRaises(iface.TrackerError):
            backed_tracker(run).item(REPO, 7)

    def test_board_poll_pages_reports_the_cap(self):
        run = BoardFakeRun()
        self.assertEqual(backed_tracker(run).board_poll_pages, 5)

    def test_changes_reads_the_rest_feed(self):
        run = BoardFakeRun()
        run.add_rest(
            f"repos/{REPO}/issues?state=all&sort=updated&direction=asc&per_page=100",
            [],
        )
        found, _ = backed_tracker(run).changes(REPO, None)
        self.assertEqual(found, [])

    def test_write_still_sends_the_text_mutation(self):
        from tests.test_board_writes import script_item as script_poll_item
        from tests.test_board_writes import script_mutation

        run = BoardFakeRun()
        script_schema(run)
        script_poll_item(run, 7)
        script_mutation(run)
        backed_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertEqual(len(run.graphql_variables("BoardWriteText")), 1)


if __name__ == "__main__":
    unittest.main()
