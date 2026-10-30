"""Doctor board check and migration path: schema, option ids, flip refusal, parity.

No network, no subprocess, no real state dir: schemas come from the
``BoardFakeRun`` GraphQL double plus a ``FakePacer``, and every store is
a temporary file. Importing ``tests.test_board_reader`` installs the
suite-wide tripwire that fails the run if any test reaches the default
pacer file or a real subprocess.
"""

import os
import tempfile
import unittest

from corral import board as store
from corral import boarddoctor, doctor
from corral.github import board as boardreader
from tests.test_board_fields import FakePacer
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    schema_payload,
    select_field,
)

REPO = "example/demo"
NOW = 1_760_000_000.0


def fetch_for(run):
    """The doctor schema seam over a canned ``gh`` double."""

    def fetch(board_config):
        return boardreader.BoardReader(board_config, run, pacer=FakePacer()).schema()

    return fetch


def schema_run(*fields):
    """A ``gh`` double answering one board schema page."""
    return BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*fields)])


def site(**over):
    base = {"repo": REPO, "variant": "board", "board": config()}
    base.update(over)
    return boarddoctor.BoardSite(**base)


def open_temp_store(directory):
    return store.open_store(os.path.join(directory, "board.sqlite"))


def stored(directory, number, **fields):
    """One open row in a fresh temp store, returned with the store."""
    handle = open_temp_store(directory)
    item = store.Item(repo=REPO, number=number, author="alice", **fields)
    handle.upsert_item(item, operators=["alice"], now=NOW)
    return handle


class LabelsModeSkipped(unittest.TestCase):
    def test_a_labels_repo_gets_no_board_row_and_no_schema_call(self):
        def fetch(board_config):
            raise AssertionError("labels mode must not resolve any board")

        rows = boarddoctor.check_sites(
            [site(variant="labels")],
            fetch=fetch,
            open_store=lambda: self.fail("labels mode must not open the store"),
        )
        self.assertEqual(rows, [])

    def test_no_board_sites_means_no_rows_and_no_seam_calls(self):
        rows = boarddoctor.check_sites([], fetch=None, open_store=None)
        self.assertEqual(rows, [])


class SchemaResolution(unittest.TestCase):
    def test_a_resolving_board_with_a_drained_outbox_is_ok(self):
        run = schema_run(*full_schema_fields())
        directory = tempfile.mkdtemp(prefix="corral-boarddoctor-")
        rows = boarddoctor.check_sites(
            [site()],
            fetch=fetch_for(run),
            open_store=lambda: open_temp_store(directory),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, f"board:{REPO}")
        self.assertEqual(rows[0].status, "ok")
        self.assertIn("resolves", rows[0].detail)
        self.assertEqual(rows[0].fix, "")

    def test_an_unreachable_board_is_a_failure_naming_the_repo(self):
        rows = boarddoctor.check_sites(
            [site()],
            fetch=fetch_for(BoardFakeRun()),
            open_store=lambda: open_temp_store(tempfile.mkdtemp(prefix="corral-boarddoctor-")),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "fail")
        self.assertIn(REPO, rows[0].detail)
        self.assertIn("does not resolve", rows[0].detail)
        self.assertTrue(rows[0].fix)

    def test_a_missing_mapped_field_is_a_failure_naming_repo_and_field(self):
        fields = [f for f in full_schema_fields() if f["name"] != "Priority"]
        rows = boarddoctor.check_sites(
            [site()],
            fetch=fetch_for(schema_run(*fields)),
            open_store=lambda: open_temp_store(tempfile.mkdtemp(prefix="corral-boarddoctor-")),
        )
        self.assertEqual(rows[0].status, "fail")
        self.assertIn(REPO, rows[0].detail)
        self.assertIn("Priority", rows[0].detail)
        self.assertIn("priority", rows[0].detail)

    def test_board_mode_without_a_board_table_is_a_failure_naming_the_repo(self):
        rows = boarddoctor.check_sites(
            [site(board=None)],
            fetch=fetch_for(schema_run(*full_schema_fields())),
            open_store=lambda: open_temp_store(tempfile.mkdtemp(prefix="corral-boarddoctor-")),
        )
        self.assertEqual(rows[0].status, "fail")
        self.assertIn(REPO, rows[0].detail)
        self.assertIn("[board]", rows[0].detail)


class OptionValidation(unittest.TestCase):
    def check(self, run):
        return boarddoctor.check_sites(
            [site()],
            fetch=fetch_for(run),
            open_store=lambda: open_temp_store(tempfile.mkdtemp(prefix="corral-boarddoctor-")),
        )

    def test_a_missing_option_id_is_a_failure_naming_repo_field_and_id(self):
        fields = [
            select_field("F_prio", "Priority", [("opt-p0-id", "P0")])
            if f["name"] == "Priority"
            else f
            for f in full_schema_fields()
        ]
        rows = self.check(schema_run(*fields))
        self.assertEqual(rows[0].status, "fail")
        self.assertIn(REPO, rows[0].detail)
        self.assertIn("Priority", rows[0].detail)
        self.assertIn("opt-p1-id", rows[0].detail)

    def test_a_display_name_rename_is_free_when_the_id_survives(self):
        fields = [
            select_field("F_prio", "Priority", [("opt-p0-id", "P-zero"), ("opt-p1-id", "P-one")])
            if f["name"] == "Priority"
            else f
            for f in full_schema_fields()
        ]
        rows = self.check(schema_run(*fields))
        self.assertEqual(rows[0].status, "ok")

    def test_option_ids_are_checked_by_id_not_by_name(self):
        found = boarddoctor.check_options(
            REPO, config(), fetch_for(schema_run(*full_schema_fields()))(config())
        )
        self.assertEqual(found, [])


class FlipRefusal(unittest.TestCase):
    def test_an_empty_outbox_is_not_a_refusal(self):
        self.assertIsNone(boarddoctor.flip_refusal(REPO, 0))

    def test_a_non_empty_outbox_is_a_refusal_naming_repo_and_count(self):
        refusal = boarddoctor.flip_refusal(REPO, 2)
        self.assertIsNotNone(refusal)
        self.assertIn(REPO, refusal or "")
        self.assertIn("2", refusal or "")
        self.assertIn("flip", refusal or "")

    def test_queued_writes_fail_the_board_row_until_the_outbox_drains(self):
        directory = tempfile.mkdtemp(prefix="corral-boarddoctor-")
        handle = stored(directory, 7)
        handle.enqueue(REPO, 7, "agent", "agent-1", now=NOW)
        handle.close()
        rows = boarddoctor.check_sites(
            [site()],
            fetch=fetch_for(schema_run(*full_schema_fields())),
            open_store=lambda: open_temp_store(directory),
        )
        self.assertEqual(rows[0].status, "fail")
        self.assertIn(REPO, rows[0].detail)
        self.assertIn("flip", rows[0].detail)
        drained = open_temp_store(directory)
        try:
            for queued in drained.queued_writes():
                if queued.repo == REPO:
                    drained.mark_sent(queued.id)
        finally:
            drained.close()
        rows = boarddoctor.check_sites(
            [site()],
            fetch=fetch_for(schema_run(*full_schema_fields())),
            open_store=lambda: open_temp_store(directory),
        )
        self.assertEqual(rows[0].status, "ok")


class ParityReport(unittest.TestCase):
    def test_matching_and_single_sided_ranks_are_silent(self):
        items = [
            store.Item(repo=REPO, number=7, priority="low", extra={"priority": "low"}),
            store.Item(repo=REPO, number=8, priority="high", extra={}),
            store.Item(repo=REPO, number=9, priority="", extra={"priority": "urgent"}),
        ]
        self.assertEqual(boarddoctor.parity_lines(items), [])

    def test_a_rank_conflict_names_the_item_and_both_ranks(self):
        items = [store.Item(repo=REPO, number=7, priority="high", extra={"priority": "urgent"})]
        (line,) = boarddoctor.parity_lines(items)
        self.assertIn(f"{REPO}#7", line)
        self.assertIn("high", line)
        self.assertIn("urgent", line)

    def test_an_unparseable_blocked_by_entry_names_the_item_and_the_entry(self):
        items = [store.Item(repo=REPO, number=7, priority="", extra={"blocked_by": "not a ref"})]
        (line,) = boarddoctor.parity_lines(items)
        self.assertIn(f"{REPO}#7", line)
        self.assertIn("not a ref", line)
        self.assertIn("blocked", line)

    def test_a_parseable_blocked_by_list_is_silent(self):
        items = [
            store.Item(
                repo=REPO,
                number=7,
                priority="",
                extra={"blocked_by": ["example/other#3"]},
            )
        ]
        self.assertEqual(boarddoctor.parity_lines(items), [])

    def test_parity_lines_are_stable_text_sorted_by_repo_and_number(self):
        items = [
            store.Item(repo="example/b", number=7, priority="high", extra={"priority": "low"}),
            store.Item(repo=REPO, number=9, priority="high", extra={"priority": "low"}),
            store.Item(repo=REPO, number=7, priority="high", extra={"priority": "low"}),
        ]
        self.assertEqual(
            boarddoctor.parity_lines(items),
            [
                "example/b#7: priority 'high' from labels but 'low' from the board",
                "example/demo#7: priority 'high' from labels but 'low' from the board",
                "example/demo#9: priority 'high' from labels but 'low' from the board",
            ],
        )

    def test_stored_mismatches_fail_the_board_row_with_stable_detail(self):
        directory = tempfile.mkdtemp(prefix="corral-boarddoctor-")
        handle = open_temp_store(directory)
        try:
            handle.upsert_item(
                store.Item(repo=REPO, number=9, priority="high", extra={"priority": "low"}),
                operators=["alice"],
                now=NOW,
            )
            handle.upsert_item(
                store.Item(repo=REPO, number=7, priority="high", extra={"priority": "low"}),
                operators=["alice"],
                now=NOW,
            )
        finally:
            handle.close()
        rows = boarddoctor.check_sites(
            [site()],
            fetch=fetch_for(schema_run(*full_schema_fields())),
            open_store=lambda: open_temp_store(directory),
        )
        self.assertEqual(rows[0].status, "fail")
        self.assertIn("example/demo#7", rows[0].detail)
        self.assertIn("example/demo#9", rows[0].detail)
        self.assertLess(rows[0].detail.index("#7"), rows[0].detail.index("#9"))


class SitesFromSettings(unittest.TestCase):
    def write(self, directory, text):
        with open(os.path.join(directory, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_only_board_mode_repos_become_sites(self):
        root = tempfile.mkdtemp(prefix="corral-boarddoctor-")
        config_dir = tempfile.mkdtemp(prefix="corral-boarddoctor-")
        self.write(
            root,
            '[[repo]]\nname = "app"\n'
            '[repo.tracker]\nrepo = "example/demo"\nvariant = "board"\n'
            '[repo.board]\nproject_owner = "example"\nproject_number = 3\n'
            '[[repo]]\nname = "plain"\n'
            '[repo.tracker]\nrepo = "example/plain"\n',
        )
        sites, error = boarddoctor.sites_from_settings(root, env={"CORRAL_CONFIG": config_dir})
        self.assertIsNone(error)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].repo, "example/demo")
        self.assertEqual(sites[0].variant, "board")
        board = sites[0].board
        assert board is not None
        self.assertEqual(board.project_owner, "example")
        self.assertEqual(board.project_number, 3)

    def test_a_broken_ops_file_is_an_error_not_a_crash(self):
        root = tempfile.mkdtemp(prefix="corral-boarddoctor-")
        config_dir = tempfile.mkdtemp(prefix="corral-boarddoctor-")
        self.write(root, '[[repo]]\nname = "app"\n[repo.board]\nproject_number = 3\n')
        sites, error = boarddoctor.sites_from_settings(root, env={"CORRAL_CONFIG": config_dir})
        self.assertEqual(sites, [])
        self.assertIsNotNone(error)
        self.assertIn("app", error or "")


class DoctorWiring(unittest.TestCase):
    def env(self, **over):
        from tests.test_doctor import env as base_env

        return base_env(**over)

    def test_no_board_sites_means_no_board_rows(self):
        checks = doctor.run_checks(self.env())
        self.assertEqual([c.name for c in checks if c.name.startswith("board")], [])

    def test_a_board_site_adds_one_row_per_repo_after_prompts(self):
        run = schema_run(*full_schema_fields())
        checks = doctor.run_checks(
            self.env(
                board_sites=[site()],
                board_fetch=fetch_for(run),
                board_open_store=lambda: open_temp_store(
                    tempfile.mkdtemp(prefix="corral-boarddoctor-")
                ),
            )
        )
        names = [c.name for c in checks]
        self.assertIn(f"board:{REPO}", names)
        self.assertLess(names.index("prompts"), names.index(f"board:{REPO}"))

    def test_a_failing_board_check_fails_with_exit_1(self):
        checks = doctor.run_checks(
            self.env(
                board_sites=[site()],
                board_fetch=fetch_for(BoardFakeRun()),
                board_open_store=lambda: open_temp_store(
                    tempfile.mkdtemp(prefix="corral-boarddoctor-")
                ),
            )
        )
        failed = [c for c in checks if c.name == f"board:{REPO}"]
        self.assertEqual([c.status for c in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_board_assembly_error_is_a_failing_board_row(self):
        checks = doctor.run_checks(self.env(board_error="cannot list [[repo]] entries: boom"))
        failed = [c for c in checks if c.name == "board"]
        self.assertEqual([c.status for c in failed], ["fail"])
        self.assertIn("boom", failed[0].detail)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_sites_without_seams_fail_closed(self):
        checks = doctor.run_checks(self.env(board_sites=[site()]))
        failed = [c for c in checks if c.name == f"board:{REPO}"]
        self.assertEqual([c.status for c in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 1)


if __name__ == "__main__":
    unittest.main()
