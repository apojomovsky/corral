"""The board store: local SQLite cache with claims as transactions. Fakes only."""

import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest import mock
from typing import Any

from korapy import board, boardstatus


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
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-")
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

    def test_wrong_typed_stored_json_fails_closed(self):
        for column, value in (("labels", '"x"'), ("assignees", "7"), ("extra", "[]")):
            with self.subTest(column=column):
                store = self.stored()
                raw = sqlite3.connect(self.path)
                try:
                    raw.execute(
                        f"UPDATE items SET {column}=? WHERE repo=? AND number=?",
                        (value, "app", 7),
                    )
                    raw.commit()
                finally:
                    raw.close()
                with self.assertRaises(board.StoreError):
                    store.item("app", 7, now=2000.0)

    def test_a_stale_reading_changes_no_people_field(self):
        store = self.stored(title="Newer", labels=["ready"], tracker_updated_at=2000.0)
        store.upsert_item(
            reading(title="Older", labels=["other"], tracker_updated_at=1500.0),
            operators=["alice"],
            now=3000.0,
        )
        item, _ = store.item("app", 7, now=3000.0)
        self.assertEqual((item.title, item.labels), ("Newer", ["ready"]))
        self.assertEqual(item.fetched_at, 3000.0)

    def test_open_fails_closed_without_wal(self):
        real = board.sqlite3.connect

        class NoWal:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.startswith("PRAGMA journal_mode"):
                    return self._conn.execute("SELECT 'delete'")
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        with mock.patch.object(board.sqlite3, "connect", lambda *a, **k: NoWal(real(*a, **k))):
            with self.assertRaises(board.StoreError):
                board.open_store(os.path.join(self.tmp.name, "nowal.sqlite"))


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

    def test_claim_with_an_adopted_epoch_skips_minting(self):
        store = self.stored()
        claim = store.claim("app", 7, "agent-1", "host-1", now=1100.0, epoch=7)
        self.assertEqual((claim.agent, claim.epoch), ("agent-1", 7))
        item, _ = store.item("app", 7, now=1100.0)
        self.assertEqual(item.confidence, board.PENDING)
        queued = store.queued_writes()
        self.assertEqual([(row.field, row.value) for row in queued], [("agent", "agent-1")])

    def test_reclaim_with_an_adopted_epoch_updates_the_row(self):
        store = self.stored()
        first = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        second = store.claim("app", 7, "agent-1", "host-1", now=1200.0, epoch=first.epoch + 1)
        self.assertEqual(second.epoch, first.epoch + 1)
        found = store.claim_of("app", 7)
        assert found is not None
        self.assertEqual(found.epoch, first.epoch + 1)

    def test_an_adopted_epoch_must_be_a_positive_integer(self):
        store = self.stored()
        for bad in (0, -1, True, "3"):
            with self.subTest(bad=bad):
                with self.assertRaises(board.StoreError):
                    store.claim("app", 7, "agent-1", "host-1", now=1100.0, epoch=bad)

    def test_an_adopted_epoch_for_a_stranger_still_loses(self):
        store = self.stored()
        store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        with self.assertRaises(board.Claimed):
            store.claim("app", 7, "agent-2", "host-2", now=1101.0, epoch=9)

    def test_release_matches_the_adopted_epoch(self):
        store = self.stored()
        claim = store.claim("app", 7, "agent-1", "host-1", now=1100.0, epoch=7)
        with self.assertRaises(board.Claimed):
            store.release("app", 7, "agent-1", claim.epoch + 1, now=1101.0)
        store.release("app", 7, "agent-1", claim.epoch, now=1102.0)
        self.assertIsNone(store.claim_of("app", 7))

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
        # The claim write stays queued until the mirror pass drops it as
        # external: an empty tracker reading never settles a queue.
        item, _ = store.item("app", 7, now=1300.0)
        self.assertEqual(item.confidence, board.PENDING)
        self.assertEqual(len(store.queued_writes()), 1)
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
        # The claim write stays queued until the mirror pass drops it as
        # closed: an empty tracker reading never settles a queue.
        item, _ = store.item("app", 7, now=1300.0)
        self.assertEqual(item.confidence, board.PENDING)
        self.assertEqual(len(store.queued_writes()), 1)

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


class AdoptClaimTest(TempStoreTest):
    def test_adopt_stores_tracker_agent_with_unknown_host(self):
        store = self.stored()
        self.assertTrue(store.adopt_claim("app", 7, "agent-1", epoch=1800000100, now=1800000100.0))
        item, _ = store.item("app", 7, now=1800000100.0)
        self.assertEqual(item.agent, "agent-1")
        self.assertEqual(item.confidence, board.CONFIRMED)

    def test_adopt_uses_given_epoch_and_enqueues_nothing(self):
        store = self.stored()
        store.adopt_claim("app", 7, "agent-1", epoch=1800000100, now=1800000100.0)
        before = store.outbox_rows()
        self.assertEqual(before, [])
        claim = store.claim("app", 7, "agent-1", "host-1", now=1800000101.0)
        self.assertEqual(claim.epoch, 1800000100)
        self.assertEqual(claim.host, "")

    def test_adopt_refused_when_claim_row_exists(self):
        store = self.stored()
        store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        self.assertFalse(store.adopt_claim("app", 7, "agent-2", epoch=1800000100, now=1800000100.0))
        item, _ = store.item("app", 7, now=1800000100.0)
        self.assertEqual(item.agent, "agent-1")

    def test_adopt_refused_after_release_clears_holder(self):
        store = self.stored()
        claim = store.claim("app", 7, "agent-1", "host-1", now=1100.0)
        store.release("app", 7, "agent-1", claim.epoch, now=1200.0)
        self.assertFalse(store.adopt_claim("app", 7, "agent-2", epoch=1800000100, now=1800000100.0))

    def test_adopt_refused_for_external_closed_and_unknown(self):
        store = self.stored(author="mallory")
        self.assertFalse(store.adopt_claim("app", 7, "agent-1", epoch=1800000100, now=1800000100.0))
        closed = self.stored(state="closed")
        self.assertFalse(
            closed.adopt_claim("app", 7, "agent-1", epoch=1800000100, now=1800000100.0)
        )
        fresh = self.open()
        self.assertFalse(fresh.adopt_claim("app", 7, "agent-1", epoch=1800000100, now=1800000100.0))

    def test_adopt_refused_while_agent_write_queued(self):
        store = self.stored()
        store.enqueue("app", 7, "agent", "agent-9", now=1500.0)
        self.assertFalse(store.adopt_claim("app", 7, "agent-1", epoch=1800000100, now=1800000100.0))

    def test_adopt_refused_while_agent_write_sent(self):
        store = self.stored()
        row = store.enqueue("app", 7, "agent", "agent-9", now=1500.0)
        store.mark_sent(row.id)
        self.assertFalse(store.adopt_claim("app", 7, "agent-1", epoch=1800000100, now=1800000100.0))


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

    def test_enqueue_refuses_decision_writes(self):
        store = self.stored()
        self.assertEqual(board.AGENT_FIELDS, ("agent", "status"))
        with self.assertRaises(board.StoreError):
            store.enqueue("app", 7, "comment", "hello", now=1500.0)
        with self.assertRaises(board.StoreError):
            store.enqueue("app", 7, "needs-decision", "", now=1500.0)
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
        with tempfile.TemporaryDirectory(prefix="korapy-state-") as state:
            old = os.environ.get("KORAPY_STATE")
            os.environ["KORAPY_STATE"] = state
            try:
                self.assertEqual(board.db_path(), os.path.join(state, "board.sqlite"))
                with board.open_store() as store:
                    store.upsert_item(reading(), operators=["alice"], now=1000.0)
                mode = stat.S_IMODE(os.stat(board.db_path()).st_mode)
                self.assertEqual(mode, 0o600)
            finally:
                if old is None:
                    del os.environ["KORAPY_STATE"]
                else:
                    os.environ["KORAPY_STATE"] = old

    def test_preexisting_loose_file_tightened_on_open(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            with board.open_store(path):
                pass
            os.chmod(path, 0o644)
            with board.open_store(path):
                pass
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_newer_schema_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version=999")
            conn.commit()
            conn.close()
            with self.assertRaises(board.StoreError):
                board.open_store(path)

    def test_corrupt_file_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            with open(path, "wb") as handle:
                handle.write(b"not a database at all")
            with self.assertRaises(board.StoreError):
                board.open_store(path)

    def test_exit_codes_documented(self):
        self.assertEqual(board.Claimed.exit_code, 2)
        self.assertEqual(board.ExternalItem.exit_code, 1)
        self.assertEqual(board.StoreError.exit_code, 4)

    def test_version_1_migrates_forward_keeping_rows(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE items(
                    repo TEXT NOT NULL, number INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'issue',
                    title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'open',
                    author TEXT NOT NULL DEFAULT '',
                    labels TEXT NOT NULL DEFAULT '[]',
                    priority TEXT NOT NULL DEFAULT '',
                    assignees TEXT NOT NULL DEFAULT '[]',
                    url TEXT NOT NULL DEFAULT '',
                    tracker_updated_at REAL NOT NULL DEFAULT 0,
                    fetched_at REAL NOT NULL DEFAULT 0,
                    confidence TEXT NOT NULL DEFAULT 'confirmed',
                    external INTEGER NOT NULL DEFAULT 0,
                    extra TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(repo, number));
                CREATE TABLE claims(
                    repo TEXT NOT NULL, number INTEGER NOT NULL,
                    agent TEXT NOT NULL, host TEXT NOT NULL,
                    epoch INTEGER NOT NULL, claimed_at REAL NOT NULL,
                    PRIMARY KEY(repo, number));
                CREATE TABLE outbox(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL, number INTEGER NOT NULL,
                    field TEXT NOT NULL, value TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'queued',
                    reason TEXT NOT NULL DEFAULT '');
                """
            )
            conn.execute(
                "INSERT INTO items(repo, number, title) VALUES(?, ?, ?)",
                ("app", 7, "Kept"),
            )
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            conn.close()
            with board.open_store(path) as store:
                self.assertEqual(board.SCHEMA_VERSION, 4)
                row, _ = store.item("app", 7, now=1000.0)
                self.assertEqual(row.title, "Kept")
                self.assertIsNone(store.get_cursor("app", "tracker"))
                store.set_cursor("app", "tracker", "cursor-1")
                self.assertEqual(store.get_cursor("app", "tracker"), "cursor-1")
                store.set_cursor("app", "tracker", "cursor-2")
                self.assertEqual(store.get_cursor("app", "tracker"), "cursor-2")
                self.assertIsNone(store.get_cursor("other", "tracker"))

    def test_a_migrated_cursors_table_matches_a_fresh_one(self):
        def cursors_sql(path):
            conn = sqlite3.connect(path)
            try:
                row = conn.execute("SELECT sql FROM sqlite_master WHERE name='cursors'").fetchone()
            finally:
                conn.close()
            return row[0] if row else None

        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            fresh = os.path.join(tmp, "fresh.sqlite")
            board.open_store(fresh).close()
            migrated = os.path.join(tmp, "migrated.sqlite")
            board.open_store(migrated).close()
            conn = sqlite3.connect(migrated)
            conn.execute("DROP TABLE cursors")
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            conn.close()
            board.open_store(migrated).close()
            self.assertIsNotNone(cursors_sql(fresh))
            self.assertEqual(cursors_sql(fresh), cursors_sql(migrated))

    def test_unknown_version_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version=5")
            conn.commit()
            conn.close()
            with self.assertRaises(board.StoreError):
                board.open_store(path)

    def test_each_migration_step_stamps_its_own_version(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            conn = sqlite3.connect(path)
            try:
                board._migrate_1_to_2(conn)
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, 2)
                board._migrate_2_to_3(conn)
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, 3)
                board._migrate_3_to_4(conn)
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, board.SCHEMA_VERSION)
            finally:
                conn.close()

    def test_version_2_migrates_forward_adding_status_sets(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE items(repo TEXT NOT NULL, number INTEGER NOT NULL,"
                " title TEXT NOT NULL DEFAULT '',"
                " PRIMARY KEY(repo, number))"
            )
            conn.execute("PRAGMA user_version=2")
            conn.commit()
            conn.close()
            with board.open_store(path) as store:
                store.record_status_set("app", 7, "opt-c", "claimed", now=1000.0)
                self.assertEqual(
                    store.status_set_of("app", 7),
                    boardstatus.StatusRecord(option_id="opt-c", derived="claimed"),
                )

    def test_absent_mark_is_dirty_without_deleting(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            with board.open_store(path) as store:
                store.upsert_item(reading(title="Gone"), operators=["alice"], now=1000.0)
                store.mark_absent("app", 7, now=2000.0)
                row, _ = store.item("app", 7, now=2000.0)
                self.assertEqual(row.title, "Gone")
                self.assertEqual(row.confidence, board.DIRTY)
                with self.assertRaises(board.StoreError):
                    store.mark_absent("app", 404, now=2000.0)

    def test_version_3_migrates_forward_adding_drift(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as tmp:
            path = os.path.join(tmp, "board.sqlite")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE items(repo TEXT NOT NULL, number INTEGER NOT NULL,"
                " PRIMARY KEY(repo, number))"
            )
            conn.execute("PRAGMA user_version=3")
            conn.commit()
            conn.close()
            with board.open_store(path) as store:
                store.note_drift("app", 7, now=1000.0)
                self.assertEqual(store.drift_flags(), [("app", 7, 1000.0)])


class DriftFlagsTest(TempStoreTest):
    def test_no_flags_on_a_fresh_store(self):
        self.assertEqual(self.open().drift_flags(), [])

    def test_first_sighting_wins(self):
        store = self.open()
        store.note_drift("acme/app", 7, now=1000.0)
        store.note_drift("acme/app", 7, now=2000.0)
        self.assertEqual(store.drift_flags(), [("acme/app", 7, 1000.0)])

    def test_flags_order_by_repo_and_number(self):
        store = self.open()
        store.note_drift("acme/app", 9, now=1000.0)
        store.note_drift("acme/api", 2, now=1000.0)
        store.note_drift("acme/app", 7, now=1000.0)
        self.assertEqual(
            store.drift_flags(),
            [("acme/api", 2, 1000.0), ("acme/app", 7, 1000.0), ("acme/app", 9, 1000.0)],
        )

    def test_clear_drift_drops_one_flag(self):
        store = self.open()
        store.note_drift("acme/app", 7, now=1000.0)
        store.note_drift("acme/app", 9, now=1000.0)
        store.clear_drift("acme/app", 7)
        store.clear_drift("acme/app", 404)
        self.assertEqual(store.drift_flags(), [("acme/app", 9, 1000.0)])


class ReposTest(TempStoreTest):
    def test_repos_lists_every_repo_holding_rows_once(self):
        store = self.open()
        store.upsert_item(reading("acme/app", 1), operators=["alice"], now=1000.0)
        store.upsert_item(reading("acme/api", 2), operators=["alice"], now=1000.0)
        store.upsert_item(reading("acme/app", 3), operators=["alice"], now=1000.0)
        self.assertEqual(store.repos(), ["acme/api", "acme/app"])

    def test_repos_lists_no_row_json_so_a_corrupt_row_cannot_break_it(self):
        store = self.open()
        store.upsert_item(reading("acme/app", 1), operators=["alice"], now=1000.0)
        store.upsert_item(reading("acme/api", 2), operators=["alice"], now=1000.0)
        raw = sqlite3.connect(self.path)
        try:
            raw.execute(
                "UPDATE items SET extra=? WHERE repo=? AND number=?",
                ("{{{broken", "acme/app", 1),
            )
            raw.commit()
        finally:
            raw.close()
        self.assertEqual(store.repos(), ["acme/api", "acme/app"])


class PullRequestRowTest(TempStoreTest):
    """A pull request is never a ticket row: readings are dropped and
    legacy rows stay hidden from every reader."""

    def seed_legacy_pr(self, store, number=9, **over):
        """One pre-fix row for a pull request, straight into the table.

        The public upsert can no longer write one, so the legacy shape
        a past sync stored goes in as raw SQL.
        """
        values = {
            "repo": "app",
            "number": number,
            "kind": "pr",
            "title": "Title 9",
            "body": "Body 9",
            "state": "open",
            "author": "alice",
            "labels": '["ready"]',
            "priority": "high",
            "assignees": "[]",
            "url": "https://example.com/app/9",
            "tracker_updated_at": 1000.0,
            "fetched_at": 1000.0,
            "confidence": board.CONFIRMED,
            "external": 0,
            "extra": "{}",
        }
        values.update(over)
        store._conn.execute(
            "INSERT INTO items(repo, number, kind, title, body, state,"
            " author, labels, priority, assignees, url,"
            " tracker_updated_at, fetched_at, confidence, external, extra)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values["repo"],
                values["number"],
                values["kind"],
                values["title"],
                values["body"],
                values["state"],
                values["author"],
                values["labels"],
                values["priority"],
                values["assignees"],
                values["url"],
                values["tracker_updated_at"],
                values["fetched_at"],
                values["confidence"],
                values["external"],
                values["extra"],
            ),
        )
        return store

    def row_count(self, store):
        return store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def test_pr_reading_is_never_stored(self):
        store = self.open()
        store.upsert_item(
            reading(kind="pr", state="open", author="alice", labels=["ready"]),
            operators=["alice"],
            now=1000.0,
        )
        self.assertIsNone(store.item("app", 7, now=1000.0)[0])
        self.assertEqual(store.items(), [])

    def test_legacy_pr_row_is_hidden_from_item_and_items(self):
        store = self.open()
        store.upsert_item(reading(number=7), operators=["alice"], now=1000.0)
        self.seed_legacy_pr(store)
        self.assertIsNone(store.item("app", 9, now=1000.0)[0])
        self.assertEqual([(row.repo, row.number) for row in store.items()], [("app", 7)])

    def test_pr_reading_drops_the_legacy_row_and_keeps_issues(self):
        store = self.open()
        store.upsert_item(reading(number=7), operators=["alice"], now=1000.0)
        self.seed_legacy_pr(store)
        store.upsert_item(
            reading(number=9, kind="pr", tracker_updated_at=2000.0),
            operators=["alice"],
            now=2000.0,
        )
        self.assertEqual(self.row_count(store), 1)
        self.assertIsNone(store.item("app", 9, now=2000.0)[0])
        row, _ = store.item("app", 7, now=2000.0)
        self.assertEqual(row.title, "Title")

    def test_pr_reading_drops_the_claim_and_queued_writes(self):
        store = self.open()
        self.seed_legacy_pr(store)
        store._conn.execute(
            "INSERT INTO claims(repo, number, agent, host, epoch, claimed_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            ("app", 9, "agent-1", "host-1", 1, 1500.0),
        )
        store._conn.execute(
            "INSERT INTO outbox(repo, number, field, value, created_at,"
            " attempts, state, reason) VALUES(?, ?, ?, ?, ?, 0, ?, '')",
            ("app", 9, "agent", "agent-1", 1500.0, board.QUEUED),
        )
        store.upsert_item(
            reading(number=9, kind="pr", tracker_updated_at=2000.0),
            operators=["alice"],
            now=2000.0,
        )
        self.assertIsNone(store.claim_of("app", 9))
        self.assertEqual(
            [row for row in store.queued_writes() if (row.repo, row.number) == ("app", 9)],
            [],
        )
        dropped = [
            row
            for row in store.outbox_rows(state=board.DROPPED)
            if (row.repo, row.number) == ("app", 9)
        ]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0].field, "agent")

    def test_write_entry_points_refuse_a_legacy_pr_row(self):
        store = self.open()
        self.seed_legacy_pr(store)
        with self.assertRaises(board.StoreError):
            store.claim("app", 9, "agent-1", "host-1", now=2000.0)
        with self.assertRaises(board.StoreError):
            store.enqueue("app", 9, "agent", "agent-1", now=2000.0)
        with self.assertRaises(board.StoreError):
            store.refresh_claim("app", 9, "agent-1", 1, now=2000.0)
        with self.assertRaises(board.StoreError):
            store.release("app", 9, "agent-1", 1, now=2000.0)
        with self.assertRaises(board.StoreError):
            store.mark_absent("app", 9, now=2000.0)
        self.assertFalse(store.adopt_claim("app", 9, "agent-1", epoch=2000, now=2000.0))


if __name__ == "__main__":
    unittest.main()
