"""Ownership settlement for board readings, ticket 6: echo, overridden,
empty and stale cases through ``upsert`` for board-sourced values.

Board-sourced corral values ride the tracker's ``agent`` and ``status``
attributes (the board reader reports them there, never in ``extra``),
so settlement must reach identical verdicts to label mode: a fresher
reading echoing a queued value marks it sent, a fresher different value
drops it as overridden, an empty value settles nothing, and a stale
reading settles nothing.

Fakes only: the tracker is a fake, the store is a temporary file. No
``gh``, no GraphQL, no network, no real state dir.
"""

import os
import tempfile
import unittest
from typing import Any

from corral import board, sync
from tests.test_tracker import FakeTracker, reading

REPO = "acme/app"
T0 = 1_800_000_100.0


def board_reading(number=1, updated_at=T0, extra=None, **over: Any):
    """One tracker reading shaped like board output: corral values as
    attributes, board concepts in ``extra``."""
    concepts = {"pool": "dispatch", "priority": "high"}
    concepts.update(extra or {})
    params: dict[str, Any] = {
        "repo": REPO,
        "number": number,
        "updated_at": updated_at,
        "extra": concepts,
    }
    params.update(over)
    return reading(**params)


def label_reading(number=1, updated_at=T0, **over: Any):
    """The same row as the label adapter reports it: no board concepts."""
    params: dict[str, Any] = {"repo": REPO, "number": number, "updated_at": updated_at}
    params.update(over)
    return reading(**params)


class SettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-board-settlement-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def seed(self, number=1, updated_at=T0 - 100, extra=None, **over: Any):
        params: dict[str, Any] = {
            "repo": REPO,
            "number": number,
            "tracker_updated_at": updated_at,
            "extra": dict(extra if extra is not None else {"pool": "dispatch"}),
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

    def apply(self, item, now=None):
        self.store.upsert_item(
            sync._reading(item), operators=["alice"], now=T0 if now is None else now
        )

    def verdict(self, number=1):
        """Settlement verdict: queued, sent and dropped outbox counts,
        plus the row confidence."""
        row, _ = self.store.item(REPO, number, now=T0)
        return (
            len(self.store.queued_writes()),
            len(self.store.outbox_rows(state=board.SENT)),
            len(self.store.outbox_rows(state=board.DROPPED)),
            row.confidence,
        )


class BoardSettlementTest(SettlementCase):
    def test_echo_marks_queued_agent_sent(self):
        self.seed()
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(board_reading(agent="agent-1", updated_at=T0), now=T0)
        self.assertEqual(self.verdict(), (0, 1, 0, board.CONFIRMED))

    def test_echo_marks_queued_status_sent(self):
        self.seed()
        self.store.enqueue(REPO, 1, "status", "working", now=T0 - 50)
        self.apply(board_reading(status="working", updated_at=T0), now=T0)
        self.assertEqual(self.verdict(), (0, 1, 0, board.CONFIRMED))

    def test_fresher_person_edit_drops_queued_agent_as_overridden(self):
        self.seed()
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(board_reading(agent="agent-2", updated_at=T0), now=T0)
        self.assertEqual(self.verdict(), (0, 0, 1, board.CONFIRMED))
        rows = self.store.outbox_rows(state=board.DROPPED)
        self.assertEqual(rows[0].reason, "overridden")

    def test_fresher_person_edit_drops_queued_status_as_overridden(self):
        self.seed()
        self.store.enqueue(REPO, 1, "status", "working", now=T0 - 50)
        self.apply(board_reading(status="review", updated_at=T0), now=T0)
        self.assertEqual(self.verdict(), (0, 0, 1, board.CONFIRMED))
        rows = self.store.outbox_rows(state=board.DROPPED)
        self.assertEqual(rows[0].reason, "overridden")

    def test_empty_reading_settles_nothing(self):
        self.seed()
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.store.enqueue(REPO, 1, "status", "working", now=T0 - 50)
        self.apply(board_reading(updated_at=T0), now=T0)
        self.assertEqual(self.verdict(), (2, 0, 0, board.PENDING))

    def test_empty_reading_preserves_stored_board_extra(self):
        self.seed(extra={"priority": "high", "pool": "dispatch"})
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(board_reading(updated_at=T0, extra={}), now=T0)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.extra, {"priority": "high", "pool": "dispatch"})

    def test_stale_reading_settles_nothing(self):
        self.seed()
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(board_reading(agent="agent-1", updated_at=T0 - 80), now=T0)
        self.assertEqual(self.verdict(), (1, 0, 0, board.PENDING))

    def test_stale_person_edit_settles_nothing(self):
        self.seed()
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(board_reading(agent="agent-2", updated_at=T0 - 80), now=T0)
        self.assertEqual(self.verdict(), (1, 0, 0, board.PENDING))

    def test_newer_queue_survives_an_older_person_edit(self):
        self.seed()
        self.apply(board_reading(agent="agent-2", updated_at=T0 - 80), now=T0 - 80)
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(board_reading(agent="agent-2", updated_at=T0 - 80), now=T0)
        self.assertEqual(self.verdict(), (1, 0, 0, board.PENDING))

    def test_echo_merges_board_concepts_per_key(self):
        self.seed(extra={"priority": "high", "pool": "a"})
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(board_reading(agent="agent-1", updated_at=T0, extra={"pool": "b"}), now=T0)
        self.assertEqual(self.verdict(), (0, 1, 0, board.CONFIRMED))
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.extra, {"priority": "high", "pool": "b"})

    def test_board_only_edit_settles_like_a_fresher_label_reading(self):
        self.seed()
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        self.apply(
            board_reading(
                agent="agent-1",
                updated_at=T0 + 50,
                extra={"priority": "urgent"},
                title="Title",
            ),
            now=T0 + 50,
        )
        row, _ = self.store.item(REPO, 1, now=T0 + 50)
        self.assertEqual(row.tracker_updated_at, T0 + 50)
        self.assertEqual(row.extra["priority"], "urgent")
        queued = [row for row in self.store.outbox_rows() if row.state == board.QUEUED]
        self.assertEqual(queued, [])
        self.assertEqual(len(self.store.outbox_rows(state=board.SENT)), 1)

    def test_board_reading_never_originates_a_claim(self):
        self.seed()
        self.apply(board_reading(agent="agent-9", updated_at=T0), now=T0)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.agent, "")


def run_scenario(make_reading):
    """One queued agent write settled by one reading, as a verdict tuple.

    A fresh temporary store per call, so board and label rows compare
    directly with no shared state. Only the settlement verdict compares:
    board rows keep their stored concepts by design.
    """
    tmp = tempfile.TemporaryDirectory(prefix="corral-board-settlement-parity-")
    try:
        store = board.open_store(os.path.join(tmp.name, "board.sqlite"))
        try:
            store.upsert_item(
                board.Item(repo=REPO, number=1, author="alice", tracker_updated_at=T0 - 100),
                operators=["alice"],
                now=T0 - 100,
            )
            store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
            store.upsert_item(sync._reading(make_reading()), operators=["alice"], now=T0)
            row, _ = store.item(REPO, 1, now=T0)
            return (
                len(store.queued_writes()),
                len(store.outbox_rows(state=board.SENT)),
                len(store.outbox_rows(state=board.DROPPED)),
                row.confidence,
            )
        finally:
            store.close()
    finally:
        tmp.cleanup()


class LabelParityTest(unittest.TestCase):
    def test_echo_verdict_matches_label_mode(self):
        board_verdict = run_scenario(lambda: board_reading(agent="agent-1", updated_at=T0))
        label_verdict = run_scenario(lambda: label_reading(agent="agent-1", updated_at=T0))
        self.assertEqual(board_verdict, label_verdict)
        self.assertEqual(board_verdict, (0, 1, 0, board.CONFIRMED))

    def test_overridden_verdict_matches_label_mode(self):
        board_verdict = run_scenario(lambda: board_reading(agent="agent-2", updated_at=T0))
        label_verdict = run_scenario(lambda: label_reading(agent="agent-2", updated_at=T0))
        self.assertEqual(board_verdict, label_verdict)
        self.assertEqual(board_verdict, (0, 0, 1, board.CONFIRMED))

    def test_empty_verdict_matches_label_mode(self):
        board_verdict = run_scenario(lambda: board_reading(updated_at=T0))
        label_verdict = run_scenario(lambda: label_reading(updated_at=T0))
        self.assertEqual(board_verdict, label_verdict)
        self.assertEqual(board_verdict, (1, 0, 0, board.PENDING))

    def test_stale_verdict_matches_label_mode(self):
        board_verdict = run_scenario(lambda: board_reading(agent="agent-1", updated_at=T0 - 80))
        label_verdict = run_scenario(lambda: label_reading(agent="agent-1", updated_at=T0 - 80))
        self.assertEqual(board_verdict, label_verdict)
        self.assertEqual(board_verdict, (1, 0, 0, board.PENDING))


class ResyncParityTest(SettlementCase):
    def test_resync_adopts_a_board_shown_claim(self):
        fake = FakeTracker([board_reading(agent="agent-1", updated_at=T0)])
        result = sync.resync_repo(self.store, fake, REPO, operators=["alice"], now=T0)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.conflicts, ())
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.agent, "agent-1")
        self.assertEqual(row.confidence, board.CONFIRMED)
        self.assertEqual(self.store.outbox_rows(), [])

    def test_resync_reports_a_store_wins_conflict_identically(self):
        self.seed()
        self.store.claim(REPO, 1, "agent-1", "host-1", now=T0 - 50)
        fake = FakeTracker([board_reading(agent="agent-2", updated_at=T0)])
        result = sync.resync_repo(self.store, fake, REPO, operators=["alice"], now=T0)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.conflicts, (f"{REPO}#1: store=agent-1 tracker=agent-2",))

    def test_queued_write_blocks_a_board_shown_adoption(self):
        self.seed()
        self.store.enqueue(REPO, 1, "agent", "agent-9", now=T0)
        fake = FakeTracker([board_reading(agent="agent-1", updated_at=T0)])
        result = sync.resync_repo(self.store, fake, REPO, operators=["alice"], now=T0)
        self.assertEqual(result.conflicts, ())
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.agent, "agent-9")

    def test_fresher_board_value_overrides_then_adopts_like_label_mode(self):
        """A fresher tracker value drops the queued write as overridden,
        and the resync then adopts the shown claim: the same two steps
        label mode takes, with the same outcome."""
        for make_reading in (
            lambda: board_reading(agent="agent-1", updated_at=T0),
            lambda: label_reading(agent="agent-1", updated_at=T0),
        ):
            with self.subTest(mode="board" if "pool" in (make_reading().extra or {}) else "label"):
                tmp = tempfile.TemporaryDirectory(prefix="corral-board-settlement-race-")
                self.addCleanup(tmp.cleanup)
                store = board.open_store(os.path.join(tmp.name, "board.sqlite"))
                self.addCleanup(store.close)
                store.upsert_item(
                    board.Item(repo=REPO, number=1, author="alice", tracker_updated_at=T0 - 100),
                    operators=["alice"],
                    now=T0 - 100,
                )
                store.enqueue(REPO, 1, "agent", "agent-9", now=T0 - 50)
                result = sync.resync_repo(
                    store, FakeTracker([make_reading()]), REPO, operators=["alice"], now=T0
                )
                self.assertEqual(result.errors, ())
                self.assertEqual(result.conflicts, ())
                dropped = store.outbox_rows(state=board.DROPPED)
                self.assertEqual([row.reason for row in dropped], ["overridden"])
                row, _ = store.item(REPO, 1, now=T0)
                self.assertEqual(row.agent, "agent-1")


if __name__ == "__main__":
    unittest.main()
