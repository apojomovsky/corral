"""The board is the maintainer's view of the work: rule-text conformance.

The stewardship rule lives word-for-word in `AGENTS.md` and
`skills/coordinator.md`, and the coordinator heartbeat checks the
board with the sweep. Like the verb-table conformance test, this pins
the sentences so a rewording fails loudly instead of silently dropping
the rule. Plain files only, no subprocess, no network.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

RULE = (
    "No agent or coordinator leaves the board stale: the loop keeps "
    "every card in step with its issue, and a coordinator who dispatches "
    "work outside the loop makes that work visible to the board the "
    "moment it starts (a claim, or a registered agent) and never ends "
    "a session with drift."
)

HEARTBEAT_CHECK = "korapy board sweep --plan"


class BoardStewardshipTest(unittest.TestCase):
    def test_agents_rules_carry_the_board_rule(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(RULE, text)

    def test_coordinator_skill_carries_the_board_rule(self):
        text = (REPO / "skills" / "coordinator.md").read_text(encoding="utf-8")
        self.assertIn(RULE, text)

    def test_coordinator_heartbeat_checks_the_board_with_the_sweep(self):
        text = (REPO / "skills" / "coordinator.md").read_text(encoding="utf-8")
        self.assertIn(HEARTBEAT_CHECK, text)


if __name__ == "__main__":
    unittest.main()
