"""`korapy repo rename`: migrate the store after a GitHub repository rename.

A renamed tracker repository keeps its issues, so GitHub redirects the
old `owner/repo` and reads keep working; but the board store keys every
row by the old name, and search-based calls quietly return nothing for
it. The operator verb migrates the store's keys in one transaction,
refuses while anything is live, and leaves a backup behind. Fakes only:
a temporary store, in-memory registry and unit data, a fake tracker.
No `gh`, no systemd, no network, no real state dir.
"""

import io
import json
import os
import unittest

from korapy import board, registry, repodoctor
from korapy import tracker as iface
from tests.test_board import TempStoreTest, reading
from tests.test_doctor import env as doctor_env
from tests.test_tracker import FakeTracker

OLD = "acme/old"
NEW = "acme/new"
SHORT = "app"
NOW = 20000.0
HOLDER = "drive-a1b2c3d4-1"


def populated(store):
    """A store holding OLD rows: two items, a claim, queued writes, cursors, a Status record."""
    store.upsert_item(reading(OLD, 1), operators=["alice"], now=1000.0)
    store.upsert_item(reading(OLD, 2), operators=["alice"], now=1000.0)
    store.claim(OLD, 1, HOLDER, "host-1", now=1000.0)
    store.enqueue(OLD, 2, "agent", HOLDER, now=1000.0)
    store.set_cursor(OLD, "tracker", "2024-01-01T00:00:00Z")
    store.set_cursor(OLD, "board", "1700000000")
    store.record_status_set(OLD, 1, "opt-c", "claimed", now=1000.0)
    return store


class RenameCountsTest(TempStoreTest):
    def test_empty_store_counts_zero(self):
        store = self.open()
        self.assertEqual(
            store.rename_counts(OLD),
            {"items": 0, "claims": 0, "outbox": 0, "cursors": 0, "status_sets": 0},
        )

    def test_counts_cover_every_keyed_table(self):
        store = populated(self.open())
        self.assertEqual(
            store.rename_counts(OLD),
            {"items": 2, "claims": 1, "outbox": 2, "cursors": 2, "status_sets": 1},
        )

    def test_counts_are_per_repo(self):
        store = populated(self.open())
        self.assertEqual(
            store.rename_counts(NEW),
            {"items": 0, "claims": 0, "outbox": 0, "cursors": 0, "status_sets": 0},
        )


class RenameRepoTest(TempStoreTest):
    def test_migrate_moves_every_key_in_one_transaction(self):
        store = populated(self.open())
        counts = store.rename_repo(OLD, NEW)
        self.assertEqual(
            counts, {"items": 2, "claims": 1, "outbox": 2, "cursors": 2, "status_sets": 1}
        )
        self.assertEqual(
            store.rename_counts(OLD),
            {"items": 0, "claims": 0, "outbox": 0, "cursors": 0, "status_sets": 0},
        )
        self.assertEqual(store.rename_counts(NEW), counts)
        self.assertEqual(sorted(item.number for item in store.items(repo=NEW)), [1, 2])
        self.assertEqual(store.get_cursor(NEW, "tracker"), "2024-01-01T00:00:00Z")
        self.assertEqual(store.get_cursor(NEW, "board"), "1700000000")
        self.assertIsNone(store.status_set_of(OLD, 1))
        record = store.status_set_of(NEW, 1)
        self.assertEqual((record.option_id, record.derived), ("opt-c", "claimed"))

    def test_claim_keeps_agent_and_epoch(self):
        store = populated(self.open())
        store.rename_repo(OLD, NEW)
        held = store.claim_of(NEW, 1)
        self.assertEqual((held.agent, held.epoch), (HOLDER, 1))

    def test_claim_epoch_fences_the_next_claim(self):
        store = populated(self.open())
        store.rename_repo(OLD, NEW)
        store.release(NEW, 1, HOLDER, 1, now=NOW)
        nxt = store.claim(NEW, 1, "drive-ffff0000-1", "host-1", now=NOW)
        self.assertGreater(nxt.epoch, 1)
        with self.assertRaises(board.Claimed):
            store.release(NEW, 1, HOLDER, 1, now=NOW)

    def test_queued_writes_move_with_their_items(self):
        store = populated(self.open())
        store.rename_repo(OLD, NEW)
        queued = store.queued_writes()
        self.assertEqual(len(queued), 2)
        self.assertTrue(all(row.repo == NEW for row in queued))

    def test_second_run_changes_nothing(self):
        store = populated(self.open())
        store.rename_repo(OLD, NEW)
        again = store.rename_repo(OLD, NEW)
        self.assertEqual(
            again, {"items": 0, "claims": 0, "outbox": 0, "cursors": 0, "status_sets": 0}
        )
        self.assertEqual(
            store.rename_counts(NEW),
            {"items": 2, "claims": 1, "outbox": 2, "cursors": 2, "status_sets": 1},
        )

    def test_existing_target_name_refuses_without_merging(self):
        store = populated(self.open())
        store.upsert_item(reading(NEW, 9), operators=["alice"], now=1000.0)
        with self.assertRaises(board.RenameRefused):
            store.rename_repo(OLD, NEW)
        self.assertEqual(store.rename_counts(OLD)["items"], 2)
        self.assertEqual(sorted(item.number for item in store.items(repo=NEW)), [9])

    def test_refusal_is_a_usage_error(self):
        self.assertEqual(board.RenameRefused.exit_code, 4)

    def test_backup_into_holds_a_consistent_snapshot(self):
        store = populated(self.open())
        target = os.path.join(self.tmp.name, "snap.sqlite")
        self.assertEqual(store.backup_into(target), target)
        with board.open_store(target) as snap:
            self.assertEqual(snap.rename_counts(OLD)["items"], 2)
            self.assertEqual(snap.claim_of(OLD, 1).agent, HOLDER)


class ParseNamesTest(unittest.TestCase):
    def test_valid_pair_round_trips(self):
        from korapy import reponame

        self.assertEqual(reponame.parse_names(OLD, NEW), (OLD, NEW))

    def test_bad_shapes_are_refused(self):
        from korapy import reponame

        for bad in ("acme", "acme/old/extra", "", "acme/", "/old", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                reponame.parse_names(bad, NEW)
            with self.assertRaises(ValueError, msg=repr(bad)):
                reponame.parse_names(OLD, bad)


class BackupNameTest(unittest.TestCase):
    def test_names_sort_chronologically(self):
        from korapy import reponame

        first = reponame.backup_name(1000.0)
        second = reponame.backup_name(2000.0)
        self.assertLess(first, second)
        self.assertTrue(first.startswith("board-backup-"))
        self.assertTrue(first.endswith(".sqlite"))

    def test_backup_path_sits_beside_the_store(self):
        from korapy import reponame

        path = reponame.backup_path(os.path.join("state", "board.sqlite"), 1000.0)
        self.assertEqual(os.path.dirname(path), "state")
        self.assertEqual(os.path.basename(path), reponame.backup_name(1000.0))

    def test_prune_keeps_the_newest_five(self):
        from korapy import reponame

        tmp = self.tmp = __import__("tempfile").TemporaryDirectory(prefix="korapy-rename-backup-")
        self.addCleanup(tmp.cleanup)
        for index in range(7):
            name = reponame.backup_name(1000.0 + index)
            with open(os.path.join(tmp.name, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        removed = reponame.prune_backups(tmp.name)
        self.assertEqual(len(removed), 2)
        kept = sorted(os.listdir(tmp.name))
        self.assertEqual(len(kept), 5)
        self.assertIn(reponame.backup_name(1006.0), kept)

    def test_prune_ignores_foreign_files(self):
        from korapy import reponame

        tmp = __import__("tempfile").TemporaryDirectory(prefix="korapy-rename-keep-")
        self.addCleanup(tmp.cleanup)
        foreign = ["board.sqlite", "drive.lock", "board-backup-partial.sqlite"]
        for name in foreign:
            with open(os.path.join(tmp.name, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        self.assertEqual(reponame.prune_backups(tmp.name), [])
        self.assertEqual(sorted(os.listdir(tmp.name)), sorted(foreign))

    def test_suffixed_stamp_parses(self):
        from korapy import reponame

        self.assertEqual(
            reponame._backup_stamp("board-backup-20240101T000000Z-2.sqlite"),
            reponame._backup_stamp("board-backup-20240101T000000Z.sqlite"),
        )
        self.assertIsNotNone(reponame._backup_stamp("board-backup-20240101T000000Z-2.sqlite"))

    def test_prune_ages_suffixed_backups_like_the_others(self):
        from korapy import reponame

        tmp = __import__("tempfile").TemporaryDirectory(prefix="korapy-rename-suffix-")
        self.addCleanup(tmp.cleanup)
        names = [reponame.backup_name(1000.0 + index) for index in range(5)]
        names.append("board-backup-19700101T000015Z-2.sqlite")
        for name in names:
            with open(os.path.join(tmp.name, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        removed = reponame.prune_backups(tmp.name)
        self.assertEqual(removed, ["board-backup-19700101T000015Z-2.sqlite"])
        self.assertEqual(len(os.listdir(tmp.name)), 5)


class ProjectOwnersTest(unittest.TestCase):
    def test_short_resolving_to_old_names_its_project(self):
        from korapy import reponame

        self.assertEqual(
            reponame.project_owners({"demo": [SHORT], "other": ["aux"]}, {SHORT: OLD}, {OLD, NEW}),
            {"demo"},
        )

    def test_short_resolving_to_new_names_its_project(self):
        from korapy import reponame

        self.assertEqual(
            reponame.project_owners({"demo": [SHORT]}, {SHORT: NEW}, {OLD, NEW}),
            {"demo"},
        )

    def test_unmapped_and_foreign_shorts_name_nobody(self):
        from korapy import reponame

        self.assertEqual(
            reponame.project_owners({"demo": ["gone", "aux"]}, {"aux": "acme/other"}, {OLD, NEW}),
            set(),
        )

    def test_empty_inputs_name_nobody(self):
        from korapy import reponame

        self.assertEqual(reponame.project_owners({}, {}, {OLD, NEW}), set())
        self.assertEqual(reponame.project_owners(None, None, {OLD, NEW}), set())


def entry(agent_id, repo=SHORT, number=1, project=""):
    return {"id": agent_id, "ticket": {"project": project, "repo": repo, "number": number}}


class LiveBlockersTest(unittest.TestCase):
    def setUp(self):
        from korapy import reponame

        self.reponame = reponame

    def blockers(self, **over):
        params = {
            "claims": [],
            "entries": [],
            "live_units": frozenset(),
            "mapping": {SHORT: OLD},
        }
        params.update(over)
        return self.reponame.live_blockers(OLD, NEW, **params)

    def test_quiet_store_is_clear(self):
        self.assertEqual(self.blockers(), [])

    def test_live_agent_for_the_repo_blocks_naming_it(self):
        found = self.blockers(entries=[(entry("agent-1"), registry.LIVE)])
        self.assertEqual(len(found), 1)
        self.assertIn("agent-1", found[0])

    def test_stale_agent_blocks_like_a_live_one(self):
        found = self.blockers(entries=[(entry("agent-1"), registry.STALE)])
        self.assertEqual(len(found), 1)
        self.assertIn("agent-1", found[0])

    def test_finished_and_gone_agents_do_not_block(self):
        self.assertEqual(
            self.blockers(
                entries=[
                    (entry("agent-1"), registry.FINISHED),
                    (entry("agent-2"), registry.GONE),
                ]
            ),
            [],
        )

    def test_agent_for_another_repo_does_not_block(self):
        self.assertEqual(
            self.blockers(
                entries=[(entry("agent-1", repo="other", number=3), registry.LIVE)],
                mapping={SHORT: OLD, "other": "acme/other"},
            ),
            [],
        )

    def test_agent_for_the_new_name_blocks_too(self):
        found = self.blockers(
            entries=[(entry("agent-1"), registry.LIVE)],
            mapping={SHORT: NEW},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("agent-1", found[0])

    def test_claim_held_by_a_live_unit_blocks(self):
        found = self.blockers(
            claims=[(OLD, 1, HOLDER)],
            live_units=frozenset({(SHORT, 1)}),
            mapping={SHORT: OLD},
        )
        self.assertEqual(len(found), 1)
        self.assertIn(HOLDER, found[0])
        self.assertIn(f"{OLD}#1", found[0])

    def test_claims_join_routes_live_tickets_through_tracker_key(self):
        from unittest import mock

        from korapy import drive

        real = drive._tracker_key
        seen = []

        def spy(ticket, mapping):
            seen.append(ticket)
            return real(ticket, mapping)

        with mock.patch.object(drive, "_tracker_key", side_effect=spy):
            found = self.blockers(
                claims=[(OLD, 1, HOLDER)],
                live_units=frozenset({(SHORT, 1)}),
                mapping={SHORT: OLD},
            )
        self.assertEqual(len(found), 1)
        self.assertIn((SHORT, 1), seen)

    def test_live_unit_without_a_claim_does_not_block(self):
        self.assertEqual(
            self.blockers(live_units=frozenset({(SHORT, 2)})),
            [],
        )

    def test_released_claims_do_not_block(self):
        self.assertEqual(
            self.blockers(
                claims=[(OLD, 1, "")],
                live_units=frozenset({(SHORT, 1)}),
            ),
            [],
        )

    def test_unmapped_short_with_a_matching_number_blocks(self):
        found = self.blockers(
            claims=[(OLD, 1, HOLDER)],
            live_units=frozenset({(SHORT, 1)}),
            mapping={},
        )
        self.assertEqual(len(found), 1)
        self.assertIn(HOLDER, found[0])
        self.assertIn(f"{OLD}#1", found[0])

    def test_unmapped_live_entry_blocks_naming_it(self):
        found = self.blockers(
            claims=[(OLD, 1, HOLDER)],
            entries=[(entry("agent-1"), registry.LIVE)],
            live_units=frozenset(),
            mapping={},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("agent-1", found[0])

    def test_unmapped_stale_entry_blocks_naming_it(self):
        found = self.blockers(
            claims=[(OLD, 1, HOLDER)],
            entries=[(entry("agent-1"), registry.STALE)],
            live_units=frozenset(),
            mapping={},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("agent-1", found[0])

    def test_unmapped_entry_without_claims_blocks(self):
        found = self.blockers(
            entries=[(entry("agent-1"), registry.LIVE)],
            live_units=frozenset(),
            mapping={},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("agent-1", found[0])

    def test_unmapped_entry_holding_a_claim_blocks_despite_its_project(self):
        found = self.blockers(
            claims=[(OLD, 1, "agent-1")],
            entries=[(entry("agent-1", project="other"), registry.LIVE)],
            live_units=frozenset(),
            mapping={},
            owners={"demo"},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("agent-1", found[0])

    def test_unmapped_entry_for_an_unrelated_project_does_not_block(self):
        self.assertEqual(
            self.blockers(
                entries=[(entry("agent-1", repo="gone", number=9, project="other"), registry.LIVE)],
                live_units=frozenset(),
                mapping={},
                owners={"demo"},
            ),
            [],
        )

    def test_unrelated_project_live_unit_does_not_block(self):
        self.assertEqual(
            self.blockers(
                claims=[(OLD, 5, HOLDER)],
                live_units=frozenset({("other", "gone", 5)}),
                mapping={},
                owners={"demo"},
            ),
            [],
        )

    def test_same_project_live_unit_blocks(self):
        found = self.blockers(
            claims=[(OLD, 5, HOLDER)],
            live_units=frozenset({("demo", "gone", 5)}),
            mapping={},
            owners={"demo"},
        )
        self.assertEqual(len(found), 1)
        self.assertIn(HOLDER, found[0])

    def test_unit_without_a_project_blocks(self):
        found = self.blockers(
            claims=[(OLD, 5, HOLDER)],
            live_units=frozenset({("gone", 5)}),
            mapping={},
            owners={"demo"},
        )
        self.assertEqual(len(found), 1)
        self.assertIn(HOLDER, found[0])

    def test_empty_owners_fail_closed(self):
        found = self.blockers(
            claims=[(OLD, 5, HOLDER)],
            live_units=frozenset({("other", "gone", 5)}),
            mapping={},
            owners=set(),
        )
        self.assertEqual(len(found), 1)
        self.assertIn(HOLDER, found[0])

    def test_unmapped_short_with_another_number_does_not_block(self):
        self.assertEqual(
            self.blockers(
                claims=[(OLD, 1, HOLDER)],
                live_units=frozenset({(SHORT, 2)}),
                mapping={},
            ),
            [],
        )

    def test_live_unit_mapped_elsewhere_does_not_block(self):
        self.assertEqual(
            self.blockers(
                claims=[(OLD, 1, HOLDER)],
                live_units=frozenset({(SHORT, 1)}),
                mapping={SHORT: "acme/other"},
            ),
            [],
        )

    def test_blockers_sort_for_a_stable_report(self):
        found = self.blockers(
            claims=[(OLD, 2, "drive-b"), (OLD, 1, "drive-a")],
            live_units=frozenset({(SHORT, 1), (SHORT, 2)}),
            entries=[(entry("agent-9"), registry.LIVE)],
        )
        self.assertEqual(found, sorted(found))
        self.assertEqual(len(found), 3)


class CanonicalMatchesTest(unittest.TestCase):
    def test_same_name_matches(self):
        from korapy import reponame

        self.assertTrue(reponame.canonical_matches(OLD, OLD))

    def test_case_only_is_not_a_rename(self):
        from korapy import reponame

        self.assertTrue(reponame.canonical_matches("ACME/Old", "acme/old"))

    def test_another_name_does_not_match(self):
        from korapy import reponame

        self.assertFalse(reponame.canonical_matches(OLD, NEW))

    def test_missing_readings_are_unknown(self):
        from korapy import reponame

        self.assertIsNone(reponame.canonical_matches(OLD, None))
        self.assertIsNone(reponame.canonical_matches(None, NEW))
        self.assertIsNone(reponame.canonical_matches(None, None))


class UnitsExitTest(unittest.TestCase):
    def test_unreachable_returns_the_constant(self):
        from korapy import repocli, units

        self.assertEqual(repocli.UNREACHABLE, 3)
        self.assertEqual(repocli.units_exit(units.Unreachable("cannot reach systemd: boom")), 3)

    def test_other_units_errors_keep_their_code(self):
        from korapy import repocli, units

        self.assertEqual(repocli.units_exit(units.UnitsError("bad prefix")), 4)
        self.assertEqual(repocli.units_exit(units.UnitExists("taken")), 2)


class RepoHelpTest(unittest.TestCase):
    @staticmethod
    def production_repo_parser():
        """The `repo` parser from the parser `main` runs (not the test one)."""
        import argparse

        from korapy import cli

        parser = cli.build_parser()
        subs = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        return subs.choices["repo"]

    def test_parser_description_is_the_module_docstring(self):
        from korapy import repocli

        self.assertEqual(repocli.build_repo_parser().description, repocli.__doc__)

    def test_production_parser_description_is_the_module_docstring(self):
        from korapy import repocli

        self.assertEqual(self.production_repo_parser().description, repocli.__doc__)

    def test_help_names_stale_entries_as_exit_2_blockers(self):
        text = self.production_repo_parser().format_help()
        flat = " ".join(text.split())
        self.assertIn(
            "a live or stale agent entry or a live-held claim blocks the move",
            flat,
        )


class LosingStore:
    """A store that loses the rename race: reads delegate, the move refuses.

    The pre-check counts read empty for the target, then `rename_repo`
    refuses as if a concurrent write landed rows in between, so the CLI
    exercises its refused-after-backup path without real concurrency.
    """

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def rename_repo(self, old, new):
        raise board.RenameRefused(
            f"{new} already holds store rows: refusing a merge of two repositories"
        )


class FailingStore(LosingStore):
    """A store whose move fails late: the backup lands, then the move errors.

    Exercises the refused-after-backup path for a store failure that is
    not a merge refusal: the fresh backup must go, older ones must stay.
    """

    def rename_repo(self, old, new):
        raise board.StoreError(f"cannot move {old} to {new}: disk is full")


class HeldLock:
    """A drive lock another pass holds: acquiring it refuses."""

    def __init__(self, path="/state/drive.lock"):
        self.path = path
        self.calls = []

    def __call__(self, path):
        from korapy import drive

        self.calls.append(path)
        raise drive.LockHeld(f"another drive pass is already running (lock {path})")


class RecordingLock:
    """A free drive lock recording its span: proves what runs under it."""

    def __init__(self):
        self.paths = []
        self.depth = 0
        self.held = False

    def __call__(self, path):
        import contextlib

        outer = self

        @contextlib.contextmanager
        def guard():
            outer.paths.append(path)
            outer.depth += 1
            outer.held = True
            try:
                yield guard
            finally:
                outer.depth -= 1
                outer.held = outer.depth > 0

        return guard()


class RenameRootCase(TempStoreTest):
    def setUp(self):
        super().setUp()
        from korapy import repocli

        self.cmd = repocli.rename_root
        self.out = io.StringIO()
        self.err = io.StringIO()

    def rename(self, old=OLD, new=NEW, **over):
        params = {
            "store": self.store,
            "store_path": self.path,
            "registry_entries": [],
            "live_units": frozenset(),
            "mapping": {SHORT: OLD},
            "project_shorts": None,
            "owners": None,
            "now": NOW,
            "out": self.out,
            "err": self.err,
            "lock": RecordingLock(),
            "lock_path": "/state/drive.lock",
        }
        params.update(over)
        return self.cmd(old, new, **params)

    def setUpStore(self):
        self.store = populated(self.open())

    def backups(self):
        return sorted(
            name for name in os.listdir(self.tmp.name) if name.startswith("board-backup-")
        )

    def test_migrate_writes_keys_a_backup_and_the_config_step(self):
        self.setUpStore()
        code = self.rename()
        self.assertEqual(code, 0)
        self.assertEqual(self.store.rename_counts(OLD)["items"], 0)
        self.assertEqual(self.store.rename_counts(NEW)["items"], 2)
        self.assertEqual(len(self.backups()), 1)
        text = self.out.getvalue()
        self.assertIn(f"{OLD} -> {NEW}", text)
        self.assertIn("backup", text)
        self.assertIn("tracker.repo", text)
        self.assertIn("origin", text)
        self.assertIn("korapy doctor", text)

    def test_backup_holds_the_old_keys(self):
        self.setUpStore()
        self.assertEqual(self.rename(), 0)
        (name,) = self.backups()
        with board.open_store(os.path.join(self.tmp.name, name)) as snap:
            self.assertEqual(snap.rename_counts(OLD)["items"], 2)

    def test_plan_prints_counts_and_changes_nothing(self):
        self.setUpStore()
        code = self.rename(plan=True)
        self.assertEqual(code, 0)
        self.assertIn("2 items", self.out.getvalue())
        self.assertIn(f"{OLD} -> {NEW}", self.out.getvalue())
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.store.rename_counts(NEW)["items"], 0)
        self.assertEqual(self.backups(), [])

    def test_plan_names_the_backup_it_would_write(self):
        self.setUpStore()
        self.rename(plan=True)
        self.assertIn("board-backup-", self.out.getvalue())

    def test_plan_with_nothing_to_do_exits_one(self):
        self.store = self.open()
        code = self.rename(plan=True)
        self.assertEqual(code, 1)
        self.assertIn("nothing to do", self.out.getvalue())

    def test_second_run_is_a_noop(self):
        self.setUpStore()
        self.assertEqual(self.rename(), 0)
        out = io.StringIO()
        err = io.StringIO()
        code = self.rename(out=out, err=err)
        self.assertEqual(code, 1)
        self.assertIn("nothing to do", out.getvalue())
        self.assertEqual(len(self.backups()), 1)

    def test_existing_target_refuses_without_merging(self):
        self.setUpStore()
        self.store.upsert_item(reading(NEW, 9), operators=["alice"], now=1000.0)
        code = self.rename()
        self.assertEqual(code, 4)
        self.assertIn(NEW, self.err.getvalue())
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.backups(), [])

    def test_bad_names_are_a_usage_error(self):
        self.store = self.open()
        code = self.rename(old="not-a-repo", out=self.out, err=self.err)
        self.assertEqual(code, 4)
        self.assertIn("not-a-repo", self.err.getvalue())

    def test_same_name_is_nothing_to_do(self):
        self.setUpStore()
        code = self.rename(old=OLD, new=OLD)
        self.assertEqual(code, 1)
        self.assertIn("nothing to do", self.out.getvalue())
        self.assertEqual(self.backups(), [])

    def test_live_agent_refuses_naming_it(self):
        self.setUpStore()
        code = self.rename(
            registry_entries=[(entry("agent-1"), registry.LIVE)],
        )
        self.assertEqual(code, 2)
        self.assertIn("agent-1", self.err.getvalue())
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.backups(), [])

    def test_claim_held_by_a_live_unit_refuses(self):
        self.setUpStore()
        code = self.rename(live_units=frozenset({(SHORT, 1)}))
        self.assertEqual(code, 2)
        self.assertIn(HOLDER, self.err.getvalue())
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.backups(), [])

    def test_quiet_fleet_migrates(self):
        self.setUpStore()
        code = self.rename(
            registry_entries=[(entry("agent-9"), registry.FINISHED)],
            live_units=frozenset({(SHORT, 9)}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.store.rename_counts(NEW)["items"], 2)

    def test_retention_keeps_the_newest_five_backups(self):
        from korapy import reponame

        self.setUpStore()
        for index in range(6):
            name = reponame.backup_name(1000.0 + index)
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        self.assertEqual(self.rename(), 0)
        self.assertEqual(len(self.backups()), 5)

    def fill_retention(self):
        from korapy import reponame

        for index in range(5):
            name = reponame.backup_name(1000.0 + index)
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        return sorted(os.listdir(self.tmp.name))

    def test_new_holds_rows_refusal_keeps_every_earlier_backup(self):
        self.setUpStore()
        self.store.upsert_item(reading(NEW, 9), operators=["alice"], now=1000.0)
        before = self.fill_retention()
        code = self.rename()
        self.assertEqual(code, 4)
        self.assertIn(NEW, self.err.getvalue())
        self.assertEqual(sorted(os.listdir(self.tmp.name)), before)
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)

    def test_unrelated_project_live_unit_migrates(self):
        self.setUpStore()
        code = self.rename(
            live_units=frozenset({("other", "gone", 1)}),
            mapping={SHORT: OLD},
            project_shorts={"demo": [SHORT], "other": ["gone"]},
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.store.rename_counts(NEW)["items"], 2)

    def test_same_project_live_unit_refuses(self):
        self.setUpStore()
        code = self.rename(
            live_units=frozenset({("demo", "gone", 1)}),
            mapping={SHORT: OLD},
            project_shorts={"demo": [SHORT, "gone"]},
        )
        self.assertEqual(code, 2)
        self.assertIn(HOLDER, self.err.getvalue())
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)

    def test_lost_race_refusal_keeps_every_earlier_backup(self):
        self.setUpStore()
        before = self.fill_retention()
        priors = [name for name in before if name.startswith("board-backup-")]
        self.assertEqual(len(priors), 5)
        code = self.rename(store=LosingStore(self.store))
        self.assertEqual(code, 4)
        kept = [name for name in os.listdir(self.tmp.name) if name.startswith("board-backup-")]
        for name in priors:
            self.assertIn(name, kept)

    def test_held_drive_lock_refuses_naming_the_holder(self):
        self.setUpStore()
        held = HeldLock()
        code = self.rename(lock=held, lock_path="/state/drive.lock")
        self.assertEqual(code, 2)
        self.assertIn("another drive pass", self.err.getvalue())
        self.assertIn("/state/drive.lock", self.err.getvalue())
        self.assertEqual(held.calls, ["/state/drive.lock"])
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.store.rename_counts(NEW)["items"], 0)
        self.assertEqual(self.backups(), [])

    def test_lock_covers_the_backup_and_the_migration(self):
        self.setUpStore()
        rec = RecordingLock()
        seen = {}
        real_backup = self.store.backup_into
        real_rename = self.store.rename_repo

        def backup_into(path):
            seen["backup"] = rec.held
            return real_backup(path)

        def rename_repo(old, new):
            seen["rename"] = rec.held
            return real_rename(old, new)

        self.store.backup_into = backup_into
        self.store.rename_repo = rename_repo
        code = self.rename(lock=rec, lock_path="/state/drive.lock")
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"backup": True, "rename": True})
        self.assertEqual(rec.paths, ["/state/drive.lock"])

    def test_plan_holds_no_lock(self):
        self.setUpStore()
        rec = RecordingLock()
        code = self.rename(plan=True, lock=rec, lock_path="/state/drive.lock")
        self.assertEqual(code, 0)
        self.assertEqual(rec.paths, [])
        self.assertIn(f"{OLD} -> {NEW}", self.out.getvalue())

    def test_refused_after_backup_removes_only_the_fresh_backup(self):
        self.setUpStore()
        before = self.fill_retention()
        code = self.rename(store=LosingStore(self.store))
        self.assertEqual(code, 4)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), before)

    def test_store_error_after_backup_removes_only_the_fresh_backup(self):
        self.setUpStore()
        before = self.fill_retention()
        code = self.rename(store=FailingStore(self.store))
        self.assertEqual(code, 4)
        self.assertIn("disk is full", self.err.getvalue())
        self.assertEqual(sorted(os.listdir(self.tmp.name)), before)
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)

    def test_liveness_gather_runs_inside_the_lock(self):
        self.setUpStore()
        rec = RecordingLock()
        seen = {}

        def gather():
            seen["held"] = rec.held
            return ([(entry("agent-1"), registry.LIVE)], frozenset())

        code = self.rename(lock=rec, lock_path="/state/drive.lock", gather=gather)
        self.assertEqual(code, 2)
        self.assertIn("agent-1", self.err.getvalue())
        self.assertTrue(seen.get("held"))
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.backups(), [])

    def test_clobbered_backup_target_is_never_removed(self):
        from korapy import repocli

        self.setUpStore()
        target = repocli._backup_target(self.path, NOW)

        class ClobberedStore(LosingStore):
            def backup_into(self, path):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("someone else")
                raise board.StoreError(f"cannot back up into {path}: target exists")

        code = self.rename(store=ClobberedStore(self.store))
        self.assertEqual(code, 4)
        self.assertIn("target exists", self.err.getvalue())
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "someone else")
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)

    def test_missing_lock_fails_closed_outside_plan(self):
        self.setUpStore()
        code = self.rename(lock=None)
        self.assertEqual(code, 4)
        self.assertIn("lock", self.err.getvalue())
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.store.rename_counts(NEW)["items"], 0)
        self.assertEqual(self.backups(), [])

    def test_plan_without_a_lock_changes_nothing(self):
        self.setUpStore()
        code = self.rename(plan=True, lock=None)
        self.assertEqual(code, 0)
        self.assertIn(f"{OLD} -> {NEW}", self.out.getvalue())
        self.assertEqual(self.store.rename_counts(OLD)["items"], 2)
        self.assertEqual(self.backups(), [])

    def test_plan_names_the_untouched_session_records(self):
        self.setUpStore()
        code = self.rename(plan=True)
        self.assertEqual(code, 0)
        text = self.out.getvalue()
        self.assertIn("session directories", text)
        self.assertIn("review records", text)
        self.assertIn("not touched", text)
        self.assertIn("#479", text)


class _UnusedGit:
    """A git seam that must never run: explicit `tracker.repo` needs no lookup."""

    def __call__(self, *args, **kwargs):
        raise AssertionError("no origin lookup: tracker.repo is set")


class RenameHoldsSyncAndMirrorTest(unittest.TestCase):
    """While the rename holds the drive lock, sync and mirror refuse.

    The migration window the ticket closes: a standalone sync of the
    old name must not land rows under the old key after the rename's
    UPDATE commits. Both verbs take the same lock the rename holds,
    so an attempt from inside that window exits 2 with its store
    untouched. The real flock lock stands in for the rename's hold.
    """

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-rename-window-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\ntracker.repo = "acme/old"\n')
        self.store = board.open_store(os.path.join(self.root, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.lock_path = os.path.join(self.root, "drive.lock")

    def test_sync_attempted_inside_the_window_exits_2(self):
        from korapy import drive, syncli
        from tests.test_tracker import FakeTracker
        from tests.test_tracker import reading as tracker_reading

        fake = FakeTracker([tracker_reading("acme/old", 1, updated_at=NOW)])
        with drive.drive_lock(self.lock_path):
            code = syncli.sync_root(
                self.root,
                [],
                resync=False,
                audit=None,
                as_json=False,
                tracker=fake,
                store=self.store,
                git=_UnusedGit(),
                now=NOW,
                out=self.out,
                err=self.err,
                lock=drive.drive_lock,
                lock_path=self.lock_path,
            )
        self.assertEqual(code, 2)
        self.assertIn("another drive pass", self.err.getvalue())
        found, _ = self.store.item("acme/old", 1, now=NOW)
        self.assertIsNone(found)

    def test_mirror_attempted_inside_the_window_exits_2(self):
        from korapy import drive, mirrorcli
        from tests.test_tracker import FakeTracker

        self.store.upsert_item(reading(OLD, 1), operators=["alice"], now=NOW - 100)
        self.store.enqueue(OLD, 1, "agent", HOLDER, now=NOW - 50)
        with drive.drive_lock(self.lock_path):
            code = mirrorcli.mirror_root(
                self.root,
                (),
                limit=None,
                as_json=False,
                tracker=FakeTracker(),
                store=self.store,
                git=_UnusedGit(),
                now=NOW,
                out=self.out,
                err=self.err,
                operators={OLD: ["alice"]},
                lock=drive.drive_lock,
                lock_path=self.lock_path,
            )
        self.assertEqual(code, 2)
        self.assertIn("another drive pass", self.err.getvalue())
        self.assertEqual(len(self.store.queued_writes()), 1)


class TrackerRowTest(unittest.TestCase):
    def rows(self, **over):
        from korapy import doctor

        base = {
            "name": "demo",
            "origin": "https://example.com/acme/demo.git",
            "branch": "ok",
            "labels": "ok",
            "tracker_repo": OLD,
            "canonical": OLD,
        }
        base.update(over)
        site = repodoctor.RepoSite(**base)
        found = {}
        for check in doctor.run_checks(doctor_env(repo_sites=(site,))):
            found[check.name] = check
        return found

    def test_no_tracker_reading_carries_no_row(self):
        from korapy import doctor

        found = {check.name: check for check in doctor.run_checks(doctor_env(repo_sites=()))}
        self.assertNotIn("tracker:demo", found)

    def test_matching_canonical_is_ok(self):
        self.assertEqual(self.rows()["tracker:demo"].status, "ok")

    def test_renamed_canonical_warns_with_the_verb_fix(self):
        from korapy import doctor

        rows = self.rows(canonical=NEW)
        row = rows["tracker:demo"]
        self.assertEqual(row.status, "warn")
        self.assertIn(OLD, row.detail)
        self.assertIn(NEW, row.detail)
        self.assertIn("korapy repo rename", row.fix)
        self.assertIn(f"{OLD} {NEW}", row.fix)
        checks = doctor.run_checks(
            doctor_env(
                repo_sites=(
                    repodoctor.RepoSite(
                        name="demo",
                        origin="https://example.com/acme/demo.git",
                        branch="ok",
                        labels="ok",
                        tracker_repo=OLD,
                        canonical=NEW,
                    ),
                )
            )
        )
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_case_only_answers_are_ok(self):
        self.assertEqual(self.rows(canonical="ACME/Old")["tracker:demo"].status, "ok")

    def test_unread_canonical_carries_no_row(self):
        self.assertNotIn("tracker:demo", self.rows(canonical=None))

    def test_row_order_is_remote_labels_tracker(self):
        self.assertEqual(
            [
                row.name
                for row in repodoctor.check_sites(
                    (
                        repodoctor.RepoSite(
                            name="demo",
                            origin="x",
                            branch="ok",
                            labels="ok",
                            tracker_repo=OLD,
                            canonical=NEW,
                        ),
                    )
                )
            ],
            ["remote:demo", "labels:demo", "tracker:demo"],
        )


class CanonicalGatherTest(unittest.TestCase):
    def load(self, **over):
        from korapy import doctor
        from tests.test_init import FakeGit
        from tests.test_repodoctor import chdir, write_ops
        import tempfile

        params = {
            "labels": [(OLD, [label.name for label in iface.BOOTSTRAP_LABELS])],
        }
        params.update(over)
        tmp = tempfile.TemporaryDirectory(prefix="korapy-rename-site-")
        self.addCleanup(tmp.cleanup)
        clone = write_ops(tmp.name)
        chdir(self, tmp.name)
        url = "https://github.com/acme/old.git"
        fake = FakeTracker(**params)
        (site,) = doctor.load_repo_sites(
            None,
            git=FakeGit(root=clone, origin_url=url),
            tracker_factory={OLD: fake}.__getitem__,
        )
        return site, fake

    def test_matching_canonical_lands_on_the_site(self):
        site, fake = self.load(canonical={OLD: OLD})
        self.assertEqual(site.tracker_repo, OLD)
        self.assertEqual(site.canonical, OLD)
        self.assertIn(("canonical_repo", OLD), fake.calls)

    def test_renamed_answer_lands_on_the_site(self):
        site, _ = self.load(canonical={OLD: NEW})
        self.assertEqual(site.canonical, NEW)

    def test_failed_canonical_read_leaves_no_row(self):
        site, _ = self.load(canonical_error=iface.TrackerError("tracker unreachable: boom"))
        self.assertIsNone(site.canonical)


class CanonicalRepoTest(unittest.TestCase):
    def run_tracker(self, payload=None, error=None):
        from korapy.github.tracker import GithubTracker

        calls = []

        def run(args):
            calls.append(list(args))
            if error is not None:
                raise error
            return json.dumps(payload)

        return GithubTracker(run), calls

    def test_answers_the_reported_full_name(self):
        tracker, calls = self.run_tracker({"full_name": NEW, "name": "new"})
        self.assertEqual(tracker.canonical_repo(OLD), NEW)
        self.assertEqual(calls, [["api", f"repos/{OLD}"]])

    def test_unchanged_name_answers_itself(self):
        tracker, _ = self.run_tracker({"full_name": OLD, "name": "old"})
        self.assertEqual(tracker.canonical_repo(OLD), OLD)

    def test_bad_payloads_fail_closed(self):
        for payload in (None, [], {}, {"full_name": "no-slash"}, {"full_name": ""}):
            tracker, _ = self.run_tracker(payload)
            with self.assertRaises(iface.TrackerError, msg=repr(payload)):
                tracker.canonical_repo(OLD)

    def test_a_failed_call_is_unreachable(self):
        tracker, _ = self.run_tracker(error=Exception("(HTTP 500) boom"))
        with self.assertRaises(iface.TrackerError):
            tracker.canonical_repo(OLD)
