"""`korapy snapshot`: one read-only view of the host.

Offline: every reader is faked. No `gh`, no systemd, no network, no
real state dirs.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from korapy import cli, registry, resources as R, settings, snapshot

T0 = 1_800_000_000.0
HOST = "host-1"


def entry(
    agent_id,
    state="live",
    project="proj-a",
    repo="repo-a",
    number=1,
    runtime="omp",
    role=None,
    profile=None,
    model="m1",
    note="working",
    age=10,
):
    """One fake registry entry with a last-seen `age` seconds before T0."""
    data = {
        "id": agent_id,
        "runtime": runtime,
        "role": role,
        "profile": profile,
        "ticket": None
        if repo is None
        else {"project": project or "", "repo": repo, "number": number},
        "ticket_source": None if repo is None else "explicit",
        "model": model,
        "parent": None,
        "pid": None,
        "transcript": None,
        "host": HOST,
        "started_at": T0 - 100,
        "last_seen": T0 - age,
        "note": note,
        "ended_at": None if state != "finished" else T0 - 5,
        "outcome": None if state != "finished" else "ok",
        "pr": None,
    }
    return data


def view(*rows):
    return list(rows)


def quota_view():
    return {
        "now": T0,
        "providers": {
            "provider-a": {
                "state": "ok",
                "windows": {"weekly": {"used_pct": 10.0, "resets_at": None}},
                "read_at": T0,
                "stale": False,
                "mark": None,
                "reader": "fake:provider-a",
                "error": None,
            }
        },
    }


def config(total_cap=4, repo_cap=1, count=True):
    return {
        "admission.total_cap": total_cap,
        "admission.repo_cap": repo_cap,
        "registry.count_toward_cap": count,
    }


def usage(path):
    return (1000, 400, 600)


def default_rows():
    return view(
        (entry("a1"), "live"),
        (entry("a2", state="stale", repo="repo-b", number=2, age=700), "stale"),
        (entry("a3", state="finished", repo=None), "finished"),
    )


def doc_limits():
    return R.Limits(
        host={"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
        roles={
            "worker": {"cpu_m": 1000, "memory_mb": 2048, "pids": 100},
            "reviewer": {"cpu_m": 500, "memory_mb": 1024, "pids": 50},
        },
        foreign=[],
        gone_s=600.0,
    )


def doc_pressure():
    return R.Pressure(load_per_cpu=0.4, mem_available_pct=55.0, saturated=False)


def resolved_settings(**extra):
    """A real `settings.Resolved` for collectors that read settings objects."""
    values = {
        "admission.total_cap": 4,
        "admission.repo_cap": 1,
        "agents.unit_prefix": "korapy-agent-",
        "registry.count_toward_cap": True,
        "registry.stale_s": 600,
        "registry.gone_s": 3600,
        "quota.low_pct": 80,
        "drive.max_starts_per_day": 50,
        "resources.gone_s": 600,
    }
    values.update(extra)
    return settings.Resolved(
        values={key: settings.Value(value, "test") for key, value in values.items()},
        resources=None,
    )


def live_listing(rows):
    """`{project: [ids]}` for the live-here ticketed rows, like the real collector.

    Ticketless live rows stay unlisted: without a units listing no
    owner names them, so they hold no share, like a crashed agent.
    """
    live = {}
    for entry, state in rows:
        ticket = entry.get("ticket") if isinstance(entry, dict) else None
        project = ticket.get("project") if isinstance(ticket, dict) else None
        if state == registry.LIVE and entry.get("host", HOST) == HOST and project:
            live.setdefault(project, []).append(entry.get("id"))
    return live


def names_of(rows):
    """The ticket projects named in `rows`: the registered names for the test."""
    names = set()
    for row_entry, _state in rows:
        ticket = row_entry.get("ticket") if isinstance(row_entry, dict) else None
        project = ticket.get("project") if isinstance(ticket, dict) else None
        if isinstance(project, str) and project:
            names.add(project)
    return names


def doc_of(**overrides):
    rows = overrides.pop("registry_view", None)
    if rows is None:
        rows = default_rows()
    resources_view = overrides.pop("resources_view", None)
    if resources_view is None:
        resources_view = (
            rows
            if isinstance(rows, Exception)
            else snapshot.resources_section(
                rows,
                HOST,
                doc_limits(),
                doc_pressure(),
                [],
                lambda scope, name, project=None: None,
                live_by_owner=live_listing(rows),
                project_names=names_of(rows),
            )
        )
    params = {
        "now": T0,
        "host": HOST,
        "registry_view": rows,
        "quota_view": quota_view(),
        "settings": config(),
        "usage_fn": usage,
        "resources_view": resources_view,
    }
    params.update(overrides)
    return snapshot.build(**params)


class FullDocument(unittest.TestCase):
    def test_full_document_pins_schema(self):
        from korapy import paths

        doc = doc_of()
        state, cache = paths.state_dir(), paths.cache_dir()
        self.assertEqual(
            doc,
            {
                "schema_version": 2,
                "fleet_header": {
                    "host": HOST,
                    "now": T0,
                    "agents_live": 1,
                    "agents_stale": 1,
                    "agents_finished": 1,
                },
                "agents": [
                    {
                        "id": "a3",
                        "project": None,
                        "repo": None,
                        "number": None,
                        "runtime": "omp",
                        "role": None,
                        "profile": "default",
                        "model": "m1",
                        "state": "finished",
                        "started_at": T0 - 100,
                        "last_seen": T0 - 10,
                        "note": "working",
                    },
                    {
                        "id": "a1",
                        "project": "proj-a",
                        "repo": "repo-a",
                        "number": 1,
                        "runtime": "omp",
                        "role": None,
                        "profile": "default",
                        "model": "m1",
                        "state": "live",
                        "started_at": T0 - 100,
                        "last_seen": T0 - 10,
                        "note": "working",
                    },
                    {
                        "id": "a2",
                        "project": "proj-a",
                        "repo": "repo-b",
                        "number": 2,
                        "runtime": "omp",
                        "role": None,
                        "profile": "default",
                        "model": "m1",
                        "state": "stale",
                        "started_at": T0 - 100,
                        "last_seen": T0 - 700,
                        "note": "working",
                    },
                ],
                "quota": quota_view(),
                "capacity": {
                    "total_cap": 4,
                    "repo_cap": 1,
                    "live": 1,
                    "by_project": {"proj-a": 1},
                    "pending": [],
                },
                "disk": {
                    "paths": [
                        {"path": state, "total": 1000, "used": 400, "free": 600},
                        {"path": cache, "total": 1000, "used": 400, "free": 600},
                    ]
                },
                "drive": {"error": "drive section not collected"},
                "resources": {
                    "schema_version": 1,
                    "host_budget": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
                    "charged": {"cpu_m": 1000, "memory_mb": 2048, "pids": 100},
                    "by_project": {"proj-a": {"cpu_m": 1000, "memory_mb": 2048, "pids": 100}},
                    "measured_by_project": {"proj-a": None},
                    "agents": [
                        {
                            "id": "a1",
                            "project": "proj-a",
                            "role": None,
                            "state": "live",
                            "charged": {"cpu_m": 1000, "memory_mb": 2048, "pids": 100},
                            "measured": None,
                        },
                        {
                            "id": "a2",
                            "project": "proj-a",
                            "role": None,
                            "state": "stale",
                            "charged": {"cpu_m": 0, "memory_mb": 0, "pids": 0},
                            "measured": None,
                        },
                        {
                            "id": "a3",
                            "project": None,
                            "role": None,
                            "state": "finished",
                            "charged": {"cpu_m": 0, "memory_mb": 0, "pids": 0},
                            "measured": None,
                        },
                    ],
                    "pressure": {
                        "load_per_cpu": 0.4,
                        "mem_available_pct": 55.0,
                        "saturated": False,
                        "cpu_some_avg10": None,
                        "memory_some_avg10": None,
                    },
                    "notes": [],
                },
            },
        )

    def test_top_level_key_order(self):
        self.assertEqual(
            list(doc_of()),
            [
                "schema_version",
                "fleet_header",
                "agents",
                "quota",
                "capacity",
                "resources",
                "disk",
                "drive",
            ],
        )

    def test_agent_without_ticket_reads_null(self):
        doc = doc_of(registry_view=view((entry("solo", repo=None), "live")))
        self.assertEqual(
            doc["agents"],
            [
                {
                    "id": "solo",
                    "project": None,
                    "repo": None,
                    "number": None,
                    "runtime": "omp",
                    "role": None,
                    "profile": "default",
                    "model": "m1",
                    "state": "live",
                    "started_at": T0 - 100,
                    "last_seen": T0 - 10,
                    "note": "working",
                }
            ],
        )
        self.assertEqual(doc["capacity"]["live"], 0)
        self.assertEqual(doc["capacity"]["by_project"], {})

    def test_stale_and_finished_free_the_slot(self):
        doc = doc_of(
            registry_view=view(
                (entry("s1", state="stale", age=700), "stale"),
                (entry("f1", state="finished"), "finished"),
            )
        )
        header = doc["fleet_header"]
        self.assertEqual(
            (header["agents_live"], header["agents_stale"], header["agents_finished"]), (0, 1, 1)
        )
        self.assertEqual(doc["capacity"]["live"], 0)

    def test_two_projects_sorted_by_project_repo_number_id(self):
        rows = view(
            (entry("b2", project="proj-b", repo="repo-a", number=2), "live"),
            (entry("a1", project="proj-a", repo="repo-b", number=1), "live"),
            (entry("b1", project="proj-b", repo="repo-a", number=1), "live"),
            (entry("zz", repo=None), "live"),
        )
        doc = doc_of(registry_view=rows)
        self.assertEqual([row["id"] for row in doc["agents"]], ["zz", "a1", "b1", "b2"])
        self.assertEqual(doc["capacity"]["by_project"], {"proj-a": 1, "proj-b": 2})
        self.assertEqual(doc["capacity"]["live"], 3)

    def test_missing_fields_read_null_never_absent(self):
        doc = doc_of(registry_view=view(({"id": "bare"}, "live")))
        self.assertEqual(
            doc["agents"][0],
            {
                "id": "bare",
                "project": None,
                "repo": None,
                "number": None,
                "runtime": None,
                "role": None,
                "profile": "default",
                "model": None,
                "state": "live",
                "started_at": None,
                "last_seen": None,
                "note": None,
            },
        )

    def test_reviewer_and_worker_roles_show_in_rows(self):
        doc = doc_of(
            registry_view=view(
                (entry("w1", role="worker"), "live"),
                (entry("r1", role="reviewer", repo="repo-b", number=2), "live"),
            )
        )
        roles = {row["id"]: row["role"] for row in doc["agents"]}
        self.assertEqual(roles, {"w1": "worker", "r1": "reviewer"})

    def test_recorded_profile_shows_in_rows(self):
        doc = doc_of(
            registry_view=view(
                (entry("p1", profile="fast"), "live"),
                (entry("p2"), "live"),
            )
        )
        by_id = {row["id"]: row["profile"] for row in doc["agents"]}
        self.assertEqual(by_id, {"p1": "fast", "p2": "default"})

    def test_entry_without_a_profile_key_reads_default(self):
        naked = entry("p3")
        del naked["profile"]
        doc = doc_of(registry_view=view((naked, "live")))
        self.assertEqual(doc["agents"][0]["profile"], "default")


class SectionErrors(unittest.TestCase):
    def test_broken_section_isolated_from_rest(self):
        doc = doc_of(quota_view=ValueError("ledger gone"))
        self.assertEqual(doc["quota"], {"error": "ledger gone"})
        self.assertIsInstance(doc["agents"], list)
        self.assertIn("live", doc["capacity"])
        self.assertIn("paths", doc["disk"])

    def test_broken_registry_breaks_dependents_only(self):
        doc = doc_of(registry_view=OSError("agents dir gone"))
        self.assertEqual(doc["agents"], {"error": "agents dir gone"})
        self.assertEqual(doc["fleet_header"], {"error": "agents dir gone"})
        self.assertEqual(doc["capacity"], {"error": "agents dir gone"})
        self.assertEqual(doc["resources"], {"error": "agents dir gone"})
        self.assertIn("paths", doc["disk"])
        self.assertIn("providers", doc["quota"])

    def test_missing_resources_view_is_its_own_error(self):
        doc = snapshot.build(T0, HOST, default_rows(), quota_view(), config(), usage)
        self.assertEqual(doc["resources"], {"error": "resources section not collected"})
        self.assertIsInstance(doc["agents"], list)

    def test_unreadable_disk_path_reports_path_error(self):
        def fail(path):
            raise OSError(f"cannot read {path}")

        doc = doc_of(usage_fn=fail)
        for item in doc["disk"]["paths"]:
            self.assertEqual(set(item), {"path", "error"})
        self.assertIsInstance(doc["agents"], list)

    def test_count_toward_cap_off_frees_every_slot(self):
        rows = view((entry("a1"), "live"))
        doc = doc_of(registry_view=rows, settings=config(count=False))
        self.assertEqual(doc["capacity"]["live"], 0)
        self.assertEqual(doc["capacity"]["by_project"], {})


class ErrorHelpers(unittest.TestCase):
    def test_error_text_prefers_the_message(self):
        self.assertEqual(snapshot.error_text(ValueError("ledger gone")), "ledger gone")

    def test_error_text_falls_back_to_repr(self):
        self.assertEqual(snapshot.error_text(ValueError()), "ValueError()")

    def test_is_error_section_matches_exactly(self):
        self.assertTrue(snapshot.is_error_section({"error": "gone"}))
        for other in ({"error": "gone", "extra": 1}, {}, [], None, "gone", 0):
            with self.subTest(other=other):
                self.assertFalse(snapshot.is_error_section(other))


class HumanFinishedCount(unittest.TestCase):
    """A hand-crafted non-finite finished count reads as 0, like the dashboard."""

    def first_line(self, finished):
        doc = {
            "agents": [],
            "fleet_header": {
                "host": HOST,
                "now": T0,
                "agents_live": 0,
                "agents_stale": 0,
                "agents_finished": finished,
            },
        }
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            snapshot._show(doc)
        return out.getvalue().splitlines()[0]

    def test_nan_finished_reads_zero(self):
        line = self.first_line(float("nan"))
        self.assertIn("0 finished", line)
        self.assertNotIn("nan", line)

    def test_infinite_finished_reads_zero(self):
        for value in (float("inf"), float("-inf")):
            with self.subTest(value=value):
                line = self.first_line(value)
                self.assertIn("0 finished", line)
                self.assertNotIn("inf", line)

    def test_finite_finished_passes_through(self):
        self.assertIn("2 finished", self.first_line(2))

    def test_non_number_finished_reads_zero(self):
        for value in (True, "2", None):
            with self.subTest(value=value):
                line = self.first_line(value)
                self.assertIn("0 finished", line)


class CapacityAgreesWithRegistry(unittest.TestCase):
    def test_capacity_ignores_other_hosts(self):
        foreign = entry("a9", repo="repo-b", number=9)
        foreign["host"] = "host-2"
        doc = doc_of(registry_view=view((entry("a1"), "live"), (foreign, "live")))
        self.assertEqual(doc["capacity"]["live"], 1)

    def test_capacity_counts_match_counted_tickets(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        alive = lambda pid: True  # noqa: E731 - one-line test fake
        now = T0
        registry.register(
            "a1",
            "omp",
            ticket=registry.make_ticket("repo-a", 1, "proj-a"),
            now=now - 10,
            path=tmp,
            host=HOST,
            alive=alive,
        )
        registry.register(
            "a2",
            "omp",
            ticket=registry.make_ticket("repo-a", 1, "proj-a"),
            now=now - 10,
            path=tmp,
            host=HOST,
            alive=alive,
        )
        registry.register(
            "a3",
            "omp",
            ticket=registry.make_ticket("repo-b", 2, "proj-b"),
            now=now - 1200,
            path=tmp,
            host=HOST,
            alive=alive,
        )
        registry.register("a4", "omp", now=now - 10, path=tmp, host=HOST, alive=alive)
        registry.done("a3", now=now - 5, path=tmp)
        cfg = registry.default_config()
        rows = registry.visible(now, cfg=cfg, path=tmp, alive=alive, host=HOST)
        counted = registry.counted_tickets(now, cfg=cfg, path=tmp, alive=alive, host=HOST)
        doc = snapshot.build(now, HOST, rows, quota_view(), config(), usage)
        self.assertEqual(doc["capacity"]["live"], len(counted))
        self.assertEqual(sum(doc["capacity"]["by_project"].values()), len(counted))


def golden_section():
    rows = view(
        (entry("a1", role="worker"), "live"),
        (entry("a2", role="reviewer", repo="repo-b", number=2), "live"),
        (entry("a9", repo="repo-b", number=9), "live"),
        (entry("s1", project="proj-b", state="stale", age=700), "stale"),
    )
    rows[2][0]["host"] = "host-2"
    measured = {
        ("agent", "a1"): R.Measured(cpu_usage_usec=5000, memory_mb=12.5, pids=8),
        ("project", "proj-a"): R.Measured(cpu_usage_usec=6000, memory_mb=20.0, pids=12),
    }

    def fake(scope, name, project=None):
        return measured.get((scope, name))

    return snapshot.resources_section(
        rows,
        HOST,
        doc_limits(),
        R.Pressure(
            load_per_cpu=0.4,
            mem_available_pct=55.0,
            saturated=False,
            cpu_some_avg10=0.02,
            memory_some_avg10=0.01,
        ),
        [],
        fake,
        live_by_owner={"proj-a": ["a1", "a2"]},
        project_names={"proj-a", "proj-b"},
    )


class ResourcesSection(unittest.TestCase):
    def test_section_matches_the_golden_file(self):
        golden = os.path.join(os.path.dirname(__file__), "golden", "snapshot-resources.json")
        with open(golden, encoding="utf-8") as handle:
            self.assertEqual(golden_section(), json.load(handle))

    def test_section_key_order_is_stable(self):
        self.assertEqual(
            list(golden_section()),
            [
                "schema_version",
                "host_budget",
                "charged",
                "by_project",
                "measured_by_project",
                "agents",
                "pressure",
                "notes",
            ],
        )

    def test_no_budget_still_reports_charged_and_pressure(self):
        section = snapshot.resources_section(
            view((entry("a1", role="worker"), "live")),
            HOST,
            R.Limits(host=None, roles={"worker": {"cpu_m": 1, "memory_mb": 1, "pids": 1}}),
            None,
            ["note: /proc/loadavg unreadable"],
            None,
            live_by_owner={"proj-a": ["a1"]},
            project_names={"proj-a"},
        )
        self.assertIsNone(section["host_budget"])
        self.assertEqual(section["charged"], {"cpu_m": 1, "memory_mb": 1, "pids": 1})
        self.assertEqual(section["pressure"]["load_per_cpu"], None)
        self.assertEqual(section["notes"], ["note: /proc/loadavg unreadable"])

    def test_unknown_role_charges_zero_like_the_launch_gate(self):
        rows = view(
            (entry("w1", role="worker"), "live"),
            (entry("x1", role="bogus"), "live"),
        )
        section = snapshot.resources_section(
            rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            None,
            live_by_owner={"proj-a": ["w1", "x1"]},
            project_names={"proj-a"},
        )
        charged = {row["id"]: row["charged"] for row in section["agents"]}
        self.assertEqual(charged["w1"], {"cpu_m": 1000, "memory_mb": 2048, "pids": 100})
        self.assertEqual(charged["x1"], {"cpu_m": 0, "memory_mb": 0, "pids": 0})

    def test_measured_defaults_to_null_and_survives_a_broken_reader(self):
        def boom(scope, name, project=None):
            raise OSError("slice gone")

        section = snapshot.resources_section(
            view((entry("a1", role="worker"), "live")),
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            boom,
            live_by_owner={"proj-a": ["a1"]},
            project_names={"proj-a"},
        )
        self.assertIsNone(section["agents"][0]["measured"])
        self.assertIsNone(section["measured_by_project"]["proj-a"])

    def test_agent_without_a_project_pays_its_prefix_owner(self):
        seen = []

        def fake(scope, name, project=None):
            seen.append((scope, name, project))
            return None

        section = snapshot.resources_section(
            view((entry("solo", repo=None), "live")),
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            fake,
            live_by_owner={"proj-b": ["solo"]},
            project_names={"proj-b"},
        )
        self.assertEqual(section["charged"], {"cpu_m": 1000, "memory_mb": 2048, "pids": 100})
        self.assertEqual(section["by_project"], {"proj-b": section["charged"]})
        self.assertNotIn("", section["by_project"])
        agent = section["agents"][0]
        self.assertIsNone(agent["project"])
        self.assertEqual(agent["charged"], section["charged"])
        self.assertIsNone(agent["measured"])
        self.assertEqual(seen, [("agent", "solo", "proj-b"), ("project", "proj-b", None)])
        self.assertIsNone(section["measured_by_project"]["proj-b"])


class ChargedMatchesGate(unittest.TestCase):
    """The snapshot charges exactly what the launch gate charges.

    Both read the same shared sum over the same registry view and the
    same units listing: a crashed agent (registry live, unit gone)
    holds no share in either, and an agent with no ticket project pays
    its prefix owner, never an empty name.
    """

    PREFIX_A = "pa-"
    PREFIX_B = "pb-"
    UNIT_A = "pa-lib-5"
    UNIT_B = "pb-web-3"
    UNIT_GONE = "pa-db-7"
    ROLES = {
        "worker": {"cpu_m": 1000, "memory_mb": 2048, "pids": 100},
        "reviewer": {"cpu_m": 500, "memory_mb": 1024, "pids": 50},
    }
    WORKER = {"cpu_m": 1000, "memory_mb": 2048, "pids": 100}
    REVIEWER = {"cpu_m": 500, "memory_mb": 1024, "pids": 50}

    def rows(self):
        return [
            (
                {
                    "id": self.UNIT_A,
                    "role": "worker",
                    "ticket": {"project": "proj-a", "repo": "lib", "number": 5},
                    "host": HOST,
                },
                "live",
            ),
            ({"id": self.UNIT_B, "role": "reviewer", "ticket": None, "host": HOST}, "live"),
            (
                {
                    "id": self.UNIT_GONE,
                    "role": "worker",
                    "ticket": {"project": "proj-a", "repo": "db", "number": 7},
                    "host": HOST,
                },
                "live",
            ),
        ]

    def listing(self):
        table = {
            self.PREFIX_A: [(self.UNIT_A, "active", "running")],
            self.PREFIX_B: [(self.UNIT_B, "active", "running")],
        }
        return table.__getitem__

    def registered(self):
        from types import SimpleNamespace

        return [
            SimpleNamespace(name="proj-a", unit_prefix=self.PREFIX_A),
            SimpleNamespace(name="proj-b", unit_prefix=self.PREFIX_B),
        ]

    def gate_charged(self, rows):
        from korapy import launch

        with mock.patch("korapy.registry.visible", return_value=list(rows)):
            return launch._charged_usage(
                "proj-a",
                self.PREFIX_A,
                self.registered(),
                self.listing(),
                dict(self.ROLES),
                None,
                T0,
            )

    def test_a_crashed_agent_is_charged_by_neither(self):
        rows = self.rows()
        charged = self.gate_charged(rows)
        self.assertEqual(charged.total, R.Usage(1500, 3072, 150), "the gone unit holds no share")
        section = snapshot.resources_section(
            rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            None,
            live_by_owner={"proj-a": [self.UNIT_A], "proj-b": [self.UNIT_B]},
            project_names={"proj-a", "proj-b"},
        )
        self.assertEqual(section["charged"], {"cpu_m": 1500, "memory_mb": 3072, "pids": 150})

    def test_snapshot_totals_equal_the_gate_for_the_same_inputs(self):
        rows = self.rows()
        charged = self.gate_charged(rows)
        section = snapshot.resources_section(
            rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            None,
            live_by_owner={"proj-a": [self.UNIT_A], "proj-b": [self.UNIT_B]},
            project_names={"proj-a", "proj-b"},
        )
        self.assertEqual(section["charged"], R.usage_dict(charged.total))
        self.assertEqual(
            section["by_project"],
            {name: R.usage_dict(used) for name, used in charged.by_project.items()},
        )
        self.assertEqual(
            section["by_project"],
            {"proj-a": dict(self.WORKER), "proj-b": dict(self.REVIEWER)},
        )
        self.assertNotIn("", section["by_project"])

    def test_snapshot_keyed_by_project_name_matches_the_gate(self):
        """The snapshot way (project name as key) equals the gate's sum.

        Registered projects proj-a/pa- and proj-b/pb-, one ticketed unit
        and one ticketless unit under the local prefix: the snapshot must
        report the gate's totals and by_project keys, with no phantom
        prefix key and no double charge.
        """
        from korapy import launch

        rows = [
            (
                {
                    "id": "pa-lib-5",
                    "role": "worker",
                    "ticket": {"project": "proj-a", "repo": "lib", "number": 5},
                    "host": HOST,
                },
                "live",
            ),
            ({"id": "pa-ops-9", "role": "reviewer", "ticket": None, "host": HOST}, "live"),
            ({"id": "pb-web-3", "role": "reviewer", "ticket": None, "host": HOST}, "live"),
        ]
        table = {
            self.PREFIX_A: [
                ("pa-lib-5", "active", "running"),
                ("pa-ops-9", "active", "running"),
            ],
            self.PREFIX_B: [("pb-web-3", "active", "running")],
        }

        def list_fn(prefix):
            return list(table.get(prefix, []))

        with mock.patch("korapy.registry.visible", return_value=list(rows)):
            charged = launch._charged_usage(
                "proj-a",
                self.PREFIX_A,
                self.registered(),
                list_fn,
                dict(self.ROLES),
                None,
                T0,
            )
        listing = R.live_units_by_owner(
            [(item.name, item.unit_prefix) for item in self.registered()],
            "proj-a",
            self.PREFIX_A,
            list_fn,
        )
        self.assertEqual(set(listing), {"proj-a", "proj-b"})
        section = snapshot.resources_section(
            rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            None,
            live_by_owner=listing,
            project_names={"proj-a", "proj-b"},
        )
        self.assertEqual(section["charged"], R.usage_dict(charged.total))
        self.assertEqual(
            section["by_project"],
            {name: R.usage_dict(used) for name, used in charged.by_project.items()},
        )
        self.assertEqual(
            section["by_project"],
            {
                "proj-a": {"cpu_m": 1500, "memory_mb": 3072, "pids": 150},
                "proj-b": dict(self.REVIEWER),
            },
        )
        self.assertNotIn("pa-", section["by_project"])

    def test_no_projects_ticketless_unit_keys_match(self):
        """Gate and snapshot key an unregistered ticketless unit the same way."""
        from korapy import launch

        unit = "korapy-agent-lib-5"
        rows = [({"id": unit, "role": None, "ticket": None, "host": HOST}, "live")]

        def list_fn(prefix):
            self.assertEqual(prefix, "korapy-agent-")
            return [(unit, "active", "running")]

        with mock.patch("korapy.registry.visible", return_value=list(rows)):
            charged = launch._charged_usage(
                "korapy-agent-",
                "korapy-agent-",
                [],
                list_fn,
                dict(self.ROLES),
                None,
                T0,
            )
        section = snapshot.resources_section(
            rows,
            HOST,
            doc_limits(),
            doc_pressure(),
            [],
            None,
            live_by_owner={"korapy-agent-": [unit]},
            project_names=[],
        )
        self.assertEqual(set(charged.by_project), set(section["by_project"]))
        self.assertEqual(set(section["by_project"]), {"korapy-agent-"})
        self.assertNotIn("", section["by_project"])


class MappingLimitsBranch(unittest.TestCase):
    def test_mapping_limits_charge_like_objects(self):
        section = snapshot.resources_section(
            view((entry("a1", role="worker"), "live")),
            HOST,
            {
                "host": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
                "roles": {
                    "worker": {"cpu_m": 1000, "memory_mb": 2048, "pids": 100},
                    "reviewer": {"cpu_m": 500, "memory_mb": 1024, "pids": 50},
                },
            },
            doc_pressure(),
            [],
            None,
            live_by_owner={"proj-a": ["a1"]},
            project_names={"proj-a"},
        )
        self.assertEqual(section["charged"], {"cpu_m": 1000, "memory_mb": 2048, "pids": 100})
        self.assertEqual(section["host_budget"], {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096})

    def test_falsy_non_mapping_roles_raise(self):
        rows = view((entry("a1", role="worker"), "live"))
        host = {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096}
        for bad in (0, "", [], False):
            with self.subTest(roles=bad):
                with self.assertRaises(R.LimitsError):
                    snapshot.resources_section(
                        rows,
                        HOST,
                        {"host": dict(host), "roles": bad},
                        doc_pressure(),
                        [],
                        None,
                        live_by_owner={"proj-a": ["a1"]},
                        project_names={"proj-a"},
                    )
        self.assertEqual(R.LimitsError.exit_code, 4)

    def test_missing_or_null_roles_charge_zero(self):
        rows = view((entry("a1", role="worker"), "live"))
        host = {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096}
        for limits in ({"host": dict(host)}, {"host": dict(host), "roles": None}):
            with self.subTest(limits=limits):
                section = snapshot.resources_section(
                    rows,
                    HOST,
                    limits,
                    doc_pressure(),
                    [],
                    None,
                    live_by_owner={"proj-a": ["a1"]},
                    project_names={"proj-a"},
                )
                self.assertEqual(section["charged"], {"cpu_m": 0, "memory_mb": 0, "pids": 0})

    def test_non_limits_and_a_non_mapping_listing_raise(self):
        rows = view((entry("a1", role="worker"), "live"))
        for bad in (42, "limits", ["host"]):
            with self.subTest(limits=bad):
                with self.assertRaises(R.LimitsError):
                    snapshot.resources_section(
                        rows,
                        HOST,
                        bad,
                        doc_pressure(),
                        [],
                        None,
                        live_by_owner={"proj-a": ["a1"]},
                        project_names={"proj-a"},
                    )
        for bad in (["a1"], "proj-a", 5, None):
            with self.subTest(live_by_owner=bad):
                with self.assertRaises(R.LimitsError):
                    snapshot.resources_section(
                        rows,
                        HOST,
                        doc_limits(),
                        doc_pressure(),
                        [],
                        None,
                        live_by_owner=bad,
                        project_names={"proj-a"},
                    )
        self.assertEqual(R.LimitsError.exit_code, 4)


class DefaultMeasured(unittest.TestCase):
    def test_agent_scope_reads_the_leaf(self):
        from korapy import units

        measured = R.Measured(cpu_usage_usec=5000, memory_mb=12.5, pids=8)
        with mock.patch("korapy.resources.read_slice_use", return_value=(measured, [])) as reader:
            got = snapshot.default_measured("agent", "korapy-agent-lib-5", "proj-a")
        self.assertEqual(got, measured)
        (directory,), _ = reader.call_args
        self.assertEqual(directory, units.agent_cgroup("proj-a", "korapy-agent-lib-5"))

    def test_project_scope_reads_the_slice(self):
        from korapy import units

        measured = R.Measured(cpu_usage_usec=6000, memory_mb=20.0, pids=12)
        with mock.patch("korapy.resources.read_slice_use", return_value=(measured, [])) as reader:
            got = snapshot.default_measured("project", "proj-a")
        self.assertEqual(got, measured)
        (directory,), _ = reader.call_args
        self.assertEqual(directory, units.project_cgroup("proj-a"))

    def test_scopes_without_a_subject_read_null_without_reading(self):
        with mock.patch(
            "korapy.resources.read_slice_use",
            side_effect=AssertionError("must not read"),
        ):
            self.assertIsNone(snapshot.default_measured("agent", "korapy-agent-lib-5", None))
            self.assertIsNone(snapshot.default_measured("agent", "korapy-agent-lib-5", ""))
            self.assertIsNone(snapshot.default_measured("project", None))
            self.assertIsNone(snapshot.default_measured("project", ""))
            self.assertIsNone(snapshot.default_measured("bogus", "x", "proj-a"))

    def test_an_unreadable_leaf_and_a_bad_unit_read_null(self):
        with mock.patch("korapy.resources.read_slice_use", side_effect=OSError("slice gone")):
            self.assertIsNone(snapshot.default_measured("agent", "korapy-agent-lib-5", "proj-a"))
        with mock.patch(
            "korapy.resources.read_slice_use",
            side_effect=AssertionError("must not read"),
        ):
            self.assertIsNone(snapshot.default_measured("agent", "not a unit!", "proj-a"))


class HubPathNeverWritesTheLedger(unittest.TestCase):
    def test_report_and_snapshot_build_with_writes_rigged_to_fail(self):
        from korapy import host_ledger

        with (
            mock.patch.object(host_ledger, "update", side_effect=AssertionError("must not write")),
            mock.patch.object(host_ledger, "ensure", side_effect=AssertionError("must not write")),
        ):
            report = R.capacity_report(
                host_budget={"cpu_m": 1, "memory_mb": 1, "pids": 1},
                charged=R.Charged(total=R.Usage()),
                pressure=R.Pressure(),
            )
            section = golden_section()
            doc = doc_of()
        self.assertEqual(report["schema_version"], 1)
        self.assertIn("charged", section)
        self.assertIn("resources", doc)

    def test_report_and_snapshot_modules_never_name_the_ledger(self):
        import ast

        for dotted in ("korapy.snapshot", "korapy.resources"):
            path = os.path.join("src", *dotted.split(".")) + ".py"
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        names.add(alias.name.split(".")[0])
                        if alias.asname:
                            names.add(alias.asname)
            self.assertNotIn("host_ledger", names, dotted)
            self.assertNotIn("ledger_path", names, dotted)


class CliExits(unittest.TestCase):
    def run_main(self, argv, registry_view=None, quota=None, resolved=None, usage_fn=None):
        from korapy import settings

        if registry_view is None:
            registry_view = view((entry("a1"), "live"))
        if quota is None:
            quota = quota_view()
        if resolved is None:
            resolved = resolved_settings()
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("korapy.registry.visible", return_value=registry_view),
            mock.patch("korapy.accounts.providers", return_value=[]),
            mock.patch("korapy.quota_ledger.load", return_value={"readings": {}, "marks": {}}),
            mock.patch("korapy.settings.load", return_value=resolved),
            mock.patch("korapy.projects.load_all", return_value=[]),
            mock.patch("korapy.units.list_units", return_value=[]),
            mock.patch("korapy.drive.drive_status", return_value={"last_exit": 0}),
            mock.patch(
                "korapy.resources.read_pressure",
                return_value=(R.Pressure(load_per_cpu=0.4), []),
            ),
            mock.patch("korapy.snapshot.default_measured", return_value=None),
            mock.patch("socket.gethostname", return_value=HOST),
            mock.patch("shutil.disk_usage", side_effect=lambda path: (1000, 400, 600)),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            if isinstance(quota, Exception):
                with mock.patch("korapy.quotacli.snapshot", side_effect=quota):
                    code = cli.main(argv)
            else:
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_json_output_parses_with_exact_key_order(self):
        code, out, _ = self.run_main(["snapshot", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(
            list(doc),
            [
                "schema_version",
                "fleet_header",
                "agents",
                "quota",
                "capacity",
                "resources",
                "disk",
                "drive",
            ],
        )
        self.assertEqual(doc["schema_version"], 2)
        self.assertEqual(len(doc["agents"]), 1)
        self.assertEqual(doc["drive"], {"last_exit": 0})
        self.assertEqual(doc["resources"]["schema_version"], 1)

    def test_ok_human_output(self):
        code, out, _ = self.run_main(["snapshot"])
        self.assertEqual(code, 0)
        self.assertIn("a1", out)

    def test_human_output_reports_charged_and_pressure(self):
        code, out, _ = self.run_main(["snapshot"])
        self.assertEqual(code, 0)
        self.assertIn("resources: charged", out)
        self.assertIn("pressure:", out)

    def test_human_output_shows_reviewer_vs_worker(self):
        rows = view(
            (entry("w1", role="worker"), "live"),
            (entry("r1", role="reviewer"), "live"),
        )
        code, out, _ = self.run_main(["snapshot"], registry_view=rows)
        self.assertEqual(code, 0)
        self.assertIn("  worker  ", out)
        self.assertIn("  reviewer  ", out)

    def test_section_error_still_exits_zero(self):
        code, out, _ = self.run_main(["snapshot", "--json"], quota=ValueError("pool gone"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["quota"], {"error": "pool gone"})

    def test_settings_error_exits_four(self):
        from korapy import settings

        with (
            mock.patch("korapy.settings.load", side_effect=settings.SettingsError("bad cap")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["snapshot", "--json"]), 4)

    def test_nothing_readable_exits_three(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("korapy.registry.visible", side_effect=OSError("gone")),
            mock.patch("korapy.accounts.providers", side_effect=OSError("gone")),
            mock.patch("korapy.settings.load", return_value=resolved_settings()),
            mock.patch("korapy.projects.load_all", return_value=[]),
            mock.patch("korapy.units.list_units", return_value=[]),
            mock.patch("korapy.paths.state_dir", side_effect=OSError("gone")),
            mock.patch(
                "korapy.resources.read_pressure",
                return_value=(R.Pressure(), ["note: gone"]),
            ),
            mock.patch("socket.gethostname", return_value=HOST),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.assertEqual(cli.main(["snapshot", "--json"]), 3)
        self.assertEqual(out.getvalue(), "")

    def test_only_drive_healthy_exits_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("korapy.registry.visible", side_effect=OSError("gone")),
            mock.patch("korapy.accounts.providers", side_effect=OSError("gone")),
            mock.patch("korapy.settings.load", return_value=resolved_settings()),
            mock.patch("korapy.projects.load_all", return_value=[]),
            mock.patch("korapy.units.list_units", return_value=[]),
            mock.patch("korapy.drive.drive_status", return_value={"last_exit": 0}),
            mock.patch("korapy.paths.cache_dir", side_effect=OSError("gone")),
            mock.patch(
                "korapy.resources.read_pressure",
                return_value=(R.Pressure(), ["note: gone"]),
            ),
            mock.patch("socket.gethostname", return_value=HOST),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.assertEqual(cli.main(["snapshot", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["drive"], {"last_exit": 0})


if __name__ == "__main__":
    unittest.main()
