"""Review records are stored under the short repo name, keyed by owner/repo.

The ticket session dir (and the review records beside it) is named for
the project's short ``[[repo]]`` entry name (``app``), while the board
store, the feed and the drive loop's items are keyed by the tracker's
``owner/repo`` (``acme/app``). Every lookup between the two goes
through the one short-name resolver; a lookup that cannot resolve a
repository is a loud line naming the key, never a silent empty result.

Production-shaped throughout: records are written through the
production writer, the store rows come from a temporary board store,
and the project mapping comes from a real ``korapy.toml`` plus
``tracker.repo``. A git seam that must never fire proves no origin
lookup runs. No ``gh``, no systemd, no network, no real state dir.
"""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from korapy import drive, mergecli, reviewcli, reviewrecord, syncli
from korapy import projects as projects_mod
from korapy.eligibility import StartRequest, TicketRow
from tests.test_drive import stored
from tests.test_drive_gates import gate as gate_candidate
from tests.test_forge import FakeForge, make_pr
from tests.test_reviewrecord import git_in

SHORT = "app"
TRACKER = "acme/app"
NUMBER = 416
COMMIT = "a" * 40

TOML = """\
[[repo]]
name = "app"
clone.path = "clone"
tracker.repo = "acme/app"
"""


def dead_git(args, cwd):
    """A git seam that must never fire: the fixture names its tracker repo."""
    raise AssertionError(f"unexpected git call: {args!r} in {cwd!r}")


def registered(root):
    return [
        projects_mod.Project(name="blue", unit_prefix="blue-agent-", root=root),
    ]


def write_record(sessions, commit=COMMIT, verdict="approve", findings=(), number=NUMBER):
    """One review record the way `review record` writes it: short name on disk."""
    return reviewrecord.write_record(
        SHORT,
        number,
        commit,
        reviewer="reviewer-1",
        author="worker-1",
        verdict=verdict,
        findings=list(findings),
        sessions_root=sessions,
    )


class LocalRepoTest(unittest.TestCase):
    def test_a_known_short_name_reads_as_itself(self):
        self.assertEqual(reviewrecord.local_repo(SHORT, {SHORT: TRACKER}), SHORT)

    def test_a_tracker_repo_reads_as_its_short_name(self):
        self.assertEqual(reviewrecord.local_repo(TRACKER, {SHORT: TRACKER}), SHORT)

    def test_an_unknown_short_name_keeps_itself(self):
        self.assertEqual(reviewrecord.local_repo("legacy", {SHORT: TRACKER}), "legacy")

    def test_an_unknown_tracker_repo_fails_naming_the_key(self):
        with self.assertRaises(ValueError) as caught:
            reviewrecord.local_repo("acme/gone", {SHORT: TRACKER})
        self.assertIn("acme/gone", str(caught.exception))

    def test_a_blank_repo_fails_naming_nothing(self):
        with self.assertRaises(ValueError):
            reviewrecord.local_repo("", {SHORT: TRACKER})

    def test_the_first_short_name_wins_for_a_shared_tracker_repo(self):
        mapping = {"first": TRACKER, "second": TRACKER}
        self.assertEqual(reviewrecord.local_repo(TRACKER, mapping), "first")


class StoreReaderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-record-key-")
        self.addCleanup(tmp.cleanup)
        self.sessions = os.path.join(tmp.name, "sessions")
        os.makedirs(self.sessions)

    def test_a_store_key_finds_the_record_written_under_the_short_name(self):
        write_record(self.sessions)
        read = reviewrecord.store_reader({SHORT: TRACKER}, sessions_root=self.sessions)
        record = read(TRACKER, NUMBER)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["verdict"], "approve")
        self.assertEqual(record["reviewer"], "reviewer-1")

    def test_a_short_name_still_reads_its_own_records(self):
        write_record(self.sessions)
        read = reviewrecord.store_reader({SHORT: TRACKER}, sessions_root=self.sessions)
        self.assertIsNotNone(read(SHORT, NUMBER))

    def test_an_unresolvable_store_key_raises_naming_the_key(self):
        read = reviewrecord.store_reader({SHORT: TRACKER}, sessions_root=self.sessions)
        with self.assertRaises(ValueError) as caught:
            read("acme/gone", NUMBER)
        self.assertIn("acme/gone", str(caught.exception))


class ReviewRecordsForTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-records-for-")
        self.addCleanup(tmp.cleanup)
        self.sessions = os.path.join(tmp.name, "sessions")
        os.makedirs(self.sessions)
        self.db = os.path.join(tmp.name, "board.sqlite")

    def test_a_tracker_row_finds_the_record_written_under_the_short_name(self):
        write_record(self.sessions)
        store = stored(self.db, [(TRACKER, NUMBER, "open")])
        self.addCleanup(store.close)
        read = reviewrecord.store_reader({SHORT: TRACKER}, sessions_root=self.sessions)
        found = drive.review_records_for(store.items(), read=read, err=io.StringIO())
        self.assertEqual(set(found), {(TRACKER, NUMBER)})
        self.assertEqual(found[(TRACKER, NUMBER)]["verdict"], "approve")

    def test_an_unresolvable_row_is_a_loud_line_naming_the_key(self):
        store = stored(self.db, [("acme/gone", NUMBER, "open")])
        self.addCleanup(store.close)
        read = reviewrecord.store_reader({SHORT: TRACKER}, sessions_root=self.sessions)
        err = io.StringIO()
        found = drive.review_records_for(store.items(), read=read, err=err)
        self.assertEqual(found, {})
        self.assertIn("acme/gone#416", err.getvalue())

    def test_a_failing_read_is_a_loud_line_never_a_silent_gap(self):
        store = stored(self.db, [(TRACKER, NUMBER, "open")])
        self.addCleanup(store.close)

        def read(repo, number):
            raise RuntimeError("disk hiccup")

        err = io.StringIO()
        found = drive.review_records_for(store.items(), read=read, err=err)
        self.assertEqual(found, {})
        self.assertIn(f"{TRACKER}#{NUMBER}", err.getvalue())


class DriveGateKeyTest(unittest.TestCase):
    """The fix-round and ready-to-merge decisions over a store-keyed record."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-gate-key-")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "ops")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.sessions = os.path.join(tmp.name, "sessions")
        os.makedirs(self.sessions)
        self.db = os.path.join(tmp.name, "board.sqlite")

    def reviews(self, **fields):
        write_record(self.sessions, **fields)
        store = stored(self.db, [(TRACKER, NUMBER, "open")])
        self.addCleanup(store.close)
        with mock.patch.dict(os.environ, {"KORAPY_SESSIONS": self.sessions}):
            read = drive.production_review_reader(registered(self.root), git=dead_git)
            return drive.review_records_for(store.items(), read=read, err=io.StringIO())

    def candidate(self):
        return drive.GateInput(
            project="blue",
            request=StartRequest(repo=TRACKER, number=NUMBER, kind="start", weight=0.0),
            row=TicketRow(
                repo=TRACKER,
                number=NUMBER,
                state="open",
                agent="",
                priority="",
                labels=(),
                external=False,
            ),
            wait=1000.0,
        )

    def test_a_record_with_only_lows_is_merge_ready(self):
        reviews = self.reviews(findings=[{"severity": "low", "text": "polish"}])
        gated = gate_candidate(self.candidate(), reviews=reviews)
        self.assertEqual(gated.verdict, "skip")
        self.assertIn("merge-ready", gated.reason)

    def test_a_record_with_a_normal_finding_is_not_merge_ready(self):
        reviews = self.reviews(findings=[{"severity": "normal", "text": "fix this"}])
        record = reviews[(TRACKER, NUMBER)]
        self.assertTrue(reviewrecord.fix_wanted(record, "normal"))
        gated = gate_candidate(self.candidate(), reviews=reviews)
        self.assertNotIn("merge-ready", gated.reason)
        self.assertNotEqual(gated.verdict, "skip")

    def test_no_record_is_not_merge_ready(self):
        gated = gate_candidate(self.candidate(), reviews={})
        self.assertNotIn("merge-ready", gated.reason)


class ProductionReviewReaderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-review-reader-")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "ops")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.sessions = os.path.join(tmp.name, "sessions")
        os.makedirs(self.sessions)

    def read(self, repo, number):
        with mock.patch.dict(os.environ, {"KORAPY_SESSIONS": self.sessions}):
            return drive.production_review_reader(registered(self.root), git=dead_git)(repo, number)

    def test_the_production_reader_finds_the_short_named_record(self):
        write_record(self.sessions)
        record = self.read(TRACKER, NUMBER)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["verdict"], "approve")

    def test_the_production_reader_names_an_unresolvable_key(self):
        with self.assertRaises(ValueError) as caught:
            self.read("acme/gone", NUMBER)
        self.assertIn("acme/gone", str(caught.exception))


class RecordCliKeyTest(unittest.TestCase):
    """`review record` accepts the store key and writes under the short name."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="korapy-record-cli-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.ops = os.path.join(self.tmp, "ops")
        self.clone = os.path.join(self.ops, "clone")
        os.makedirs(self.clone)
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.sessions = os.path.join(self.tmp, "sessions")
        os.environ["KORAPY_SESSIONS"] = self.sessions
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp, "state")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp, "config")
        git_in(self.clone, "init", "-q", "-b", "master")
        with open(os.path.join(self.clone, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        git_in(self.clone, "add", "-A")
        git_in(self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
        self.commit = git_in(self.clone, "rev-parse", "HEAD")
        here = os.getcwd()
        os.chdir(self.clone)
        self.addCleanup(os.chdir, here)

    def record(self, **overrides):
        base = {
            "repo": TRACKER,
            "number": 1,
            "commit": self.commit,
            "verdict": "approve",
            "finding": [],
            "reviewer": "reviewer-1",
            "author": "worker-1",
            "question": "",
            "session": "",
        }
        base.update(overrides)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = reviewcli.cmd_record(argparse.Namespace(**base))
        return code, out.getvalue(), err.getvalue()

    def test_a_store_key_writes_the_record_under_the_short_name(self):
        code, out, err = self.record()
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.strip().startswith(self.sessions + os.sep))
        record = reviewrecord.read_record(SHORT, 1, self.commit, sessions_root=self.sessions)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["repo"], SHORT)

    def test_an_unresolvable_key_fails_naming_the_key(self):
        code, _, err = self.record(repo="acme/gone")
        self.assertEqual(code, 4)
        self.assertIn("acme/gone", err)
        self.assertFalse(os.path.exists(os.path.join(self.sessions, "acme")))


class ShowCliKeyTest(unittest.TestCase):
    """`review show` finds the short-named record through the store key."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="korapy-show-cli-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.ops = os.path.join(self.tmp, "ops")
        self.clone = os.path.join(self.ops, "clone")
        os.makedirs(self.clone)
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.sessions = os.path.join(self.tmp, "sessions")
        os.environ["KORAPY_SESSIONS"] = self.sessions
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp, "state")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp, "config")
        git_in(self.clone, "init", "-q", "-b", "master")
        with open(os.path.join(self.clone, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\ntwo\nthree\n")
        git_in(self.clone, "add", "-A")
        git_in(
            self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"
        )
        git_in(self.clone, "checkout", "-q", "-b", "feat/1-x")
        with open(os.path.join(self.clone, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("feature\n")
        git_in(self.clone, "add", "-A")
        git_in(
            self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "work"
        )
        self.head = git_in(self.clone, "rev-parse", "HEAD")
        self.base = git_in(self.clone, "rev-parse", "master")
        reviewrecord.write_record(
            SHORT,
            1,
            self.head,
            reviewer="reviewer-1",
            author="worker-1",
            verdict="approve",
            sessions_root=self.sessions,
        )
        here = os.getcwd()
        os.chdir(self.clone)
        self.addCleanup(os.chdir, here)

    def show(self, **overrides):
        base = {
            "repo": TRACKER,
            "number": 1,
            "commit": "HEAD",
            "pr": 7,
            "forge_repo": TRACKER,
            "head": None,
            "json": False,
        }
        base.update(overrides)
        fake = FakeForge(
            pulls=[make_pr(repo=TRACKER, number=7, base_sha=self.base, head_sha=self.head)]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = reviewcli.cmd_show(argparse.Namespace(**base), forge=fake)
        return code, out.getvalue(), err.getvalue()

    def test_a_store_key_finds_the_short_named_record(self):
        code, out, _ = self.show()
        self.assertEqual(code, 0)
        self.assertIn("approve by reviewer-1", out)

    def test_an_unresolvable_key_fails_naming_the_key(self):
        code, _, err = self.show(repo="acme/gone")
        self.assertEqual(code, 4)
        self.assertIn("acme/gone", err)


class MergeCliKeyTest(unittest.TestCase):
    """`merge` finds the short-named record through the store key."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="korapy-merge-key-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.clone = os.path.join(self.tmp, "clone")
        os.makedirs(self.clone)
        with open(os.path.join(self.tmp, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.sessions = os.path.join(self.tmp, "sessions")
        os.environ["KORAPY_SESSIONS"] = self.sessions
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp, "state")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp, "config")
        git_in(self.clone, "init", "-q", "-b", "master")
        with open(os.path.join(self.clone, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\ntwo\nthree\n")
        git_in(self.clone, "add", "-A")
        git_in(
            self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"
        )
        git_in(self.clone, "checkout", "-q", "-b", "feat/1-x")
        with open(os.path.join(self.clone, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("feature\n")
        git_in(self.clone, "add", "-A")
        git_in(
            self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "work"
        )
        self.head = git_in(self.clone, "rev-parse", "HEAD")
        self.base = git_in(self.clone, "rev-parse", "master")
        reviewrecord.write_record(
            SHORT,
            1,
            self.head,
            reviewer="reviewer-1",
            author="worker-1",
            verdict="approve",
            sessions_root=self.sessions,
        )
        here = os.getcwd()
        os.chdir(self.clone)
        self.addCleanup(os.chdir, here)

    def merge(self, **overrides):
        base = {
            "repo": TRACKER,
            "number": 1,
            "forge_repo": TRACKER,
            "pr": 7,
            "head": self.head,
            "method": None,
            "skip": ["checks"],
            "plan": False,
            "json": False,
        }
        base.update(overrides)
        fake = FakeForge(
            pulls=[make_pr(repo=TRACKER, number=7, base_sha=self.base, head_sha=self.head)]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mergecli.cmd_merge(argparse.Namespace(**base), forge=fake)
        return code, out.getvalue(), err.getvalue()

    def test_a_store_key_merges_on_the_short_named_record(self):
        code, _, err = self.merge()
        self.assertEqual((code, err), (0, ""))

    def test_an_unresolvable_key_is_usage_naming_the_key(self):
        code, _, err = self.merge(repo="acme/gone")
        self.assertEqual(code, 4)
        self.assertIn("acme/gone", err)


class MalformedConfigTest(unittest.TestCase):
    """A malformed korapy.toml never blocks the reviewer: resolution is fail-soft.

    `short_to_tracker` documents that a configuration problem reads as
    no entries, so short names (the on-disk truth every writer uses)
    keep resolving: only a tracker spelling the empty map cannot
    translate fails, loudly, at the caller.
    """

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="korapy-malformed-key-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.ops = os.path.join(self.tmp, "ops")
        self.clone = os.path.join(self.ops, "clone")
        os.makedirs(self.clone)
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write("[[repo\nname = \n")
        self.sessions = os.path.join(self.tmp, "sessions")
        os.environ["KORAPY_SESSIONS"] = self.sessions
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp, "state")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp, "config")
        git_in(self.clone, "init", "-q", "-b", "master")
        with open(os.path.join(self.clone, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        git_in(self.clone, "add", "-A")
        git_in(self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
        self.commit = git_in(self.clone, "rev-parse", "HEAD")
        here = os.getcwd()
        os.chdir(self.clone)
        self.addCleanup(os.chdir, here)

    def test_a_broken_config_reads_as_no_entries(self):
        self.assertEqual(syncli.short_to_tracker(self.ops, dead_git), {})

    def record(self, repo):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = reviewcli.cmd_record(
                argparse.Namespace(
                    repo=repo,
                    number=1,
                    commit=self.commit,
                    verdict="approve",
                    finding=[],
                    reviewer="reviewer-1",
                    author="worker-1",
                    question="",
                    session="",
                )
            )
        return code, out.getvalue(), err.getvalue()

    def test_record_with_a_short_name_succeeds_despite_a_broken_config(self):
        code, _out, err = self.record(SHORT)
        self.assertEqual((code, err), (0, ""))
        record = reviewrecord.read_record(SHORT, 1, self.commit, sessions_root=self.sessions)
        self.assertIsNotNone(record)

    def test_an_unresolvable_key_still_fails_naming_the_key(self):
        code, _, err = self.record("acme/gone")
        self.assertEqual(code, 4)
        self.assertIn("acme/gone", err)


TWO_ENTRY_TOML = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"

[[repo]]
name = "web"
tracker.repo = "acme/app"
"""


class ResolverAgreementTest(unittest.TestCase):
    """The two short-to-tracker builders agree over the same fixture.

    `syncli.short_to_tracker` (one root, fail-soft) and
    `drive.production_repo_map` (registered roots, strict) build the
    same fact; this pins them together so they cannot drift. A
    tracker repo two shorts feed resolves to the first short name on
    both sides: the drive map keeps the first entry and `local_repo`
    takes the first match.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-resolver-agreement-")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "ops")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO_ENTRY_TOML)
        self.registered = [
            projects_mod.Project(name="blue", unit_prefix="blue-agent-", root=self.root),
        ]

    def test_both_builders_agree_on_one_root(self):
        expected = {SHORT: TRACKER, "web": TRACKER}
        self.assertEqual(syncli.short_to_tracker(self.root, dead_git), expected)
        self.assertEqual(drive.production_repo_map(self.registered, git=dead_git), expected)

    def test_a_shared_tracker_repo_resolves_to_the_first_short_name(self):
        mapping = syncli.short_to_tracker(self.root, dead_git)
        self.assertEqual(reviewrecord.local_repo(TRACKER, mapping), SHORT)


METHOD_TOML = """\
[[repo]]
name = "app"
clone.path = "clone"
tracker.repo = "acme/app"

[repo.merge]
method = "merge"
"""


class MergeMethodKeyTest(unittest.TestCase):
    """`merge` loads per-repo settings for the translated entry.

    The ticket spelling (`acme/app`) resolves to the short entry
    (`app`) before `settings.load`, so the entry's `merge.method`
    applies instead of the default.
    """

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="korapy-merge-method-key-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.clone = os.path.join(self.tmp, "clone")
        os.makedirs(self.clone)
        with open(os.path.join(self.tmp, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(METHOD_TOML)
        self.sessions = os.path.join(self.tmp, "sessions")
        os.environ["KORAPY_SESSIONS"] = self.sessions
        os.environ["KORAPY_STATE"] = os.path.join(self.tmp, "state")
        os.environ["KORAPY_CONFIG"] = os.path.join(self.tmp, "config")
        git_in(self.clone, "init", "-q", "-b", "master")
        with open(os.path.join(self.clone, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\ntwo\nthree\n")
        git_in(self.clone, "add", "-A")
        git_in(
            self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"
        )
        git_in(self.clone, "checkout", "-q", "-b", "feat/1-x")
        with open(os.path.join(self.clone, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("feature\n")
        git_in(self.clone, "add", "-A")
        git_in(
            self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "work"
        )
        self.head = git_in(self.clone, "rev-parse", "HEAD")
        self.base = git_in(self.clone, "rev-parse", "master")
        reviewrecord.write_record(
            SHORT,
            1,
            self.head,
            reviewer="reviewer-1",
            author="worker-1",
            verdict="approve",
            sessions_root=self.sessions,
        )
        here = os.getcwd()
        os.chdir(self.clone)
        self.addCleanup(os.chdir, here)

    def test_the_tracker_spelling_honors_the_entry_merge_method(self):
        fake = FakeForge(
            pulls=[make_pr(repo=TRACKER, number=7, base_sha=self.base, head_sha=self.head)]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mergecli.cmd_merge(
                argparse.Namespace(
                    repo=TRACKER,
                    number=1,
                    forge_repo=TRACKER,
                    pr=7,
                    head=self.head,
                    method=None,
                    skip=["checks"],
                    plan=False,
                    json=False,
                ),
                forge=fake,
            )
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(fake.calls, [("merge", TRACKER, 7, self.head, "merge")])


if __name__ == "__main__":
    unittest.main()
