"""The resolver guard: holder ids never key registry maps directly. Fakes only.

A claim holder is an opaque id this loop minted
(`drive-<hash8>-<n>`); registry entries are keyed by unit name. Every
reader resolves the holder to its entry through `entry_for_claim` (by
ticket), never by keying a registry map with the holder id. This test
parses `drive.py` and `drivehub.py` and fails when a `.get(`, `in` or
`[...]` keys on `claim.agent`, `holder` or `agent` outside the
allow-list below. Additions need the one resolver or an explicit
allow-list entry with a comment.

The same files must never compare anything with a claim's
`(repo, number)` outside the resolver either: registry tickets carry
the short `[[repo]]` name while claims carry the tracker repo, so a
raw `ticket != (claim.repo, claim.number)` can never match and
mistakes every finished worker for a crash. Every `==`/`!=` against a
`(claim.repo, claim.number)` tuple outside `entry_for_claim` fails,
whatever the other operand is called: a registry ticket held in a
differently named variable is still a ticket.

The same files must never compare a claim holder id with a registry
entry id outside the resolver either: holders are opaque
`drive-<hash8>-<n>` ids while registry entries are keyed by unit name,
so a raw `prior[0] == agent` can never match in production and hides
behind same-namespace fixtures in tests. Every `==`/`!=` between a
holder-shaped value and a `[0]`-subscripted entry id outside the
allow-list below fails. Additions need the one resolver or an explicit
allow-list entry with a comment.
"""

import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "korapy"
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

#: (file, enclosing function) pairs allowed to compare a holder id
#: against a `[0]`-subscripted entry id. `reviewed_claims` reads
#: `finished` from `resolve_finished`, whose values are holder ids
#: from this pass's capture, so `newest[0] == agent` there is
#: holder-vs-holder, never holder-vs-registry. `gate` reads the
#: finished agent id through `_finished_agent`, so no raw comparison
#: remains there. `resolve_finished` keeps the raw `prior[0] == agent`
#: fast path only for same-namespace fixtures with no ticket index
#: (production always passes one and resolves through
#: `entry_for_claim`, comparing registry-vs-registry as
#: `entry[0] == prior[0]`).
HOLDER_COMPARE_ALLOW = {
    ("drive.py", "reviewed_claims"),
    ("drive.py", "resolve_finished"),
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


#: The claim attributes that make a store key. Any other pair
#: (`(claim.agent, claim.epoch)`) is an identity tuple, not a ticket key.
CLAIM_KEY_ATTRS = {"repo", "number"}


def _is_claim_key(node):
    """Whether `node` is a `(claim.repo, claim.number)`-shaped tuple.

    Only those two attributes, in either order: a tuple of any other
    two claim attributes is a different fact and must not trip the
    ticket guard.
    """
    if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
        return False
    if not all(
        isinstance(elt, ast.Attribute)
        and isinstance(elt.value, ast.Name)
        and elt.value.id == "claim"
        for elt in node.elts
    ):
        return False
    return {elt.attr for elt in node.elts} == CLAIM_KEY_ATTRS


def _ticket_comparisons_in(tree):
    """`(function, lineno)` for every `==`/`!=` against a claim key.

    The other operand's source text never matters: a registry ticket
    held in a variable named anything at all still cannot match a
    claim's tracker-repo key, so any equality against the tuple is
    suspect outside `entry_for_claim`.
    """
    enclosing = _enclosing_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        parts = [node.left, *node.comparators]
        if any(_is_claim_key(part) for part in parts):
            found.append((enclosing[node], node.lineno))
    return found


def _ticket_comparisons(path):
    return _ticket_comparisons_in(ast.parse(path.read_text(encoding="utf-8")))


#: Holder-id spellings that pair with a `[0]`-subscripted entry id in a
#: raw comparison: the claim holder (`claim.agent`, `holder`,
#: `holder_now`, `current.agent`), the bare loop variable (`agent`) and
#: the reviewer author (`author`, itself a holder id).
_HOLDER_ID_TEXTS = HOLDER_KEYS | {"holder_now", "author", "current.agent"}


def _is_holder_id(node):
    """Whether `node` names a claim holder id."""
    text = ast.unparse(node).replace(" ", "")
    return text in _HOLDER_ID_TEXTS or text.endswith(".agent")


def _is_registry_id(node):
    """Whether `node` is a `[0]`-subscripted entry id (`prior[0]`, ...)."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 0
    )


def _holder_comparisons_in(tree):
    """`(function, lineno)` for every holder-vs-registry-id `==`/`!=`."""
    enclosing = _enclosing_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        parts = [node.left, *node.comparators]
        if any(_is_holder_id(part) for part in parts) and any(
            _is_registry_id(part) for part in parts
        ):
            found.append((enclosing[node], node.lineno))
    return found


def _holder_comparisons(path):
    return _holder_comparisons_in(ast.parse(path.read_text(encoding="utf-8")))


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

    def test_a_differently_named_ticket_still_fails_the_guard(self):
        tree = ast.parse(
            "def somewhere_else(held, claim):\n    return held == (claim.repo, claim.number)\n"
        )
        self.assertEqual(_ticket_comparisons_in(tree), [("somewhere_else", 2)])

    def test_a_claim_key_on_the_left_still_fails_the_guard(self):
        tree = ast.parse(
            "def somewhere_else(held, claim):\n    return (claim.repo, claim.number) != held\n"
        )
        self.assertEqual(_ticket_comparisons_in(tree), [("somewhere_else", 2)])

    def test_another_pair_of_claim_attributes_is_not_a_ticket_key(self):
        tree = ast.parse(
            "def somewhere_else(claim):\n    return (claim.agent, claim.epoch) == prior\n"
        )
        self.assertEqual(_ticket_comparisons_in(tree), [])

    def test_the_resolver_comparison_itself_passes(self):
        tree = ast.parse(
            "def entry_for_claim(ticket, claim):\n    return ticket != (claim.repo, claim.number)\n"
        )
        self.assertEqual(_ticket_comparisons_in(tree), [("entry_for_claim", 2)])

    def test_a_holder_id_comparison_is_not_a_ticket_comparison(self):
        tree = ast.parse("def somewhere_else(prior, agent):\n    return prior[0] == agent\n")
        self.assertEqual(_ticket_comparisons_in(tree), [])

    def test_claim_holder_ids_never_compare_against_registry_ids_directly(self):
        problems = []
        for name in FILES:
            for func, lineno in _holder_comparisons(ROOT / name):
                if (name, func) not in HOLDER_COMPARE_ALLOW:
                    problems.append(f"{name}:{lineno} {func} holder ==/!= registry[0]")
        self.assertEqual(problems, [], f"raw holder-id comparisons: {problems}")

    def test_the_holder_compare_allow_list_names_real_uses(self):
        seen = set()
        for name in FILES:
            for func, _ in _holder_comparisons(ROOT / name):
                seen.add((name, func))
        for entry in HOLDER_COMPARE_ALLOW:
            self.assertIn(entry, seen, f"stale holder-compare allow-list entry: {entry}")

    def test_a_second_holder_comparison_outside_the_resolver_fails(self):
        tree = ast.parse("def somewhere_else(prior, agent):\n    return prior[0] == agent\n")
        self.assertEqual(_holder_comparisons_in(tree), [("somewhere_else", 2)])

    def test_a_holder_inequality_outside_the_resolver_fails(self):
        tree = ast.parse("def somewhere_else(prior, holder):\n    return prior[0] != holder\n")
        self.assertEqual(_holder_comparisons_in(tree), [("somewhere_else", 2)])

    def test_a_registry_against_registry_comparison_passes(self):
        tree = ast.parse("def resolve_finished(entry, prior):\n    return entry[0] == prior[0]\n")
        self.assertEqual(_holder_comparisons_in(tree), [])

    def test_a_holder_against_holder_comparison_passes(self):
        tree = ast.parse("def somewhere_else(current, agent):\n    return current.agent != agent\n")
        self.assertEqual(_holder_comparisons_in(tree), [])

    def test_a_role_comparison_is_not_a_holder_comparison(self):
        tree = ast.parse('def somewhere_else(newest):\n    return newest[1] == "reviewer"\n')
        self.assertEqual(_holder_comparisons_in(tree), [])


if __name__ == "__main__":
    unittest.main()
