"""Board mirror writes: concept-to-field translation in the adapter's write.

Fakes only: ``BoardFakeRun`` answers GraphQL pages at the injected ``run``
seam, ``FakePacer`` stands in for the shared bucket, and the store is a
temporary file. No ``gh``, GraphQL, network, or real state dir.
"""

import os
import tempfile
import unittest
from typing import Any

from corral import board, mirror, tracker
from corral.github import board as boardmod
from corral.github import gh
from tests.test_board_fields import FakePacer
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    item_node,
    items_payload,
    rest_issue,
    schema_payload,
    select_field,
    text_value,
)
from tests.test_tracker import FakeTracker

REPO = "example/demo"
T0 = 1_800_000_100.0

WRITE_MARKER = "BoardWriteText"
CLEAR_MARKER = "BoardClearField"


def write_tracker(run, **over):
    """A board write adapter over a fake run and a fake pacer."""
    return boardmod.BoardTracker(config(**over), run, pacer=FakePacer())


def script_schema(run, *fields, **over):
    """One live schema answering with the given fields."""
    run.script("BoardSchemaByNumber", [schema_payload(*fields, **over)])
    return run


def script_item(run, number, values=(), repo=REPO, updated="2026-10-28T13:00:00Z"):
    """One board items page holding a single item with the given values."""
    run.script("BoardItems", [items_payload([item_node(number, repo=repo, values=list(values))])])
    return run


def script_mutation(run, marker=WRITE_MARKER, payload=None):
    """One mutation answer under its operation-name marker."""
    run.script(
        marker, [payload or {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI-7"}}}]
    )
    return run


def mutations(run, marker=WRITE_MARKER):
    """The GraphQL variables of every mutation call under ``marker``."""
    return run.graphql_variables(marker)


def rest_paths(run):
    """Every non-GraphQL ``gh`` call the adapter made (label writes use these)."""
    return [args for args, _ in run.calls if args[1] != "graphql"]


class BoardWriteTest(unittest.TestCase):
    def test_agent_write_sends_one_text_mutation(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7)
        script_mutation(run)
        write_tracker(run).write(REPO, 7, "agent", "agent-1")
        found = mutations(run)
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0],
            {"project": "PVT_1", "item": "PVTI_Issue-7", "field": "F_agent", "text": "agent-1"},
        )
        self.assertEqual(rest_paths(run), [])

    def test_status_write_uses_the_status_field(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7)
        script_mutation(run)
        write_tracker(run).write(REPO, 7, "status", "working")
        found = mutations(run)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["field"], "F_status")
        self.assertEqual(found[0]["text"], "working")
        self.assertEqual(rest_paths(run), [])

    def test_clear_sends_the_clear_mutation(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7, [text_value("F_agent", "Agent", "agent-1")])
        run.script(
            CLEAR_MARKER, [{"clearProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI-7"}}}]
        )
        write_tracker(run).write(REPO, 7, "agent", "")
        self.assertEqual(mutations(run, WRITE_MARKER), [])
        cleared = mutations(run, CLEAR_MARKER)
        self.assertEqual(len(cleared), 1)
        self.assertEqual(
            cleared[0], {"project": "PVT_1", "item": "PVTI_Issue-7", "field": "F_agent"}
        )
        self.assertEqual(rest_paths(run), [])

    def test_repeat_write_sends_no_mutation(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7, [text_value("F_agent", "Agent", "agent-1")])
        write_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertEqual(mutations(run), [])
        self.assertEqual(mutations(run, CLEAR_MARKER), [])

    def test_clear_when_empty_sends_no_mutation(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7)
        write_tracker(run).write(REPO, 7, "agent", "")
        self.assertEqual(mutations(run), [])
        self.assertEqual(mutations(run, CLEAR_MARKER), [])

    def test_written_value_round_trips_through_the_reader(self):
        run = BoardFakeRun()
        fields = full_schema_fields()
        run.script("BoardSchemaByNumber", [schema_payload(*fields), schema_payload(*fields)])
        script_item(run, 7)
        script_mutation(run)
        write_tracker(run).write(REPO, 7, "agent", "agent-1")
        script_item(run, 7, [text_value("F_agent", "Agent", "agent-1")])
        rest_issue(run, 7)
        reader = boardmod.BoardReader(config(), run, pacer=FakePacer())
        found, _ = reader.poll(reader.schema(), max_pages=5)
        self.assertEqual(found[0].agent, "agent-1")

    def test_people_owned_concepts_have_no_write_path(self):
        run = BoardFakeRun()
        adapter = write_tracker(run)
        for field in ("priority", "board_status", "area", "pool", "blocked_by", "title"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    adapter.write(REPO, 7, field, "x")
        self.assertEqual(run.calls, [])

    def test_unmapped_concept_is_unknown_option(self):
        run = BoardFakeRun()
        adapter = write_tracker(run, agent_field="")
        with self.assertRaises(tracker.UnknownOption):
            adapter.write(REPO, 7, "agent", "agent-1")
        self.assertEqual(run.calls, [])

    def test_missing_live_field_is_unknown_option(self):
        run = BoardFakeRun()
        fields = [f for f in full_schema_fields() if f["name"] != "Agent"]
        script_schema(run, *fields)
        with self.assertRaises(tracker.UnknownOption) as caught:
            write_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertIn("Agent", str(caught.exception))
        self.assertEqual(mutations(run), [])

    def test_unknown_option_is_a_tracker_error(self):
        self.assertTrue(issubclass(tracker.UnknownOption, tracker.TrackerError))
        self.assertEqual(tracker.UnknownOption("gone").exit_code, 3)

    def test_wrong_kind_field_fails_closed_without_dropping(self):
        run = BoardFakeRun()
        fields = [
            select_field("F_agent", "Agent", [("o1", "alice")]) if f["name"] == "Agent" else f
            for f in full_schema_fields()
        ]
        script_schema(run, *fields)
        with self.assertRaises(tracker.TrackerError) as caught:
            write_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertNotIsInstance(caught.exception, tracker.UnknownOption)
        self.assertEqual(mutations(run), [])

    def test_unknown_board_item_fails_closed(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 8)
        with self.assertRaises(tracker.TrackerError):
            write_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertEqual(mutations(run), [])

    def test_malformed_items_page_fails_closed(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        run.script("BoardItems", [{"node": {"items": {"nodes": "nope"}}}])
        with self.assertRaises(tracker.TrackerError):
            write_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertEqual(mutations(run), [])

    def test_option_rename_by_id_is_free_on_the_write_path(self):
        run = BoardFakeRun()
        fields = [
            select_field("F_prio", "Priority", [("opt-p0-id", "Highest"), ("opt-p1-id", "High")])
            if f["name"] == "Priority"
            else f
            for f in full_schema_fields()
        ]
        script_schema(run, *fields)
        script_item(run, 7)
        script_mutation(run)
        write_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertEqual(len(mutations(run)), 1)

    def test_rate_limit_text_maps_to_tracker_rate_limited(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7)
        pacer = FakePacer()
        run.script(
            WRITE_MARKER, [gh.GhError("unavailable", "API rate limit exceeded, retry after 45")]
        )
        adapter = boardmod.BoardTracker(config(), run, pacer=pacer)
        with self.assertRaises(tracker.TrackerRateLimited) as caught:
            adapter.write(REPO, 7, "agent", "agent-1")
        self.assertEqual(caught.exception.retry_after, 45.0)
        self.assertEqual(len(pacer.cooldowns), 1)

    def test_rate_limited_error_payload_maps_to_tracker_rate_limited(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7)
        run.script(
            WRITE_MARKER,
            [{"errors": [{"type": "RATE_LIMITED", "message": "slow down"}], "data": None}],
        )
        with self.assertRaises(tracker.TrackerRateLimited):
            write_tracker(run).write(REPO, 7, "agent", "agent-1")

    def test_run_failure_is_a_tracker_error(self):
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7)
        run.script(WRITE_MARKER, [gh.GhError("other", "nope")])
        with self.assertRaises(tracker.TrackerError) as caught:
            write_tracker(run).write(REPO, 7, "agent", "agent-1")
        self.assertNotIsInstance(caught.exception, tracker.TrackerRateLimited)
        self.assertEqual(caught.exception.exit_code, 3)


class FieldWriteTracker(FakeTracker):
    """A tracker double failing per (repo, number, field) with scripted errors."""

    def __init__(self, items=(), errors=None):
        super().__init__(items)
        self.field_errors = dict(errors or {})

    def write(self, repo, number, field, value):
        self.calls.append(("write", repo, number, field, value))
        err = self.field_errors.get((repo, number, field))
        if err is not None:
            raise err


def stored(path, number=7, **over: Any):
    """A board store holding one operator-authored item."""
    params: dict[str, Any] = {
        "repo": REPO,
        "number": number,
        "kind": "issue",
        "title": "Title",
        "body": "Body",
        "state": "open",
        "author": "alice",
        "labels": [],
        "priority": "",
        "assignees": [],
        "url": "https://example.com/demo/7",
        "tracker_updated_at": T0 - 100,
        "extra": {},
    }
    params.update(over)
    store = board.open_store(path)
    store.upsert_item(board.Item(**params), operators=["alice"], now=T0 - 100)
    return store


class BoardMirrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-writes-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "board.sqlite")

    def open(self, number=7, **over):
        store = stored(self.path, number=number, **over)
        self.addCleanup(store.close)
        return store

    def seed(self, store, number, **over: Any):
        params: dict[str, Any] = {
            "repo": REPO,
            "number": number,
            "title": "Title",
            "state": "open",
            "author": "alice",
        }
        params.update(over)
        store.upsert_item(board.Item(**params), operators=["alice"], now=T0 - 100)

    def test_unknown_option_drops_with_reason_and_an_error_line(self):
        store = self.open()
        store.enqueue(REPO, 7, "agent", "agent-1", now=T0 - 50)
        fake = FieldWriteTracker(
            errors={(REPO, 7, "agent"): tracker.UnknownOption("field is gone")}
        )
        result = mirror.push(store, fake, operators=["alice"], now=T0)
        self.assertEqual((result.sent, result.dropped, result.dirty), (0, 1, 0))
        self.assertEqual(len(result.errors), 1)
        self.assertIn(f"{REPO}#7", result.errors[0])
        rows = store.outbox_rows(state="dropped")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason, "unknown-option")
        self.assertEqual(store.queued_writes(), [])

    def test_unknown_option_for_one_field_keeps_the_other(self):
        store = self.open()
        store.enqueue(REPO, 7, "agent", "agent-1", now=T0 - 50)
        store.enqueue(REPO, 7, "status", "working", now=T0 - 40)
        fake = FieldWriteTracker(
            errors={(REPO, 7, "agent"): tracker.UnknownOption("field is gone")}
        )
        result = mirror.push(store, fake, operators=["alice"], now=T0)
        self.assertEqual((result.sent, result.dropped, result.dirty), (1, 1, 0))
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(
            fake.calls,
            [("write", REPO, 7, "agent", "agent-1"), ("write", REPO, 7, "status", "working")],
        )

    def test_unknown_option_does_not_block_other_items(self):
        store = self.open(number=7)
        self.seed(store, 8)
        store.enqueue(REPO, 7, "agent", "agent-1", now=T0 - 50)
        store.enqueue(REPO, 8, "agent", "agent-2", now=T0 - 50)
        fake = FieldWriteTracker(errors={(REPO, 7, "agent"): tracker.UnknownOption("gone")})
        result = mirror.push(store, fake, operators=["alice"], now=T0)
        self.assertEqual((result.sent, result.dropped), (1, 1))
        self.assertEqual(len(result.errors), 1)

    def test_board_rate_limit_halts_the_drain_with_rows_queued(self):
        store = self.open(number=7)
        self.seed(store, 8)
        store.enqueue(REPO, 7, "agent", "agent-1", now=T0 - 50)
        store.enqueue(REPO, 8, "agent", "agent-2", now=T0 - 40)
        run = BoardFakeRun()
        script_schema(run, *full_schema_fields())
        script_item(run, 7)
        run.script(
            WRITE_MARKER, [gh.GhError("unavailable", "API rate limit exceeded, retry after 45")]
        )
        adapter = boardmod.BoardTracker(config(), run, pacer=FakePacer())
        # BoardTracker covers the write side only; reads arrive in a later
        # ticket, so the write-only use below needs the narrow carve-out.
        result = mirror.push(store, adapter, operators=["alice"], now=T0)  # ty: ignore[invalid-argument-type]
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.dirty, 1)
        self.assertEqual(result.stopped, 45.0)
        self.assertEqual(len(result.errors), 1)
        queued = {row.number: row for row in store.queued_writes()}
        self.assertEqual(set(queued), {7, 8})
        self.assertEqual(queued[7].attempts, 1)
        self.assertEqual(queued[8].attempts, 0)

    def test_plain_tracker_errors_still_stay_queued(self):
        store = self.open()
        store.enqueue(REPO, 7, "agent", "agent-1", now=T0 - 50)
        fake = FieldWriteTracker(errors={(REPO, 7, "agent"): tracker.TrackerError("down")})
        result = mirror.push(store, fake, operators=["alice"], now=T0)
        self.assertEqual((result.sent, result.dropped, result.dirty), (0, 0, 1))
        self.assertEqual(len(store.queued_writes()), 1)


class BoardSettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-settle-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=1,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=T0 - 100,
                extra={"priority": "high"},
            ),
            operators=["alice"],
            now=T0 - 100,
        )

    def test_fresher_person_edit_drops_the_queued_write(self):
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=1,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=T0,
                agent="agent-9",
                extra={"priority": "high"},
            ),
            operators=["alice"],
            now=T0,
        )
        self.assertEqual(self.store.queued_writes(), [])
        rows = self.store.outbox_rows(state="dropped")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason, "overridden")

    def test_newer_queue_survives_a_person_edit(self):
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=1,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=T0,
                agent="agent-9",
                extra={"priority": "high"},
            ),
            operators=["alice"],
            now=T0,
        )
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 + 50)
        queued = self.store.queued_writes()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].state, board.QUEUED)

    def test_echoed_board_agent_confirms_the_queued_write(self):
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=1,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=T0,
                agent="agent-1",
                extra={"priority": "high"},
            ),
            operators=["alice"],
            now=T0,
        )
        self.assertEqual(self.store.queued_writes(), [])
        self.assertEqual(len(self.store.outbox_rows(state="sent")), 1)


if __name__ == "__main__":
    unittest.main()
