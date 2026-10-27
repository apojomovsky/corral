"""Coordinator logbook and brief.

Offline: API tests use a temp state root and a fake clock; CLI tests
redirect the state dir through CORRAL_STATE; snapshot and PR
reads use injected fakes (never the real state dir, the network or the
board).
"""

import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

from corral import cli, coordcli
from corral import coord

T0 = datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc)


def fake_snapshot():
    return {
        "attention": [{"kind": "question", "ref": "lib#1", "question": "merge?"}],
        "fleet": {"board_open": 2, "prs_open": 3, "units_active": 1},
    }


def fake_prs():
    return [
        {
            "repo": "lib",
            "number": 7,
            "title": "held one",
            "url": "http://x/7",
            "labels": [{"name": "needs-decision"}],
            "body": "",
        },
        {
            "repo": "hal",
            "number": 8,
            "title": "waits on a person",
            "url": "http://x/8",
            "labels": ["needs-human"],
            "body": "a plain body",
        },
        {
            "repo": "lib",
            "number": 9,
            "title": "plain",
            "url": "http://x/9",
            "labels": [],
            "body": "all green",
        },
    ]


class JournalApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_append_and_read_back(self):
        entry = coord.append_entry(
            "decision",
            "closed as covered",
            refs=["lib#1157"],
            holder="h",
            generation=7,
            root=self.root,
            now=T0,
        )
        self.assertEqual(set(entry), {"at", "holder", "generation", "kind", "refs", "text"})
        self.assertEqual(entry["kind"], "decision")
        self.assertEqual(entry["refs"], ["lib#1157"])
        live = coord.read_entries(root=self.root)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["text"], "closed as covered")

    def test_all_kinds_accepted_bad_kind_rejected(self):
        for kind in coord.KINDS:
            coord.append_entry(kind, "t", root=self.root, now=T0)
        self.assertEqual(len(coord.read_entries(root=self.root)), len(coord.KINDS))
        with self.assertRaises(coord.UsageError):
            coord.append_entry("bogus", "t", root=self.root, now=T0)

    def test_followup_open_then_done_disappears(self):
        opened = coord.followup_add("check CI", holder="h", generation=1, root=self.root, now=T0)
        self.assertEqual(len(opened["id"]), 8)
        int(opened["id"], 16)
        live = coord.read_entries(root=self.root)
        self.assertEqual([e["id"] for e in coord.open_followups(live)], [opened["id"]])
        done = coord.followup_done(opened["id"], holder="h", generation=1, root=self.root, now=T0)
        self.assertEqual(done["id"], opened["id"])
        self.assertEqual(done["kind"], "done")
        live = coord.read_entries(root=self.root)
        self.assertEqual(coord.open_followups(live), [])

    def test_rotation_at_501_keeps_500_plus_archive_and_open(self):
        kept = coord.followup_add("keep me", holder="h", generation=1, root=self.root, now=T0)
        for i in range(500):
            coord.append_entry("note", f"n{i}", holder="h", generation=1, root=self.root, now=T0)
        live = coord.read_entries(root=self.root)
        self.assertEqual(len(live), 500)
        self.assertIn(kept["id"], coord.open_followup_ids(live))
        names = os.listdir(os.path.join(self.root, "coord"))
        archives = [n for n in names if n.startswith("journal-")]
        self.assertEqual(archives, ["journal-2026-10-14.jsonl"])

    def test_archives_older_than_14_days_removed(self):
        old = os.path.join(self.root, "coord", "journal-2026-09-01.jsonl")
        os.makedirs(os.path.dirname(old), exist_ok=True)
        with open(old, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        coord.append_entry("note", "t", holder="h", generation=1, root=self.root, now=T0)
        self.assertFalse(os.path.exists(old))

    def test_brief_json_has_every_key(self):
        coord.append_entry(
            "handoff", "in flight: X", holder="h", generation=1, root=self.root, now=T0
        )
        coord.followup_add("open thing", holder="h", generation=1, root=self.root, now=T0)
        data = coord.brief(root=self.root, now=T0, snapshot_fn=fake_snapshot, prs_fn=fake_prs)
        self.assertEqual(
            set(data),
            {
                "lease",
                "handoff",
                "followups",
                "entries",
                "attention",
                "fleet",
                "human_prs",
                "events",
                "rules_path",
            },
        )
        self.assertEqual(data["handoff"]["text"], "in flight: X")
        self.assertEqual(len(data["followups"]), 1)
        self.assertEqual(data["attention"], fake_snapshot()["attention"])
        self.assertEqual(data["fleet"], fake_snapshot()["fleet"])
        self.assertEqual([p["number"] for p in data["human_prs"]], [7, 8])
        self.assertEqual(data["rules_path"], coord.RULES_PATH)
        text = coord.render_brief_text(data)
        for section in (
            "lease:",
            "handoff",
            "follow-ups:",
            "entries (last",
            "events:",
            "fleet:",
            "human PRs:",
            "rules:",
        ):
            self.assertIn(section, text)
        self.assertLess(text.index("lease:"), text.index("handoff"))
        self.assertLess(text.index("handoff"), text.index("follow-ups:"))
        self.assertLess(text.index("follow-ups:"), text.index("entries (last"))
        self.assertLess(text.index("entries (last"), text.index("events:"))
        self.assertLess(text.index("events:"), text.index("fleet:"))
        self.assertLess(text.index("fleet:"), text.index("human PRs:"))
        self.assertLess(text.index("human PRs:"), text.index("rules:"))

    def test_brief_writes_no_journal(self):
        data = coord.brief(root=self.root, now=T0, snapshot_fn=fake_snapshot, prs_fn=fake_prs)
        self.assertEqual(data["entries"], [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "coord", "journal.jsonl")))

    def test_brief_entries_n_zero_and_negative(self):
        coord.append_entry("note", "a", root=self.root, now=T0)
        coord.append_entry("note", "b", root=self.root, now=T0)
        for count, want in ((0, []), (-1, [])):
            self.assertEqual(
                coord.brief(
                    root=self.root,
                    now=T0,
                    snapshot_fn=fake_snapshot,
                    prs_fn=fake_prs,
                    entries_n=count,
                )["entries"],
                want,
            )
        self.assertEqual(
            len(
                coord.brief(
                    root=self.root, now=T0, snapshot_fn=fake_snapshot, prs_fn=fake_prs, entries_n=1
                )["entries"]
            ),
            1,
        )

    def test_stale_generation_refused(self):
        with self.assertRaises(coord.StaleLeaseError):
            coord.require_current(99, root=self.root)


class JournalCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_STATE": tmp.name, "CORRAL_PROJECT": "demo"})
        env.start()
        self.addCleanup(env.stop)
        for name, fake in (("_coord_snapshot", fake_snapshot), ("_coord_human_prs", fake_prs)):
            patcher = mock.patch.object(coordcli, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_log_followup_handoff_brief(self):
        code, out, _ = self._run(["coord", "log", "decision", "did X", "--ref", "lib#1157"])
        self.assertEqual(code, 0)
        self.assertIn("logged decision", out)
        code, out, _ = self._run(["coord", "followup", "add", "check CI"])
        self.assertEqual(code, 0)
        fid = out.strip().split()[-1]
        self.assertEqual(len(fid), 8)
        code, out, _ = self._run(["coord", "followup", "list"])
        self.assertEqual(code, 0)
        self.assertIn(fid, out)
        code, out, _ = self._run(["coord", "followup", "done", fid])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["coord", "followup", "list"])
        self.assertEqual(code, 0)
        self.assertNotIn(fid, out)
        code, out, _ = self._run(["coord", "handoff", "in flight: X"])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["coord", "brief"])
        self.assertEqual(code, 0)
        self.assertIn("in flight: X", out)

    def test_stale_generation_refused_without_blocking_manual(self):
        self._run(["coord", "acquire", "--holder", "cli-one"])
        code, _, err = self._run(["coord", "log", "note", "x", "--coord-generation", "99"])
        self.assertEqual(code, cli.RACE)
        self.assertIn("generation 1 is current", err)
        code, out, _ = self._run(["coord", "log", "note", "x", "--coord-generation", "1"])
        self.assertEqual(code, 0)
        self.assertIn("logged note", out)


if __name__ == "__main__":
    unittest.main()
