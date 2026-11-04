"""The resolver guard: holder ids never key registry maps directly. Fakes only.

A claim holder is an opaque id this loop minted
(`drive-<hash8>-<n>`); registry entries are keyed by unit name. Every
reader resolves the holder to its entry through `entry_for_claim` (by
ticket), never by keying a registry map with the holder id. This test
parses `drive.py` and `drivehub.py` and fails when a `.get(`, `in` or
`[...]` keys on `claim.agent`, `holder` or `agent` outside the
allow-list below. Additions need the one resolver or an explicit
allow-list entry with a comment.
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


if __name__ == "__main__":
    unittest.main()
