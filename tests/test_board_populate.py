"""Ticket 5: the adapter populate operation and its read-only preview.

Canned payloads only: ``ProvisionFakeRun`` answers GraphQL bodies and
REST routes at the injected ``run`` seam, and ``FakePacer`` stands in
for the shared bucket. No test reaches a real ``gh``, the network, or
a real board.
"""

import json
import unittest

from korapy import tracker as iface
from korapy.github import gh
from korapy.github.board import GithubBoardProvisioner
from tests.test_board_fields import FakePacer
from tests.test_board_provision import ProvisionFakeRun


def issue(number, nid=None, state="open", author="alice", pr=False):
    """One REST issue payload: open by default, PR-shaped when asked."""
    payload = {
        "number": number,
        "node_id": nid or f"I_{number}",
        "state": state,
        "author": {"login": author},
        "title": f"Issue {number}",
    }
    if pr:
        payload["pull_request"] = {"url": f"https://example.com/pull/{number}"}
    return payload


def item_node(item_id, content):
    """One board item node: content is an issue node id, None, or "pr"."""
    if content is None:
        content_payload = None
    elif content == "pr":
        content_payload = {"__typename": "PullRequest", "id": "PR_1"}
    else:
        content_payload = {"__typename": "Issue", "id": content}
    return {"id": item_id, "content": content_payload}


def items_page(*nodes, more=False):
    """A board-items connection payload with an explicit pageInfo."""
    return {
        "node": {
            "items": {
                "nodes": list(nodes),
                "pageInfo": {"hasNextPage": more, "endCursor": "CUR" if more else None},
            }
        }
    }


def items_route(run, *pages):
    """Script the paged board-items read."""
    return run.script("BoardItems", list(pages))


def issues_route(run, *pages):
    """Script the paged open-issues REST listing."""
    for index, page in enumerate(pages):
        path = "repos/example/demo/issues?state=open&per_page=100"
        if index:
            path += f"&page={index + 1}"
        if isinstance(page, Exception):
            run.add_rest(path, page)
        else:
            run.add_rest(path, list(page))
    return run


def add_route(run, answers):
    """Script the add-item mutation answers."""
    return run.script("AddItem", list(answers))


def added_answer(item_id="ITEM_new"):
    return {"addProjectV2ItemById": {"item": {"id": item_id}}}


def provisioner_for(run):
    """A provisioner on one fake run, for the tracker repo's board."""
    return GithubBoardProvisioner(run, pacer=FakePacer())


ISSUES = "repos/example/demo/issues?state=open&per_page=100"


class PopulateTests(unittest.TestCase):
    def test_missing_open_issues_are_added_by_node_id(self):
        run = ProvisionFakeRun()
        items_route(run, items_page(item_node("ITEM_1", "I_1")))
        issues_route(run, [issue(1), issue(2)])
        add_route(run, [added_answer("ITEM_2")])
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ("example/demo#2",))
        self.assertEqual(report.kept, ("example/demo#1",))
        self.assertEqual(report.refused, ())
        flags = run.flag_values("AddItem", "")
        self.assertIn("projectId=PVT_1", flags)
        self.assertIn("contentId=I_2", flags)
        self.assertNotIn("contentId=I_1", flags)

    def test_an_already_present_issue_is_kept_without_a_mutation(self):
        run = ProvisionFakeRun()
        items_route(run, items_page(item_node("ITEM_1", "I_1"), item_node("ITEM_2", "I_2")))
        issues_route(run, [issue(1), issue(2)])
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ())
        self.assertEqual(report.kept, ("example/demo#1", "example/demo#2"))
        self.assertEqual(run.mutations(), [])

    def test_a_closed_issue_and_a_pr_are_never_added(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(3, state="closed"), issue(4, pr=True), issue(5)])
        add_route(run, [added_answer("ITEM_5")])
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ("example/demo#5",))
        self.assertEqual(len(run.mutations()), 1)
        self.assertIn("contentId=I_5", run.flag_values("AddItem", ""))

    def test_an_external_authors_open_issue_is_added(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(6, author="someone-else")])
        add_route(run, [added_answer("ITEM_6")])
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ("example/demo#6",))

    def test_board_items_page_without_page_info_fails_closed(self):
        run = ProvisionFakeRun()
        run.script("BoardItems", [{"node": {"items": {"nodes": []}}}])
        issues_route(run, [issue(1)])
        with self.assertRaises(iface.TrackerError):
            provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(run.mutations(), [])

    def test_a_malformed_issues_page_fails_closed(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        run.add_rest(ISSUES, {"not": "a list"})
        with self.assertRaises(iface.TrackerError):
            provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(run.mutations(), [])

    def test_a_permission_denied_add_is_a_refused_line(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(2)])
        add_route(run, [gh.GhError("other", "Must have admin rights (HTTP 403)")])
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ())
        self.assertEqual(len(report.refused), 1)
        self.assertIn("example/demo#2", report.refused[0])

    def test_partial_failure_reports_per_issue_and_reruns_converge(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(2), issue(3)])
        add_route(
            run,
            [
                gh.GhError("other", "boom (HTTP 500)"),
                added_answer("ITEM_3"),
            ],
        )
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ("example/demo#3",))
        self.assertEqual(len(report.refused), 1)
        self.assertIn("example/demo#2", report.refused[0])
        rerun = ProvisionFakeRun()
        items_route(rerun, items_page(item_node("ITEM_3", "I_3")))
        issues_route(rerun, [issue(2), issue(3)])
        add_route(rerun, [added_answer("ITEM_2")])
        again = provisioner_for(rerun).populate("PVT_1", "example/demo")
        self.assertEqual(again.added, ("example/demo#2",))
        self.assertEqual(again.kept, ("example/demo#3",))
        self.assertEqual(again.refused, ())

    def test_a_temporary_move_conflict_is_retried(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(2)])
        add_route(
            run,
            [
                gh.GhError(
                    "other",
                    "UNPROCESSABLE: Your attempt to move this item created a"
                    " temporary conflict. Please try again.",
                ),
                added_answer("ITEM_2"),
            ],
        )
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ("example/demo#2",))
        self.assertEqual(report.refused, ())
        self.assertEqual(len([q for q in run.queries() if "AddItem" in q]), 2)

    def test_an_endless_conflict_is_refused_after_a_bound(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(2)])
        add_route(
            run,
            [gh.GhError("other", "temporary conflict, try again") for _ in range(10)],
        )
        report = provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ())
        self.assertEqual(len(report.refused), 1)
        self.assertIn("example/demo#2", report.refused[0])
        self.assertLessEqual(len([q for q in run.queries() if "AddItem" in q]), 5)

    def test_a_rate_limited_add_propagates(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(2)])
        body = {
            "data": {"addProjectV2ItemById": None},
            "errors": [{"type": "RATE_LIMITED", "message": "slow down"}],
        }
        add_route(run, [gh.GhError("other", "slow", output=json.dumps(body))])
        with self.assertRaises(iface.TrackerRateLimited):
            provisioner_for(run).populate("PVT_1", "example/demo")

    def test_a_rate_limited_listing_propagates(self):
        run = ProvisionFakeRun()
        run.script(
            "BoardItems",
            [gh.GhError("unavailable", "API rate limit exceeded for user ID 1. (HTTP 403)")],
        )
        with self.assertRaises(iface.TrackerRateLimited):
            provisioner_for(run).populate("PVT_1", "example/demo")
        self.assertEqual(run.mutations(), [])

    def test_an_add_answer_without_an_item_id_fails_closed(self):
        run = ProvisionFakeRun()
        items_route(run, items_page())
        issues_route(run, [issue(2)])
        add_route(run, [{"addProjectV2ItemById": {"item": {}}}])
        with self.assertRaises(iface.TrackerError):
            provisioner_for(run).populate("PVT_1", "example/demo")

    def test_an_empty_project_id_is_refused_before_any_read(self):
        run = ProvisionFakeRun()
        with self.assertRaises(ValueError):
            provisioner_for(run).populate("", "example/demo")
        self.assertEqual(run.calls, [])


class PreviewTests(unittest.TestCase):
    def test_preview_lists_would_add_without_mutating(self):
        run = ProvisionFakeRun()
        items_route(run, items_page(item_node("ITEM_1", "I_1")))
        issues_route(run, [issue(1), issue(2)])
        report = provisioner_for(run).preview_populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ("example/demo#2",))
        self.assertEqual(report.kept, ("example/demo#1",))
        self.assertEqual(report.refused, ())
        self.assertEqual(run.mutations(), [])

    def test_preview_reads_the_same_membership_as_populate(self):
        run = ProvisionFakeRun()
        items_route(run, items_page(), items_page(item_node("ITEM_2", "I_2")))
        issues_route(run, [issue(1), issue(2)])
        add_route(run, [added_answer("ITEM_2")])
        provisioner = provisioner_for(run)
        preview = provisioner.preview_populate("PVT_1", "example/demo")
        self.assertEqual(preview.added, ("example/demo#1", "example/demo#2"))
        report = provisioner.populate("PVT_1", "example/demo")
        self.assertEqual(report.added, ("example/demo#1",))
        self.assertEqual(report.kept, ("example/demo#2",))


if __name__ == "__main__":
    unittest.main()
