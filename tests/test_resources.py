"""Host resource budgets: the pure admission decision over CPU, memory and pids.

Plain data in, a verdict with a reason string out, shaped like
``admission.admit``: no subprocess, no clock reads, no state dir. Every
refusal names its level (host, project or role cost) with the numbers, and
every refusal maps to exit 75 at the launch seam. Configuration mistakes
raise ``LimitsError`` (exit 4).
"""

import unittest
from typing import Any
from unittest import mock

from korapy import prompts
from korapy import resources as R

NOW = 1_000_000.0

WORKER = {"cpu_m": 1000, "memory_mb": 2048, "pids": 100}
REVIEWER = {"cpu_m": 500, "memory_mb": 1024, "pids": 50}


def triple(cpu_m=0, memory_mb=0, pids=0):
    return {"cpu_m": cpu_m, "memory_mb": memory_mb, "pids": pids}


def req(project="web", role="worker"):
    return R.Request(project=project, role=role)


def limits(
    host=(8000, 16000, 4096), projects=None, roles=None, foreign=(), gone_s=600.0, pressure=None
):
    if host is not None:
        host = triple(*host)
    if roles is None:
        roles = {"worker": dict(WORKER), "reviewer": dict(REVIEWER)}
    kwargs = {}
    if pressure is not None:
        kwargs["pressure"] = pressure
    return R.Limits(
        host=host,
        projects=dict(projects or {}),
        roles=dict(roles),
        foreign=list(foreign),
        gone_s=gone_s,
        **kwargs,
    )


def charged(total=(0, 0, 0), by_project=None):
    return R.Charged(
        total=triple(*total),
        by_project={name: triple(*used) for name, used in (by_project or {}).items()},
    )


def claim(name="desktop", cpu_m=0, memory_mb=0, pids=0, at=NOW):
    return {"name": name, "cpu_m": cpu_m, "memory_mb": memory_mb, "pids": pids, "claimed_at": at}


class UnsetHost(unittest.TestCase):
    def test_no_host_budget_passes_everything(self):
        verdict = R.admit(req(), charged((8000, 16000, 4096)), limits(host=None), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))
        self.assertIn("no host budget", verdict.reason)

    def test_no_host_budget_warns_instead_of_enforcing(self):
        verdict = R.admit(req("web", "worker"), charged(), limits(host=None), NOW)
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.reason)


class EmptyHost(unittest.TestCase):
    def test_an_empty_host_allows_a_start(self):
        verdict = R.admit(req(), charged(), limits(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))
        self.assertTrue(verdict.reason)

    def test_an_unknown_project_reads_floor_zero_and_no_ceiling(self):
        verdict = R.admit(req("newcomer"), charged(), limits(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_an_unknown_project_gets_a_default_weight_share(self):
        host = limits(host=(1000, 16000, 4096))
        verdict = R.admit(req("newcomer"), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))
        self.assertIn("1000", verdict.reason)

    def test_an_unknown_role_is_a_configuration_error(self):
        with self.assertRaises(R.LimitsError) as caught:
            R.admit(req("web", "auditor"), charged(), limits(), NOW)
        self.assertIn("auditor", str(caught.exception))
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_the_verdict_carries_the_numbers_it_was_decided_from(self):
        verdict = R.admit(
            req(),
            charged(total=(1000, 2048, 100), by_project={"web": (1000, 2048, 100)}),
            limits(),
            NOW,
        )
        self.assertEqual(verdict.charged, R.Usage(1000, 2048, 100))
        self.assertEqual(verdict.project, R.Usage(1000, 2048, 100))
        self.assertEqual(verdict.cost, R.Usage(1000, 2048, 100))


class RoleCosts(unittest.TestCase):
    def test_a_cheaper_role_fits_where_the_worker_does_not(self):
        host = limits(host=(2000, 16000, 4096))
        load = charged(total=(1500, 0, 0), by_project={"web": (1500, 0, 0)})
        refused = R.admit(req("web", "worker"), load, host, NOW)
        self.assertEqual((refused.allowed, refused.code), (False, "project-share"))
        self.assertIn("2000", refused.reason)
        allowed = R.admit(req("web", "reviewer"), load, host, NOW)
        self.assertEqual((allowed.allowed, allowed.code), (True, "ok"))

    def test_a_charge_exactly_at_the_share_is_allowed(self):
        host = limits(host=(2000, 16000, 4096))
        load = charged(total=(1000, 0, 0), by_project={"web": (1000, 0, 0)})
        verdict = R.admit(req("web", "reviewer"), load, host, NOW)
        self.assertTrue(verdict.allowed)
        self.assertIn("2000", verdict.reason)

    def test_a_charge_bigger_than_the_pool_is_a_role_cost_refusal(self):
        host = limits(host=(800, 16000, 4096))
        verdict = R.admit(req("web", "worker"), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "role-cost"))
        self.assertIn("1000", verdict.reason)
        self.assertIn("800", verdict.reason)

    def test_a_role_cost_refusal_names_the_memory_dimension(self):
        host = limits(host=(64000, 1000, 4096))
        verdict = R.admit(req("web", "worker"), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "role-cost"))
        self.assertIn("memory_mb", verdict.reason)


class Floors(unittest.TestCase):
    def host(self):
        return limits(
            projects={"a": {"weight": 1, "cpu_floor_m": 4000}, "b": {"weight": 1}},
        )

    def test_below_its_floor_a_project_starts_past_a_full_pool(self):
        load = charged(total=(8000, 0, 0), by_project={"b": (8000, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))
        self.assertIn("4000", verdict.reason)

    def test_at_its_floor_a_project_waits_for_a_full_pool(self):
        load = charged(total=(8000, 0, 0), by_project={"a": (4000, 0, 0), "b": (4000, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "host-full"))
        self.assertIn("8000", verdict.reason)
        self.assertIn("4000", verdict.reason)

    def test_a_project_without_a_floor_waits_for_a_full_pool(self):
        load = charged(total=(8000, 0, 0), by_project={"a": (8000, 0, 0)})
        verdict = R.admit(req("b"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "host-full"))

    def test_a_zero_weight_keeps_only_its_floor(self):
        floors = {"weight": 0, "cpu_floor_m": 500, "memory_floor_mb": 1024, "pids_floor": 50}
        host = limits(projects={"a": floors, "b": {"weight": 1}})
        allowed = R.admit(req("a", "reviewer"), charged(), host, NOW)
        self.assertEqual((allowed.allowed, allowed.code), (True, "ok"))
        refused = R.admit(req("a", "worker"), charged(), host, NOW)
        self.assertEqual((refused.allowed, refused.code), (False, "project-share"))
        self.assertIn("500", refused.reason)


class Ceilings(unittest.TestCase):
    def host(self):
        return limits(projects={"a": {"weight": 1, "cpu_ceiling_m": 1000}})

    def test_a_project_at_its_ceiling_is_refused(self):
        load = charged(total=(1000, 0, 0), by_project={"a": (1000, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "project-ceiling"))
        self.assertIn("1000", verdict.reason)
        self.assertIn("ceiling", verdict.reason)

    def test_a_charge_exactly_at_the_ceiling_is_allowed(self):
        verdict = R.admit(req("a"), charged(), self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_a_ceiling_caps_the_weight_share(self):
        load = charged(total=(500, 0, 0), by_project={"a": (500, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "project-ceiling"))


class Weights(unittest.TestCase):
    def host(self):
        return limits(projects={"a": {"weight": 3}, "b": {"weight": 1}})

    def test_the_remainder_splits_by_weight(self):
        load = charged(total=(5000, 0, 0), by_project={"a": (5000, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))
        self.assertIn("6000", verdict.reason)

    def test_one_unit_past_the_weight_share_is_refused(self):
        load = charged(total=(5001, 0, 0), by_project={"a": (5001, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "project-share"))
        self.assertIn("6000", verdict.reason)

    def test_the_small_share_gets_only_its_quarter(self):
        load = charged(total=(1000, 0, 0), by_project={"b": (1000, 0, 0)})
        verdict = R.admit(req("b"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))
        over = charged(total=(1001, 0, 0), by_project={"b": (1001, 0, 0)})
        refused = R.admit(req("b"), over, self.host(), NOW)
        self.assertEqual((refused.allowed, refused.code), (False, "project-share"))
        self.assertIn("2000", refused.reason)


class HostFull(unittest.TestCase):
    def host(self):
        return limits(projects={"a": {"weight": 3}, "b": {"weight": 1}})

    def test_neighbors_fill_the_pool_but_not_the_share(self):
        load = charged(total=(8000, 0, 0), by_project={"a": (3000, 0, 0), "b": (5000, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "host-full"))
        self.assertIn("8000", verdict.reason)

    def test_total_plus_cost_exactly_at_available_is_allowed(self):
        load = charged(total=(7000, 0, 0), by_project={"a": (0, 0, 0), "b": (7000, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_one_unit_past_available_is_host_full(self):
        load = charged(total=(7001, 0, 0), by_project={"a": (0, 0, 0), "b": (7001, 0, 0)})
        verdict = R.admit(req("a"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "host-full"))

    def test_a_full_memory_pool_names_memory(self):
        host = limits(
            host=(64000, 3000, 4096),
            projects={"a": {"weight": 3}, "b": {"weight": 1}},
        )
        load = charged(total=(0, 3000, 0), by_project={"b": (0, 3000, 0)})
        verdict = R.admit(req("a"), load, host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "host-full"))
        self.assertIn("memory_mb", verdict.reason)
        self.assertIn("3000", verdict.reason)


class FloorBypassDimensions(unittest.TestCase):
    def host(self):
        return limits(
            host=(8000, 4000, 4096),
            projects={
                "a": {"weight": 1, "cpu_floor_m": 1000, "memory_floor_mb": 1000},
                "b": {"weight": 1},
            },
            roles={"small": {"cpu_m": 100, "memory_mb": 100, "pids": 10}},
        )

    def test_below_floor_in_every_full_dimension_starts(self):
        load = charged(
            total=(8000, 3500, 0),
            by_project={"a": (500, 500, 0), "b": (7500, 3000, 0)},
        )
        verdict = R.admit(req("a", "small"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_above_floor_in_a_full_dimension_waits(self):
        load = charged(
            total=(8000, 4000, 0),
            by_project={"a": (2000, 1500, 0), "b": (6000, 2500, 0)},
        )
        verdict = R.admit(req("a", "small"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "host-full"))

    def test_below_floor_in_the_only_full_dimension_starts(self):
        load = charged(
            total=(8000, 3500, 0),
            by_project={"a": (500, 1500, 0), "b": (7500, 2000, 0)},
        )
        verdict = R.admit(req("a", "small"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_exactly_at_floor_in_a_full_pool_waits(self):
        load = charged(
            total=(8000, 3500, 0),
            by_project={"a": (1000, 500, 0), "b": (7000, 3000, 0)},
        )
        verdict = R.admit(req("a", "small"), load, self.host(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "host-full"))


class ForeignClaims(unittest.TestCase):
    def host(self, foreign):
        projects = {"web": {"weight": 1}, "other": {"weight": 1}}
        return limits(projects=projects, foreign=foreign)

    def test_a_live_claim_subtracts_from_the_pool(self):
        host = self.host([claim(cpu_m=6000, memory_mb=11000)])
        verdict = R.admit(req(), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))
        self.assertIn("1000", verdict.reason)

    def test_one_unit_past_the_claim_remainder_is_refused(self):
        host = self.host([claim(cpu_m=6002, memory_mb=11000)])
        verdict = R.admit(req(), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "project-share"))
        self.assertIn("999", verdict.reason)

    def test_an_expired_claim_frees_the_pool(self):
        host = self.host([claim(cpu_m=8000, memory_mb=16000, at=NOW - 601.0)])
        verdict = R.admit(req(), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_a_claim_exactly_at_the_staleness_bound_still_counts(self):
        host = self.host([claim(cpu_m=8000, memory_mb=16000, at=NOW - 600.0)])
        verdict = R.admit(req(), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "role-cost"))

    def test_a_cost_exactly_at_the_shrunk_pool_is_allowed(self):
        host = limits(
            projects={"web": {"weight": 1}},
            foreign=[claim(cpu_m=7000, memory_mb=11000)],
        )
        verdict = R.admit(req(), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_a_cost_one_unit_past_the_shrunk_pool_is_role_cost(self):
        host = limits(
            projects={"web": {"weight": 1}},
            foreign=[claim(cpu_m=7001, memory_mb=11000)],
        )
        verdict = R.admit(req(), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "role-cost"))

    def test_a_claim_past_the_pool_refuses_as_role_cost(self):
        host = self.host([claim(cpu_m=7500, memory_mb=15000)])
        verdict = R.admit(req(), charged(), host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "role-cost"))
        self.assertIn("7500", verdict.reason)

    def test_missing_claimed_at_reads_as_the_shared_default(self):
        self.assertEqual(R.ForeignClaim(name="desktop").claimed_at, R.DEFAULT_CLAIMED_AT)
        parsed = R._claim({"name": "desktop"}, "foreign claim 9")
        self.assertEqual(parsed.claimed_at, R.DEFAULT_CLAIMED_AT)


class PlainData(unittest.TestCase):
    def test_plain_mappings_admit_like_constructors(self):
        verdict = R.admit(
            {"project": "web", "role": "worker"},
            {"total": triple(1000, 2048, 100), "by_project": {"web": triple(1000, 2048, 100)}},
            {
                "host": triple(8000, 16000, 4096),
                "projects": {"web": {"weight": 1}},
                "roles": {"worker": dict(WORKER)},
                "foreign": [],
                "gone_s": 600.0,
            },
            NOW,
        )
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_constructors_admit_like_plain_mappings(self):
        host = R.Limits(
            host=R.Usage(8000, 16000, 4096),
            projects={"web": R.Share(weight=1)},
            roles={"worker": R.Usage(**WORKER)},
            foreign=[R.ForeignClaim(name="desktop", cpu_m=100, claimed_at=NOW)],
            gone_s=600.0,
        )
        load = R.Charged(total=R.Usage(), by_project={})
        verdict = R.admit(R.Request(project="web", role="worker"), load, host, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))


class ValidateLimits(unittest.TestCase):
    def check(self, make, *fragments):
        with self.assertRaises(R.LimitsError) as caught:
            R.validate_limits(make())
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_valid_limits_pass(self):
        R.validate_limits(limits())
        R.validate_limits(limits(host=None))
        R.validate_limits(
            limits(
                host=(1000, 2000, 100),
                projects={"a": {"weight": 0, "cpu_floor_m": 500}},
            )
        )

    def test_a_zero_weight_with_a_floor_is_allowed(self):
        R.validate_limits(limits(projects={"a": {"weight": 0, "cpu_floor_m": 500}}))

    def test_bad_numbers_name_themselves(self):
        self.check(lambda: limits(host=(-1, 16000, 4096)), "-1", "cpu_m")
        self.check(lambda: limits(projects={"a": {"cpu_floor_m": -1}}), "-1", "cpu_floor_m")
        self.check(lambda: limits(projects={"a": {"cpu_ceiling_m": -1}}), "-1", "cpu_ceiling_m")
        self.check(lambda: limits(projects={"a": {"weight": -1}}), "-1", "weight")
        self.check(lambda: limits(roles={"worker": {"cpu_m": -1}}), "-1", "cpu_m")
        self.check(lambda: limits(foreign=[claim(cpu_m=-1)]), "-1", "cpu_m")
        self.check(lambda: limits(gone_s=-1.0), "-1", "gone_s")

    def test_a_floor_above_its_ceiling_names_both(self):
        self.check(
            lambda: limits(projects={"a": {"cpu_floor_m": 2000, "cpu_ceiling_m": 1000}}),
            "2000",
            "1000",
        )

    def test_floors_summing_above_the_host_name_the_sum(self):
        self.check(
            lambda: limits(projects={"a": {"cpu_floor_m": 5000}, "b": {"cpu_floor_m": 4000}}),
            "9000",
            "8000",
        )

    def test_a_malformed_claim_names_itself(self):
        self.check(lambda: limits(foreign=[{"name": "", "claimed_at": NOW}]), "name")
        self.check(lambda: limits(foreign=[{"name": "x", "claimed_at": "soon"}]), "claimed_at")

    def test_the_error_is_a_configuration_error(self):
        self.assertEqual(R.LimitsError.exit_code, 4)


class NonMappingTables(unittest.TestCase):
    MISSING = object()

    def table(self, projects=MISSING, roles=MISSING, foreign=MISSING):
        if projects is self.MISSING:
            projects = {"web": {"weight": 1}}
        if roles is self.MISSING:
            roles = {"worker": dict(WORKER)}
        if foreign is self.MISSING:
            foreign = []
        return {
            "host": triple(8000, 16000, 4096),
            "projects": projects,
            "roles": roles,
            "foreign": foreign,
            "gone_s": 600.0,
        }

    def test_non_mapping_projects_are_a_limits_error(self):
        for bad in ([], ["web"], "web", 5, None):
            with self.subTest(projects=bad):
                with self.assertRaises(R.LimitsError):
                    R.validate_limits(self.table(projects=bad))
                with self.assertRaises(R.LimitsError):
                    R.admit(req(), charged(), self.table(projects=bad), NOW)
                with self.assertRaises(R.LimitsError):
                    R.share_cap(self.table(projects=bad), "web", NOW)
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_non_mapping_roles_are_a_limits_error(self):
        for bad in ([], ["worker"], "worker", 5, None):
            with self.subTest(roles=bad):
                with self.assertRaises(R.LimitsError):
                    R.validate_limits(self.table(roles=bad))
                with self.assertRaises(R.LimitsError):
                    R.admit(req(), charged(), self.table(roles=bad), NOW)
                with self.assertRaises(R.LimitsError):
                    R.share_cap(self.table(roles=bad), "web", NOW)
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_direct_limits_with_list_tables_refuse_at_construction(self):
        host = triple(8000, 16000, 4096)
        good_projects: Any = {"web": {"weight": 1}}
        good_roles: Any = {"worker": dict(WORKER)}
        bad_projects: tuple[Any, ...] = ([], ["web"])
        bad_roles: tuple[Any, ...] = ([], ["worker"])
        for bad in bad_projects:
            with self.subTest(projects=bad):
                with self.assertRaises(R.LimitsError):
                    R.Limits(host=host, projects=bad, roles=dict(good_roles))
        for bad in bad_roles:
            with self.subTest(roles=bad):
                with self.assertRaises(R.LimitsError):
                    R.Limits(host=host, projects=dict(good_projects), roles=bad)
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_non_mapping_foreign_is_a_limits_error(self):
        bad_values: tuple[Any, ...] = (5, "x", [1], {"name": "desk"})
        for bad in bad_values:
            with self.subTest(foreign=bad):
                with self.assertRaises(R.LimitsError):
                    R.validate_limits(self.table(foreign=bad))
                with self.assertRaises(R.LimitsError):
                    R.admit(req(), charged(), self.table(foreign=bad), NOW)
                with self.assertRaises(R.LimitsError):
                    R.share_cap(self.table(foreign=bad), "web", NOW)
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_direct_limits_with_bad_foreign_refuses_at_construction(self):
        host = triple(8000, 16000, 4096)
        bad_values: tuple[Any, ...] = (5, "x", [1], {"name": "desk"})
        for bad in bad_values:
            with self.subTest(foreign=bad):
                with self.assertRaises(R.LimitsError):
                    R.Limits(
                        host=host,
                        projects={"web": {"weight": 1}},
                        roles={"worker": dict(WORKER)},
                        foreign=bad,
                    )
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_none_foreign_reads_as_empty(self):
        none: Any = None
        made = R.Limits(
            host=triple(8000, 16000, 4096),
            projects={"web": {"weight": 1}},
            roles={"worker": dict(WORKER)},
            foreign=none,
        )
        self.assertEqual(made.foreign, [])
        R.validate_limits(made)
        R.validate_limits(self.table(foreign=None))

    def test_tuple_and_claim_entries_are_accepted(self):
        entry = {"name": "desk", "cpu_m": 100, "claimed_at": NOW}
        tupled: Any = (dict(entry),)
        made = R.Limits(
            host=triple(8000, 16000, 4096),
            projects={"web": {"weight": 1}},
            roles={"worker": dict(WORKER)},
            foreign=tupled,
        )
        self.assertEqual(made.foreign, [entry])
        R.validate_limits(made)
        R.validate_limits(
            self.table(foreign=[R.ForeignClaim(name="desk", cpu_m=100, claimed_at=NOW)])
        )


class Join(unittest.TestCase):
    def base(self, host=(8000, 16000, 4096)):
        return R.Limits(
            host=R.Usage(*host) if host is not None else None,
            roles={"worker": R.Usage(**WORKER)},
            gone_s=600.0,
        )

    def test_shares_and_claims_join_into_one_limits(self):
        joined = R.join(
            self.base(),
            {"web": {"weight": 1, "cpu_floor_m": 500}},
            [{"name": "desk", "cpu_m": 100, "claimed_at": NOW}],
            where="config.toml",
        )
        self.assertEqual(joined.host, R.Usage(8000, 16000, 4096))
        self.assertEqual(joined.projects["web"].cpu_floor_m, 500)
        self.assertEqual(len(joined.foreign), 1)
        R.validate_limits(joined)

    def test_a_missing_share_reads_as_floor_zero(self):
        joined = R.join(self.base(), {"web": None}, [], where="config.toml")
        self.assertEqual(joined.projects["web"], R.Share())

    def test_floors_at_the_host_pass(self):
        R.join(self.base(), {"a": {"cpu_floor_m": 8000}}, [], where="config.toml")

    def test_floors_above_the_host_name_the_file_and_key(self):
        with self.assertRaisesRegex(R.settings.SettingsError, r"config\.toml.*cpu_m") as caught:
            R.join(
                self.base(),
                {"a": {"cpu_floor_m": 5000}, "b": {"cpu_floor_m": 4000}},
                [],
                where="config.toml",
            )
        message = str(caught.exception)
        self.assertIn("9000", message)
        self.assertIn("8000", message)

    def test_no_host_budget_skips_the_floor_sum(self):
        joined = R.join(self.base(host=None), {"a": {"cpu_floor_m": 5000}}, [], where="c")
        self.assertIsNone(joined.host)

    def test_a_floor_above_its_ceiling_is_still_a_limits_error(self):
        with self.assertRaises(R.LimitsError):
            R.join(
                self.base(),
                {"a": {"cpu_floor_m": 2000, "cpu_ceiling_m": 1000}},
                [],
                where="config.toml",
            )

    def test_non_list_foreign_is_a_limits_error(self):
        bad_values: tuple[Any, ...] = (5, "x", [1])
        for bad in bad_values:
            with self.subTest(foreign=bad):
                with self.assertRaises(R.LimitsError):
                    R.join(self.base(), {"web": {"weight": 1}}, bad, where="config.toml")
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_an_admit_on_joined_limits_enforces_the_share(self):
        joined = R.join(
            self.base(host=(2000, 16000, 4096)),
            {"web": {"weight": 1}},
            [],
            where="config.toml",
        )
        load = charged(total=(1500, 0, 0), by_project={"web": (1500, 0, 0)})
        verdict = R.admit(req("web", "worker"), load, joined, NOW)
        self.assertEqual((verdict.allowed, verdict.code), (False, "project-share"))


class ShareCap(unittest.TestCase):
    def test_no_host_budget_reads_as_no_share(self):
        self.assertIsNone(R.share_cap(limits(host=None), "web", NOW))

    def test_an_empty_host_splits_nothing_yet(self):
        cap = R.share_cap(limits(), "web", NOW)
        assert cap is not None
        self.assertEqual((cap.cpu_m, cap.memory_mb, cap.pids), (8000, 16000, 4096))

    def test_the_remainder_splits_by_weight(self):
        host = limits(projects={"a": {"weight": 3}, "b": {"weight": 1}})
        cap = R.share_cap(host, "a", NOW)
        assert cap is not None
        self.assertEqual(cap.cpu_m, 6000)

    def test_a_floor_is_kept_and_a_ceiling_caps(self):
        host = limits(projects={"a": {"weight": 1, "cpu_floor_m": 500, "cpu_ceiling_m": 1000}})
        cap = R.share_cap(host, "a", NOW)
        assert cap is not None
        self.assertEqual(cap.cpu_m, 1000)

    def test_a_live_foreign_claim_shrinks_the_share(self):
        host = limits(
            projects={"a": {"weight": 1}},
            foreign=[claim(cpu_m=6000, memory_mb=11000)],
        )
        cap = R.share_cap(host, "a", NOW)
        assert cap is not None
        self.assertEqual(cap.cpu_m, 2000)

    def test_an_unknown_project_gets_the_default_weight(self):
        host = limits(host=(1000, 16000, 4096), projects={"a": {"weight": 1}})
        cap = R.share_cap(host, "newcomer", NOW)
        assert cap is not None
        self.assertEqual(cap.cpu_m, 500)

    def test_a_negative_gone_s_is_a_configuration_error(self):
        bad = limits()
        with self.assertRaises(R.LimitsError):
            R.share_cap(R.Limits(host=bad.host, gone_s=-1.0), "web", NOW)


def zeroed(**overrides):
    """The resolver output for a project that reserves nothing: no weight, no floor."""
    share = {
        "weight": 0,
        "cpu_floor_m": 0,
        "memory_floor_mb": 0,
        "pids_floor": 0,
        "cpu_ceiling_m": None,
        "memory_ceiling_mb": None,
        "pids_ceiling": None,
    }
    share.update(overrides)
    return share


class ZeroShareSplit(unittest.TestCase):
    """The split with one zeroed project: the other keeps the whole budget.

    A ceiling-0 project takes no weighted share and no floor: the
    derivation divides the host budget among the projects that can
    start agents, and the zeroed project's own cap is zero.
    """

    def table(self, projects):
        return limits(host=(3000, 8000, 512), projects=projects)

    def test_a_two_project_split_gives_the_other_the_whole_budget(self):
        host = self.table({"web": {"weight": 1}, "shadow": zeroed()})
        cap = R.share_cap(host, "web", NOW)
        assert cap is not None
        self.assertEqual((cap.cpu_m, cap.memory_mb, cap.pids), (3000, 8000, 512))

    def test_the_zeroed_project_caps_at_zero(self):
        host = self.table({"web": {"weight": 1}, "shadow": zeroed()})
        cap = R.share_cap(host, "shadow", NOW)
        assert cap is not None
        self.assertEqual((cap.cpu_m, cap.memory_mb, cap.pids), (0, 0, 0))

    def test_a_floored_split_still_gives_the_other_the_whole_budget(self):
        host = self.table(
            {
                "web": {"weight": 1, "cpu_floor_m": 500, "memory_floor_mb": 1024},
                "shadow": zeroed(),
            }
        )
        joined = R.join(
            host, {name: host.projects[name] for name in host.projects}, (), where="here"
        )
        cap = R.share_cap(joined, "web", NOW)
        assert cap is not None
        self.assertEqual((cap.cpu_m, cap.memory_mb, cap.pids), (3000, 8000, 512))

    def test_the_other_project_may_fill_the_whole_budget(self):
        host = self.table({"web": {"weight": 1}, "shadow": zeroed()})
        verdict = R.admit(
            req("web"), charged((2000, 5000, 100), {"web": (2000, 5000, 100)}), host, NOW
        )
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_a_launch_under_a_zero_share_is_refused(self):
        host = self.table({"web": {"weight": 1}, "shadow": zeroed()})
        verdict = R.admit(req("shadow"), charged(), host, NOW)
        self.assertFalse(verdict.allowed)


class Codes(unittest.TestCase):
    def test_codes_are_stable_strings(self):
        self.assertEqual(
            (R.OK, R.HOST_FULL, R.PROJECT_SHARE, R.PROJECT_CEILING, R.ROLE_COST),
            ("ok", "host-full", "project-share", "project-ceiling", "role-cost"),
        )
        self.assertEqual(R.PRESSURE, "pressure")


def hot(load_per_cpu=8.0, mem_available_pct=50.0):
    return {"load_per_cpu": load_per_cpu, "mem_available_pct": mem_available_pct}


class Saturated(unittest.TestCase):
    def test_a_saturated_host_refuses_while_shares_are_free(self):
        verdict = R.admit(req(), charged(), limits(), NOW, pressure=hot())
        self.assertEqual((verdict.allowed, verdict.code), (False, "pressure"))
        self.assertIn("load", verdict.reason)

    def test_low_memory_refuses_and_names_memory(self):
        verdict = R.admit(req(), charged(), limits(), NOW, pressure=hot(0.2, 3.0))
        self.assertEqual((verdict.allowed, verdict.code), (False, "pressure"))
        self.assertIn("MemAvailable", verdict.reason)

    def test_an_unknown_reading_never_blocks(self):
        verdict = R.admit(
            req(),
            charged(),
            limits(),
            NOW,
            pressure={"load_per_cpu": None, "mem_available_pct": None},
        )
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_no_reading_is_an_unknown_reading(self):
        verdict = R.admit(req(), charged(), limits(), NOW)
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_saturation_waits_even_below_the_floor(self):
        poor = limits(projects={"web": {"weight": 1, "cpu_floor_m": 7000}})
        verdict = R.admit(req(), charged((8000, 16000, 4096)), poor, NOW, pressure=hot())
        self.assertEqual((verdict.allowed, verdict.code), (False, "pressure"))

    def test_no_host_budget_passes_a_saturated_host(self):
        verdict = R.admit(req(), charged(), limits(host=None), NOW, pressure=hot())
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_lax_thresholds_admit_a_hot_host(self):
        lax = limits()
        lax = R.Limits(
            host=lax.host,
            projects=lax.projects,
            roles=lax.roles,
            foreign=lax.foreign,
            gone_s=lax.gone_s,
            pressure={"max_load_per_cpu": 16.0, "min_mem_available_pct": 1.0},
        )
        verdict = R.admit(req(), charged(), lax, NOW, pressure=hot())
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_a_garbage_reading_is_a_configuration_error(self):
        with self.assertRaises(R.LimitsError) as caught:
            R.admit(req(), charged(), limits(), NOW, pressure={"load_per_cpu": "hot"})
        self.assertIn("pressure", str(caught.exception))


class PressureCaps(unittest.TestCase):
    def test_defaults_are_conservative(self):
        caps = R.caps()
        self.assertEqual((caps.max_load_per_cpu, caps.min_mem_available_pct), (2.0, 10.0))

    def test_limits_without_caps_read_as_defaults(self):
        verdict = R.admit(req(), charged(), limits(), NOW, pressure=hot(1.0, 50.0))
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_a_zero_max_is_a_configuration_error(self):
        with self.assertRaises(R.LimitsError):
            R.validate_limits(limits(pressure={"max_load_per_cpu": 0}))

    def test_a_negative_min_is_a_configuration_error(self):
        with self.assertRaises(R.LimitsError):
            R.validate_limits(limits(pressure={"min_mem_available_pct": -1.0}))

    def test_a_min_above_a_hundred_is_a_configuration_error(self):
        with self.assertRaises(R.LimitsError):
            R.validate_limits(limits(pressure={"min_mem_available_pct": 101.0}))

    def test_a_non_number_is_a_configuration_error(self):
        with self.assertRaises(R.LimitsError):
            R.validate_limits(limits(pressure={"max_load_per_cpu": "lots"}))


LOADAVG = "0.80 0.60 0.45 2/500 12345\n"
MEMINFO = "MemTotal:        8000000 kB\nMemFree:         1000000 kB\nMemAvailable:    4400000 kB\n"


class PressureParse(unittest.TestCase):
    def test_load_per_cpu_and_mem_pct(self):
        reading = R.parse_pressure(LOADAVG, MEMINFO, 2)
        self.assertEqual(reading.load_per_cpu, 0.4)
        self.assertEqual(reading.mem_available_pct, 55.0)
        self.assertFalse(reading.saturated)

    def test_saturation_over_load_or_under_mem(self):
        over = R.parse_pressure("5.00 0.10 0.10 1/100 1\n", MEMINFO, 2)
        self.assertTrue(over.saturated)
        under = R.parse_pressure(LOADAVG, MEMINFO.replace("4400000", "400000"), 2)
        self.assertTrue(under.saturated)

    def test_thresholds_are_explicit(self):
        reading = R.parse_pressure(
            LOADAVG, MEMINFO, 2, max_load_per_cpu=0.1, min_mem_available_pct=99.0
        )
        self.assertTrue(reading.saturated)

    def test_missing_files_read_unknown_never_saturated(self):
        for missing in (
            (None, MEMINFO, 2),
            (LOADAVG, None, 2),
            ("garbage\n", MEMINFO, 2),
            (LOADAVG, "MemTotal: nope\n", 2),
            (LOADAVG, MEMINFO, None),
            (LOADAVG, MEMINFO, 0),
        ):
            with self.subTest(missing=missing):
                reading = R.parse_pressure(*missing)
                self.assertFalse(reading.saturated)
                if missing[0] is None or missing[0] == "garbage\n":
                    self.assertIsNone(reading.load_per_cpu)
                if missing[1] is None or "nope" in (missing[1] or ""):
                    self.assertIsNone(reading.mem_available_pct)
                if not missing[2]:
                    self.assertIsNone(reading.load_per_cpu)

    def test_stall_files_are_informational_only(self):
        reading = R.parse_pressure(
            LOADAVG,
            MEMINFO,
            2,
            cpu_stall="some avg10=1.50 avg60=0.20 avg300=0.05 total=123\n",
            memory_stall=None,
        )
        self.assertEqual(reading.cpu_some_avg10, 1.5)
        self.assertIsNone(reading.memory_some_avg10)
        self.assertFalse(reading.saturated)


class ReadPressure(unittest.TestCase):
    def test_reads_through_the_injected_reader(self):
        files = {
            R.LOADAVG_PATH: LOADAVG,
            R.MEMINFO_PATH: MEMINFO,
            R.PRESSURE_CPU_PATH: "some avg10=0.10 avg60=0.05 avg300=0.01 total=9\n",
            R.PRESSURE_MEMORY_PATH: "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        }
        reading, notes = R.read_pressure(read=files.__getitem__, cpu_count=2)
        self.assertEqual(reading.load_per_cpu, 0.4)
        self.assertEqual(reading.mem_available_pct, 55.0)
        self.assertEqual(reading.cpu_some_avg10, 0.1)
        self.assertEqual(notes, [])

    def test_unreadable_files_read_unknown_with_notes(self):
        def read(path):
            raise OSError(f"cannot read {path}")

        reading, notes = R.read_pressure(read=read, cpu_count=2)
        self.assertIsNone(reading.load_per_cpu)
        self.assertIsNone(reading.mem_available_pct)
        self.assertFalse(reading.saturated)
        self.assertEqual(len(notes), 2)
        self.assertTrue(all("cannot read" in note for note in notes))

    def test_absent_stall_files_are_quiet(self):
        files = {R.LOADAVG_PATH: LOADAVG, R.MEMINFO_PATH: MEMINFO}

        def read(path):
            if path in files:
                return files[path]
            raise FileNotFoundError(path)

        reading, notes = R.read_pressure(read=read, cpu_count=2)
        self.assertIsNone(reading.cpu_some_avg10)
        self.assertEqual(notes, [])


class SliceUseParse(unittest.TestCase):
    def test_parses_the_three_leaf_files(self):
        measured = R.parse_slice_use("12582912\n", "8\n", "usage_usec 5000\n")
        self.assertEqual(
            (measured.memory_mb, measured.pids, measured.cpu_usage_usec), (12.0, 8, 5000)
        )

    def test_missing_or_garbage_reads_unknown(self):
        measured = R.parse_slice_use(None, "garbage\n", "no counter here\n")
        self.assertIsNone(measured.memory_mb)
        self.assertIsNone(measured.pids)
        self.assertIsNone(measured.cpu_usage_usec)

    def test_cpu_stat_finds_usage_among_other_lines(self):
        measured = R.parse_slice_use(None, None, "user_usec 1\nusage_usec 42\nsystem_usec 2\n")
        self.assertEqual(measured.cpu_usage_usec, 42)


class ReadSliceUse(unittest.TestCase):
    def test_reads_the_leaf_dir_through_the_reader(self):
        files = {
            "/slice/memory.current": "2097152\n",
            "/slice/pids.current": "3\n",
            "/slice/cpu.stat": "usage_usec 7000\n",
        }
        measured, notes = R.read_slice_use("/slice", read=files.__getitem__)
        self.assertEqual(
            (measured.memory_mb, measured.pids, measured.cpu_usage_usec), (2.0, 3, 7000)
        )
        self.assertEqual(notes, [])

    def test_missing_leaf_dir_reads_unknown_with_notes(self):
        def read(path):
            raise OSError(f"cannot read {path}")

        measured, notes = R.read_slice_use("/slice", read=read)
        self.assertIsNone(measured.memory_mb)
        self.assertIsNone(measured.pids)
        self.assertIsNone(measured.cpu_usage_usec)
        self.assertEqual(len(notes), 3)
        self.assertTrue(all("cannot read" in note for note in notes))


class CapacityReport(unittest.TestCase):
    def test_report_shape_matches_the_golden_file(self):
        import json
        import os

        report = R.capacity_report(
            host_budget=triple(8000, 16000, 4096),
            charged=charged(
                (2500, 5120, 120), {"proj-a": (2000, 4096, 100), "proj-b": (500, 1024, 20)}
            ),
            pressure=R.Pressure(load_per_cpu=0.4, mem_available_pct=55.0, saturated=False),
        )
        golden = os.path.join(os.path.dirname(__file__), "golden", "capacity-report.json")
        with open(golden, encoding="utf-8") as handle:
            self.assertEqual(report, json.load(handle))

    def test_report_carries_its_schema_version(self):
        report = R.capacity_report(host_budget=None, charged=charged(), pressure=R.Pressure())
        self.assertEqual(report["schema_version"], R.REPORT_SCHEMA_VERSION)
        self.assertIsNone(report["host_budget"])
        self.assertEqual(
            report["pressure"],
            {
                "load_per_cpu": None,
                "mem_available_pct": None,
                "saturated": False,
                "cpu_some_avg10": None,
                "memory_some_avg10": None,
            },
        )

    def test_by_project_is_sorted(self):
        report = R.capacity_report(
            host_budget=None,
            charged=charged((0, 0, 0), {"b": (1, 1, 1), "a": (2, 2, 2)}),
            pressure={"load_per_cpu": None, "mem_available_pct": None, "saturated": False},
        )
        self.assertEqual(list(report["by_project"]), ["a", "b"])


class ChargeLiveUnits(unittest.TestCase):
    """The shared charged-usage sum the gate and the snapshot both read.

    Live means listed by the manager: a registry entry whose unit is
    gone holds no share, so a crash frees the pool at once. A unit
    counts for the project its ticket names, else the project whose
    listing owns it, never an empty name.
    """

    ROLES = {
        "worker": {"cpu_m": 1000, "memory_mb": 2048, "pids": 100},
        "reviewer": {"cpu_m": 500, "memory_mb": 1024, "pids": 50},
    }

    def test_a_crashed_agent_holds_no_share(self):
        rows = [
            (
                {
                    "id": "pa-lib-5",
                    "role": "worker",
                    "ticket": {"project": "proj-a", "repo": "lib", "number": 5},
                },
                "live",
            )
        ]
        charged, per_unit = R.charge_live_units(rows, {"proj-a": []}, {"proj-a"}, self.ROLES)
        self.assertEqual(charged.total, R.Usage())
        self.assertEqual(charged.by_project, {})
        self.assertEqual(per_unit, {})

    def test_a_live_agent_without_a_ticket_pays_its_prefix_owner(self):
        rows = [({"id": "pb-web-3", "role": "reviewer", "ticket": None}, "live")]
        charged, per_unit = R.charge_live_units(
            rows, {"proj-b": ["pb-web-3"]}, {"proj-a", "proj-b"}, self.ROLES
        )
        self.assertEqual(charged.total, R.Usage(500, 1024, 50))
        self.assertEqual(charged.by_project, {"proj-b": R.Usage(500, 1024, 50)})
        self.assertNotIn("", charged.by_project)
        self.assertEqual(per_unit, {"pb-web-3": ("proj-b", R.Usage(500, 1024, 50))})

    def test_a_ticket_naming_no_registered_project_pays_the_listing_owner(self):
        rows = [
            (
                {
                    "id": "pb-web-3",
                    "role": "worker",
                    "ticket": {"project": "ghost", "repo": "web", "number": 3},
                },
                "live",
            )
        ]
        charged, _per_unit = R.charge_live_units(
            rows, {"proj-b": ["pb-web-3"]}, {"proj-b"}, self.ROLES
        )
        self.assertEqual(charged.by_project, {"proj-b": R.Usage(1000, 2048, 100)})

    def test_a_ticket_naming_a_project_pays_that_project(self):
        rows = [
            (
                {
                    "id": "pb-web-3",
                    "role": "worker",
                    "ticket": {"project": "proj-a", "repo": "web", "number": 3},
                },
                "live",
            )
        ]
        charged, _per_unit = R.charge_live_units(
            rows, {"proj-b": ["pb-web-3"]}, {"proj-a", "proj-b"}, self.ROLES
        )
        self.assertEqual(charged.by_project, {"proj-a": R.Usage(1000, 2048, 100)})

    def test_an_unpriced_role_charges_zero_but_still_attributes(self):
        rows = [({"id": "pa-lib-5", "role": "bogus", "ticket": None}, "live")]
        charged, _per_unit = R.charge_live_units(
            rows, {"proj-a": ["pa-lib-5"]}, {"proj-a"}, self.ROLES
        )
        self.assertEqual(charged.total, R.Usage())
        self.assertEqual(charged.by_project, {"proj-a": R.Usage()})

    def test_a_missing_role_is_priced_as_the_default_role(self):
        rows = [({"id": "pa-lib-5", "ticket": None}, "live")]
        charged, _per_unit = R.charge_live_units(
            rows, {"proj-a": ["pa-lib-5"]}, {"proj-a"}, self.ROLES
        )
        self.assertEqual(charged.total, R.Usage(1000, 2048, 100))

    def test_a_tuple_listing_charges_like_a_list(self):
        rows = [({"id": "pa-lib-5", "role": "worker", "ticket": None}, "live")]
        charged, _per_unit = R.charge_live_units(
            rows, {"proj-a": ("pa-lib-5",)}, {"proj-a"}, self.ROLES
        )
        self.assertEqual(charged.total, R.Usage(1000, 2048, 100))

    def test_a_non_sequence_listing_is_a_limits_error(self):
        rows = [({"id": "pa-lib-5", "role": "worker", "ticket": None}, "live")]
        for bad in (None, 5, "pa-lib-5", ["pa-lib-5", 7], ("pa-lib-5", None)):
            with self.subTest(listing=bad):
                with self.assertRaises(R.LimitsError):
                    R.charge_live_units(rows, {"proj-a": bad}, {"proj-a"}, self.ROLES)
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_non_mapping_roles_are_a_limits_error(self):
        rows = [({"id": "pa-lib-5", "role": "worker", "ticket": None}, "live")]
        for bad in (None, [], "worker", [("worker", dict(WORKER))]):
            with self.subTest(roles=bad):
                with self.assertRaises(R.LimitsError):
                    R.charge_live_units(rows, {"proj-a": ["pa-lib-5"]}, {"proj-a"}, bad)
        self.assertEqual(R.LimitsError.exit_code, 4)


class LiveUnitsByOwner(unittest.TestCase):
    """The one listing the gate and the snapshot intersect the registry with."""

    PREFIX_A = "pa-"
    PREFIX_B = "pb-"

    def list_fn(self, table):
        def fake(prefix):
            return table.get(prefix, [])

        return fake

    def test_lists_live_well_formed_units_per_owner(self):
        table = {
            self.PREFIX_A: [
                ("pa-lib-5", "active", "running"),
                ("pa-db-7", "failed", "failed"),
                ("pa-noise", "active", "running"),
            ],
            self.PREFIX_B: [("pb-web-3", "activating", "start")],
        }
        self.assertEqual(
            R.live_units_by_owner(
                [("proj-a", self.PREFIX_A), ("proj-b", self.PREFIX_B)],
                "proj-a",
                self.PREFIX_A,
                self.list_fn(table),
            ),
            {"proj-a": ["pa-lib-5"], "proj-b": ["pb-web-3"]},
        )

    def test_every_registered_project_gets_a_key(self):
        self.assertEqual(
            R.live_units_by_owner(
                [("proj-a", self.PREFIX_A), ("proj-b", self.PREFIX_B)],
                "proj-a",
                self.PREFIX_A,
                self.list_fn({}),
            ),
            {"proj-a": [], "proj-b": []},
        )

    def test_an_unregistered_fallback_is_keyed_by_project_name(self):
        table = {self.PREFIX_B: [("pb-web-3", "active", "running")]}
        self.assertEqual(
            R.live_units_by_owner(
                [("proj-a", self.PREFIX_A)],
                "other",
                self.PREFIX_B,
                self.list_fn(table),
            ),
            {"proj-a": [], "other": ["pb-web-3"]},
        )

    def test_an_already_listed_prefix_is_never_read_twice(self):
        calls = []

        def fake(prefix):
            calls.append(prefix)
            return [(self.PREFIX_A + "lib-5", "active", "running")]

        self.assertEqual(
            R.live_units_by_owner(
                [("proj-a", self.PREFIX_A), ("proj-b", self.PREFIX_B)],
                "pa-",
                self.PREFIX_A,
                fake,
            ),
            {"proj-a": ["pa-lib-5"], "proj-b": []},
        )
        self.assertEqual(sorted(calls), [self.PREFIX_A, self.PREFIX_B])


class RequestDefaults(unittest.TestCase):
    def test_a_role_less_request_is_priced_as_the_default_role(self):
        with mock.patch.object(prompts, "DEFAULT_ROLE", "reviewer"):
            verdict = R.admit({"project": "web"}, charged(), limits(), NOW)
        self.assertEqual(verdict.cost, R.Usage(500, 1024, 50))

    def test_a_role_less_request_mapping_names_the_default_role(self):
        self.assertEqual(
            R._request({"project": "web"}),
            R.Request(project="web", role=prompts.DEFAULT_ROLE),
        )

    def test_an_explicit_empty_role_is_not_coerced_to_the_default(self):
        self.assertEqual(
            R._request({"project": "web", "role": ""}),
            R.Request(project="web", role=""),
        )

    def test_an_explicit_empty_role_is_a_configuration_error(self):
        with self.assertRaises(R.LimitsError) as caught:
            R.admit({"project": "web", "role": ""}, charged(), limits(), NOW)
        self.assertIn("unknown role", str(caught.exception))
        self.assertEqual(R.LimitsError.exit_code, 4)


class CapacityReportShape(unittest.TestCase):
    def test_a_non_mapping_by_project_is_a_limits_error(self):
        bad_inputs: tuple[Any, ...] = (["proj-a"], "proj-a", 5, 0, "", [], False, None)
        for bad in bad_inputs:
            with self.subTest(by_project=bad):
                with self.assertRaises(R.LimitsError):
                    R.capacity_report(
                        host_budget=None,
                        charged={
                            "total": triple(),
                            "by_project": bad,
                        },
                        pressure=R.Pressure(),
                    )
                with self.assertRaises(R.LimitsError):
                    R.capacity_report(
                        host_budget=None,
                        charged=R.Charged(total=R.Usage(), by_project=bad),
                        pressure=R.Pressure(),
                    )
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_a_missing_by_project_reads_empty(self):
        report = R.capacity_report(
            host_budget=None, charged={"total": triple()}, pressure=R.Pressure()
        )
        self.assertEqual(report["by_project"], {})
        self.assertEqual(report["charged"], triple())

    def test_a_non_boolean_saturated_is_a_limits_error(self):
        bad_inputs: tuple[Any, ...] = ("false", "true", 0, 1, None)
        for bad in bad_inputs:
            with self.subTest(saturated=bad):
                with self.assertRaises(R.LimitsError):
                    R.capacity_report(
                        host_budget=None,
                        charged=charged(),
                        pressure={"load_per_cpu": None, "saturated": bad},
                    )
                with self.assertRaises(R.LimitsError):
                    R.capacity_report(
                        host_budget=None,
                        charged=charged(),
                        pressure=R.Pressure(saturated=bad),
                    )

    def test_real_booleans_and_a_missing_flag_stay_valid(self):
        for good in (True, False):
            with self.subTest(saturated=good):
                report = R.capacity_report(
                    host_budget=None,
                    charged=charged(),
                    pressure={"saturated": good},
                )
                self.assertIs(report["pressure"]["saturated"], good)
        report = R.capacity_report(
            host_budget=None, charged=charged(), pressure={"load_per_cpu": None}
        )
        self.assertIs(report["pressure"]["saturated"], False)


if __name__ == "__main__":
    unittest.main()
