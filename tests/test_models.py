"""Model declarations and the per-launch overlay: chains, re-route, refusal."""

import os
import unittest

from corral import accounts, launch, launchcli, models, quota, settings
from corral.runtimes import omp, opencode
from tests.test_launch import TOML, OpsCase, roomy

T0 = 1_800_000_000.0
STALE = 900.0


def reading(windows, at=T0):
    return {"windows": windows, "read_at": at}


def win(used, resets_at=T0 + 3600):
    return {"used_pct": used, "resets_at": resets_at}


def mark(until):
    return {"until": until, "reason": "quota", "source": "reader"}


def ledger(**entries):
    readings, marks = {}, {}
    for provider, entry in entries.items():
        kind, payload = entry
        if kind == "reading":
            readings[provider] = payload
        else:
            marks[provider] = payload
    return {"readings": readings, "marks": marks}


def resolve(project, **kw):
    kw.setdefault("env", {})
    return settings.resolve(project=project, **kw)


class ChainsSetting(unittest.TestCase):
    def test_models_chains_resolve_from_the_project_file(self):
        resolved = resolve({"models": {"default": {"chain": ["a/x", "b/y"]}}})
        self.assertEqual(resolved.models, {"default": ["a/x", "b/y"]})

    def test_no_models_table_resolves_to_no_chains(self):
        self.assertEqual(resolve({}).models, {})
        self.assertEqual(resolve({"models": {}}).models, {})

    def test_a_models_table_must_hold_roles_with_chains(self):
        for project in (
            {"models": ["default"]},
            {"models": {"default": ["a/x"]}},
            {"models": {"default": {}}},
            {"models": {"default": {"chain": "a/x"}}},
            {"models": {"default": {"chain": []}}},
            {"models": {"default": {"chain": [1]}}},
            {"models": {"default": {"chain": [""]}}},
            {"models": {"default": {"chain": ["a/x"], "other": 1}}},
            {"models": {"": {"chain": ["a/x"]}}},
        ):
            with self.subTest(project=project), self.assertRaises(settings.SettingsError):
                resolve(project)

    def test_models_are_project_level_not_host_or_repo(self):
        with self.assertRaisesRegex(settings.SettingsError, "unknown setting"):
            resolve({}, host={"models": {"default": {"chain": ["a/x"]}}})
        with self.assertRaisesRegex(settings.SettingsError, "unknown setting"):
            resolve(
                {"repo": [{"name": "app", "models": {"default": {"chain": ["a/x"]}}}]}, repo="app"
            )


class Validate(unittest.TestCase):
    def test_known_providers_pass(self):
        chains = {"default": ["a/x", "b/y"]}
        self.assertIsNone(models.validate(chains, ["a", "b"]))

    def test_an_unknown_provider_is_exit_4_naming_role_and_entry(self):
        with self.assertRaises(models.ModelsError) as ctx:
            models.validate({"default": ["a/x", "c/z"]}, ["a", "b"])
        self.assertIsInstance(ctx.exception, settings.SettingsError)
        self.assertIn("default", str(ctx.exception))
        self.assertIn("c/z", str(ctx.exception))

    def test_a_model_id_starting_with_a_dash_is_refused(self):
        with self.assertRaises(models.ModelsError) as ctx:
            models.validate({"default": ["-a/x"]}, ["a"])
        self.assertIn("default", str(ctx.exception))
        self.assertIn("-a/x", str(ctx.exception))

    def test_a_non_string_entry_is_refused(self):
        with self.assertRaises(models.ModelsError):
            models.validate({"default": [None]}, ["a"])

    def test_an_empty_pool_refuses_every_entry(self):
        with self.assertRaises(models.ModelsError):
            models.validate({"default": ["a/x"]}, [])


class Reroute(unittest.TestCase):
    def test_only_exhausted_providers_leave_the_chain(self):
        seen = ledger(
            a=("reading", reading({"w": win(10)})),
            b=("reading", reading({"w": win(100)})),
        )
        self.assertEqual(
            models.reroute({"default": ["a/x", "b/y", "a/z"]}, seen, T0, stale_after=STALE),
            {"default": ["a/x", "a/z"]},
        )

    def test_marks_low_and_unknown_providers_stay_by_default(self):
        seen = ledger(
            a=("reading", reading({"w": win(95)})),
            b=("mark", mark(T0 + 60)),
            c=("reading", reading({"w": win(10, resets_at=None)})),
        )
        filtered = models.reroute(
            {"default": ["a/x", "b/y", "unknown/q"]}, seen, T0, stale_after=STALE
        )
        self.assertEqual(filtered, {"default": ["a/x", "unknown/q"]})

    def test_unknown_providers_leave_when_unknown_blocks(self):
        seen = ledger()
        self.assertEqual(
            models.reroute(
                {"default": ["unknown/q"]}, seen, T0, stale_after=STALE, unknown_blocks=True
            ),
            {"default": []},
        )

    def test_empty_chains_pass_through(self):
        self.assertEqual(models.reroute({}, ledger(), T0, stale_after=STALE), {})
        self.assertEqual(
            models.reroute({"default": []}, ledger(), T0, stale_after=STALE),
            {"default": []},
        )


class Refusal(unittest.TestCase):
    def test_a_live_role_returns_its_filtered_chain(self):
        seen = ledger(
            a=("reading", reading({"w": win(100)})), b=("reading", reading({"w": win(1)}))
        )
        self.assertEqual(
            models.check({"default": ["a/x", "b/y"]}, seen, T0, stale_after=STALE),
            {"default": ["b/y"]},
        )

    def test_a_fully_exhausted_role_refuses_naming_role_and_reset(self):
        seen = ledger(
            a=("reading", reading({"w": win(100, resets_at=T0 + 300)})),
            b=("reading", reading({"w": win(100, resets_at=T0 + 100)})),
        )
        with self.assertRaises(models.Exhausted) as ctx:
            models.check({"default": ["a/x", "b/y"]}, seen, T0, stale_after=STALE)
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertIn("default", str(ctx.exception))
        self.assertIn(str(T0 + 100), str(ctx.exception))

    def test_a_mark_until_is_the_reset_when_no_window_reset_is_known(self):
        seen = ledger(b=("mark", mark(T0 + 60)))
        with self.assertRaises(models.Exhausted) as ctx:
            models.check({"default": ["b/y"]}, seen, T0, stale_after=STALE)

    def test_only_required_roles_can_refuse(self):
        seen = ledger(
            a=("reading", reading({"w": win(10)})),
            b=("reading", reading({"w": win(100)})),
        )
        filtered = models.check(
            {"default": ["a/x"], "other": ["b/z"]},
            seen,
            T0,
            roles=["default"],
            stale_after=STALE,
        )
        self.assertEqual(filtered, {"default": ["a/x"], "other": []})

    def test_an_unknown_role_passes_unless_unknown_blocks(self):
        chains = {"default": ["missing/q"]}
        self.assertEqual(models.check(chains, ledger(), T0, stale_after=STALE), chains)
        with self.assertRaises(models.Exhausted):
            models.check(chains, ledger(), T0, stale_after=STALE, unknown_blocks=True)


class OmpModels(unittest.TestCase):
    def test_heads_become_roles_and_tails_become_fallbacks(self):
        config = omp.with_models({}, {"default": ["a/x", "b/y"], "judge": ["a/x"]})
        self.assertEqual(
            config,
            {
                "modelRoles": {"default": "a/x", "judge": "a/x"},
                "retry": {"fallbackChains": {"default": ["b/y"]}},
            },
        )

    def test_model_keys_win_and_other_keys_survive(self):
        base = {
            "modelRoles": {"default": "old/x", "extra": "keep/y"},
            "retry": {"limit": 2, "fallbackChains": {"default": ["old/y"]}},
            "task": {"agentModelOverrides": {"reviewer": "a/x"}},
        }
        config = omp.with_models(base, {"default": ["a/x"]})
        self.assertEqual(config["modelRoles"], {"default": "a/x", "extra": "keep/y"})
        self.assertEqual(config["retry"], {"limit": 2, "fallbackChains": {}})
        self.assertEqual(config["task"], {"agentModelOverrides": {"reviewer": "a/x"}})

    def test_an_empty_chain_sets_no_role(self):
        self.assertEqual(omp.with_models({}, {"default": []}), {})


class OpencodeModels(unittest.TestCase):
    def test_the_default_head_becomes_the_model(self):
        config = opencode.with_models({"share": "never"}, {"default": ["a/x", "b/y"]})
        self.assertEqual(config, {"share": "never", "model": "a/x"})

    def test_without_a_default_role_or_entry_there_is_no_model(self):
        for chains in ({"other": ["a/x"]}, {"default": []}):
            with self.subTest(chains=chains), self.assertRaises(models.ModelsError):
                opencode.with_models({}, chains)


class ChainProvider(unittest.TestCase):
    def test_the_provider_is_the_part_before_the_slash(self):
        self.assertEqual(quota.chain_provider("provider-a/model-x"), "provider-a")


class BareId(unittest.TestCase):
    def test_the_bare_id_is_the_part_after_the_first_slash(self):
        self.assertEqual(models.bare_id("muse/spark-1"), "spark-1")

    def test_a_bare_id_with_no_slash_passes_through(self):
        self.assertEqual(models.bare_id("spark-1"), "spark-1")

    def test_only_the_first_slash_strips(self):
        self.assertEqual(models.bare_id("muse/a/b"), "a/b")


def pool(*names):
    return [accounts.Provider(name=name, reader="fake:Reader") for name in names]


class LaunchModels(OpsCase):
    """Launches validate, re-route and refuse on the project's chains."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, settings.PROJECT_FILE), "w", encoding="utf-8") as handle:
            handle.write(TOML + '[models.default]\nchain = ["a/x", "b/y"]\n')

    def start(self, seen, number=5, pool_names=("a", "b"), runtime="omp"):
        return launch.launch(
            self.target("lib", runtime),
            number,
            "do the thing",
            title="feat: add the thing",
            list_fn=self.system.list_fn,
            run=self.system.run,
            usage=roomy,
            pool_fn=lambda: pool(*pool_names),
            ledger_fn=lambda: seen,
            now_fn=lambda: T0,
        )

    def overlay(self, repo="lib", number=5):
        with open(os.path.join(self.leaf(repo, number), "overlay.yml"), encoding="utf-8") as handle:
            return handle.read()

    def test_a_launch_routes_around_an_exhausted_provider(self):
        seen = ledger(
            a=("reading", reading({"w": win(100)})), b=("reading", reading({"w": win(1)}))
        )
        self.start(seen)
        text = self.overlay()
        self.assertIn('default: "b/y"\n', text)
        self.assertNotIn("a/x", text)

    def test_a_fully_exhausted_chain_refuses_before_anything_is_made(self):
        seen = ledger(
            a=("reading", reading({"w": win(100, resets_at=T0 + 300)})),
            b=("reading", reading({"w": win(100, resets_at=T0 + 100)})),
        )
        with self.assertRaises(models.Exhausted) as ctx:
            self.start(seen)
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertIn("default", str(ctx.exception))
        self.assertIn(str(T0 + 100), str(ctx.exception))
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))
        self.assertEqual(self.worktrees_of("lib"), [])

    def test_an_unknown_provider_in_a_chain_is_exit_4_before_anything_is_made(self):
        with self.assertRaises(models.ModelsError) as ctx:
            self.start(ledger(), pool_names=("a",))
        self.assertIsInstance(ctx.exception, settings.SettingsError)
        self.assertIn("b/y", str(ctx.exception))
        self.assertEqual(self.system.started, [])

    def test_exhaustion_is_a_launch_verb_error_with_exit_75(self):
        self.assertIn(models.Exhausted, launchcli.KNOWN)
        self.assertEqual(launchcli._exit_code(models.Exhausted("spent")), 75)


if __name__ == "__main__":
    unittest.main()
