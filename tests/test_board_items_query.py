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
import re
import tempfile
import unittest

from korapy import trackers
from korapy.github import board as boardmod
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
    """Every module-scope ``(name, lineno, is_plain_import)`` binding of one module.

    One generic walk visits every node evaluated where the module body
    runs and records each name that binds there: stored names
    (assignment, ``for`` and ``with ... as`` targets), walrus targets
    (a walrus binds its enclosing scope, even in a comprehension, a
    decorator or a default), ``except ... as`` names, ``match``
    captures, import aliases, and each ``def`` or ``class`` name.
    Function, lambda and class bodies open a new scope, so only the
    names around them are visited: decorators, defaults, annotations,
    bases and keywords. Comprehension loop targets stay out for the
    same reason. An augmented assignment never binds on its own, so
    ``X += 1`` after ``X = 1`` is not a second binding.
    """
    found = []

    def bind(name, lineno, plain=False):
        found.append((name, lineno, plain))

    def visit(node, in_comprehension=False):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store) and not in_comprehension:
                bind(node.id, node.lineno)
            return
        if isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                bind(node.target.id, node.lineno)
            visit(node.value, in_comprehension)
            return
        if isinstance(node, ast.ExceptHandler):
            if node.name:
                bind(node.name, node.lineno)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name:
                bind(node.name, node.lineno)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                bind(node.rest, node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bind(alias.asname or alias.name.split(".")[0], node.lineno, plain=True)
            return
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    bind(alias.asname or alias.name, node.lineno)
            return
        elif isinstance(node, ast.AugAssign):
            visit(node.value, in_comprehension)
            return
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bind(node.name, node.lineno)
            for decorator in node.decorator_list:
                visit(decorator, in_comprehension)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    visit(default, in_comprehension)
            params = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if node.args.vararg is not None:
                params += (node.args.vararg,)
            if node.args.kwarg is not None:
                params += (node.args.kwarg,)
            for param in params:
                if param.annotation is not None:
                    visit(param.annotation, in_comprehension)
            if node.returns is not None:
                visit(node.returns, in_comprehension)
            return
        elif isinstance(node, ast.ClassDef):
            bind(node.name, node.lineno)
            for decorator in node.decorator_list:
                visit(decorator, in_comprehension)
            for base in node.bases:
                visit(base, in_comprehension)
            for keyword in node.keywords:
                visit(keyword.value, in_comprehension)
            return
        elif isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    visit(default, in_comprehension)
            return
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for child in ast.iter_child_nodes(node):
                visit(child, True)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_comprehension)

    for node in tree.body:
        visit(node)
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


def describe_offenders(offenders):
    """One line per duplicated binding: the file, the name, every line."""
    return [
        f"{path}: {name} bound at lines {', '.join(map(str, lines))}"
        for path, names in sorted(offenders.items())
        for name, lines in sorted(names.items())
    ]


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
        self.assertEqual(describe_offenders(offenders), [])

    def test_failure_message_names_file_name_and_lines(self):
        self.assertEqual(
            describe_offenders({"src/korapy/github/board.py": {"_ITEMS": [12, 40]}}),
            ["src/korapy/github/board.py: _ITEMS bound at lines 12, 40"],
        )


class WrappedBindingGuardTest(unittest.TestCase):
    """Duplicates hidden inside a compound statement still bind at module scope.

    ``if``, ``try``, ``for``, ``with`` and ``match`` suites run in the
    module scope, as do guards, decorators, defaults, bases and bare
    expressions, so a second binding there replaces the first at import
    time exactly like a second direct statement. Function, lambda and
    class bodies open a new scope and stay out of the walk, as do
    comprehension targets (a walrus inside one still binds outside).
    Each new bypass gets a CASES entry, never a new branch: the walk is
    generic, so one entry is the whole proof.
    """

    CASES = [
        ("if", "if cond:\n    _DUP = 1\nelse:\n    _DUP = 2\n"),
        ("try", "try:\n    _DUP = 1\nexcept OSError:\n    _DUP = 2\n"),
        ("finally", "try:\n    pass\nfinally:\n    _DUP = 1\n_DUP = 2\n"),
        ("for body", "for item in items:\n    _DUP = item\n_DUP = None\n"),
        ("for target", "for _DUP in items:\n    pass\n_DUP = None\n"),
        ("with body", 'with open(path) as handle:\n    _DUP = handle.read()\n_DUP = ""\n'),
        ("with target", 'with open(path) as _DUP:\n    pass\n_DUP = ""\n'),
        (
            "match body",
            'match cmd:\n    case "run":\n        _DUP = 1\n    case _:\n        _DUP = 2\n',
        ),
        (
            "match capture",
            "match cmd:\n    case [first, *_DUP]:\n        pass\n_DUP = []\n",
        ),
        (
            "nested",
            "if ready:\n    for item in items:\n        _DUP = item\nelse:\n    _DUP = None\n",
        ),
        ("while", "while cond:\n    _DUP = next(source)\n_DUP = None\n"),
        ("except as", "try:\n    pass\nexcept OSError as _DUP:\n    pass\n_DUP = None\n"),
        ("walrus in condition", "if (_DUP := load()):\n    pass\n_DUP = None\n"),
        ("walrus in iterable", "for item in (_DUP := load()):\n    pass\n_DUP = None\n"),
        (
            "match-case guard",
            "match cmd:\n    case _ if (_DUP := check()):\n        pass\n_DUP = None\n",
        ),
        ("bare walrus", "(_DUP := load())\n_DUP = None\n"),
        ("assert walrus", "assert (_DUP := check())\n_DUP = None\n"),
        ("augmented value", "_TOTAL = 0\n_TOTAL += (_DUP := 1)\n_DUP = 2\n"),
        ("decorator", "@(_DUP := deco)\ndef handle():\n    pass\n_DUP = None\n"),
        ("default", "def handle(arg=(_DUP := load())):\n    pass\n_DUP = None\n"),
        ("class base", "class Thing((_DUP := Base)):\n    pass\n_DUP = None\n"),
        (
            "walrus in comprehension",
            "[(_DUP := render(item)) for item in items]\n_DUP = None\n",
        ),
    ]

    CLEAN_CASES = [
        ("except as bound once", "try:\n    pass\nexcept OSError as _DUP:\n    print(_DUP)\n"),
        ("walrus bound once", "if (_DUP := load()):\n    print(_DUP)\n"),
        ("augassign is not a rebinding", "_TOTAL = 0\nif cond:\n    _TOTAL += 2\n"),
        ("comprehension target stays out", "[_DUP for _DUP in items]\n"),
        (
            "lambda default walrus bound once",
            "apply(lambda arg=(_DUP := load()): arg)\n",
        ),
    ]

    def test_each_wrapper_reports_both_bindings(self):
        for label, source in self.CASES:
            with self.subTest(wrapper=label):
                names = [name for name, _, _ in top_level_bindings(ast.parse(source))]
                self.assertEqual(names.count("_DUP"), 2)

    def test_clean_cases_bind_no_name_twice(self):
        with tempfile.TemporaryDirectory(prefix="korapy-guard-clean-") as directory:
            for index, (label, source) in enumerate(self.CLEAN_CASES):
                with self.subTest(case=label):
                    path = pathlib.Path(directory) / f"clean_{index}.py"
                    path.write_text(source, encoding="utf-8")
                    self.assertEqual(shadowed_names(path), {})

    def test_wrapped_duplicate_is_shadowed(self):
        with tempfile.TemporaryDirectory(prefix="korapy-guard-") as directory:
            path = pathlib.Path(directory) / "wrapped.py"
            path.write_text("if cond:\n    _DUP = 1\nelse:\n    _DUP = 2\n", encoding="utf-8")
            self.assertEqual(shadowed_names(path), {"_DUP": [2, 4]})

    def test_function_and_class_bodies_stay_out(self):
        source = (
            "def first():\n    _DUP = 1\n\n"
            "def second():\n    _DUP = 2\n\n"
            "class Thing:\n    _DUP = 3\n"
            "_DUP = 4\n"
        )
        names = [name for name, _, _ in top_level_bindings(ast.parse(source))]
        self.assertEqual(names.count("_DUP"), 1)

    def test_wrapped_def_records_only_its_name(self):
        source = "if cond:\n    def _DUP():\n        _DUP = 1\n    _DUP = 2\n"
        names = [name for name, _, _ in top_level_bindings(ast.parse(source))]
        self.assertEqual(names.count("_DUP"), 2)


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


class ReadKindsAgreementTest(unittest.TestCase):
    def test_value_fragments_match_the_read_typenames(self):
        pattern = re.compile(r"\.\.\. on (ProjectV2ItemField\w+)")
        for query in (boardmod._ITEMS, trackers._ITEM):
            with self.subTest(query=query.splitlines()[0]):
                self.assertEqual(set(pattern.findall(query)), set(boardmod._READ_TYPENAMES))


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
