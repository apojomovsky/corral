"""Coordinator event queue and `coord wait`/`ack`.

Offline: every test gets its own state dir (explicit `root` for API
tests, `CORRAL_STATE` for CLI tests); the clock, event ids
and sleeps are injected fakes. No GitHub calls, no network, no real
state dir.
"""

import io
import json
import os
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

from corral import cli, coordcli
from corral import coord
from corral import coordevents as events


def ids(*values):
    """Deterministic event-id factory from the given values."""
    stream = iter(values)
    return lambda: next(stream)


class QueueApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_record_appends_with_shape(self):
        rows = events.record(
            [{"kind": "held-merge", "ref": "lib#1", "evidence": "HOLD: baseline"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row["id"], row["kind"], row["ref"], row["evidence"]),
            ("aaaabbbb", "held-merge", "lib#1", "HOLD: baseline"),
        )
        self.assertEqual(row["first_seen"], "1970-01-01T00:16:40+00:00")
        self.assertIsNone(row["resolved_at"])

    def test_record_dedupes_and_refreshes_evidence(self):
        events.record(
            [{"kind": "held-merge", "ref": "lib#1", "evidence": "old"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        rows = events.record(
            [{"kind": "held-merge", "ref": "lib#1", "evidence": "new"}],
            root=self.root,
            now=1001.0,
            event_id_fn=ids("ccccdddd"),
        )
        open_rows = [row for row in rows if not row.get("resolved_at")]
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["id"], "aaaabbbb")
        self.assertEqual(open_rows[0]["evidence"], "new")
        self.assertEqual(open_rows[0]["first_seen"], "1970-01-01T00:16:40+00:00")

    def test_record_default_scope_clears_only_synced_kinds(self):
        events.record(
            [
                {"kind": "held-merge", "ref": "lib#1", "evidence": "H"},
                {"kind": "supply", "ref": "fleet", "evidence": "S"},
            ],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb", "ccccdddd"),
        )
        rows = events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "S"}],
            root=self.root,
            now=1001.0,
            event_id_fn=ids("eeeeffff"),
        )
        by_id = {row["id"]: row for row in rows}
        self.assertIsNone(by_id["aaaabbbb"]["resolved_at"])
        self.assertIsNone(by_id["ccccdddd"]["resolved_at"])

    def test_record_explicit_scope_clears_gone_causes(self):
        events.record(
            [
                {"kind": "held-merge", "ref": "lib#1", "evidence": "H"},
                {"kind": "supply", "ref": "fleet", "evidence": "S"},
            ],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb", "ccccdddd"),
        )
        rows = events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "S"}],
            root=self.root,
            now=1001.0,
            event_id_fn=ids("eeeeffff"),
            kinds=("held-merge", "supply"),
        )
        by_id = {row["id"]: row for row in rows}
        self.assertIsNotNone(by_id["aaaabbbb"]["resolved_at"])
        self.assertIsNone(by_id["ccccdddd"]["resolved_at"])

    def test_record_does_not_recur_after_ack_for_the_same_state(self):
        # The coordinator acked the event and the cause is still there:
        # the next pass must not open a second event for that state
        # . A changed state is a new cause and does re-emit.
        events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "S", "state": "slots free"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        events.ack("aaaabbbb", root=self.root, now=1001.0)
        rows = events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "again", "state": "slots free"}],
            root=self.root,
            now=1002.0,
            event_id_fn=ids("ccccdddd"),
        )
        open_rows = [row for row in rows if not row.get("resolved_at")]
        self.assertEqual(open_rows, [])

    def test_record_recurs_after_ack_when_the_state_changed(self):
        events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "S", "state": "slots free"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        events.ack("aaaabbbb", root=self.root, now=1001.0)
        rows = events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "new", "state": "queue full"}],
            root=self.root,
            now=1002.0,
            event_id_fn=ids("ccccdddd"),
        )
        open_rows = [row for row in rows if not row.get("resolved_at")]
        self.assertEqual([row["id"] for row in open_rows], ["ccccdddd"])

    def test_record_recurs_after_the_cause_went_away(self):
        # The cause left, so the event resolved itself; its return is a
        # new cause and re-emits.
        events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "S", "state": "slots free"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        events.record(
            [{"kind": "supply", "ref": "other", "evidence": "O"}],
            root=self.root,
            now=1001.0,
            event_id_fn=ids("ccccdddd"),
        )
        rows = events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "back", "state": "slots free"}],
            root=self.root,
            now=1002.0,
            event_id_fn=ids("eeeeffff"),
        )
        open_rows = [row for row in rows if not row.get("resolved_at")]
        self.assertEqual([row["id"] for row in open_rows], ["eeeeffff"])

    def test_record_drops_malformed_and_prunes_old_resolved(self):
        rows = events.record(
            [
                {"kind": "", "ref": "lib#1"},
                {"kind": "supply"},
                "not-a-dict",
                {"kind": "supply", "ref": "fleet"},
            ],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        self.assertEqual([row["ref"] for row in rows if not row.get("resolved_at")], ["fleet"])
        events.ack("aaaabbbb", root=self.root, now=1000.0)
        rows = events.record(
            [{"kind": "held-merge", "ref": "lib#9", "evidence": "H"}],
            root=self.root,
            now=1000.0 + 15 * 86400,
            event_id_fn=ids("ccccdddd"),
        )
        self.assertNotIn("aaaabbbb", [row["id"] for row in rows])

    def test_ack_unknown_raises(self):
        with self.assertRaises(events.UnknownEventError):
            events.ack("deadbeef", root=self.root)

    def test_duplicate_open_rows_collapse(self):
        import json

        path = events.events_path(root=self.root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        dupe = {
            "id": "dup1",
            "kind": "supply",
            "ref": "fleet",
            "evidence": "old",
            "first_seen": "t",
            "resolved_at": None,
        }
        row = dict(dupe, id="dup2")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dupe) + "\n")
            handle.write(json.dumps(row) + "\n")
        rows = events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "new"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("eeeeffff"),
        )
        open_rows = [r for r in rows if not r.get("resolved_at")]
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["id"], "dup1")
        self.assertEqual(open_rows[0]["evidence"], "new")


class StateDedupeTest(unittest.TestCase):
    """One event per distinct cause state.

    A stopped agent awaiting a human used to re-emit its
    coordinator-review and held-merge events on every daemon pass,
    with a fresh id each time. The dedupe key now carries the cause's
    state: a question's comment body, a hold's head SHA.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def _open(self, rows):
        return [row for row in rows if not row.get("resolved_at")]

    def test_same_state_is_one_event_across_passes(self):
        item = {
            "kind": "coordinator-review",
            "ref": "app#170",
            "evidence": "needs-human: publish the registry",
            "state": "publish the registry",
        }
        events.record([item], root=self.root, now=1000.0, event_id_fn=ids("aaaabbbb"))
        rows = events.record([item], root=self.root, now=1060.0, event_id_fn=ids("ccccdddd"))
        open_rows = self._open(rows)
        self.assertEqual([row["id"] for row in open_rows], ["aaaabbbb"])
        self.assertEqual(open_rows[0]["state"], "publish the registry")

    def test_changed_state_re_emits(self):
        events.record(
            [
                {
                    "kind": "coordinator-review",
                    "ref": "lib#1",
                    "evidence": "Q1",
                    "state": "first question",
                }
            ],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        rows = events.record(
            [
                {
                    "kind": "coordinator-review",
                    "ref": "lib#1",
                    "evidence": "Q2",
                    "state": "second question",
                }
            ],
            root=self.root,
            now=1060.0,
            event_id_fn=ids("ccccdddd"),
        )
        open_rows = self._open(rows)
        self.assertEqual([row["id"] for row in open_rows], ["ccccdddd"])
        self.assertEqual(open_rows[0]["state"], "second question")

    def test_new_head_sha_re_emits_the_hold(self):
        def hold(sha):
            return {
                "kind": "held-merge",
                "ref": "lib#1",
                "evidence": "HOLD: baseline",
                "state": sha,
            }

        events.record([hold("aaa111")], root=self.root, now=1000.0, event_id_fn=ids("aaaabbbb"))
        rows = events.record(
            [hold("bbb222")], root=self.root, now=1060.0, event_id_fn=ids("ccccdddd")
        )
        open_rows = self._open(rows)
        self.assertEqual([row["id"] for row in open_rows], ["ccccdddd"])
        self.assertEqual(open_rows[0]["state"], "bbb222")

    def test_absent_state_dedupes_on_kind_and_ref(self):
        item = {"kind": "supply", "ref": "fleet", "evidence": "S"}
        events.record([item], root=self.root, now=1000.0, event_id_fn=ids("aaaabbbb"))
        rows = events.record([item], root=self.root, now=1060.0, event_id_fn=ids("ccccdddd"))
        self.assertEqual([row["id"] for row in self._open(rows)], ["aaaabbbb"])

    def test_row_without_state_matches_a_stateless_fact(self):
        # A row written without a state: it still dedupes
        # against a fact that carries none.
        path = events.events_path(root=self.root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": "oldrow1",
                        "kind": "supply",
                        "ref": "fleet",
                        "evidence": "old",
                        "first_seen": "t",
                        "resolved_at": None,
                    }
                )
                + "\n"
            )
        rows = events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "new"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("eeeeffff"),
        )
        open_rows = self._open(rows)
        self.assertEqual([row["id"] for row in open_rows], ["oldrow1"])
        self.assertEqual(open_rows[0]["evidence"], "new")

    def test_a_legacy_row_does_not_hide_a_new_state(self):
        # A live queue can hold rows written without a state.
        # The first pass after the upgrade sees a stateful fact, whose
        # state differs from the legacy row's empty one: a new event
        # opens, and the legacy row resolves as stale.
        path = events.events_path(root=self.root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": "oldrow1",
                        "kind": "held-merge",
                        "ref": "lib#1",
                        "evidence": "H",
                        "first_seen": "t",
                        "resolved_at": None,
                    }
                )
                + "\n"
            )
        rows = events.record(
            [
                {
                    "kind": "held-merge",
                    "ref": "lib#1",
                    "evidence": "H",
                    "state": events.hold_state("abc123", "H"),
                }
            ],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("eeeeffff"),
        )
        by_id = {row["id"]: row for row in rows}
        self.assertIsNotNone(by_id["oldrow1"]["resolved_at"])
        self.assertIsNone(by_id["eeeeffff"]["resolved_at"])
        self.assertEqual([row["id"] for row in self._open(rows)], ["eeeeffff"])

    def test_two_states_under_one_ref_stay_visible_together(self):
        # Found in review: a ticket can carry two held PRs, so one
        # (kind, ref) may hold several live states. Both must stay
        # open; the first must not be resolved stale in the same pass.
        rows = events.record(
            [
                {"kind": "held-merge", "ref": "lib#1", "evidence": "A", "state": "SA"},
                {"kind": "held-merge", "ref": "lib#1", "evidence": "B", "state": "SB"},
            ],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaa1111", "bbbb2222"),
        )
        self.assertEqual(sorted(row["state"] for row in self._open(rows)), ["SA", "SB"])

    def test_acked_state_stays_suppressed_beside_another_state(self):
        # Found in review: acking SB must not be undone by SA being
        # live under the same ref on the next pass.
        items = [
            {"kind": "held-merge", "ref": "lib#1", "evidence": "A", "state": "SA"},
            {"kind": "held-merge", "ref": "lib#1", "evidence": "B", "state": "SB"},
        ]
        rows = events.record(
            items, root=self.root, now=1000.0, event_id_fn=ids("aaaa1111", "bbbb2222")
        )
        sb = [row for row in self._open(rows) if row["state"] == "SB"][0]
        events.ack(sb["id"], root=self.root, now=1001.0)
        rows = events.record(items, root=self.root, now=1060.0, event_id_fn=ids("cccc3333"))
        self.assertEqual([row["state"] for row in self._open(rows)], ["SA"])
        self.assertEqual(len(rows), 2)

    def test_acked_state_that_stops_being_reported_re_emits(self):
        # Found in review: with two states on one ref, an acked state
        # that disappears (the pair still reported by its sibling)
        # must turn "gone", so its return is a new event.
        items = [
            {"kind": "held-merge", "ref": "lib#1", "evidence": "A", "state": "SA"},
            {"kind": "held-merge", "ref": "lib#1", "evidence": "B", "state": "SB"},
        ]
        rows = events.record(
            items, root=self.root, now=1000.0, event_id_fn=ids("aaaa1111", "bbbb2222")
        )
        sb = [row for row in self._open(rows) if row["state"] == "SB"][0]
        events.ack(sb["id"], root=self.root, now=1001.0)
        events.record(items[:1], root=self.root, now=1060.0, event_id_fn=ids("cccc3333"))
        by_state = {row["state"]: row for row in events.read_events(root=self.root)}
        self.assertEqual(by_state["SB"]["resolved_by"], "gone")
        rows = events.record(items, root=self.root, now=1120.0, event_id_fn=ids("dddd4444"))
        self.assertEqual(sorted(row["state"] for row in self._open(rows)), ["SA", "SB"])

    def test_repeated_passes_do_not_churn_rows_with_two_states(self):
        # Found in review: an append-only queue must not add
        # a dead row per pass when one ref carries two states.
        items = [
            {"kind": "held-merge", "ref": "lib#1", "evidence": "A", "state": "SA"},
            {"kind": "held-merge", "ref": "lib#1", "evidence": "B", "state": "SB"},
        ]
        rows = events.record(
            items, root=self.root, now=1000.0, event_id_fn=ids("aaaa1111", "bbbb2222")
        )
        for n in range(5):
            rows = events.record(items, root=self.root, now=1060.0 + n, event_id_fn=ids("cccc3333"))
        self.assertEqual(len(rows), 2)

    def test_stale_state_row_resolves_when_the_cause_moves_on(self):
        # The old state is gone, so its row resolves and the new state
        # opens its own row: one open row, and the old one is closed.
        events.record(
            [{"kind": "held-merge", "ref": "lib#1", "evidence": "H", "state": "aaa111"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        rows = events.record(
            [{"kind": "held-merge", "ref": "lib#1", "evidence": "H", "state": "bbb222"}],
            root=self.root,
            now=1060.0,
            event_id_fn=ids("ccccdddd"),
        )
        by_id = {row["id"]: row for row in rows}
        self.assertIsNotNone(by_id["aaaabbbb"]["resolved_at"])
        self.assertIsNone(by_id["ccccdddd"]["resolved_at"])
        self.assertEqual([row["id"] for row in self._open(rows)], ["ccccdddd"])


class SyncQuestionsHoldsDedupeTest(unittest.TestCase):
    """The daemon pass seam: repeated passes emit one event."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def _open(self, rows):
        return [row for row in rows if not row.get("resolved_at")]

    def _pr(self, head_sha="deadbeef"):
        return {"repo": "app", "number": 170, "head_sha": head_sha, "closes": [], "refs": []}

    def test_repeated_passes_emit_one_event_per_state(self):
        questions = {"app#170": ("T", "url", "needs-human: publish the registry")}
        holds = [(self._pr(), "stalled: checks not green: compat=FAILURE", [])]
        seen = None
        for step in range(3):
            rows = events.sync_questions_holds(
                questions, holds, root=self.root, now=1000.0 + step * 60.0
            )
            open_rows = self._open(rows)
            self.assertEqual(len(open_rows), 2, "pass %d re-emitted" % step)
            ids = {(row["kind"], row["id"]) for row in open_rows}
            if seen is not None:
                self.assertEqual(ids, seen, "pass %d opened new events" % step)
            seen = ids

    def test_new_head_sha_emits_a_second_hold_event(self):
        questions = {}
        rows = events.sync_questions_holds(
            questions, [(self._pr("aaa111"), "stalled: red", [])], root=self.root, now=1000.0
        )
        first = [row for row in self._open(rows) if row["kind"] == "held-merge"]
        self.assertEqual(len(first), 1)
        rows = events.sync_questions_holds(
            questions, [(self._pr("bbb222"), "stalled: red", [])], root=self.root, now=1060.0
        )
        open_rows = self._open(rows)
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["state"], events.hold_state("bbb222", "stalled: red"))
        self.assertNotEqual(open_rows[0]["id"], first[0]["id"])

    def test_new_question_body_emits_a_second_review_event(self):
        rows = events.sync_questions_holds(
            {"app#170": ("T", "url", "first question")}, [], root=self.root, now=1000.0
        )
        first = [row for row in self._open(rows) if row["kind"] == "coordinator-review"]
        self.assertEqual(len(first), 1)
        rows = events.sync_questions_holds(
            {"app#170": ("T", "url", "second question")}, [], root=self.root, now=1060.0
        )
        open_rows = self._open(rows)
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["state"], "second question")
        self.assertNotEqual(open_rows[0]["id"], first[0]["id"])

    def test_ack_does_not_let_the_next_pass_re_emit(self):
        # A coordinator acks the event; the cause is still there, so the
        # next pass must not open a second event for the same state.
        questions = {"app#170": ("T", "url", "question body")}
        rows = events.sync_questions_holds(questions, [], root=self.root, now=1000.0)
        open_rows = self._open(rows)
        self.assertEqual(len(open_rows), 1)
        events.ack(open_rows[0]["id"], root=self.root, now=1001.0)
        rows = events.sync_questions_holds(questions, [], root=self.root, now=1060.0)
        self.assertEqual(self._open(rows), [])


class AttentionStateTest(unittest.TestCase):
    """The snapshot attention seam: the state rides the fact."""

    def test_held_merge_state_is_the_head_sha_and_the_reason(self):
        facts = events.facts_from_attention(
            [
                {
                    "kind": "held-merge",
                    "ref": "lib#1",
                    "tier": "judgment",
                    "reason": "HOLD",
                    "head_sha": "abc123",
                }
            ]
        )
        self.assertEqual(facts[0]["state"], events.hold_state("abc123", "HOLD"))

    def test_question_state_is_the_comment_body(self):
        facts = events.facts_from_attention(
            [{"kind": "question", "ref": "lib#1", "tier": "judgment", "question": "which one?"}]
        )
        self.assertEqual(facts[0]["state"], "which one?")

    def test_state_is_the_reason_without_a_head(self):
        facts = events.facts_from_attention(
            [{"kind": "held-merge", "ref": "lib#1", "tier": "judgment", "reason": "HOLD"}]
        )
        self.assertEqual(facts[0]["state"], events.hold_state("", "HOLD"))

    def test_two_heads_never_share_a_state(self):
        one = events.hold_state("ab", "HOLD")
        two = events.hold_state("abc", "HOLD")
        self.assertNotEqual(one, two)


class WaitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_wait_returns_batch_without_sleeping(self):
        events.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "S"}],
            root=self.root,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        slept = []
        batch = events.wait_for_events(timeout=5, root=self.root, sleep_fn=slept.append)
        self.assertEqual([row["id"] for row in batch], ["aaaabbbb"])
        self.assertEqual(slept, [])

    def test_wait_timeout_returns_none(self):
        clock = iter([0.0, 0.0, 6.0])
        batch = events.wait_for_events(
            timeout=5, root=self.root, now_fn=lambda: next(clock), sleep_fn=lambda s: None
        )
        self.assertIsNone(batch)

    def test_wait_bad_timeout_is_usage_error(self):
        with self.assertRaises(events.UsageError):
            events.wait_for_events(timeout="soon", root=self.root)
        with self.assertRaises(events.UsageError):
            events.wait_for_events(timeout=-1, root=self.root)
        with self.assertRaises(events.UsageError):
            events.wait_for_events(timeout=float("nan"), root=self.root)


class AttentionMappingTest(unittest.TestCase):
    def test_pre_tier_keeps_coordinator_kinds_only(self):
        facts = events.facts_from_attention(
            [
                {"kind": "question", "ref": "lib#1", "title": "T", "question": "Q body"},
                {"kind": "held-merge", "ref": "lib#2", "reason": "HOLD"},
                {"kind": "queued-merge", "ref": "lib#3", "reason": "q"},
                {"kind": "urgent", "ref": "lib#4"},
                {"kind": "question", "ref": ""},
                "not-a-dict",
            ]
        )
        self.assertEqual(
            [(fact["kind"], fact["ref"]) for fact in facts],
            [("coordinator-review", "lib#1"), ("held-merge", "lib#2")],
        )
        self.assertEqual(facts[0]["evidence"], "T: Q body")

    def test_unknown_attention_kind_passes_through(self):
        facts = events.facts_from_attention(
            [
                {"kind": "question", "ref": "a#1", "tier": "judgment"},
                {"kind": "ci-red-unexplained", "ref": "a#2", "tier": "judgment", "reason": "red"},
            ]
        )
        self.assertEqual(
            [(fact["kind"], fact["ref"]) for fact in facts],
            [("coordinator-review", "a#1"), ("ci-red-unexplained", "a#2")],
        )

    def test_tiered_keeps_only_judgment(self):
        facts = events.facts_from_attention(
            [
                {"kind": "question", "ref": "lib#1", "tier": "human"},
                {"kind": "held-merge", "ref": "lib#2", "tier": "judgment", "reason": "H"},
                {"kind": "stalled-merge", "ref": "lib#3", "tier": "deterministic"},
            ]
        )
        self.assertEqual([(fact["kind"], fact["ref"]) for fact in facts], [("held-merge", "lib#2")])

    def test_mixed_batch_judges_each_item_alone(self):
        facts = events.facts_from_attention(
            [
                {"kind": "question", "ref": "a#1", "tier": "judgment"},
                {"kind": "question", "ref": "a#2"},
                {"kind": "queued-merge", "ref": "a#3"},
                {"kind": "urgent", "ref": "a#4", "tier": None},
            ]
        )
        self.assertEqual(
            [(fact["kind"], fact["ref"]) for fact in facts],
            [("coordinator-review", "a#1"), ("coordinator-review", "a#2")],
        )

    def test_escalated_item_stays_a_coordinator_fact(self):
        # An item the snapshot serves as human after the coordinator
        # grace still wakes the coordinator: the
        # escalated_from tag keeps it in the judgment lane.
        facts = events.facts_from_attention(
            [
                {
                    "kind": "held-merge",
                    "ref": "lib#1",
                    "tier": "human",
                    "escalated_from": "judgment",
                    "reason": "HOLD",
                    "head_sha": "abc123",
                }
            ]
        )
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["kind"], "held-merge")
        self.assertEqual(facts[0]["state"], events.hold_state("abc123", "HOLD"))

    def test_sync_questions_holds_records(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pr = {"repo": "lib", "number": 7, "closes": [("acme/lib", 1)], "refs": []}
        rows = events.sync_questions_holds(
            {"lib#1": ("T", "url", "needs-human: decide X")},
            [(pr, "HOLD: baseline", [])],
            root=tmp.name,
            now=1000.0,
        )
        open_rows = [row for row in rows if not row.get("resolved_at")]
        self.assertEqual(
            sorted((row["kind"], row["ref"]) for row in open_rows),
            [("coordinator-review", "lib#1"), ("held-merge", "lib#1")],
        )

    def test_empty_pass_clears_observed_kinds_keeps_others(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events.record(
            [
                {"kind": "held-merge", "ref": "lib#1", "evidence": "H"},
                {"kind": "supply", "ref": "fleet", "evidence": "S"},
            ],
            root=tmp.name,
            now=1000.0,
            event_id_fn=ids("aaaabbbb", "ccccdddd"),
        )
        rows = events.sync_questions_holds({}, [], root=tmp.name, now=1001.0)
        by_id = {row["id"]: row for row in rows}
        self.assertIsNotNone(by_id["aaaabbbb"]["resolved_at"])
        self.assertIsNone(by_id["ccccdddd"]["resolved_at"])

    def test_unobserved_kind_never_clears(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events.record(
            [{"kind": "held-merge", "ref": "lib#1", "evidence": "H"}],
            root=tmp.name,
            now=1000.0,
            event_id_fn=ids("aaaabbbb"),
        )
        rows = events.sync_questions_holds(
            {}, [], root=tmp.name, now=1001.0, kinds=("coordinator-review",)
        )
        by_id = {row["id"]: row for row in rows}
        self.assertIsNone(by_id["aaaabbbb"]["resolved_at"])


class CoordEventsCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_STATE": tmp.name, "CORRAL_PROJECT": "demo"})
        env.start()
        self.addCleanup(env.stop)
        for name, fake in (("_coord_snapshot", lambda: {}), ("_coord_human_prs", lambda: [])):
            patcher = mock.patch.object(coordcli, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_wait_timeout_exit_1(self):
        code, out, err = self._run(["coord", "wait", "--timeout", "0"])
        self.assertEqual(code, cli.NO)
        self.assertEqual(out, "")
        self.assertIn("timeout", err)
        code, out, _ = self._run(["coord", "wait", "--timeout", "0", "--json"])
        self.assertEqual(code, cli.NO)
        self.assertEqual(json.loads(out), [])

    def test_wait_bad_timeout_exit_4(self):
        code, _, err = self._run(["coord", "wait", "--timeout", "soon"])
        self.assertEqual(code, cli.USAGE)
        self.assertIn("seconds", err)

    def test_wait_then_ack_round_trip(self):
        code, out, _ = self._run(["coord", "wait", "--timeout", "0"])
        self.assertEqual(code, cli.NO)
        from corral import coordevents as live

        live.record(
            [{"kind": "supply", "ref": "fleet", "evidence": "slots free, queue empty"}], now=1000.0
        )
        code, out, _ = self._run(["coord", "wait", "--timeout", "5"])
        self.assertEqual(code, cli.OK)
        self.assertIn("supply fleet slots free, queue empty", out)
        event_id = out.split()[0]
        code, out, _ = self._run(["coord", "ack", event_id])
        self.assertEqual(code, cli.OK)
        self.assertIn(event_id, out)
        code, _, _ = self._run(["coord", "wait", "--timeout", "0"])
        self.assertEqual(code, cli.NO)

    def test_ack_unknown_exit_1(self):
        code, _, err = self._run(["coord", "ack", "deadbeef"])
        self.assertEqual(code, cli.NO)
        self.assertIn("deadbeef", err)

    def test_brief_carries_events_section(self):
        from corral import coordevents as live

        live.record(
            [{"kind": "held-merge", "ref": "lib#1", "evidence": "HOLD"}],
            now=1000.0,
            event_id_fn=lambda: "aaaabbbb",
        )
        code, out, _ = self._run(["coord", "brief", "--json"])
        self.assertEqual(code, cli.OK)
        data = json.loads(out)
        self.assertEqual(
            [(row["id"], row["kind"]) for row in data["events"]], [("aaaabbbb", "held-merge")]
        )
        code, out, _ = self._run(["coord", "brief"])
        self.assertEqual(code, cli.OK)
        self.assertIn("events:", out)
        self.assertIn("aaaabbbb held-merge lib#1: HOLD", out)

    def test_brief_without_events_names_empty_section(self):
        data = coord.brief(root=coord.project_root())
        self.assertEqual(data["events"], [])
        self.assertIn("events: none waiting", coord.render_brief_text(data))


if __name__ == "__main__":
    unittest.main()
