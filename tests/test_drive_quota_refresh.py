"""The drive pass refreshes stale quota readings through the reader seam.

Offline: fake readers through the injected loader, temporary board
stores, injected pool and ledger dicts. No network, no real state dir.
"""

import io
import os
import tempfile
import unittest

from korapy import accounts, admission, drive, projects, quota_ledger
from tests.test_drive import driven_for, policy_source, stored


T0 = 2000.0
AGE = 900.0


def provider(name="provider-a", stale_after_s=accounts.DEFAULT_STALE_AFTER_S):
    return accounts.Provider(name=name, reader=f"fake:{name}", stale_after_s=stale_after_s)


def windows(used=10.0):
    return {"weekly": accounts.Window(used_pct=used, resets_at=None)}


class CountingReader:
    """A fake reader that counts its reads and reports one window."""

    def __init__(self, used=10.0):
        self.calls = 0
        self.used = used

    def read(self, now):
        self.calls += 1
        return accounts.Reading(
            windows={"weekly": accounts.Window(used_pct=self.used, resets_at=None)}
        )

    def parse_limit_error(self, text, now):
        return None


class FailingReader:
    """A fake reader whose read fails at call time."""

    def __init__(self):
        self.calls = 0

    def read(self, now):
        self.calls += 1
        raise accounts.ReaderError("fake: the quota endpoint refused")

    def parse_limit_error(self, text, now):
        return None


class HangingReader:
    """A fake reader that times out instead of answering."""

    def __init__(self):
        self.calls = 0

    def read(self, now):
        self.calls += 1
        raise TimeoutError("fake: the quota endpoint timed out")

    def parse_limit_error(self, text, now):
        return None


def loader_for(reader):
    def load(entry):
        return reader

    return load


def ledger_with(entry):
    return {"readings": {"provider-a": entry}, "marks": {}}


def fresh_entry(at=T0):
    return {"windows": {"weekly": {"used_pct": 10.0, "resets_at": None}}, "read_at": at}


class StaleSelectionTest(unittest.TestCase):
    def test_a_fresh_reading_is_never_re_read(self):
        reader = CountingReader()
        fresh, results = drive.refresh_stale(
            [provider()],
            ledger_with(fresh_entry(at=T0)),
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 0)
        self.assertEqual(results, {})
        self.assertEqual(fresh, ledger_with(fresh_entry(at=T0)))

    def test_a_missing_reading_is_read(self):
        reader = CountingReader()
        fresh, results = drive.refresh_stale(
            [provider()],
            {"readings": {}, "marks": {}},
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(results, {"provider-a": (True, None)})
        self.assertEqual(fresh["readings"]["provider-a"]["read_at"], T0)

    def test_a_reading_older_than_the_age_is_re_read_once(self):
        reader = CountingReader()
        fresh, results = drive.refresh_stale(
            [provider()],
            ledger_with(fresh_entry(at=T0 - AGE - 1)),
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(results, {"provider-a": (True, None)})
        self.assertEqual(fresh["readings"]["provider-a"]["read_at"], T0)

    def test_a_reading_within_the_age_is_left_alone(self):
        reader = CountingReader()
        _, results = drive.refresh_stale(
            [provider()],
            ledger_with(fresh_entry(at=T0 - AGE + 60)),
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 0)
        self.assertEqual(results, {})

    def test_an_old_failure_is_re_read(self):
        reader = CountingReader()
        old = {"error": "fake: gone", "at": T0 - AGE - 1}
        fresh, results = drive.refresh_stale(
            [provider()],
            ledger_with(old),
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(results, {"provider-a": (True, None)})
        self.assertIn("windows", fresh["readings"]["provider-a"])

    def test_only_stale_providers_are_read(self):
        good = CountingReader()
        bad = CountingReader()
        readers = {"fake:provider-a": good, "fake:provider-b": bad}
        ledger = {
            "readings": {"provider-a": fresh_entry(at=T0), "provider-b": fresh_entry(at=1.0)},
            "marks": {},
        }
        _, results = drive.refresh_stale(
            [provider("provider-a"), provider("provider-b")],
            ledger,
            T0,
            refresh_after_s=AGE,
            load_reader=lambda entry: readers[entry],
        )
        self.assertEqual(good.calls, 0)
        self.assertEqual(bad.calls, 1)
        self.assertEqual(set(results), {"provider-b"})


class FailureRecordingTest(unittest.TestCase):
    def test_a_reader_error_is_recorded_never_raised(self):
        reader = FailingReader()
        fresh, results = drive.refresh_stale(
            [provider()],
            {"readings": {}, "marks": {}},
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        entry = fresh["readings"]["provider-a"]
        self.assertEqual(entry["error"], "fake: the quota endpoint refused")
        self.assertEqual(entry["at"], T0)
        self.assertEqual(results, {"provider-a": (False, "fake: the quota endpoint refused")})

    def test_a_timeout_is_recorded_never_raised(self):
        reader = HangingReader()
        fresh, results = drive.refresh_stale(
            [provider()],
            {"readings": {}, "marks": {}},
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        entry = fresh["readings"]["provider-a"]
        self.assertIn("timed out", entry["error"])
        self.assertEqual(results["provider-a"][0], False)

    def test_a_reader_that_does_not_build_is_recorded_never_raised(self):
        def load(entry):
            raise accounts.AccountsError("accounts: reader 'fake:x' does not import")

        fresh, results = drive.refresh_stale(
            [provider()],
            {"readings": {}, "marks": {}},
            T0,
            refresh_after_s=AGE,
            load_reader=load,
        )
        self.assertIn("does not import", fresh["readings"]["provider-a"]["error"])
        self.assertEqual(results["provider-a"][0], False)

    def test_garbage_windows_are_recorded_never_raised(self):
        class GarbageReader:
            def __init__(self):
                self.calls = 0

            def read(self, now):
                self.calls += 1
                return {"windows": {"weekly": {"used_pct": "high", "resets_at": None}}}

            def parse_limit_error(self, text, now):
                return None

        reader = GarbageReader()
        fresh, results = drive.refresh_stale(
            [provider()],
            {"readings": {}, "marks": {}},
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(results["provider-a"][0], False)
        entry = fresh["readings"]["provider-a"]
        self.assertIn("error", entry)
        self.assertEqual(entry["at"], T0)

    def test_a_failed_refresh_keeps_todays_admission_behavior(self):
        from korapy import quota

        reader = FailingReader()
        fresh, _ = drive.refresh_stale(
            [provider()],
            {"readings": {}, "marks": {}},
            T0,
            refresh_after_s=AGE,
            load_reader=loader_for(reader),
        )
        verdict = quota.gate(
            ["worker"],
            {"worker": ["provider-a/model"]},
            fresh,
            T0,
            stale_after=AGE,
            unknown_blocks=True,
        )
        self.assertTrue(verdict["shut"])


class PassRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-quota-pass-"))
        self.db = os.path.join(self.tmp, "board.sqlite")
        self.lock = os.path.join(self.tmp, "drive.lock")
        self.policies = os.path.join(self.tmp, "policies")
        os.makedirs(self.policies)

    def write_policy(self, name, wants):
        path = os.path.join(self.policies, name + ".py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(policy_source(wants))
        return path + ":eligible"

    def project(self, name, entry):
        return projects.Project(
            name=name,
            unit_prefix=name + "-agent-",
            root=os.path.join(self.tmp, "ops", name),
            eligibility=entry,
        )

    def run_pass(self, store, found, **fields):
        out, err = io.StringIO(), io.StringIO()
        values = {
            "plan": False,
            "store": store,
            "lock_path": self.lock,
            "out": out,
            "err": err,
            "now": T0,
            "projects_fn": lambda: found,
            "live": frozenset(),
            "limits": admission.Limits(
                total=4, projects={p.name: {"floor": 0, "ceiling": None} for p in found}
            ),
            "read_labels": lambda kind, repo, number: [],
            "hold_label": "hold",
            "present_agents": frozenset(),
            "live_units": frozenset(),
            "gone_s": 3600.0,
        }
        values.update(fields)
        values.setdefault("driven", driven_for(found, store))
        code = drive.run_pass(**values)
        return code, out.getvalue(), err.getvalue()

    def quotas(self, **fields):
        values = {"stale_after": AGE, "low_pct": 90, "unknown_blocks": True}
        values.update(fields)
        return {"blue": drive.QuotaSource(chains={"worker": ["provider-a/model"]}, **values)}

    def test_a_stale_reading_blocks_when_unknown_blocks(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        old = {
            "windows": {"weekly": {"used_pct": 10.0, "resets_at": None}},
            "read_at": T0 - 6 * 86400,
        }
        code, out, _ = self.run_pass(
            store,
            [self.project("blue", entry)],
            quotas=self.quotas(),
            ledger={"readings": {"provider-a": old}, "marks": {}},
            pool=[provider()],
        )
        self.assertEqual(code, 0)
        self.assertIn("app#7: skip", out)
        self.assertIn("quota", out)

    def test_the_pass_refreshes_a_stale_reading_before_gating(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        old = {
            "windows": {"weekly": {"used_pct": 10.0, "resets_at": None}},
            "read_at": T0 - 6 * 86400,
        }
        reader = CountingReader()
        code, out, err = self.run_pass(
            store,
            [self.project("blue", entry)],
            quotas=self.quotas(),
            ledger={"readings": {"provider-a": old}, "marks": {}},
            pool=[provider()],
            quota_refresh_after_s=AGE,
            quota_load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(code, 0)
        self.assertIn("app#7: start", out)
        self.assertNotIn("quota", err)

    def test_a_failed_refresh_keeps_the_pass_going_with_the_old_reading(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        old = {
            "windows": {"weekly": {"used_pct": 10.0, "resets_at": None}},
            "read_at": T0 - 6 * 86400,
        }
        reader = FailingReader()
        code, out, err = self.run_pass(
            store,
            [self.project("blue", entry)],
            quotas=self.quotas(),
            ledger={"readings": {"provider-a": old}, "marks": {}},
            pool=[provider()],
            quota_refresh_after_s=AGE,
            quota_load_reader=loader_for(reader),
        )
        self.assertEqual(code, 0)
        self.assertIn("app#7: skip", out)
        self.assertIn("quota", out)
        self.assertIn("provider-a", err)

    def test_the_plan_pass_never_runs_a_reader(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        reader = CountingReader()
        code, out, err = self.run_pass(
            store,
            [self.project("blue", entry)],
            plan=True,
            quotas=self.quotas(),
            ledger={"readings": {}, "marks": {}},
            pool=[provider()],
            quota_refresh_after_s=AGE,
            quota_load_reader=loader_for(reader),
        )
        self.assertEqual(code, 0)
        self.assertEqual(reader.calls, 0)
        self.assertEqual(err, "")

    def test_no_refresh_runs_without_the_age(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        reader = CountingReader()
        code, _, err = self.run_pass(
            store,
            [self.project("blue", entry)],
            quotas=self.quotas(),
            ledger={"readings": {}, "marks": {}},
            pool=[provider()],
            quota_load_reader=loader_for(reader),
        )
        self.assertEqual(code, 0)
        self.assertEqual(reader.calls, 0)
        self.assertEqual(err, "")


class PassRefreshLockBranchTest(unittest.TestCase):
    """The production branch persists through the ledger lock and gates fresh.

    Production never injects `ledger`, so the pass loads it from the
    state dir and persists through `quota_ledger.update`. These tests
    point `KORAPY_STATE` at a temp dir and omit `ledger`, covering the
    branch every injected-ledger test skips: if the branch persisted
    the fresh reading yet gated on the stale one, the first test here
    would fail.
    """

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="korapy-quota-lock-"))
        self.db = os.path.join(self.tmp, "board.sqlite")
        self.lock = os.path.join(self.tmp, "drive.lock")
        self.policies = os.path.join(self.tmp, "policies")
        os.makedirs(self.policies)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        old = os.environ.get("KORAPY_STATE")
        os.environ["KORAPY_STATE"] = self.state
        self.addCleanup(self._restore_state, old)

    def _restore_state(self, old):
        if old is None:
            os.environ.pop("KORAPY_STATE", None)
        else:
            os.environ["KORAPY_STATE"] = old

    def write_policy(self, name, wants):
        path = os.path.join(self.policies, name + ".py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(policy_source(wants))
        return path + ":eligible"

    def project(self, name, entry):
        return projects.Project(
            name=name,
            unit_prefix=name + "-agent-",
            root=os.path.join(self.tmp, "ops", name),
            eligibility=entry,
        )

    def quotas(self, **fields):
        values = {"stale_after": AGE, "low_pct": 90, "unknown_blocks": True}
        values.update(fields)
        return {"blue": drive.QuotaSource(chains={"worker": ["provider-a/model"]}, **values)}

    def run_pass(self, store, found, **fields):
        out, err = io.StringIO(), io.StringIO()
        values = {
            "plan": False,
            "store": store,
            "lock_path": self.lock,
            "out": out,
            "err": err,
            "now": T0,
            "projects_fn": lambda: found,
            "live": frozenset(),
            "limits": admission.Limits(
                total=4, projects={p.name: {"floor": 0, "ceiling": None} for p in found}
            ),
            "read_labels": lambda kind, repo, number: [],
            "hold_label": "hold",
            "present_agents": frozenset(),
            "live_units": frozenset(),
            "gone_s": 3600.0,
        }
        values.update(fields)
        values.setdefault("driven", driven_for(found, store))
        code = drive.run_pass(**values)
        return code, out.getvalue(), err.getvalue()

    def ledger_file(self):
        return os.path.join(self.state, quota_ledger.LEDGER_NAME)

    def test_a_stale_disk_reading_is_rewritten_and_gates_fresh(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        old = {
            "windows": {"weekly": {"used_pct": 10.0, "resets_at": None}},
            "read_at": T0 - 6 * 86400,
        }
        quota_ledger.save({"readings": {"provider-a": old}, "marks": {}})
        reader = CountingReader()
        code, out, err = self.run_pass(
            store,
            [self.project("blue", entry)],
            quotas=self.quotas(),
            pool=[provider()],
            quota_refresh_after_s=AGE,
            quota_load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(code, 0)
        self.assertIn("app#7: start", out)
        self.assertNotIn("quota", err)
        reread = quota_ledger.load()
        self.assertEqual(reread["readings"]["provider-a"]["read_at"], T0)

    def test_a_fresh_disk_reading_leaves_the_file_alone(self):
        store = stored(self.db, [("app", 7, "open")])
        self.addCleanup(store.close)
        entry = self.write_policy("blue_policy", [("app", 7, "start")])
        quota_ledger.save({"readings": {"provider-a": fresh_entry(at=T0)}, "marks": {}})
        with open(self.ledger_file(), encoding="utf-8") as handle:
            before = handle.read()
        reader = CountingReader()
        code, out, _ = self.run_pass(
            store,
            [self.project("blue", entry)],
            quotas=self.quotas(),
            pool=[provider()],
            quota_refresh_after_s=AGE,
            quota_load_reader=loader_for(reader),
        )
        self.assertEqual(reader.calls, 0)
        self.assertEqual(code, 0)
        self.assertIn("app#7: start", out)
        with open(self.ledger_file(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
