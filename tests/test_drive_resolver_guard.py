"""The resolver guard: holder ids never key registry maps directly. Fakes only.

A claim holder is an opaque id this loop minted
(`drive-<hash8>-<n>`); registry entries are keyed by unit name. Every
reader resolves the holder to its entry through `entry_for_claim` (by
ticket), never by keying a registry map with the holder id. This test
parses `drive.py` and `drivehub.py` and fails when a `.get(`, `in` or
`[...]` keys on `claim.agent`, `holder` or `agent` outside the
allow-list below. Additions need the one resolver or an explicit
allow-list entry with a comment.

The same files must never compare a registry ticket with a claim's
`(repo, number)` outside the resolver either: registry tickets carry
the short `[[repo]]` name while claims carry the tracker repo, so a
raw `ticket != (claim.repo, claim.number)` can never match and
mistakes every finished worker for a crash. Every `==`/`!=` between a
ticket-shaped value and a `(claim.repo, claim.number)` tuple outside
`entry_for_claim` fails.
"""

import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "corral"
FILES = ["drive.py", "drivehub.py"]
HOLDER_KEYS = {"holder", "agent", "claim.agent"}

#: (file, enclosing function, kind, key) uses that are not the bug.
#: `entry_for_claim` falls back to the holder id only when no ticket
#: index is passed (production always passes it; tests use it for
#: same-namespace fixtures). `is_orphan` keeps the same-namespace
#: `present_agents` check as a legacy fallback; ticket presence via the
#: one resolver is primary.
ALLOW = {
    ("drive.py", "entry_for_claim", "get", "claim.agent"),
    ("drive.py", "is_orphan", "in", "claim.agent"),
}

#: (file, enclosing function) pairs allowed to compare a registry
#: ticket against a claim's `(repo, number)`: the one resolver only.
TICKET_COMPARE_ALLOW = {
    ("drive.py", "entry_for_claim"),
}


def _enclosing_names(tree):
    """Map every node to its enclosing function name."""
    names = {}

    def visit(node, func="<module>"):
        names[node] = func
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                visit(child, func)

    visit(tree)
    return names


def _raw_uses(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    enclosing = _enclosing_names(tree)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            key = ast.unparse(node.args[0]).replace(" ", "")
            if key in HOLDER_KEYS:
                found.append((enclosing[node], "get", key, node.lineno))
        elif isinstance(node, ast.Subscript):
            key = ast.unparse(node.slice).replace(" ", "")
            if key in HOLDER_KEYS:
                found.append((enclosing[node], "subscript", key, node.lineno))
        elif isinstance(node, ast.Compare):
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    left = ast.unparse(node.left).replace(" ", "")
                    right = ast.unparse(comp).replace(" ", "")
                    if left in HOLDER_KEYS or right in HOLDER_KEYS:
                        key = left if left in HOLDER_KEYS else right
                        found.append((enclosing[node], "in", key, node.lineno))
    return found


def _is_claim_key(node):
    """Whether `node` is a `(claim.repo, claim.number)`-shaped tuple."""
    return (
        isinstance(node, ast.Tuple)
        and len(node.elts) == 2
        and all(
            isinstance(elt, ast.Attribute)
            and isinstance(elt.value, ast.Name)
            and elt.value.id == "claim"
            for elt in node.elts
        )
    )


def _mentions_ticket(node):
    """Whether `node` names a registry ticket value."""
    return "ticket" in ast.unparse(node).replace(" ", "").lower()


def _ticket_comparisons_in(tree):
    """`(function, lineno)` for every ticket-vs-claim-key `==`/`!=`."""
    enclosing = _enclosing_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        parts = [node.left, *node.comparators]
        if any(_is_claim_key(part) for part in parts) and any(
            _mentions_ticket(part) for part in parts if not _is_claim_key(part)
        ):
            found.append((enclosing[node], node.lineno))
    return found


def _ticket_comparisons(path):
    return _ticket_comparisons_in(ast.parse(path.read_text(encoding="utf-8")))


class ResolverGuardTest(unittest.TestCase):
    def test_holder_ids_never_key_registry_maps_directly(self):
        problems = []
        for name in FILES:
            for func, kind, key, lineno in _raw_uses(ROOT / name):
                if (name, func, kind, key) not in ALLOW:
                    problems.append(f"{name}:{lineno} {func} {kind} {key}")
        self.assertEqual(problems, [], f"raw holder-keyed lookups: {problems}")

    def test_the_allow_list_names_real_uses(self):
        seen = set()
        for name in FILES:
            for func, kind, key, _ in _raw_uses(ROOT / name):
                seen.add((name, func, kind, key))
        for entry in ALLOW:
            self.assertIn(entry, seen, f"stale allow-list entry: {entry}")

    def test_registry_tickets_never_compare_against_claim_keys_directly(self):
        problems = []
        for name in FILES:
            for func, lineno in _ticket_comparisons(ROOT / name):
                if (name, func) not in TICKET_COMPARE_ALLOW:
                    problems.append(
                        f"{name}:{lineno} {func} ticket ==/!= (claim.repo, claim.number)"
                    )
        self.assertEqual(problems, [], f"raw ticket-keyed comparisons: {problems}")

    def test_the_resolver_still_compares_tickets_to_claim_keys(self):
        seen = set()
        for name in FILES:
            for func, _ in _ticket_comparisons(ROOT / name):
                seen.add((name, func))
        for entry in TICKET_COMPARE_ALLOW:
            self.assertIn(entry, seen, f"stale ticket-compare allow-list entry: {entry}")

    def test_a_second_ticket_comparison_outside_the_resolver_fails(self):
        tree = ast.parse(
            "def somewhere_else(ticket, claim):\n    return ticket != (claim.repo, claim.number)\n"
        )
        self.assertEqual(_ticket_comparisons_in(tree), [("somewhere_else", 2)])

    def test_the_resolver_comparison_itself_passes(self):
        tree = ast.parse(
            "def entry_for_claim(ticket, claim):\n    return ticket != (claim.repo, claim.number)\n"
        )
        self.assertEqual(_ticket_comparisons_in(tree), [("entry_for_claim", 2)])

    def test_a_holder_id_comparison_is_not_a_ticket_comparison(self):
        tree = ast.parse("def somewhere_else(prior, agent):\n    return prior[0] == agent\n")
        self.assertEqual(_ticket_comparisons_in(tree), [])


if __name__ == "__main__":
    unittest.main()
