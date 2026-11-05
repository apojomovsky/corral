"""Doctor rows for the remote-dependent project checks.

`docs/06-korapy-new.md`, section 6: a fresh `new` tree has no remote,
so the rows that need one warn instead of failing; once a remote exists
the rows are strict. Readings arrive as `repodoctor.RepoSite` data and
`doctor.run_checks` only renders them; `doctor.load_repo_sites`
gathers them from fixture trees with a fake git and a fake tracker, so
no test touches git, the network or the state dirs.
"""

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from korapy import doctor, repodoctor, settings
from korapy import tracker as iface
from tests.test_doctor import env
from tests.test_forge import FakeForge
from tests.test_init import FakeGit
from tests.test_new import FIXTURE, isolated_config, run_new
from tests.test_tracker import FakeTracker

NAMES = [label.name for label in iface.BOOTSTRAP_LABELS]


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
        self.assertIn("korapy github setup", found["remote:demo"].fix)
        self.assertIn("--create-remote", found["remote:demo"].fix)
        self.assertIn("korapy github setup", found["labels:demo"].fix)

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
        self.assertIn("korapy github setup", found["labels:demo"].fix)
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


class UnreachedRemote(unittest.TestCase):
    def test_labels_unknown_keeps_the_unreachable_warning(self):
        checks = rows(site(origin="https://example.com/o/demo.git"))
        found = by_name(checks)
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertIn("could not be reached", found["remote:demo"].detail)
        self.assertEqual(doctor.exit_code(checks), 0)


class RowOrder(unittest.TestCase):
    def test_each_site_reads_remote_then_labels(self):
        self.assertEqual(
            [row.name for row in repodoctor.check_sites((site(name="a"), site(name="b")))],
            ["remote:a", "labels:a", "remote:b", "labels:b"],
        )


def write_ops(root, name="demo", body=None):
    """An ops root with one `[[repo]]` entry pointing at a `clone` dir."""
    os.makedirs(os.path.join(root, "clone"), exist_ok=True)
    with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
        handle.write(body or f'[[repo]]\nname = "{name}"\nclone.path = "clone"\n')
    return os.path.join(root, "clone")


def chdir(test, path):
    here = os.getcwd()
    os.chdir(path)
    test.addCleanup(os.chdir, here)


class LoadRepoSites(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-repodoctor-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.clone = write_ops(self.root)
        chdir(self, self.root)

    def load(self, git=None, factory=None, settings_error=None):
        if factory is None:
            factory = {}.__getitem__
        return doctor.load_repo_sites(
            settings_error,
            git=git if git is not None else FakeGit(root=self.clone),
            tracker_factory=factory,
        )

    def test_settings_failure_means_no_sites(self):
        self.assertEqual(
            self.load(
                settings_error="boom",
                factory=lambda repo: self.fail("no tracker read without settings"),
            ),
            (),
        )

    def test_no_root_means_no_sites(self):
        bare = tempfile.mkdtemp(prefix="korapy-repodoctor-bare-")
        self.addCleanup(lambda: os.rmdir(bare))
        chdir(self, bare)
        self.assertEqual(self.load(), ())

    def test_origin_unset_warns_both_rows_with_the_fix_line(self):
        sites = self.load()
        self.assertEqual(sites, (repodoctor.RepoSite("demo", None),))
        found = by_name(rows(*sites))
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertIn("--create-remote", found["remote:demo"].fix)
        self.assertIn("korapy github setup", found["remote:demo"].fix)
        self.assertEqual(found["labels:demo"].status, "warn")
        self.assertIn("korapy github setup", found["labels:demo"].fix)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_origin_read_is_local_only(self):
        git = FakeGit(root=self.clone)
        self.load(git=git)
        self.assertEqual(git.calls, [(["remote", "get-url", "origin"], self.clone)])

    def test_origin_with_labels_and_an_advertised_branch_is_ok(self):
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels=[("acme/demo", NAMES)])
        sites = self.load(
            git=FakeGit(root=self.clone, origin_url=url),
            factory={"acme/demo": fake}.__getitem__,
        )
        self.assertEqual(sites, (repodoctor.RepoSite("demo", url, branch="ok", labels="ok"),))
        found = by_name(rows(*sites))
        self.assertEqual(found["labels:demo"].status, "ok")
        self.assertEqual(found["remote:demo"].status, "ok")
        self.assertIn("exists on the remote", found["remote:demo"].detail)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_branch_read_is_one_ls_remote_call(self):
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels=[("acme/demo", NAMES)])
        git = FakeGit(root=self.clone, origin_url=url)
        self.load(
            git=git,
            factory={"acme/demo": fake}.__getitem__,
        )
        self.assertEqual(
            git.calls,
            [
                (["remote", "get-url", "origin"], self.clone),
                (["ls-remote", "--symref", url, "HEAD"], self.clone),
            ],
        )

    def test_deleted_default_branch_fails_the_remote_row_despite_ok_labels(self):
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels=[("acme/demo", NAMES)])
        sites = self.load(
            git=FakeGit(root=self.clone, origin_url=url, remote_head=None),
            factory={"acme/demo": fake}.__getitem__,
        )
        self.assertEqual(sites[0].branch, "missing")
        found = by_name(rows(*sites))
        self.assertEqual(found["labels:demo"].status, "ok")
        self.assertEqual(found["remote:demo"].status, "fail")
        self.assertIn("default branch", found["remote:demo"].detail)
        self.assertIn("push the default branch", found["remote:demo"].fix)
        self.assertEqual(doctor.exit_code(rows(*sites)), 1)

    def test_failed_branch_read_warns_naming_the_cause(self):
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels=[("acme/demo", NAMES)])
        sites = self.load(
            git=FakeGit(root=self.clone, origin_url=url, fail={"ls-remote"}),
            factory={"acme/demo": fake}.__getitem__,
        )
        self.assertEqual(len(sites), 1)
        self.assertIsNotNone(sites[0].cause)
        found = by_name(rows(*sites))
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertIn("default branch", found["remote:demo"].detail)
        self.assertEqual(found["labels:demo"].status, "ok")
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_unrunnable_branch_read_warns_naming_the_cause(self):
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels=[("acme/demo", NAMES)])

        def git(args, cwd):
            if args == ["remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout=url + "\n", stderr="")
            self.assertEqual(args[:2], ["ls-remote", "--symref"])
            return None

        sites = self.load(git=git, factory={"acme/demo": fake}.__getitem__)
        self.assertIsNotNone(sites[0].cause)
        found = by_name(rows(*sites))
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertIn("default branch", found["remote:demo"].detail)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_no_such_remote_reads_as_no_remote(self):
        def git(args, cwd):
            self.assertEqual(args, ["remote", "get-url", "origin"])
            return SimpleNamespace(
                returncode=1, stdout="", stderr="error: No such remote 'origin'\n"
            )

        sites = self.load(git=git)
        self.assertEqual(sites, (repodoctor.RepoSite("demo", None),))
        found = by_name(rows(*sites))
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertIn("--create-remote", found["remote:demo"].fix)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_not_a_repository_warns_with_the_stderr_and_the_reinit_hint(self):
        def git(args, cwd):
            self.assertEqual(args, ["remote", "get-url", "origin"])
            return SimpleNamespace(
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository (or any of the parent directories): .git\n",
            )

        sites = self.load(git=git)
        self.assertEqual(len(sites), 1)
        self.assertIsNone(sites[0].origin)
        self.assertIsNotNone(sites[0].cause)
        found = by_name(rows(*sites))
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertIn("demo", found["remote:demo"].detail)
        self.assertIn("not a git repository", found["remote:demo"].detail)
        self.assertIn("re-init", found["remote:demo"].detail)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_origin_set_with_a_missing_label_fails_with_the_setup_fix(self):
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels=[("acme/demo", [name for name in NAMES if name != "hold"])])
        sites = self.load(
            git=FakeGit(root=self.clone, origin_url=url),
            factory={"acme/demo": fake}.__getitem__,
        )
        self.assertEqual(sites[0].labels, "missing")
        found = by_name(rows(*sites))
        self.assertEqual(found["labels:demo"].status, "fail")
        self.assertIn("korapy github setup", found["labels:demo"].fix)
        self.assertEqual(doctor.exit_code(rows(*sites)), 1)

    def test_unreachable_tracker_warns_naming_the_repo_and_the_cause(self):
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels_error=iface.TrackerError("the tracker is unreachable: boom"))
        sites = self.load(
            git=FakeGit(root=self.clone, origin_url=url),
            factory={"acme/demo": fake}.__getitem__,
        )
        self.assertEqual(len(sites), 1)
        found = by_name(rows(*sites))
        self.assertEqual(found["labels:demo"].status, "warn")
        self.assertIn("demo", found["labels:demo"].detail)
        self.assertIn("boom", found["labels:demo"].detail)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_unknown_tracker_warns_naming_the_repo_and_the_cause(self):
        url = "https://github.com/acme/demo.git"
        sites = self.load(git=FakeGit(root=self.clone, origin_url=url))
        found = by_name(rows(*sites))
        self.assertEqual(found["labels:demo"].status, "warn")
        self.assertIn("acme/demo", found["labels:demo"].detail)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_explicit_tracker_repo_skips_the_origin_parse(self):
        write_ops(
            self.root,
            body='[[repo]]\nname = "demo"\nclone.path = "clone"\ntracker.repo = "acme/app"\n',
        )
        fake = FakeTracker(labels=[("acme/app", NAMES)])
        sites = self.load(
            git=FakeGit(root=self.clone, origin_url="/srv/repos/demo"),
            factory={"acme/app": fake}.__getitem__,
        )
        self.assertEqual(sites[0].labels, "ok")
        self.assertEqual(by_name(rows(*sites))["labels:demo"].status, "ok")

    def test_unresolvable_tracker_repo_warns_with_the_cause(self):
        sites = self.load(git=FakeGit(root=self.clone, origin_url="/srv/repos/demo"))
        found = by_name(rows(*sites))
        self.assertEqual(found["labels:demo"].status, "warn")
        self.assertIn("demo", found["labels:demo"].detail)
        self.assertIn("owner/name", found["labels:demo"].detail)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)

    def test_a_repo_whose_settings_fail_warns_with_the_cause(self):
        with mock.patch.object(
            doctor.settings,
            "load",
            side_effect=settings.SettingsError("cannot resolve [[repo]] demo: boom"),
        ):
            sites = self.load(factory=lambda repo: self.fail("no tracker read without settings"))
        self.assertEqual(len(sites), 1)
        found = by_name(rows(*sites))
        self.assertEqual(found["remote:demo"].status, "warn")
        self.assertIn("boom", found["remote:demo"].detail)
        self.assertEqual(doctor.exit_code(rows(*sites)), 0)


class FreshScaffoldEndToEnd(unittest.TestCase):
    def test_a_fresh_scaffold_shows_the_pre_remote_rows(self):
        parent = tempfile.mkdtemp(prefix="korapy-repodoctor-new-")
        self.addCleanup(shutil.rmtree, parent, True)
        isolated_config(self)
        code, _, err = run_new(name=FIXTURE, path=parent, git=FakeGit(root=parent))
        self.assertEqual((code, err), (0, ""))
        target = os.path.join(parent, FIXTURE)
        self.assertEqual(settings.repo_names(target), [FIXTURE])
        chdir(self, target)

        def unused(repo):
            self.fail(f"no tracker read without a remote: {repo}")

        sites = doctor.load_repo_sites(None, git=FakeGit(root=target), tracker_factory=unused)
        self.assertEqual(sites, (repodoctor.RepoSite(FIXTURE, None),))
        checks = doctor.run_checks(env(repo_sites=sites))
        found = by_name(checks)
        self.assertEqual(found[f"remote:{FIXTURE}"].status, "warn")
        self.assertEqual(found[f"labels:{FIXTURE}"].status, "warn")
        self.assertIn("korapy github setup", found[f"labels:{FIXTURE}"].fix)
        self.assertEqual(doctor.exit_code(checks), 0)

    def test_after_create_remote_and_setup_doctor_shows_no_warning(self):
        parent = tempfile.mkdtemp(prefix="korapy-repodoctor-setup-")
        self.addCleanup(shutil.rmtree, parent, True)
        isolated_config(self)
        forge = FakeForge(user="operator")
        code, _, err = run_new(
            name=FIXTURE,
            path=parent,
            git=FakeGit(root=parent),
            forge=forge,
            tracker=FakeTracker(),
            create_remote=True,
            owner="acme",
        )
        self.assertEqual((code, err), (0, ""))
        target = os.path.join(parent, FIXTURE)
        chdir(self, target)
        url = "https://github.com/acme/demo.git"
        fake = FakeTracker(labels=[("acme/demo", NAMES)])
        sites = doctor.load_repo_sites(
            None,
            git=FakeGit(root=target, origin_url=url),
            tracker_factory={"acme/demo": fake}.__getitem__,
        )
        self.assertEqual(sites, (repodoctor.RepoSite(FIXTURE, url, branch="ok", labels="ok"),))
        checks = doctor.run_checks(env(repo_sites=sites))
        found = by_name(checks)
        self.assertEqual(found[f"remote:{FIXTURE}"].status, "ok")
        self.assertEqual(found[f"labels:{FIXTURE}"].status, "ok")
        self.assertEqual(doctor.exit_code(checks), 0)


if __name__ == "__main__":
    unittest.main()
