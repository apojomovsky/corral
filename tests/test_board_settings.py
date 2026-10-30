"""Per-repo board settings: registry scalars and the ``[repo.board]`` table.

Pure: everything goes through ``settings.resolve`` with plain dicts, so no
file, subprocess or network is touched.
"""

import unittest

from corral import settings


def resolve(**kw):
    kw.setdefault("env", {})
    return settings.resolve(**kw)


def documented_board(**over):
    """The documented ``[repo.board]`` table, with overrides."""
    table = {
        "project_owner": "example",
        "project_number": 3,
        "agent_field": "Agent",
        "status_field": "Agent state",
        "status_column": "Status",
        "priority_column": "Priority",
        "area_column": "Area",
        "pool_column": "Pool",
        "blocked_by_column": "Blocked by",
        "priority_map": {
            "opt-p0-id": "urgent",
            "opt-p1-id": "high",
            "opt-p2-id": "normal",
            "opt-p3-id": "low",
        },
    }
    table.update(over)
    return table


def project(board=None, variant="board", name="app"):
    entry = {"name": name, "tracker": {"repo": "example/demo", "variant": variant}}
    if board is not None:
        entry["board"] = board
    return {"repo": [entry]}


class ScalarDefaults(unittest.TestCase):
    def test_label_mode_is_the_default(self):
        self.assertEqual(resolve()["tracker.variant"], "labels")

    def test_board_scalars_have_small_sane_defaults(self):
        resolved = resolve()
        for key in ("board.poll_pages", "board.schema_max_age_s", "board.item_max_age_s"):
            with self.subTest(key=key):
                value = resolved[key]
                self.assertIsInstance(value, int)
                self.assertGreater(value, 0)

    def test_no_board_table_resolves_to_no_board(self):
        self.assertIsNone(resolve().board)
        self.assertIsNone(resolve(project=project(board=None, variant="labels")).board)

    def test_board_settings_come_from_the_sync_module(self):
        self.assertIn("corral.sync", settings.PROVIDERS)

    def test_scalars_layer_like_every_other_setting(self):
        resolved = resolve(
            host={"board": {"poll_pages": 2}},
            project=project(board=None, variant="labels"),
            repo="app",
            env={"CORRAL_TRACKER_VARIANT": "board", "CORRAL_BOARD_POLL_PAGES": "9"},
            flags={"board.poll_pages": 7},
        )
        self.assertEqual(resolved["tracker.variant"], "board")
        self.assertEqual(resolved["board.poll_pages"], 7)


class DocumentedTable(unittest.TestCase):
    def test_the_documented_table_loads_through_resolve(self):
        resolved = resolve(project=project(documented_board()), repo="app")
        self.assertEqual(resolved["tracker.variant"], "board")
        board = resolved.board
        self.assertEqual(board.project_owner, "example")
        self.assertEqual(board.project_number, 3)
        self.assertIsNone(board.project_id)
        self.assertEqual(board.agent_field, "Agent")
        self.assertEqual(board.status_field, "Agent state")
        self.assertEqual(board.status_column, "Status")
        self.assertEqual(board.priority_column, "Priority")
        self.assertEqual(board.area_column, "Area")
        self.assertEqual(board.pool_column, "Pool")
        self.assertEqual(board.blocked_by_column, "Blocked by")
        self.assertEqual(
            board.priority_map,
            {
                "opt-p0-id": "urgent",
                "opt-p1-id": "high",
                "opt-p2-id": "normal",
                "opt-p3-id": "low",
            },
        )

    def test_option_ids_are_opaque_and_kept_verbatim(self):
        table = documented_board()
        table["priority_map"] = {"weird id?!": "high", "x" * 200: "low"}
        board = resolve(project=project(table), repo="app").board
        self.assertEqual(board.priority_map, {"weird id?!": "high", "x" * 200: "low"})

    def test_a_node_id_loads_instead_of_a_number(self):
        table = documented_board()
        del table["project_number"]
        table["project_id"] = "PVT_node123"
        board = resolve(project=project(table), repo="app").board
        self.assertIsNone(board.project_number)
        self.assertEqual(board.project_id, "PVT_node123")

    def test_concept_columns_default_to_empty(self):
        board = resolve(
            project=project({"project_owner": "example", "project_number": 3}), repo="app"
        ).board
        self.assertEqual(board.project_owner, "example")
        self.assertEqual(board.project_number, 3)
        self.assertEqual(board.agent_field, "")
        self.assertEqual(board.status_field, "")
        self.assertEqual(board.status_column, "")
        self.assertEqual(board.priority_map, {})

    def test_a_board_table_loads_whatever_the_variant(self):
        resolved = resolve(project=project(documented_board(), variant="labels"), repo="app")
        self.assertEqual(resolved["tracker.variant"], "labels")
        self.assertEqual(resolved.board.project_owner, "example")


class ScalarCoexistence(unittest.TestCase):
    def test_a_scalar_only_board_table_stays_a_scalar(self):
        resolved = resolve(
            project={"repo": [{"name": "app", "board": {"hold_label": "paused"}}]}, repo="app"
        )
        self.assertIsNone(resolved.board)
        self.assertEqual(resolved["board.hold_label"], "paused")

    def test_scalars_and_concepts_mix_in_one_table(self):
        table = documented_board(hold_label="paused")
        resolved = resolve(project=project(table), repo="app")
        self.assertEqual(resolved["board.hold_label"], "paused")
        self.assertEqual(resolved.board.project_owner, "example")

    def test_a_scalar_set_twice_is_an_error(self):
        with self.assertRaisesRegex(settings.SettingsError, "board.hold_label is set twice"):
            resolve(
                project={
                    "repo": [
                        {
                            "name": "app",
                            "board": {"hold_label": "paused"},
                            "board.hold_label": "stopped",
                        }
                    ]
                },
                repo="app",
            )

    def test_an_unknown_board_key_names_the_repo_and_key(self):
        with self.assertRaisesRegex(
            settings.SettingsError, "app.*unknown setting 'board.frobnicate'"
        ):
            resolve(project=project(documented_board(frobnicate=1)), repo="app")


class TableErrors(unittest.TestCase):
    def test_a_board_that_is_not_a_table_is_an_error(self):
        for raw in ("Agent", 3, ["Agent"]):
            with self.subTest(raw=raw), self.assertRaises(settings.SettingsError):
                resolve(project=project(raw), repo="app")

    def test_an_empty_board_table_is_an_error(self):
        with self.assertRaisesRegex(settings.SettingsError, "empty table"):
            resolve(project=project({}), repo="app")

    def test_a_missing_owner_or_address_is_an_error(self):
        table = documented_board()
        del table["project_owner"]
        with self.assertRaisesRegex(settings.SettingsError, "app.*project_owner"):
            resolve(project=project(table), repo="app")
        table = documented_board()
        del table["project_number"]
        with self.assertRaisesRegex(settings.SettingsError, "app.*project_number"):
            resolve(project=project(table), repo="app")

    def test_a_number_and_a_node_id_together_are_an_error(self):
        table = documented_board(project_id="PVT_node123")
        with self.assertRaisesRegex(settings.SettingsError, "app.*project_number"):
            resolve(project=project(table), repo="app")

    def test_bad_types_are_errors(self):
        base = documented_board()
        cases = [
            dict(base, project_owner=""),
            dict(base, project_owner=3),
            dict(base, project_number="3"),
            dict(base, project_number=True),
            dict(base, project_number=0),
            dict(base, project_id=""),
            dict(base, agent_field=""),
            dict(base, status_column=3),
            dict(base, priority_map=[]),
            dict(base, priority_map={"": "high"}),
            dict(base, priority_map={"opt-p0-id": ""}),
            dict(base, priority_map={"opt-p0-id": 3}),
        ]
        for table in cases:
            with self.subTest(table=table), self.assertRaises(settings.SettingsError):
                resolve(project=project(table), repo="app")

    def test_a_priority_map_without_a_priority_column_is_an_error(self):
        table = documented_board()
        del table["priority_column"]
        with self.assertRaisesRegex(settings.SettingsError, "app.*priority_map.*priority_column"):
            resolve(project=project(table), repo="app")

    def test_every_entry_is_checked_not_only_the_selected_one(self):
        other = {"name": "other", "board": documented_board(frobnicate="nope")}
        mine = {"name": "app", "tracker": {"variant": "labels"}}
        with self.assertRaisesRegex(settings.SettingsError, "other.*frobnicate"):
            resolve(project={"repo": [mine, other]}, repo="app")

    def test_a_board_table_never_reads_as_a_setting(self):
        with self.assertRaisesRegex(settings.SettingsError, "unknown setting"):
            resolve(host={"board": {"priority_map": {"x": "high"}}})


if __name__ == "__main__":
    unittest.main()
