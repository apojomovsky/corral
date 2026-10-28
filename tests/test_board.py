"""The board store: local SQLite cache with claims as transactions. Fakes only."""

import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from typing import Any

from corral import board


def reading(repo="app", number=7, **over: Any):
    """A tracker reading with sane defaults; override any field."""
    params: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "kind": "issue",
        "title": "Title",
        "body": "Body",
        "state": "open",
        "author": "alice",
        "labels": [],
        "priority": "",
        "assignees": [],
        "url": "https://example.com/app/7",
        "tracker_updated_at": 1000.0,
        "extra": {},
    }
    params.update(over)
    return board.Item(**params)


class TempStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "board.sqlite")

    def open(self):
        store = board.open_store(self.path)
        self.addCleanup(store.close)
        return store

    def stored(self, repo="app", number=7, **over):
        store = self.open()
        store.upsert_item(reading(repo, number, **over), operators=["alice"], now=1000.0)
        return store


class IsExternalTest(unittest.TestCase):
    def test_operator_is_not_external(self):
        self.assertFalse(board.is_external("alice", ["alice", "bob"], False))

    def test_login_match_ignores_case(self):
        self.assertFalse(board.is_external("Alice", ["alice"], False))

    def test_stranger_is_external(self):
        self.assertTrue(board.is_external("mallory", ["alice"], False))

    def test_fork_is_external_even_for_operator(self):
        self.assertTrue(board.is_external("alice", ["alice"], True))

    def test_missing_author_is_external(self):
        self.assertTrue(board.is_external("", ["alice"], False))


class UpsertTest(TempStoreTest):
    def test_insert_and_read_round_trip(self):
        store = self.open()
        store.upsert_item(
            reading(labels=["ready"], priority="high", assignees=["alice"]),
            operators=["alice"],
            now=1000.0,
        )
        item, fresh = store.item("app", 7, now=1000.0)
        self.assertTrue(fresh)
        self.assertEqual(item.title, "Title")
        self.assertEqual(item.labels, ["ready"])
        self.assertEqual(item.priority, "high")
        self.assertEqual(item.assignees, ["alice"])
        self.assertEqual(item.confidence, board.CONFIRMED)
        self.assertFalse(item.external)

    def test_missing_item_reads_none_and_stale(self):
        store = self.open()
        item, fresh = store.item("app", 7, now=1000.0)
        self.assertIsNone(item)
        self.assertFalse(fresh)

    def test_people_fields_always_follow_tracker(self):
        store = self.stored()
        store.upsert_item(
            reading(
                title="Person edit",
                body="New",
                labels=["x"],
                priority="low",
                state="open",
                assignees=["bob"],
            ),
            operators=["alice"],
            now=2000.0,
        )
        item, _ = store.item("app", 7, now=2000.0)
        self.assertEqual(item.title, "Person edit")
        self.assertEqual(item.body, "New")
        self.assertEqual(item.labels, ["x"])
        self.assertEqual(item.priority, "low")
        self.assertEqual(item.assignees, ["bob"])

    def test_pending_confirmed_on_echo(self):
        store = self.stored()
        store.enqueue("app", 7, "agent", "bob-agent", now=1500.0)
        item, _ = store.item("app", 7, now=1500.0)
        self.assertEqual(item.confidence, board.PENDING)
        store.upsert_item(
            reading(agent="bob-agent", tracker_updated_at=1600.0),
            operators=["alice"],
            now=1600.0,
        )
        item, _ = store.item("app", 7, now=1600.0)
        self.assertEqual(item.confidence, board.CONFIRMED)
        self.assertEqual(store.queued_writes(), [])
        rows = store.outbox_rows(state="sent")
        self.assertEqual(len(rows), 1)

    def test_person_override_drops_queued_write(self):
        store = self.stored()
        store.enqueue("app", 7, "agent", "bob-agent", now=1500.0)
        store.upsert_item(
            reading(agent="carol-agent", tracker_updated_at=1600.0),
            operators=["alice"],
            now=1600.0,
        )
        queued = store.queued_writes()
        self.assertEqual(queued, [])
        rows = store.outbox_rows(state="dropped")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason, "overridden")
        item, _ = store.item("app", 7, now=1600.0)
        self.assertEqual(item.confidence, board.CONFIRMED)

    def test_queued_write_protected_from_stale_reading(self):
        store = self.stored()
        store.enqueue("app", 7, "agent", "bob-agent", now=1500.0)
        store.upsert_item(
            reading(agent="", tracker_updated_at=1400.0),
            operators=["alice"],
            now=1600.0,
        )
        queued = store.queued_writes()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].state, board.QUEUED)
        item, _ = store.item("app", 7, now=1600.0)
        self.assertEqual(item.confidence, board.PENDING)
        self.assertEqual(item.agent, "bob-agent")

    def test_tracker_reading_never_originates_agent(self):
        store = self.stored()
        store.upsert_item(
            reading(agent="someone", tracker_updated_at=2000.0),
            operators=["alice"],
            now=2000.0,
        )
        item, _ = store.item("app", 7, now=2000.0)
        self.assertEqual(item.agent, "")

    def test_tracker_reading_never_displaces_a_claim(self):
        store = self.stored()
        store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        for row in store.queued_writes():
            store.mark_sent(row.id)
        store.upsert_item(
            reading(agent="someone-else", tracker_updated_at=2000.0),
            operators=["alice"],
            now=2000.0,
        )
        item, _ = store.item("app", 7, now=2000.0)
        self.assertEqual(item.agent, "agent-1")

    def test_external_never_becomes_non_external(self):
        store = self.stored(author="mallory")
        item, _ = store.item("app", 7, now=1000.0)
        self.assertTrue(item.external)
        store.upsert_item(reading(author="alice"), operators=["alice"], now=2000.0)
        item, _ = store.item("app", 7, now=2000.0)
        self.assertTrue(item.external)

    def test_from_fork_is_external(self):
        store = self.stored(from_fork=True)
        item, _ = store.item("app", 7, now=1000.0)
        self.assertTrue(item.external)

    def test_extra_round_trips(self):
        store = self.open()
        extra = {"pool": "dispatch", "phase": "ready", "nested": {"a": [1, 2]}}
        store.upsert_item(reading(extra=extra), operators=["alice"], now=1000.0)
        item, _ = store.item("app", 7, now=1000.0)
        self.assertEqual(item.extra, extra)

    def test_items_filters(self):
        store = self.open()
        store.upsert_item(
            reading(number=7, state="open", labels=["ready"]), operators=["alice"], now=1000.0
        )
        store.upsert_item(
            reading(number=8, state="closed", labels=["ready"]), operators=["alice"], now=1000.0
        )
        store.upsert_item(
            reading(repo="other", number=9, state="open"), operators=["alice"], now=1000.0
        )
        self.assertEqual(len(store.items()), 3)
        self.assertEqual(len(store.items(repo="app")), 2)
        self.assertEqual(len(store.items(state="open")), 2)
        self.assertEqual(len(store.items(label="ready")), 2)
        self.assertEqual(len(store.items(repo="app", state="closed")), 1)

    def test_corrupt_stored_json_fails_closed(self):
        store = self.stored()
        raw = sqlite3.connect(self.path)
        try:
            raw.execute(
                "UPDATE items SET labels=? WHERE repo=? AND number=?",
                ("{{{broken", "app", 7),
            )
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(board.StoreError):
            store.item("app", 7, now=2000.0)


class FreshnessTest(TempStoreTest):
    def test_max_age(self):
        store = self.stored()
        _, fresh = store.item("app", 7, now=1059.0, max_age=60)
        self.assertTrue(fresh)
        _, fresh = store.item("app", 7, now=1061.0, max_age=60)
        self.assertFalse(fresh)

    def test_no_max_age_is_fresh(self):
        store = self.stored()
        _, fresh = store.item("app", 7, now=10**9)
        self.assertTrue(fresh)


class ClaimTest(TempStoreTest):
    def test_claim_win(self):
        store = self.stored()
        claim = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        self.assertEqual(claim.agent, "agent-1")
        self.assertEqual(claim.host, "host-1")
        self.assertEqual(claim.epoch, 1)
        item, _ = store.item("app", 7, now=1100.0)
        self.assertEqual(item.agent, "agent-1")
        self.assertEqual(item.confidence, board.PENDING)
        queued = store.queued_writes()
        self.assertEqual([(row.field, row.value) for row in queued], [("agent", "agent-1")])

    def test_claim_missing_item_fails_closed(self):
        store = self.open()
        with self.assertRaises(board.StoreError):
            store.claim("app", 7, "agent-1", "host-1", now=1100.0)

    def test_claim_closed_item_fails_closed(self):
        store = self.stored(state="closed")
        with self.assertRaises(board.StoreError):
            store.claim("app", 7, "agent-1", "host-1", now=1100.0)

    def test_claim_external_refused(self):
        store = self.stored(author="mallory")
        with self.assertRaises(board.ExternalItem):
            store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        store2 = self.stored(from_fork=True)
        with self.assertRaises(board.ExternalItem):
            store2.claim("app", 7, "agent-1", "host-1", now=1100.0)

    def test_second_claimant_loses(self):
        store = self.stored()
        store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        with self.assertRaises(board.Claimed) as ctx:
            store.claim("app", 7, "agent-2", "host-2", now=1101.0)
        self.assertIn("agent-1", str(ctx.exception))

    def test_reclaim_is_idempotent(self):
        store = self.stored()
        first = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        second = store.claim("app", 7, "agent-1", "host-1", now=1101.0)
        self.assertEqual(first.epoch, second.epoch)

    def test_release_and_reclaim_bumps_epoch(self):
        store = self.stored()
        first = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        store.release("app", 7, "agent-1", first.epoch, now=1200.0)
        second = store.claim("app", 7, "agent-2", "host-2", now=1300.0)
        self.assertGreater(second.epoch, first.epoch)

    def test_old_holder_release_refused(self):
        store = self.stored()
        first = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        store.release("app", 7, "agent-1", first.epoch, now=1200.0)
        store.claim("app", 7, "agent-2", "host-2", now=1300.0)
        with self.assertRaises(board.Claimed):
            store.release("app", 7, "agent-1", first.epoch, now=1400.0)

    def test_stranger_release_refused(self):
        store = self.stored()
        claim = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        with self.assertRaises(board.Claimed):
            store.release("app", 7, "agent-2", claim.epoch, now=1101.0)
        with self.assertRaises(board.Claimed):
            store.release("app", 7, "agent-1", claim.epoch + 1, now=1101.0)

    def test_release_on_external_clears_claim_and_enqueues_nothing(self):
        store = self.stored()
        claim = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        store.upsert_item(
            reading(author="mallory", tracker_updated_at=1200.0),
            operators=["alice"],
            now=1200.0,
        )
        item, _ = store.item("app", 7, now=1200.0)
        self.assertTrue(item.external)
        before = store.outbox_rows()
        store.release("app", 7, "agent-1", claim.epoch, now=1300.0)
        self.assertEqual(store.outbox_rows(), before)
        item, _ = store.item("app", 7, now=1300.0)
        self.assertEqual(item.confidence, board.CONFIRMED)
        with self.assertRaises(board.Claimed) as ctx:
            store.release("app", 7, "agent-1", claim.epoch, now=1301.0)
        self.assertIn("nobody", str(ctx.exception))

    def test_release_on_closed_clears_claim_and_enqueues_nothing(self):
        store = self.stored()
        claim = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        store.upsert_item(
            reading(state="closed", tracker_updated_at=1200.0),
            operators=["alice"],
            now=1200.0,
        )
        before = store.outbox_rows()
        store.release("app", 7, "agent-1", claim.epoch, now=1300.0)
        self.assertEqual(store.outbox_rows(), before)
        item, _ = store.item("app", 7, now=1300.0)
        self.assertEqual(item.confidence, board.CONFIRMED)

    def test_racing_claims_one_wins(self):
        store = self.stored()
        barrier = threading.Barrier(2)
        outcomes = []

        def race(agent):
            peer = board.open_store(self.path)
            try:
                barrier.wait(timeout=10)
                try:
                    claim = peer.claim("app", 7, agent, "host-1", now=1100.0)
                    outcomes.append(("won", agent, claim.epoch))
                except board.Claimed as err:
                    outcomes.append(("lost", agent, str(err)))
            finally:
                peer.close()

        threads = [threading.Thread(target=race, args=(agent,)) for agent in ("agent-1", "agent-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        wins = [o for o in outcomes if o[0] == "won"]
        losses = [o for o in outcomes if o[0] == "lost"]
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), 1)


class EnqueueTest(TempStoreTest):
    def test_enqueue_marks_pending(self):
        store = self.stored()
        row = store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        self.assertEqual(row.field, "agent")
        self.assertEqual(row.value, "agent-1")
        self.assertEqual(row.state, board.QUEUED)
        item, _ = store.item("app", 7, now=1500.0)
        self.assertEqual(item.confidence, board.PENDING)

    def test_enqueue_rejects_unknown_field(self):
        store = self.stored()
        with self.assertRaises(board.StoreError):
            store.enqueue("app", 7, "title", "New", now=1500.0)
        self.assertEqual(store.queued_writes(), [])

    def test_enqueue_refused_for_external(self):
        store = self.stored(author="mallory")
        with self.assertRaises(board.ExternalItem):
            store.enqueue("app", 7, "agent", "agent-1", now=1500.0)

    def test_enqueue_missing_item_fails_closed(self):
        store = self.open()
        with self.assertRaises(board.StoreError):
            store.enqueue("app", 7, "agent", "agent-1", now=1500.0)

    def test_mark_sent_and_dropped(self):
        store = self.stored()
        row = store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        store.mark_sent(row.id)
        self.assertEqual(store.outbox_rows(state="sent")[0].id, row.id)
        row2 = store.enqueue("app", 7, "agent", "agent-2", now=1501.0)
        store.mark_dropped(row2.id, "superseded")
        dropped = store.outbox_rows(state="dropped")
        self.assertEqual(dropped[0].reason, "superseded")

    def test_mark_dirty(self):
        store = self.stored()
        row = store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        store.mark_dirty(row.id, "push failed")
        item, _ = store.item("app", 7, now=1500.0)
        self.assertEqual(item.confidence, board.DIRTY)
        rows = store.outbox_rows(state="queued")
        self.assertEqual(rows[0].attempts, 1)
        self.assertEqual(rows[0].reason, "push failed")

    def test_queued_writes_limit(self):
        store = self.stored()
        store.enqueue("app", 7, "agent", "a", now=1500.0)
        store.enqueue("app", 7, "status", "b", now=1501.0)
        self.assertEqual(len(store.queued_writes(limit=1)), 1)
        self.assertEqual(len(store.queued_writes(limit=10)), 2)


class OpenTest(unittest.TestCase):
    def test_file_created_owner_only_in_state_dir(self):
        with tempfile.TemporaryDirectory(prefix="corral-state-") as state:
            old = os.environ.get("CORRAL_STATE")
            os.environ["CORRAL_STATE"] = state
            try:
                self.assertEqual(board.db_path(), os.path.join(state, "board.sqlite"))
                with board.open_store() as store:
                    store.upsert_item(reading(), operators=["alice"], now=1000.0)
                mode = stat.S_IMODE(os.stat(board.db_path()).st_mode)
                self.assertEqual(mode, 0o600)
            finally:
                if old is None:
                    del os.environ["CORRAL_STATE"]
                else:
                    os.environ["CORRAL_STATE"] = old

    def test_preexisting_loose_file_tightened_on_open(self):
        with tempfile.TemporaryDirectory(prefix="corral-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            with board.open_store(path):
                pass
            os.chmod(path, 0o644)
            with board.open_store(path):
                pass
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_newer_schema_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="corral-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version=999")
            conn.commit()
            conn.close()
            with self.assertRaises(board.StoreError):
                board.open_store(path)

    def test_corrupt_file_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="corral-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            with open(path, "wb") as handle:
                handle.write(b"not a database at all")
            with self.assertRaises(board.StoreError):
                board.open_store(path)

    def test_exit_codes_documented(self):
        self.assertEqual(board.Claimed.exit_code, 2)
        self.assertEqual(board.ExternalItem.exit_code, 1)
        self.assertEqual(board.StoreError.exit_code, 4)


if __name__ == "__main__":
    unittest.main()
