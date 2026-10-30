"""Project eligibility: the per-project hook naming startable tickets.

Tests use stub policy modules written to a temporary directory (loaded
by dotted name through an added `sys.path` entry, or by file path) and
fixture board items. No tracker, no network, no subprocess, no real
state or config dir: the registry dir is a temporary `CORRAL_CONFIG`.
"""

import dataclasses
import os
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

from corral import board, eligibility, projects


def item(repo="app", number=7, **fields):
    """A board item with sane defaults, overridden per test."""
    values: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "kind": "issue",
        "title": f"Title {number}",
        "body": "Body",
        "state": "open",
        "author": "operator",
        "labels": ["needs-review"],
        "priority": "high",
        "assignees": [],
        "url": f"https://example.com/{repo}/{number}",
        "tracker_updated_at": 1000.0,
        "extra": {},
    }
    values.update(fields)
    return board.Item(**values)


STUB_MODULE = """\
def eligible(rows, *, now):
    assert isinstance(now, float)
    return [
        make("app", 7, "start", weight=1.0),
        make("app", 9, "review", weight=3.0),
        make("api", 2, "start", weight=3.0),
    ]


def make(repo, number, kind, weight=0.0):
    from corral.eligibility import StartRequest

    return StartRequest(repo=repo, number=number, kind=kind, weight=weight)
"""


class TypesTest(unittest.TestCase):
    def test_row_from_item_copies_every_named_field(self):
        row = eligibility.row_from_item(
            item(agent="drive-host-1--app-7", status="working", external=True)
        )
        self.assertEqual(row.repo, "app")
        self.assertEqual(row.number, 7)
        self.assertEqual(row.title, "Title 7")
        self.assertEqual(row.labels, ("needs-review",))
        self.assertEqual(row.priority, "high")
        self.assertEqual(row.state, "open")
        self.assertEqual(row.agent, "drive-host-1--app-7")
        self.assertEqual(row.status, "working")
        self.assertTrue(row.external)
        self.assertEqual(row.pulls, ())

    def test_rows_are_frozen_plain_data(self):
        row = eligibility.row_from_item(item())
        self.assertTrue(dataclasses.is_dataclass(row))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.__setattr__("number", 8)

    def test_rows_from_items_keeps_each_item(self):
        rows = eligibility.rows_from_items([item(number=7), item(number=9)])
        self.assertEqual([row.number for row in rows], [7, 9])

    def test_request_kinds_are_the_four_dispatch_kinds(self):
        self.assertEqual(sorted(eligibility.KINDS), ["fix", "resume", "review", "start"])


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-elig-"))
        with open(os.path.join(self.tmp, "stub_policy.py"), "w", encoding="utf-8") as handle:
            handle.write(STUB_MODULE)
        self.path_entry = os.path.join(self.tmp, "stub_policy.py") + ":eligible"

    def test_dotted_entry_loads_the_named_function(self):
        with mock.patch.object(sys, "path", [self.tmp, *sys.path]):
            policy = eligibility.load_policy("stub_policy:eligible", project="blue")
        rows = eligibility.rows_from_items([item()])
        requests = policy.eligible(rows, now=2000.0)
        self.assertEqual(len(requests), 3)

    def test_path_entry_loads_from_the_file(self):
        policy = eligibility.load_policy(self.path_entry, project="blue")
        rows = eligibility.rows_from_items([item()])
        requests = policy.eligible(rows, now=2000.0)
        self.assertEqual(
            [(request.repo, request.number) for request in requests],
            [("api", 2), ("app", 9), ("app", 7)],
        )

    def test_bad_entries_name_the_project_and_entry(self):
        bad = [
            "no-colon-here",
            ":eligible",
            "stub_policy:",
            "missing_module_xyz:eligible",
            "stub_policy:missing_attr_xyz",
            os.path.join(self.tmp, "missing_xyz.py") + ":eligible",
        ]
        with mock.patch.object(sys, "path", [self.tmp, *sys.path]):
            for entry in bad:
                with self.subTest(entry=entry):
                    with self.assertRaises(eligibility.EligibilityError) as ctx:
                        eligibility.load_policy(entry, project="blue")
                    self.assertEqual(ctx.exception.exit_code, 4)
                    self.assertIn("blue", str(ctx.exception))
                    self.assertIn(entry, str(ctx.exception))

    def test_a_non_callable_entry_is_exit_four(self):
        module_path = os.path.join(self.tmp, "notfn.py")
        with open(module_path, "w", encoding="utf-8") as handle:
            handle.write("eligible = 42\n")
        with mock.patch.object(sys, "path", [self.tmp, *sys.path]):
            with self.assertRaises(eligibility.EligibilityError) as ctx:
                eligibility.load_policy("notfn:eligible", project="blue")
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("blue", str(ctx.exception))

    def test_a_policy_returning_a_bad_kind_names_the_project_and_entry(self):
        module_path = os.path.join(self.tmp, "badkind.py")
        with open(module_path, "w", encoding="utf-8") as handle:
            handle.write(
                "def eligible(rows, *, now):\n"
                "    from corral.eligibility import StartRequest\n"
                "    return [StartRequest(repo='app', number=1, kind='merge')]\n"
            )
        policy = eligibility.load_policy(module_path + ":eligible", project="blue")
        with self.assertRaises(eligibility.EligibilityError) as ctx:
            policy.eligible([], now=2000.0)
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("blue", str(ctx.exception))
        self.assertIn("badkind", str(ctx.exception))

    def test_a_policy_returning_plain_data_names_the_project_and_entry(self):
        module_path = os.path.join(self.tmp, "plaindata.py")
        with open(module_path, "w", encoding="utf-8") as handle:
            handle.write("def eligible(rows, *, now):\n    return [('app', 1)]\n")
        policy = eligibility.load_policy(module_path + ":eligible", project="blue")
        with self.assertRaises(eligibility.EligibilityError) as ctx:
            policy.eligible([], now=2000.0)
        self.assertIn("blue", str(ctx.exception))

    def test_relative_path_entry_resolves_against_base_not_cwd(self):
        ops_root = os.path.join(self.tmp, "ops")
        os.makedirs(os.path.join(ops_root, "policies"))
        with open(
            os.path.join(ops_root, "policies", "blue_policy.py"), "w", encoding="utf-8"
        ) as handle:
            handle.write("def eligible(rows, *, now):\n    return []\n")
        elsewhere = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-elig-cwd-"))
        old = os.getcwd()
        os.chdir(elsewhere)
        try:
            policy = eligibility.load_policy(
                "policies/blue_policy.py:eligible", project="blue", base=ops_root
            )
        finally:
            os.chdir(old)
        self.assertEqual(policy.eligible([], now=2000.0), [])

    def test_relative_path_entry_without_base_stays_relative_to_cwd(self):
        policy_dir = os.path.join(self.tmp, "relonly")
        os.makedirs(policy_dir)
        with open(os.path.join(policy_dir, "rel_policy.py"), "w", encoding="utf-8") as handle:
            handle.write("def eligible(rows, *, now):\n    return []\n")
        old = os.getcwd()
        os.chdir(policy_dir)
        try:
            policy = eligibility.load_policy("rel_policy.py:eligible", project="blue")
        finally:
            os.chdir(old)
        self.assertEqual(policy.eligible([], now=2000.0), [])

    def test_dotted_entry_with_attribute_path(self):
        module_path = os.path.join(self.tmp, "attrmod.py")
        with open(module_path, "w", encoding="utf-8") as handle:
            handle.write(
                "class Holder:\n"
                "    def eligible(self, rows, *, now):\n"
                "        return []\n"
                "holder = Holder()\n"
            )
        with mock.patch.object(sys, "path", [self.tmp, *sys.path]):
            policy = eligibility.load_policy("attrmod:holder.eligible", project="blue")
        self.assertEqual(policy.eligible([], now=2000.0), [])


class PureCallTest(unittest.TestCase):
    def test_requests_come_back_heaviest_first_then_repo_and_number(self):
        policy = eligibility.LoadedPolicy(
            project="blue",
            entry="stub:eligible",
            func=lambda rows, *, now: [
                eligibility.StartRequest(repo="app", number=7, kind="start", weight=1.0),
                eligibility.StartRequest(repo="app", number=9, kind="review", weight=3.0),
                eligibility.StartRequest(repo="api", number=2, kind="start", weight=3.0),
            ],
        )
        rows = eligibility.rows_from_items([item()])
        requests = policy.eligible(rows, now=2000.0)
        self.assertEqual(
            [(request.repo, request.number, request.kind) for request in requests],
            [("api", 2, "start"), ("app", 9, "review"), ("app", 7, "start")],
        )

    def test_the_policy_sees_only_rows_and_now(self):
        seen = {}

        def func(rows, *, now):
            seen["rows"] = rows
            seen["now"] = now
            return []

        policy = eligibility.LoadedPolicy(project="blue", entry="stub:eligible", func=func)
        rows = eligibility.rows_from_items([item(number=7)])
        self.assertEqual(policy.eligible(rows, now=2000.0), [])
        self.assertEqual(seen["now"], 2000.0)
        self.assertEqual([row.number for row in seen["rows"]], [7])

    def test_no_rows_means_no_requests(self):
        policy = eligibility.LoadedPolicy(
            project="blue", entry="stub:eligible", func=lambda rows, *, now: []
        )
        self.assertEqual(policy.eligible([], now=2000.0), [])

    def test_a_non_list_return_names_the_project_and_entry(self):
        for found in ({"app": 7}, "app:7", None):
            with self.subTest(found=found):
                policy = eligibility.LoadedPolicy(
                    project="blue",
                    entry="stub:eligible",
                    func=lambda rows, *, now, found=found: found,
                )
                with self.assertRaises(eligibility.EligibilityError) as ctx:
                    policy.eligible([], now=2000.0)
                self.assertEqual(ctx.exception.exit_code, 4)
                self.assertIn("blue", str(ctx.exception))
                self.assertIn("stub:eligible", str(ctx.exception))

    def test_a_bool_weight_is_not_a_number(self):
        policy = eligibility.LoadedPolicy(
            project="blue",
            entry="stub:eligible",
            func=lambda rows, *, now: [
                eligibility.StartRequest(repo="app", number=1, kind="start", weight=True)
            ],
        )
        with self.assertRaises(eligibility.EligibilityError) as ctx:
            policy.eligible([], now=2000.0)
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("blue", str(ctx.exception))

    def test_a_non_finite_weight_is_exit_four(self):
        for weight in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(weight=weight):
                policy = eligibility.LoadedPolicy(
                    project="blue",
                    entry="stub:eligible",
                    func=lambda rows, *, now, weight=weight: [
                        eligibility.StartRequest(repo="app", number=1, kind="start", weight=weight)
                    ],
                )
                with self.assertRaises(eligibility.EligibilityError) as ctx:
                    policy.eligible([], now=2000.0)
                self.assertEqual(ctx.exception.exit_code, 4)
                self.assertIn("blue", str(ctx.exception))

    def test_a_bad_repo_or_number_names_the_project_and_entry(self):
        bad = [
            eligibility.StartRequest(repo=42, number=1, kind="start"),  # ty: ignore[invalid-argument-type]
            eligibility.StartRequest(repo="app", number="1", kind="start"),  # ty: ignore[invalid-argument-type]
            eligibility.StartRequest(repo="app", number=True, kind="start"),
        ]
        for request in bad:
            with self.subTest(request=request):
                policy = eligibility.LoadedPolicy(
                    project="blue", entry="stub:eligible", func=lambda rows, *, now: [request]
                )
                with self.assertRaises(eligibility.EligibilityError) as ctx:
                    policy.eligible([], now=2000.0)
                self.assertEqual(ctx.exception.exit_code, 4)
                self.assertIn("blue", str(ctx.exception))
                self.assertIn("stub:eligible", str(ctx.exception))

    def test_a_policy_that_raises_eligibility_error_is_annotated(self):
        def func(rows, *, now):
            raise eligibility.EligibilityError("inner trouble")

        policy = eligibility.LoadedPolicy(project="blue", entry="stub:eligible", func=func)
        with self.assertRaises(eligibility.EligibilityError) as ctx:
            policy.eligible([], now=2000.0)
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("blue", str(ctx.exception))
        self.assertIn("stub:eligible", str(ctx.exception))


class ForProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-elig-"))
        with open(os.path.join(self.tmp, "stub_policy.py"), "w", encoding="utf-8") as handle:
            handle.write(STUB_MODULE)
        self.entry = os.path.join(self.tmp, "stub_policy.py") + ":eligible"

    def test_a_project_without_the_entry_is_skipped_with_a_line(self):
        project = projects.Project(name="blue", unit_prefix="blue-agent", root="/srv/ops/blue")
        outcome = eligibility.for_project(project, [item()], now=2000.0)
        self.assertEqual(outcome.requests, [])
        assert outcome.skipped is not None
        self.assertIn("blue", outcome.skipped)
        self.assertIn("skipped", outcome.skipped)

    def test_a_project_with_the_entry_returns_its_ordered_requests(self):
        project = projects.Project(
            name="blue",
            unit_prefix="blue-agent",
            root="/srv/ops/blue",
            eligibility=self.entry,
        )
        outcome = eligibility.for_project(project, [item()], now=2000.0)
        self.assertIsNone(outcome.skipped)
        self.assertEqual(
            [(request.repo, request.number) for request in outcome.requests],
            [("api", 2), ("app", 9), ("app", 7)],
        )

    def test_a_bad_entry_is_exit_four_naming_project_and_entry(self):
        project = projects.Project(
            name="blue",
            unit_prefix="blue-agent",
            root="/srv/ops/blue",
            eligibility="missing_module_xyz:eligible",
        )
        with self.assertRaises(eligibility.EligibilityError) as ctx:
            eligibility.for_project(project, [item()], now=2000.0)
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("blue", str(ctx.exception))
        self.assertIn("missing_module_xyz:eligible", str(ctx.exception))

    def test_a_relative_entry_resolves_against_the_project_root(self):
        ops_root = os.path.join(self.tmp, "ops")
        os.makedirs(os.path.join(ops_root, "policies"))
        with open(
            os.path.join(ops_root, "policies", "blue_policy.py"), "w", encoding="utf-8"
        ) as handle:
            handle.write("def eligible(rows, *, now):\n    return []\n")
        project = projects.Project(
            name="blue",
            unit_prefix="blue-agent",
            root=ops_root,
            eligibility="policies/blue_policy.py:eligible",
        )
        elsewhere = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-elig-cwd-"))
        old = os.getcwd()
        os.chdir(elsewhere)
        try:
            outcome = eligibility.for_project(project, [item()], now=2000.0)
        finally:
            os.chdir(old)
        self.assertIsNone(outcome.skipped)
        self.assertEqual(outcome.requests, [])


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-elig-reg-"))
        kept = {k: v for k, v in os.environ.items() if not k.startswith("CORRAL_")}
        env = dict(kept, CORRAL_CONFIG=os.path.join(self.tmp, "config"))
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))

    def write_project(self, name, text):
        directory = projects.projects_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{name}.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_the_entry_loads_with_the_registration(self):
        self.write_project(
            "blue",
            'name = "blue"\nunit_prefix = "blue-agent"\nroot = "/srv/ops/blue"\n'
            'eligibility = "blue_policy:eligible"\n',
        )
        (found,) = projects.load_all()
        self.assertEqual(found.eligibility, "blue_policy:eligible")

    def test_the_entry_defaults_to_none(self):
        self.write_project(
            "blue",
            'name = "blue"\nunit_prefix = "blue-agent"\nroot = "/srv/ops/blue"\n',
        )
        (found,) = projects.load_all()
        self.assertIsNone(found.eligibility)

    def test_a_bad_entry_names_its_file(self):
        for raw in ('eligibility = ""\n', "eligibility = 42\n", 'eligibility = "no-colon"\n'):
            with self.subTest(raw=raw):
                path = self.write_project(
                    "blue",
                    'name = "blue"\nunit_prefix = "blue-agent"\nroot = "/srv/ops/blue"\n' + raw,
                )
                with self.assertRaises(projects.ProjectsError) as ctx:
                    projects.load_all()
                self.assertIn(path, str(ctx.exception))
                os.remove(path)

    def test_check_reports_an_unreadable_policy_module(self):
        self.write_project(
            "blue",
            'name = "blue"\nunit_prefix = "blue-agent"\nroot = "/srv/ops/blue"\n'
            'eligibility = "missing_module_xyz:eligible"\n',
        )
        problems = projects.check()
        self.assertEqual(len(problems), 1)
        self.assertIn("blue", problems[0])

    def test_check_is_quiet_for_a_readable_policy_module(self):
        policy_dir = os.path.join(self.tmp, "policies")
        os.makedirs(policy_dir)
        with open(os.path.join(policy_dir, "blue_policy.py"), "w", encoding="utf-8") as handle:
            handle.write("def eligible(rows, *, now):\n    return []\n")
        self.write_project(
            "blue",
            'name = "blue"\nunit_prefix = "blue-agent"\nroot = "/srv/ops/blue"\n'
            'eligibility = "blue_policy:eligible"\n',
        )
        with mock.patch.object(sys, "path", [policy_dir, *sys.path]):
            self.assertEqual(projects.check(), [])

    def test_check_resolves_a_relative_entry_against_the_project_root(self):
        ops_root = os.path.join(self.tmp, "ops")
        os.makedirs(os.path.join(ops_root, "policies"))
        with open(
            os.path.join(ops_root, "policies", "blue_policy.py"), "w", encoding="utf-8"
        ) as handle:
            handle.write("def eligible(rows, *, now):\n    return []\n")
        self.write_project(
            "blue",
            'name = "blue"\nunit_prefix = "blue-agent"\n'
            f'root = "{ops_root}"\n'
            'eligibility = "policies/blue_policy.py:eligible"\n',
        )
        elsewhere = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-elig-cwd-"))
        old = os.getcwd()
        os.chdir(elsewhere)
        try:
            self.assertEqual(projects.check(), [])
        finally:
            os.chdir(old)


if __name__ == "__main__":
    unittest.main()
