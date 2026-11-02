"""Doctor rows for the remote-dependent project checks.

`docs/06-corral-new.md`, section 6: a fresh `new` tree has no remote,
so the rows that need one warn instead of failing; once a remote exists
the rows are strict. Readings arrive as `repodoctor.RepoSite` data and
`doctor.run_checks` only renders them, so no test touches git, the
network or the state dirs.
"""

import unittest

from corral import doctor, repodoctor
from tests.test_doctor import env


def site(**over):
    base = {"name": "demo", "origin": None, "branch": "unknown", "labels": "unknown"}
    base.update(over)
    return repodoctor.RepoSite(**base)


def rows(*sites):
    return doctor.run_checks(env(repo_sites=sites))


def by_name(checks):
    return {check.name: check for check in checks}


class NoSites(unittest.TestCase):
    def test_no_sites_means_no_rows(self):
        self.assertEqual(repodoctor.check_sites(()), [])

    def test_default_env_carries_no_remote_row(self):
        self.assertNotIn("remote:demo", by_name(doctor.run_checks(env())))
        self.assertNotIn("labels:demo", by_name(doctor.run_checks(env())))


class PreRemoteWarnings(unittest.TestCase):
    def test_no_remote_warns_both_rows_and_exits_zero(self):
        checks = rows(site())
        found = by_name(checks)
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertEqual(found["labels:demo"].status, "warn")
        self.assertIn("no remote is set", found["remote:demo"].detail)
        self.assertIn("no remote is set", found["labels:demo"].detail)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_no_remote_fixes_name_the_manual_next_steps(self):
        found = by_name(rows(site()))
        self.assertIn("corral github setup", found["remote:demo"].fix)
        self.assertIn("--create-remote", found["remote:demo"].fix)
        self.assertIn("corral github setup", found["labels:demo"].fix)

    def test_readings_are_ignored_without_an_origin(self):
        checks = rows(site(branch="missing", labels="missing"))
        found = by_name(checks)
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertEqual(found["labels:demo"].status, "warn")
        self.assertEqual(doctor.exit_code(checks), 0)


class StrictRemote(unittest.TestCase):
    def test_ok_readings_are_ok(self):
        checks = rows(site(origin="https://example.com/o/demo.git", branch="ok", labels="ok"))
        found = by_name(checks)
        self.assertEqual(found["remote:demo"].status, "ok")
        self.assertEqual(found["labels:demo"].status, "ok")
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_missing_branch_fails(self):
        checks = rows(site(origin="https://example.com/o/demo.git", branch="missing"))
        found = by_name(checks)
        self.assertEqual(found["remote:demo"].status, "fail")
        self.assertIn("default branch", found["remote:demo"].detail)
        self.assertIn("push the default branch", found["remote:demo"].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_missing_labels_fails(self):
        checks = rows(site(origin="https://example.com/o/demo.git", labels="missing"))
        found = by_name(checks)
        self.assertEqual(found["labels:demo"].status, "fail")
        self.assertIn("corral github setup", found["labels:demo"].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_refused_labels_fails_with_the_owner_fix(self):
        checks = rows(site(origin="https://example.com/o/demo.git", labels="refused"))
        found = by_name(checks)
        self.assertEqual(found["labels:demo"].status, "fail")
        self.assertIn("ask an owner", found["labels:demo"].fix)
        self.assertEqual(doctor.exit_code(checks), 1)

    def test_unreachable_remote_warns(self):
        checks = rows(site(origin="https://example.com/o/demo.git"))
        found = by_name(checks)
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertEqual(found["labels:demo"].status, "warn")
        self.assertEqual(doctor.exit_code(checks), 0)


class RowOrder(unittest.TestCase):
    def test_each_site_reads_remote_then_labels(self):
        self.assertEqual(
            [row.name for row in repodoctor.check_sites((site(name="a"), site(name="b")))],
            ["remote:a", "labels:a", "remote:b", "labels:b"],
        )


if __name__ == "__main__":
    unittest.main()
