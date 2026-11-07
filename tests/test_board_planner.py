"""Ticket 1: board types, the standard spec and the pure planner.

Tests for the provider-neutral board provisioning types in
``korapy.tracker``: ``FieldSpec``, ``BoardSpec``, ``BoardProvision``,
``BoardPopulate``, the ``BoardProvisioner`` protocol, ``STANDARD_BOARD``,
``effective_spec`` and the pure ``plan_board`` planner. No seams, no
provider: every input is plain data, so nothing here can reach the
outside world.
"""

import unittest
from typing import Any

from korapy import settings, tracker as iface
from korapy.tracker import (
    BoardPlan,
    BoardPopulate,
    BoardProvision,
    BoardProvisioner,
    BoardSpec,
    FieldPlan,
    FieldSpec,
    LiveField,
    LiveOption,
    plan_board,
)


def table(**over):
    """A ``BoardConfig`` with an addressed board and blank columns."""
    base: dict[str, Any] = {
        "project_owner": "example",
        "project_number": 3,
        "project_id": None,
        "agent_field": "",
        "status_field": "",
        "status_column": "",
        "priority_column": "",
        "area_column": "",
        "pool_column": "",
        "blocked_by_column": "",
        "priority_map": {},
    }
    base.update(over)
    return settings.BoardConfig(**base)


def option(oid, name, color="blue", description=""):
    """One live board option carrying color and description."""
    return LiveOption(id=oid, name=name, color=color, description=description or name)


def select(name, options):
    """One live single-select field."""
    return LiveField(name=name, kind="single_select", options=tuple(options))


def text(name):
    """One live text field."""
    return LiveField(name=name, kind="text")


PRIORITY_OPTIONS = (
    option("opt-1", "Urgent", "red", "handled first"),
    option("opt-2", "High", "orange", "a correctness bug"),
    option("opt-3", "Normal", "yellow", "a feature"),
    option("opt-4", "Low", "green", "cleanup"),
    option("opt-5", "Backlog", "grey", "someday"),
)


def standard_live():
    """A live board matching the standard spec exactly."""
    return (
        text("Agent"),
        text("Agent state"),
        select("Status", [option("st-1", "Todo"), option("st-2", "In Progress")]),
        select("Priority", PRIORITY_OPTIONS),
        text("Blocked by"),
    )


def spec_for(config, repo="example/demo", **flags):
    """An ``effective_spec`` for one config, defaulting to no flags."""
    return iface.effective_spec(variant="board", board=config, repo=repo, **flags)


class StandardBoardTests(unittest.TestCase):
    def test_seven_concepts_with_standard_names_and_kinds(self):
        by_concept = {field.concept: field for field in iface.STANDARD_BOARD}
        self.assertEqual(
            sorted(by_concept),
            ["agent", "area", "blocked_by", "board_status", "pool", "priority", "status"],
        )
        self.assertEqual(
            [(c, by_concept[c].name) for c in sorted(by_concept)],
            [
                ("agent", "Agent"),
                ("area", "Area"),
                ("blocked_by", "Blocked by"),
                ("board_status", "Status"),
                ("pool", "Pool"),
                ("priority", "Priority"),
                ("status", "Agent state"),
            ],
        )
        for concept in ("agent", "status", "blocked_by"):
            self.assertEqual(by_concept[concept].kind, "text")
            self.assertEqual(by_concept[concept].options, ())
        for concept in ("board_status", "priority", "area", "pool"):
            self.assertEqual(by_concept[concept].kind, "single_select")

    def test_priority_options_are_title_cased_ranks_in_order(self):
        priority = next(f for f in iface.STANDARD_BOARD if f.concept == "priority")
        self.assertEqual(priority.options, ("Urgent", "High", "Normal", "Low", "Backlog"))
        self.assertEqual([rank.title() for rank in iface.PRIORITIES], list(priority.options))

    def test_status_options_are_creation_defaults(self):
        status = next(f for f in iface.STANDARD_BOARD if f.concept == "board_status")
        self.assertEqual(status.options, ("Todo", "In Progress", "Done"))

    def test_area_and_pool_carry_no_options(self):
        for concept in ("area", "pool"):
            field = next(f for f in iface.STANDARD_BOARD if f.concept == concept)
            self.assertEqual(field.options, ())
            self.assertFalse(field.keep_only)


class EffectiveSpecTests(unittest.TestCase):
    def test_number_address_carried_from_table(self):
        spec = spec_for(table())
        self.assertEqual(spec.owner, "example")
        self.assertEqual(spec.number, 3)
        self.assertIsNone(spec.project_id)
        self.assertEqual(spec.repo, "example/demo")
        self.assertEqual(spec.title, "demo board")
        self.assertTrue(spec.private)

    def test_node_id_address_carried_from_table(self):
        spec = spec_for(table(project_number=None, project_id="PVT_x"))
        self.assertIsNone(spec.number)
        self.assertEqual(spec.project_id, "PVT_x")

    def test_no_table_means_no_address(self):
        spec = iface.effective_spec(variant="board", board=None, repo="example/demo")
        self.assertIsNone(spec.number)
        self.assertIsNone(spec.project_id)
        self.assertEqual(spec.owner, "example")
        self.assertEqual(spec.title, "demo board")
        self.assertEqual(spec.priority_map, {})

    def test_both_addresses_refused(self):
        with self.assertRaises(ValueError):
            spec_for(table(project_number=3, project_id="PVT_x"))

    def test_non_positive_number_refused(self):
        with self.assertRaises(ValueError):
            spec_for(table(project_number=0, project_id=None))

    def test_priority_maps_are_never_shared(self):
        config = table(priority_map={"opt-1": "urgent"})
        first = spec_for(config)
        second = spec_for(config)
        self.assertIsNot(first.priority_map, second.priority_map)
        self.assertIsNot(first.priority_map, config.priority_map)
        first.priority_map["opt-9"] = "low"
        self.assertNotIn("opt-9", second.priority_map)
        self.assertNotIn("opt-9", config.priority_map)

    def test_flags_override_owner_and_title(self):
        spec = spec_for(table(), owner="other", title="Custom")
        self.assertEqual(spec.owner, "other")
        self.assertEqual(spec.title, "Custom")

    def test_empty_flag_values_refused(self):
        with self.assertRaises(ValueError):
            spec_for(table(), title="")
        with self.assertRaises(ValueError):
            spec_for(table(), owner="")
        with self.assertRaises(ValueError):
            spec_for(table(), areas=("",))

    def test_bad_repo_refused(self):
        for repo in ("demo", "", "a/b/c", "/demo"):
            with self.assertRaises(ValueError):
                iface.effective_spec(variant="board", board=None, repo=repo)

    def test_config_names_win_over_standard(self):
        config = table(
            status_column="State",
            priority_column="Prio",
            priority_map={"opt-7": "high"},
        )
        spec = spec_for(config)
        by_concept = {field.concept: field for field in spec.fields}
        self.assertEqual(by_concept["board_status"].name, "State")
        self.assertEqual(by_concept["priority"].name, "Prio")
        self.assertEqual(by_concept["agent"].name, "Agent")
        self.assertEqual(spec.priority_map, {"opt-7": "high"})
        self.assertIsNot(spec.priority_map, config.priority_map)

    def test_area_mapped_without_flags_is_keep_only(self):
        spec = spec_for(table(area_column="Area", pool_column="Pool"))
        by_concept = {field.concept: field for field in spec.fields}
        self.assertTrue(by_concept["area"].keep_only)
        self.assertEqual(by_concept["area"].options, ())
        self.assertTrue(by_concept["pool"].keep_only)

    def test_area_undeclared_and_unmapped_is_absent(self):
        spec = spec_for(table())
        self.assertNotIn("area", [field.concept for field in spec.fields])
        self.assertNotIn("pool", [field.concept for field in spec.fields])

    def test_declared_options_enter_the_spec(self):
        spec = spec_for(table(), areas=("Mobile", "Web"), pools=("Core",))
        by_concept = {field.concept: field for field in spec.fields}
        self.assertEqual(by_concept["area"].options, ("Mobile", "Web"))
        self.assertFalse(by_concept["area"].keep_only)
        self.assertEqual(by_concept["pool"].options, ("Core",))

    def test_declared_options_repeat_flagged_run_names(self):
        spec = spec_for(table(area_column="Area"), areas=("Mobile",))
        area = next(f for f in spec.fields if f.concept == "area")
        self.assertEqual(area.name, "Area")
        self.assertEqual(area.options, ("Mobile",))
        self.assertFalse(area.keep_only)

    def test_bare_string_options_are_refused_not_split(self):
        with self.assertRaises(ValueError):
            spec_for(table(), areas="Mobile")
        with self.assertRaises(ValueError):
            spec_for(table(), pools="Core")


class PlanBoardTests(unittest.TestCase):
    def test_empty_board_creates_every_mapped_field(self):
        spec = iface.effective_spec(variant="board", board=None, repo="example/demo")
        plan = plan_board(spec, ())
        self.assertIsInstance(plan, BoardPlan)
        by_concept = {field.concept: field for field in plan.fields}
        for concept in ("agent", "status", "board_status", "priority", "blocked_by"):
            self.assertEqual(by_concept[concept].decision, "create")
        self.assertNotIn("area", by_concept)
        self.assertEqual(
            by_concept["priority"].options,
            ("Urgent", "High", "Normal", "Low", "Backlog"),
        )
        self.assertEqual(by_concept["board_status"].options, ("Todo", "In Progress", "Done"))
        self.assertEqual(by_concept["agent"].options, ())
        self.assertEqual(plan.refused, ())

    def test_exact_board_keeps_everything(self):
        config = table(
            agent_field="Agent",
            status_field="Agent state",
            status_column="Status",
            priority_column="Priority",
            blocked_by_column="Blocked by",
            priority_map={f"opt-{n}": rank for n, rank in enumerate(iface.PRIORITIES, 1)},
        )
        plan = plan_board(spec_for(config), standard_live())
        self.assertEqual(plan.refused, ())
        for field in plan.fields:
            self.assertEqual(field.decision, "keep", field.concept)
            self.assertEqual(field.options, ())
            self.assertEqual(field.resend, ())
        self.assertEqual(
            plan.priority_map,
            {f"opt-{n}": rank for n, rank in enumerate(iface.PRIORITIES, 1)},
        )

    def test_wrong_kind_refuses_without_a_lookalike(self):
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            text("Priority"),
            text("Blocked by"),
        )
        plan = plan_board(spec_for(table()), live)
        priority = next(f for f in plan.fields if f.concept == "priority")
        self.assertEqual(priority.decision, "refuse")
        self.assertIn("Priority", priority.fix)
        self.assertEqual(len(plan.refused), 1)
        creates = [f for f in plan.fields if f.decision == "create"]
        self.assertNotIn("priority", [f.concept for f in creates])

    def test_missing_option_plans_one_append(self):
        present = PRIORITY_OPTIONS[:3] + PRIORITY_OPTIONS[4:]
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            select("Priority", present),
            text("Blocked by"),
        )
        config = table(
            agent_field="Agent",
            status_field="Agent state",
            status_column="Status",
            priority_column="Priority",
            blocked_by_column="Blocked by",
        )
        plan = plan_board(spec_for(config), live)
        priority = next(f for f in plan.fields if f.concept == "priority")
        self.assertEqual(priority.decision, "keep")
        self.assertEqual(priority.options, ("Low",))
        self.assertEqual(priority.resend, present)
        self.assertEqual(
            [(o.id, o.name, o.color, o.description) for o in priority.resend],
            [(o.id, o.name, o.color, o.description) for o in present],
        )
        self.assertNotIn("low", plan.priority_map.values())
        self.assertEqual(plan.refused, ())

    def test_extra_team_fields_and_options_are_ignored(self):
        live = standard_live() + (text("Sprint"),)
        priority = select("Priority", PRIORITY_OPTIONS + (option("opt-9", "P0", "black", "team"),))
        live = tuple(f for f in live if f.name != "Priority") + (priority,)
        config = table(
            agent_field="Agent",
            status_field="Agent state",
            status_column="Status",
            priority_column="Priority",
            blocked_by_column="Blocked by",
            priority_map={f"opt-{n}": rank for n, rank in enumerate(iface.PRIORITIES, 1)},
        )
        plan = plan_board(spec_for(config), live)
        self.assertEqual(plan.refused, ())
        for field in plan.fields:
            self.assertEqual(field.decision, "keep", field.concept)
        self.assertNotIn("Sprint", [field.name for field in plan.fields])

    def test_extra_option_rides_the_append_unchanged(self):
        present = PRIORITY_OPTIONS[:3] + PRIORITY_OPTIONS[4:]
        extra = option("opt-9", "P0", "black", "team")
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            select("Priority", present + (extra,)),
            text("Blocked by"),
        )
        config = table(
            agent_field="Agent",
            status_field="Agent state",
            status_column="Status",
            priority_column="Priority",
            blocked_by_column="Blocked by",
        )
        plan = plan_board(spec_for(config), live)
        kept = next(f for f in plan.fields if f.concept == "priority")
        self.assertEqual(kept.decision, "keep")
        self.assertEqual(kept.options, ("Low",))
        self.assertIn(extra, kept.resend)
        self.assertEqual(plan.refused, ())

    def test_renamed_option_keeps_its_rank_by_id(self):
        renamed = (
            option("opt-1", "Urgent"),
            option("opt-2", "P1"),
            option("opt-3", "Normal"),
            option("opt-4", "Low"),
            option("opt-5", "Backlog"),
        )
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            select("Priority", renamed),
            text("Blocked by"),
        )
        config = table(
            agent_field="Agent",
            status_field="Agent state",
            status_column="Status",
            priority_column="Priority",
            blocked_by_column="Blocked by",
            priority_map={"opt-2": "high"},
        )
        plan = plan_board(spec_for(config), live)
        priority = next(f for f in plan.fields if f.concept == "priority")
        self.assertEqual(priority.decision, "keep")
        self.assertEqual(priority.options, ())
        self.assertEqual(plan.priority_map["opt-2"], "high")
        self.assertEqual(plan.refused, ())

    def test_standard_names_resolve_ranks_without_a_map(self):
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            select("Priority", PRIORITY_OPTIONS),
            text("Blocked by"),
        )
        config = table(
            agent_field="Agent",
            status_field="Agent state",
            status_column="Status",
            priority_column="Priority",
            blocked_by_column="Blocked by",
        )
        plan = plan_board(spec_for(config), live)
        priority = next(f for f in plan.fields if f.concept == "priority")
        self.assertEqual(priority.options, ())
        self.assertEqual(
            plan.priority_map,
            {f"opt-{n}": rank for n, rank in enumerate(iface.PRIORITIES, 1)},
        )

    def test_kept_status_is_never_topped_up(self):
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", [option("s-1", "Todo")]),
            select("Priority", PRIORITY_OPTIONS),
            text("Blocked by"),
        )
        plan = plan_board(spec_for(table()), live)
        status = next(f for f in plan.fields if f.concept == "board_status")
        self.assertEqual(status.decision, "keep")
        self.assertEqual(status.options, ())
        self.assertEqual(plan.refused, ())

    def test_kept_area_is_never_topped_up(self):
        spec = spec_for(table(), areas=("Mobile",))
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            select("Priority", PRIORITY_OPTIONS),
            select("Area", [option("a-1", "Web")]),
            text("Blocked by"),
        )
        plan = plan_board(spec, live)
        area = next(f for f in plan.fields if f.concept == "area")
        self.assertEqual(area.decision, "keep")
        self.assertEqual(area.options, ())

    def test_duplicate_field_names_refuse(self):
        live = standard_live() + (text("Priority"),)
        plan = plan_board(spec_for(table()), live)
        priority = next(f for f in plan.fields if f.concept == "priority")
        self.assertEqual(priority.decision, "refuse")
        self.assertIn("duplicate", priority.fix)
        self.assertIn("Priority", priority.fix)
        self.assertEqual(len(plan.refused), 1)

    def test_duplicate_option_names_refuse(self):
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            select("Priority", PRIORITY_OPTIONS + (option("opt-9", "High"),)),
            text("Blocked by"),
        )
        plan = plan_board(spec_for(table()), live)
        priority = next(f for f in plan.fields if f.concept == "priority")
        self.assertEqual(priority.decision, "refuse")
        self.assertIn("High", priority.fix)
        self.assertEqual(len(plan.refused), 1)

    def test_keep_only_area_present_is_kept(self):
        spec = spec_for(table(area_column="Area"))
        live = standard_live() + (select("Area", []),)
        plan = plan_board(spec, live)
        area = next(f for f in plan.fields if f.concept == "area")
        self.assertEqual(area.decision, "keep")
        self.assertEqual(plan.refused, ())

    def test_keep_only_area_absent_names_the_flag(self):
        spec = spec_for(table(area_column="Area"))
        plan = plan_board(spec, standard_live())
        area = next(f for f in plan.fields if f.concept == "area")
        self.assertEqual(area.decision, "refuse")
        self.assertIn("--area", area.fix)
        self.assertEqual(plan.refused, (area.fix,))

    def test_unknown_concept_is_refused(self):
        spec = BoardSpec(
            owner="example",
            title="demo board",
            repo="example/demo",
            fields=(FieldSpec(concept="sprint", name="Sprint", kind="text", options=()),),
        )
        with self.assertRaises(ValueError):
            plan_board(spec, ())

    def test_unknown_kind_is_refused(self):
        spec = BoardSpec(
            owner="example",
            title="demo board",
            repo="example/demo",
            fields=(FieldSpec(concept="agent", name="Agent", kind="number", options=()),),
        )
        with self.assertRaises(ValueError):
            plan_board(spec, ())

    def test_single_select_create_without_options_is_refused(self):
        spec = BoardSpec(
            owner="example",
            title="demo board",
            repo="example/demo",
            fields=(FieldSpec(concept="area", name="Area", kind="single_select", options=()),),
        )
        with self.assertRaises(ValueError):
            plan_board(spec, ())

    def test_spec_map_entries_with_live_ids_are_carried(self):
        live = (
            text("Agent"),
            text("Agent state"),
            select("Status", []),
            select("Priority", PRIORITY_OPTIONS),
            text("Blocked by"),
        )
        config = table(
            agent_field="Agent",
            status_field="Agent state",
            status_column="Status",
            priority_column="Priority",
            blocked_by_column="Blocked by",
            priority_map={"opt-1": "urgent"},
        )
        plan = plan_board(spec_for(config), live)
        self.assertEqual(plan.priority_map["opt-1"], "urgent")


class ProtocolShapeTests(unittest.TestCase):
    def test_tracker_gains_no_board_method(self):
        names = {name for name in dir(iface.Tracker) if not name.startswith("_")}
        self.assertEqual(
            names,
            {
                "item",
                "changes",
                "items",
                "pulls_index",
                "write",
                "comment",
                "add_label",
                "ensure_labels",
                "missing_labels",
                "canonical_repo",
            },
        )

    def test_provisioner_protocol_carries_both_operations(self):
        self.assertTrue(callable(BoardProvisioner.ensure_board))
        self.assertTrue(callable(BoardProvisioner.populate))

    def test_report_shapes(self):
        provision = BoardProvision(
            owner="example",
            number=3,
            project_id="PVT_1",
            fields=(),
            created=("project",),
            kept=(),
            refused=(),
            priority_map={},
        )
        self.assertEqual(provision.created, ("project",))
        populate = BoardPopulate(added=("a/b#1",), kept=(), refused=())
        self.assertEqual(populate.added, ("a/b#1",))
        spec = BoardSpec(owner="example", title="demo board", repo="example/demo", fields=())
        self.assertTrue(spec.private)
        self.assertIsNone(spec.number)
        self.assertIsNone(spec.project_id)
        self.assertIsNone(spec.priority_map)
        field = FieldSpec(concept="agent", name="Agent", kind="text", options=())
        self.assertFalse(field.keep_only)
        plan = FieldPlan(concept="agent", name="Agent", kind="text", decision="keep")
        self.assertEqual(plan.options, ())
        self.assertEqual(plan.resend, ())
        self.assertEqual(plan.fix, "")


if __name__ == "__main__":
    unittest.main()
