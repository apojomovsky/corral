"""The board Status sync: korapy sets the Status column from issue state.

Derived state, in order: a closed issue means Done, an open linked pull
request means In review, a ticket holding a live claim means Claimed,
otherwise Available. A person's manual move wins until the derived
state changes. Fakes only: no ``gh``, no GraphQL, no network, no real
state dir.
"""

import os
import tempfile
import unittest

from korapy import board, boardstatus, sync
from korapy import tracker as iface
from korapy.github.board import BoardSchema, FieldDef
from tests.test_tracker import reading


class DeriveTest(unittest.TestCase):
    def test_closed_issue_means_done(self):
        self.assertEqual(
            boardstatus.derive(state="closed", has_open_pull=True, claimed=True),
            boardstatus.DONE,
        )

    def test_open_pull_means_review(self):
        self.assertEqual(
            boardstatus.derive(state="open", has_open_pull=True, claimed=True),
            boardstatus.REVIEW,
        )

    def test_live_claim_means_claimed(self):
        self.assertEqual(
            boardstatus.derive(state="open", has_open_pull=False, claimed=True),
            boardstatus.CLAIMED,
        )

    def test_plain_open_issue_means_available(self):
        self.assertEqual(
            boardstatus.derive(state="open", has_open_pull=False, claimed=False),
            boardstatus.AVAILABLE,
        )


IDS = {
    boardstatus.AVAILABLE: "opt-a",
    boardstatus.CLAIMED: "opt-c",
    boardstatus.REVIEW: "opt-r",
    boardstatus.DONE: "opt-d",
}


def record(option_id, derived):
    return boardstatus.StatusRecord(option_id=option_id, derived=derived)


class DecideTest(unittest.TestCase):
    def test_stale_card_writes_derived(self):
        self.assertEqual(
            boardstatus.decide("opt-a", None, boardstatus.CLAIMED, IDS),
            ("write", "opt-c"),
        )

    def test_matching_card_with_no_record_adopts_it(self):
        self.assertEqual(
            boardstatus.decide("opt-c", None, boardstatus.CLAIMED, IDS),
            ("record", record("opt-c", boardstatus.CLAIMED)),
        )

    def test_matching_card_with_matching_record_rests(self):
        self.assertEqual(
            boardstatus.decide(
                "opt-c", record("opt-c", boardstatus.CLAIMED), boardstatus.CLAIMED, IDS
            ),
            ("leave", None),
        )

    def test_manual_move_wins_until_derived_changes(self):
        self.assertEqual(
            boardstatus.decide(
                "opt-x", record("opt-c", boardstatus.CLAIMED), boardstatus.CLAIMED, IDS
            ),
            ("leave", None),
        )

    def test_manual_move_releases_when_derived_changes(self):
        self.assertEqual(
            boardstatus.decide(
                "opt-x", record("opt-c", boardstatus.CLAIMED), boardstatus.REVIEW, IDS
            ),
            ("write", "opt-r"),
        )

    def test_unmapped_derived_state_syncs_nothing(self):
        partial = dict(IDS)
        del partial[boardstatus.CLAIMED]
        self.assertEqual(
            boardstatus.decide("opt-a", None, boardstatus.CLAIMED, partial),
            ("leave", None),
        )

    def test_remapped_option_rewrites_the_same_state(self):
        self.assertEqual(
            boardstatus.decide(
                "opt-c",
                record("opt-c", boardstatus.CLAIMED),
                boardstatus.CLAIMED,
                {**IDS, boardstatus.CLAIMED: "opt-c2"},
            ),
            ("write", "opt-c2"),
        )


class StatusRecordStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-status-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def test_unknown_card_has_no_record(self):
        self.assertIsNone(self.store.status_set_of("acme/app", 7))

    def test_recorded_set_reads_back(self):
        self.store.record_status_set("acme/app", 7, "opt-c", boardstatus.CLAIMED, now=1000.0)
        self.assertEqual(
            self.store.status_set_of("acme/app", 7),
            boardstatus.StatusRecord(option_id="opt-c", derived=boardstatus.CLAIMED),
        )

    def test_second_set_overwrites_the_first(self):
        self.store.record_status_set("acme/app", 7, "opt-c", boardstatus.CLAIMED, now=1000.0)
        self.store.record_status_set("acme/app", 7, "opt-r", boardstatus.REVIEW, now=2000.0)
        self.assertEqual(
            self.store.status_set_of("acme/app", 7),
            boardstatus.StatusRecord(option_id="opt-r", derived=boardstatus.REVIEW),
        )


REPO = "acme/app"
T0 = 1_800_000_100.0

OPTIONS = {"opt-a": "Available", "opt-c": "Claimed", "opt-r": "In review", "opt-d": "Done"}


def schema(options=None):
    return BoardSchema(
        project_id="PVT_1",
        fields={
            "board_status": FieldDef(
                id="F_wf", kind="single_select", options=dict(options or OPTIONS)
            )
        },
    )


def card(number, state="open", status="", **over):
    params = {"repo": REPO, "number": number, "state": state}
    if status or "extra" not in over:
        params["extra"] = {"board_status": status} if status else {}
    params.update(over)
    return reading(**params)


class FakeStatusReader:
    """A board-reader double for the status step: scripted pulls, recorded writes."""

    def __init__(self, *, limit=20, ids=None, pulls=None, write_errors=None, pulls_errors=None):
        self.board_status_limit = limit
        self.board_status_ids = dict(ids) if ids is not None else dict(IDS)
        self.pulls = dict(pulls or {})
        self.write_errors = dict(write_errors or {})
        self.pulls_errors = dict(pulls_errors or {})
        self.writes = []
        self.pull_calls = []

    def linked_pulls(self, repo, number):
        self.pull_calls.append((repo, number))
        if (repo, number) in self.pulls_errors:
            raise self.pulls_errors[(repo, number)]
        return list(self.pulls.get((repo, number), ()))

    def write_option(self, repo, number, option_id):
        self.writes.append((repo, number, option_id))
        if (repo, number) in self.write_errors:
            raise self.write_errors[(repo, number)]


class BoardStepReader(FakeStatusReader):
    """A full pass double: feed, poll and adopt plus the status seams."""

    board_poll_pages = 5

    def __init__(self, items, **kw):
        super().__init__(**kw)
        self._items = list(items)
        self.adopt_calls = []

    def board_schema(self):
        return schema()

    def changes(self, repo, cursor):
        return [], None

    def poll(self, found_schema, *, after=None, max_pages=5):
        return list(self._items), None

    def adopt_missing(self, project_id, repo, present, *, limit, now):
        self.adopt_calls.append((project_id, repo, set(present), limit))
        return iface.BoardAdopt((), (), (), None)


class StatusStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-status-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def sync_cards(self, cards, reader=None, **over):
        params = {"now": T0}
        params.update(over)
        return sync.sync_board_status(
            self.store, reader or FakeStatusReader(), schema(), REPO, list(cards), **params
        )

    def seed(self, number, agent=None):
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=number,
                title="Title",
                body="Body",
                state="open",
                author="alice",
                url="https://example.com/acme/app",
                tracker_updated_at=T0,
            ),
            operators=["alice"],
            now=T0,
        )
        if agent is not None:
            self.store.claim(REPO, number, agent, "host-1", now=T0)

    def test_closed_card_writes_done(self):
        reader = FakeStatusReader()
        outcome = self.sync_cards([card(7, state="closed", status="Available")], reader=reader)
        self.assertEqual(outcome.written, (f"{REPO}#7",))
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-d", derived=boardstatus.DONE),
        )

    def test_open_card_with_a_linked_pull_writes_review(self):
        reader = FakeStatusReader(pulls={(REPO, 7): [object()]})
        outcome = self.sync_cards([card(7, status="Available")], reader=reader)
        self.assertEqual(outcome.written, (f"{REPO}#7",))
        self.assertEqual(reader.writes, [(REPO, 7, "opt-r")])

    def test_claimed_card_writes_claimed(self):
        self.seed(7, agent="agent-1")
        reader = FakeStatusReader()
        outcome = self.sync_cards([card(7, status="Available")], reader=reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-c")])
        self.assertEqual(outcome.written, (f"{REPO}#7",))

    def test_plain_card_writes_available(self):
        reader = FakeStatusReader()
        self.sync_cards([card(7, status="Claimed")], reader=reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-a")])

    def test_closed_card_skips_the_pull_check(self):
        reader = FakeStatusReader()
        self.sync_cards([card(7, state="closed", status="Available")], reader=reader)
        self.assertEqual(reader.pull_calls, [])

    def test_pull_beats_a_live_claim(self):
        self.seed(7, agent="agent-1")
        reader = FakeStatusReader(pulls={(REPO, 7): [object()]})
        self.sync_cards([card(7, status="Claimed")], reader=reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-r")])

    def test_matching_card_adopts_a_record_without_a_write(self):
        reader = FakeStatusReader()
        outcome = self.sync_cards([card(7, status="Available")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-a", derived=boardstatus.AVAILABLE),
        )

    def test_manual_move_wins_until_the_derived_state_changes(self):
        self.store.record_status_set(REPO, 7, "opt-c", boardstatus.CLAIMED, now=T0)
        self.seed(7, agent="agent-1")
        reader = FakeStatusReader()
        outcome = self.sync_cards([card(7, status="In review")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-c", derived=boardstatus.CLAIMED),
        )

    def test_manual_move_releases_when_the_derived_state_changes(self):
        self.store.record_status_set(REPO, 7, "opt-a", boardstatus.AVAILABLE, now=T0)
        self.seed(7, agent="agent-1")
        reader = FakeStatusReader()
        self.sync_cards([card(7, status="Done")], reader=reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-c")])

    def test_partial_map_syncs_only_what_it_names(self):
        reader = FakeStatusReader(ids={boardstatus.DONE: "opt-d"})
        outcome = self.sync_cards(
            [card(7, state="closed", status="Available"), card(8, status="Available")],
            reader=reader,
        )
        self.assertEqual(outcome.written, (f"{REPO}#7",))
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])

    def test_unmapped_project_changes_nothing(self):
        reader = FakeStatusReader(ids={})
        outcome = self.sync_cards([card(7, state="closed", status="Available")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(outcome.errors, ())
        self.assertEqual(outcome.status_errors, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.pull_calls, [])
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_reader_without_a_status_limit_changes_nothing(self):
        reader = FakeStatusReader(limit=None)
        outcome = self.sync_cards([card(7, state="closed", status="Available")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.pull_calls, [])

    def test_schema_without_a_status_column_changes_nothing(self):
        reader = FakeStatusReader()
        outcome = sync.sync_board_status(
            self.store,
            reader,
            BoardSchema(project_id="PVT_1", fields={}),
            REPO,
            [card(7, state="closed", status="Available")],
            now=T0,
        )
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.pull_calls, [])

    def test_zero_limit_syncs_nothing(self):
        reader = FakeStatusReader(limit=0)
        outcome = self.sync_cards([card(7, state="closed", status="Available")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.pull_calls, [])

    def test_per_pass_cap_leaves_the_rest_for_next_pass(self):
        reader = FakeStatusReader(limit=1)
        outcome = self.sync_cards(
            [
                card(7, state="closed", status="Available"),
                card(8, state="closed", status="Available"),
            ],
            reader=reader,
        )
        self.assertEqual(outcome.written, (f"{REPO}#7",))
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])
        self.assertIsNone(self.store.status_set_of(REPO, 8))
        outcome = self.sync_cards(
            [card(7, state="closed", status="Done"), card(8, state="closed", status="Available")],
            reader=reader,
        )
        self.assertEqual(outcome.written, (f"{REPO}#8",))

    def test_failed_write_reports_and_continues(self):
        reader = FakeStatusReader(write_errors={(REPO, 7): iface.TrackerError("locked")})
        outcome = self.sync_cards(
            [
                card(7, state="closed", status="Available"),
                card(8, state="closed", status="Available"),
            ],
            reader=reader,
        )
        self.assertEqual(outcome.written, (f"{REPO}#8",))
        self.assertEqual(len(outcome.status_errors), 1)
        self.assertIn(f"{REPO}#7", outcome.status_errors[0])
        self.assertEqual(outcome.errors, ())
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_rate_limit_mid_pass_halts_with_an_outage(self):
        reader = FakeStatusReader(
            write_errors={(REPO, 8): iface.TrackerRateLimited("slow down", 45.0)}
        )
        outcome = self.sync_cards(
            [
                card(7, state="closed", status="Available"),
                card(8, state="closed", status="Available"),
                card(9, state="closed", status="Available"),
            ],
            reader=reader,
        )
        self.assertEqual(outcome.written, (f"{REPO}#7",))
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("45", outcome.errors[0])
        self.assertIsNone(self.store.status_set_of(REPO, 9))
        self.assertNotIn((REPO, 9, "opt-d"), reader.writes)

    def test_pull_rate_limit_halts_with_an_outage(self):
        reader = FakeStatusReader(
            pulls_errors={(REPO, 7): iface.TrackerRateLimited("slow down", 30.0)}
        )
        outcome = self.sync_cards([card(7, status="Available")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("30", outcome.errors[0])
        self.assertEqual(reader.writes, [])

    def test_pull_failure_reports_and_continues(self):
        reader = FakeStatusReader(pulls_errors={(REPO, 7): iface.TrackerError("gone")})
        outcome = self.sync_cards(
            [card(7, status="Available"), card(8, status="Claimed")], reader=reader
        )
        self.assertEqual(outcome.written, (f"{REPO}#8",))
        self.assertEqual(len(outcome.status_errors), 1)

    def test_unresolvable_current_status_writes_derived(self):
        reader = FakeStatusReader()
        self.sync_cards([card(7, status="Renamed")], reader=reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-a")])

    def test_pull_request_cards_are_skipped(self):
        reader = FakeStatusReader()
        outcome = self.sync_cards(
            [card(7, state="closed", status="Available", kind="pr")], reader=reader
        )
        self.assertEqual(outcome.written, ())
        self.assertEqual(outcome.status_errors, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.pull_calls, [])
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_duplicate_option_names_read_as_unknown(self):
        reader = FakeStatusReader()
        outcome = sync.sync_board_status(
            self.store,
            reader,
            schema({**OPTIONS, "opt-x": "Available"}),
            REPO,
            [card(7, status="Available")],
            now=T0,
        )
        self.assertEqual(reader.writes, [(REPO, 7, "opt-a")])
        self.assertEqual(outcome.written, (f"{REPO}#7",))


class BoardStepStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-status-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def test_board_step_syncs_status_from_the_poll(self):
        reader = BoardStepReader([card(7, state="closed", status="Available")])
        result = sync.sync_board_repo(
            self.store,
            reader,
            schema(),
            REPO,
            operators=["alice"],
            now=T0,
            max_pages=5,
        )
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])
        self.assertEqual(result.status_errors, ())
        self.assertEqual(result.errors, ())
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-d", derived=boardstatus.DONE),
        )

    def test_board_step_without_a_status_reader_stays_read_only(self):
        reader = BoardStepReader([card(7, state="closed", status="Available")], ids={})
        result = sync.sync_board_repo(
            self.store,
            reader,
            schema(),
            REPO,
            operators=["alice"],
            now=T0,
            max_pages=5,
        )
        self.assertEqual(reader.writes, [])
        self.assertEqual(result.status_errors, ())
        self.assertIsNone(self.store.status_set_of(REPO, 7))


class DriveStatusTest(unittest.TestCase):
    def setUp(self):
        from korapy import drive

        self.drive = drive
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-status-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def seed(self, number):
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=number,
                title="Title",
                body="Body",
                state="open",
                author="alice",
                url="https://example.com/acme/app",
                tracker_updated_at=T0 - 100,
            ),
            operators=["alice"],
            now=T0 - 100,
        )

    def test_sync_all_runs_the_status_step(self):
        self.seed(7)
        reader = BoardStepReader([card(7, state="closed", status="Available")])
        seen, errors, store_errors, refused, adopt_errors, status_errors = self.drive.sync_all(
            self.store, lambda repo: reader, operators=["alice"], now=T0
        )
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])
        self.assertEqual(status_errors, [])
        self.assertEqual(errors, [])
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-d", derived=boardstatus.DONE),
        )

    def test_sync_all_carries_status_failures_out(self):
        self.seed(7)
        reader = BoardStepReader(
            [card(7, state="closed", status="Available")],
            write_errors={(REPO, 7): iface.TrackerError("locked")},
        )
        seen, errors, store_errors, refused, adopt_errors, status_errors = self.drive.sync_all(
            self.store, lambda repo: reader, operators=["alice"], now=T0
        )
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])
        self.assertEqual(len(status_errors), 1)
        self.assertIn(f"{REPO}#7", status_errors[0])
        self.assertEqual(errors, [])
