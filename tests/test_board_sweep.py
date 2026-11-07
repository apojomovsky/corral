"""`korapy board sweep`: the drift moves listed and written on demand.

Plan mode lists every out-of-step card without writing; apply mode
runs the board step uncapped until a round fixes nothing. Fakes only:
a scripted board tracker plus a real board store on a scratch file,
no ``gh``, no network, no real state dir.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from korapy import board, boardcli, boardstatus, sync
from korapy import tracker as iface
from korapy.github.board import BoardSchema, FieldDef
from korapy.trackers import SetupEntry
from tests.test_tracker import reading


REPO = "example/demo"
T0 = 1_800_000_100.0

IDS = {
    boardstatus.AVAILABLE: "opt-a",
    boardstatus.CLAIMED: "opt-c",
    boardstatus.REVIEW: "opt-r",
    boardstatus.DONE: "opt-d",
}

OPTIONS = {"opt-a": "Available", "opt-c": "Claimed", "opt-r": "In review", "opt-d": "Done"}


def schema():
    return BoardSchema(
        project_id="PVT_1",
        fields={"board_status": FieldDef(id="F_wf", kind="single_select", options=dict(OPTIONS))},
    )


def card(number, state="open", status="", updated_at=T0, **over):
    params = {"repo": REPO, "number": number, "state": state, "updated_at": updated_at}
    if status or "extra" not in over:
        params["extra"] = {"board_status": status} if status else {}
    params.update(over)
    return reading(**params)


class FakeBoardTracker:
    """A scripted board tracker: poll pages, pulls, recorded writes."""

    board_poll_pages = 5

    def __init__(
        self,
        items,
        *,
        limit=20,
        adopt_limit=20,
        ids=None,
        pulls=None,
        added=(),
        refused=(),
        stopped=None,
        write_errors=None,
        schema_error=None,
        poll_error=None,
        pulls_error=None,
        pages=None,
    ):
        self._items = list(items)
        self._pages = pages
        self.board_status_limit = limit
        self.board_adopt_limit = adopt_limit
        self.board_status_ids = dict(ids) if ids is not None else dict(IDS)
        self.pulls = dict(pulls or {})
        self.added = list(added)
        self.refused = list(refused)
        self.stopped = stopped
        self.write_errors = dict(write_errors or {})
        self.schema_error = schema_error
        self.poll_error = poll_error
        self.pulls_error = pulls_error
        self.writes = []
        self.write_option_calls = []
        self.adopt_calls = []
        self.pulls_calls = []

    def board_schema(self):
        if self.schema_error is not None:
            raise self.schema_error
        return schema()

    def poll(self, found_schema, *, after=None, max_pages=5):
        if self.poll_error is not None:
            raise self.poll_error
        if self._pages is not None:
            for token, items, resume in self._pages:
                if token == after:
                    return list(items), resume
            raise AssertionError(f"unexpected poll cursor {after!r}")
        return list(self._items), None

    def pulls_index(self, repo, max_calls):
        self.pulls_calls.append((repo, max_calls))
        if self.pulls_error is not None:
            raise self.pulls_error
        by_issue = {}
        for (key_repo, number), linked in self.pulls.items():
            if key_repo == repo:
                by_issue[number] = list(linked)
        return iface.PullsIndex(by_issue, 1, True)

    def write_option(self, repo, number, option_id):
        self.writes.append((repo, number, option_id))
        self.write_option_calls.append((repo, number, option_id))
        if (repo, number) in self.write_errors:
            raise self.write_errors[(repo, number)]

    def adopt_missing(self, project_id, repo, present, *, limit, now):
        self.adopt_calls.append((project_id, repo, set(present), limit))
        if limit is not None and limit <= 0:
            return iface.BoardAdopt((), (), (), None)
        # NOTE: BoardAdopt is (added, kept, refused, stopped): refused
        # lines ride third, kept stays empty in this fake.
        if not hasattr(self, "_adopted_done"):
            self._adopted_done = set()
        present_numbers = {number for key_repo, number in present if key_repo == repo}
        added = []
        for name in self.added:
            key_repo, _, number = name.rpartition("#")
            if (
                key_repo != repo
                or not number.isdigit()
                or int(number) in present_numbers
                or name in self._adopted_done
            ):
                continue
            added.append(name)
            self._adopted_done.add(name)
            if limit is not None and len(added) >= limit:
                break
        return iface.BoardAdopt(tuple(added), (), tuple(self.refused), self.stopped)


class OpenLock:
    """A free drive lock: the default seam for sweep tests not about locking."""

    def __init__(self):
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return contextlib.nullcontext()


class WatchingBoardTracker(FakeBoardTracker):
    """Records whether the drive lock was held while the poll ran."""

    def __init__(self, items, *, lock, seen, **over):
        super().__init__(items, **over)
        self._lock = lock
        self._seen = seen

    def poll(self, found_schema, *, after=None, max_pages=5):
        self._seen["held"] = self._lock.held
        return super().poll(found_schema, after=after, max_pages=max_pages)


class NoAdoptTracker(FakeBoardTracker):
    """A board tracker with no adopt seam: missing issues are refused."""

    adopt_missing = None  # type: ignore[assignment]


class NeverAppearingTracker(FakeBoardTracker):
    """An adopt that never lands: every round looks like progress.

    The poll never shows the adopted issue, so the sweep would loop
    forever without the round cap.
    """

    def poll(self, found_schema, *, after=None, max_pages=5):
        return [], None

    def adopt_missing(self, project_id, repo, present, *, limit, now):
        self.adopt_calls.append((project_id, repo, set(present), limit))
        return iface.BoardAdopt((f"{repo}#8",), (), (), None)


_DEFAULT_LOCK = object()


def entry(name="app", tracker_repo=REPO, variant="board"):
    return SetupEntry(
        name=name,
        tracker_repo=tracker_repo,
        operators=["alice"],
        variant=variant,
        board=object(),
        max_pages=5,
        adopt_limit=20,
    )


class SweepCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-board-sweep-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "board.sqlite")
        with open(os.path.join(self.tmp.name, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                '[[repo]]\nname = "app"\n[repo.tracker]\nrepo = "example/demo"\n'
                '[repo.clone]\npath = "app"\n'
                '[[repo]]\nname = "api"\n[repo.tracker]\nrepo = "example/api"\n'
                '[repo.clone]\npath = "api"\n'
            )
        self.out = io.StringIO()
        self.err = io.StringIO()

    def open_store(self):
        return board.open_store(self.path)

    def seed(self, number, state="open"):
        store = board.open_store(self.path)
        try:
            store.upsert_item(
                board.Item(
                    repo=REPO,
                    number=number,
                    title="Title",
                    body="Body",
                    state=state,
                    author="alice",
                    url="https://example.com/x",
                    tracker_updated_at=T0,
                ),
                operators=["alice"],
                now=T0,
            )
        finally:
            store.close()

    def sweep(
        self,
        entries,
        trackers,
        *,
        plan=True,
        names=None,
        lock=_DEFAULT_LOCK,
        lock_path="/state/drive.lock",
    ):
        table = {candidate.name: tracker for candidate, tracker in zip(entries, trackers)}

        def entry_factory(name):
            for candidate in entries:
                if candidate.name == name:
                    return candidate
            raise ValueError(f"unknown repo {name!r}")

        if lock is _DEFAULT_LOCK:
            lock = OpenLock()
        return boardcli.sweep_root(
            self.tmp.name,
            names if names is not None else [entry.name for entry in entries],
            entry_factory=entry_factory,
            tracker_factory=lambda entry: table[entry.name],
            open_store=self.open_store,
            out=self.out,
            err=self.err,
            now=T0,
            plan=plan,
            lock=lock,
            lock_path=lock_path,
        )


class PlanTest(SweepCase):
    def test_clean_board_plans_nothing_and_exits_zero(self):
        self.seed(7)
        code = self.sweep([entry()], [FakeBoardTracker([card(7, status="Available")])], plan=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")

    def test_drifted_card_lists_its_state_and_move_and_exits_one(self):
        self.seed(7, state="closed")
        code = self.sweep(
            [entry()],
            [FakeBoardTracker([card(7, state="closed", status="Available")])],
            plan=True,
        )
        self.assertEqual(code, 1)
        text = self.out.getvalue()
        self.assertIn(f"{REPO}#7: state closed, set Status to 'Done'", text)

    def test_missing_open_issue_lists_an_add_move(self):
        self.seed(8)
        code = self.sweep([entry()], [FakeBoardTracker([card(7, status="Available")])], plan=True)
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}#8: state open, add to board", self.out.getvalue())

    def test_plan_writes_nothing(self):
        self.seed(7, state="closed")
        tracker = FakeBoardTracker([card(7, state="closed", status="Available")])
        self.sweep([entry()], [tracker], plan=True)
        self.assertEqual(tracker.writes, [])
        store = board.open_store(self.path)
        try:
            self.assertEqual(store.drift_flags(), [])
        finally:
            store.close()

    def test_unreachable_tracker_exits_three(self):
        code = self.sweep(
            [entry()],
            [FakeBoardTracker([], poll_error=iface.TrackerError("boom"))],
            plan=True,
        )
        self.assertEqual(code, 3)
        self.assertIn("boom", self.err.getvalue())

    def test_rejected_poll_exits_one(self):
        code = self.sweep(
            [entry()],
            [FakeBoardTracker([], poll_error=iface.TrackerRejected("nope"))],
            plan=True,
        )
        self.assertEqual(code, 1)

    def test_unknown_repo_exits_four(self):
        code = boardcli.sweep_root(
            self.tmp.name,
            ["ghost"],
            entry_factory=lambda name: (_ for _ in ()).throw(ValueError("unknown")),
            tracker_factory=lambda entry: None,
            open_store=self.open_store,
            out=self.out,
            err=self.err,
            now=T0,
            plan=True,
            git=None,
            lock=None,
            lock_path="/state/drive.lock",
        )
        self.assertEqual(code, 4)

    def test_labels_variant_exits_four(self):
        code = self.sweep([entry(variant="labels")], [FakeBoardTracker([])], plan=True)
        self.assertEqual(code, 4)
        self.assertIn("labels", self.err.getvalue())

    def test_tracker_spelling_resolves_to_the_entry(self):
        code = self.sweep(
            [entry()],
            [FakeBoardTracker([card(7, status="Available")])],
            plan=True,
            names=[REPO],
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")


class ApplyTest(SweepCase):
    def test_apply_sets_the_status_and_clears_the_flag(self):
        self.seed(7, state="closed")
        tracker = FakeBoardTracker([card(7, state="closed", status="Available")])
        code = self.sweep([entry()], [tracker], plan=False)
        self.assertEqual(code, 0)
        self.assertEqual(tracker.writes, [(REPO, 7, "opt-d")])
        self.assertIn(f"set Status on {REPO}#7", self.out.getvalue())
        store = board.open_store(self.path)
        try:
            self.assertEqual(store.drift_flags(), [])
        finally:
            store.close()

    def test_apply_adopts_missing_issues(self):
        self.seed(8)
        tracker = FakeBoardTracker([card(7, status="Available")], added=[f"{REPO}#8"])
        code = self.sweep([entry()], [tracker], plan=False)
        self.assertEqual(code, 0)
        self.assertIn(f"added {REPO}#8 to the board", self.out.getvalue())

    def test_apply_runs_to_completion_without_the_cap(self):
        self.seed(7, state="closed")
        self.seed(9, state="closed")
        tracker = FakeBoardTracker(
            [
                card(7, state="closed", status="Available"),
                card(9, state="closed", status="Available"),
            ],
            limit=1,
        )
        code = self.sweep([entry()], [tracker], plan=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(tracker.writes), 2)

    def test_apply_outage_with_nothing_applied_exits_three(self):
        code = self.sweep(
            [entry()],
            [FakeBoardTracker([], poll_error=iface.TrackerError("boom"))],
            plan=False,
        )
        self.assertEqual(code, 3)

    def test_apply_refused_with_nothing_applied_exits_one(self):
        code = self.sweep(
            [entry()],
            [FakeBoardTracker([], poll_error=iface.TrackerRejected("nope"))],
            plan=False,
        )
        self.assertEqual(code, 1)

    def test_apply_failed_add_with_nothing_applied_exits_one(self):
        self.seed(8)
        tracker = FakeBoardTracker([card(7, status="Available")], refused=[f"{REPO}#8: locked"])
        code = self.sweep([entry()], [tracker], plan=False)
        self.assertEqual(code, 1)
        self.assertIn(REPO, self.err.getvalue())


class SweepLockTest(SweepCase):
    def test_apply_with_a_held_lock_exits_two_and_writes_nothing(self):
        from tests.test_reponame import HeldLock

        self.seed(7, state="closed")
        tracker = FakeBoardTracker([card(7, state="closed", status="Available")])
        code = self.sweep([entry()], [tracker], plan=False, lock=HeldLock())
        self.assertEqual(code, 2)
        self.assertIn("another drive pass", self.err.getvalue())
        self.assertIn("/state/drive.lock", self.err.getvalue())
        self.assertEqual(tracker.writes, [])
        self.assertEqual(tracker.adopt_calls, [])

    def test_apply_without_a_lock_fails_closed(self):
        self.seed(7, state="closed")
        tracker = FakeBoardTracker([card(7, state="closed", status="Available")])
        code = self.sweep([entry()], [tracker], plan=False, lock=None)
        self.assertEqual(code, 4)
        self.assertIn("lock", self.err.getvalue())
        self.assertEqual(tracker.writes, [])
        self.assertEqual(tracker.adopt_calls, [])

    def test_plan_never_takes_the_lock(self):
        from tests.test_reponame import HeldLock

        held = HeldLock()
        self.seed(7, state="closed")
        code = self.sweep(
            [entry()],
            [FakeBoardTracker([card(7, state="closed", status="Available")])],
            plan=True,
            lock=held,
        )
        self.assertEqual(code, 1)
        self.assertEqual(held.calls, [])

    def test_apply_writes_run_under_the_lock(self):
        from tests.test_reponame import RecordingLock

        rec = RecordingLock()
        seen = {}
        watching = WatchingBoardTracker(
            [card(7, state="closed", status="Available")], lock=rec, seen=seen
        )
        self.seed(7, state="closed")
        code = self.sweep([entry()], [watching], plan=False, lock=rec)
        self.assertEqual(code, 0)
        self.assertTrue(seen.get("held"))
        self.assertEqual(rec.paths, ["/state/drive.lock"])


class SweepAdoptSeamTest(SweepCase):
    def test_apply_without_an_adopt_seam_reports_refused(self):
        self.seed(8)
        tracker = NoAdoptTracker([card(7, status="Available")])
        code = self.sweep([entry()], [tracker], plan=False)
        self.assertEqual(code, 1)
        self.assertIn("adopt", self.err.getvalue())


class SharedPullsAccessorTest(SweepCase):
    """The plan and the pass resolve Status pulls through one public accessor."""

    def test_plan_and_pass_share_the_status_pulls_accessor(self):
        tracker = FakeBoardTracker([card(7, status="Available")])
        self.seed(7)
        seen = []
        real = sync.status_pulls

        def spy(reader, field_schema, repo, cards, *, pulls_max_calls, uncapped=False):
            seen.append((reader, repo, uncapped))
            return real(
                reader,
                field_schema,
                repo,
                cards,
                pulls_max_calls=pulls_max_calls,
                uncapped=uncapped,
            )

        store = self.open_store()
        try:
            with mock.patch.object(sync, "status_pulls", side_effect=spy):
                plan_code = self.sweep([entry()], [tracker], plan=True)
                sync.sync_board_repo(
                    store,
                    tracker,
                    schema(),
                    REPO,
                    operators=["alice"],
                    now=T0,
                    max_pages=5,
                )
        finally:
            store.close()
        self.assertEqual(plan_code, 0)
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0][0], tracker)
        self.assertIs(seen[1][0], tracker)
        self.assertTrue(seen[0][2])
        self.assertFalse(seen[1][2])
        self.assertEqual(len(tracker.pulls_calls), 2)

    def test_plan_with_only_closed_cards_lists_no_pulls(self):
        self.seed(7, state="closed")
        tracker = FakeBoardTracker([card(7, state="closed", status="Available")])
        code = self.sweep([entry()], [tracker], plan=True)
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}#7: state closed, set Status to 'Done'", self.out.getvalue())
        self.assertEqual(tracker.pulls_calls, [])

    def test_plan_with_an_open_card_lists_pulls_once(self):
        self.seed(7)
        tracker = FakeBoardTracker([card(7, status="Available")], pulls={(REPO, 7): [object()]})
        code = self.sweep([entry()], [tracker], plan=True)
        self.assertEqual(code, 1)
        self.assertIn(f"{REPO}#7: state open, set Status to 'In review'", self.out.getvalue())
        self.assertEqual(len(tracker.pulls_calls), 1)

    def test_plan_with_rate_limited_pulls_notes_and_lists_no_moves(self):
        self.seed(7)
        tracker = FakeBoardTracker(
            [card(7, status="Available")],
            pulls_error=iface.TrackerRateLimited("slow down", 45.0),
        )
        code = self.sweep([entry()], [tracker], plan=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            self.out.getvalue(),
            f"{REPO}#7: board pulls rate limited, retry after 45s (slow down)\n",
        )
        self.assertEqual(len(tracker.pulls_calls), 1)

    def test_plan_with_failing_pulls_notes_the_cause_and_lists_no_moves(self):
        self.seed(7)
        tracker = FakeBoardTracker(
            [card(7, status="Available")],
            pulls_error=iface.TrackerError("boom"),
        )
        code = self.sweep([entry()], [tracker], plan=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), f"{REPO}#7: boom\n")
        self.assertEqual(len(tracker.pulls_calls), 1)


class SweepCapTest(SweepCase):
    def test_adds_that_never_appear_stop_at_the_round_cap(self):
        self.seed(8)
        tracker = NeverAppearingTracker([])
        code = self.sweep([entry()], [tracker], plan=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(tracker.adopt_calls), boardcli.MAX_APPLY_ROUNDS)
        self.assertIn(f"{REPO}#8: state open, add to board", self.err.getvalue())
        self.assertIn("rounds", self.err.getvalue())

    def test_the_round_cap_is_generous(self):
        self.assertGreaterEqual(boardcli.MAX_APPLY_ROUNDS, 20)


if __name__ == "__main__":
    unittest.main()
