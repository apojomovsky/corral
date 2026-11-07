"""Doctor board checks: schema comparison, scope, titles, flip, parity, membership.

No network, no subprocess, no real state dir: snapshots are plain
``tracker`` data (or come from the ``BoardFakeRun`` GraphQL double plus
a ``FakePacer`` at the adapter level), and every store is a temporary
file. Importing ``tests.test_board_reader`` installs the suite-wide
tripwire that fails the run if any test reaches the default pacer file
or a real subprocess.
"""

import os
import tempfile
import unittest
from typing import Any

from korapy import board as store
from korapy import boarddoctor, doctor
from korapy import tracker as iface
from tests.test_board_fields import FakePacer
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    item_node,
    items_payload,
    schema_payload,
    select_field,
)

REPO = "example/demo"
NOW = 1_760_000_000.0
TITLE = "Demo board"


def live_field(name, kind="text", options=()):
    """One raw live field as plain data."""
    return iface.LiveField(
        name=name,
        kind=kind,
        options=tuple(iface.LiveOption(id=oid, name=oname) for oid, oname in options),
    )


def full_live_fields():
    return (
        live_field("Agent"),
        live_field("Agent state"),
        live_field("Status", "single_select", (("opt-s1", "In progress"),)),
        live_field("Priority", "single_select", (("opt-p0-id", "P0"), ("opt-p1-id", "P1"))),
        live_field("Area", "single_select", (("opt-a1", "Backend"),)),
        live_field("Pool", "single_select", (("opt-q1", "Dispatch"),)),
        live_field("Blocked by"),
    )


def snapshot(fields=None, title=TITLE, owner_boards=None):
    """One board snapshot as plain data, defaulting to the configured board alone."""
    if fields is None:
        fields = full_live_fields()
    if owner_boards is None:
        owner_boards = (iface.OwnerBoard(number=3, title=title),)
    return iface.BoardSnapshot(title=title, fields=tuple(fields), owner_boards=tuple(owner_boards))


def titles_payload(*entries, more=False, end="CURSOR-1"):
    return {
        "user": {
            "projectsV2": {
                "nodes": [
                    {"id": eid, "number": number, "title": title} for eid, number, title in entries
                ],
                "pageInfo": {"hasNextPage": more, "endCursor": end},
            }
        },
        "organization": None,
    }


def fetch_snapshot(found=None):
    """The doctor schema seam over canned snapshot data."""

    def fetch(board_config):
        return found if found is not None else snapshot()

    return fetch


def items_on(*refs):
    """The doctor membership seam over canned ``(repo, number)`` pairs."""

    def board_items(board_config):
        return set(refs)

    return board_items


def tempdir():
    return tempfile.mkdtemp(prefix="korapy-boarddoctor-")


def check(sites, *, snap=None, refs=(), fetch=None, store_dir=None, board_items=None, now=None):
    """``check_sites`` with fakes: snapshot ``snap``, membership ``refs``."""
    if fetch is None:
        fetch = fetch_snapshot(snap)
    if board_items is None:
        board_items = items_on(*refs)
    directory = store_dir if store_dir is not None else tempdir()
    return boarddoctor.check_sites(
        sites,
        fetch=fetch,
        open_store=lambda: open_temp_store(directory),
        board_items=board_items,
        now=now,
    )


def flag_drift(directory, *entries, repo=REPO, since=NOW):
    """Drift verdicts in a temp store: ``entries`` are numbers or ``(number, since)``."""
    handle = open_temp_store(directory)
    try:
        for entry in entries:
            number, at = entry if isinstance(entry, tuple) else (entry, since)
            handle.note_drift(repo, number, now=at)
    finally:
        handle.close()


_UNSET: Any = object()


def site(repo=REPO, variant="board", board=_UNSET, drift_max_age_s=None):
    table = config() if board is _UNSET else board
    return boarddoctor.BoardSite(
        repo=repo,
        variant=variant,
        board=table,
        drift_max_age_s=drift_max_age_s,
    )


def open_temp_store(directory):
    return store.open_store(os.path.join(directory, "board.sqlite"))


def stored_numbers(directory, *numbers, repo=REPO, state="open"):
    """Open (or ``state``) rows in a fresh temp store, closed on return."""
    handle = open_temp_store(directory)
    try:
        for number in numbers:
            handle.upsert_item(
                store.Item(repo=repo, number=number, author="alice", state=state),
                operators=["alice"],
                now=NOW,
            )
    finally:
        handle.close()


class LabelsModeSkipped(unittest.TestCase):
    def test_a_labels_repo_gets_no_board_row_and_no_seam_calls(self):
        def fetch(board_config):
            raise AssertionError("labels mode must not resolve any board")

        def board_items(board_config):
            raise AssertionError("labels mode must not list any board")

        rows = boarddoctor.check_sites(
            [site(variant="labels", board=None)],
            fetch=fetch,
            open_store=lambda: self.fail("labels mode must not open the store"),
            board_items=board_items,
        )
        self.assertEqual(rows, [])

    def test_no_board_sites_means_no_rows_and_no_seam_calls(self):
        rows = boarddoctor.check_sites([], fetch=None, open_store=None, board_items=None)
        self.assertEqual(rows, [])

    def test_a_labels_table_warn_needs_no_seam(self):
        rows = boarddoctor.check_sites(
            [site(variant="labels", board=config())],
            fetch=None,
            open_store=None,
            board_items=None,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "warn")


class LabelsTableWarn(unittest.TestCase):
    def test_a_labels_entry_with_a_table_warns_without_any_fetch_or_store_call(self):
        def fetch(board_config):
            raise AssertionError("the labels warn answers from config alone")

        def board_items(board_config):
            raise AssertionError("the labels warn answers from config alone")

        rows = boarddoctor.check_sites(
            [site(variant="labels", board=config())],
            fetch=fetch,
            open_store=lambda: self.fail("the labels warn opens no store"),
            board_items=board_items,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, f"board:{REPO}")
        self.assertEqual(rows[0].status, "warn")
        self.assertIn("[repo.board]", rows[0].detail)
        self.assertIn("labels", rows[0].detail)
        self.assertIn('tracker.variant = "board"', rows[0].fix)
        self.assertIn("remove the", rows[0].fix)

    def test_site_order_is_kept_across_board_and_labels_rows(self):
        other = site(repo="example/other")
        rows = check(
            [site(), site(variant="labels", board=config()), other],
        )
        self.assertEqual(
            [row.name for row in rows],
            [f"board:{REPO}", f"board:{REPO}", "board:example/other"],
        )
        self.assertEqual([row.status for row in rows], ["ok", "warn", "ok"])

    def test_the_labels_warn_carries_no_duplicate_title_check(self):
        seen = []

        def fetch(board_config):
            seen.append(board_config)
            return snapshot(
                owner_boards=(
                    iface.OwnerBoard(number=3, title=TITLE),
                    iface.OwnerBoard(number=7, title=TITLE),
                )
            )

        rows = boarddoctor.check_sites(
            [site(variant="labels", board=config())],
            fetch=fetch,
            open_store=lambda: self.fail("the labels warn opens no store"),
            board_items=items_on(),
        )
        self.assertEqual(seen, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "warn")
        self.assertNotIn("shares the title", rows[0].detail)


class SnapshotResolution(unittest.TestCase):
    def test_a_resolving_board_with_a_drained_outbox_is_ok(self):
        (row,) = check([site()])
        self.assertEqual(row.name, f"board:{REPO}")
        self.assertEqual(row.status, "ok")
        self.assertIn("resolves", row.detail)
        self.assertEqual(row.fix, "")

    def test_an_unreachable_board_is_a_failure_naming_owner_and_number(self):
        def fetch(board_config):
            raise iface.TrackerError("board project is missing or ambiguous")

        (row,) = check([site()], fetch=fetch)
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("example", row.detail)
        self.assertIn("3", row.detail)
        self.assertIn("does not resolve", row.detail)
        self.assertIn("remove the table", row.fix)
        self.assertIn("korapy github setup", row.fix)

    def test_a_board_addressed_by_id_names_the_id(self):
        by_id = config(project_number=None, project_id="PVT_1")

        def fetch(board_config):
            raise iface.TrackerError("board project is missing or ambiguous")

        (row,) = check([site(board=by_id)], fetch=fetch)
        self.assertEqual(row.status, "fail")
        self.assertIn("PVT_1", row.detail)
        self.assertIn("does not resolve", row.detail)

    def test_board_mode_without_a_board_table_points_at_setup(self):
        (row,) = check([site(board=None)])
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("[repo.board]", row.detail)
        self.assertIn("[repo.board]", row.fix)
        self.assertIn("korapy github setup", row.fix)

    def test_a_missing_mapped_field_is_a_failure_naming_repo_concept_and_field(self):
        fields = [f for f in full_live_fields() if f.name != "Priority"]
        (row,) = check([site()], snap=snapshot(fields))
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("Priority", row.detail)
        self.assertIn("priority", row.detail)
        self.assertIn("missing", row.detail)
        self.assertIn("korapy github setup", row.fix)

    def test_a_wrong_kind_field_is_a_failure_naming_concept_field_and_both_kinds(self):
        fields = [live_field("Priority") if f.name == "Priority" else f for f in full_live_fields()]
        (row,) = check([site()], snap=snapshot(fields))
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("Priority", row.detail)
        self.assertIn("priority", row.detail)
        self.assertIn("text", row.detail)
        self.assertIn("single_select", row.detail)
        self.assertIn("rename or delete", row.fix)
        self.assertIn("korapy github setup", row.fix)

    def test_a_rate_limited_board_read_is_a_transient_budget_row(self):
        def fetch(board_config):
            raise iface.TrackerRateLimited("slow down", 30.0)

        (row,) = check([site()], fetch=fetch)
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("budget", row.detail)
        self.assertIn("cooldown", row.fix)
        self.assertIn("re-run", row.fix)


class FieldComparison(unittest.TestCase):
    def compare(self, cfg=None, fields=None):
        return boarddoctor.compare_fields(
            REPO, cfg if cfg is not None else config(), tuple(fields or full_live_fields())
        )

    def test_an_exact_board_has_no_findings(self):
        self.assertEqual(self.compare(), ())

    def test_a_display_name_rename_is_free_when_the_id_survives(self):
        fields = [
            live_field(
                "Priority", "single_select", (("opt-p0-id", "P-zero"), ("opt-p1-id", "P-one"))
            )
            if f.name == "Priority"
            else f
            for f in full_live_fields()
        ]
        self.assertEqual(self.compare(fields=fields), ())

    def test_a_kept_status_field_is_never_topped_up(self):
        fields = [
            live_field("Status", "single_select", (("opt-x", "Whatever"),))
            if f.name == "Status"
            else f
            for f in full_live_fields()
        ]
        self.assertEqual(self.compare(fields=fields), ())

    def test_unmapped_concepts_are_silent(self):
        cfg = config(area_column="", pool_column="")
        fields = [f for f in full_live_fields() if f.name not in ("Area", "Pool")]
        self.assertEqual(self.compare(cfg=cfg, fields=fields), ())

    def test_a_duplicate_team_field_is_silent(self):
        fields = full_live_fields() + (live_field("Team"), live_field("Team"))
        self.assertEqual(self.compare(fields=fields), ())

    def test_duplicate_field_names_refuse_without_a_pick(self):
        fields = full_live_fields() + (
            live_field("Priority", "single_select", (("opt-p9", "P9"),)),
        )
        (finding,) = self.compare(fields=fields)
        self.assertEqual(finding.severity, "fail")
        self.assertIn("Priority", finding.detail)
        self.assertIn("ambiguous", finding.detail)
        self.assertIn("duplicate", finding.fix)

    def test_duplicate_option_names_refuse_without_a_pick(self):
        fields = [
            live_field("Priority", "single_select", (("opt-p0-id", "P0"), ("opt-p1-id", "P0")))
            if f.name == "Priority"
            else f
            for f in full_live_fields()
        ]
        findings = self.compare(fields=fields)
        self.assertTrue(findings)
        self.assertIn("P0", findings[0].detail)
        self.assertIn("duplicate", findings[0].fix)
        self.assertEqual(findings[0].severity, "fail")

    def test_a_gone_option_id_is_a_failure_naming_field_id_and_rank(self):
        fields = [
            live_field("Priority", "single_select", (("opt-p0-id", "P0"),))
            if f.name == "Priority"
            else f
            for f in full_live_fields()
        ]
        (finding,) = self.compare(fields=fields)
        self.assertEqual(finding.severity, "fail")
        self.assertIn(REPO, finding.detail)
        self.assertIn("Priority", finding.detail)
        self.assertIn("opt-p1-id", finding.detail)
        self.assertIn("high", finding.detail)
        self.assertIn("re-add the option", finding.fix)

    def test_a_live_option_missing_from_the_map_warns_naming_its_name(self):
        fields = [
            live_field(
                "Priority",
                "single_select",
                (("opt-p0-id", "P0"), ("opt-p1-id", "P1"), ("opt-p9", "P-nine")),
            )
            if f.name == "Priority"
            else f
            for f in full_live_fields()
        ]
        (finding,) = self.compare(fields=fields)
        self.assertEqual(finding.severity, "warn")
        self.assertIn(REPO, finding.detail)
        self.assertIn("P-nine", finding.detail)
        self.assertIn("priority_map", finding.fix)

    def test_a_map_with_no_priority_concept_is_a_finding(self):
        cfg = config(priority_column="", priority_map={"opt-p0-id": "urgent"})
        (finding,) = self.compare(cfg=cfg)
        self.assertEqual(finding.severity, "fail")
        self.assertIn(REPO, finding.detail)
        self.assertIn("priority", finding.detail)
        self.assertIn("not checked", finding.detail)

    def test_an_empty_map_with_no_priority_concept_is_silent(self):
        cfg = config(priority_column="", priority_map={})
        self.assertEqual(self.compare(cfg=cfg), ())

    def test_a_gone_status_option_id_is_a_failure_naming_field_id_and_state(self):
        fields = [
            live_field("Status", "single_select", (("opt-a", "Available"),))
            if f.name == "Status"
            else f
            for f in full_live_fields()
        ]
        cfg = config(status_available="opt-a", status_done="opt-d")
        (finding,) = self.compare(cfg=cfg, fields=fields)
        self.assertEqual(finding.severity, "fail")
        self.assertIn(REPO, finding.detail)
        self.assertIn("Status", finding.detail)
        self.assertIn("opt-d", finding.detail)
        self.assertIn("done", finding.detail)
        self.assertIn("re-add the option", finding.fix)

    def test_mapped_status_option_ids_all_present_are_silent(self):
        fields = [
            live_field(
                "Status",
                "single_select",
                (("opt-a", "Available"), ("opt-d", "Done")),
            )
            if f.name == "Status"
            else f
            for f in full_live_fields()
        ]
        cfg = config(status_available="opt-a", status_done="opt-d")
        self.assertEqual(self.compare(cfg=cfg, fields=fields), ())

    def test_status_ids_with_no_status_concept_are_a_finding(self):
        cfg = config(status_column="", status_done="opt-d")
        (finding,) = self.compare(cfg=cfg)
        self.assertEqual(finding.severity, "fail")
        self.assertIn(REPO, finding.detail)
        self.assertIn("status", finding.detail)
        self.assertIn("not checked", finding.detail)


class DuplicateTitle(unittest.TestCase):
    def test_a_second_board_with_the_same_title_warns_naming_title_and_numbers(self):
        snap = snapshot(
            owner_boards=(
                iface.OwnerBoard(number=3, title=TITLE),
                iface.OwnerBoard(number=7, title=TITLE),
            )
        )
        (row,) = check([site()], snap=snap)
        self.assertEqual(row.status, "warn")
        self.assertIn(REPO, row.detail)
        self.assertIn(TITLE, row.detail)
        self.assertIn("3", row.detail)
        self.assertIn("7", row.detail)
        self.assertIn("delete the board", row.fix)

    def test_a_differently_titled_second_board_stays_ok(self):
        snap = snapshot(
            owner_boards=(
                iface.OwnerBoard(number=3, title=TITLE),
                iface.OwnerBoard(number=7, title="Other board"),
            )
        )
        (row,) = check([site()], snap=snap)
        self.assertEqual(row.status, "ok")

    def test_the_configured_board_alone_is_no_warn(self):
        (row,) = check([site()])
        self.assertEqual(row.status, "ok")
        self.assertNotIn("shares the title", row.detail)

    def test_a_warn_only_row_still_exits_zero(self):
        snap = snapshot(
            owner_boards=(
                iface.OwnerBoard(number=3, title=TITLE),
                iface.OwnerBoard(number=7, title=TITLE),
            )
        )
        (row,) = check([site()], snap=snap)
        self.assertEqual(row.status, "warn")
        self.assertEqual(doctor.exit_code([doctor.Check(*row)]), 0)


class AdapterDuplicateTitle(unittest.TestCase):
    """The real reader snapshot all the way through the warn and the rows.

    The snapshot comes from canned GraphQL bodies through the real
    ``BoardReader``, never from a fake that already returns
    ``OwnerBoard`` objects, so a shape fork between the adapter and
    ``duplicate_title_warn`` fails here instead of in production.
    """

    BOARD_TITLE = "Board"

    def snapshot_run(self, *titles):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        run.script("ProvisionBoardTitles", [titles_payload(*titles)])
        return run

    def snapshot_via_reader(self, *titles):
        from korapy.github import board as boardreader

        return boardreader.BoardReader(
            config(), self.snapshot_run(*titles), pacer=FakePacer()
        ).snapshot()

    def fetch_via_reader(self, *titles):
        run = self.snapshot_run(*titles)
        from korapy.github import board as boardreader

        def fetch(board_config):
            return boardreader.BoardReader(board_config, run, pacer=FakePacer()).snapshot()

        return fetch

    def test_adapter_owner_boards_are_ownerboard_entries(self):
        snap = self.snapshot_via_reader(("PVT_1", 3, self.BOARD_TITLE))
        for entry in snap.owner_boards:
            self.assertIsInstance(entry, iface.OwnerBoard)
        self.assertEqual(snap.owner_boards, (iface.OwnerBoard(number=3, title=self.BOARD_TITLE),))

    def test_adapter_snapshot_with_two_boards_of_one_title_warns(self):
        snap = self.snapshot_via_reader(
            ("PVT_1", 3, self.BOARD_TITLE), ("PVT_2", 7, self.BOARD_TITLE)
        )
        finding = boarddoctor.duplicate_title_warn(REPO, snap)
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.severity, "warn")
        self.assertIn(self.BOARD_TITLE, finding.detail)
        self.assertIn("3", finding.detail)
        self.assertIn("7", finding.detail)

    def test_adapter_snapshot_with_distinct_titles_stays_ok(self):
        snap = self.snapshot_via_reader(("PVT_1", 3, self.BOARD_TITLE), ("PVT_2", 7, "Other board"))
        self.assertIsNone(boarddoctor.duplicate_title_warn(REPO, snap))

    def test_adapter_snapshot_duplicate_title_warns_the_doctor_row(self):
        fetch = self.fetch_via_reader(
            ("PVT_1", 3, self.BOARD_TITLE), ("PVT_2", 7, self.BOARD_TITLE)
        )
        (row,) = check([site()], fetch=fetch)
        self.assertEqual(row.status, "warn")
        self.assertIn(self.BOARD_TITLE, row.detail)
        self.assertIn("3", row.detail)
        self.assertIn("7", row.detail)

    def test_adapter_snapshot_without_a_duplicate_stays_ok(self):
        fetch = self.fetch_via_reader(("PVT_1", 3, self.BOARD_TITLE), ("PVT_2", 7, "Other board"))
        (row,) = check([site()], fetch=fetch)
        self.assertEqual(row.status, "ok")

    def test_an_unexpected_title_check_failure_is_a_fail_row_not_a_crash(self):
        snap = iface.BoardSnapshot(
            title=TITLE,
            fields=tuple(full_live_fields()),
            owner_boards=(("unusable",),),  # ty: ignore[invalid-argument-type]
        )
        rows = check([site()], snap=snap)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "fail")
        self.assertIn(REPO, rows[0].detail)


class ScopeRow(unittest.TestCase):
    SCOPE_TEXT = (
        "board unreachable: The 'project' field requires one of the"
        " following scopes: ['read:project']"
    )

    def test_a_scope_error_is_a_failure_naming_the_scope(self):
        def fetch(board_config):
            raise iface.TrackerError(self.SCOPE_TEXT)

        (row,) = check([site()], fetch=fetch)
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("project", row.detail)
        self.assertIn("gh auth refresh -s project", row.fix)

    def test_a_scope_error_listing_items_is_a_scope_finding(self):
        def board_items(board_config):
            raise iface.TrackerError(self.SCOPE_TEXT)

        (row,) = check([site()], board_items=board_items)
        self.assertEqual(row.status, "fail")
        self.assertIn("project", row.detail)
        self.assertIn("gh auth refresh -s project", row.fix)

    def test_a_non_scope_error_keeps_the_generic_row(self):
        def fetch(board_config):
            raise iface.TrackerError("board unreachable: HTTP 503 blew up")

        (row,) = check([site()], fetch=fetch)
        self.assertEqual(row.status, "fail")
        self.assertIn("does not resolve", row.detail)
        self.assertNotIn("gh auth refresh", row.fix)


class MissingFromBoard(unittest.TestCase):
    def test_board_items_covering_every_open_row_is_ok(self):
        directory = tempdir()
        stored_numbers(directory, 7, 8)
        (row,) = check([site()], store_dir=directory, refs=[(REPO, 7), (REPO, 8)])
        self.assertEqual(row.status, "ok")

    def test_a_missing_open_issue_fails_naming_the_count_and_number(self):
        directory = tempdir()
        stored_numbers(directory, 7, 9)
        (row,) = check([site()], store_dir=directory, refs=[(REPO, 7)])
        self.assertEqual(row.status, "fail")
        self.assertIn("1 open issue", row.detail)
        self.assertIn("9", row.detail)
        self.assertIn("korapy github setup --populate", row.fix)

    def test_missing_numbers_name_the_first_few_then_count_the_rest(self):
        directory = tempdir()
        stored_numbers(directory, *range(1, 9))
        (row,) = check([site()], store_dir=directory, refs=[(REPO, 1)])
        self.assertEqual(row.status, "fail")
        self.assertIn("7 open issues", row.detail)
        self.assertIn("(numbers 2, 3, 4, 5, 6 and 2 more)", row.detail)

    def test_closed_rows_are_not_board_members(self):
        directory = tempdir()
        stored_numbers(directory, 7, state="closed")
        (row,) = check([site()], store_dir=directory, refs=[])
        self.assertEqual(row.status, "ok")

    def test_other_repos_rows_are_not_compared(self):
        directory = tempdir()
        stored_numbers(directory, 7, repo="example/other")
        (row,) = check([site()], store_dir=directory, refs=[])
        self.assertEqual(row.status, "ok")

    def test_a_missing_membership_seam_fails_closed(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        rows = boarddoctor.check_sites(
            [site()],
            fetch=fetch_snapshot(),
            open_store=lambda: open_temp_store(directory),
            board_items=None,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "fail")
        self.assertIn("membership seam", rows[0].detail)

    def test_an_item_listing_failure_fails_the_row(self):
        def board_items(board_config):
            raise iface.TrackerError("board items are missing")

        (row,) = check([site()], board_items=board_items)
        self.assertEqual(row.status, "fail")
        self.assertIn("cannot list the board's items", row.detail)


class DriftRowTest(unittest.TestCase):
    def drift_row(self, rows):
        found = [row for row in rows if row.name == f"board-drift:{REPO}"]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_no_verdicts_reads_ok_after_the_board_row(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        rows = check([site(drift_max_age_s=600)], store_dir=directory, refs=[(REPO, 7)], now=NOW)
        self.assertEqual([row.name for row in rows], [f"board:{REPO}", f"board-drift:{REPO}"])
        row = self.drift_row(rows)
        self.assertEqual(row.status, "ok")
        self.assertIn("no board drift", row.detail)

    def test_young_drift_warns_naming_the_number_and_the_sweep(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        flag_drift(directory, (7, NOW - 60))
        (row,) = [
            row
            for row in check(
                [site(drift_max_age_s=600)], store_dir=directory, refs=[(REPO, 7)], now=NOW
            )
            if row.name == f"board-drift:{REPO}"
        ]
        self.assertEqual(row.status, "warn")
        self.assertIn("1 card drifted", row.detail)
        self.assertIn("#7", row.detail)
        self.assertIn("1m ago", row.detail)
        self.assertIn(f"korapy board sweep {REPO} --apply", row.fix)

    def test_drift_older_than_the_max_age_fails(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        flag_drift(directory, (7, NOW - 601))
        row = self.drift_row(
            check([site(drift_max_age_s=600)], store_dir=directory, refs=[(REPO, 7)], now=NOW)
        )
        self.assertEqual(row.status, "fail")
        self.assertIn("10m ago", row.detail)

    def test_drift_at_exactly_the_max_age_still_warns(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        flag_drift(directory, (7, NOW - 600))
        row = self.drift_row(
            check([site(drift_max_age_s=600)], store_dir=directory, refs=[(REPO, 7)], now=NOW)
        )
        self.assertEqual(row.status, "warn")

    def test_many_flags_name_the_first_few_then_count_the_rest(self):
        directory = tempdir()
        stored_numbers(directory, *range(1, 9))
        flag_drift(directory, *range(1, 9))
        row = self.drift_row(
            check(
                [site(drift_max_age_s=600)],
                store_dir=directory,
                refs=[(REPO, number) for number in range(1, 9)],
                now=NOW,
            )
        )
        self.assertEqual(row.status, "warn")
        self.assertIn("8 cards drifted", row.detail)
        self.assertIn("(#1, #2, #3, #4, #5 and 3 more", row.detail)

    def test_other_repos_flags_are_not_counted(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        flag_drift(directory, 9, repo="example/other")
        row = self.drift_row(
            check([site(drift_max_age_s=600)], store_dir=directory, refs=[(REPO, 7)], now=NOW)
        )
        self.assertEqual(row.status, "ok")

    def test_a_site_without_a_max_age_gets_no_drift_row(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        flag_drift(directory, (7, NOW - 3600))
        (row,) = check([site()], store_dir=directory, refs=[(REPO, 7)], now=NOW)
        self.assertEqual(row.name, f"board:{REPO}")

    def test_a_labels_site_gets_no_drift_row(self):
        directory = tempdir()
        stored_numbers(directory, 7)
        flag_drift(directory, (7, NOW - 3600))
        rows = check(
            [site(variant="labels", drift_max_age_s=None)],
            store_dir=directory,
            refs=[(REPO, 7)],
            now=NOW,
        )
        self.assertEqual(len(rows), 1)
        self.assertNotIn("drift", rows[0].name)

    def test_an_unreadable_verdict_table_fails_closed(self):
        class BrokenDriftStore(store.Store):
            def drift_flags(self):
                raise store.StoreError("test verdicts unreadable")

        def open_broken():
            inner = open_temp_store(tempdir())
            return BrokenDriftStore(inner._conn)

        rows = boarddoctor.check_sites(
            [site(drift_max_age_s=600)],
            fetch=fetch_snapshot(),
            open_store=open_broken,
            board_items=items_on((REPO, 7)),
            now=NOW,
        )
        row = self.drift_row(rows)
        self.assertEqual(row.status, "fail")
        self.assertIn("cannot read the board drift verdicts", row.detail)

    def test_sites_carry_the_repo_drift_max_age(self):
        root = tempdir()
        config_dir = tempdir()
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                '[[repo]]\nname = "app"\n'
                '[repo.tracker]\nrepo = "example/demo"\nvariant = "board"\n'
                '[repo.board]\nproject_owner = "example"\nproject_number = 3\n'
            )
        sites, error = boarddoctor.sites_from_settings(root, env={"KORAPY_CONFIG": config_dir})
        self.assertIsNone(error)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].drift_max_age_s, 600)

    def test_sites_honor_a_configured_drift_max_age(self):
        root = tempdir()
        config_dir = tempdir()
        with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                '[[repo]]\nname = "app"\n'
                '[repo.tracker]\nrepo = "example/demo"\nvariant = "board"\n'
                '[repo.board]\nproject_owner = "example"\nproject_number = 3\n'
            )
        sites, error = boarddoctor.sites_from_settings(
            root,
            env={"KORAPY_CONFIG": config_dir, "KORAPY_BOARD_DRIFT_MAX_AGE_S": "900"},
        )
        self.assertIsNone(error)
        self.assertEqual(sites[0].drift_max_age_s, 900)


class OptionValidation(unittest.TestCase):
    def check(self, snap):
        return check([site()], snap=snap)

    def test_a_missing_option_id_is_a_failure_naming_repo_field_and_id(self):
        fields = [
            live_field("Priority", "single_select", (("opt-p0-id", "P0"),))
            if f.name == "Priority"
            else f
            for f in full_live_fields()
        ]
        (row,) = self.check(snapshot(fields))
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("Priority", row.detail)
        self.assertIn("opt-p1-id", row.detail)


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
        directory = tempdir()
        handle = open_temp_store(directory)
        handle.upsert_item(
            store.Item(repo=REPO, number=7, author="alice"), operators=["alice"], now=NOW
        )
        handle.enqueue(REPO, 7, "agent", "agent-1", now=NOW)
        handle.close()
        (row,) = check([site()], store_dir=directory, refs=[(REPO, 7)])
        self.assertEqual(row.status, "fail")
        self.assertIn(REPO, row.detail)
        self.assertIn("flip", row.detail)
        drained = open_temp_store(directory)
        try:
            for queued in drained.queued_writes():
                if queued.repo == REPO:
                    drained.mark_sent(queued.id)
        finally:
            drained.close()
        (row,) = check([site()], store_dir=directory, refs=[(REPO, 7)])
        self.assertEqual(row.status, "ok")


class StoreFailures(unittest.TestCase):
    def check_with_store(self, handle):
        return boarddoctor.check_sites(
            [site()],
            fetch=fetch_snapshot(),
            open_store=lambda: handle,
            board_items=items_on(),
        )

    def test_store_read_failures_fail_the_board_row_naming_the_repo(self):
        for which in ("queued_writes", "items"):
            with self.subTest(which=which):

                class RaisingStore:
                    def queued_writes(self):
                        if which == "queued_writes":
                            raise OSError("no disk")
                        return []

                    def items(self, repo=None, state=None):
                        if which == "items":
                            raise OSError("no disk")
                        return []

                    def close(self):
                        pass

                rows = self.check_with_store(RaisingStore())
                self.assertEqual(rows[0].status, "fail")
                self.assertIn(REPO, rows[0].detail)
                self.assertIn("board store", rows[0].detail)

    def test_an_unopenable_store_names_the_repos(self):
        def fail():
            raise OSError("no disk")

        rows = boarddoctor.check_sites(
            [site(), site(repo="example/other")],
            fetch=fetch_snapshot(),
            open_store=fail,
            board_items=items_on(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "fail")
        self.assertIn(REPO, rows[0].detail)
        self.assertIn("example/other", rows[0].detail)
        self.assertIn("board store", rows[0].detail)

    def test_sites_without_seams_fail_closed(self):
        rows = boarddoctor.check_sites([site()], fetch=None, open_store=None, board_items=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "fail")
        self.assertIn("schema seam", rows[0].detail)


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
        directory = tempdir()
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
        (row,) = check([site()], store_dir=directory, refs=[(REPO, 7), (REPO, 9)])
        self.assertEqual(row.status, "fail")
        self.assertIn("example/demo#7", row.detail)
        self.assertIn("example/demo#9", row.detail)
        self.assertLess(row.detail.index("#7"), row.detail.index("#9"))


class SitesFromSettings(unittest.TestCase):
    def write(self, directory, text):
        with open(os.path.join(directory, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_only_board_mode_repos_become_sites(self):
        root = tempdir()
        config_dir = tempdir()
        self.write(
            root,
            '[[repo]]\nname = "app"\n'
            '[repo.tracker]\nrepo = "example/demo"\nvariant = "board"\n'
            '[repo.board]\nproject_owner = "example"\nproject_number = 3\n'
            '[[repo]]\nname = "plain"\n'
            '[repo.tracker]\nrepo = "example/plain"\n',
        )
        sites, error = boarddoctor.sites_from_settings(root, env={"KORAPY_CONFIG": config_dir})
        self.assertIsNone(error)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].repo, "example/demo")
        self.assertEqual(sites[0].variant, "board")
        board = sites[0].board
        assert board is not None
        self.assertEqual(board.project_owner, "example")
        self.assertEqual(board.project_number, 3)

    def test_a_labels_entry_with_a_table_becomes_a_warn_only_site(self):
        root = tempdir()
        config_dir = tempdir()
        self.write(
            root,
            '[[repo]]\nname = "plain"\n'
            '[repo.tracker]\nrepo = "example/plain"\n'
            '[repo.board]\nproject_owner = "example"\nproject_number = 3\n',
        )
        sites, error = boarddoctor.sites_from_settings(root, env={"KORAPY_CONFIG": config_dir})
        self.assertIsNone(error)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].repo, "example/plain")
        self.assertEqual(sites[0].variant, "labels")
        self.assertIsNotNone(sites[0].board)

    def test_a_broken_ops_file_is_an_error_not_a_crash(self):
        root = tempdir()
        config_dir = tempdir()
        self.write(root, '[[repo]]\nname = "app"\n[repo.board]\nproject_number = 3\n')
        sites, error = boarddoctor.sites_from_settings(root, env={"KORAPY_CONFIG": config_dir})
        self.assertEqual(sites, [])
        self.assertIsNotNone(error)
        self.assertIn("app", error or "")


class AdapterSnapshot(unittest.TestCase):
    """The snapshot and membership reads over canned GraphQL payloads."""

    def reader(self, run):
        from korapy.github import board as boardreader

        return boardreader.BoardReader(config(), run, pacer=FakePacer())

    def snapshot_run(self, *fields, titles=(("PVT_1", 3, TITLE),)):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*fields)])
        run.script("ProvisionBoardTitles", [titles_payload(*titles)])
        return run

    def test_snapshot_reads_title_fields_and_owner_boards(self):
        snap = self.reader(self.snapshot_run(*full_schema_fields())).snapshot()
        self.assertEqual(snap.title, "Board")
        self.assertEqual(len(snap.fields), 7)
        priority = next(f for f in snap.fields if f.name == "Priority")
        self.assertEqual(priority.kind, "single_select")
        self.assertEqual(
            [(o.id, o.name) for o in priority.options],
            [("opt-p0-id", "P0"), ("opt-p1-id", "P1")],
        )
        self.assertEqual(snap.owner_boards, (iface.OwnerBoard(number=3, title=TITLE),))

    def test_snapshot_pages_the_owner_list(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        run.script(
            "ProvisionBoardTitles",
            [
                titles_payload(("PVT_1", 3, TITLE), more=True, end="C1"),
                titles_payload(("PVT_2", 7, "Other")),
            ],
        )
        snap = self.reader(run).snapshot()
        self.assertEqual(
            snap.owner_boards,
            (
                iface.OwnerBoard(number=3, title=TITLE),
                iface.OwnerBoard(number=7, title="Other"),
            ),
        )
        variables = run.graphql_variables("ProvisionBoardTitles")
        self.assertEqual(variables, [{"login": "example"}, {"login": "example", "after": "C1"}])

    def test_snapshot_keeps_duplicate_field_names_for_the_comparison(self):
        fields = full_schema_fields() + [
            select_field("F_prio2", "Priority", [("opt-p9", "P9")]),
        ]
        snap = self.reader(self.snapshot_run(*fields)).snapshot()
        names = [f.name for f in snap.fields if f.name == "Priority"]
        self.assertEqual(len(names), 2)

    def test_snapshot_without_a_title_is_unusable(self):
        payload = dict(schema_payload(*full_schema_fields()))
        payload["user"] = dict(payload["user"])
        payload["user"]["projectV2"] = dict(payload["user"]["projectV2"])
        del payload["user"]["projectV2"]["title"]
        run = BoardFakeRun().script("BoardSchemaByNumber", [payload])
        run.script("ProvisionBoardTitles", [titles_payload(("PVT_1", 3, TITLE))])
        with self.assertRaises(iface.TrackerError):
            self.reader(run).snapshot()

    def test_snapshot_with_a_malformed_option_is_unusable(self):
        fields = [
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": "F_prio",
                "name": "Priority",
                "dataType": "SINGLE_SELECT",
                "options": [{"id": "opt-p0-id"}],
            }
            if f["name"] == "Priority"
            else f
            for f in full_schema_fields()
        ]
        run = self.snapshot_run(*fields)
        with self.assertRaises(iface.TrackerError):
            self.reader(run).snapshot()

    def test_snapshot_rate_limit_maps_to_tracker_rate_limited(self):
        from korapy.github import gh

        run = BoardFakeRun().script(
            "BoardSchemaByNumber",
            [gh.GhError("unavailable", "API rate limit exceeded for user")],
        )
        with self.assertRaises(iface.TrackerRateLimited):
            self.reader(run).snapshot()

    def test_issue_refs_list_repo_and_number_without_rest_reads(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        run.script(
            "BoardItems",
            [
                items_payload(
                    [
                        item_node(7),
                        item_node(8, repo="example/other"),
                        {
                            "id": "PVTI-draft",
                            "updatedAt": "2026-10-28T13:00:00Z",
                            "content": {"__typename": "DraftIssue", "title": "An idea"},
                            "fieldValues": {"nodes": []},
                        },
                        {
                            "id": "PVTI-gone",
                            "updatedAt": "2026-10-28T13:00:00Z",
                            "content": None,
                            "fieldValues": {"nodes": []},
                        },
                    ]
                )
            ],
        )
        found = self.reader(run).issue_refs()
        self.assertEqual(found, {(REPO, 7), ("example/other", 8)})
        self.assertEqual(len(run.calls), 2)

    def test_issue_refs_page_until_the_end(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        first = [item_node(n) for n in range(1, 101)]
        run.script(
            "BoardItems",
            [
                items_payload(first, end="C1", more=True),
                items_payload([item_node(101)]),
            ],
        )
        found = self.reader(run).issue_refs()
        self.assertEqual(found, {(REPO, n) for n in range(1, 102)})
        variables = run.graphql_variables("BoardItems")
        self.assertEqual(variables, [{"project": "PVT_1"}, {"project": "PVT_1", "after": "C1"}])

    def test_issue_refs_with_a_malformed_page_fail_closed(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        run.script(
            "BoardItems",
            [{"node": {"items": {"pageInfo": {"hasNextPage": False}, "nodes": [{"nope": 1}]}}}],
        )
        with self.assertRaises(iface.TrackerError):
            self.reader(run).issue_refs()


class DoctorWiring(unittest.TestCase):
    def env(self, **over):
        from tests.test_doctor import env as base_env

        return base_env(**over)

    def test_no_board_sites_means_no_board_rows(self):
        checks = doctor.run_checks(self.env())
        self.assertEqual([c.name for c in checks if c.name.startswith("board")], [])

    def test_a_board_site_adds_one_row_per_repo_after_prompts(self):
        checks = doctor.run_checks(
            self.env(
                board_sites=[site()],
                board_fetch=fetch_snapshot(),
                board_open_store=lambda: open_temp_store(tempdir()),
                board_items=items_on(),
            )
        )
        names = [c.name for c in checks]
        self.assertIn(f"board:{REPO}", names)
        self.assertLess(names.index("prompts"), names.index(f"board:{REPO}"))

    def test_a_passing_board_check_exits_zero(self):
        checks = doctor.run_checks(
            self.env(
                board_sites=[site()],
                board_fetch=fetch_snapshot(),
                board_open_store=lambda: open_temp_store(tempdir()),
                board_items=items_on(),
            )
        )
        passed = [c for c in checks if c.name == f"board:{REPO}"]
        self.assertEqual([c.status for c in passed], ["ok"])
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_an_unreachable_board_check_fails_with_exit_1(self):
        def fetch(board_config):
            raise iface.TrackerError("board project is missing or ambiguous")

        checks = doctor.run_checks(
            self.env(
                board_sites=[site()],
                board_fetch=fetch,
                board_open_store=lambda: open_temp_store(tempdir()),
                board_items=items_on(),
            )
        )
        failed = [c for c in checks if c.name == f"board:{REPO}"]
        self.assertEqual([c.status for c in failed], ["fail"])
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_a_warn_only_board_check_exits_zero(self):
        snap = snapshot(
            fields=[
                live_field(
                    "Priority",
                    "single_select",
                    (("opt-p0-id", "P0"), ("opt-p1-id", "P1"), ("opt-p9", "P-nine")),
                )
                if f.name == "Priority"
                else f
                for f in full_live_fields()
            ]
        )
        checks = doctor.run_checks(
            self.env(
                board_sites=[site()],
                board_fetch=fetch_snapshot(snap),
                board_open_store=lambda: open_temp_store(tempdir()),
                board_items=items_on(),
            )
        )
        warned = [c for c in checks if c.name == f"board:{REPO}"]
        self.assertEqual([c.status for c in warned], ["warn"])
        self.assertEqual(doctor.exit_code(checks), 0)

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
