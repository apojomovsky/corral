"""The host project registry, and launch admission across projects."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral import admission, cli, launch, projects, registry, settings, units
from tests.test_reviewrecord import git_in


def corral_toml(cap):
    return f"""\
[agents]
cap = {cap}

[[repo]]
name = "lib"
clone = {{ path = "clones/lib" }}
"""


class HostCase(unittest.TestCase):
    """Two ops roots, a temp config/state tree, and a fake service manager."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="corral-projects-"))
        self.roots = {}
        for name in ("project-a", "project-b"):
            self.roots[name] = os.path.join(self.tmp, "roots", name)
        self.units = {}

        kept = {k: v for k, v in os.environ.items() if not k.startswith("CORRAL_")}
        env = dict(kept)
        env.update(
            CORRAL_STATE=os.path.join(self.tmp, "state"),
            CORRAL_SESSIONS=os.path.join(self.tmp, "sessions"),
            CORRAL_CONFIG=os.path.join(self.tmp, "config"),
            CORRAL_CACHE=os.path.join(self.tmp, "cache"),
        )
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))

    def list_fn(self, prefix):
        return [(name, state, "running") for name, state in self.units.items()]

    def add_units(self, prefix, *numbers):
        for number in numbers:
            self.units[f"{prefix}lib-{number}"] = "active"

    def make_root(self, name, cap=2):
        root = self.roots[name]
        os.makedirs(os.path.join(root, "clones"), exist_ok=True)
        clone = os.path.join(root, "clones", "lib")
        if not os.path.isdir(os.path.join(clone, ".git")):
            origin = os.path.join(self.tmp, f"{name}-lib.git")
            seed = os.path.join(self.tmp, f"{name}-seed")
            git_in(self.tmp, "init", "-q", "--bare", "-b", "main", origin)
            git_in(self.tmp, "init", "-q", "-b", "main", seed)
            git_in(seed, "commit", "-q", "--allow-empty", "-m", "one")
            git_in(seed, "remote", "add", "origin", origin)
            git_in(seed, "push", "-q", "origin", "main")
            git_in(self.tmp, "clone", "-q", origin, clone)
        with open(os.path.join(root, settings.PROJECT_FILE), "w", encoding="utf-8") as h:
            h.write(corral_toml(cap))
        return root

    def write_host(self, total_cap):
        config = os.path.join(os.environ["CORRAL_CONFIG"], "config.toml")
        os.makedirs(os.path.dirname(config), exist_ok=True)
        with open(config, "w", encoding="utf-8") as h:
            h.write(f"[admission]\ntotal_cap = {total_cap}\n")

    def write_project(self, name, raw=None, **fields):
        directory = projects.projects_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{name}.toml")
        if raw is not None:
            text = raw
        else:
            merged = {
                "name": name,
                "unit_prefix": f"{name}-agent",
                "root": self.roots[name],
                "floor": 0,
            }
            merged.update(fields)
            lines = []
            for key, value in merged.items():
                lines.append(f"{key} = {value!r}" if isinstance(value, str) else f"{key} = {value}")
            text = "\n".join(lines) + "\n"
        with open(path, "w", encoding="utf-8") as h:
            h.write(text)
        return path

    def target(self, name):
        return launch.target(self.roots[name], "lib")


class LoadTest(HostCase):
    def test_a_missing_registry_dir_is_an_empty_list(self):
        self.assertEqual(projects.load_all(), [])

    def test_projects_load_sorted_with_defaults(self):
        self.write_project("project-b", ceiling=2)
        self.write_project("project-a")
        with open(os.path.join(projects.projects_dir(), "notes.txt"), "w") as h:
            h.write("not a registration\n")
        found = projects.load_all()
        self.assertEqual([p.name for p in found], ["project-a", "project-b"])
        first = found[0]
        self.assertEqual(first.unit_prefix, "project-a-agent")
        self.assertEqual(first.root, self.roots["project-a"])
        self.assertEqual(first.floor, 0)
        self.assertIsNone(first.ceiling)
        self.assertEqual((found[1].floor, found[1].ceiling), (0, 2))

    def test_each_bad_file_names_itself(self):
        bad = [
            "not toml = [",
            'unit_prefix = "project-a-agent"\nroot = "/srv/ops/project-a"\n',
            'name = "other"\nunit_prefix = "project-a-agent"\nroot = "/srv/ops/project-a"\n',
            'name = "project-a"\nroot = "/srv/ops/project-a"\n',
            'name = "project-a"\nunit_prefix = 3\nroot = "/srv/ops/project-a"\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\nroot = 3\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\nroot = "rel/path"\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\nroot = "/srv/ops/a"\nfloor = -1\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\nroot = "/srv/ops/a"\nfloor = "1"\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\nroot = "/srv/ops/a"\nfloor = true\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\nroot = "/srv/ops/a"\nceiling = -2\n',
            'name = "project-a"\nunit_prefix = "project-a-agent"\nroot = "/srv/ops/a"\nfloor = 2\nceiling = 1\n',
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                path = self.write_project("project-a", raw=raw)
                with self.assertRaises(projects.ProjectsError) as ctx:
                    projects.load_all()
                self.assertIn(path, str(ctx.exception))
                os.remove(path)

    def test_a_plain_name_is_required_even_when_it_matches_the_stem(self):
        path = self.write_project("a!", raw='name = "a!"\nunit_prefix = "x"\nroot = "/srv/ops/a"\n')
        with self.assertRaises(projects.ProjectsError) as ctx:
            projects.load_all()
        self.assertIn(path, str(ctx.exception))

    def test_a_duplicate_unit_prefix_names_the_second_file(self):
        self.write_project("project-a")
        second = self.write_project("project-b", unit_prefix="project-a-agent")
        with self.assertRaises(projects.ProjectsError) as ctx:
            projects.load_all()
        self.assertIn(second, str(ctx.exception))

    def test_a_registration_error_is_a_configuration_error(self):
        self.assertEqual(projects.ProjectsError.exit_code, 4)


class ForRootTest(HostCase):
    def test_roots_resolve_by_realpath(self):
        self.write_project("project-a")
        self.write_project("project-b")
        found = projects.for_root(os.path.join(self.roots["project-a"], "sub", ".."))
        assert found is not None
        self.assertEqual(found.name, "project-a")
        self.assertIsNone(projects.for_root(os.path.join(self.tmp, "elsewhere")))
        self.assertIsNone(projects.for_root(None))


class LimitsTest(HostCase):
    def test_limits_carry_floors_and_clamp_ceilings(self):
        table = projects.limits(
            [
                projects.Project("project-a", "pa-", "/srv/ops/a", floor=1, ceiling=5),
                projects.Project("project-b", "pb-", "/srv/ops/b"),
            ],
            total_cap=3,
            repo_cap=0,
        )
        self.assertEqual(table.total, 3)
        self.assertEqual(table.projects["project-a"], {"floor": 1, "ceiling": 3})
        self.assertEqual(table.projects["project-b"], {"floor": 0, "ceiling": None})

    def test_floors_above_the_total_are_rejected(self):
        with self.assertRaises(admission.LimitsError) as ctx:
            projects.limits(
                [
                    projects.Project("project-a", "pa-", "/srv/ops/a", floor=2),
                    projects.Project("project-b", "pb-", "/srv/ops/b", floor=2),
                ],
                total_cap=3,
                repo_cap=0,
            )
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("4", str(ctx.exception))
        self.assertIn("admission.total_cap", str(ctx.exception))


class CheckTest(HostCase):
    def test_a_missing_registry_is_usable(self):
        self.assertEqual(projects.check(), [])

    def test_a_clean_registry_has_no_problems(self):
        self.write_host(3)
        self.write_project("project-a", ceiling=3, floor=1)
        self.assertEqual(projects.check(), [])

    def test_a_ceiling_above_the_host_total_is_reported(self):
        self.write_host(3)
        self.write_project("project-a", ceiling=5)
        self.assertEqual(projects.check(), ["project project-a asks ceiling 5, host allows 3"])

    def test_a_bad_file_is_reported(self):
        path = self.write_project("project-a", raw="not toml = [")
        self.assertEqual(len(projects.check()), 1)
        self.assertIn(path, projects.check()[0])


class AdmitAcrossProjectsTest(HostCase):
    def setUp(self):
        super().setUp()
        self.make_root("project-a", cap=3)
        self.make_root("project-b", cap=1)
        self.write_host(3)
        self.write_project("project-a", ceiling=3)
        self.write_project("project-b", ceiling=1, floor=1)

    def test_the_total_filled_by_one_project_leaves_the_other_its_floor(self):
        self.add_units("project-a-agent", 1, 2, 3)
        launch.admit(self.target("project-b"), 9, list_fn=self.list_fn)

    def test_beyond_the_floor_nothing_more_is_admitted(self):
        self.add_units("project-a-agent", 1, 2, 3)
        self.add_units("project-b-agent", 4)
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-b"), 9, list_fn=self.list_fn)
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertIn("ceiling", str(ctx.exception))

    def test_a_project_at_its_ceiling_is_refused(self):
        self.add_units("project-a-agent", 1, 2, 3)
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-a"), 9, list_fn=self.list_fn)
        self.assertIn("project-a at 3 of 3 ceiling", str(ctx.exception))

    def test_two_projects_units_count_into_one_total(self):
        self.add_units("project-a-agent", 1, 2)
        self.add_units("project-b-agent", 3)
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-a"), 9, list_fn=self.list_fn)
        self.assertIn("host full (3 of 3)", str(ctx.exception))

    def test_a_registry_ticket_counts_for_its_named_project(self):
        registry.register(
            "worker-1", "claude-code", ticket=registry.make_ticket("lib", 7, project="project-b")
        )
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-b"), 8, list_fn=self.list_fn)
        self.assertIn("project-b at 1 of 1 ceiling", str(ctx.exception))

    def test_an_unnamed_registry_ticket_counts_for_the_launching_project(self):
        registry.register("worker-1", "claude-code", ticket=registry.make_ticket("lib", 7))
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-b"), 8, list_fn=self.list_fn)
        self.assertIn("project-b at 1 of 1 ceiling", str(ctx.exception))
        launch.admit(self.target("project-a"), 5, list_fn=self.list_fn)

    def test_a_foreign_registry_ticket_is_one_slot_not_two(self):
        self.write_project("project-a", ceiling=1)
        registry.register(
            "worker-1", "claude-code", ticket=registry.make_ticket("lib", 7, project="project-b")
        )
        launch.admit(self.target("project-a"), 5, list_fn=self.list_fn)

    def test_a_stale_holder_does_not_steal_a_tickets_attribution(self):
        old = time.time() - 10_000
        registry.register(
            "worker-old",
            "claude-code",
            ticket=registry.make_ticket("lib", 7, project="project-a"),
            now=old,
        )
        registry.register(
            "worker-new", "claude-code", ticket=registry.make_ticket("lib", 7, project="project-b")
        )
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-b"), 8, list_fn=self.list_fn)
        self.assertIn("project-b at 1 of 1 ceiling", str(ctx.exception))

    def test_floors_above_the_total_are_rejected(self):
        self.write_project("project-a", ceiling=3, floor=2)
        self.write_project("project-b", ceiling=2, floor=2)
        with self.assertRaises(admission.LimitsError) as ctx:
            launch.admit(self.target("project-a"), 9, list_fn=self.list_fn)
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_a_live_ticket_is_still_already_live_before_admission(self):
        self.add_units("project-a-agent", 1, 2, 3)
        with self.assertRaises(units.UnitExists) as ctx:
            launch.admit(self.target("project-a"), 2, list_fn=self.list_fn)
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_a_differing_agents_cap_is_noted_not_an_error(self):
        self.make_root("project-a", cap=2)
        self.add_units("project-a-agent", 1, 2, 3)
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-a"), 9, list_fn=self.list_fn)
        self.assertIn("agents.cap 2", str(ctx.exception))
        self.assertIn("the ceiling wins", str(ctx.exception))

    def test_a_matching_agents_cap_carries_no_note(self):
        self.add_units("project-a-agent", 1, 2, 3)
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-a"), 9, list_fn=self.list_fn)
        self.assertNotIn("agents.cap", str(ctx.exception))


class AdmitWithoutRegistrationTest(HostCase):
    def test_the_cap_stays_the_agents_cap(self):
        self.make_root("project-a", cap=2)
        launch.admit(self.target("project-a"), 5, list_fn=self.list_fn)
        self.add_units("corral-agent-", 6, 7)
        with self.assertRaises(launch.CapFull) as ctx:
            launch.admit(self.target("project-a"), 5, list_fn=self.list_fn)
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertIn("at 2 of 2 ceiling", str(ctx.exception))


class ProjectsVerbTest(HostCase):
    def run_projects(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            with mock.patch.object(units, "list_units", self.list_fn):
                code = cli.main(["projects", *argv])
        return code, out.getvalue()

    def test_projects_lists_live_counts(self):
        self.make_root("project-a")
        self.make_root("project-b")
        self.write_project("project-a", ceiling=3)
        self.write_project("project-b")
        self.add_units("project-a-agent", 1)
        registry.register(
            "worker-1", "claude-code", ticket=registry.make_ticket("lib", 7, project="project-b")
        )
        code, out = self.run_projects()
        self.assertEqual(code, 0)
        self.assertIn("project-a", out)
        self.assertIn("live=1", out)
        self.assertIn("project-b", out)
        self.assertIn("prefix=project-b-agent", out)

    def test_projects_json_is_a_stable_document(self):
        self.make_root("project-a")
        self.write_project("project-a", ceiling=3, floor=1)
        self.add_units("project-a-agent", 1)
        code, out = self.run_projects("--json")
        self.assertEqual(code, 0)
        (row,) = json.loads(out)["projects"]
        self.assertEqual(
            row,
            {
                "ceiling": 3,
                "floor": 1,
                "live": 1,
                "name": "project-a",
                "root": self.roots["project-a"],
                "unit_prefix": "project-a-agent",
            },
        )

    def test_projects_json_marks_a_missing_ceiling(self):
        self.make_root("project-a")
        self.write_project("project-a")
        code, out = self.run_projects("--json")
        self.assertEqual(code, 0)
        (row,) = json.loads(out)["projects"]
        self.assertIsNone(row["ceiling"])
        self.assertEqual(row["live"], 0)

    def test_a_bad_registration_is_exit_4(self):
        self.write_project("project-a", raw="not toml = [")
        with mock.patch.object(units, "list_units", self.list_fn):
            self.assertEqual(cli.main(["projects"]), 4)
            self.assertEqual(cli.main(["projects", "--json"]), 4)


if __name__ == "__main__":
    unittest.main()
