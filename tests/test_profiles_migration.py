"""Agent profiles, ticket 8: migration cutover.

The old `[models.*]` tables and `agents.runtime` are gone: a corral.toml
still carrying them fails fast with SettingsError (exit 4) naming the new
`[profiles.*]` keys. Every launch resolves one profile; the pre-profiles
path is gone, so a config with no declarations runs the built-in default.
"""

import os
import tempfile
import unittest

from corral import accounts, launch, profiles, projects, settings
from corral import drive as drive_mod


def resolve(project=None, host=None, repo=None, env=None, flags=None):
    return settings.resolve(
        project=project or {},
        host=host or {},
        repo=repo,
        env={} if env is None else env,
        flags=flags,
    )


def pool(*names):
    return [accounts.Provider(name=name, reader="pkg:Reader") for name in names]


Profile = profiles.Profile


class OldKeysFail(unittest.TestCase):
    def test_a_project_models_table_names_profiles(self):
        with self.assertRaisesRegex(settings.SettingsError, "profiles"):
            resolve(project={"models": {"default": {"chain": ["a/x"]}}})

    def test_a_project_agents_runtime_names_the_profile_runtime(self):
        with self.assertRaisesRegex(settings.SettingsError, "profiles"):
            resolve(project={"agents": {"runtime": "muse"}})

    def test_a_host_agents_runtime_names_the_profile_runtime(self):
        with self.assertRaisesRegex(settings.SettingsError, "profiles"):
            resolve(host={"agents": {"runtime": "muse"}})

    def test_a_repo_entry_agents_runtime_names_the_profile_runtime(self):
        project = {"repo": [{"name": "app", "agents": {"runtime": "muse"}}]}
        with self.assertRaisesRegex(settings.SettingsError, "profiles"):
            resolve(project=project, repo="app")

    def test_the_env_agents_runtime_is_rejected(self):
        with self.assertRaisesRegex(settings.SettingsError, "profiles"):
            resolve(env={"CORRAL_AGENTS_RUNTIME": "muse"})

    def test_the_agents_runtime_flag_is_rejected(self):
        with self.assertRaisesRegex(settings.SettingsError, "profiles"):
            resolve(flags={"agents.runtime": "muse"})

    def test_no_models_field_survives_on_the_resolved_settings(self):
        resolved = resolve(project={})
        self.assertFalse(hasattr(resolved, "models"))


class SinglePath(unittest.TestCase):
    def test_an_empty_declaration_still_selects_the_builtin_default(self):
        tgt = FakeTarget(resolved=resolve(project={}))
        found = launch.select_profile(
            tgt,
            7,
            role="worker",
            selections=profiles.empty_selections(),
            store=FakeStore(),
            tracker=None,
            pool=pool("provider-a"),
            ledger={"readings": {}, "marks": {}},
            now=1000.0,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.profile, profiles.BUILTIN_DEFAULT)
        self.assertEqual(found.runtime, profiles.BUILTIN_DEFAULT.runtime)


class DriveQuotaFromProfiles(unittest.TestCase):
    def test_project_quota_reads_the_declared_default_profiles_chains(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-migration-"))
        root = os.path.join(tmp, "ops")
        os.makedirs(root)
        with open(os.path.join(root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                '[profiles.default]\nruntime = "omp"\n'
                '[profiles.default.models.default]\nchain = ["tired/model"]\n'
            )
        project = projects.Project(name="blue", unit_prefix="blue-agent", root=root)
        pool_ = [accounts.Provider(name="tired", reader="pkg:Reader", stale_after_s=60.0)]
        source = drive_mod._project_quota(project, pool_)
        self.assertEqual(source.chains, {"default": ["tired/model"]})
        self.assertEqual(source.stale_after, 60.0)

    def test_project_quota_rejects_the_old_models_table(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-migration-"))
        root = os.path.join(tmp, "ops")
        os.makedirs(root)
        with open(os.path.join(root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[models.default]\nchain = ["tired/model"]\n')
        project = projects.Project(name="blue", unit_prefix="blue-agent", root=root)
        with self.assertRaises(settings.SettingsError):
            drive_mod._project_quota(project, pool("tired"))


class FakeStore:
    def __init__(self, found=None, fresh=True):
        self.found = found
        self.fresh = fresh

    def item(self, repo, number, *, now=None, max_age=None):
        return self.found, self.fresh


class FakeTarget:
    def __init__(self, resolved, repo="example"):
        self.repo = repo
        self.resolved = resolved
