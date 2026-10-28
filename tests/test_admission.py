"""Admission: one pure decision over the host total, floors and ceilings."""

import unittest

from corral import admission, settings

A = admission


def req(project="a", repo="r", ticket=1, kind="start", dispatch_now=False):
    return A.Request(
        project=project, repo=repo, ticket=ticket, kind=kind, dispatch_now=dispatch_now
    )


def state(live=(), pending=(), forced=()):
    return A.State(live=list(live), pending=list(pending), forced=list(forced))


def limits(total=4, projects=None, repo_cap=0):
    return A.Limits(total=total, projects=dict(projects or {}), repo_cap=repo_cap)


class MessageNames(unittest.TestCase):
    def test_limit_errors_name_the_registered_setting(self):
        for bad in (limits(total=-1), limits(total=2, projects={"a": {"floor": 3}})):
            with self.assertRaises(A.LimitsError) as caught:
                A.validate_limits(bad)
            self.assertIn("admission.total_cap", str(caught.exception))


class VerdictTable(unittest.TestCase):
    def check(self, request, st, lim, allowed, code, counts=None):
        with self.subTest(request=request, state=st, limits=lim):
            verdict = A.admit(request, st, lim)
            self.assertEqual(verdict.allowed, allowed)
            self.assertEqual(verdict.code, code)
            if counts is not None:
                self.assertEqual(
                    (verdict.total, verdict.effective, verdict.project, verdict.repo), counts
                )
            self.assertTrue(verdict.reason)

    def test_table(self):
        full = state(live=[("a", "r", 1), ("a", "r", 2)], pending=[])
        cases = [
            # (name, request, state, limits, allowed, code, counts)
            (
                "an empty host allows a start",
                req(),
                state(),
                limits(),
                True,
                "ok",
                (0, 4, 0, 0),
            ),
            (
                "a ticket in both live and pending counts once",
                req("b", "s", 9),
                state(live=[("a", "r", 1), ("a", "r", 2)], pending=[("a", "r", 1)]),
                limits(total=2),
                False,
                "total-full",
                (2, 2, 0, 0),
            ),
            (
                "a ticket in review with no agent takes no slot",
                req("b", "s", 9),
                state(live=[("a", "r", 1)]),
                limits(total=2),
                True,
                "ok",
                (1, 2, 0, 0),
            ),
            (
                "a full fleet refuses a resume like a start: urgency is not an input",
                req("b", "s", 9, kind="resume"),
                state(live=[("a", "r", 1)]),
                limits(total=1),
                False,
                "total-full",
                (1, 1, 0, 0),
            ),
            (
                "a project at its ceiling is refused",
                req("a", "r", 9),
                state(live=[("a", "r", 1)]),
                limits(total=4, projects={"a": {"floor": 0, "ceiling": 1}}),
                False,
                "project-ceiling",
                (1, 4, 1, 1),
            ),
            (
                "a repo at its cap is refused",
                req("b", "r", 9),
                state(live=[("a", "r", 1)]),
                limits(total=4, repo_cap=1),
                False,
                "repo-cap",
                (1, 4, 0, 1),
            ),
            (
                "a pending resume holds a slot",
                req("b", "s", 9),
                state(pending=[("a", "r", 1)]),
                limits(total=1),
                False,
                "total-full",
                (1, 1, 0, 0),
            ),
            (
                "a floor admits a second project when the first fills the host",
                req("b", "s", 9),
                full,
                limits(total=2, projects={"a": {"floor": 0}, "b": {"floor": 1}}),
                True,
                "ok",
                (2, 2, 0, 0),
            ),
            (
                "a project without a floor waits when the host is full",
                req("c", "t", 9),
                full,
                limits(total=2, projects={"a": {"floor": 0}, "b": {"floor": 1}}),
                False,
                "total-full",
                (2, 2, 0, 0),
            ),
            (
                "a project already at its floor waits when the host is full",
                req("b", "s", 9),
                state(live=[("a", "r", 1), ("b", "s", 7)]),
                limits(total=2, projects={"a": {"floor": 0}, "b": {"floor": 1}}),
                False,
                "total-full",
                (2, 2, 1, 1),
            ),
            (
                "dispatch_now skips a full total",
                req("b", "s", 9, dispatch_now=True),
                full,
                limits(total=2),
                True,
                "ok",
                (2, 2, 0, 0),
            ),
            (
                "a forced occupant raises the effective total for normal requests",
                req("b", "s", 9),
                state(live=[("a", "r", 1)], forced=[("a", "r", 1)]),
                limits(total=1),
                True,
                "ok",
                (1, 2, 0, 0),
            ),
            (
                "a forced ticket outside live and pending raises nothing",
                req("b", "s", 9),
                state(live=[("a", "r", 1)], forced=[("a", "r", 2)]),
                limits(total=1),
                False,
                "total-full",
                (1, 1, 0, 0),
            ),
            (
                "a ceiling holds even for dispatch_now",
                req("a", "r", 9, dispatch_now=True),
                state(live=[("a", "r", 1)]),
                limits(total=1, projects={"a": {"floor": 0, "ceiling": 1}}),
                False,
                "project-ceiling",
                (1, 1, 1, 1),
            ),
            (
                "a repo cap holds even for dispatch_now",
                req("b", "r", 9, dispatch_now=True),
                state(live=[("a", "r", 1)]),
                limits(total=1, repo_cap=1),
                False,
                "repo-cap",
                (1, 1, 0, 1),
            ),
            (
                "an unknown project defaults to floor zero and no ceiling",
                req("b", "s", 9),
                state(live=[("a", "r", 1), ("a", "r", 2), ("a", "r", 3)]),
                limits(total=4, projects={"a": {"floor": 0, "ceiling": 3}}),
                True,
                "ok",
                (3, 4, 0, 0),
            ),
            (
                "an unknown project waits when the host is full",
                req("b", "s", 9),
                full,
                limits(total=2, projects={"a": {"floor": 0, "ceiling": 2}}),
                False,
                "total-full",
                (2, 2, 0, 0),
            ),
        ]
        for name, request, st, lim, allowed, code, counts in cases:
            with self.subTest(name):
                self.check(request, st, lim, allowed, code, counts)

    def test_every_verdict_names_its_numbers(self):
        verdict = A.admit(
            req("a", "r", 9),
            state(live=[("a", "r", 1)]),
            limits(total=4, projects={"a": {"floor": 0, "ceiling": 1}}),
        )
        self.assertIn("1", verdict.reason)
        self.assertIn("ceiling", verdict.reason)

        verdict = A.admit(req("b", "s", 9), state(live=[("a", "r", 1)]), limits(total=1))
        self.assertIn("1", verdict.reason)
        self.assertIn("full", verdict.reason.lower())

    def test_ceiling_checked_before_repo_cap(self):
        verdict = A.admit(
            req("a", "r", 9),
            state(live=[("a", "r", 1)]),
            limits(total=4, projects={"a": {"floor": 0, "ceiling": 1}}, repo_cap=1),
        )
        self.assertEqual((verdict.allowed, verdict.code), (False, "project-ceiling"))


class ValidateLimits(unittest.TestCase):
    def test_valid_limits_pass(self):
        for lim in (
            limits(),
            limits(total=0, projects={}),
            limits(total=2, projects={"a": {"floor": 0}, "b": {"floor": 2}}),
            limits(total=1, projects={"a": {"floor": 0, "ceiling": 0}}),
            limits(total=3, projects={"a": {"ceiling": 2}}, repo_cap=1),
        ):
            with self.subTest(lim=lim):
                A.validate_limits(lim)

    def test_bad_limits_raise_a_configuration_error(self):
        bad = [
            limits(total=-1),
            limits(repo_cap=-1),
            limits(total=1, projects={"a": {"floor": -1}}),
            limits(total=1, projects={"a": {"floor": 0, "ceiling": -1}}),
            limits(total=2, projects={"a": {"floor": 3}}),
            limits(total=2, projects={"a": {"floor": 2}, "b": {"floor": 1}}),
            limits(total=2, projects={"a": {"floor": 2, "ceiling": 1}}),
        ]
        for lim in bad:
            with self.subTest(lim=lim):
                with self.assertRaises(A.LimitsError) as ctx:
                    A.validate_limits(lim)
                self.assertTrue(str(ctx.exception))
        self.assertEqual(A.LimitsError.exit_code, 4)


class Settings(unittest.TestCase):
    def test_admission_registers_its_settings(self):
        self.assertEqual(settings.REGISTRY["admission.total_cap"].default, 4)
        self.assertEqual(settings.REGISTRY["admission.repo_cap"].default, 0)
        self.assertIn("corral.admission", settings.PROVIDERS)

    def test_a_host_file_may_set_admission_keys(self):
        resolved = settings.resolve(host={"admission": {"total_cap": 6, "repo_cap": 2}}, env={})
        self.assertEqual(resolved["admission.total_cap"], 6)
        self.assertEqual(resolved["admission.repo_cap"], 2)


if __name__ == "__main__":
    unittest.main()
