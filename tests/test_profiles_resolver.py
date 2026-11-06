"""Agent profiles, ticket 2: selections and the resolver.

Offline: the resolver is a pure function over plain data, and the
selections file lives in a temp dir per test. No subprocesses, no
network, no real state dirs.
"""

import os
import tempfile
import unittest

from korapy import board, paths, profiles, settings, tracker as iface
from tests.test_tracker import FakeTracker, reading


def profile(name, runtime="omp"):
    return profiles.Profile(name=name, runtime=runtime)


def parsed(*names):
    return {name: profile(name) for name in names}


def sel(default=None, projects=None, roles=None, pins=None, override=None):
    return profiles.Selections(
        default=default,
        projects=dict(projects or {}),
        roles=dict(roles or {}),
        pins=dict(pins or {}),
        override=override,
    )


def over(scope, name, until, reason=""):
    return profiles.Override(scope=scope, profile=name, until=until, reason=reason)


T0 = 1_800_000_000.0
REPO = "example"
ROLE = "worker"
NUMBER = 12


def resolve(parsed_profiles, selections=None, **kw):
    args = {"repo": REPO, "role": ROLE, "number": NUMBER, "now": T0}
    args.update(kw)
    return profiles.resolve(parsed_profiles, selections or sel(), **args)  # ty: ignore[invalid-argument-type]


class SelectionsPath(unittest.TestCase):
    def test_the_selections_file_lives_in_the_host_config_dir(self):
        self.assertEqual(profiles.SELECTIONS_FILE, "profiles.toml")
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            self.assertEqual(
                profiles.selections_path(config_dir=directory),
                os.path.join(directory, "profiles.toml"),
            )

    def test_the_default_path_comes_from_the_config_dir_resolver(self):
        self.assertEqual(
            profiles.selections_path(), os.path.join(paths.config_dir(), "profiles.toml")
        )

    def test_a_pin_key_names_the_ticket_unambiguously(self):
        self.assertEqual(profiles.pin_key("owner/name", 12), "owner/name#12")


class LoadSave(unittest.TestCase):
    def test_a_missing_file_reads_as_empty_selections(self):
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            found = profiles.load_selections(os.path.join(directory, "profiles.toml"))
        self.assertEqual(found, profiles.empty_selections())

    def test_a_full_file_round_trips_through_save_and_load(self):
        selections = sel(
            default="fast",
            projects={REPO: "fast"},
            roles={ROLE: "careful"},
            pins={profiles.pin_key("owner/name", NUMBER): "fast"},
            override=over("project:example", "fast", T0 + 3600, "trying it out"),
        )
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            path = os.path.join(directory, "profiles.toml")
            profiles.save_selections(selections, path)
            self.assertEqual(profiles.load_selections(path), selections)

    def test_save_creates_the_config_dir_first(self):
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            path = os.path.join(directory, "nested", "profiles.toml")
            profiles.save_selections(sel(default="fast"), path)
            self.assertEqual(profiles.load_selections(path).default, "fast")

    def test_a_dangling_reference_loads_cleanly_for_the_doctor_to_report(self):
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            path = os.path.join(directory, "profiles.toml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('[default]\nprofile = "ghost"\n')
            self.assertEqual(profiles.load_selections(path).default, "ghost")

    def test_a_corrupt_file_is_exit_4(self):
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            path = os.path.join(directory, "profiles.toml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not = [valid toml\n")
            with self.assertRaises(settings.SettingsError):
                profiles.load_selections(path)

    def test_an_unknown_top_level_table_is_exit_4(self):
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            path = os.path.join(directory, "profiles.toml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('[selections]\nprofile = "fast"\n')
            with self.assertRaises(settings.SettingsError) as ctx:
                profiles.load_selections(path)
            self.assertIn("selections", str(ctx.exception))

    def test_a_second_override_table_is_exit_4(self):
        with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
            path = os.path.join(directory, "profiles.toml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    '[override]\nscope = "global"\nprofile = "a"\nuntil = 1900000000\n'
                    '[override]\nscope = "global"\nprofile = "b"\nuntil = 1900000001\n'
                )
            with self.assertRaises(settings.SettingsError):
                profiles.load_selections(path)

    def test_bad_shapes_are_exit_4(self):
        bad = [
            "[default]\nprofile = 7\n",
            '[projects.example]\nprofile = ["fast"]\n',
            "[roles.worker]\n",
            '[pins."owner/name#x"]\nprofile = "fast"\n',
            '[pins."noname"]\nprofile = "fast"\n',
            '[override]\nscope = "ticket:example#1"\nprofile = "fast"\nuntil = 1900000000\n',
            '[override]\nscope = "global"\nprofile = "fast"\nuntil = "soon"\n',
            '[override]\nscope = ""\nprofile = "fast"\nuntil = 1900000000\n',
            '[override]\nprofile = "fast"\nuntil = 1900000000\n',
        ]
        for text in bad:
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory(prefix="korapy-config-") as directory:
                    path = os.path.join(directory, "profiles.toml")
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(text)
                    with self.assertRaises(settings.SettingsError):
                        profiles.load_selections(path)


class TicketAssignment(unittest.TestCase):
    def test_no_assignment_reads_as_absent(self):
        name, _ = profiles.ticket_assignment(labels=[], extra={}, repo=REPO, number=NUMBER)
        self.assertIsNone(name)

    def test_a_profile_label_names_the_assignment(self):
        name, source = profiles.ticket_assignment(
            labels=["ready", "profile:fast"], extra={}, repo=REPO, number=NUMBER
        )
        self.assertEqual(name, "fast")
        self.assertIn("profile:fast", source)

    def test_a_repeated_label_is_one_assignment(self):
        name, _ = profiles.ticket_assignment(
            labels=["profile:fast", "profile:fast"], extra={}, repo=REPO, number=NUMBER
        )
        self.assertEqual(name, "fast")

    def test_two_distinct_profile_labels_refuse_closed(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.ticket_assignment(
                labels=["profile:a", "profile:b"], extra={}, repo=REPO, number=NUMBER
            )
        self.assertIn("profile:a", str(ctx.exception))
        self.assertIn("profile:b", str(ctx.exception))

    def test_an_empty_profile_label_refuses_closed(self):
        with self.assertRaises(settings.SettingsError):
            profiles.ticket_assignment(labels=["profile:"], extra={}, repo=REPO, number=NUMBER)

    def test_a_board_profile_field_is_carried_verbatim(self):
        name, source = profiles.ticket_assignment(
            labels=[], extra={"profile": "Fast-Backend"}, repo=REPO, number=NUMBER
        )
        self.assertEqual(name, "Fast-Backend")
        self.assertIn("profile", source)

    def test_an_empty_board_field_reads_as_absent(self):
        for extra in (None, {}, {"profile": ""}, {"profile": None}):
            with self.subTest(extra=extra):
                name, _ = profiles.ticket_assignment(
                    labels=[], extra=extra, repo=REPO, number=NUMBER
                )
                self.assertIsNone(name)

    def test_a_non_string_board_value_refuses_closed(self):
        with self.assertRaises(settings.SettingsError):
            profiles.ticket_assignment(
                labels=[], extra={"profile": ["fast"]}, repo=REPO, number=NUMBER
            )

    def test_a_non_string_label_element_refuses_closed(self):
        for labels in (["profile:fast", 7], [None], [b"profile:fast"], [True]):
            with self.subTest(labels=labels):
                with self.assertRaises(settings.SettingsError):
                    profiles.ticket_assignment(labels=labels, extra={}, repo=REPO, number=NUMBER)

    def test_a_string_labels_value_is_refused_not_read_as_characters(self):
        for labels in ("profile:fast", None, 7, {"profile:fast": True}):
            with self.subTest(labels=labels):
                with self.assertRaises(settings.SettingsError):
                    profiles.ticket_assignment(labels=labels, extra={}, repo=REPO, number=NUMBER)

    def test_a_non_dict_extra_container_is_refused(self):
        for extra in ("profile:fast", ["profile:fast"], 7):
            with self.subTest(extra=extra):
                with self.assertRaises(settings.SettingsError):
                    profiles.ticket_assignment(labels=[], extra=extra, repo=REPO, number=NUMBER)

    def test_agreeing_label_and_field_resolve_together(self):
        name, source = profiles.ticket_assignment(
            labels=["profile:fast"], extra={"profile": "fast"}, repo=REPO, number=NUMBER
        )
        self.assertEqual(name, "fast")
        self.assertIn("profile:fast", source)

    def test_disagreeing_label_and_field_refuse_closed(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.ticket_assignment(
                labels=["profile:a"], extra={"profile": "b"}, repo=REPO, number=NUMBER
            )
        self.assertIn("profile:a", str(ctx.exception))


class EachLayerWinsAlone(unittest.TestCase):
    def test_the_flag_wins_alone(self):
        found, provenance = resolve(parsed("flagged", "other"), flag="flagged")
        self.assertEqual(found.name, "flagged")
        self.assertEqual(provenance.layer, 1)
        self.assertEqual(provenance.layer_name, "flag")
        self.assertIn("flagged", provenance.source)

    def test_the_tracker_assignment_wins_alone(self):
        name, source = profiles.ticket_assignment(
            labels=["profile:fast"], extra={}, repo=REPO, number=NUMBER
        )
        found, provenance = resolve(parsed("fast", "other"), tracker=name, tracker_source=source)
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 2)
        self.assertEqual(provenance.layer_name, "ticket")

    def test_the_pin_wins_alone(self):
        selections = sel(pins={profiles.pin_key(REPO, NUMBER): "fast"})
        found, provenance = resolve(parsed("fast", "other"), selections)
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 2)

    def test_a_live_override_wins_alone(self):
        selections = sel(override=over("global", "fast", T0 + 3600))
        found, provenance = resolve(parsed("fast", "other"), selections)
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 3)
        self.assertEqual(provenance.until, T0 + 3600)

    def test_the_project_selection_wins_alone(self):
        selections = sel(projects={REPO: "fast"})
        found, provenance = resolve(parsed("fast", "other"), selections)
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 4)

    def test_the_role_selection_wins_alone(self):
        selections = sel(roles={ROLE: "fast"})
        found, provenance = resolve(parsed("fast", "other"), selections)
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 5)

    def test_the_local_default_wins_alone(self):
        found, provenance = resolve(parsed("fast", "other"), sel(default="fast"))
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 6)

    def test_the_committed_default_wins_alone(self):
        found, provenance = resolve(parsed("fast", "other"), committed={"default": "fast"})
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 6)

    def test_nothing_set_falls_back_to_the_builtin_default(self):
        found, provenance = resolve(parsed("other"))
        self.assertEqual(found, profiles.BUILTIN_DEFAULT)
        self.assertEqual(provenance.layer, 6)
        self.assertEqual(provenance.layer_name, "default")
        self.assertIn("built-in", provenance.source)


class Precedence(unittest.TestCase):
    def test_a_more_specific_layer_beats_every_lower_one(self):
        everything = sel(
            default="sixth",
            roles={ROLE: "fifth"},
            projects={REPO: "fourth"},
            override=over("global", "third", T0 + 60),
            pins={profiles.pin_key(REPO, NUMBER): "second"},
        )
        all_names = ("flagged", "second", "third", "fourth", "fifth", "sixth")
        found, provenance = resolve(parsed(*all_names), everything, flag="flagged")
        self.assertEqual((found.name, provenance.layer), ("flagged", 1))
        found, provenance = resolve(
            parsed(*all_names), everything, tracker="second", tracker_source="a label"
        )
        self.assertEqual((found.name, provenance.layer), ("second", 2))
        no_pin = sel(
            default="sixth",
            roles={ROLE: "fifth"},
            projects={REPO: "fourth"},
            override=over("global", "third", T0 + 60),
        )
        found, provenance = resolve(parsed(*all_names), no_pin)
        self.assertEqual((found.name, provenance.layer), ("third", 3))
        lower = sel(default="sixth", roles={ROLE: "fifth"}, projects={REPO: "fourth"})
        found, provenance = resolve(parsed(*all_names), lower)
        self.assertEqual((found.name, provenance.layer), ("fourth", 4))
        found, provenance = resolve(parsed(*all_names), sel(default="sixth", roles={ROLE: "fifth"}))
        self.assertEqual((found.name, provenance.layer), ("fifth", 5))
        found, provenance = resolve(parsed(*all_names), sel(default="sixth"))
        self.assertEqual((found.name, provenance.layer), ("sixth", 6))

    def test_local_beats_committed_inside_each_layer(self):
        selections = sel(
            default="local-six",
            roles={ROLE: "local-five"},
            projects={REPO: "local-four"},
        )
        committed = {
            "default": "committed-six",
            "repos": {REPO: "committed-four"},
            "roles": {ROLE: "committed-five"},
        }
        found, provenance = resolve(
            parsed("local-four", "local-five", "local-six"), selections, committed=committed
        )
        self.assertEqual(found.name, "local-four")
        role_committed = {
            "default": "committed-six",
            "roles": {ROLE: "committed-five"},
        }
        found, _ = resolve(
            parsed("local-five", "local-six"),
            sel(roles={ROLE: "local-five"}, default="local-six"),
            committed=role_committed,
        )
        self.assertEqual(found.name, "local-five")
        found, _ = resolve(
            parsed("local-six"),
            sel(default="local-six"),
            committed={"default": "committed-six"},
        )
        self.assertEqual(found.name, "local-six")

    def test_committed_project_beats_committed_role_beats_committed_default(self):
        committed = {
            "default": "sixth",
            "repos": {REPO: "fourth"},
            "roles": {ROLE: "fifth"},
        }
        found, provenance = resolve(parsed("fourth", "fifth", "sixth"), committed=committed)
        self.assertEqual((found.name, provenance.layer), ("fourth", 4))
        committed = {"default": "sixth", "roles": {ROLE: "fifth"}}
        found, provenance = resolve(parsed("fifth", "sixth"), committed=committed)
        self.assertEqual((found.name, provenance.layer), ("fifth", 5))

    def test_an_agreeing_pin_and_tracker_resolve_together(self):
        name, source = profiles.ticket_assignment(
            labels=["profile:fast"], extra={}, repo=REPO, number=NUMBER
        )
        selections = sel(pins={profiles.pin_key(REPO, NUMBER): "fast"})
        found, provenance = resolve(parsed("fast"), selections, tracker=name, tracker_source=source)
        self.assertEqual((found.name, provenance.layer), ("fast", 2))


class FailClosed(unittest.TestCase):
    def test_an_unknown_flag_refuses_before_anything_is_made(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            resolve(parsed("fast"), flag="ghost")
        self.assertIn("ghost", str(ctx.exception))

    def test_a_ticket_assignment_naming_an_unknown_profile_refuses(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            resolve(parsed("fast"), tracker="ghost", tracker_source="a label")
        self.assertIn("ghost", str(ctx.exception))

    def test_a_tracker_pin_disagreement_names_both_sources(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            resolve(
                parsed("fast"),
                sel(pins={profiles.pin_key(REPO, NUMBER): "fast"}),
                tracker="other",
                tracker_source="label 'profile:other'",
            )
        self.assertIn("label 'profile:other'", str(ctx.exception))
        self.assertIn("pin", str(ctx.exception))

    def test_a_dangling_local_selection_fails_instead_of_falling_back(self):
        selections = sel(projects={REPO: "ghost"})
        with self.assertRaises(settings.SettingsError) as ctx:
            resolve(parsed("fast"), selections, committed={"repos": {REPO: "fast"}})
        self.assertIn("ghost", str(ctx.exception))

    def test_an_expired_override_is_ignored(self):
        selections = sel(default="sixth", override=over("global", "fast", T0 - 1))
        found, provenance = resolve(parsed("fast", "sixth"), selections)
        self.assertEqual((found.name, provenance.layer), ("sixth", 6))

    def test_an_out_of_scope_override_is_ignored(self):
        selections = sel(default="sixth", override=over("project:elsewhere", "fast", T0 + 3600))
        found, _ = resolve(parsed("fast", "sixth"), selections)
        self.assertEqual(found.name, "sixth")
        selections = sel(default="sixth", override=over("role:reviewer", "fast", T0 + 3600))
        found, _ = resolve(parsed("fast", "sixth"), selections)
        self.assertEqual(found.name, "sixth")

    def test_a_scoped_override_applies_only_in_scope(self):
        selections = sel(override=over("project:example", "fast", T0 + 3600))
        found, _ = resolve(parsed("fast", "sixth"), selections)
        self.assertEqual(found.name, "fast")
        selections = sel(override=over("role:worker", "fast", T0 + 3600))
        found, _ = resolve(parsed("fast", "sixth"), selections)
        self.assertEqual(found.name, "fast")

    def test_bad_request_shapes_are_exit_4(self):
        for kw in (
            {"repo": ""},
            {"repo": None},
            {"role": ""},
            {"number": 0},
            {"number": -3},
            {"number": True},
            {"now": float("nan")},
            {"now": "soon"},
            {"committed": ["fast"]},
            {"committed": {"repos": "fast"}},
        ):
            with self.subTest(kw=kw):
                with self.assertRaises(settings.SettingsError):
                    resolve(parsed("fast"), sel(default="fast"), **kw)

    def test_an_unknown_committed_key_is_exit_4(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            resolve(parsed("fast"), sel(default="fast"), committed={"bogus": "fast"})
        self.assertIn("bogus", str(ctx.exception))

    def test_a_committed_default_of_the_wrong_type_is_exit_4(self):
        for default in (7, "", ["fast"], {"profile": "fast"}):
            with self.subTest(default=default):
                with self.assertRaises(settings.SettingsError):
                    resolve(parsed("fast"), sel(), committed={"default": default})

    def test_a_falsy_mistyped_committed_table_is_refused_not_dropped(self):
        for committed in (
            {"repos": []},
            {"repos": ""},
            {"repos": 0},
            {"repos": False},
            {"roles": []},
            {"roles": ""},
            {"roles": 0},
            {"roles": False},
        ):
            with self.subTest(committed=committed):
                with self.assertRaises(settings.SettingsError):
                    resolve(parsed("fast"), sel(), committed=committed)

    def test_selections_none_reads_as_empty(self):
        found, provenance = profiles.resolve(
            parsed("fast"),
            None,  # ty: ignore[invalid-argument-type]
            repo=REPO,
            role=ROLE,
            number=NUMBER,
            now=T0,
            committed={"default": "fast"},
        )
        self.assertEqual((found.name, provenance.layer), ("fast", 6))

    def test_an_empty_flag_string_is_exit_4_naming_the_value(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.resolve(
                parsed("fast"),
                sel(),
                repo=REPO,
                role=ROLE,
                number=NUMBER,
                now=T0,
                flag="",
            )
        self.assertIn("''", str(ctx.exception))


class Explain(unittest.TestCase):
    def test_the_chain_names_every_layer_with_its_source(self):
        selections = sel(
            default="sixth",
            roles={ROLE: "fifth"},
            projects={REPO: "fourth"},
            override=over("global", "third", T0 + 60),
            pins={profiles.pin_key(REPO, NUMBER): "second"},
        )
        chain = profiles.explain(
            parsed("flagged", "second", "third", "fourth", "fifth", "sixth"),
            selections,
            repo=REPO,
            role=ROLE,
            number=NUMBER,
            now=T0,
            flag="flagged",
        )
        self.assertEqual([entry.layer for entry in chain], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [entry.profile for entry in chain],
            ["flagged", "second", "third", "fourth", "fifth", "sixth"],
        )
        self.assertEqual(
            [entry.state for entry in chain],
            ["selected", "shadowed", "shadowed", "shadowed", "shadowed", "shadowed"],
        )
        self.assertTrue(all(entry.source for entry in chain))

    def test_absent_expired_and_unknown_states_are_visible(self):
        selections = sel(
            projects={REPO: "fast"},
            roles={ROLE: "ghost"},
            override=over("global", "fast", T0 - 1),
        )
        chain = profiles.explain(
            parsed("fast", "sixth"),
            selections,
            repo=REPO,
            role=ROLE,
            number=NUMBER,
            now=T0,
            committed={"default": "sixth"},
        )
        by_layer = {entry.layer: entry for entry in chain}
        self.assertEqual(by_layer[1].state, "absent")
        self.assertEqual(by_layer[2].state, "absent")
        self.assertEqual(by_layer[3].state, "expired")
        self.assertEqual(by_layer[4].state, "selected")
        self.assertEqual(by_layer[5].state, "unknown")
        self.assertEqual(by_layer[6].state, "shadowed")

    def test_an_out_of_scope_override_is_marked_not_silent(self):
        selections = sel(override=over("project:elsewhere", "fast", T0 + 3600))
        chain = profiles.explain(
            parsed("fast"),
            selections,
            repo=REPO,
            role=ROLE,
            number=NUMBER,
            now=T0,
        )
        by_layer = {entry.layer: entry for entry in chain}
        self.assertEqual(by_layer[3].state, "out-of-scope")
        self.assertEqual(by_layer[6].profile, None)

    def test_explain_agrees_with_resolve_on_the_winner(self):
        selections = sel(projects={REPO: "fast"}, default="sixth")
        chain = profiles.explain(
            parsed("fast", "sixth"),
            selections,
            repo=REPO,
            role=ROLE,
            number=NUMBER,
            now=T0,
        )
        found, provenance = resolve(parsed("fast", "sixth"), selections)
        winners = [entry for entry in chain if entry.state == "selected"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].profile, found.name)
        self.assertEqual(winners[0].layer, provenance.layer)


def stored_row(directory, *, labels=(), extra=None, fetched_at):
    """A board store holding one row for the ticket, fetched at `fetched_at`."""
    store = board.open_store(os.path.join(directory, "board.db"))
    store.upsert_item(
        board.Item(
            repo=REPO,
            number=NUMBER,
            kind="issue",
            title="Title",
            body="Body",
            state="open",
            author="operator",
            labels=list(labels),
            priority="",
            assignees=[],
            url=f"https://example.com/{REPO}/{NUMBER}",
            tracker_updated_at=fetched_at,
            extra=dict(extra or {}),
        ),
        operators=["operator"],
        now=fetched_at,
    )
    return store


STALE_AFTER = 3600.0


def load(store, tracker, **kw):
    args = {"repo": REPO, "number": NUMBER, "now": T0, "stale_after": STALE_AFTER}
    args.update(kw)
    return profiles.load_tracker_assignment(store, tracker, **args)


class StoreAssignment(unittest.TestCase):
    def test_a_fresh_labels_row_resolves_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(directory, labels=["ready", "profile:fast"], fetched_at=T0)
            fake = FakeTracker([])
            name, source, skip = load(store, fake)
        self.assertIsNone(skip)
        self.assertEqual(name, "fast")
        self.assertIn("profile:fast", source)
        self.assertEqual(fake.calls, [])
        found, provenance = resolve(parsed("fast", "other"), tracker=name, tracker_source=source)
        self.assertEqual((found.name, provenance.layer), ("fast", 2))

    def test_a_fresh_board_field_row_resolves_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(
                directory, labels=["ready"], extra={"profile": "fast"}, fetched_at=T0
            )
            name, source, skip = load(store, FakeTracker([]))
        self.assertIsNone(skip)
        self.assertEqual(name, "fast")
        self.assertIn("profile", source)
        found, provenance = resolve(parsed("fast", "other"), tracker=name, tracker_source=source)
        self.assertEqual((found.name, provenance.layer), ("fast", 2))

    def test_a_row_without_assignment_reads_absent(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(directory, labels=["ready"], fetched_at=T0)
            fake = FakeTracker([])
            name, source, skip = load(store, fake)
        self.assertIsNone(skip)
        self.assertIsNone(name)
        self.assertEqual(source, "")
        self.assertEqual(fake.calls, [])
        found, provenance = resolve(
            parsed("other"), sel(default="other"), tracker=name, tracker_source=source
        )
        self.assertEqual((found.name, provenance.layer), ("other", 6))

    def test_a_missing_row_skips_as_withdrawn(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = board.open_store(os.path.join(directory, "board.db"))
            fake = FakeTracker([])
            name, source, skip = load(store, fake)
        self.assertIsNone(name)
        self.assertEqual(source, "")
        self.assertIn("withdrawn", skip)
        self.assertEqual(fake.calls, [])

    def test_a_stale_row_is_reread_live_not_trusted(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(directory, labels=["ready"], fetched_at=T0 - 2 * STALE_AFTER)
            fake = FakeTracker([reading(repo=REPO, number=NUMBER, labels=("profile:live",))])
            name, source, skip = load(store, fake)
        self.assertIsNone(skip)
        self.assertEqual(name, "live")
        self.assertIn("profile:live", source)
        self.assertIn(("item", REPO, NUMBER), fake.calls)

    def test_a_fresh_row_never_touches_a_failing_tracker(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(directory, labels=["profile:fast"], fetched_at=T0)
            fake = FakeTracker([])
            fake.item_errors[(REPO, NUMBER)] = "down"
            name, _, skip = load(store, fake)
        self.assertIsNone(skip)
        self.assertEqual(name, "fast")
        self.assertEqual(fake.calls, [])

    def test_a_stale_row_with_an_unreachable_tracker_raises_exit_3(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(directory, labels=["ready"], fetched_at=T0 - 2 * STALE_AFTER)
            fake = FakeTracker([])
            fake.item_errors[(REPO, NUMBER)] = "down"
            with self.assertRaises(iface.TrackerError) as ctx:
                load(store, fake)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_a_stale_row_without_a_tracker_uses_the_cached_row(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(
                directory,
                labels=["profile:cached"],
                fetched_at=T0 - 2 * STALE_AFTER,
            )
            name, _, skip = load(store, None)
        self.assertIsNone(skip)
        self.assertEqual(name, "cached")

    def test_a_corrupt_shared_value_fails_closed_instead_of_skipping(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(
                directory,
                labels=["profile:a", "profile:b"],
                fetched_at=T0,
            )
            with self.assertRaises(settings.SettingsError):
                load(store, FakeTracker([]))

    def test_a_store_pin_disagreement_names_both_sources(self):
        with tempfile.TemporaryDirectory(prefix="korapy-board-") as directory:
            store = stored_row(directory, labels=["profile:fast"], fetched_at=T0)
            name, source, _ = load(store, FakeTracker([]))
            selections = sel(pins={profiles.pin_key(REPO, NUMBER): "other"})
            with self.assertRaises(settings.SettingsError) as ctx:
                resolve(parsed("fast", "other"), selections, tracker=name, tracker_source=source)
        self.assertIn("profile:fast", str(ctx.exception))
        self.assertIn("pin", str(ctx.exception))


class DeclaredDefault(unittest.TestCase):
    """A declared [profiles.default] replaces the built-in default.

    The converted config that failed here declares only the table (no
    [profile] selection): resolution must use the declared table, never
    the built-in default silently.
    """

    def test_a_declared_default_wins_when_nothing_selects_one(self):
        table = {"default": profile("default", "muse"), "other": profile("other")}
        found, provenance = resolve(table)
        self.assertEqual(found.name, "default")
        self.assertEqual(found.runtime, "muse")
        self.assertEqual(provenance.layer, 6)
        self.assertEqual(provenance.layer_name, "default")
        self.assertNotIn("built-in", provenance.source)

    def test_explain_marks_the_declared_default_selected(self):
        table = {"default": profile("default", "muse"), "other": profile("other")}
        chain = profiles.explain(table, sel(), repo=REPO, role=ROLE, number=NUMBER, now=T0)
        layer6 = next(entry for entry in chain if entry.layer == 6)
        self.assertEqual(layer6.state, "selected")
        self.assertEqual(layer6.profile, "default")

    def test_a_selected_default_still_beats_the_declared_table(self):
        table = {"default": profile("default", "muse"), "fast": profile("fast")}
        found, provenance = resolve(table, sel(default="fast"))
        self.assertEqual(found.name, "fast")
        self.assertEqual(provenance.layer, 6)
