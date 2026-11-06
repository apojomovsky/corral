"""Board item queries: the reader and the provisioner send their own.

The reader's poll query and the provisioner's membership query once
shared the module-level name ``_ITEMS`` in ``korapy.github.board``:
the second assignment silently replaced the first at import time, so
every board read sent the membership query and the strict parser
rejected its nodes. The unit fakes route on a query substring, so
none of them noticed. These tests pin each caller's query text, and
the module-binding guard below fails on any second top-level binding
of one name, so the class cannot come back.

Canned payloads only: ``BoardFakeRun`` and ``ProvisionFakeRun``
answer GraphQL pages and REST routes at the injected ``run`` seam,
and ``FakePacer`` stands in for the shared bucket. No test reaches a
real ``gh``, the network, or a real board.
"""

import ast
import json
import pathlib
import unittest

from korapy.github.board import GithubBoardProvisioner
from tests.test_board_fields import FakePacer
from tests.test_board_populate import (
    ProvisionFakeRun,
    issue as open_issue,
    items_page as provision_items_page,
)
from tests.test_board_reader import (
    BoardFakeRun,
    full_schema_fields,
    item_node,
    items_payload,
    reader_for,
    rest_issue,
    schema_payload,
    select_value,
)
from tests.test_tracker import item_payload, pull_payload

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "korapy"


def top_level_bindings(tree):
    """Every top-level ``(name, lineno, is_plain_import)`` binding of one module."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node.lineno, False))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                found.extend(
                    (name.id, node.lineno, False)
                    for name in ast.walk(target)
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
                )
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                found.append((node.target.id, node.lineno, False))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                found.append((bound, node.lineno, True))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    found.append((alias.asname or alias.name, node.lineno, False))
    return found


def shadowed_names(path):
    """Top-level names bound twice in one module, minus harmless re-imports.

    A repeated plain ``import`` of one root package (``import a.b``
    beside ``import a.c``) binds the same module object and never
    shadows, so those stay allowed. Anything else binding one name
    twice replaces the first binding at import time.
    """
    seen = {}
    for name, lineno, is_plain_import in top_level_bindings(
        ast.parse(path.read_bytes(), filename=str(path))
    ):
        seen.setdefault(name, []).append((lineno, is_plain_import))
    return {
        name: [lineno for lineno, _ in entries]
        for name, entries in seen.items()
        if len(entries) > 1 and not all(plain for _, plain in entries)
    }


def graphql_queries(run):
    """Every GraphQL query string a fake run received, in call order."""
    found = []
    for args, _ in run.calls:
        if len(args) < 2 or args[1] != "graphql":
            continue
        toks = args[2:]
        i = 0
        while i < len(toks):
            if toks[i] in ("-f", "-F") and i + 1 < len(toks):
                key, _, value = toks[i + 1].partition("=")
                if key == "query":
                    found.append(value)
                i += 2
            else:
                i += 1
    return found


def items_queries(run):
    """The board-items queries a fake run received, in call order."""
    return [query for query in graphql_queries(run) if "items(first:" in query]


class NoShadowedModuleNamesTest(unittest.TestCase):
    def test_no_module_binds_one_top_level_name_twice(self):
        offenders = {}
        for path in sorted(PACKAGE.rglob("*.py")):
            shadowed = shadowed_names(path)
            if shadowed:
                offenders[str(path.relative_to(PACKAGE.parents[0]))] = shadowed
        self.assertEqual(offenders, {})


class ReaderQueryIdentityTest(unittest.TestCase):
    def test_page_sends_the_reader_query(self):
        run = BoardFakeRun()
        rest_issue(run, 7)
        run.script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        run.script("BoardItems", [items_payload([item_node(7)])])
        reader = reader_for(run)
        found, _ = reader.poll(reader.schema(), max_pages=10)
        self.assertEqual([item.number for item in found], [7])
        sent = items_queries(run)
        self.assertTrue(sent)
        for query in sent:
            with self.subTest(query=query.splitlines()[0]):
                self.assertIn("BoardItems", query)
                self.assertNotIn("ProvisionBoardItems", query)
                self.assertIn("updatedAt", query)
                self.assertIn("fieldValues", query)

    def test_issue_refs_sends_the_reader_query(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        run.script("BoardItems", [items_payload([item_node(7)])])
        found = reader_for(run).issue_refs()
        self.assertEqual(found, {("example/demo", 7)})
        sent = items_queries(run)
        self.assertTrue(sent)
        for query in sent:
            self.assertIn("updatedAt", query)
            self.assertIn("fieldValues", query)


class ProvisionerQueryIdentityTest(unittest.TestCase):
    def test_membership_read_sends_the_provisioner_query(self):
        run = ProvisionFakeRun()
        run.script(
            "ProvisionBoardItems",
            [
                provision_items_page(
                    {"id": "ITEM_1", "content": {"__typename": "Issue", "id": "I_1"}}
                )
            ],
        )
        run.add_rest("repos/example/demo/issues?state=open&per_page=100", [open_issue(1)])
        report = GithubBoardProvisioner(run, pacer=FakePacer()).populate("PVT_1", "example/demo")
        self.assertEqual(report.kept, ("example/demo#1",))
        self.assertEqual(report.added, ())
        sent = [query for query in graphql_queries(run) if "ProvisionBoardItems" in query]
        self.assertTrue(sent)
        for query in sent:
            with self.subTest(query=query.splitlines()[0]):
                self.assertNotIn("updatedAt", query)
                self.assertNotIn("fieldValues", query)


class RealisticBoardReadTest(unittest.TestCase):
    """A few-hundred-card board poll completes: issues, a pull request
    card, a draft, and a card whose fields are unset."""

    PR_NUMBER = 42
    UNSET_NUMBER = 43
    TOTAL = 250

    def rest_for(self, path):
        number = int(path.rsplit("/", 1)[-1])
        if "/pulls/" in path:
            return json.dumps(pull_payload(number, fork=True))
        payload = item_payload(number)
        if number == self.PR_NUMBER:
            payload = item_payload(number, pull_request={})
        return json.dumps(payload)

    def board_nodes(self):
        nodes = []
        for number in range(1, self.TOTAL + 1):
            if number == self.PR_NUMBER:
                nodes.append(item_node(number, typename="PullRequest"))
            elif number == self.UNSET_NUMBER:
                nodes.append(
                    item_node(
                        number,
                        values=(
                            {
                                "__typename": "ProjectV2ItemFieldTextValue",
                                "text": None,
                                "field": {
                                    "__typename": "ProjectV2Field",
                                    "id": "F_agent",
                                    "name": "Agent",
                                },
                            },
                            {
                                "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                "field": {
                                    "__typename": "ProjectV2SingleSelectField",
                                    "id": "F_prio",
                                    "name": "Priority",
                                },
                            },
                            select_value("F_wf", "Status", "opt-s1", "In progress"),
                        ),
                    )
                )
            else:
                nodes.append(item_node(number))
        nodes.append(item_node(0, typename="DraftIssue"))
        return nodes

    def test_full_board_poll_reads_every_card(self):
        run = BoardFakeRun()
        run.rest_fallback = self.rest_for
        run.script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        nodes = self.board_nodes()
        run.script(
            "BoardItems",
            [
                items_payload(nodes[:100], end="C1", more=True),
                items_payload(nodes[100:200], end="C2", more=True),
                items_payload(nodes[200:]),
            ],
        )
        reader = reader_for(run, project_owner="example", project_number=3)
        found, cursor = reader.poll(reader.schema(), max_pages=10)
        self.assertIsNone(cursor)
        self.assertEqual(len(found), self.TOTAL)
        by_number = {item.number: item for item in found}
        self.assertEqual(by_number[self.PR_NUMBER].kind, "pr")
        unset = by_number[self.UNSET_NUMBER]
        self.assertEqual(unset.agent, "")
        self.assertNotIn("priority", unset.extra)
        self.assertEqual(unset.extra["board_status"], "In progress")
        sent = items_queries(run)
        self.assertEqual(len(sent), 3)
        for query in sent:
            self.assertIn("updatedAt", query)
            self.assertIn("fieldValues", query)
            self.assertNotIn("ProvisionBoardItems", query)


if __name__ == "__main__":
    unittest.main()
