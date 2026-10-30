"""Provider states and the quota gate: pure decisions over the ledger."""

import unittest

from corral import accounts, quota, quota_ledger as ql, settings

T0 = 1_800_000_000.0
STALE = 900.0
LOW = 90


def reading(windows, at=T0):
    return {"windows": windows, "read_at": at}


def win(used, resets_at=T0 + 3600):
    return {"used_pct": used, "resets_at": resets_at}


def mark(until):
    return {"until": until, "reason": "quota", "source": "reader"}


def state(provider, ledger, now=T0, **kw):
    kw.setdefault("stale_after", STALE)
    kw.setdefault("low_pct", LOW)
    readings = ledger.get("readings", {})
    marks = ledger.get("marks", {})
    return quota.provider_state(readings.get(provider), marks.get(provider), now, **kw)


class ProviderState(unittest.TestCase):
    def test_fresh_low_usage_is_ok(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(25.0)}), None, T0, stale_after=STALE, low_pct=LOW
            ),
            "ok",
        )

    def test_a_window_at_the_low_pct_is_low(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(90.0)}), None, T0, stale_after=STALE, low_pct=LOW
            ),
            "low",
        )

    def test_a_window_above_the_low_pct_is_low(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(97.5)}), None, T0, stale_after=STALE, low_pct=LOW
            ),
            "low",
        )

    def test_a_window_at_100_is_exhausted(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(100.0)}), None, T0, stale_after=STALE, low_pct=LOW
            ),
            "exhausted",
        )

    def test_a_full_window_past_its_reset_is_ok_again(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(100.0, resets_at=T0 - 10)}),
                None,
                T0,
                stale_after=STALE,
                low_pct=LOW,
            ),
            "ok",
        )

    def test_a_low_window_past_its_reset_is_ok_again(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(95.0, resets_at=T0 - 10)}),
                None,
                T0,
                stale_after=STALE,
                low_pct=LOW,
            ),
            "ok",
        )

    def test_no_reading_is_unknown(self):
        self.assertEqual(
            quota.provider_state(None, None, T0, stale_after=STALE, low_pct=LOW),
            "unknown",
        )

    def test_a_failed_reading_is_unknown(self):
        self.assertEqual(
            quota.provider_state(
                {"error": "reader timed out", "at": T0}, None, T0, stale_after=STALE, low_pct=LOW
            ),
            "unknown",
        )

    def test_a_stale_reading_is_unknown(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(10.0)}, at=T0 - STALE - 1),
                None,
                T0,
                stale_after=STALE,
                low_pct=LOW,
            ),
            "unknown",
        )

    def test_a_reading_at_the_staleness_bound_is_still_fresh(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(10.0)}, at=T0 - STALE),
                None,
                T0,
                stale_after=STALE,
                low_pct=LOW,
            ),
            "ok",
        )

    def test_an_active_mark_means_exhausted_without_a_reading(self):
        self.assertEqual(
            quota.provider_state(None, mark(T0 + 60), T0, stale_after=STALE, low_pct=LOW),
            "exhausted",
        )

    def test_an_active_mark_wins_over_a_healthy_reading(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(10.0)}), mark(T0 + 3600), T0, stale_after=STALE, low_pct=LOW
            ),
            "exhausted",
        )

    def test_an_active_mark_wins_over_a_stale_reading(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(10.0)}, at=T0 - STALE - 1),
                mark(T0 + 3600),
                T0,
                stale_after=STALE,
                low_pct=LOW,
            ),
            "exhausted",
        )

    def test_an_expired_mark_is_ignored(self):
        self.assertEqual(
            quota.provider_state(
                reading({"session": win(10.0)}), mark(T0 - 1), T0, stale_after=STALE, low_pct=LOW
            ),
            "ok",
        )

    def test_an_expired_mark_without_a_reading_is_unknown(self):
        self.assertEqual(
            quota.provider_state(None, mark(T0 - 1), T0, stale_after=STALE, low_pct=LOW),
            "unknown",
        )


class MalformedWindows(unittest.TestCase):
    def read(self, windows):
        return quota.provider_state(
            {"windows": windows, "read_at": T0}, None, T0, stale_after=STALE, low_pct=LOW
        )

    def test_a_non_dict_window_reads_unknown(self):
        self.assertEqual(self.read({"session": 50}), "unknown")

    def test_a_non_numeric_reset_reads_unknown(self):
        self.assertEqual(self.read({"session": win(50.0, "soon")}), "unknown")

    def test_a_window_without_a_reset_stays_live(self):
        self.assertEqual(self.read({"session": {"used_pct": 100}}), "exhausted")
        self.assertEqual(self.read({"session": win(100.0, None)}), "exhausted")


class RoleState(unittest.TestCase):
    def test_an_empty_chain_is_unknown(self):
        self.assertEqual(quota.role_state([], {}), "unknown")

    def test_a_single_ok_provider_passes(self):
        self.assertEqual(quota.role_state(["a"], {"a": "ok"}), "ok")

    def test_a_single_low_provider_passes_as_low(self):
        self.assertEqual(quota.role_state(["a"], {"a": "low"}), "low")

    def test_one_passing_provider_carries_the_chain(self):
        self.assertEqual(quota.role_state(["a", "b"], {"a": "exhausted", "b": "ok"}), "ok")

    def test_low_carries_a_chain_without_ok(self):
        self.assertEqual(quota.role_state(["a", "b"], {"a": "exhausted", "b": "low"}), "low")

    def test_an_all_exhausted_chain_is_exhausted(self):
        self.assertEqual(
            quota.role_state(["a", "b"], {"a": "exhausted", "b": "exhausted"}),
            "exhausted",
        )

    def test_a_missing_provider_reads_as_unknown(self):
        self.assertEqual(quota.role_state(["a"], {}), "unknown")

    def test_unknown_does_not_block_by_default(self):
        self.assertEqual(
            quota.role_state(["a", "b"], {"a": "exhausted", "b": "unknown"}),
            "unknown",
        )

    def test_unknown_blocks_when_configured(self):
        self.assertEqual(
            quota.role_state(["a", "b"], {"a": "exhausted", "b": "unknown"}, unknown_blocks=True),
            "exhausted",
        )

    def test_an_all_unknown_chain_blocks_only_when_configured(self):
        self.assertEqual(quota.role_state(["a"], {"a": "unknown"}), "unknown")
        self.assertEqual(
            quota.role_state(["a"], {"a": "unknown"}, unknown_blocks=True),
            "exhausted",
        )


class EmptyChains(unittest.TestCase):
    def verdict(self, chains, unknown_blocks):
        return quota.gate(
            ["default"], chains, ql.empty(), T0, stale_after=STALE, unknown_blocks=unknown_blocks
        )

    def test_empty_and_missing_chains_follow_unknown_blocks(self):
        for chains in ({"default": []}, {}):
            self.assertFalse(self.verdict(chains, False)["shut"])
            self.assertTrue(self.verdict(chains, True)["shut"])

    def test_an_empty_chain_role_state_follows_unknown_blocks(self):
        self.assertEqual(quota.role_state([], {}, unknown_blocks=True), "exhausted")


class Gate(unittest.TestCase):
    def ledger(self, **entries):
        ledger = ql.empty()
        for provider, windows in entries.items():
            ledger = ql.record_reading(ledger, provider, windows, T0)
        return ledger

    def test_a_healthy_chain_is_not_shut(self):
        ledger = self.ledger(**{"provider-a": {"session": win(10.0)}})
        verdict = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x"]},
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertFalse(verdict["shut"])
        self.assertEqual(verdict["blocked"], {})

    def test_an_exhausted_chain_is_shut_and_names_its_role(self):
        ledger = self.ledger(**{"provider-a": {"session": win(100.0)}})
        verdict = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x"]},
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertTrue(verdict["shut"])
        self.assertIn("default", verdict["blocked"])

    def test_a_chain_with_one_passing_provider_is_not_shut(self):
        ledger = self.ledger(
            **{
                "provider-a": {"session": win(100.0)},
                "provider-b": {"session": win(10.0)},
            }
        )
        verdict = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x", "provider-b/model-y"]},
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertFalse(verdict["shut"])

    def test_a_mark_shuts_a_role_without_a_reading(self):
        ledger, _ = ql.post_mark(ql.empty(), "provider-a", T0 + 3600, "quota", "reader")
        verdict = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x"]},
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertTrue(verdict["shut"])
        self.assertIn("default", verdict["blocked"])

    def test_unknown_does_not_shut_by_default(self):
        verdict = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x"]},
            ql.empty(),
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertFalse(verdict["shut"])

    def test_unknown_shuts_when_configured(self):
        verdict = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x"]},
            ql.empty(),
            T0,
            stale_after=STALE,
            low_pct=LOW,
            unknown_blocks=True,
        )
        self.assertTrue(verdict["shut"])
        self.assertIn("default", verdict["blocked"])

    def test_two_projects_against_one_ledger(self):
        ledger = self.ledger(
            **{
                "provider-a": {"session": win(100.0)},
                "provider-b": {"session": win(10.0)},
            }
        )
        first = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x"]},
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        second = quota.gate(
            ["default"],
            {"default": ["provider-b/model-y"]},
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertTrue(first["shut"])
        self.assertFalse(second["shut"])

    def test_only_required_roles_can_shut(self):
        ledger = self.ledger(**{"provider-a": {"session": win(100.0)}})
        verdict = quota.gate(
            ["plan"],
            {
                "default": ["provider-a/model-x"],
                "plan": ["provider-b/model-y"],
            },
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertFalse(verdict["shut"])


class GateVerdict(unittest.TestCase):
    def test_the_verdict_carries_provider_and_role_states(self):
        ledger = ql.record_reading(ql.empty(), "provider-a", {"session": win(100.0)}, T0)
        verdict = quota.gate(
            ["default"],
            {"default": ["provider-a/model-x"]},
            ledger,
            T0,
            stale_after=STALE,
            low_pct=LOW,
        )
        self.assertEqual(verdict["states"], {"provider-a": "exhausted"})
        self.assertEqual(verdict["roles"], {"default": "exhausted"})
        self.assertEqual(verdict["blocked"]["default"]["providers"], {"provider-a": "exhausted"})


def provider(name, stale_after_s):
    return accounts.Provider(name=name, reader="pkg:Reader", stale_after_s=stale_after_s)


class StaleAfterForTest(unittest.TestCase):
    def test_minimum_over_the_chained_pool_providers(self):
        pool = [provider("a", 3600.0), provider("tired", 60.0)]
        self.assertEqual(quota.stale_after_for({"worker": ["tired/model", "a/model"]}, pool), 60.0)

    def test_unchained_pool_providers_do_not_tighten_the_bound(self):
        pool = [provider("a", 3600.0), provider("other", 30.0)]
        self.assertEqual(quota.stale_after_for({"worker": ["a/model"]}, pool), 3600.0)

    def test_missing_pool_falls_back_to_the_default(self):
        self.assertEqual(
            quota.stale_after_for({"worker": ["a/model"]}, []),
            accounts.DEFAULT_STALE_AFTER_S,
        )
        self.assertEqual(
            quota.stale_after_for({"worker": ["a/model"]}, None),
            accounts.DEFAULT_STALE_AFTER_S,
        )

    def test_a_provider_without_a_numeric_bound_is_ignored(self):
        pool = [provider("a", "soon")]
        self.assertEqual(
            quota.stale_after_for({"worker": ["a/model"]}, pool),
            accounts.DEFAULT_STALE_AFTER_S,
        )

    def test_empty_chains_use_the_default(self):
        pool = [provider("a", 60.0)]
        self.assertEqual(quota.stale_after_for({}, pool), accounts.DEFAULT_STALE_AFTER_S)


class QuotaSettings(unittest.TestCase):
    def test_low_pct_defaults_to_90(self):
        self.assertEqual(settings.REGISTRY["quota.low_pct"].default, 90)

    def test_unknown_blocks_defaults_to_false(self):
        self.assertEqual(settings.REGISTRY["quota.unknown_blocks"].default, False)

    def test_quota_registers_its_settings(self):
        self.assertIn("corral.quota", settings.PROVIDERS)

    def test_a_host_file_may_set_quota_keys(self):
        resolved = settings.resolve(host={"quota": {"low_pct": 95, "unknown_blocks": True}}, env={})
        self.assertEqual(resolved["quota.low_pct"], 95)
        self.assertEqual(resolved["quota.unknown_blocks"], True)


if __name__ == "__main__":
    unittest.main()
