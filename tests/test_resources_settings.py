"""The `[resources]` settings surface: host budget, role charges, project shares.

Section 5 of docs/09-host-resource-governance.md: the documented tables load
through `settings.resolve` (host config only) and the project registry
(`projects.d/<name>.toml`, beside the count floor and ceiling), with strict
keys. Every validation is an exit-4 error naming the file and the key.
Settings keys only: no CLI surface here.

Pure temp dirs and plain dicts throughout: no subprocess, no network, no
real state dir.
"""

import os
import tempfile
import unittest

from corral import projects, resources as R, settings as S


def resolve(**kw):
    kw.setdefault("env", {})
    if "host" in kw:
        # A host dict stands for the host config file: name it like one.
        kw.setdefault("host_name", "config.toml")
    return S.resolve(**kw)


def host_config(*tables, **scalars):
    """A host-config dict holding `[resources...]` tables plus raw scalars."""
    config = dict(scalars)
    if tables:
        merged: dict = {}
        for table in tables:
            for key, value in table.items():
                if key in merged and isinstance(merged[key], dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
        config["resources"] = merged
    return config


HOST = {"host": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096}}
WORKER = {"role": {"worker": {"cpu_m": 1000, "memory_mb": 2048}}}
REVIEWER = {"role": {"reviewer": {"cpu_m": 500, "memory_mb": 1024}}}


class Providers(unittest.TestCase):
    def test_resources_registers_its_settings(self):
        self.assertIn("corral.resources", S.PROVIDERS)

    def test_gone_s_defaults_to_ten_minutes(self):
        self.assertEqual(resolve()["resources.gone_s"], 600)


class HostBudget(unittest.TestCase):
    def test_the_documented_host_table_loads(self):
        tables = resolve(host=host_config(HOST)).resources
        self.assertEqual(tables.host, R.Usage(cpu_m=8000, memory_mb=16000, pids=4096))
        self.assertEqual(tables.roles, {})

    def test_no_resources_table_reads_as_unset(self):
        self.assertIsNone(resolve(host={}).resources)
        self.assertIsNone(resolve().resources)

    def test_a_partial_host_triple_names_the_missing_key(self):
        with self.assertRaisesRegex(S.SettingsError, r"config.toml.*\[resources\.host\].*'pids'"):
            resolve(host=host_config({"host": {"cpu_m": 8000, "memory_mb": 16000}}))

    def test_a_negative_host_budget_names_the_key(self):
        with self.assertRaisesRegex(S.SettingsError, r"config.toml.*\[resources\.host\].*cpu_m"):
            resolve(host=host_config({"host": {"cpu_m": -1, "memory_mb": 0, "pids": 0}}))

    def test_a_non_integer_host_budget_is_an_error(self):
        for bad in (True, 1.5, "8000"):
            with self.subTest(bad=bad), self.assertRaises(S.SettingsError):
                resolve(host=host_config({"host": {"cpu_m": bad, "memory_mb": 0, "pids": 0}}))

    def test_an_unknown_host_key_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, r"config.toml.*\[resources\.host\].*'cpu_x'"):
            resolve(host=host_config({"host": {"cpu_m": 1, "memory_mb": 1, "pids": 1, "cpu_x": 1}}))

    def test_there_is_no_instance_table(self):
        with self.assertRaisesRegex(S.SettingsError, r"config.toml.*'instance'"):
            resolve(host=host_config({"instance": {"weight": 2}}))

    def test_resources_must_be_a_table(self):
        with self.assertRaisesRegex(S.SettingsError, r"config.toml.*\[resources\]"):
            resolve(host={"resources": 5})

    def test_an_empty_resources_table_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, "empty table"):
            resolve(host={"resources": {}})

    def test_resources_tables_do_not_belong_in_the_project_file(self):
        with self.assertRaisesRegex(S.SettingsError, r"corral.toml.*resources\.host"):
            resolve(project={"resources": {"host": {"cpu_m": 1}}})

    def test_resources_tables_do_not_belong_in_a_repo_entry(self):
        with self.assertRaisesRegex(S.SettingsError, "resources"):
            resolve(project={"repo": [{"name": "app", "resources": {"host": {}}}]}, repo="app")


class RoleCharges(unittest.TestCase):
    def test_the_documented_role_tables_load_with_zero_defaults(self):
        tables = resolve(host=host_config(HOST, WORKER, REVIEWER)).resources
        self.assertEqual(tables.roles["worker"], R.Usage(cpu_m=1000, memory_mb=2048, pids=0))
        self.assertEqual(tables.roles["reviewer"], R.Usage(cpu_m=500, memory_mb=1024, pids=0))

    def test_an_unknown_role_names_the_role(self):
        with self.assertRaisesRegex(S.SettingsError, r"config\.toml.*\[resources\.role\].*'boss'"):
            resolve(host=host_config({"role": {"boss": {"cpu_m": 1}}}))

    def test_a_negative_charge_names_the_key(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"config\.toml.*\[resources\.role\.worker\].*memory_mb"
        ):
            resolve(host=host_config({"role": {"worker": {"memory_mb": -1}}}))

    def test_a_non_integer_charge_is_an_error(self):
        for bad in (True, 1.5, "1000"):
            with self.subTest(bad=bad), self.assertRaises(S.SettingsError):
                resolve(host=host_config({"role": {"worker": {"cpu_m": bad}}}))

    def test_an_unknown_charge_key_is_an_error(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"config\.toml.*\[resources\.role\.worker\].*'cpu_x'"
        ):
            resolve(host=host_config({"role": {"worker": {"cpu_x": 1}}}))

    def test_an_empty_role_table_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, "empty table"):
            resolve(host=host_config({"role": {"worker": {}}}))

    def test_a_role_table_must_be_a_table(self):
        with self.assertRaisesRegex(S.SettingsError, r"config\.toml.*\[resources\.role\.worker\]"):
            resolve(host=host_config({"role": {"worker": 5}}))

    def test_unpriced_roles_cost_nothing(self):
        limits = R.config(resolve(host=host_config(HOST, WORKER)))
        self.assertEqual(limits.roles["reviewer"], R.Usage())
        self.assertEqual(limits.roles["triage"], R.Usage())
        self.assertEqual(limits.roles["worker"], R.Usage(cpu_m=1000, memory_mb=2048))


class GoneS(unittest.TestCase):
    def test_gone_s_defaults_to_ten_minutes(self):
        self.assertEqual(resolve()["resources.gone_s"], 600)

    def test_the_settings_default_agrees_with_the_resources_constant(self):
        """The registered default and resources.DEFAULT_GONE_S are one fact."""
        self.assertEqual(S.REGISTRY["resources.gone_s"].default, int(R.DEFAULT_GONE_S))
        self.assertEqual(resolve()["resources.gone_s"], int(R.DEFAULT_GONE_S))

    def test_gone_s_is_settable_in_any_layer(self):
        self.assertEqual(resolve(host={"resources.gone_s": 60})["resources.gone_s"], 60)
        self.assertEqual(resolve(project={"resources.gone_s": 30})["resources.gone_s"], 30)
        self.assertEqual(resolve(env={"CORRAL_RESOURCES_GONE_S": "45"})["resources.gone_s"], 45)

    def test_config_carries_gone_s_with_the_limits(self):
        limits = R.config(resolve())
        self.assertEqual((limits.gone_s, limits.projects, limits.foreign), (600.0, {}, []))
        self.assertIsNone(limits.host)
        tuned = R.config(resolve(env={"CORRAL_RESOURCES_GONE_S": "60"}))
        self.assertEqual(tuned.gone_s, 60)

    def test_a_negative_gone_s_is_a_settings_error(self):
        with self.assertRaisesRegex(S.SettingsError, "resources.gone_s"):
            R.config(resolve(host={"resources.gone_s": -1}))

    def test_gone_s_loads_from_the_nested_resources_table(self):
        # tomllib nests `[resources] gone_s` under the resources table, so
        # the nested dict (not the dotted flat form) is what real files give.
        tables = resolve(host={"resources": {"gone_s": 60}})
        self.assertEqual(tables["resources.gone_s"], 60)
        self.assertIsNone(tables.resources)

    def test_gone_s_loads_beside_the_host_and_role_tables(self):
        import tomllib

        parsed = tomllib.loads(
            "[resources]\ngone_s = 60\n"
            "[resources.host]\ncpu_m = 8000\nmemory_mb = 16000\npids = 4096\n"
            "[resources.role.worker]\ncpu_m = 1000\nmemory_mb = 2048\n"
        )
        resolved = resolve(host=parsed, host_name="config.toml")
        self.assertEqual(resolved["resources.gone_s"], 60)
        assert resolved.resources is not None
        self.assertEqual(resolved.resources.host, R.Usage(cpu_m=8000, memory_mb=16000, pids=4096))
        self.assertEqual(resolved.resources.roles["worker"], R.Usage(cpu_m=1000, memory_mb=2048))

    def test_a_nested_gone_s_beside_no_tables_reads_as_unset(self):
        import tomllib

        parsed = tomllib.loads("[resources]\ngone_s = 45\n")
        resolved = resolve(host=parsed, host_name="config.toml")
        self.assertEqual(resolved["resources.gone_s"], 45)
        self.assertIsNone(resolved.resources)

    def test_a_bad_nested_gone_s_names_the_key(self):
        with self.assertRaisesRegex(S.SettingsError, r"config\.toml.*resources\.gone_s"):
            resolve(host={"resources": {"gone_s": "60"}})
        with self.assertRaisesRegex(S.SettingsError, "resources.gone_s"):
            R.config(resolve(host={"resources": {"gone_s": -1}}))

    def test_gone_s_set_twice_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, r"config\.toml.*set twice"):
            resolve(host={"resources": {"gone_s": 60}, "resources.gone_s": 30})


class DefaultsLeaveBehaviorUnchanged(unittest.TestCase):
    def test_an_unset_host_budget_passes_the_gate(self):
        limits = R.config(resolve())
        verdict = R.admit(
            R.Request(project="web", role="worker"),
            R.Charged(total=R.Usage(cpu_m=8000, memory_mb=16000, pids=4096)),
            limits,
            1_000_000.0,
        )
        self.assertEqual((verdict.allowed, verdict.code), (True, "ok"))

    def test_count_floors_still_load_without_a_resources_table(self):
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(
                config_dir,
                "web",
                'name = "web"\nunit_prefix = "corral-web-"\nroot = "/srv/web"\n'
                "floor = 2\nceiling = 4\n",
            )
            (found,) = projects.load_all(config_dir)
        self.assertEqual((found.floor, found.ceiling, found.resources), (2, 4, None))


def write_registration(config_dir, name, text):
    """Write `projects.d/<name>.toml` under a temp config dir."""
    directory = os.path.join(config_dir, projects.PROJECTS_DIR)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{name}.toml"), "w", encoding="utf-8") as handle:
        handle.write(text)


SHARE = """\
name = "web"
unit_prefix = "corral-web-"
root = "/srv/web"

[resources]
cpu_floor_m = 500
weight = 1
memory_floor_mb = 1024
"""


class ProjectShares(unittest.TestCase):
    def test_the_documented_project_table_loads(self):
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(config_dir, "web", SHARE)
            (found,) = projects.load_all(config_dir)
        self.assertEqual(
            found.resources,
            {
                "weight": 1,
                "cpu_floor_m": 500,
                "memory_floor_mb": 1024,
                "pids_floor": 0,
                "cpu_ceiling_m": None,
                "memory_ceiling_mb": None,
                "pids_ceiling": None,
            },
        )

    def test_a_full_share_table_loads(self):
        text = SHARE.replace("weight = 1", "weight = 2") + (
            "pids_floor = 50\ncpu_ceiling_m = 2000\nmemory_ceiling_mb = 4096\npids_ceiling = 200\n"
        )
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(config_dir, "web", text)
            (found,) = projects.load_all(config_dir)
        self.assertEqual(
            found.resources,
            {
                "weight": 2,
                "cpu_floor_m": 500,
                "memory_floor_mb": 1024,
                "pids_floor": 50,
                "cpu_ceiling_m": 2000,
                "memory_ceiling_mb": 4096,
                "pids_ceiling": 200,
            },
        )

    def test_no_resources_table_reads_as_unset(self):
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(
                config_dir,
                "web",
                'name = "web"\nunit_prefix = "corral-web-"\nroot = "/srv/web"\n',
            )
            (found,) = projects.load_all(config_dir)
        self.assertIsNone(found.resources)

    def test_an_unknown_share_key_names_the_file_and_key(self):
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(config_dir, "web", SHARE + "cpu_cap_m = 5\n")
            with self.assertRaisesRegex(projects.ProjectsError, r"web\.toml.*cpu_cap_m"):
                projects.load_all(config_dir)

    def test_a_prefixed_weight_is_not_a_key(self):
        # The doc's example spells this `cpu_weight`; the merged core and
        # O2 call the single remainder weight `weight`, so only that loads.
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(config_dir, "web", SHARE.replace("weight = 1", "cpu_weight = 1"))
            with self.assertRaisesRegex(projects.ProjectsError, "cpu_weight"):
                projects.load_all(config_dir)

    def test_a_share_table_must_be_a_table(self):
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(
                config_dir,
                "web",
                'name = "web"\nunit_prefix = "corral-web-"\nroot = "/srv/web"\nresources = 5\n',
            )
            with self.assertRaisesRegex(projects.ProjectsError, r"web\.toml.*\[resources\]"):
                projects.load_all(config_dir)

    def test_bad_share_amounts_name_the_key(self):
        cases = [
            ("cpu_floor_m = 500", "cpu_floor_m = -1", "cpu_floor_m"),
            ("cpu_floor_m = 500", "cpu_floor_m = true", "cpu_floor_m"),
            ("memory_floor_mb = 1024", "memory_floor_mb = 1.5", "memory_floor_mb"),
            ("weight = 1", "weight = -1", "weight"),
            ("weight = 1", "weight = true", "weight"),
        ]
        for old, new, key in cases:
            with self.subTest(new=new):
                with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
                    write_registration(config_dir, "web", SHARE.replace(old, new))
                    with self.assertRaisesRegex(projects.ProjectsError, key):
                        projects.load_all(config_dir)

    def test_a_floor_above_its_ceiling_names_the_numbers(self):
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(config_dir, "web", SHARE + "cpu_ceiling_m = 100\n")
            with self.assertRaisesRegex(projects.ProjectsError, r"500.*100"):
                projects.load_all(config_dir)

    def test_a_weight_of_zero_with_a_floor_is_allowed(self):
        with tempfile.TemporaryDirectory(prefix="corral-resources-") as config_dir:
            write_registration(config_dir, "web", SHARE.replace("weight = 1", "weight = 0"))
            (found,) = projects.load_all(config_dir)
        assert found.resources is not None
        self.assertEqual((found.resources["weight"], found.resources["cpu_floor_m"]), (0, 500))


class PressureThresholds(unittest.TestCase):
    def test_no_pressure_table_reads_as_defaults(self):
        tables = resolve(host=host_config(HOST)).resources
        assert tables is not None
        self.assertIsNone(tables.pressure)
        caps = R.caps()
        self.assertEqual((caps.max_load_per_cpu, caps.min_mem_available_pct), (2.0, 10.0))

    def test_the_documented_pressure_table_loads(self):
        tables = resolve(
            host=host_config(
                HOST, {"pressure": {"max_load_per_cpu": 4.0, "min_mem_available_pct": 5.0}}
            )
        ).resources
        assert tables is not None
        assert tables.pressure is not None
        self.assertEqual(
            (tables.pressure.max_load_per_cpu, tables.pressure.min_mem_available_pct), (4.0, 5.0)
        )

    def test_a_partial_pressure_table_keeps_defaults(self):
        tables = resolve(host=host_config(HOST, {"pressure": {"max_load_per_cpu": 4.0}})).resources
        assert tables is not None
        assert tables.pressure is not None
        self.assertEqual(
            (tables.pressure.max_load_per_cpu, tables.pressure.min_mem_available_pct), (4.0, 10.0)
        )

    def test_whole_numbers_read_as_thresholds(self):
        tables = resolve(
            host=host_config(
                HOST, {"pressure": {"max_load_per_cpu": 4, "min_mem_available_pct": 5}}
            )
        ).resources
        assert tables is not None
        assert tables.pressure is not None
        self.assertEqual(
            (tables.pressure.max_load_per_cpu, tables.pressure.min_mem_available_pct), (4.0, 5.0)
        )

    def test_an_unknown_pressure_key_names_it(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"config\.toml.*\[resources\.pressure\].*'turbo'"
        ):
            resolve(host=host_config(HOST, {"pressure": {"turbo": True}}))

    def test_a_zero_max_names_it(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"config\.toml.*\[resources\.pressure\].*max_load_per_cpu"
        ):
            resolve(host=host_config(HOST, {"pressure": {"max_load_per_cpu": 0}}))

    def test_a_min_above_a_hundred_names_it(self):
        with self.assertRaisesRegex(
            S.SettingsError, r"config\.toml.*\[resources\.pressure\].*min_mem_available_pct"
        ):
            resolve(host=host_config(HOST, {"pressure": {"min_mem_available_pct": 101}}))

    def test_a_non_number_names_it(self):
        with self.assertRaisesRegex(S.SettingsError, r"config\.toml.*\[resources\.pressure\]"):
            resolve(host=host_config(HOST, {"pressure": {"max_load_per_cpu": "lots"}}))

    def test_an_empty_pressure_table_is_an_error(self):
        with self.assertRaisesRegex(S.SettingsError, "empty table"):
            resolve(host=host_config(HOST, {"pressure": {}}))
