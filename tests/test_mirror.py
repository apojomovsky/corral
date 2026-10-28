"""The mirror pass: drain the board store's outbox into the tracker. Fakes only."""

import io
import json
import os
import tempfile
import unittest
from typing import Any

from corral import board, mirror, mirrorcli, sync, syncli, tracker
from corral.github import gh as ghmod
from corral.github import tracker as ghtracker
from tests.test_syncli import git_ok
from tests.test_tracker import FakeRun, FakeTracker, item_payload, reading


REPO = "acme/app"
T0 = 1_800_000_100.0


def stored(path, number=7, **over: Any):
    """A board store holding one operator-authored item."""
    params: dict[str, Any] = {
        "repo": "app",
        "number": number,
        "kind": "issue",
        "title": "Title",
        "body": "Body",
        "state": "open",
        "author": "alice",
        "labels": [],
        "priority": "",
        "assignees": [],
        "url": "https://example.com/app/7",
        "tracker_updated_at": 1000.0,
        "extra": {},
    }
    params.update(over)
    store = board.open_store(path)
    store.upsert_item(board.Item(**params), operators=["alice"], now=1000.0)
    return store


class MirrorTracker(FakeTracker):
    """A tracker double that records writes and plays failures per item."""

    def __init__(self, items=()):
        super().__init__(items)
        self.writes = []
        self.write_errors = {}

    def write(self, repo, number, field, value):
        self.writes.append((repo, number, field, value))
        if (repo, number) in self.write_errors:
            raise self.write_errors[(repo, number)]


class SettleRegressionTest(unittest.TestCase):
    """Section 0: tracker readings carry agent/status and settle queues."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-mirror-settle-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "board.sqlite")

    def test_title_only_reading_keeps_queued_agent_write(self):
        store = stored(self.path)
        self.addCleanup(store.close)
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Person edit",
                state="open",
                author="alice",
                tracker_updated_at=1600.0,
                agent="",
                status="",
            ),
            operators=["alice"],
            now=1600.0,
        )
        queued = store.queued_writes()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].state, board.QUEUED)
        item, _ = store.item("app", 7, now=1600.0)
        self.assertEqual(item.confidence, board.PENDING)

    def test_different_nonempty_agent_drops_as_overridden(self):
        store = stored(self.path)
        self.addCleanup(store.close)
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=1600.0,
                agent="agent-2",
            ),
            operators=["alice"],
            now=1600.0,
        )
        self.assertEqual(store.queued_writes(), [])
        rows = store.outbox_rows(state="dropped")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason, "overridden")

    def test_echoed_agent_confirms_sent(self):
        store = stored(self.path)
        self.addCleanup(store.close)
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=1600.0,
                agent="agent-1",
            ),
            operators=["alice"],
            now=1600.0,
        )
        self.assertEqual(store.queued_writes(), [])
        rows = store.outbox_rows(state="sent")
        self.assertEqual(len(rows), 1)

    def test_status_reading_settles_status_write(self):
        store = stored(self.path)
        self.addCleanup(store.close)
        store.enqueue("app", 7, "status", "working", now=1500.0)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=1600.0,
                status="working",
            ),
            operators=["alice"],
            now=1600.0,
        )
        self.assertEqual(store.queued_writes(), [])
        self.assertEqual(len(store.outbox_rows(state="sent")), 1)


class SyncPassthroughTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-mirror-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.store = board.open_store(os.path.join(self.tmp.name, "board.sqlite"))
        self.addCleanup(self.store.close)

    def test_sync_reading_carries_agent_and_status(self):
        fake = FakeTracker([reading(number=1, agent="agent-1", status="working", updated_at=T0)])
        result = sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        self.assertEqual(result.items_seen, 1)
        row, _ = self.store.item(REPO, 1, now=T0)
        self.assertEqual(row.title, "Title")

    def test_sync_echo_confirms_queued_write(self):
        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=1,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=T0 - 100,
            ),
            operators=["alice"],
            now=T0 - 100,
        )
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        fake = FakeTracker([reading(number=1, agent="agent-1", status="", updated_at=T0)])
        sync.sync_repo(self.store, fake, REPO, operators=["alice"], now=T0, cursor=None)
        self.assertEqual(self.store.queued_writes(), [])
        self.assertEqual(len(self.store.outbox_rows(state="sent")), 1)


class LabelDerivationTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeRun()
        self.adapter = ghtracker.GithubTracker(self.gh)

    def read(self, labels):
        payload = item_payload(7, labels=[{"name": name} for name in labels])
        self.gh.add("repos/acme/app/issues/7", payload)
        return self.adapter.item("acme/app", 7)

    def test_agent_and_status_come_from_labels(self):
        found = self.read(["agent:agent-1", "status:working", "ready"])
        self.assertEqual(found.agent, "agent-1")
        self.assertEqual(found.status, "working")

    def test_absent_labels_read_empty(self):
        found = self.read(["ready"])
        self.assertEqual(found.agent, "")
        self.assertEqual(found.status, "")

    def test_smallest_label_wins(self):
        found = self.read(["agent:zeta", "agent:alpha", "status:b", "status:a"])
        self.assertEqual(found.agent, "alpha")
        self.assertEqual(found.status, "a")


class LabelRun:
    """A `gh` seam double for the issues-labels endpoint: answers the GET
    with canned label names, records every call, and plays one error."""

    def __init__(self, labels=()):
        self.labels: list[str] = list(labels)
        self.calls = []
        self.error = None

    def __call__(self, args):
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        if "--method" not in args:
            return json.dumps([{"name": name} for name in self.labels])
        return ""

    def mutating(self):
        return [args for args in self.calls if "--method" in args]


class AdapterWriteTest(unittest.TestCase):
    def setUp(self):
        self.gh = LabelRun()
        self.adapter = ghtracker.GithubTracker(self.gh)

    def test_add_missing_label(self):
        self.adapter.write("acme/app", 7, "agent", "agent-1")
        posts = self.gh.mutating()
        self.assertEqual(len(posts), 1)
        self.assertIn("POST", posts[0])
        self.assertIn("labels[]=agent:agent-1", posts[0])

    def test_same_value_writes_nothing(self):
        self.gh.labels = ["agent:agent-1"]
        self.adapter.write("acme/app", 7, "agent", "agent-1")
        self.assertEqual(self.gh.mutating(), [])

    def test_replacement_removes_other_prefixed_labels(self):
        self.gh.labels = ["agent:old", "agent:agent-1", "status:working", "ready"]
        self.adapter.write("acme/app", 7, "agent", "agent-1")
        deletes = [args for args in self.gh.calls if "DELETE" in args]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0][-1].endswith("/agent:old"))
        posts = [args for args in self.gh.calls if "POST" in args]
        self.assertEqual(posts, [])

    def test_clear_removes_prefixed_labels(self):
        self.gh.labels = ["agent:agent-1", "ready"]
        self.adapter.write("acme/app", 7, "agent", "")
        deletes = [args for args in self.gh.calls if "DELETE" in args]
        self.assertEqual(len(deletes), 1)
        posts = [args for args in self.gh.calls if "POST" in args]
        self.assertEqual(posts, [])

    def test_clear_with_none_present_writes_nothing(self):
        self.gh.labels = ["ready"]
        self.adapter.write("acme/app", 7, "status", "")
        self.assertEqual(self.gh.mutating(), [])

    def test_other_field_is_refused(self):
        with self.assertRaises(ValueError):
            self.adapter.write("acme/app", 7, "title", "New")

    def test_gh_failure_is_a_tracker_error(self):
        self.gh.error = RuntimeError("connection refused")
        with self.assertRaises(tracker.TrackerError) as ctx:
            self.adapter.write("acme/app", 7, "agent", "agent-1")
        self.assertNotIsInstance(ctx.exception, tracker.TrackerRateLimited)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_rate_limit_maps_to_tracker_rate_limited(self):
        self.gh.error = ghmod.GhError("unavailable", "API rate limit exceeded, retry after 62s")
        with self.assertRaises(tracker.TrackerRateLimited) as ctx:
            self.adapter.write("acme/app", 7, "agent", "agent-1")
        self.assertEqual(ctx.exception.retry_after, 62.0)


class PushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-mirror-push-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "board.sqlite")

    def open(self, number=7, **over):
        store = stored(self.path, number=number, **over)
        self.addCleanup(store.close)
        return store

    def seed(self, store, number, **over: Any):
        params: dict[str, Any] = {
            "repo": "app",
            "number": number,
            "title": "Title",
            "state": "open",
            "author": "alice",
            "tracker_updated_at": 1000.0,
        }
        params.update(over)
        store.upsert_item(board.Item(**params), operators=["alice"], now=1000.0)

    def test_last_value_per_field_wins(self):
        store = self.open()
        first = store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        second = store.enqueue("app", 7, "agent", "agent-2", now=1501.0)
        fake = MirrorTracker()
        result = mirror.push(store, fake, operators=["alice"], now=1600.0)
        self.assertEqual(fake.writes, [("app", 7, "agent", "agent-2")])
        self.assertEqual(result.sent, 1)
        rows = {row.id: row for row in store.outbox_rows()}
        self.assertEqual(rows[first.id].state, board.DROPPED)
        self.assertEqual(rows[first.id].reason, "superseded")
        self.assertEqual(rows[second.id].state, board.SENT)
        self.assertEqual(result.dropped, 1)

    def test_external_item_drops_without_writing(self):
        store = self.open()
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Title",
                state="open",
                author="mallory",
                tracker_updated_at=1600.0,
            ),
            operators=["alice"],
            now=1600.0,
        )
        fake = MirrorTracker()
        result = mirror.push(store, fake, operators=["alice"], now=1700.0)
        self.assertEqual(fake.writes, [])
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.dropped, 1)
        rows = store.outbox_rows(state="dropped")
        self.assertEqual(rows[0].reason, "external")

    def test_closed_item_drops_without_writing(self):
        store = self.open()
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        store.upsert_item(
            board.Item(
                repo="app",
                number=7,
                title="Title",
                state="closed",
                author="alice",
                tracker_updated_at=1600.0,
            ),
            operators=["alice"],
            now=1600.0,
        )
        fake = MirrorTracker()
        result = mirror.push(store, fake, operators=["alice"], now=1700.0)
        self.assertEqual(fake.writes, [])
        rows = store.outbox_rows(state="dropped")
        self.assertEqual(rows[0].reason, "closed")

    def test_failing_item_does_not_block_others(self):
        store = self.open(number=7)
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        self.seed(store, 8)
        store.enqueue("app", 8, "agent", "agent-2", now=1500.0)
        fake = MirrorTracker()
        fake.write_errors[("app", 7)] = tracker.TrackerError("connection refused")
        result = mirror.push(store, fake, operators=["alice"], now=1600.0)
        self.assertEqual(
            fake.writes,
            [("app", 7, "agent", "agent-1"), ("app", 8, "agent", "agent-2")],
        )
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.dirty, 1)
        self.assertEqual(len(result.errors), 1)
        queued = {row.number: row for row in store.queued_writes()}
        self.assertEqual(set(queued), {7})
        self.assertEqual(queued[7].attempts, 1)

    def test_rate_limit_stops_pass_with_rest_queued(self):
        store = self.open(number=7)
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        self.seed(store, 8)
        store.enqueue("app", 8, "agent", "agent-2", now=1501.0)
        fake = MirrorTracker()
        fake.write_errors[("app", 7)] = tracker.TrackerRateLimited("slow down", 45.0)
        result = mirror.push(store, fake, operators=["alice"], now=1600.0)
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.dirty, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.stopped, 45.0)
        queued = {row.number: row for row in store.queued_writes()}
        self.assertEqual(set(queued), {7, 8})
        self.assertEqual(queued[7].attempts, 1)
        self.assertEqual(queued[8].attempts, 0)

    def test_limit_caps_the_batch(self):
        store = self.open(number=7)
        store.enqueue("app", 7, "agent", "agent-1", now=1500.0)
        self.seed(store, 8)
        store.enqueue("app", 8, "agent", "agent-2", now=1501.0)
        fake = MirrorTracker()
        result = mirror.push(store, fake, operators=["alice"], now=1600.0, limit=1)
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(store.queued_writes()), 1)


class MirrorCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-mirrorcli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                '[[repo]]\nname = "app"\nclone.path = "clone"\ntracker.repo = "acme/app"\n'
            )
        self.store = board.open_store(os.path.join(self.root, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_mirror(self, fake, **over):
        params = {
            "limit": None,
            "as_json": False,
            "tracker": fake,
            "store": self.store,
            "git": git_ok(),
            "now": T0,
            "out": self.out,
            "err": self.err,
            "operators": {"acme/app": ["alice"]},
        }
        params.update(over)
        return mirrorcli.mirror_root(self.root, (), **params)

    def seed(self, number=1, **over: Any):
        params: dict[str, Any] = {
            "repo": "acme/app",
            "number": number,
            "kind": "issue",
            "title": "Title",
            "body": "Body",
            "state": "open",
            "author": "alice",
            "tracker_updated_at": T0 - 100,
        }
        params.update(over)
        self.store.upsert_item(board.Item(**params), operators=["alice"], now=T0 - 100)

    def test_nothing_queued_exits_1(self):
        self.assertEqual(self.run_mirror(MirrorTracker()), 1)

    def test_sent_exits_0(self):
        self.seed()
        self.store.enqueue("acme/app", 1, "agent", "agent-1", now=T0 - 50)
        self.assertEqual(self.run_mirror(MirrorTracker()), 0)

    def test_all_failed_exits_3(self):
        self.seed()
        self.store.enqueue("acme/app", 1, "agent", "agent-1", now=T0 - 50)
        fake = MirrorTracker()
        fake.write_errors[("acme/app", 1)] = tracker.TrackerError("down")
        self.assertEqual(self.run_mirror(fake), 3)

    def test_rate_limited_with_nothing_sent_exits_3(self):
        self.seed()
        self.store.enqueue("acme/app", 1, "agent", "agent-1", now=T0 - 50)
        fake = MirrorTracker()
        fake.write_errors[("acme/app", 1)] = tracker.TrackerRateLimited("slow", 30.0)
        self.assertEqual(self.run_mirror(fake), 3)

    def test_rate_limited_with_sent_exits_0(self):
        self.seed(number=1)
        self.seed(number=2)
        self.store.enqueue("acme/app", 1, "agent", "agent-1", now=T0 - 50)
        self.store.enqueue("acme/app", 2, "agent", "agent-2", now=T0 - 49)
        fake = MirrorTracker()
        fake.write_errors[("acme/app", 2)] = tracker.TrackerRateLimited("slow", 30.0)
        self.assertEqual(self.run_mirror(fake), 0)

    def test_no_repo_entries_exits_4(self):
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write("")
        self.assertEqual(self.run_mirror(MirrorTracker()), 4)

    def test_json_reports_counts(self):
        import json

        self.seed()
        self.store.enqueue("acme/app", 1, "agent", "agent-1", now=T0 - 50)
        code = self.run_mirror(MirrorTracker(), as_json=True)
        self.assertEqual(code, 0)
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["sent"], 1)
        self.assertEqual(doc["errors"], [])

    def test_parser_wires_mirror(self):
        import argparse

        parser = mirrorcli.build_mirror_parser()
        args = parser.parse_args([])
        self.assertIsNone(args.limit)
        self.assertFalse(args.json)
        args = parser.parse_args(["--limit", "5", "--json"])
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.json)


class SyncPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corral-syncpush-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "corral.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                '[[repo]]\nname = "app"\nclone.path = "clone"\ntracker.operators = ["alice"]\n'
            )
        self.store = board.open_store(os.path.join(self.root, "board.sqlite"))
        self.addCleanup(self.store.close)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def test_sync_push_sends_queued_write(self):
        from tests.test_syncli import git_ok

        self.store.upsert_item(
            board.Item(
                repo=REPO,
                number=1,
                title="Title",
                state="open",
                author="alice",
                tracker_updated_at=T0 - 100,
            ),
            operators=["alice"],
            now=T0 - 100,
        )
        self.store.enqueue(REPO, 1, "agent", "agent-1", now=T0 - 50)
        fake = MirrorTracker([reading(number=1, agent="", status="", updated_at=T0)])
        code = syncli.sync_root(
            self.root,
            [],
            resync=False,
            audit=None,
            as_json=False,
            tracker=fake,
            store=self.store,
            git=git_ok(),
            now=T0,
            out=self.out,
            err=self.err,
            push=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes, [(REPO, 1, "agent", "agent-1")])


if __name__ == "__main__":
    unittest.main()
