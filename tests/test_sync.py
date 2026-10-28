"""The sync pass: feed the board store from the tracker. Fakes only."""

import os
import tempfile
import unittest
from typing import Any
from corral import board, sync, tracker
from tests.test_tracker import FakeTracker, reading


REPO = "acme/app"
T0 = 1_800_000_100.0


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "board.sqlite")
        self.store = board.open_store(self.path)
        self.addCleanup(self.store.close)

    def stored(self, number, **over: Any):
        params: dict[str, Any] = {"repo": REPO, "number": number}
        params.update(over)
        self.store.upsert_item(
            board.Item(
                repo=params.get("repo", REPO),
                number=params.get("number", number),
                kind=params.get("kind", "issue"),
                title=params.get("title", "Title"),
                body=params.get("body", "Body"),
                state=params.get("state", "open"),
                author=params.get("author", "alice"),
                labels=params.get("labels", []),
                priority=params.get("priority", ""),
                assignees=params.get("assignees", []),
                url=params.get("url", ""),
                tracker_updated_at=params.get("tracker_updated_at", T0 - 100),
                from_fork=params.get("from_fork", False),
            ),
            operators=["alice"],
            now=T0 - 100,
        )


class SyncRepoTest(StoreCase):
    def test_incremental_sync_applies_changed_items_and_advances_cursor(self):
        fake = FakeTracker(
            [
                reading(number=1, updated_at=T0 - 50),
                reading(number=2, updated_at=T0),
            ]
        )
        result = sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        self.assertEqual(result.items_seen, 2)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.cursor, tracker.format_cursor(T0))
        self.assertEqual(self.store.get_cursor(REPO, sync.CURSOR_NAME), result.cursor)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.title, "Title")
        row, _ = self.store.item(REPO, 2, now=T0)
        self.assertEqual(row.priority, "high")

    def test_empty_feed_changes_nothing(self):
        fake = FakeTracker()
        cursor = tracker.format_cursor(T0)
        result = sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=cursor)
        self.assertEqual(result, sync.SyncResult(0, cursor, ()))
        self.assertEqual(self.store.get_cursor(REPO, sync.CURSOR_NAME), cursor)

    def test_redelivery_is_idempotent(self):
        fake = FakeTracker([reading(number=1, updated_at=T0)])
        first = sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        second = sync.sync_repo(
            self.store, fake, REPO, operators=["alice"], now=T0 + 10, cursor=first.cursor
        )
        self.assertEqual(second.items_seen, 1)
        self.assertEqual(second.errors, ())
        self.assertEqual(second.cursor, first.cursor)
        rows = self.store.items(repo=REPO)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].confidence, board.CONFIRMED)

    def test_page_error_keeps_rows_and_cursor(self):
        self.stored(1)
        before, _ = self.store.item(REPO, 1, now=T0)
        fake = FakeTracker([reading(number=2, updated_at=T0)])
        fake.changes_error = "page failed"
        cursor = tracker.format_cursor(T0 - 100)
        result = sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=cursor)
        self.assertEqual(result.items_seen, 0)
        self.assertEqual(result.cursor, cursor)
        self.assertEqual(len(result.errors), 1)
        self.assertIsNone(self.store.get_cursor(REPO, sync.CURSOR_NAME))
        after, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual((after.title, after.confidence), (before.title, before.confidence))
        self.assertIsNone(self.store.item(REPO, 2, now=T0)[0])

    def test_priority_flows_from_tracker_reading(self):
        fake = FakeTracker([reading(number=1, priority="urgent", updated_at=T0)])
        sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.priority, "urgent")


class ExternalTest(StoreCase):
    def test_stranger_author_is_external(self):
        fake = FakeTracker([reading(number=1, author="mallory", updated_at=T0)])
        sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertTrue(row.external)

    def test_operator_author_is_not_external(self):
        fake = FakeTracker([reading(number=1, author="alice", updated_at=T0)])
        sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertFalse(row.external)

    def test_fork_pr_is_external_for_operator_author(self):
        fake = FakeTracker(
            [reading(number=9, kind="pr", author="alice", from_fork=True, updated_at=T0)]
        )
        sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        row, _ = self.store.item(REPO, 9, now=T0)
        self.assertTrue(row.external)


class ResyncTest(StoreCase):
    def test_resync_upserts_everything_and_resets_cursor(self):
        fake = FakeTracker(
            [
                reading(number=1, updated_at=T0 - 50),
                reading(number=2, updated_at=T0),
            ]
        )
        result = sync.resync_repo(self.store, fake, REPO, operators=["alice"], now=T0)
        self.assertEqual(result.items_seen, 2)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.cursor, tracker.format_cursor(T0))
        self.assertEqual(self.store.get_cursor(REPO, sync.CURSOR_NAME), result.cursor)

    def test_resync_marks_absent_items_dirty_and_keeps_rows(self):
        self.stored(1, title="Gone", tracker_updated_at=T0 - 200)
        fake = FakeTracker([reading(number=2, updated_at=T0)])
        result = sync.resync_repo(self.store, fake, REPO, operators=["alice"], now=T0)
        self.assertEqual(result.errors, ())
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.title, "Gone")
        self.assertEqual(row.confidence, board.DIRTY)
        live, _ = self.store.item(REPO, 2, now=T0)
        self.assertEqual(live.confidence, board.CONFIRMED)

    def test_resync_error_keeps_applied_and_marks_nothing_absent(self):
        self.stored(1, title="Old", tracker_updated_at=T0 - 200)
        self.stored(3, title="Later", tracker_updated_at=T0 - 200)
        fake = FakeTracker(
            [
                reading(number=2, title="New", updated_at=T0 - 50),
                reading(number=3, title="Fresh", updated_at=T0),
            ]
        )
        fake.items_break_after = 1
        result = sync.resync_repo(self.store, fake, REPO, operators=["alice"], now=T0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.cursor, tracker.format_cursor(T0 - 50))
        self.assertEqual(self.store.get_cursor(REPO, sync.CURSOR_NAME), result.cursor)
        row, _ = self.store.item(REPO, 2, now=T0)
        self.assertEqual(row.title, "New")
        for number in (1, 3):
            row, _ = self.store.item(REPO, number, now=T0)
            self.assertEqual(row.confidence, board.CONFIRMED)

    def test_resync_empty_listing_marks_everything_absent(self):
        self.stored(1)
        result = sync.resync_repo(self.store, FakeTracker(), REPO, operators=["alice"], now=T0)
        self.assertEqual(result.errors, ())
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.confidence, board.DIRTY)


class AuditTest(StoreCase):
    def test_audit_rereads_oldest_first(self):
        self.stored(1)
        self.stored(2)
        self.stored(3)
        fake = FakeTracker(
            [
                reading(number=1, title="One", updated_at=T0),
                reading(number=2, title="Two", updated_at=T0),
                reading(number=3, title="Three", updated_at=T0),
            ]
        )
        result = sync.audit_repo(self.store, fake, REPO, operators=["alice"], now=T0, limit=2)
        self.assertEqual(result.items_seen, 2)
        self.assertEqual(result.errors, ())
        self.assertEqual([call[1:] for call in fake.calls], [(REPO, 1), (REPO, 2)])
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.title, "One")

    def test_audit_isolates_a_failing_item(self):
        self.stored(1)
        self.stored(2)
        fake = FakeTracker(
            [
                reading(number=1, title="One", updated_at=T0),
                reading(number=2, title="Two", updated_at=T0),
            ]
        )
        fake.item_errors[(REPO, 1)] = "gone from tracker"
        result = sync.audit_repo(self.store, fake, REPO, operators=["alice"], now=T0, limit=10)
        self.assertEqual(result.items_seen, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn(f"{REPO}#1", result.errors[0])
        row, _ = self.store.item(REPO, 2, now=T0)
        self.assertEqual(row.title, "Two")
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.title, "Title")

    def test_audit_zero_limit_reads_nothing(self):
        self.stored(1)
        fake = FakeTracker([reading(number=1, updated_at=T0)])
        result = sync.audit_repo(self.store, fake, REPO, operators=["alice"], now=T0, limit=0)
        self.assertEqual(result, sync.SyncResult(0, None, ()))


if __name__ == "__main__":
    unittest.main()
