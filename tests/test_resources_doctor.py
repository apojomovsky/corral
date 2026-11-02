"""Doctor `resources*` rows: budget, ledger, marker, pressure, claims, slices.

Pure checks over `resourcedoctor.ResourceReadings`, fakes at every seam:
no systemd, no cgroup filesystem, no ledger outside a temp dir.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from corral import doctor, registry, resourcedoctor
from corral import resources as R


def readings(**overrides):
    fields: dict[str, Any] = {
        "host": R.Usage(cpu_m=8000, memory_mb=16000, pids=4096),
        "shares": {"web": R.Share(weight=1.0)},
        "foreign": [],
        "gone_s": 600.0,
        "now": 1_000_000.0,
        "limits_error": None,
        "priced_roles": frozenset({"worker"}),
        "live_roles": (),
        "controllers": frozenset({"cpu", "memory", "pids"}),
        "controllers_error": None,
        "ledger": {"version": 1, "host_budget": None, "projects": {}, "foreign": []},
        "ledger_error": None,
        "ledger_path": "/state/corral/host-resources.json",
        "marker": "/state",
        "marker_error": None,
        "state_dir": "/state",
        "marker_path": "/state/corral/host-state-dir",
        "sensor_errors": (),
        "slices": {},
        "config_path": "/config/config.toml",
    }
    fields.update(overrides)
    return resourcedoctor.ResourceReadings(**fields)


def by_name(rows):
    return {row.name: row for row in rows}


def live_unit(name="corral-agent-web-3", active="active", sub=""):
    return (name, active, sub)


def show(memory_bytes="17179869184", quota_usec="8000000", tasks="512"):
    return f"MemoryMax={memory_bytes}\nCPUQuotaPerSecUSec={quota_usec}\nTasksMax={tasks}\n"


def props(*args):
    return resourcedoctor.parse_show(show(*args))


class NoReadings(unittest.TestCase):
    def test_no_state_means_no_resource_rows(self):
        self.assertEqual(resourcedoctor.check(None), [])

    def test_default_env_carries_no_resource_rows(self):
        from tests.test_doctor import env

        checks = doctor.run_checks(env())
        self.assertFalse([c for c in checks if c.name.startswith("resources")])

    def test_row_names(self):
        rows = resourcedoctor.check(readings())
        self.assertEqual(
            [row.name for row in rows],
            [
                "resources",
                "resources-ledger",
                "resources-marker",
                "resources-pressure",
                "resources-foreign",
                "resources-slices",
            ],
        )


class UnsetBudget(unittest.TestCase):
    def test_unset_host_warns_and_skips_slices(self):
        rows = by_name(resourcedoctor.check(readings(host=None)))
        self.assertEqual(rows["resources"].status, "warn")
        self.assertIn("passes everything", rows["resources"].detail)
        self.assertIn("/config/config.toml", rows["resources"].fix)
        self.assertNotIn("resources-slices", rows)

    def test_unset_host_ignores_controllers_and_roles(self):
        rows = by_name(
            resourcedoctor.check(
                readings(
                    host=None,
                    controllers=None,
                    controllers_error="cannot read delegation",
                    live_roles=("triage",),
                )
            )
        )
        self.assertEqual(rows["resources"].status, "warn")


class Delegation(unittest.TestCase):
    def test_full_delegation_is_ok(self):
        rows = by_name(resourcedoctor.check(readings()))
        self.assertEqual(rows["resources"].status, "ok")
        self.assertIn("cpu", rows["resources"].detail)

    def test_optional_controllers_read_as_unavailable_never_a_failure(self):
        rows = by_name(
            resourcedoctor.check(readings(controllers=frozenset({"cpu", "memory", "pids"})))
        )
        self.assertEqual(rows["resources"].status, "ok")
        self.assertIn("cpuset", rows["resources"].detail)

    def test_present_optional_controllers_are_not_listed(self):
        rows = by_name(
            resourcedoctor.check(
                readings(controllers=frozenset({"cpu", "memory", "pids", "cpuset", "io"}))
            )
        )
        self.assertEqual(rows["resources"].status, "ok")
        self.assertNotIn("unavailable", rows["resources"].detail)

    def test_missing_required_controller_fails(self):
        rows = by_name(resourcedoctor.check(readings(controllers=frozenset({"cpu", "memory"}))))
        self.assertEqual(rows["resources"].status, "fail")
        self.assertIn("pids", rows["resources"].detail)

    def test_unreadable_controllers_fail(self):
        rows = by_name(
            resourcedoctor.check(
                readings(controllers=None, controllers_error="cannot read at /sys: boom")
            )
        )
        self.assertEqual(rows["resources"].status, "fail")
        self.assertIn("cannot read", rows["resources"].detail)

    def test_a_controllers_failure_keeps_the_unpriced_role_warnings(self):
        rows = by_name(
            resourcedoctor.check(readings(controllers=frozenset({"cpu"}), live_roles=("triage",)))
        )
        row = rows["resources"]
        self.assertEqual(row.status, "fail")
        self.assertIn("memory", row.detail)
        self.assertIn("triage", row.detail)

    def test_a_failing_row_exits_1(self):
        rows = by_name(resourcedoctor.check(readings(controllers=frozenset({"cpu"}))))
        checks = [doctor.Check(r.name, r.status, r.detail, r.fix) for r in rows.values()]
        self.assertEqual(doctor.exit_code(checks), 1)


class InvalidTables(unittest.TestCase):
    def test_limits_error_fails_and_names_the_config(self):
        rows = by_name(
            resourcedoctor.check(
                readings(limits_error="project web cpu_m floor 9000 is above its ceiling 100")
            )
        )
        self.assertEqual(rows["resources"].status, "fail")
        self.assertIn("9000", rows["resources"].detail)
        self.assertIn("/config/config.toml", rows["resources"].fix)

    def test_limits_error_skips_the_slices_row(self):
        rows = by_name(resourcedoctor.check(readings(limits_error="boom")))
        self.assertNotIn("resources-slices", rows)

    def test_foreign_claim_limits_error_points_at_the_ledger(self):
        rows = by_name(
            resourcedoctor.check(
                readings(limits_error="foreign claim 0 must be a foreign claim, not {}")
            )
        )
        self.assertEqual(rows["resources"].status, "fail")
        self.assertIn("/state/corral/host-resources.json", rows["resources"].fix)


class UnpricedRoles(unittest.TestCase):
    def test_live_unpriced_role_warns(self):
        rows = by_name(resourcedoctor.check(readings(live_roles=("worker", "triage"))))
        self.assertEqual(rows["resources"].status, "warn")
        self.assertIn("triage", rows["resources"].detail)
        self.assertIn("[resources.role.triage]", rows["resources"].detail)
        self.assertIn("/config/config.toml", rows["resources"].fix)

    def test_priced_live_roles_stay_ok(self):
        rows = by_name(
            resourcedoctor.check(
                readings(live_roles=("worker",), priced_roles=frozenset({"worker"}))
            )
        )
        self.assertEqual(rows["resources"].status, "ok")

    def test_no_live_roles_stays_ok(self):
        rows = by_name(resourcedoctor.check(readings()))
        self.assertEqual(rows["resources"].status, "ok")

    def test_non_string_roles_are_ignored(self):
        rows = by_name(resourcedoctor.check(readings(live_roles=(123,))))
        self.assertEqual(rows["resources"].status, "ok")


class LedgerHealth(unittest.TestCase):
    def test_corrupt_ledger_fails_and_names_the_file(self):
        rows = by_name(
            resourcedoctor.check(
                readings(
                    ledger=None,
                    ledger_error="host ledger /state/corral/host-resources.json is corrupt: boom",
                )
            )
        )
        self.assertEqual(rows["resources-ledger"].status, "fail")
        self.assertIn("/state/corral/host-resources.json", rows["resources-ledger"].detail)
        self.assertIn("never reset", rows["resources-ledger"].fix)

    def test_readable_ledger_is_ok(self):
        rows = by_name(resourcedoctor.check(readings()))
        self.assertEqual(rows["resources-ledger"].status, "ok")
        self.assertIn("/state/corral/host-resources.json", rows["resources-ledger"].detail)


class GuardMarker(unittest.TestCase):
    def test_foreign_state_dir_fails_and_names_both_dirs(self):
        rows = by_name(resourcedoctor.check(readings(marker="/other-state")))
        self.assertEqual(rows["resources-marker"].status, "fail")
        self.assertIn("/other-state", rows["resources-marker"].detail)
        self.assertIn("/state", rows["resources-marker"].detail)
        self.assertIn("/state/corral/host-state-dir", rows["resources-marker"].fix)

    def test_matching_marker_is_ok(self):
        rows = by_name(resourcedoctor.check(readings()))
        self.assertEqual(rows["resources-marker"].status, "ok")

    def test_absent_marker_is_ok_until_the_first_launch(self):
        rows = by_name(resourcedoctor.check(readings(marker=None)))
        self.assertEqual(rows["resources-marker"].status, "ok")
        self.assertIn("next launch", rows["resources-marker"].detail)

    def test_unreadable_marker_fails(self):
        rows = by_name(resourcedoctor.check(readings(marker_error="cannot read the marker: boom")))
        self.assertEqual(rows["resources-marker"].status, "fail")
        self.assertIn("boom", rows["resources-marker"].detail)


class PressureSensors(unittest.TestCase):
    def test_unreadable_sensors_warn(self):
        rows = by_name(resourcedoctor.check(readings(sensor_errors=("/proc/loadavg: boom",))))
        self.assertEqual(rows["resources-pressure"].status, "warn")
        self.assertIn("/proc/loadavg", rows["resources-pressure"].detail)

    def test_readable_sensors_are_ok(self):
        rows = by_name(resourcedoctor.check(readings()))
        self.assertEqual(rows["resources-pressure"].status, "ok")


class ForeignClaims(unittest.TestCase):
    def claim(self, name, age_s, **extra):
        entry = {
            "name": name,
            "cpu_m": 1000,
            "memory_mb": 1024,
            "pids": 10,
            "claimed_at": 1_000_000.0 - age_s,
        }
        entry.update(extra)
        return entry

    def test_expired_claim_warns_and_names_it(self):
        rows = by_name(resourcedoctor.check(readings(foreign=[self.claim("desktop", 3600.0)])))
        self.assertEqual(rows["resources-foreign"].status, "warn")
        self.assertIn("desktop", rows["resources-foreign"].detail)
        self.assertIn("ignored", rows["resources-foreign"].detail)

    def test_live_claim_is_ok(self):
        rows = by_name(resourcedoctor.check(readings(foreign=[self.claim("desktop", 60.0)])))
        self.assertEqual(rows["resources-foreign"].status, "ok")
        self.assertIn("1", rows["resources-foreign"].detail)

    def test_no_claims_is_ok(self):
        rows = by_name(resourcedoctor.check(readings()))
        self.assertEqual(rows["resources-foreign"].status, "ok")
        self.assertIn("no foreign claims", rows["resources-foreign"].detail)

    def test_malformed_claim_fails(self):
        rows = by_name(resourcedoctor.check(readings(foreign=[{"name": "desktop"}])))
        self.assertEqual(rows["resources-foreign"].status, "fail")
        self.assertIn("/state/corral/host-resources.json", rows["resources-foreign"].fix)

    def test_a_foreign_limits_error_fails_the_foreign_row_too(self):
        error = "foreign claim 0 must be a foreign claim, not {}"
        rows = by_name(resourcedoctor.check(readings(limits_error=error)))
        row = rows["resources-foreign"]
        self.assertEqual(row.status, "fail")
        self.assertIn(error, row.detail)
        self.assertIn("/state/corral/host-resources.json", row.fix)


class SliceDrift(unittest.TestCase):
    def test_no_budget_means_no_slices_row(self):
        rows = by_name(resourcedoctor.check(readings(host=None)))
        self.assertNotIn("resources-slices", rows)

    def test_unreadable_slices_warn_instead_of_a_false_ok(self):
        rows = by_name(resourcedoctor.check(readings(slices=None)))
        row = rows["resources-slices"]
        self.assertEqual(row.status, "warn")
        self.assertIn("could not read the resource slices", row.detail)

    def test_no_slices_yet_is_ok(self):
        rows = by_name(resourcedoctor.check(readings(slices={})))
        self.assertEqual(rows["resources-slices"].status, "ok")
        self.assertIn("no resource slices", rows["resources-slices"].detail.lower())

    def test_launch_created_slices_matching_the_budget_are_ok(self):
        launch_slices = {
            "corral.slice": {"MemoryMax": "16777216000", "CPUQuotaPerSecUSec": "8000000"},
            "corral-web.slice": {
                "MemoryMax": "16777216000",
                "CPUQuotaPerSecUSec": "8000000",
            },
        }
        rows = by_name(resourcedoctor.check(readings(slices=launch_slices)))
        self.assertEqual(rows["resources-slices"].status, "ok")

    def test_a_tasksmax_on_a_slice_is_not_drift(self):
        slices = {
            "corral.slice": {
                "MemoryMax": "16777216000",
                "CPUQuotaPerSecUSec": "8000000",
                "TasksMax": "512",
            },
            "corral-web.slice": {
                "MemoryMax": "16777216000",
                "CPUQuotaPerSecUSec": "8000000",
            },
        }
        rows = by_name(resourcedoctor.check(readings(slices=slices)))
        self.assertEqual(rows["resources-slices"].status, "ok")

    def test_missing_slice_is_created_on_launch_not_drift(self):
        rows = by_name(
            resourcedoctor.check(
                readings(
                    slices={
                        "corral.slice": {
                            "MemoryMax": "16777216000",
                            "CPUQuotaPerSecUSec": "8000000",
                        }
                    }
                )
            )
        )
        self.assertEqual(rows["resources-slices"].status, "ok")

    def test_drift_warns_with_the_operator_refresh_step(self):
        rows = by_name(
            resourcedoctor.check(
                readings(
                    slices={
                        "corral.slice": {
                            "MemoryMax": "17179869184",
                            "CPUQuotaPerSecUSec": "4000000",
                        },
                        "corral-web.slice": {
                            "MemoryMax": "16777216000",
                            "CPUQuotaPerSecUSec": "8000000",
                        },
                    }
                )
            )
        )
        row = rows["resources-slices"]
        self.assertEqual(row.status, "warn")
        self.assertIn("corral.slice", row.detail)
        self.assertIn("MemoryMax", row.detail)
        self.assertIn("CPUQuotaPerSecUSec", row.detail)
        self.assertIn("never reconcile", row.detail)
        self.assertIn("daemon-reload", row.fix)

    def test_unset_limit_matches_infinity(self):
        state = readings(
            host=R.Usage(cpu_m=8000, memory_mb=16000, pids=0),
            slices={"corral.slice": props("16777216000", "8000000", "infinity")},
        )
        rows = by_name(resourcedoctor.check(state))
        self.assertEqual(rows["resources-slices"].status, "ok")

    def test_rounded_share_values_do_not_count_as_drift(self):
        memory_bytes = str(int(5333.33 * 1024 * 1024))
        state = readings(
            shares={
                "web": R.Share(weight=1.0, pids_floor=1),
                "api": R.Share(weight=2.0),
            },
            slices={
                "corral-web.slice": props(memory_bytes, "2666670", "1366"),
            },
        )
        self.assertEqual(by_name(resourcedoctor.check(state))["resources-slices"].status, "ok")


class ParseShow(unittest.TestCase):
    def test_parses_key_value_lines(self):
        props = resourcedoctor.parse_show(show())
        self.assertEqual(
            props,
            {
                "MemoryMax": "17179869184",
                "CPUQuotaPerSecUSec": "8000000",
                "TasksMax": "512",
            },
        )

    def test_skips_blanks_and_garbage(self):
        props = resourcedoctor.parse_show("\nMemoryMax=1G\nnot a line\n\n")
        self.assertEqual(props, {"MemoryMax": "1G"})


class SliceDriftUnit(unittest.TestCase):
    def test_equal_values_match(self):
        self.assertEqual(
            resourcedoctor.slice_drift(
                R.Usage(cpu_m=8000, memory_mb=16000, pids=4096),
                {"MemoryMax": "16777216000", "CPUQuotaPerSecUSec": "8000000", "TasksMax": "4096"},
            ),
            [],
        )

    def test_each_dimension_reports(self):
        drifts = resourcedoctor.slice_drift(
            R.Usage(cpu_m=8000, memory_mb=16000, pids=4096),
            {"MemoryMax": "1", "CPUQuotaPerSecUSec": "1"},
        )
        self.assertEqual(len(drifts), 2)

    def test_missing_property_is_unset(self):
        drifts = resourcedoctor.slice_drift(R.Usage(cpu_m=8000, memory_mb=16000, pids=4096), {})
        self.assertEqual(len(drifts), 2)

    def test_zero_expected_matches_infinity(self):
        self.assertEqual(
            resourcedoctor.slice_drift(
                R.Usage(cpu_m=0, memory_mb=0, pids=0),
                {"MemoryMax": "infinity", "CPUQuotaPerSecUSec": "infinity", "TasksMax": "infinity"},
            ),
            [],
        )

    def test_unparsable_actual_counts_as_unset(self):
        drifts = resourcedoctor.slice_drift(
            R.Usage(cpu_m=8000, memory_mb=16000, pids=4096),
            {"MemoryMax": "huge", "CPUQuotaPerSecUSec": "lots", "TasksMax": "many"},
        )
        self.assertEqual(len(drifts), 2)

    def test_a_tasksmax_actual_is_ignored(self):
        self.assertEqual(
            resourcedoctor.slice_drift(
                R.Usage(cpu_m=8000, memory_mb=16000, pids=4096),
                {
                    "MemoryMax": "16777216000",
                    "CPUQuotaPerSecUSec": "8000000",
                    "TasksMax": "1",
                },
            ),
            [],
        )

    def test_unset_expected_but_set_actual_drifts(self):
        drifts = resourcedoctor.slice_drift(
            R.Usage(cpu_m=0, memory_mb=0, pids=0),
            {"MemoryMax": "100", "CPUQuotaPerSecUSec": "100", "TasksMax": "100"},
        )
        self.assertEqual(len(drifts), 2)


class LoadLiveRoles(unittest.TestCase):
    def entry(self, name, role, live=True):
        return ({"id": name, "role": role}, registry.LIVE if live else registry.GONE)

    def test_roles_come_from_live_entries(self):
        roles = resourcedoctor.load_live_roles(
            ("corral-agent-",),
            list_fn=lambda prefix: [live_unit("corral-agent-web-3")],
            entries={"corral-agent-web-3": {"role": "reviewer"}},
        )
        self.assertEqual(roles, ("reviewer",))

    def test_units_without_an_entry_read_as_worker(self):
        roles = resourcedoctor.load_live_roles(
            ("corral-agent-",),
            list_fn=lambda prefix: [live_unit("corral-agent-web-3")],
            entries={},
        )
        self.assertEqual(roles, ("worker",))

    def test_non_live_units_and_unparseable_names_are_ignored(self):
        roles = resourcedoctor.load_live_roles(
            ("other-",),
            list_fn=lambda prefix: [
                live_unit("corral-agent-web-3", active="inactive"),
                live_unit("not-ours"),
            ],
            entries={},
        )
        self.assertEqual(roles, ())

    def test_an_unreachable_manager_reads_as_no_roles(self):
        from corral import units

        def down(prefix):
            raise units.Unreachable("down")

        roles = resourcedoctor.load_live_roles(("prefix-",), list_fn=down, entries={})
        self.assertEqual(roles, ())

    def test_roles_are_sorted_unique(self):
        roles = resourcedoctor.load_live_roles(
            ("a-", "b-"),
            list_fn=lambda prefix: [
                live_unit(f"{prefix}web-1"),
                live_unit(f"{prefix}web-2"),
            ],
            entries={f"a-web-1": {"role": "triage"}, f"b-web-2": {"role": "reviewer"}},
        )
        self.assertEqual(roles, ("reviewer", "triage", "worker"))


class LoadSliceActual(unittest.TestCase):
    def test_show_output_parses_per_slice(self):
        def run(argv):
            return SimpleNamespace(returncode=0, stdout=show(), stderr="")

        actual = resourcedoctor.load_slice_actual(("corral.slice", "corral-web.slice"), run=run)
        self.assertEqual(
            actual,
            {
                "corral.slice": {
                    "MemoryMax": "17179869184",
                    "CPUQuotaPerSecUSec": "8000000",
                    "TasksMax": "512",
                },
                "corral-web.slice": {
                    "MemoryMax": "17179869184",
                    "CPUQuotaPerSecUSec": "8000000",
                    "TasksMax": "512",
                },
            },
        )

    def test_missing_slice_is_absent_not_a_failure(self):
        def run(argv):
            if argv[-1] == "corral-web.slice":
                return SimpleNamespace(returncode=1, stdout="", stderr="gone")
            return SimpleNamespace(returncode=0, stdout=show(), stderr="")

        actual = resourcedoctor.load_slice_actual(("corral.slice", "corral-web.slice"), run=run)
        self.assertIsNotNone(actual)
        assert actual is not None
        self.assertEqual(list(actual), ["corral.slice"])

    def test_every_dump_failing_reads_as_no_reading(self):
        def run(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="gone")

        self.assertIsNone(resourcedoctor.load_slice_actual(("corral.slice",), run=run))

    def test_an_unreachable_bus_reads_as_no_reading(self):
        def run(argv):
            raise OSError("down")

        self.assertIsNone(resourcedoctor.load_slice_actual(("corral.slice",), run=run))


class LoadSensorErrors(unittest.TestCase):
    def test_all_readable_is_clean(self):
        errors = resourcedoctor.load_sensor_errors(read=lambda path: "0")
        self.assertEqual(errors, ())

    def test_unreadable_files_are_named(self):
        def read(path):
            if path == "/proc/meminfo":
                raise OSError("boom")
            return "0"

        errors = resourcedoctor.load_sensor_errors(read=read)
        self.assertEqual(errors, ("/proc/meminfo: boom",))

    def test_missing_pressure_files_are_named_not_fatal(self):
        def read(path):
            raise FileNotFoundError("no pressure on this kernel")

        errors = resourcedoctor.load_sensor_errors(read=read)
        self.assertTrue(all("no pressure" in error for error in errors))
        self.assertEqual(len(errors), len(resourcedoctor.SENSOR_FILES))


class Wiring(unittest.TestCase):
    def test_resource_rows_sit_between_disk_and_hub(self):
        from tests.test_doctor import env

        probe = doctor.HubProbe("ok", "the hub answers", hub_api=1, hub_schema=1)
        checks = doctor.run_checks(
            env(
                resource_readings=readings(),
                disk="ok",
                hub_url="http://hub-1.example.com",
                hub_auth=doctor.HubAuth("ok", "token present"),
                hub_probe=probe,
            )
        )
        ordered = [c.name for c in checks]
        self.assertLess(ordered.index("disk"), ordered.index("resources"))
        self.assertLess(ordered.index("resources-slices"), ordered.index("hub-token"))
        self.assertLess(ordered.index("hub-token"), ordered.index("hub"))

    def test_warnings_alone_still_exit_zero(self):
        from tests.test_doctor import env

        checks = doctor.run_checks(env(resource_readings=readings(host=None)))
        self.assertTrue(any(c.name == "resources" for c in checks))
        self.assertEqual(doctor.exit_code(checks), 0)


class ProductionAssembly(unittest.TestCase):
    def test_full_gathering_from_a_temp_host(self):
        from corral import host_ledger, paths, projects, settings, units

        with tempfile.TemporaryDirectory(prefix="corral-resources-") as tmp:
            state = os.path.join(tmp, "state")
            config = os.path.join(tmp, "config")
            os.makedirs(os.path.join(state, "corral"))
            os.makedirs(config, exist_ok=True)
            with open(os.path.join(config, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write(
                    "[resources.host]\ncpu_m = 8000\nmemory_mb = 16000\npids = 4096\n"
                    "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
                )
            ledger_path = os.path.join(state, "corral", "host-resources.json")
            with open(ledger_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"version": 1, "host_budget": null, "projects": {},'
                    ' "foreign": [{"name": "desktop", "cpu_m": 100,'
                    ' "memory_mb": 128, "claimed_at": 999999.0}]}'
                )
            with open(
                os.path.join(state, "corral", "host-state-dir"), "w", encoding="utf-8"
            ) as handle:
                handle.write(state)
            project = projects.Project(
                name="web",
                unit_prefix="corral-agent-",
                root="/ops",
                floor=0,
                ceiling=2,
                resources={"weight": 1},
            )
            with mock.patch.dict(
                os.environ,
                {"CORRAL_STATE": state, "CORRAL_CONFIG": config},
                clear=False,
            ):
                resolved = settings.load(root=None)
                with (
                    mock.patch.object(projects, "load_all", return_value=[project]),
                    mock.patch.object(
                        registry,
                        "visible",
                        return_value=[({"id": "u", "role": "triage"}, registry.LIVE)],
                    ),
                    mock.patch.object(host_ledger, "ledger_path", return_value=ledger_path),
                    mock.patch.object(paths, "state_dir", return_value=state),
                ):

                    def read(path):
                        if "cgroup" in path or path.startswith("/proc/"):
                            return "cpu memory pids\n"
                        with open(path, encoding="utf-8") as handle:
                            return handle.read()

                    state_readings = doctor.load_resource_readings(
                        resolved,
                        list_fn=lambda prefix: [],
                        run=lambda argv: SimpleNamespace(returncode=1, stdout="", stderr=""),
                        read=read,
                        now=1_000_000.0,
                    )
        self.assertIsNotNone(state_readings)
        assert state_readings is not None
        self.assertEqual(state_readings.host, R.Usage(cpu_m=8000, memory_mb=16000, pids=4096))
        self.assertEqual(state_readings.priced_roles, frozenset({"worker"}))
        self.assertEqual(state_readings.live_roles, ())
        self.assertIsNone(state_readings.limits_error)
        rows = by_name(resourcedoctor.check(state_readings))
        self.assertEqual(rows["resources"].status, "ok")
        self.assertEqual(rows["resources-foreign"].status, "ok")
        self.assertEqual(rows["resources-slices"].status, "warn")

    def test_no_resolved_means_no_readings(self):
        self.assertIsNone(
            doctor.load_resource_readings(None, list_fn=lambda prefix: [], run=lambda argv: None)
        )

    def test_corrupt_ledger_surfaces_as_a_failing_row(self):
        from corral import host_ledger, paths, projects, settings

        with tempfile.TemporaryDirectory(prefix="corral-resources-") as tmp:
            state = os.path.join(tmp, "state")
            config = os.path.join(tmp, "config")
            os.makedirs(os.path.join(state, "corral"))
            os.makedirs(config, exist_ok=True)
            with open(os.path.join(config, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write("[resources.host]\ncpu_m = 8000\nmemory_mb = 16000\npids = 4096\n")
            ledger_path = os.path.join(state, "corral", "host-resources.json")
            with open(ledger_path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            with mock.patch.dict(
                os.environ,
                {"CORRAL_STATE": state, "CORRAL_CONFIG": config},
                clear=False,
            ):
                resolved = settings.load(root=None)
                with (
                    mock.patch.object(projects, "load_all", return_value=[]),
                    mock.patch.object(registry, "visible", return_value=[]),
                    mock.patch.object(host_ledger, "ledger_path", return_value=ledger_path),
                    mock.patch.object(paths, "state_dir", return_value=state),
                ):
                    state_readings = doctor.load_resource_readings(
                        resolved,
                        list_fn=lambda prefix: [],
                        run=lambda argv: SimpleNamespace(returncode=1, stdout="", stderr=""),
                        read=lambda path: "cpu memory pids",
                        now=1_000_000.0,
                    )
        assert state_readings is not None
        rows = by_name(resourcedoctor.check(state_readings))
        self.assertEqual(rows["resources-ledger"].status, "fail")


if __name__ == "__main__":
    unittest.main()
