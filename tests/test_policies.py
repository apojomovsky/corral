"""The ready-made basic eligibility policy: one start request per open ticket.

Tests use plain TicketRow values, never a tracker, the network, a
subprocess, or a real state dir. The purity test fails when the policy
module touches the clock, the environment, or the settings layer.
"""

import ast
import inspect
import os
import sys
import unittest
from typing import Any
from unittest import mock

from corral import eligibility
from corral import policies
from corral import tracker


def row(number, repo="app", **fields):
    """A plain policy row with boring defaults, overridden per test."""
    values: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "title": f"Ticket {number}",
        "labels": (),
        "priority": "normal",
        "state": "open",
        "agent": "",
        "status": "",
        "external": False,
        "pulls": (),
        "extra": {},
    }
    values.update(fields)
    return eligibility.TicketRow(**values)


def selected(rows):
    """The (repo, number) pairs basic() selects, in row order."""
    return [(request.repo, request.number) for request in policies.basic(rows, now=1000.0)]


class SelectionTest(unittest.TestCase):
    def test_open_unclaimed_ticket_is_selected(self):
        self.assertEqual(selected([row(7)]), [("app", 7)])

    def test_kind_is_start(self):
        (request,) = policies.basic([row(7)], now=1000.0)
        self.assertEqual(request.kind, "start")

    def test_closed_ticket_is_skipped(self):
        self.assertEqual(selected([row(7, state="closed")]), [])

    def test_non_open_state_is_skipped(self):
        self.assertEqual(selected([row(7, state="merged")]), [])

    def test_external_ticket_is_skipped(self):
        self.assertEqual(selected([row(7, external=True)]), [])

    def test_claimed_ticket_is_requested_as_start(self):
        self.assertEqual(
            selected([row(7, agent="drive-host-1--app-7")]),
            [("app", 7)],
        )

    def test_claimed_ticket_request_kind_is_start(self):
        (request,) = policies.basic([row(7, agent="drive-host-1--app-7")], now=1000.0)
        self.assertEqual(request.kind, "start")

    def test_claimed_ticket_with_a_hold_label_is_skipped(self):
        self.assertEqual(selected([row(7, agent="drive-x", labels=("hold",))]), [])
        self.assertEqual(selected([row(7, agent="drive-x", labels=("needs-decision",))]), [])

    def test_claimed_ticket_with_a_hold_word_in_title_is_skipped(self):
        self.assertEqual(
            selected([row(7, agent="drive-x", title="Widget 7: cutover (HOLD for approval)")]),
            [],
        )

    def test_closed_claimed_ticket_is_skipped(self):
        self.assertEqual(selected([row(7, agent="drive-x", state="closed")]), [])

    def test_external_claimed_ticket_is_skipped(self):
        self.assertEqual(selected([row(7, agent="drive-x", external=True)]), [])

    def test_blocked_claimed_ticket_is_skipped(self):
        self.assertEqual(selected([row(7, agent="drive-x", extra={"blocked_by": "app#3"})]), [])
        self.assertEqual(selected([row(7, agent="drive-x", labels=("blocked",))]), [])

    def test_claimed_ticket_with_an_open_linked_pull_is_skipped(self):
        pull = eligibility.LinkedPull(number=11, state="open")
        self.assertEqual(selected([row(7, agent="drive-x", pulls=(pull,))]), [])

    def test_hold_label_is_skipped(self):
        self.assertEqual(selected([row(7, labels=("hold",))]), [])

    def test_needs_decision_label_is_skipped(self):
        self.assertEqual(selected([row(7, labels=("needs-decision",))]), [])

    def test_needs_triage_label_is_skipped(self):
        self.assertEqual(selected([row(7, labels=("needs-triage",))]), [])

    def test_unrelated_label_is_selected(self):
        self.assertEqual(selected([row(7, labels=("ready",))]), [("app", 7)])

    def test_standalone_hold_word_in_title_is_skipped(self):
        self.assertEqual(
            selected([row(7, title="Widget 8: cutover (HOLD for approval)")]),
            [],
        )

    def test_bare_hold_word_in_title_is_skipped(self):
        self.assertEqual(selected([row(7, title="HOLD")]), [])

    def test_lowercase_title_word_is_selected(self):
        self.assertEqual(selected([row(7, title="holding pattern")]), [("app", 7)])

    def test_hold_prefix_word_in_title_is_selected(self):
        self.assertEqual(selected([row(7, title="HOLDING the release")]), [("app", 7)])

    def test_blocked_by_extra_is_skipped(self):
        self.assertEqual(selected([row(7, extra={"blocked_by": "app#3"})]), [])

    def test_empty_blocked_by_extra_is_selected(self):
        self.assertEqual(selected([row(7, extra={"blocked_by": ""})]), [("app", 7)])
        self.assertEqual(selected([row(7, extra={"blocked_by": []})]), [("app", 7)])

    def test_blocked_label_is_skipped(self):
        self.assertEqual(selected([row(7, labels=("blocked",))]), [])

    def test_open_linked_pull_is_skipped(self):
        pull = eligibility.LinkedPull(number=11, state="open")
        self.assertEqual(selected([row(7, pulls=(pull,))]), [])

    def test_closed_linked_pull_is_selected(self):
        pull = eligibility.LinkedPull(number=11, state="closed")
        self.assertEqual(selected([row(7, pulls=(pull,))]), [("app", 7)])

    def test_mixed_rows_select_only_the_startable_ones(self):
        rows = [
            row(1),
            row(2, state="closed"),
            row(3, agent="drive-host-1--app-3"),
            row(4, labels=("hold",)),
            row(5, extra={"blocked_by": "app#1"}),
            row(6),
            row(8, agent="drive-host-1--app-8", labels=("hold",)),
        ]
        self.assertEqual(selected(rows), [("app", 1), ("app", 3), ("app", 6)])


class WeightsTest(unittest.TestCase):
    def test_weights_follow_priority_rank_highest_first(self):
        rows = [
            row(1, priority="backlog"),
            row(2, priority="low"),
            row(3, priority="normal"),
            row(4, priority="high"),
            row(5, priority="urgent"),
        ]
        weights = [request.weight for request in policies.basic(rows, now=1.0)]
        self.assertEqual(weights, sorted(weights))
        self.assertEqual(len(set(weights)), 5)
        ordered = sorted(zip(weights, rows), reverse=True)
        self.assertEqual(
            [row.priority for _, row in ordered],
            ["urgent", "high", "normal", "low", "backlog"],
        )

    def test_unknown_priority_ranks_lowest(self):
        rows = [row(1, priority="someday"), row(2, priority="")]
        weights = [request.weight for request in policies.basic(rows, now=1.0)]
        self.assertEqual(weights, [0, 0])

    def test_ties_keep_row_order_for_the_drive_loop(self):
        rows = [row(9, priority="high"), row(7, priority="high")]
        self.assertEqual(selected(rows), [("app", 9), ("app", 7)])

    def test_resolve_orders_by_weight_then_repo_number(self):
        policy = eligibility.load_policy("corral.policies:basic", project="blue")
        rows = [
            row(9, priority="normal"),
            row(7, priority="urgent"),
            row(8, priority="urgent", repo="api"),
        ]
        requests = policy.eligible(rows, now=1000.0)
        self.assertEqual(
            [(request.repo, request.number) for request in requests],
            [("api", 8), ("app", 7), ("app", 9)],
        )


class OddDataTest(unittest.TestCase):
    def test_missing_title_reads_as_empty(self):
        self.assertEqual(selected([row(7, title=None)]), [("app", 7)])

    def test_non_string_title_reads_as_empty(self):
        self.assertEqual(selected([row(7, title=123)]), [("app", 7)])

    def test_non_tuple_labels_read_as_none(self):
        self.assertEqual(selected([row(7, labels="hold")]), [("app", 7)])
        self.assertEqual(selected([row(7, labels=None)]), [("app", 7)])
        self.assertEqual(selected([row(7, labels=["hold"])]), [("app", 7)])


class PurityTest(unittest.TestCase):
    def test_no_clock_settings_or_environ_access(self):
        rows = [row(7, priority="high"), row(8, labels=("hold",))]
        first = policies.basic(rows, now=1000.0)
        second = policies.basic(rows, now=9999.5)
        self.assertEqual(first, second)
        with mock.patch.dict(os.environ, {}, clear=True):
            third = policies.basic(rows, now=1000.0)
        self.assertEqual(first, third)
        module = sys.modules["corral.policies"]
        namespace = vars(module)
        for name in ("time", "environ", "settings"):
            self.assertNotIn(name, namespace)
        for value in namespace.values():
            self.assertIsNot(value, sys.modules.get("time"))
            self.assertIsNot(value, sys.modules.get("corral.settings"))
            self.assertIsNot(value, os.environ)
        source = inspect.getsource(module)
        for token in (
            "settings",
            "os.environ",
            "getenv",
            "import time",
            "from time",
            "time.",
            "datetime",
            "clock",
        ):
            self.assertNotIn(token, source)


class ConstantsTest(unittest.TestCase):
    def test_hold_labels_match_tracker_needs_decision(self):
        self.assertEqual(
            policies.HOLD_LABELS,
            ("hold", tracker.NEEDS_DECISION, "needs-triage"),
        )

    def test_hold_labels_reference_tracker_constant(self):
        source = inspect.getsource(policies)
        self.assertIn("tracker.NEEDS_DECISION", source)

    def test_constants_have_docstrings(self):
        source = inspect.getsource(policies)
        tree = ast.parse(source)
        names = ("HOLD_LABELS", "HOLD_TITLE_WORD")
        for index, node in enumerate(tree.body):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets
            ):
                follower = tree.body[index + 1]
                assert isinstance(follower, ast.Expr)
                value = follower.value
                assert isinstance(value, ast.Constant)
                assert isinstance(value.value, str)
                self.assertTrue(value.value.strip())
                names = tuple(
                    name
                    for name in names
                    if name
                    not in (target.id for target in node.targets if isinstance(target, ast.Name))
                )
        self.assertEqual(names, ())


class ResolveTest(unittest.TestCase):
    def test_entry_resolves_as_corral_policies_basic(self):
        self.assertIsNone(eligibility.check_entry("corral.policies:basic", project="blue"))
        policy = eligibility.load_policy("corral.policies:basic", project="blue")
        self.assertEqual(policy.entry, "corral.policies:basic")
        requests = policy.eligible([row(7)], now=1000.0)
        self.assertEqual(
            [(request.repo, request.number) for request in requests],
            [("app", 7)],
        )

    def test_weights_match_the_tracker_vocabulary_order(self):
        ranks = {
            name: len(tracker.PRIORITIES) - tracker.PRIORITIES.index(name)
            for name in tracker.PRIORITIES
        }
        rows = [row(number, priority=name) for number, name in enumerate(tracker.PRIORITIES)]
        for request, name in zip(policies.basic(rows, now=1.0), tracker.PRIORITIES):
            self.assertEqual(request.weight, ranks[name])


if __name__ == "__main__":
    unittest.main()
