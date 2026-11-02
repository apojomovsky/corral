"""Agent profiles, ticket 5: launch and resume wiring.

Offline: the resolver inputs are plain dicts, the store and tracker are
fakes, and the pool and ledger are injected. No subprocesses, no network,
no real state dirs.
"""

import sqlite3
import unittest
from types import SimpleNamespace

from corral import accounts, board, launch, models, profiles, settings
from corral import tracker as iface
from tests.test_tracker import FakeTracker, reading

T0 = 1_800_000_000.0
REPO = "example"
ROLE = "worker"
NUMBER = 12
STALE_AFTER = 3600.0


def resolve(project=None, host=None, repo=None):
    return settings.resolve(project=project or {}, host=host or {}, repo=repo, env={})


def selections(default=None, projects=None, roles=None, pins=None, override=None):
    return profiles.Selections(
        default=default,
        projects=dict(projects or {}),
        roles=dict(roles or {}),
        pins=dict(pins or {}),
        override=override,
    )


class FakeStore:
    """A board store fake: a canned item, or a canned failure. Records reads."""

    def __init__(self, found=None, fresh=True, error=None):
        self.found = found
        self.fresh = fresh
        self.error = error
        self.calls = []

    def item(self, repo, number, *, now=None, max_age=None):
        self.calls.append((repo, number))
        if self.error is not None:
            raise self.error
        return self.found, self.fresh


def row(labels=(), extra=None):
    return SimpleNamespace(labels=list(labels), extra=dict(extra or {}))


def pool(*names):
    return [accounts.Provider(name=name, reader="pkg:Reader") for name in names]


def ledger(*live, now=T0):
    readings = {
        name: {
            "windows": {"hour": {"used_pct": 5, "resets_at": now + 3600}},
            "read_at": now - 120,
        }
        for name in live
    }
    return {"readings": readings, "marks": {}}


def target(resolved, repo=REPO):
    return SimpleNamespace(repo=repo, resolved=resolved)


def select(tgt, number=NUMBER, **kw):
    args = {
        "role": ROLE,
        "selections": selections(),
        "store": FakeStore(),
        "tracker": None,
        "pool": pool("provider-a"),
        "ledger": ledger("provider-a"),
        "now": T0,
    }
    args.update(kw)
    return launch.select_profile(tgt, number, **args)


class SettingsCarriage(unittest.TestCase):
    def test_profiles_tables_are_carried_raw(self):
        declared = {"fast": {"runtime": "omp"}}
        resolved = resolve(project={"profiles": declared})
        self.assertEqual(resolved.profiles, declared)
        self.assertEqual(resolved.profile_committed, {"default": None, "repos": {}, "roles": {}})

    def test_absent_profiles_read_as_no_declarations(self):
        resolved = resolve(project={})
        self.assertEqual(resolved.profiles, {})

    def test_host_profiles_shadow_committed_ones_per_name(self):
        resolved = resolve(
            project={"profiles": {"fast": {"runtime": "omp"}}},
            host={"profiles": {"fast": {"runtime": "muse"}}},
        )
        self.assertEqual(resolved.profiles, {"fast": {"runtime": "muse"}})

    def test_a_non_table_profiles_value_is_exit_4(self):
        for where in ({"project": {"profiles": "fast"}}, {"host": {"profiles": ["fast"]}}):
            with self.subTest(where=where):
                with self.assertRaises(settings.SettingsError):
                    resolve(**where)

    def test_the_committed_default_is_carried(self):
        resolved = resolve(project={"profile": {"default": "fast"}})
        self.assertEqual(resolved.profile_committed["default"], "fast")

    def test_a_bad_committed_default_shape_is_exit_4(self):
        for project in (
            {"profile": {"default": 7}},
            {"profile": {"other": "fast"}},
            {"profile": "fast"},
        ):
            with self.subTest(project=project):
                with self.assertRaises(settings.SettingsError):
                    resolve(project=project)

    def test_a_repo_profile_is_carried(self):
        resolved = resolve(project={"repo": [{"name": REPO, "profile": "fast"}]}, repo=REPO)
        self.assertEqual(resolved.profile_committed["repos"], {REPO: "fast"})

    def test_a_non_string_repo_profile_is_exit_4(self):
        with self.assertRaises(settings.SettingsError):
            resolve(project={"repo": [{"name": REPO, "profile": 7}]}, repo=REPO)

    def test_a_role_profile_is_carried(self):
        resolved = resolve(project={"role": {"reviewer": {"profile": "fast"}}})
        self.assertEqual(resolved.profile_committed["roles"], {"reviewer": "fast"})

    def test_a_bad_role_profile_shape_is_exit_4(self):
        for project in (
            {"role": {"reviewer": {"profile": 7}}},
            {"role": {"reviewer": {"other": "fast"}}},
            {"role": "fast"},
        ):
            with self.subTest(project=project):
                with self.assertRaises(settings.SettingsError):
                    resolve(project=project)

    def test_committed_defaults_are_project_only(self):
        for host in ({"profile": {"default": "fast"}}, {"role": {"worker": {"p": "x"}}}):
            with self.subTest(host=host):
                with self.assertRaises(settings.SettingsError):
                    resolve(host=host)


class SelectionGate(unittest.TestCase):
    def test_no_declarations_and_no_flag_returns_none_without_touching_the_store(self):
        tgt = target(resolve(project={}))
        store = FakeStore()
        self.assertIsNone(select(tgt, store=store))
        self.assertEqual(store.calls, [])

    def test_a_flag_with_no_declarations_still_refuses_an_unknown_name(self):
        tgt = target(resolve(project={}))
        with self.assertRaises(profiles.ProfileError):
            select(tgt, flag="fast")


class FlagLayer(unittest.TestCase):
    def test_the_flag_selects_the_named_profile(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        found = select(tgt, flag="fast")
        self.assertEqual(found.profile.name, "fast")
        self.assertEqual(found.runtime, "muse")
        self.assertEqual(found.provenance.layer, 1)
        self.assertIn("--profile fast", found.provenance.source)

    def test_a_model_refinement_is_noted_in_provenance(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        found = select(tgt, flag="fast", model="provider-a/model-x")
        self.assertEqual(found.model, "provider-a/model-x")
        self.assertIn("--model provider-a/model-x", found.provenance.source)

    def test_a_runtime_refinement_replaces_the_adapter(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        found = select(tgt, flag="fast", runtime="omp")
        self.assertEqual(found.runtime, "omp")
        self.assertIn("--runtime omp", found.provenance.source)

    def test_no_refinement_leaves_provenance_untouched(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        found = select(tgt, flag="fast")
        self.assertNotIn("refined", found.provenance.source)


class StoreFailures(unittest.TestCase):
    def test_a_truncated_database_maps_to_exit_4(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        store = FakeStore(error=sqlite3.DatabaseError("file is not a database"))
        with self.assertRaises(launch.LaunchError) as ctx:
            select(tgt, store=store)
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_a_corrupt_store_maps_to_exit_4(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        store = FakeStore(error=board.StoreError("unusable item"))
        with self.assertRaises(launch.LaunchError) as ctx:
            select(tgt, store=store)
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_a_withdrawn_row_falls_through_to_lower_layers(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        store = FakeStore(found=None, fresh=False)
        found = select(tgt, store=store, selections=selections(default="fast"), flag=None)
        self.assertEqual((found.profile.name, found.provenance.layer), ("fast", 6))

    def test_a_stale_row_with_a_dead_tracker_is_exit_3(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        store = FakeStore(found=row(labels=["ready"]), fresh=False)
        fake = FakeTracker([])
        fake.item_errors[(REPO, NUMBER)] = "down"
        with self.assertRaises(iface.TrackerError) as ctx:
            select(tgt, store=store, tracker=fake)
        self.assertEqual(ctx.exception.exit_code, 3)


class TicketLayer(unittest.TestCase):
    def test_a_store_label_selects_layer_2(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "muse"}}}))
        store = FakeStore(found=row(labels=["ready", "profile:fast"]), fresh=True)
        found = select(tgt, store=store, tracker=FakeTracker([]))
        self.assertEqual((found.profile.name, found.provenance.layer), ("fast", 2))


class Chains(unittest.TestCase):
    def project(self):
        return {
            "profiles": {
                "fast": {
                    "runtime": "omp",
                    "models": {"worker": {"chain": ["provider-a/one", "provider-b/two"]}},
                }
            }
        }

    def test_the_resolved_chains_are_rerouted_not_raw(self):
        tgt = target(resolve(project=self.project()))
        assert tgt.resolved is not None
        exhausted = {
            "readings": {
                "provider-a": {
                    "windows": {"hour": {"used_pct": 100, "resets_at": T0 + 3600}},
                    "read_at": T0 - 120,
                },
                "provider-b": {
                    "windows": {"hour": {"used_pct": 5, "resets_at": T0 + 3600}},
                    "read_at": T0 - 120,
                },
            },
            "marks": {},
        }
        found = select(
            tgt,
            flag="fast",
            pool=pool("provider-a", "provider-b"),
            ledger=exhausted,
        )
        self.assertEqual(found.chains, {"worker": ["provider-b/two"]})

    def test_a_fully_exhausted_chain_refuses_unchanged(self):
        tgt = target(resolve(project=self.project()))
        assert tgt.resolved is not None
        exhausted = {
            "readings": {
                "provider-a": {
                    "windows": {"hour": {"used_pct": 100, "resets_at": T0 + 3600}},
                    "read_at": T0 - 120,
                },
                "provider-b": {
                    "windows": {"hour": {"used_pct": 100, "resets_at": T0 + 3600}},
                    "read_at": T0 - 120,
                },
            },
            "marks": {},
        }
        with self.assertRaises(models.Exhausted) as ctx:
            select(tgt, flag="fast", pool=pool("provider-a", "provider-b"), ledger=exhausted)
        self.assertEqual(ctx.exception.exit_code, 75)

    def test_an_unknown_profile_runtime_is_exit_4(self):
        tgt = target(resolve(project={"profiles": {"fast": {"runtime": "nope"}}}))
        with self.assertRaises(settings.SettingsError) as ctx:
            select(tgt, flag="fast")
        self.assertIn("nope", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
