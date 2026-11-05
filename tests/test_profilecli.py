"""`korapy profile`: operator verbs over the local selections.

Offline: declarations validate against the real runtime adapters with an
injected provider pool, selections round-trip through a temp file, and
the store and tracker are fakes. No subprocesses, no network, no real
state or config dirs.
"""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace

from korapy import accounts, board, profiles, settings
from korapy import tracker as iface
from tests.test_profiles_launch import FakeStore, row
from tests.test_tracker import FakeTracker, reading

T0 = 1_800_000_000.0
SOURCE = os.path.join("cfg", "profiles.toml")


def pool(*names):
    return [accounts.Provider(name=name, reader="pkg:Reader") for name in names]


def declared():
    return {
        "default": {
            "runtime": "muse",
            "models": {"default": {"chain": ["provider-a/model-x"]}},
        },
        "fast": {
            "runtime": "omp",
            "models": {"default": {"chain": ["provider-a/model-x"]}},
            "options": {},
        },
    }


def parsed(names=None):
    return profiles.validate(names if names is not None else declared(), ["provider-a"])


def resolved(project=None, host=None):
    return settings.resolve(project=project or {}, host=host or {}, env={})


class ListRows(unittest.TestCase):
    def test_lists_every_profile_sorted_with_runtime_chains_and_source(self):
        from korapy import profilecli

        rows = profilecli.list_rows(parsed(), {"default": "proj", "fast": "host"}, {})
        self.assertEqual(
            rows,
            [
                {
                    "name": "default",
                    "runtime": "muse",
                    "models": {"default": ["provider-a/model-x"]},
                    "options": {},
                    "source": "proj",
                    "shadowed": None,
                },
                {
                    "name": "fast",
                    "runtime": "omp",
                    "models": {"default": ["provider-a/model-x"]},
                    "options": {},
                    "source": "host",
                    "shadowed": None,
                },
            ],
        )

    def test_a_shadowed_name_carries_the_losing_source(self):
        from korapy import profilecli

        rows = profilecli.list_rows(parsed(), {"fast": "host"}, {"fast": "proj"})
        self.assertEqual(rows[1]["source"], "host")
        self.assertEqual(rows[1]["shadowed"], "proj")

    def test_no_declarations_list_as_empty(self):
        from korapy import profilecli

        self.assertEqual(profilecli.list_rows({}, {}, {}), [])

    def test_text_names_the_runtime_source_and_shadow(self):
        from korapy import profilecli

        text = profilecli.render_list(
            profilecli.list_rows(parsed(), {"default": "proj", "fast": "host"}, {"fast": "proj"}),
            json_mode=False,
        )
        self.assertIn("default: runtime=muse", text)
        self.assertIn("from proj", text)
        self.assertIn("shadows proj", text)
        self.assertIn("provider-a/model-x", text)

    def test_text_with_no_profiles_says_so(self):
        from korapy import profilecli

        self.assertIn("no profiles declared", profilecli.render_list([], json_mode=False))

    def test_json_shape(self):
        from korapy import profilecli

        doc = json.loads(
            profilecli.render_list(
                profilecli.list_rows(parsed(), {"default": "p", "fast": "h"}, {}),
                json_mode=True,
            )
        )
        self.assertEqual([row["name"] for row in doc["profiles"]], ["default", "fast"])
        self.assertEqual(doc["profiles"][0]["runtime"], "muse")
        self.assertIsNone(doc["profiles"][0]["shadowed"])


class ShowDocument(unittest.TestCase):
    def test_flag_layer_wins_with_provenance(self):
        from korapy import profilecli

        doc = profilecli.show_document(
            parsed(),
            profiles.empty_selections(),
            repo="myrepo",
            role="worker",
            number=7,
            now=T0,
            flag="fast",
            committed={"default": None, "repos": {}, "roles": {}},
            selections_source=SOURCE,
        )
        self.assertEqual(doc["profile"], "fast")
        self.assertEqual(doc["runtime"], "omp")
        self.assertEqual(doc["provenance"]["layer"], 1)
        self.assertEqual(doc["provenance"]["layer_name"], "flag")
        self.assertIn("--profile fast", doc["provenance"]["source"])
        winner = [entry for entry in doc["layers"] if entry["state"] == "selected"]
        self.assertEqual([(entry["layer"], entry["profile"]) for entry in winner], [(1, "fast")])

    def test_ticket_layer_from_assignment(self):
        from korapy import profilecli

        doc = profilecli.show_document(
            parsed(),
            profiles.empty_selections(),
            repo="myrepo",
            role="worker",
            number=7,
            now=T0,
            tracker="fast",
            tracker_source="label 'profile:fast' on myrepo#7",
            committed={"default": None, "repos": {}, "roles": {}},
            selections_source=SOURCE,
        )
        self.assertEqual(doc["profile"], "fast")
        self.assertEqual(doc["provenance"]["layer"], 2)
        self.assertEqual(doc["provenance"]["layer_name"], "ticket")

    def test_tracker_and_pin_disagreement_refuses_naming_both(self):
        from korapy import profilecli

        selections = profiles.Selections(pins={profiles.pin_key("myrepo", 7): "default"})
        with self.assertRaises(settings.SettingsError) as ctx:
            profilecli.show_document(
                parsed(),
                selections,
                repo="myrepo",
                role="worker",
                number=7,
                now=T0,
                tracker="fast",
                tracker_source="the tracker assignment",
                committed={"default": None, "repos": {}, "roles": {}},
                selections_source=SOURCE,
            )
        self.assertIn("fast", str(ctx.exception))
        self.assertIn("default", str(ctx.exception))

    def test_winning_unknown_name_refuses(self):
        from korapy import profilecli

        with self.assertRaises(settings.SettingsError) as ctx:
            profilecli.show_document(
                parsed(),
                profiles.empty_selections(),
                repo="myrepo",
                role="worker",
                number=7,
                now=T0,
                flag="nope",
                committed={"default": None, "repos": {}, "roles": {}},
                selections_source=SOURCE,
            )
        self.assertIn("nope", str(ctx.exception))

    def test_json_shape_carries_provenance_and_layers(self):
        from korapy import profilecli

        doc = json.loads(
            profilecli.render_show(
                profilecli.show_document(
                    parsed(),
                    profiles.empty_selections(),
                    repo="myrepo",
                    role="worker",
                    number=7,
                    now=T0,
                    committed={"default": None, "repos": {}, "roles": {}},
                    selections_source=SOURCE,
                ),
                json_mode=True,
            )
        )
        self.assertEqual(doc["profile"], "default")
        self.assertEqual(doc["provenance"]["layer_name"], "default")
        self.assertEqual(len(doc["layers"]), 6)
        self.assertEqual([entry["layer"] for entry in doc["layers"]], [1, 2, 3, 4, 5, 6])

    def test_text_names_the_winner_and_the_full_chain(self):
        from korapy import profilecli

        text = profilecli.render_show(
            profilecli.show_document(
                parsed(),
                profiles.Selections(default="default"),
                repo="myrepo",
                role="worker",
                number=7,
                now=T0,
                flag="fast",
                committed={"default": None, "repos": {}, "roles": {}},
                selections_source=SOURCE,
            ),
            json_mode=False,
        )
        self.assertIn("profile: fast", text)
        self.assertIn("runtime: omp", text)
        self.assertIn("layer 1 (flag)", text)
        self.assertIn("[selected]", text)
        self.assertIn("[shadowed]", text)


class SetSelection(unittest.TestCase):
    def test_global_project_role_and_ticket_write_their_slots(self):
        from korapy import profilecli

        fresh = profiles.empty_selections()
        updated, message = profilecli.set_selection(fresh, scope="global", name="fast", now=T0)
        self.assertEqual(updated.default, "fast")
        self.assertIn("fast", message)
        updated, message = profilecli.set_selection(
            fresh, scope="project", repo="myrepo", name="fast", now=T0
        )
        self.assertEqual(updated.projects, {"myrepo": "fast"})
        self.assertIn("myrepo", message)
        updated, message = profilecli.set_selection(
            fresh, scope="role", role="reviewer", name="fast", now=T0
        )
        self.assertEqual(updated.roles, {"reviewer": "fast"})
        self.assertIn("reviewer", message)
        updated, message = profilecli.set_selection(
            fresh, scope="ticket", repo="myrepo", number=7, name="fast", now=T0
        )
        self.assertEqual(updated.pins, {"myrepo#7": "fast"})
        self.assertIn("myrepo#7", message)

    def test_temp_writes_an_override_with_scope_and_until(self):
        from korapy import profilecli

        updated, message = profilecli.set_selection(
            profiles.empty_selections(),
            scope="project",
            repo="myrepo",
            name="fast",
            temp="2h",
            reason="trying it",
            now=T0,
        )
        assert updated.override is not None
        self.assertEqual(updated.override.scope, "project:myrepo")
        self.assertEqual(updated.override.profile, "fast")
        self.assertEqual(updated.override.until, T0 + 7200)
        self.assertEqual(updated.override.reason, "trying it")
        self.assertIn("until", message)

    def test_until_takes_epoch_seconds(self):
        from korapy import profilecli

        updated, _ = profilecli.set_selection(
            profiles.empty_selections(),
            scope="global",
            name="fast",
            until=str(T0 + 60),
            now=T0,
        )
        assert updated.override is not None
        self.assertEqual(updated.override.until, T0 + 60)

    def test_setting_over_an_override_replaces_it_and_names_what_stood(self):
        from korapy import profilecli

        first, _ = profilecli.set_selection(
            profiles.empty_selections(), scope="global", name="slow", temp="1h", now=T0
        )
        second, message = profilecli.set_selection(
            first, scope="role", role="worker", name="fast", temp="2h", now=T0
        )
        assert second.override is not None
        self.assertEqual(second.override.profile, "fast")
        self.assertIn("slow", message)
        self.assertIn("replaced", message)

    def test_temp_with_ticket_is_refused_naming_layer_2_and_the_fix(self):
        from korapy import profilecli

        with self.assertRaises(settings.SettingsError) as ctx:
            profilecli.set_selection(
                profiles.empty_selections(),
                scope="ticket",
                repo="myrepo",
                number=7,
                name="fast",
                temp="2h",
                now=T0,
            )
        text = str(ctx.exception)
        self.assertIn("layer 2", text)
        self.assertIn("pin", text)

    def test_temp_and_until_together_are_refused(self):
        from korapy import profilecli

        with self.assertRaises(settings.SettingsError):
            profilecli.set_selection(
                profiles.empty_selections(),
                scope="global",
                name="fast",
                temp="2h",
                until=str(T0 + 60),
                now=T0,
            )

    def test_a_bad_duration_is_refused_naming_the_value(self):
        from korapy import profilecli

        with self.assertRaises(settings.SettingsError) as ctx:
            profilecli.set_selection(
                profiles.empty_selections(), scope="global", name="fast", temp="2x", now=T0
            )
        self.assertIn("2x", str(ctx.exception))

    def test_an_empty_name_is_refused(self):
        from korapy import profilecli

        with self.assertRaises(settings.SettingsError):
            profilecli.set_selection(profiles.empty_selections(), scope="global", name="", now=T0)


class ResetSelection(unittest.TestCase):
    def test_each_scope_clears_its_slot(self):
        from korapy import profilecli

        full = profiles.Selections(
            default="fast",
            projects={"myrepo": "fast"},
            roles={"worker": "fast"},
            pins={"myrepo#7": "fast"},
            override=profiles.Override(scope="global", profile="fast", until=T0 + 60),
        )
        updated, _ = profilecli.reset_selection(full, scope="global")
        self.assertIsNone(updated.default)
        updated, _ = profilecli.reset_selection(full, scope="project", repo="myrepo")
        self.assertEqual(updated.projects, {})
        updated, _ = profilecli.reset_selection(full, scope="role", role="worker")
        self.assertEqual(updated.roles, {})
        updated, _ = profilecli.reset_selection(full, scope="ticket", repo="myrepo", number=7)
        self.assertEqual(updated.pins, {})
        updated, message = profilecli.reset_selection(full, scope="temp")
        self.assertIsNone(updated.override)
        self.assertIn("override", message)

    def test_reset_with_nothing_stored_exits_1_naming_the_piece(self):
        from korapy import profilecli

        empty = profiles.empty_selections()
        with self.assertRaises(profilecli.EmptyReset) as ctx:
            profilecli.reset_selection(empty, scope="global")
        self.assertIn("global default", str(ctx.exception))
        with self.assertRaises(profilecli.EmptyReset) as ctx:
            profilecli.reset_selection(empty, scope="project", repo="myrepo")
        self.assertIn("myrepo", str(ctx.exception))
        with self.assertRaises(profilecli.EmptyReset) as ctx:
            profilecli.reset_selection(empty, scope="role", role="worker")
        self.assertIn("worker", str(ctx.exception))
        with self.assertRaises(profilecli.EmptyReset) as ctx:
            profilecli.reset_selection(empty, scope="ticket", repo="myrepo", number=7)
        self.assertIn("myrepo#7", str(ctx.exception))
        with self.assertRaises(profilecli.EmptyReset) as ctx:
            profilecli.reset_selection(empty, scope="temp")
        self.assertIn("override", str(ctx.exception))


def seams(tmp, **over):
    from korapy import profilecli

    path = os.path.join(tmp, "profiles.toml")
    current = {"selections": profiles.empty_selections()}

    def load_selections():
        return profiles.load_selections(path), path

    def save(updated):
        current["selections"] = updated
        return profiles.save_selections(updated, path)

    fields = {
        "settings_fn": lambda: (
            resolved(
                project={"profiles": declared()},
                host={"profiles": {"fast": {"runtime": "omp"}}},
            ),
            None,
        ),
        "selections_fn": load_selections,
        "save_fn": save,
        "pool_fn": lambda: pool("provider-a"),
        "now_fn": lambda: T0,
        "store": FakeStore(),
        "tracker": None,
        "tracker_for": lambda root, repo: FakeTracker([]),
    }
    fields.update(over)
    return profilecli.Seams(**fields)


class RunVerb(unittest.TestCase):
    def run_cli(self, argv, tmp, **over):
        from korapy import profilecli

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = profilecli.run_verb(argv, seams(tmp, **over))
        return code, out.getvalue(), err.getvalue()

    def test_list_reports_declared_profiles_with_sources(self):
        from korapy import profilecli  # noqa: F401

        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self.run_cli(["profile", "list"], tmp)
        self.assertEqual(code, 0)
        self.assertIn("default: runtime=muse", out)
        self.assertIn("fast: runtime=omp", out)

    def test_list_json_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self.run_cli(["profile", "list", "--json"], tmp)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual([row["name"] for row in doc["profiles"]], ["default", "fast"])

    def test_show_resolves_the_global_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_cli(["profile", "set", "--global", "fast"], tmp)[0], 0)
            code, out, _ = self.run_cli(["profile", "show"], tmp)
        self.assertEqual(code, 0)
        self.assertIn("profile: fast", out)
        self.assertIn("layer 6 (default)", out)

    def test_show_reads_the_ticket_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self.run_cli(["profile", "set", "--ticket", "myrepo", "7", "fast"], tmp)
            self.assertEqual(code, 0)
            code, out, _ = self.run_cli(
                ["profile", "show", "--repo", "myrepo", "--ticket", "7"], tmp
            )
        self.assertEqual(code, 0)
        self.assertIn("profile: fast", out)
        self.assertIn("layer 2 (ticket)", out)

    def test_show_with_a_ticket_needs_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(["profile", "show", "--ticket", "7"], tmp)
        self.assertEqual(code, 4)
        self.assertIn("--repo", err)

    def test_show_with_a_dangling_global_selection_is_exit_4(self):
        # The old test passed --profile to show, which has no such flag,
        # so argparse's unrecognized-arguments rejection (exit 4) fired
        # before the dangling selection was ever resolved. Store the
        # dangling name first, then show: the refusal must come from
        # resolve naming the value.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_cli(["profile", "set", "--global", "nope"], tmp)[0], 0)
            code, _, err = self.run_cli(["profile", "show"], tmp)
        self.assertEqual(code, 4)
        self.assertIn("nope", err)

    def test_show_with_an_unreachable_tracker_is_exit_3(self):
        fake = FakeTracker([])
        fake.item_errors[("myrepo", 7)] = "boom"
        store = FakeStore(found=row(labels=["profile:fast"]), fresh=False)
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(
                ["profile", "show", "--repo", "myrepo", "--ticket", "7"],
                tmp,
                store=store,
                tracker=fake,
            )
        self.assertEqual(code, 3)
        self.assertIn("boom", err)

    def test_show_with_a_rejected_tracker_read_is_exit_1(self):
        class RejectingTracker(FakeTracker):
            def item(self, repo, number):
                raise iface.TrackerRejected("tracker rejected: gh: Validation Failed (HTTP 422)")

        store = FakeStore(found=row(labels=["profile:fast"]), fresh=False)
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(
                ["profile", "show", "--repo", "myrepo", "--ticket", "7"],
                tmp,
                store=store,
                tracker=RejectingTracker([]),
            )
        self.assertEqual(code, 1)
        self.assertIn("tracker rejected", err)

    def test_set_writes_through_and_show_reads_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self.run_cli(["profile", "set", "--global", "fast"], tmp)
            self.assertEqual(code, 0)
            self.assertIn("fast", out)
            code, out, _ = self.run_cli(["profile", "show"], tmp)
            self.assertEqual(code, 0)
            self.assertIn("profile: fast", out)

    def test_set_ticket_with_temp_is_exit_4_naming_layer_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(
                ["profile", "set", "--ticket", "myrepo", "7", "fast", "--temp", "2h"], tmp
            )
        self.assertEqual(code, 4)
        self.assertIn("layer 2", err)

    def test_reset_with_nothing_stored_is_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(["profile", "reset", "--temp"], tmp)
        self.assertEqual(code, 1)
        self.assertIn("override", err)

    def test_reset_clears_what_set_wrote(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_cli(["profile", "set", "--global", "fast"], tmp)[0], 0)
            code, _, _ = self.run_cli(["profile", "reset", "--global"], tmp)
            self.assertEqual(code, 0)
            code, _, _ = self.run_cli(["profile", "reset", "--global"], tmp)
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
