"""The board sync step: the `board` cursor as its own clock, the board poll,
and the audit extended to board fields. Fakes only.

The board reader is a stub: scripted ``TrackerItem`` readings or one
failure, recording the schema and page cap it was called with. The store
is a temporary file. No ``gh``, no GraphQL, no network, no real state dir.
"""

import os
import tempfile
import unittest
from typing import Any

from corral import board, sync, tracker
from corral.github.board import BoardSchema
from tests.test_tracker import FakeTracker, reading

REPO = "acme/app"
T0 = 1_800_000_100.0


def schema():
    """One resolved board schema; the poll step never inspects it."""
    return BoardSchema(project_id="PVT_1", fields={})


class FakeBoardReader:
    """A board poll double: scripted readings or one failure. Records calls."""

    def __init__(self, items=(), error=None):
        self.items = list(items)
        self.error = error
        self.calls = []

    def poll(self, found_schema, *, after=None, max_pages=5):
        self.calls.append({"schema": found_schema, "after": after, "max_pages": max_pages})
        if self.error is not None:
            raise self.error
        return list(self.items), None


class BoardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def seed(self, number, updated_at, extra=None, **over: Any):
        params: dict[str, Any] = {
            "repo": REPO,
            "number": number,
            "tracker_updated_at": updated_at,
            "extra": dict(extra or {}),
        }
        params.update(over)
        self.store.upsert_item(
            board.Item(
                repo=params["repo"],
                number=params["number"],
                kind=params.get("kind", "issue"),
                title=params.get("title", "Title"),
                body=params.get("body", "Body"),
                state=params.get("state", "open"),
                author=params.get("author", "alice"),
                labels=params.get("labels", []),
                priority=params.get("priority", ""),
                assignees=params.get("assignees", []),
                url=params.get("url", ""),
                tracker_updated_at=params["tracker_updated_at"],
                from_fork=params.get("from_fork", False),
                extra=params["extra"],
            ),
            operators=["alice"],
            now=params["tracker_updated_at"],
        )

    def board_cursor(self):
        return self.store.get_cursor(REPO, sync.BOARD_CURSOR_NAME)

    def feed_cursor(self):
        return self.store.get_cursor(REPO, sync.CURSOR_NAME)

    def poll(self, reader, **over):
        params: dict[str, Any] = {
            "max_pages": 5,
            "operators": ["alice"],
            "now": T0,
        }
        params.update(over)
        return sync.sync_board_repo(
            self.store,
            reader,
            schema(),
            REPO,
            **params,  # type: ignore[arg-type]
        )

    def stored(self, number):
        row, _ = self.store.item(REPO, number, now=T0)
        return row


class BoardPollTest(BoardCase):
    def test_poll_applies_items_and_advances_the_board_cursor(self):
        reader = FakeBoardReader(
            [
                reading(repo=REPO, number=1, updated_at=T0 - 50, extra={"pool": "a"}),
                reading(repo=REPO, number=2, updated_at=T0, extra={"pool": "b"}),
            ]
        )
        result = self.poll(reader)
        self.assertEqual(result.items_seen, 2)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.cursor, tracker.format_cursor(T0))
        self.assertEqual(self.board_cursor(), result.cursor)
        self.assertEqual(self.stored(1).extra, {"pool": "a"})
        self.assertEqual(self.stored(2).extra, {"pool": "b"})

    def test_poll_passes_the_schema_and_page_cap_to_the_reader(self):
        found = schema()
        reader = FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0)])
        sync.sync_board_repo(
            self.store, reader, found, REPO, operators=["alice"], now=T0, max_pages=2
        )
        self.assertEqual(len(reader.calls), 1)
        self.assertIs(reader.calls[0]["schema"], found)
        self.assertEqual(reader.calls[0]["max_pages"], 2)

    def test_empty_poll_keeps_the_old_board_cursor(self):
        self.store.set_cursor(REPO, sync.BOARD_CURSOR_NAME, tracker.format_cursor(T0))
        result = self.poll(FakeBoardReader([]))
        self.assertEqual(result.items_seen, 0)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.cursor, tracker.format_cursor(T0))
        self.assertEqual(self.board_cursor(), tracker.format_cursor(T0))

    def test_failed_poll_keeps_the_old_cursor_and_reports(self):
        self.seed(1, T0 - 100, {"pool": "a"})
        before = self.stored(1)
        self.store.set_cursor(REPO, sync.BOARD_CURSOR_NAME, tracker.format_cursor(T0 - 100))
        result = self.poll(FakeBoardReader(error=tracker.TrackerError("board down")))
        self.assertEqual(result.items_seen, 0)
        self.assertEqual(result.cursor, tracker.format_cursor(T0 - 100))
        self.assertEqual(len(result.errors), 1)
        self.assertIn(REPO, result.errors[0])
        self.assertEqual(self.board_cursor(), tracker.format_cursor(T0 - 100))
        after = self.stored(1)
        self.assertEqual((after.title, after.extra), (before.title, before.extra))

    def test_board_cursor_never_moves_backwards(self):
        self.seed(1, T0, {"pool": "new"})
        self.store.set_cursor(REPO, sync.BOARD_CURSOR_NAME, tracker.format_cursor(T0))
        result = self.poll(
            FakeBoardReader(
                [reading(repo=REPO, number=1, updated_at=T0 - 50, extra={"pool": "old"})]
            )
        )
        self.assertEqual(result.cursor, tracker.format_cursor(T0))
        self.assertEqual(self.board_cursor(), tracker.format_cursor(T0))
        self.assertEqual(self.stored(1).extra, {"pool": "new"})

    def test_stale_poll_reading_settles_nothing(self):
        self.seed(1, T0, {"priority": "high"})
        self.poll(
            FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0 - 50, extra={"pool": "x"})])
        )
        self.assertEqual(self.stored(1).extra, {"priority": "high"})

    def test_board_only_edit_moves_the_row_forward(self):
        self.seed(1, T0, {"priority": "high"})
        result = self.poll(
            FakeBoardReader(
                [
                    reading(
                        repo=REPO,
                        number=1,
                        title="Title",
                        updated_at=T0 + 50,
                        extra={"priority": "urgent"},
                    )
                ]
            ),
            now=T0 + 50,
        )
        row = self.stored(1)
        self.assertEqual(row.extra, {"priority": "urgent"})
        self.assertEqual(row.tracker_updated_at, T0 + 50)
        self.assertEqual(result.cursor, tracker.format_cursor(T0 + 50))
        self.assertEqual(self.board_cursor(), tracker.format_cursor(T0 + 50))

    def test_poll_merges_board_concepts_per_key(self):
        self.seed(1, T0 - 100, {"priority": "high", "pool": "a"})
        self.poll(
            FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0, extra={"pool": "b"})])
        )
        self.assertEqual(self.stored(1).extra, {"priority": "high", "pool": "b"})

    def test_only_the_cleared_encoding_removes_a_concept(self):
        self.seed(1, T0 - 100, {"priority": "high", "pool": "a"})
        self.poll(
            FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0, extra={"priority": None})])
        )
        self.assertEqual(self.stored(1).extra, {"pool": "a"})

    def test_empty_list_clears_a_list_concept_through_the_poll(self):
        self.seed(1, T0 - 100, {"blocked_by": ["acme/app#2"], "pool": "a"})
        self.poll(
            FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0, extra={"blocked_by": []})])
        )
        self.assertEqual(self.stored(1).extra, {"pool": "a"})


class CursorPairTest(BoardCase):
    def test_cursors_advance_independently(self):
        feed = FakeTracker([reading(repo=REPO, number=1, updated_at=T0 - 100)])
        sync.sync_repo(self.store, feed, REPO, operators=["alice"], now=T0 - 100, cursor=None)
        feed_cursor = self.feed_cursor()
        self.assertIsNotNone(feed_cursor)
        self.poll(
            FakeBoardReader(
                [reading(repo=REPO, number=2, updated_at=T0 - 50, extra={"pool": "a"})]
            ),
            now=T0 - 50,
        )
        board_cursor = self.board_cursor()
        self.assertIsNotNone(board_cursor)
        feed2 = FakeTracker([reading(repo=REPO, number=3, updated_at=T0)])
        sync.sync_repo(self.store, feed2, REPO, operators=["alice"], now=T0, cursor=feed_cursor)
        self.assertEqual(self.board_cursor(), board_cursor)
        self.assertNotEqual(self.feed_cursor(), feed_cursor)
        self.assertEqual(self.feed_cursor(), tracker.format_cursor(T0))

    def test_board_step_leaves_the_feed_cursor_alone(self):
        self.store.set_cursor(REPO, sync.CURSOR_NAME, tracker.format_cursor(T0))
        self.poll(FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0)]))
        self.assertEqual(self.feed_cursor(), tracker.format_cursor(T0))

    def test_board_cursor_is_never_compared_with_the_feed_cursor(self):
        self.store.set_cursor(REPO, sync.CURSOR_NAME, tracker.format_cursor(T0 + 500))
        result = self.poll(
            FakeBoardReader([reading(repo=REPO, number=1, updated_at=T0, extra={"pool": "a"})])
        )
        self.assertEqual(result.cursor, tracker.format_cursor(T0))
        self.assertEqual(self.board_cursor(), tracker.format_cursor(T0))

    def test_feed_audit_and_resync_never_touch_the_board_cursor(self):
        self.seed(1, T0 - 100, {"pool": "a"})
        self.store.set_cursor(REPO, sync.BOARD_CURSOR_NAME, tracker.format_cursor(T0 - 100))
        feed = FakeTracker([reading(repo=REPO, number=1, updated_at=T0)])
        sync.sync_repo(
            self.store,
            feed,
            REPO,
            operators=["alice"],
            now=T0,
            cursor=self.feed_cursor(),
        )
        sync.audit_repo(self.store, feed, REPO, operators=["alice"], now=T0, limit=10)
        sync.resync_repo(self.store, feed, REPO, operators=["alice"], now=T0)
        self.assertEqual(self.board_cursor(), tracker.format_cursor(T0 - 100))


class ExtendedAuditTest(BoardCase):
    def audit(self, *items, **over):
        params: dict[str, Any] = {"operators": ["alice"], "now": T0, "limit": 10}
        params.update(over)
        return sync.audit_repo(
            self.store,
            FakeTracker(list(items)),
            REPO,
            **params,  # type: ignore[arg-type]
        )

    def test_audit_detects_a_board_only_edit(self):
        self.seed(1, T0, {"priority": "high"})
        result = self.audit(
            reading(
                repo=REPO, number=1, title="Title", updated_at=T0 + 30, extra={"priority": "urgent"}
            ),
            now=T0 + 30,
        )
        self.assertEqual(result.items_seen, 1)
        row = self.stored(1)
        self.assertEqual(row.extra, {"priority": "urgent"})
        self.assertEqual(row.tracker_updated_at, T0 + 30)

    def test_audit_merges_board_concepts_per_key(self):
        self.seed(1, T0 - 100, {"priority": "high", "pool": "a"})
        self.audit(reading(repo=REPO, number=1, updated_at=T0, extra={"pool": "b"}))
        self.assertEqual(self.stored(1).extra, {"priority": "high", "pool": "b"})

    def test_audit_applies_the_cleared_encoding(self):
        self.seed(1, T0 - 100, {"priority": "high", "pool": "a"})
        self.audit(reading(repo=REPO, number=1, updated_at=T0, extra={"priority": None}))
        self.assertEqual(self.stored(1).extra, {"pool": "a"})

    def test_audit_leaves_the_board_cursor_alone(self):
        self.seed(1, T0 - 100, {"pool": "a"})
        self.store.set_cursor(REPO, sync.BOARD_CURSOR_NAME, tracker.format_cursor(T0 - 100))
        self.audit(reading(repo=REPO, number=1, updated_at=T0, extra={"pool": "b"}))
        self.assertEqual(self.board_cursor(), tracker.format_cursor(T0 - 100))


if __name__ == "__main__":
    unittest.main()
