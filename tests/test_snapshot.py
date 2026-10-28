"""`corral snapshot`: one read-only view of the host.

Offline: every reader is faked. No `gh`, no systemd, no network, no
real state dirs.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import cli, registry, snapshot

T0 = 1_800_000_000.0
HOST = "host-1"


def entry(
    agent_id,
    state="live",
    project="proj-a",
    repo="repo-a",
    number=1,
    runtime="omp",
    model="m1",
    note="working",
    age=10,
):
    """One fake registry entry with a last-seen `age` seconds before T0."""
    data = {
        "id": agent_id,
        "runtime": runtime,
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


def doc_of(**overrides):
    params = {
        "now": T0,
        "host": HOST,
        "registry_view": view(
            (entry("a1"), "live"),
            (entry("a2", state="stale", repo="repo-b", number=2, age=700), "stale"),
            (entry("a3", state="finished", repo=None), "finished"),
        ),
        "quota_view": quota_view(),
        "settings": config(),
        "usage_fn": usage,
    }
    params.update(overrides)
    return snapshot.build(**params)


class FullDocument(unittest.TestCase):
    def test_full_document_pins_schema(self):
        from corral import paths

        doc = doc_of()
        state, cache = paths.state_dir(), paths.cache_dir()
        self.assertEqual(
            doc,
            {
                "schema_version": 1,
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
            },
        )

    def test_top_level_key_order(self):
        self.assertEqual(
            list(doc_of()),
            ["schema_version", "fleet_header", "agents", "quota", "capacity", "disk"],
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
                "model": None,
                "state": "live",
                "started_at": None,
                "last_seen": None,
                "note": None,
            },
        )


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
        self.assertIn("paths", doc["disk"])
        self.assertIn("providers", doc["quota"])

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


class CapacityAgreesWithRegistry(unittest.TestCase):
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


class CliExits(unittest.TestCase):
    def run_main(self, argv, registry_view=None, quota=None, resolved=None, usage_fn=None):
        from corral import settings

        if registry_view is None:
            registry_view = view((entry("a1"), "live"))
        if quota is None:
            quota = quota_view()
        if resolved is None:
            resolved = {
                "admission.total_cap": 4,
                "admission.repo_cap": 1,
                "registry.count_toward_cap": True,
                "registry.stale_s": 600,
                "registry.gone_s": 3600,
                "quota.low_pct": 80,
            }
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("corral.registry.visible", return_value=registry_view),
            mock.patch("corral.accounts.providers", return_value=[]),
            mock.patch("corral.quota_ledger.load", return_value={"readings": {}, "marks": {}}),
            mock.patch("corral.settings.load", return_value=resolved),
            mock.patch("socket.gethostname", return_value=HOST),
            mock.patch("shutil.disk_usage", side_effect=lambda path: (1000, 400, 600)),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            if isinstance(quota, Exception):
                with mock.patch("corral.quotacli.snapshot", side_effect=quota):
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
            ["schema_version", "fleet_header", "agents", "quota", "capacity", "disk"],
        )
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(len(doc["agents"]), 1)

    def test_ok_human_output(self):
        code, out, _ = self.run_main(["snapshot"])
        self.assertEqual(code, 0)
        self.assertIn("a1", out)

    def test_section_error_still_exits_zero(self):
        code, out, _ = self.run_main(["snapshot", "--json"], quota=ValueError("pool gone"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["quota"], {"error": "pool gone"})

    def test_settings_error_exits_four(self):
        from corral import settings

        with (
            mock.patch("corral.settings.load", side_effect=settings.SettingsError("bad cap")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["snapshot", "--json"]), 4)

    def test_nothing_readable_exits_three(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("corral.registry.visible", side_effect=OSError("gone")),
            mock.patch("corral.accounts.providers", side_effect=OSError("gone")),
            mock.patch(
                "corral.settings.load",
                return_value={
                    "admission.total_cap": 4,
                    "admission.repo_cap": 1,
                    "registry.count_toward_cap": True,
                    "registry.stale_s": 600,
                    "registry.gone_s": 3600,
                    "quota.low_pct": 80,
                },
            ),
            mock.patch("corral.paths.state_dir", side_effect=OSError("gone")),
            mock.patch("socket.gethostname", return_value=HOST),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.assertEqual(cli.main(["snapshot", "--json"]), 3)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
