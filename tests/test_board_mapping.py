"""Ticket 3: the board mapping writer.

Tests for ``init.render_board_table`` and ``init.board_table_change``:
the machine-written ``[repo.board]`` table, inserted after the named
entry's own block and never rewritten once present. Pure text plus
``settings.load`` round-trips; no subprocess, no network.
"""

import inspect
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from corral import init, settings, tracker as iface


BASE_INLINE = """\
[[repo]]
name = "demo"

[repo.tracker]
repo = "example/demo"
variant = "board"

[repo.clone]
path = "."
default_branch = "main"

[repo.checks]
test = "make check"
"""

GOLDEN_TABLE = """\
[repo.board]
project_owner = "example"  # board owner, written by corral github setup
project_number = 3
agent_field = "Agent"
status_field = "Agent state"
status_column = "Status"
priority_column = "Priority"
blocked_by_column = "Blocked by"

[repo.board.priority_map]  # live option id to rank; names are comments only
"opt-1" = "urgent"  # Urgent
"opt-2" = "high"  # High
"opt-3" = "normal"  # Normal
"opt-4" = "low"  # Low
"opt-5" = "backlog"  # Backlog
"""


def spec_for(board=None, **flags):
    """An ``effective_spec`` for the demo entry, defaulting to no flags."""
    return iface.effective_spec(variant="board", board=board, repo="example/demo", **flags)


def standard_provision(spec, **over):
    """A provision for ``spec``: every asked concept live under its spec name."""
    fields = tuple(
        iface.FieldSpec(concept=field.concept, name=field.name, kind=field.kind, options=())
        for field in spec.fields
    )
    provision = {
        "owner": "example",
        "number": 3,
        "project_id": "node-1",
        "fields": fields,
        "created": (),
        "kept": (),
        "refused": (),
        "priority_map": {
            "opt-1": "urgent",
            "opt-2": "high",
            "opt-3": "normal",
            "opt-4": "low",
            "opt-5": "backlog",
        },
    }
    provision.update(over)
    return iface.BoardProvision(**provision)


def board_config(**over):
    """A ``BoardConfig`` with an addressed board and blank columns."""
    table = {
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
    table.update(over)
    return settings.BoardConfig(**table)


def write_tmp(text):
    """A scratch ``corral.toml`` holding ``text``."""
    tmp = tempfile.TemporaryDirectory()
    target = os.path.join(tmp.name, "corral.toml")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    return tmp, target


class RenderTests(unittest.TestCase):
    def test_standard_provision_matches_the_golden_table(self):
        spec = spec_for()
        self.assertEqual(init.render_board_table(spec, standard_provision(spec)), GOLDEN_TABLE)

    def test_names_come_from_provision_fields_not_defaults(self):
        spec = spec_for(board_config(priority_column="Ranks", status_column="State"))
        provision = standard_provision(spec)
        text = init.render_board_table(spec, provision)
        self.assertIn('priority_column = "Ranks"\n', text)
        self.assertIn('status_column = "State"\n', text)
        self.assertNotIn('"Priority"\n', text)

    def test_a_refused_concept_renders_nothing_never_an_empty_string(self):
        spec = spec_for()
        provision = standard_provision(
            spec,
            fields=tuple(f for f in standard_provision(spec).fields if f.concept != "blocked_by"),
        )
        text = init.render_board_table(spec, provision)
        self.assertNotIn("blocked_by_column", text)
        self.assertNotIn('""', text)

    def test_an_empty_priority_map_omits_the_subtable(self):
        spec = spec_for()
        text = init.render_board_table(spec, standard_provision(spec, priority_map={}))
        self.assertIn("[repo.board]\n", text)
        self.assertNotIn("priority_map", text)

    def test_node_id_address_renders_project_id_not_a_number(self):
        spec = spec_for()
        text = init.render_board_table(spec, standard_provision(spec, number=None))
        self.assertIn('project_id = "node-1"\n', text)
        self.assertNotIn("project_number", text)

    def test_map_entries_order_by_rank_whatever_the_dict_order(self):
        spec = spec_for()
        shuffled = {
            "opt-5": "backlog",
            "opt-3": "normal",
            "opt-1": "urgent",
            "opt-4": "low",
            "opt-2": "high",
        }
        text = init.render_board_table(spec, standard_provision(spec, priority_map=shuffled))
        self.assertEqual(text, GOLDEN_TABLE)


class ChangeTests(unittest.TestCase):
    def change(self, text, entry="demo", **over):
        tmp, target = write_tmp(text)
        self.addCleanup(tmp.cleanup)
        spec = over.pop("spec", None) or spec_for()
        provision = over.pop("provision", None) or standard_provision(spec)
        out = io.StringIO()
        with redirect_stdout(out):
            result = init.board_table_change(target, entry, spec, provision)
        new_text, _ = result
        if new_text is not None:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(new_text)
        with open(target, encoding="utf-8") as handle:
            after = handle.read()
        return result, after, out.getvalue()

    def test_insert_appends_after_the_last_subtable(self):
        (new_text, is_noop), after, _ = self.change(BASE_INLINE)
        self.assertFalse(is_noop)
        self.assertEqual(new_text, BASE_INLINE + "\n" + GOLDEN_TABLE)
        self.assertEqual(after, BASE_INLINE + "\n" + GOLDEN_TABLE)

    def test_insert_attaches_to_the_named_entry_before_a_later_entry(self):
        other = (
            '\n[[repo]]\nname = "other"\n\n[repo.tracker]\nrepo = "example/other"\n'
            'variant = "labels"\n'
        )
        (new_text, is_noop), _, _ = self.change(BASE_INLINE + other)
        self.assertFalse(is_noop)
        head, tail = new_text.split(GOLDEN_TABLE)
        self.assertEqual(head, BASE_INLINE + "\n")
        self.assertEqual(tail, "\n" + other.lstrip("\n"))
        self.assertIn('name = "other"', tail)

    def test_an_identical_table_is_a_noop(self):
        (new_text, is_noop), after, out = self.change(BASE_INLINE + "\n" + GOLDEN_TABLE)
        self.assertTrue(is_noop)
        self.assertIsNone(new_text)
        self.assertEqual(after, BASE_INLINE + "\n" + GOLDEN_TABLE)
        self.assertEqual(out, "")

    def test_a_different_table_refuses_with_the_diff(self):
        other = GOLDEN_TABLE.replace("project_number = 3", "project_number = 9")
        with self.assertRaises(init.InitError) as ctx:
            self.change(BASE_INLINE + "\n" + other)
        self.assertIn("edit it by hand", str(ctx.exception))
        tmp, target = write_tmp(BASE_INLINE + "\n" + other)
        self.addCleanup(tmp.cleanup)
        spec = spec_for()
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(init.InitError):
                init.board_table_change(target, "demo", spec, standard_provision(spec))
        self.assertIn("project_number", out.getvalue())
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), BASE_INLINE + "\n" + other)

    def test_no_force_flag_rewrites_a_different_table(self):
        self.assertNotIn("force", inspect.signature(init.board_table_change).parameters)

    def test_an_unscannable_entry_block_prints_the_table(self):
        inline_table = (
            'repo = [{name = "demo", tracker = {repo = "example/demo", variant = "board"}}]\n'
        )
        tmp, target = write_tmp(inline_table)
        self.addCleanup(tmp.cleanup)
        spec = spec_for()
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(init.InitError):
                init.board_table_change(target, "demo", spec, standard_provision(spec))
        self.assertIn("[repo.board]", out.getvalue())
        self.assertIn("project_number = 3", out.getvalue())

    def test_a_missing_file_names_init_as_the_fix(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, "corral.toml")
        spec = spec_for()
        with self.assertRaises(init.InitError) as ctx:
            init.board_table_change(target, "demo", spec, standard_provision(spec))
        self.assertIn("corral init", str(ctx.exception))

    def test_split_layout_writes_the_ops_file_and_leaves_the_clone_alone(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ops_target = os.path.join(tmp.name, "corral.toml")
        clone_target = os.path.join(tmp.name, "clone.toml")
        with open(ops_target, "w", encoding="utf-8") as handle:
            handle.write(BASE_INLINE)
        with open(clone_target, "w", encoding="utf-8") as handle:
            handle.write("# the project clone's own file\n")
        spec = spec_for()
        out = io.StringIO()
        with redirect_stdout(out):
            new_text, is_noop = init.board_table_change(
                ops_target, "demo", spec, standard_provision(spec)
            )
        self.assertFalse(is_noop)
        with open(ops_target, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        with open(clone_target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# the project clone's own file\n")

    def test_rendered_table_round_trips_through_settings_load(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, "corral.toml")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(BASE_INLINE)
        spec = spec_for()
        provision = standard_provision(spec)
        out = io.StringIO()
        with redirect_stdout(out):
            new_text, _ = init.board_table_change(target, "demo", spec, provision)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        resolved = settings.load(root=tmp.name, repo="demo", env={"CORRAL_CONFIG": tmp.name})
        board = resolved.board
        self.assertIsNotNone(board)
        assert board is not None
        self.assertEqual(board.project_owner, "example")
        self.assertEqual(board.project_number, 3)
        self.assertIsNone(board.project_id)
        self.assertEqual(board.agent_field, "Agent")
        self.assertEqual(board.priority_column, "Priority")
        self.assertEqual(board.area_column, "")
        self.assertEqual(board.priority_map, provision.priority_map)

    def test_a_kept_keep_only_area_renders_so_a_plain_rerun_is_a_noop(self):
        config = board_config(area_column="Area")
        spec = spec_for(config, areas=("Team",))
        provision = standard_provision(spec)
        area_table = GOLDEN_TABLE.replace(
            'priority_column = "Priority"\n',
            'priority_column = "Priority"\narea_column = "Area"\n',
        )
        self.assertEqual(init.render_board_table(spec, provision), area_table)
        (new_text, is_noop), after, _ = self.change(BASE_INLINE, spec=spec, provision=provision)
        self.assertFalse(is_noop)
        self.assertTrue(new_text.endswith(area_table))
        second, _, _ = self.change(after, spec=spec, provision=provision)
        self.assertEqual(second, (None, True))


if __name__ == "__main__":
    unittest.main()
