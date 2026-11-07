"""Board drift: the shared finders the pass and the sweep run.

Pure moves over normalized cards plus the store reconciliation the
board step runs every poll. Fakes only: a real board store on a
scratch file, plain card data, no ``gh``, no network, no real state
dir.
"""

import os
import tempfile
import unittest

from korapy import board, boardsweep, boardstatus


REPO = "example/demo"
T0 = 1_800_000_100.0

IDS = {
    boardstatus.AVAILABLE: "opt-a",
    boardstatus.CLAIMED: "opt-c",
    boardstatus.REVIEW: "opt-r",
    boardstatus.DONE: "opt-d",
}

NAMES = {"Available": "opt-a", "Claimed": "opt-c", "In review": "opt-r", "Done": "opt-d"}


def card(number, state="open", status="", updated_at=T0, repo=REPO):
    return boardsweep.Card(repo, number, state, status, updated_at)


def moves_for(cards, **over):
    params = {
        "names": dict(NAMES),
        "ids": dict(IDS),
        "claims": set(),
        "records": {},
        "pulls": set(),
        "now": T0,
    }
    params.update(over)
    return boardsweep.status_moves(list(cards), **params)


class PollCardsTest(unittest.TestCase):
    def test_pull_requests_are_never_tickets(self):
        from korapy import tracker as iface

        item = iface.TrackerItem(
            repo=REPO,
            number=7,
            kind="pr",
            title="",
            body="",
            state="open",
            author="alice",
            labels=(),
            priority="",
            assignees=(),
            url="",
            updated_at=T0,
            from_fork=False,
            extra={"board_status": "Available"},
        )
        self.assertEqual(boardsweep.poll_cards([item]), [])

    def test_missing_status_reads_as_unset(self):
        from korapy import tracker as iface

        item = iface.TrackerItem(
            repo=REPO,
            number=7,
            kind="issue",
            title="",
            body="",
            state="open",
            author="alice",
            labels=(),
            priority="",
            assignees=(),
            url="",
            updated_at=T0,
            from_fork=False,
            extra={},
        )
        self.assertEqual(boardsweep.poll_cards([item]), [boardsweep.Card(REPO, 7, "open", "", T0)])

    def test_cards_sort_by_repo_and_number(self):
        found = [card(9), card(7)]
        self.assertEqual([item.number for item in boardsweep.poll_cards(found)], [7, 9])


class StatusMovesTest(unittest.TestCase):
    def test_closed_card_not_done_moves_to_done(self):
        moves, notes = moves_for([card(7, state="closed", status="Available")])
        self.assertEqual(notes, ())
        (move,) = moves
        self.assertEqual(move.kind, "set")
        self.assertEqual(move.want, "Done")
        self.assertEqual(move.derived, boardstatus.DONE)
        self.assertEqual(move.age_s, 0.0)

    def test_claimed_card_showing_available_moves_to_claimed(self):
        moves, _ = moves_for([card(7, status="Available", updated_at=T0 - 90)], claims={(REPO, 7)})
        (move,) = moves
        self.assertEqual((move.kind, move.want), ("set", "Claimed"))
        self.assertEqual(move.age_s, 90.0)

    def test_card_at_its_derived_value_with_no_record_rests(self):
        moves, _ = moves_for([card(7, status="Available")])
        self.assertEqual(moves, ())

    def test_card_at_its_derived_value_with_matching_record_rests(self):
        record = boardstatus.StatusRecord("opt-a", boardstatus.AVAILABLE)
        moves, _ = moves_for([card(7, status="Available")], records={(REPO, 7): record})
        self.assertEqual(moves, ())

    def test_manual_move_rests_until_derived_changes(self):
        record = boardstatus.StatusRecord("opt-c", boardstatus.CLAIMED)
        moves, _ = moves_for(
            [card(7, status="Available")],
            claims={(REPO, 7)},
            records={(REPO, 7): record},
        )
        self.assertEqual(moves, ())

    def test_manual_move_releases_when_derived_changes(self):
        record = boardstatus.StatusRecord("opt-c", boardstatus.CLAIMED)
        moves, _ = moves_for(
            [card(7, state="closed", status="Available")], records={(REPO, 7): record}
        )
        (move,) = moves
        self.assertEqual(move.want, "Done")

    def test_unmapped_derived_state_syncs_nothing(self):
        ids = dict(IDS)
        del ids[boardstatus.AVAILABLE]
        moves, _ = moves_for([card(7, status="Claimed")], ids=ids)
        self.assertEqual(moves, ())

    def test_open_card_rests_with_a_note_when_pulls_are_unknown(self):
        moves, notes = moves_for(
            [card(7, status="Available")],
            pulls=None,
            pulls_error="pulls lookup limited at 0 call(s)",
        )
        self.assertEqual(moves, ())
        self.assertEqual(notes, (f"{REPO}#7: pulls lookup limited at 0 call(s)",))

    def test_closed_card_needs_no_pulls(self):
        moves, notes = moves_for(
            [card(7, state="closed", status="Available")], pulls=None, pulls_error="boom"
        )
        self.assertEqual(len(moves), 1)
        self.assertEqual(notes, ())

    def test_open_pull_means_review(self):
        moves, _ = moves_for([card(7, status="Available")], pulls={7})
        (move,) = moves
        self.assertEqual(move.want, "In review")

    def test_ambiguous_display_name_resolves_to_neither(self):
        resolved = boardstatus.option_names(
            {"opt-a": "Available", "opt-c": "Available", "opt-r": "In review"}
        )
        self.assertEqual(resolved, {"In review": "opt-r"})


class MissingMovesTest(unittest.TestCase):
    def test_open_card_missing_from_the_board_moves_to_add(self):
        moves = boardsweep.missing_moves([card(7, updated_at=T0 - 30)], present={(REPO, 9)}, now=T0)
        (move,) = moves
        self.assertEqual((move.kind, move.want, move.derived, move.age_s), ("add", "", "", 30.0))

    def test_present_cards_and_closed_cards_rest(self):
        self.assertEqual(boardsweep.missing_moves([card(7)], present={(REPO, 7)}, now=T0), ())


class FormatMoveTest(unittest.TestCase):
    def test_add_move_names_the_board(self):
        move = boardsweep.Move(REPO, 7, "open", "", "", "add", "", 45.0)
        self.assertEqual(
            boardsweep.format_move(move),
            f"{REPO}#7: state open, add to board (drift 45s ago)",
        )

    def test_set_move_names_the_status(self):
        move = boardsweep.Move(REPO, 9, "closed", "Available", "done", "set", "Done", 600.0)
        self.assertEqual(
            boardsweep.format_move(move),
            f"{REPO}#9: state closed, set Status to 'Done' (drift 10m ago)",
        )


class BoardLineTest(unittest.TestCase):
    def test_quiet_pass_prints_nothing(self):
        self.assertIsNone(boardsweep.board_line(0, 0, 0))

    def test_changed_pass_names_what_changed(self):
        self.assertEqual(boardsweep.board_line(2, 1, 0), "board kept: 2 added, 1 status set")

    def test_found_drift_prints_the_remainder(self):
        self.assertEqual(boardsweep.board_line(0, 0, 3), "board kept: drift remaining: 3")
        self.assertEqual(boardsweep.board_line(1, 0, 2), "board kept: 1 added, drift remaining: 2")


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-boardsweep-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def seed(self, number, state="open", author="alice", agent=None):
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=number,
                title="Title",
                body="Body",
                state=state,
                author=author,
                url="https://example.com/x",
                tracker_updated_at=T0,
            ),
            operators=["alice"],
            now=T0,
        )
        if agent is not None:
            self.store.claim(REPO, number, agent, "host-1", now=T0)

    def test_drifted_card_is_flagged_and_a_fixed_card_cleared(self):
        self.seed(7, state="closed")
        self.seed(9)
        self.store.note_drift(REPO, 9, now=T0 - 10)
        moves, _ = boardsweep.reconcile(
            self.store,
            REPO,
            cards=[card(7, state="closed", status="Available"), card(9, status="Available")],
            open_cards=boardsweep.store_cards(self.store.items(repo=REPO, state="open")),
            present={(REPO, 7), (REPO, 9)},
            names=dict(NAMES),
            ids=dict(IDS),
            pulls=set(),
            now=T0,
            complete=True,
            status_active=True,
            adopt_active=True,
        )
        self.assertEqual([(move.number, move.kind) for move in moves], [(7, "set")])
        self.assertEqual(self.store.drift_flags(), [(REPO, 7, T0)])

    def test_first_sighting_wins(self):
        self.seed(7, state="closed")
        moves, _ = boardsweep.reconcile(
            self.store,
            REPO,
            cards=[card(7, state="closed", status="Available")],
            open_cards=boardsweep.store_cards(self.store.items(repo=REPO, state="open")),
            present={(REPO, 7)},
            names=dict(NAMES),
            ids=dict(IDS),
            pulls=set(),
            now=T0,
            complete=False,
            status_active=True,
            adopt_active=True,
        )
        self.assertEqual(len(moves), 1)
        boardsweep.reconcile(
            self.store,
            REPO,
            cards=[card(7, state="closed", status="Available")],
            open_cards=boardsweep.store_cards(self.store.items(repo=REPO, state="open")),
            present={(REPO, 7)},
            names=dict(NAMES),
            ids=dict(IDS),
            pulls=set(),
            now=T0 + 60,
            complete=False,
            status_active=True,
            adopt_active=True,
        )
        self.assertEqual(self.store.drift_flags(), [(REPO, 7, T0)])

    def test_truncated_poll_leaves_missing_verdicts_alone(self):
        self.seed(7)
        self.store.note_drift(REPO, 7, now=T0 - 10)
        moves, _ = boardsweep.reconcile(
            self.store,
            REPO,
            cards=[card(9, status="Available")],
            open_cards=boardsweep.store_cards(self.store.items(repo=REPO, state="open")),
            present={(REPO, 9)},
            names=dict(NAMES),
            ids=dict(IDS),
            pulls=set(),
            now=T0,
            complete=False,
            status_active=True,
            adopt_active=True,
        )
        self.assertEqual(moves, ())
        self.assertEqual(self.store.drift_flags(), [(REPO, 7, T0 - 10)])

    def test_complete_poll_flags_missing_and_clears_gone(self):
        self.seed(7)
        self.seed(9, state="closed")
        self.store.note_drift(REPO, 9, now=T0 - 10)
        moves, _ = boardsweep.reconcile(
            self.store,
            REPO,
            cards=[card(9, state="closed", status="Done")],
            open_cards=boardsweep.store_cards(self.store.items(repo=REPO, state="open")),
            present={(REPO, 9)},
            names=dict(NAMES),
            ids=dict(IDS),
            pulls=set(),
            now=T0,
            complete=True,
            status_active=True,
            adopt_active=True,
        )
        self.assertEqual([(move.number, move.kind) for move in moves], [(7, "add")])
        self.assertEqual(self.store.drift_flags(), [(REPO, 7, T0)])

    def test_inactive_status_step_clears_present_flags_on_complete(self):
        self.seed(7, state="closed")
        self.store.note_drift(REPO, 7, now=T0 - 10)
        moves, _ = boardsweep.reconcile(
            self.store,
            REPO,
            cards=[card(7, state="closed", status="Available")],
            open_cards=[],
            present={(REPO, 7)},
            names=dict(NAMES),
            ids=dict(IDS),
            pulls=set(),
            now=T0,
            complete=True,
            status_active=False,
            adopt_active=True,
        )
        self.assertEqual(moves, ())
        self.assertEqual(self.store.drift_flags(), [])


if __name__ == "__main__":
    unittest.main()
