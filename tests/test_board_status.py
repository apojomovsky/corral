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
from unittest import mock

from korapy import board, boardstatus, settings, sync, trackers
from korapy import tracker as iface
from korapy.github.board import BoardSchema, FieldDef
from tests.test_board_fields import FakePacer
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    item_node,
    items_payload,
    rest_issue,
    schema_payload,
    select_field,
    select_value,
)
from tests.test_tracker import pr_payload, reading


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
    """A board-reader double for the status step: a scripted pulls index, recorded writes.

    Implements only the production seams the step reads: ``pulls_index``
    for the once-per-repo open-pull listing, ``write_option`` for the
    writes, and the ``board_status_limit``/``board_status_ids``
    attributes. There is deliberately no per-card pulls method: the
    step resolves pulls once per repo, and the fake cannot drift from
    that protocol again.
    """

    def __init__(
        self,
        *,
        limit=20,
        ids=None,
        pulls=None,
        write_errors=None,
        index_error=None,
        incomplete=False,
    ):
        self.board_status_limit = limit
        self.board_status_ids = dict(ids) if ids is not None else dict(IDS)
        self.pulls = dict(pulls or {})
        self.write_errors = dict(write_errors or {})
        self.index_error = index_error
        self.incomplete = incomplete
        self.writes = []
        self.index_calls = []

    def pulls_index(self, repo, max_calls):
        self.index_calls.append((repo, max_calls))
        if self.index_error is not None:
            raise self.index_error
        by_issue = {}
        for (key_repo, number), linked in self.pulls.items():
            if key_repo == repo:
                by_issue[number] = list(linked)
        return iface.PullsIndex(by_issue, 1, not self.incomplete)

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

    def sync_cards(self, cards, reader=None, *, pulls=frozenset(), **over):
        params = {"now": T0, "pulls": pulls}
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
        reader = FakeStatusReader()
        outcome = self.sync_cards([card(7, status="Available")], reader=reader, pulls={7})
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

    def test_closed_card_needs_no_pulls(self):
        reader = FakeStatusReader()
        outcome = self.sync_cards(
            [card(7, state="closed", status="Available")], reader=reader, pulls=None
        )
        self.assertEqual(outcome.written, (f"{REPO}#7",))
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])

    def test_step_never_reads_pulls_per_card(self):
        reader = FakeStatusReader()
        self.sync_cards([card(7, status="Available")], reader=reader)
        self.assertEqual(reader.index_calls, [])

    def test_pull_beats_a_live_claim(self):
        self.seed(7, agent="agent-1")
        reader = FakeStatusReader()
        self.sync_cards([card(7, status="Claimed")], reader=reader, pulls={7})
        self.assertEqual(reader.writes, [(REPO, 7, "opt-r")])

    def test_unknown_pulls_rest_the_open_card_with_the_cause(self):
        reader = FakeStatusReader()
        outcome = self.sync_cards(
            [card(7, status="Available")],
            reader=reader,
            pulls=None,
            pulls_error="board unreachable: gone",
        )
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(len(outcome.status_errors), 1)
        self.assertIn(f"{REPO}#7", outcome.status_errors[0])
        self.assertIn("gone", outcome.status_errors[0])
        self.assertEqual(outcome.errors, ())
        self.assertIsNone(self.store.status_set_of(REPO, 7))

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
        self.assertEqual(reader.index_calls, [])
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_reader_without_a_status_limit_changes_nothing(self):
        reader = FakeStatusReader(limit=None)
        outcome = self.sync_cards([card(7, state="closed", status="Available")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.index_calls, [])

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
        self.assertEqual(reader.index_calls, [])

    def test_zero_limit_syncs_nothing(self):
        reader = FakeStatusReader(limit=0)
        outcome = self.sync_cards([card(7, state="closed", status="Available")], reader=reader)
        self.assertEqual(outcome.written, ())
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.index_calls, [])

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

    def test_unknown_pulls_rest_every_open_card_and_spare_the_closed(self):
        reader = FakeStatusReader()
        outcome = self.sync_cards(
            [card(7, status="Available"), card(8, state="closed", status="Available")],
            reader=reader,
            pulls=None,
            pulls_error="board unreachable: gone",
        )
        self.assertEqual(outcome.written, (f"{REPO}#8",))
        self.assertEqual(len(outcome.status_errors), 1)
        self.assertIn(f"{REPO}#7", outcome.status_errors[0])
        self.assertEqual(outcome.errors, ())
        self.assertIsNone(self.store.status_set_of(REPO, 7))

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
        self.assertEqual(reader.index_calls, [])
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
            pulls=set(),
        )
        self.assertEqual(reader.writes, [(REPO, 7, "opt-a")])
        self.assertEqual(outcome.written, (f"{REPO}#7",))


class BoardStepStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-status-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def repo_sync(self, reader, **over):
        params = {"operators": ["alice"], "now": T0, "max_pages": 5}
        params.update(over)
        return sync.sync_board_repo(self.store, reader, schema(), REPO, **params)

    def test_board_step_resolves_pulls_once_per_repo(self):
        reader = BoardStepReader(
            [card(7, status="Available"), card(8, status="Claimed")],
            pulls={(REPO, 7): [object()]},
        )
        result = self.repo_sync(reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-r"), (REPO, 8, "opt-a")])
        self.assertEqual(reader.index_calls, [(REPO, sync.DEFAULT_PULLS_MAX_CALLS)])
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status_errors, ())

    def test_board_step_spends_the_shared_pulls_budget(self):
        from korapy import drive

        self.assertEqual(sync.DEFAULT_PULLS_MAX_CALLS, drive.DEFAULT_PULLS_MAX_CALLS)
        reader = BoardStepReader([card(7, status="Available")])
        self.repo_sync(reader, pulls_max_calls=7)
        self.assertEqual(reader.index_calls, [(REPO, 7)])

    def test_board_step_falls_back_to_the_default_budget(self):
        from korapy import drive

        reader = BoardStepReader([card(7, status="Available")])
        with mock.patch.object(
            sync.settings, "load", side_effect=settings.SettingsError("no host config")
        ):
            self.repo_sync(reader)
        self.assertEqual(reader.index_calls, [(REPO, drive.DEFAULT_PULLS_MAX_CALLS)])

    def test_board_step_rate_limited_pulls_halt_with_an_outage(self):
        reader = BoardStepReader(
            [card(7, status="Available")],
            index_error=iface.TrackerRateLimited("slow down", 30.0),
        )
        result = self.repo_sync(reader)
        self.assertEqual(reader.writes, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("30", result.errors[0])
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_board_step_unreadable_pulls_rest_the_open_and_spare_the_closed(self):
        reader = BoardStepReader(
            [card(7, status="Available"), card(8, state="closed", status="Available")],
            index_error=iface.TrackerError("board unreachable: gone"),
        )
        result = self.repo_sync(reader)
        self.assertEqual(reader.writes, [(REPO, 8, "opt-d")])
        self.assertEqual(len(result.status_errors), 1)
        self.assertIn(f"{REPO}#7", result.status_errors[0])
        self.assertIn("gone", result.status_errors[0])
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_board_step_capped_pulls_rest_the_open_cards(self):
        reader = BoardStepReader([card(7, status="Available")], incomplete=True)
        result = self.repo_sync(reader)
        self.assertEqual(reader.writes, [])
        self.assertEqual(len(result.status_errors), 1)
        self.assertIn("limited", result.status_errors[0])
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_board_step_closed_only_poll_lists_no_pulls(self):
        reader = BoardStepReader([card(7, state="closed", status="Available")])
        result = self.repo_sync(reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])
        self.assertEqual(reader.index_calls, [])
        self.assertEqual(result.status_errors, ())

    def test_board_step_unmapped_project_lists_no_pulls(self):
        reader = BoardStepReader([card(7, status="Available")], ids={})
        result = self.repo_sync(reader)
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.index_calls, [])
        self.assertEqual(result.status_errors, ())
        self.assertIsNone(self.store.status_set_of(REPO, 7))

    def test_board_step_zero_pulls_budget_lists_nothing(self):
        reader = BoardStepReader([card(7, status="Available")])
        result = self.repo_sync(reader, pulls_max_calls=0)
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.index_calls, [])
        self.assertEqual(len(result.status_errors), 1)
        self.assertIn("limited", result.status_errors[0])

    def test_board_step_non_pull_rows_are_not_in_review(self):
        reader = BoardStepReader(
            [card(7, status="Claimed")],
            pulls={(REPO, 7): [reading(repo=REPO, number=99, kind="issue")]},
        )
        result = self.repo_sync(reader)
        self.assertEqual(reader.writes, [(REPO, 7, "opt-a")])
        self.assertEqual(result.status_errors, ())

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
        seen, errors, store_errors, refused, adopt_errors, status_errors, adopted, written = (
            self.drive.sync_all(self.store, lambda repo: reader, operators=["alice"], now=T0)
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
        seen, errors, store_errors, refused, adopt_errors, status_errors, adopted, written = (
            self.drive.sync_all(self.store, lambda repo: reader, operators=["alice"], now=T0)
        )
        self.assertEqual(reader.writes, [(REPO, 7, "opt-d")])
        self.assertEqual(len(status_errors), 1)
        self.assertIn(f"{REPO}#7", status_errors[0])
        self.assertEqual(errors, [])


STATUS_OPTIONS = [("opt-a", "Available"), ("opt-c", "Claimed"), ("opt-r", "In review")]


def status_config(**over):
    params = {
        "status_available": "opt-a",
        "status_claimed": "opt-c",
        "status_review": "opt-r",
        "status_done": "opt-d",
    }
    params.update(over)
    return config(**params)


def status_schema_payload():
    others = [field for field in full_schema_fields() if field["name"] != "Status"]
    return schema_payload(
        *others,
        select_field("F_wf", "Status", [*STATUS_OPTIONS, ("opt-d", "Done")]),
    )


def status_poll_node(status_id="opt-a", status_name="Available"):
    return item_node(7, repo=REPO, values=[select_value("F_wf", "Status", status_id, status_name)])


class RealTrackerStatusTest(unittest.TestCase):
    """The status step against a real ``BoardBackedTracker`` over canned payloads.

    Guards the production seam: the step resolves pulls through
    ``pulls_index``, the only pulls method the real tracker exposes, so
    an open card with an open linked pull derives In review instead of
    raising ``AttributeError``. Canned payloads only: ``BoardFakeRun``
    answers GraphQL and REST at the injected ``run`` seam, ``FakePacer``
    stands in for the shared bucket, and the store is a temporary file.
    """

    PULLS_PATH = f"repos/{REPO}/pulls?state=open&sort=updated&direction=asc&per_page=100"
    FEED_PATH = f"repos/{REPO}/issues?state=all&sort=updated&direction=asc&per_page=100"
    WRITE_MARKER = "BoardWriteSelect"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-status-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def script_schema(self, run):
        run.script("BoardSchemaByNumber", [status_schema_payload()])
        return run

    def script_poll(self, run, pages=1, **node_over):
        run.script("BoardItems", [items_payload([status_poll_node(**node_over)])] * pages)
        rest_issue(run, 7, repo=REPO)
        return run

    def script_pulls(self, run, pulls):
        run.add_rest(self.PULLS_PATH, pulls)
        return run

    def script_mutation(self, run):
        run.script(
            self.WRITE_MARKER,
            [{"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI-7"}}}],
        )
        return run

    def backed_tracker(self, run, **over):
        params = {"status_limit": 20}
        params.update(over)
        return trackers.BoardBackedTracker(
            status_config(), run, pacer=FakePacer(), max_pages=5, **params
        )

    def test_board_step_derives_in_review_through_pulls_index(self):
        run = BoardFakeRun()
        self.script_schema(run)
        self.script_poll(run, pages=2)
        self.script_pulls(run, [pr_payload(9, "Fixes things.\n\nCloses #7")])
        self.script_mutation(run)
        tracker = self.backed_tracker(run)
        result = sync.sync_board_repo(
            self.store,
            tracker,
            tracker.board_schema(),
            REPO,
            operators=["alice"],
            now=T0,
            max_pages=5,
            pulls_max_calls=20,
        )
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status_errors, ())
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-r", derived=boardstatus.REVIEW),
        )
        found = run.graphql_variables(self.WRITE_MARKER)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["option"], "opt-r")

    def test_board_step_closed_card_lists_no_pulls(self):
        run = BoardFakeRun()
        self.script_schema(run)
        self.script_poll(run, pages=2, status_id="opt-a", status_name="Available")
        rest_issue(run, 7, repo=REPO, state="closed")
        self.script_mutation(run)
        tracker = self.backed_tracker(run)
        result = sync.sync_board_repo(
            self.store,
            tracker,
            tracker.board_schema(),
            REPO,
            operators=["alice"],
            now=T0,
            max_pages=5,
            pulls_max_calls=20,
        )
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status_errors, ())
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-d", derived=boardstatus.DONE),
        )
        self.assertEqual([args for args, _ in run.calls if args[1] == self.PULLS_PATH], [])

    def test_sync_all_derives_in_review_through_pulls_index(self):
        from korapy import drive

        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=7,
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
        run = BoardFakeRun()
        self.script_schema(run)
        self.script_poll(run, pages=2)
        self.script_pulls(run, [pr_payload(9, "Fixes things.\n\nCloses #7")])
        self.script_mutation(run)
        run.add_rest(self.FEED_PATH, [])
        tracker = self.backed_tracker(run)
        seen, errors, store_errors, refused, adopt_errors, status_errors, adopted, written = (
            drive.sync_all(self.store, lambda repo: tracker, operators=["alice"], now=T0)
        )
        self.assertEqual(errors, [])
        self.assertEqual(status_errors, [])
        self.assertEqual(
            self.store.status_set_of(REPO, 7),
            boardstatus.StatusRecord(option_id="opt-r", derived=boardstatus.REVIEW),
        )
        found = run.graphql_variables(self.WRITE_MARKER)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["option"], "opt-r")


class RecordingReader(BoardStepReader):
    """A pass double with an adopt cap and stateful adds for recording tests."""

    board_adopt_limit = 20

    def __init__(self, items, *, added=(), resume=None, **kw):
        super().__init__(items, **kw)
        self._added = list(added)
        self._added_done = set()
        self._resume = resume

    def poll(self, found_schema, *, after=None, max_pages=5):
        return list(self._items), self._resume

    def adopt_missing(self, project_id, repo, present, *, limit, now):
        self.adopt_calls.append((project_id, repo, set(present), limit))
        if limit is not None and limit <= 0:
            return iface.BoardAdopt((), (), (), None)
        present_numbers = {number for key_repo, number in present if key_repo == repo}
        added = []
        for name in self._added:
            key_repo, _, number = name.rpartition("#")
            if (
                key_repo != repo
                or not number.isdigit()
                or int(number) in present_numbers
                or name in self._added_done
            ):
                continue
            added.append(name)
            self._added_done.add(name)
            if limit is not None and len(added) >= limit:
                break
        return iface.BoardAdopt(tuple(added), (), (), None)


class DriftRecordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-recording-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def seed(self, number, state="open"):
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=number,
                title="Title",
                body="Body",
                state=state,
                author="alice",
                url="https://example.com/acme/app",
                tracker_updated_at=T0 - 100,
            ),
            operators=["alice"],
            now=T0 - 100,
        )

    def run_board_step(self, reader, *, uncapped=False):
        return sync.sync_board_repo(
            self.store,
            reader,
            schema(),
            REPO,
            operators=["alice"],
            now=T0,
            max_pages=5,
            uncapped=uncapped,
        )

    def test_written_card_clears_its_flag(self):
        self.seed(7, state="closed")
        result = self.run_board_step(RecordingReader([card(7, state="closed", status="Available")]))
        self.assertEqual(result.status_written, (f"{REPO}#7",))
        self.assertEqual(self.store.drift_flags(), [])

    def test_deferred_card_stays_flagged_until_a_later_pass_writes_it(self):
        self.seed(7, state="closed")
        self.seed(9, state="closed")
        reader = RecordingReader(
            [
                card(7, state="closed", status="Available"),
                card(9, state="closed", status="Available"),
            ],
            limit=1,
        )
        first = self.run_board_step(reader)
        self.assertEqual(len(first.status_written), 1)
        self.assertEqual(self.store.drift_flags(), [(REPO, 9, T0)])
        second = self.run_board_step(reader)
        self.assertEqual(len(second.status_written), 1)
        self.assertEqual(
            set(first.status_written) | set(second.status_written), {f"{REPO}#7", f"{REPO}#9"}
        )
        self.assertEqual(self.store.drift_flags(), [])

    def test_uncapped_run_writes_everything_despite_no_caps(self):
        self.seed(7, state="closed")
        self.seed(9, state="closed")
        reader = RecordingReader(
            [
                card(7, state="closed", status="Available"),
                card(9, state="closed", status="Available"),
            ]
        )
        reader.board_status_limit = None
        result = self.run_board_step(reader, uncapped=True)
        self.assertEqual(len(result.status_written), 2)
        self.assertEqual(self.store.drift_flags(), [])

    def test_missing_open_issue_is_flagged_and_the_add_is_named(self):
        self.seed(7)
        self.seed(8)
        reader = RecordingReader([card(7, status="Available")], added=[f"{REPO}#8"])
        result = self.run_board_step(reader)
        self.assertEqual(result.adopted, (f"{REPO}#8",))
        self.assertEqual(self.store.drift_flags(), [(REPO, 8, T0)])

    def test_truncated_poll_leaves_missing_verdicts_alone(self):
        self.seed(7)
        self.seed(8)
        self.store.note_drift(REPO, 8, now=T0 - 10)
        reader = RecordingReader([card(7, status="Available")], resume="TOKEN")
        result = self.run_board_step(reader)
        self.assertEqual(result.adopted, ())
        self.assertEqual(self.store.drift_flags(), [(REPO, 8, T0 - 10)])

    def test_complete_poll_clears_gone_verdicts(self):
        self.seed(7)
        self.seed(9, state="closed")
        self.store.note_drift(REPO, 9, now=T0 - 10)
        self.run_board_step(RecordingReader([card(7, status="Available")]))
        self.assertEqual(self.store.drift_flags(), [])

    def test_manual_move_is_not_drift(self):
        self.seed(7)
        self.store.claim(REPO, 7, "bob", "host-1", now=T0 - 100)
        self.store.record_status_set(REPO, 7, "opt-c", "claimed", now=T0 - 100)
        result = self.run_board_step(RecordingReader([card(7, status="Available")]))
        self.assertEqual(result.status_written, ())
        self.assertEqual(self.store.drift_flags(), [])

    def test_unmapped_project_records_nothing(self):
        self.seed(7, state="closed")
        result = self.run_board_step(
            RecordingReader([card(7, state="closed", status="Available")], ids={})
        )
        self.assertEqual(result.status_written, ())
        self.assertEqual(self.store.drift_flags(), [])

    def test_disabled_caps_still_record_mapped_drift(self):
        self.seed(7, state="closed")
        reader = RecordingReader([card(7, state="closed", status="Available")], limit=0)
        result = self.run_board_step(reader)
        self.assertEqual(result.status_written, ())
        self.assertEqual(self.store.drift_flags(), [(REPO, 7, T0)])
