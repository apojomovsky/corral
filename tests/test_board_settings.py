"""Per-repo board settings: registry scalars and the ``[repo.board]`` table.

Pure: everything goes through ``settings.resolve`` with plain dicts, so no
file, subprocess or network is touched.
"""

import os
import tempfile
import unittest

from korapy import settings


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
        for key in (
            "board.poll_pages",
            "board.schema_max_age_s",
            "board.item_max_age_s",
            "board.add_per_pass",
        ):
            with self.subTest(key=key):
                value = resolved[key]
                self.assertIsInstance(value, int)
                self.assertGreater(value, 0)

    def test_board_adopt_cap_defaults_to_twenty(self):
        self.assertEqual(resolve()["board.add_per_pass"], 20)

    def test_board_status_cap_defaults_to_twenty(self):
        self.assertEqual(resolve()["board.status_per_pass"], 20)

    def test_no_board_table_resolves_to_no_board(self):
        self.assertIsNone(resolve().board)
        self.assertIsNone(resolve(project=project(board=None, variant="labels")).board)

    def test_board_settings_come_from_the_sync_module(self):
        self.assertIn("korapy.sync", settings.PROVIDERS)

    def test_scalars_layer_like_every_other_setting(self):
        resolved = resolve(
            host={"board": {"poll_pages": 2}},
            project=project(board=None, variant="labels"),
            repo="app",
            env={"KORAPY_TRACKER_VARIANT": "board", "KORAPY_BOARD_POLL_PAGES": "9"},
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

    def test_status_option_ids_default_to_empty(self):
        board = resolve(
            project=project({"project_owner": "example", "project_number": 3}), repo="app"
        ).board
        self.assertEqual(board.status_available, "")
        self.assertEqual(board.status_claimed, "")
        self.assertEqual(board.status_review, "")
        self.assertEqual(board.status_done, "")

    def test_status_option_ids_load_through_resolve(self):
        table = documented_board(
            status_available="opt-a",
            status_claimed="opt-c",
            status_review="opt-r",
            status_done="opt-d",
        )
        board = resolve(project=project(table), repo="app").board
        self.assertEqual(board.status_available, "opt-a")
        self.assertEqual(board.status_claimed, "opt-c")
        self.assertEqual(board.status_review, "opt-r")
        self.assertEqual(board.status_done, "opt-d")

    def test_status_option_ids_without_a_status_column_are_an_error(self):
        table = documented_board(status_done="opt-d")
        del table["status_column"]
        with self.assertRaisesRegex(settings.SettingsError, "app.*status.*status_column"):
            resolve(project=project(table), repo="app")

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


class VariantValidation(unittest.TestCase):
    def test_a_typo_names_the_repo_and_the_key(self):
        # A typo next to a complete [repo.board] table still fails here,
        # instead of silently running label mode forever.
        with self.assertRaisesRegex(settings.SettingsError, "app.*tracker\\.variant.*bord"):
            resolve(project=project(documented_board(), variant="bord"), repo="app")

    def test_every_entry_is_checked_not_only_the_selected_one(self):
        other = {"name": "other", "tracker": {"variant": "carrier-pigeon"}}
        mine = {"name": "app", "tracker": {"variant": "labels"}}
        with self.assertRaisesRegex(settings.SettingsError, "other.*tracker\\.variant"):
            resolve(project={"repo": [mine, other]}, repo="app")

    def test_an_env_override_typo_is_rejected(self):
        with self.assertRaisesRegex(settings.SettingsError, "app.*tracker\\.variant"):
            resolve(
                project=project(board=None, variant="labels"),
                repo="app",
                env={"KORAPY_TRACKER_VARIANT": "bord"},
            )

    def test_both_variants_resolve(self):
        for variant in ("labels", "board"):
            resolved = resolve(project=project(board=None, variant=variant), repo="app")
            self.assertEqual(resolved["tracker.variant"], variant)


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
            dict(base, project_id=3),
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

    def test_board_tables_are_checked_with_no_selected_repo(self):
        # Entry validation runs for every [[repo]] entry, not only the
        # selected one: a bad board table fails even when no repo is read.
        with self.assertRaisesRegex(settings.SettingsError, "app.*frobnicate"):
            resolve(project=project(documented_board(frobnicate="nope")))
        resolved = resolve(project=project(documented_board()))
        self.assertIsNone(resolved.board)


class KeyLinkage(unittest.TestCase):
    def test_columns_are_a_subset_of_keys(self):
        self.assertLessEqual(set(settings._BOARD_COLUMNS), set(settings._BOARD_KEYS))

    def test_status_ids_are_a_subset_of_keys(self):
        self.assertLessEqual(set(settings._BOARD_STATUS_IDS), set(settings._BOARD_KEYS))

    def test_keys_are_address_columns_status_ids_and_map(self):
        self.assertEqual(
            set(settings._BOARD_KEYS),
            set(settings._BOARD_ADDRESS)
            | set(settings._BOARD_COLUMNS)
            | {"priority_map"}
            | set(settings._BOARD_STATUS_IDS),
        )

    def test_board_config_fields_match_keys(self):
        self.assertEqual(set(settings.BoardConfig._fields), set(settings._BOARD_KEYS))


class TomlText(unittest.TestCase):
    def test_repo_board_headers_resolve_from_toml_text(self):
        text = (
            "[[repo]]\n"
            'name = "app"\n'
            "\n"
            "[repo.tracker]\n"
            'repo = "example/demo"\n'
            'variant = "board"\n'
            "\n"
            "[repo.board]\n"
            'project_owner = "example"\n'
            "project_number = 3\n"
            'agent_field = "Agent"\n'
            'status_column = "Status"\n'
            'priority_column = "Priority"\n'
            "\n"
            "[repo.board.priority_map]\n"
            '"opt-p0-id" = "urgent"\n'
            '"opt-p1-id" = "high"\n'
        )
        with tempfile.TemporaryDirectory(prefix="korapy-board-settings-") as tmp:
            config = os.path.join(tmp, "config")
            os.makedirs(config)
            root = os.path.join(tmp, "ops")
            os.makedirs(root)
            with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
                handle.write(text)
            resolved = settings.load(root=root, repo="app", env={"KORAPY_CONFIG": config})
        self.assertEqual(resolved["tracker.variant"], "board")
        self.assertEqual(resolved["tracker.repo"], "example/demo")
        board = resolved.board
        self.assertEqual(board.project_owner, "example")
        self.assertEqual(board.project_number, 3)
        self.assertIsNone(board.project_id)
        self.assertEqual(board.agent_field, "Agent")
        self.assertEqual(board.status_column, "Status")
        self.assertEqual(board.priority_column, "Priority")
        self.assertEqual(board.status_field, "")
        self.assertEqual(board.priority_map, {"opt-p0-id": "urgent", "opt-p1-id": "high"})


if __name__ == "__main__":
    unittest.main()
