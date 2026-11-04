"""The drive gate order: managed holders, priority rank, candidate ordering.

Pure functions over plain data, no store, no network, no subprocess.
The pass tests use a temporary board store, stub policy modules written
to a temporary directory, and injected plain-data inputs for every
host read, so nothing reaches the network, subprocesses or real dirs.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from corral import accounts, admission, board, drive, projects, registry
from corral.eligibility import LinkedPull, StartRequest, TicketRow
from tests.test_drive import policy_source, stored


def request(repo="app", number=7, kind="start", weight=0.0):
    return StartRequest(repo=repo, number=number, kind=kind, weight=weight)


def row(
    repo="app",
    number=7,
    state="open",
    agent="",
    priority="",
    labels=(),
    external=False,
):
    return TicketRow(
        repo=repo,
        number=number,
        state=state,
        agent=agent,
        priority=priority,
        labels=labels,
        external=external,
    )


def requests_of(items):
    """The requests of ordered candidates, asserting none is withdrawn."""
    answered = []
    for item in items:
        assert item.request is not None
        answered.append(item.request)
    return answered


def entry(project="blue", req=None, row_=None, number=7, kind="start", wait=1000.0):
    return drive.GateInput(
        project=project,
        request=req if req is not None else request(number=number, kind=kind),
        row=row_ if row_ is not None else row(number=number),
        wait=wait,
    )


class ManagedAgentTest(unittest.TestCase):
    def test_drive_prefix_is_managed(self):
        self.assertTrue(
            drive.is_managed_agent(drive.mint_agent("acme/app", 7, host="host-1", now=2000.0))
        )
        self.assertTrue(drive.is_managed_agent("drive-"))

    def test_a_previously_minted_shape_stays_managed(self):
        self.assertTrue(drive.is_managed_agent("drive-host-1-1700000000-app--7"))

    def test_anything_else_is_foreign(self):
        self.assertFalse(drive.is_managed_agent("agent-1"))
        self.assertFalse(drive.is_managed_agent("drivex-1"))
        self.assertFalse(drive.is_managed_agent(""))
        self.assertFalse(drive.is_managed_agent(None))

    def test_prefix_is_the_single_shared_constant(self):
        self.assertEqual(drive.MANAGED_PREFIX, "drive-")


class PriorityRankTest(unittest.TestCase):
    def test_higher_first_following_the_tracker_order(self):
        ranks = [drive.priority_rank(name) for name in ("urgent", "high", "normal", "low")]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(len(set(ranks)), 4)

    def test_unknown_priorities_sort_last(self):
        self.assertEqual(drive.priority_rank(""), 0)
        self.assertEqual(drive.priority_rank("someday"), 0)
        self.assertGreater(drive.priority_rank("low"), 0)


class RouteKindTest(unittest.TestCase):
    def test_unclaimed_resume_and_fix_reclassify_as_start(self):
        self.assertEqual(drive.route_kind("resume", ""), "start")
        self.assertEqual(drive.route_kind("fix", ""), "start")

    def test_claimed_kinds_and_plain_starts_pass_through(self):
        self.assertEqual(drive.route_kind("resume", "drive-x"), "resume")
        self.assertEqual(drive.route_kind("fix", "drive-x"), "fix")
        self.assertEqual(drive.route_kind("start", ""), "start")
        self.assertEqual(drive.route_kind("review", ""), "review")
        self.assertEqual(drive.route_kind("start", "drive-x"), "start")


class OrderTest(unittest.TestCase):
    def test_resume_and_fix_come_before_new_starts(self):
        ordered = drive.order(
            [
                entry(number=1, kind="start"),
                entry(number=2, kind="review"),
                entry(number=3, kind="fix"),
                entry(number=4, kind="resume"),
            ]
        )
        self.assertEqual(
            [r.kind for r in requests_of(ordered)],
            ["fix", "resume", "start", "review"],
        )

    def test_higher_priority_first(self):
        ordered = drive.order(
            [
                entry(req=request(number=1, kind="start"), row_=row(number=1, priority="low")),
                entry(req=request(number=2, kind="start"), row_=row(number=2, priority="high")),
            ]
        )
        self.assertEqual([r.number for r in requests_of(ordered)], [2, 1])

    def test_longer_wait_first_within_a_kind(self):
        ordered = drive.order(
            [
                entry(req=request(number=1, kind="start"), wait=2000.0),
                entry(req=request(number=2, kind="start"), wait=1000.0),
            ]
        )
        self.assertEqual([r.number for r in requests_of(ordered)], [2, 1])

    def test_ties_break_by_project_repo_number(self):
        ordered = drive.order(
            [
                entry(project="blue", req=request(repo="app", number=9, kind="start")),
                entry(project="amber", req=request(repo="app", number=9, kind="start")),
                entry(project="amber", req=request(repo="app", number=7, kind="start")),
            ]
        )
        self.assertEqual(
            [(item.project, r.repo, r.number) for item, r in zip(ordered, requests_of(ordered))],
            [("amber", "app", 7), ("amber", "app", 9), ("blue", "app", 9)],
        )

    def test_missing_rows_sort_with_unknown_priority_and_zero_wait(self):
        ordered = drive.order(
            [
                drive.GateInput(project="blue", request=request(number=1), row=None),
                entry(number=2, kind="start"),
            ]
        )
        self.assertEqual([r.number for r in requests_of(ordered)], [1, 2])


def limits(total=4, floor=0, ceiling=None, repo_cap=0, project="blue"):
    return admission.Limits(
        total=total,
        projects={project: {"floor": floor, "ceiling": ceiling}},
        repo_cap=repo_cap,
    )


def quota_source(chains=None, **fields):
    values = {"stale_after": 900.0, "low_pct": 90, "unknown_blocks": False}
    values.update(fields)
    return drive.QuotaSource(chains=chains or {}, **values)


def labels_for(mapping):
    def read(kind, repo, number):
        key = (kind, repo, number)
        if key not in mapping:
            raise AssertionError(f"unexpected label read {key}")
        value = mapping[key]
        if isinstance(value, Exception):
            raise value
        return value

    return read


def empty_labels(kind, repo, number):
    return []


def gate(candidate, **fields):
    values = {
        "read_labels": empty_labels,
        "hold_label": "hold",
        "live": frozenset(),
        "limits": limits(),
        "quota_source": quota_source(),
        "ledger": {},
        "now": 2000.0,
    }
    values.update(fields)
    return drive.gate(candidate, **values)


class RowGateTest(unittest.TestCase):
    def test_a_request_with_no_store_row_skips_as_withdrawn(self):
        gated = gate(drive.GateInput(project="blue", request=request(number=7), row=None))
        self.assertEqual(gated.verdict, "skip")
        self.assertEqual(gated.code, "row")
        self.assertIn("no store row", gated.reason)

    def test_a_closed_row_skips_as_not_open(self):
        gated = gate(entry(row_=row(state="closed")))
        self.assertEqual(gated.verdict, "skip")
        self.assertEqual(gated.code, "row")
        self.assertIn("closed", gated.reason)
        self.assertIn("not open", gated.reason)

    def test_an_external_row_skips(self):
        gated = gate(entry(row_=row(external=True)))
        self.assertEqual(gated.verdict, "skip")
        self.assertEqual(gated.code, "row")
        self.assertIn("externally owned", gated.reason)

    def test_a_claimed_row_no_project_requested_skips_as_withdrawn(self):
        candidate = drive.GateInput(
            project="",
            request=None,
            row=row(agent="drive-host-1-x"),
            wait=1000.0,
        )
        gated = gate(candidate)
        self.assertEqual(gated.verdict, "skip")
        self.assertEqual(gated.code, "row")
        self.assertIn("withdrawn", gated.reason)


class ClaimRoutingTest(unittest.TestCase):
    def test_a_start_on_a_claimed_row_skips_naming_the_holder(self):
        gated = gate(entry(row_=row(agent="drive-host-1-x")))
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("already claimed by `drive-host-1-x`", gated.reason)

    def test_a_review_on_a_claimed_row_waits_for_release(self):
        gated = gate(entry(kind="review", row_=row(agent="drive-host-1-x")))
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("still holds", gated.reason)

    def test_a_resume_on_a_managed_holder_proceeds(self):
        gated = gate(entry(kind="resume", row_=row(agent="drive-host-1-x")))
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("resume", "resume", ""))
        self.assertIn("drive-host-1-x", gated.reason)

    def test_a_fix_on_a_managed_holder_proceeds(self):
        gated = gate(entry(kind="fix", row_=row(agent="drive-host-1-x")))
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("fix", "fix", ""))

    def test_a_fix_behind_a_reviewer_holder_skips_with_the_claim_kept(self):
        gated = gate(
            entry(kind="fix", row_=row(agent="drive-host-1-x")),
            agent_states={"drive-host-1-x": (registry.LIVE, 1990.0)},
            agent_roles={"drive-host-1-x": "reviewer"},
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("waits for release", gated.reason)

    def test_a_resume_behind_a_reviewer_holder_skips_with_the_claim_kept(self):
        gated = gate(
            entry(kind="resume", row_=row(agent="drive-host-1-x")),
            agent_states={"drive-host-1-x": (registry.LIVE, 1990.0)},
            agent_roles={"drive-host-1-x": "reviewer"},
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("waits for release", gated.reason)

    def test_a_fix_behind_a_worker_holder_proceeds(self):
        gated = gate(
            entry(kind="fix", row_=row(agent="drive-host-1-x")),
            agent_states={"drive-host-1-x": (registry.LIVE, 1990.0)},
            agent_roles={"drive-host-1-x": "worker"},
        )
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("fix", "fix", ""))

    def test_a_fix_behind_a_live_reviewer_waits_through_the_ticket_resolver(self):
        holder, unit = "drive-a1b2c3d4-7", "blue-agent-app-7-r1"
        gated = gate(
            entry(kind="fix", row_=row(agent=holder)),
            agent_states={unit: (registry.LIVE, 1990.0)},
            agent_tickets={unit: ("app", 7)},
            agent_roles={unit: "reviewer"},
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("waits for release", gated.reason)
        self.assertIn(holder, gated.reason)

    def test_a_resume_behind_a_live_reviewer_waits_through_the_ticket_resolver(self):
        holder, unit = "drive-a1b2c3d4-7", "blue-agent-app-7-r1"
        gated = gate(
            entry(kind="resume", row_=row(agent=holder)),
            agent_states={unit: (registry.LIVE, 1990.0)},
            agent_tickets={unit: ("app", 7)},
            agent_roles={unit: "reviewer"},
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("waits for release", gated.reason)

    def test_a_reviewer_role_on_another_ticket_does_not_block_a_fix(self):
        holder, unit = "drive-a1b2c3d4-7", "blue-agent-app-8-r1"
        gated = gate(
            entry(kind="fix", row_=row(agent=holder)),
            agent_states={unit: (registry.LIVE, 1990.0)},
            agent_tickets={unit: ("app", 8)},
            agent_roles={unit: "reviewer"},
        )
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("fix", "fix", ""))

    def test_a_resume_on_a_foreign_holder_skips_as_foreign_claim(self):
        gated = gate(entry(kind="resume", row_=row(agent="operator-hand")))
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("foreign claim", gated.reason)

    def test_a_fix_on_a_foreign_holder_skips_as_foreign_claim(self):
        gated = gate(entry(kind="fix", row_=row(agent="other-host-agent")))
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("foreign claim", gated.reason)

    def test_an_unclaimed_resume_reclassifies_as_a_start(self):
        gated = gate(entry(kind="resume"))
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("start", "start", ""))
        self.assertIn("fresh start", gated.reason)

    def test_an_unclaimed_fix_reclassifies_as_a_start(self):
        gated = gate(entry(kind="fix"))
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("start", "start", ""))

    def test_an_unclaimed_start_proceeds(self):
        gated = gate(entry(kind="start"))
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("start", "start", ""))

    def test_an_unclaimed_review_with_a_finished_worker_proceeds(self):
        gated = gate(entry(kind="review"), finished={("app", 7): ("drive-w-1", "worker")})
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("review", "review", ""))
        self.assertIn("review slot", gated.reason)

    def test_a_review_without_a_finished_worker_is_not_pr_ready(self):
        gated = gate(entry(kind="review"))
        self.assertEqual((gated.verdict, gated.code), ("skip", "row"))
        self.assertIn("nothing to review", gated.reason)

    def test_a_review_after_a_review_is_merge_ready(self):
        gated = gate(entry(kind="review"), finished={("app", 7): ("drive-r-1", "reviewer")})
        self.assertEqual((gated.verdict, gated.code), ("skip", "row"))
        self.assertIn("merge-ready", gated.reason)

    def test_a_fix_after_the_fix_landed_is_skipped(self):
        gated = gate(entry(kind="fix"), finished={("app", 7): ("drive-w-2", "worker")})
        self.assertEqual((gated.verdict, gated.code), ("skip", "row"))
        self.assertIn("fix already landed", gated.reason)

    def test_a_fix_after_a_review_starts_fresh(self):
        gated = gate(entry(kind="fix"), finished={("app", 7): ("drive-r-1", "reviewer")})
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("start", "start", ""))
        self.assertIn("fresh start", gated.reason)

    def test_a_start_on_a_finished_holder_is_taken_over_by_a_reviewer(self):
        gated = gate(
            entry(row_=row(agent="drive-w-1")),
            finished={("app", 7): ("drive-w-1", "worker")},
        )
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("review", "review", ""))
        self.assertIn("takes over", gated.reason)
        self.assertIn("drive-w-1", gated.reason)

    def test_a_start_on_a_finished_holder_with_an_unknown_role_is_taken_over(self):
        gated = gate(
            entry(row_=row(agent="drive-w-1")),
            finished={("app", 7): ("drive-w-1", None)},
        )
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("review", "review", ""))
        self.assertIn("takes over", gated.reason)

    def test_a_review_on_a_finished_holder_is_taken_over_by_a_reviewer(self):
        gated = gate(
            entry(kind="review", row_=row(agent="drive-w-1")),
            finished={("app", 7): ("drive-w-1", "worker")},
        )
        self.assertEqual((gated.verdict, gated.kind, gated.code), ("review", "review", ""))
        self.assertIn("takes over", gated.reason)

    def test_a_start_on_a_live_holder_is_still_already_claimed(self):
        gated = gate(
            entry(row_=row(agent="drive-w-1")),
            finished={("app", 7): ("drive-w-9", "worker")},
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("already claimed by `drive-w-1`", gated.reason)

    def test_a_takeover_needs_the_finished_entry_to_name_the_holder(self):
        gated = gate(
            entry(row_=row(agent="drive-w-2")),
            finished={("app", 7): ("drive-w-1", "worker")},
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertIn("already claimed by `drive-w-2`", gated.reason)

    def test_a_finished_reviewer_holder_is_never_taken_over(self):
        for kind in ("start", "review"):
            gated = gate(
                entry(kind=kind, row_=row(agent="drive-r-1")),
                finished={("app", 7): ("drive-r-1", "reviewer")},
            )
            self.assertEqual(gated.verdict, "skip")
            self.assertNotIn("takes over", gated.reason)

    def test_a_takeover_never_touches_a_foreign_holder(self):
        gated = gate(
            entry(kind="review", row_=row(agent="operator-hand")),
            finished={("app", 7): ("operator-hand", "worker")},
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "claim"))
        self.assertNotIn("takes over", gated.reason)

    def test_a_takeover_still_gates_on_hold_labels(self):
        gated = gate(
            entry(row_=row(agent="drive-w-1")),
            finished={("app", 7): ("drive-w-1", "worker")},
            read_labels=labels_for({("issue", "app", 7): ["hold"]}),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "hold"))
        self.assertNotIn("takes over", gated.reason)


class HoldGateTest(unittest.TestCase):
    def test_a_hold_label_on_the_ticket_skips(self):
        gated = gate(
            entry(),
            read_labels=labels_for({("issue", "app", 7): ["hold"]}),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "hold"))
        self.assertIn("hold", gated.reason)

    def test_unreadable_ticket_labels_skip_fail_closed(self):
        gated = gate(
            entry(),
            read_labels=labels_for({("issue", "app", 7): ValueError("boom")}),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "hold"))
        self.assertIn("unreadable", gated.reason)

    def test_malformed_label_answers_skip_fail_closed(self):
        gated = gate(
            entry(),
            read_labels=labels_for({("issue", "app", 7): ["hold", 7]}),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "hold"))
        self.assertIn("unreadable", gated.reason)

    def test_a_hold_label_on_a_linked_pull_skips(self):
        candidate = entry(
            row_=drive_row_with_pulls(row(), (("app", 12),)),
        )
        gated = gate(
            candidate,
            read_labels=labels_for(
                {
                    ("issue", "app", 7): [],
                    ("pr", "app", 12): ["hold"],
                }
            ),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "hold"))
        self.assertIn("hold", gated.reason)

    def test_needs_decision_never_starts(self):
        gated = gate(entry(row_=row(labels=["needs-decision"])))
        self.assertEqual((gated.verdict, gated.code), ("skip", "hold"))
        self.assertIn("needs-decision", gated.reason)


def drive_row_with_pulls(base, pulls):
    return TicketRow(
        repo=base.repo,
        number=base.number,
        title=base.title,
        labels=base.labels,
        priority=base.priority,
        state=base.state,
        agent=base.agent,
        status=base.status,
        external=base.external,
        pulls=tuple(LinkedPull(number=number) for _, number in pulls),
    )


class AdmissionGateTest(unittest.TestCase):
    def test_a_project_ceiling_skips(self):
        gated = gate(
            entry(),
            live=frozenset({("blue", "app", 1), ("blue", "app", 2)}),
            limits=limits(ceiling=2),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "project-ceiling"))

    def test_a_repo_cap_skips(self):
        gated = gate(
            entry(),
            live=frozenset({("blue", "app", 1)}),
            limits=limits(repo_cap=1),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "repo-cap"))

    def test_a_full_total_skips_a_project_at_its_floor(self):
        gated = gate(
            entry(),
            live=frozenset({("blue", "app", 1)}),
            limits=limits(total=1, floor=0),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "total-full"))

    def test_a_floor_project_is_admitted_at_a_full_total(self):
        gated = gate(
            entry(),
            live=frozenset({("blue", "app", 1)}),
            limits=limits(total=1, floor=2),
        )
        self.assertEqual((gated.verdict, gated.code), ("start", ""))
        self.assertIn("floor", gated.reason)


def exhausted_ledger(provider, now):
    return {
        "readings": {provider: {"windows": {"hour": {"used_pct": 100}}, "read_at": now}},
        "marks": {},
    }


class QuotaGateTest(unittest.TestCase):
    def test_an_exhausted_chain_skips(self):
        gated = gate(
            entry(),
            quota_source=quota_source(chains={"worker": ["tired/model"]}),
            ledger=exhausted_ledger("tired", 2000.0),
        )
        self.assertEqual((gated.verdict, gated.code), ("skip", "quota"))
        self.assertIn("quota", gated.reason)
        self.assertIn("worker", gated.reason)

    def test_a_project_without_chains_passes_the_quota_gate(self):
        gated = gate(entry(), ledger=exhausted_ledger("tired", 2000.0))
        self.assertEqual((gated.verdict, gated.code), ("start", ""))


class ProjectQuotaTest(unittest.TestCase):
    def test_project_quota_uses_the_chained_provider_bound(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-quota-pool-"))
        root = os.path.join(tmp, "ops", "blue")
        os.makedirs(root)
        with open(os.path.join(root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[models.default]\nchain = ["tired/model"]\n')
        project = projects.Project(name="blue", unit_prefix="blue-agent", root=root)
        pool = [accounts.Provider(name="tired", reader="pkg:Reader", stale_after_s=60.0)]
        source = drive._project_quota(project, pool)
        self.assertEqual(source.chains, {"default": ["tired/model"]})
        self.assertEqual(source.stale_after, 60.0)

    def test_project_quota_ignores_unchained_pool_providers(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-quota-pool-"))
        root = os.path.join(tmp, "ops", "blue")
        os.makedirs(root)
        with open(os.path.join(root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write('[models.default]\nchain = ["fresh/model"]\n')
        project = projects.Project(name="blue", unit_prefix="blue-agent", root=root)
        pool = [
            accounts.Provider(name="fresh", reader="pkg:Reader", stale_after_s=3600.0),
            accounts.Provider(name="other", reader="pkg:Reader", stale_after_s=30.0),
        ]
        self.assertEqual(drive._project_quota(project, pool).stale_after, 3600.0)


class StoreLabelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-labels-"))
        self.db = os.path.join(self.tmp, "board.sqlite")

    def test_a_pull_read_fails_closed(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        with self.assertRaises(board.StoreError):
            drive._store_labels(store, 2000.0)("pr", "app", 12)

    def test_a_row_with_a_linked_pull_skips_as_unreadable(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        candidate = entry(row_=drive_row_with_pulls(row(), (("app", 12),)))
        gated = gate(candidate, read_labels=drive._store_labels(store, 2000.0))
        self.assertEqual((gated.verdict, gated.code), ("skip", "hold"))
        self.assertIn("unreadable", gated.reason)


class PassWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-gates-"))
        self.db = os.path.join(self.tmp, "board.sqlite")
        self.lock = os.path.join(self.tmp, "drive.lock")
        self.policies = os.path.join(self.tmp, "policies")
        os.makedirs(self.policies)

    def write_policy(self, name, wants):
        path = os.path.join(self.policies, name + ".py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(policy_source(wants))
        return path + ":eligible"

    def project(self, name, entry=None, root=None):
        return projects.Project(
            name=name,
            unit_prefix=name + "-agent-",
            root=root or os.path.join(self.tmp, "ops", name),
            eligibility=entry,
        )

    def run_pass(self, store, found, **fields):
        out, err = io.StringIO(), io.StringIO()
        values = {
            "plan": True,
            "store": store,
            "lock_path": self.lock,
            "out": out,
            "err": err,
            "now": 2000.0,
            "projects_fn": lambda: found,
            "live": frozenset(),
            "limits": admission.Limits(
                total=4, projects={p.name: {"floor": 0, "ceiling": None} for p in found}
            ),
            "quotas": {p.name: drive.QuotaSource(chains={}) for p in found},
            "ledger": {},
            "read_labels": lambda kind, repo, number: [],
            "hold_label": "hold",
            "present_agents": frozenset(),
            "live_units": frozenset(),
            "gone_s": 3600.0,
        }
        values.update(fields)
        code = drive.run_pass(**values)
        return code, out.getvalue(), err.getvalue()

    def test_requests_join_their_rows_and_print_verdicts(self):
        store = stored(self.db, [("app", 7, "open"), ("app", 9, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        code, out, err = self.run_pass(store, [self.project("blue", entry)])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("app#7: start", out)
        self.assertNotIn("app#9", out)
        self.assertIn("would start 1", out)

    def test_a_project_without_an_entry_registers_no_candidates(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        code, out, err = self.run_pass(store, [self.project("blue")])
        self.assertEqual(code, 0)
        self.assertIn("blue", out)
        self.assertIn("would start 0", out)
        self.assertNotIn("app#7: start", out)

    def test_an_unreadable_accounts_file_is_exit_four(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        with mock.patch.object(
            accounts,
            "providers",
            side_effect=accounts.AccountsError("accounts.toml: boom"),
        ):
            code, _, err = self.run_pass(store, [self.project("blue", entry)], quotas={}, pool=None)
        self.assertEqual(code, 4)
        self.assertIn("corral drive:", err)
        self.assertIn("accounts.toml", err)

    def test_a_bad_entry_is_exit_four_naming_project_and_entry(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        good = self.write_policy("good_policy", [("app", 7, "start")])
        code, out, err = self.run_pass(
            store,
            [self.project("blue", "missing_module_xyz:eligible"), self.project("red", good)],
        )
        self.assertEqual(code, 4)
        self.assertIn("blue", err)
        self.assertIn("missing_module_xyz:eligible", err)
        self.assertIn("app#7: start", out)

    def test_a_claimed_row_no_project_requested_skips_as_withdrawn(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        store.claim("app", 7, "drive-host-1-x", "host-1", now=1500.0)
        entry = self.write_policy("blue_policy", [])
        code, out, _ = self.run_pass(store, [self.project("blue", entry)])
        self.assertEqual(code, 0)
        self.assertIn("app#7: skip", out)
        self.assertIn("withdrawn", out)

    def test_a_full_total_stops_new_starts_but_still_scans_resumes(self):
        store = stored(self.db, [("app", 1, "open"), ("app", 2, "open"), ("app", 5, "open")])
        self.addCleanup(store.close)
        store.claim("app", 1, "drive-host-1-x", "host-1", now=1500.0)
        blue = self.write_policy("blue_policy", [("app", 1, "resume"), ("app", 2, "start")])
        red = self.write_policy("red_policy", [("app", 5, "start")])
        code, out, _ = self.run_pass(
            store,
            [self.project("blue", blue), self.project("red", red)],
            live=frozenset({("blue", "app", 1)}),
            limits=admission.Limits(
                total=1,
                projects={
                    "blue": {"floor": 0, "ceiling": None},
                    "red": {"floor": 1, "ceiling": None},
                },
            ),
        )
        self.assertEqual(code, 0)
        self.assertIn("app#1: skip", out)
        self.assertIn("host full", out)
        self.assertIn("app#2: skip", out)
        self.assertIn("new starts stop here", out)
        self.assertIn("app#5: start", out)

    def test_a_resume_without_a_store_row_skips_as_row_under_a_full_total(self):
        store = board.open_store(self.db)
        self.addCleanup(store.close)
        store.upsert_item(
            board.Item(
                repo="app",
                number=1,
                kind="issue",
                title="Title 1",
                body="Body",
                state="open",
                author="operator",
                labels=[],
                priority="high",
                assignees=[],
                url="https://example.com/app/1",
                tracker_updated_at=1000.0,
                extra={},
            ),
            operators=["operator"],
            now=1000.0,
        )
        store.claim("app", 1, "drive-host-1-x", "host-1", now=1500.0)
        blue = self.write_policy("blue_policy", [("app", 1, "fix"), ("app", 9, "resume")])
        code, out, _ = self.run_pass(
            store,
            [self.project("blue", blue)],
            live=frozenset({("blue", "app", 1)}),
            limits=admission.Limits(total=1, projects={"blue": {"floor": 0, "ceiling": None}}),
        )
        self.assertEqual(code, 0)
        self.assertIn("app#1: skip", out)
        self.assertIn("host full", out)
        self.assertIn("app#9: skip", out)
        self.assertIn("no store row", out)
        self.assertNotIn("new starts stop here", out)

    def test_a_project_ceiling_skips_and_the_pass_continues(self):
        store = stored(self.db, [("app", 2, "open"), ("app", 5, "open")])
        self.addCleanup(store.close)
        blue = self.write_policy("blue_policy", [("app", 2, "start")])
        red = self.write_policy("red_policy", [("app", 5, "start")])
        code, out, _ = self.run_pass(
            store,
            [self.project("blue", blue), self.project("red", red)],
            live=frozenset({("blue", "app", 1)}),
            limits=admission.Limits(
                total=4,
                projects={
                    "blue": {"floor": 0, "ceiling": 1},
                    "red": {"floor": 0, "ceiling": None},
                },
            ),
        )
        self.assertEqual(code, 0)
        self.assertIn("app#2: skip", out)
        self.assertIn("ceiling", out)
        self.assertIn("app#5: start", out)

    def test_a_quota_refusal_skips_one_candidate_only(self):
        store = stored(self.db, [("app", 2, "open"), ("app", 5, "open")])
        self.addCleanup(store.close)
        blue = self.write_policy("blue_policy", [("app", 2, "start")])
        red = self.write_policy("red_policy", [("app", 5, "start")])
        quotas = {
            "blue": drive.QuotaSource(chains={"worker": ["tired/model"]}),
            "red": drive.QuotaSource(chains={}),
        }
        code, out, _ = self.run_pass(
            store,
            [self.project("blue", blue), self.project("red", red)],
            quotas=quotas,
            ledger={
                "readings": {"tired": {"windows": {"hour": {"used_pct": 100}}, "read_at": 2000.0}},
                "marks": {},
            },
        )
        self.assertEqual(code, 0)
        self.assertIn("app#2: skip", out)
        self.assertIn("quota", out)
        self.assertIn("app#5: start", out)

    def test_the_non_plan_pass_claims_its_candidate(self):
        store = stored(self.db, [("app", 7, "open")])
        store.enqueue("app", 7, "status", "working", now=1500.0)
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        code, out, err = self.run_pass(store, [self.project("blue", entry)], plan=False)
        self.assertEqual(code, 0)
        self.assertIn("pass: 1 candidate(s)", out)
        self.assertIn("app#7: start", out)
        self.assertIn("launch skipped (no launcher)", out)
        row, _ = store.item("app", 7, now=2000.0)
        self.assertTrue(row.agent.startswith(drive.MANAGED_PREFIX))
        self.assertEqual(len(store.outbox_rows()), 2)

    def test_host_defaults_resolve_without_touching_the_real_dirs(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        kept = {k: v for k, v in os.environ.items() if not k.startswith("CORRAL_")}
        env = dict(
            kept,
            CORRAL_CONFIG=os.path.join(self.tmp, "config"),
            CORRAL_STATE=os.path.join(self.tmp, "state"),
        )
        with mock.patch.dict(os.environ, env, clear=True):
            out, err = io.StringIO(), io.StringIO()
            code = drive.run_pass(
                plan=True,
                store=store,
                lock_path=self.lock,
                out=out,
                err=err,
                now=2000.0,
                projects_fn=lambda: [self.project("blue", entry)],
                live=frozenset(),
            )
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("app#7: start", out.getvalue())


if __name__ == "__main__":
    unittest.main()
