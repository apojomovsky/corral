"""`corral quota`: one view of the pool and ledger, refresh, marks and the tail hook.

Offline: fake readers through injected seams, temp state and config dirs,
no real reader and no network.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from corral import accounts, cli, launch, models, quota_ledger, quotacli, settings
from tests.test_launch import OpsCase, TOML, roomy

T0 = 1_800_000_000.0


def win(used, resets_at=T0 + 3600):
    return {"used_pct": used, "resets_at": resets_at}


def reading(windows, at=T0):
    return {"windows": windows, "read_at": at}


def ledger(**entries):
    readings, marks = {}, {}
    for name, (kind, entry) in entries.items():
        if kind == "reading":
            readings[name] = entry
        else:
            marks[name] = entry
    return {"readings": readings, "marks": marks}


def mark(until):
    return {"until": until, "reason": "limit", "source": "reader"}


def pool(*names):
    return [accounts.Provider(name=name, reader=f"fake:{name}") for name in names]


class OkReader:
    """A reader that reports one window and never parses a limit error."""

    def read(self, now):
        return accounts.Reading(windows={"weekly": accounts.Window(used_pct=10.0, resets_at=None)})

    def parse_limit_error(self, text, now):
        return None


class FullReader:
    """A reader whose window is spent."""

    def read(self, now):
        return accounts.Reading(
            windows={"weekly": accounts.Window(used_pct=100.0, resets_at=T0 + 300)}
        )

    def parse_limit_error(self, text, now):
        return None


class FailingReader:
    """A reader whose read fails at call time."""

    def read(self, now):
        raise accounts.ReaderError("fake: the quota endpoint refused")

    def parse_limit_error(self, text, now):
        return None


class LimitReader:
    """A reader that parses one marker line into a fixed reset."""

    def read(self, now):
        return accounts.Reading(windows={"weekly": accounts.Window(used_pct=10.0, resets_at=None)})

    def parse_limit_error(self, text, now):
        if "LIMIT provider-a" in text:
            return T0 + 600
        return None


def loader(readers):
    def load(entry):
        for key in (entry, entry.partition(":")[2]):
            if key in readers:
                return readers[key]
        raise accounts.AccountsError(f"accounts: reader {entry!r} does not import")

    return load


class SnapshotView(unittest.TestCase):
    def view(self, names, seen, now=T0, **kw):
        from corral import quotacli

        kw.setdefault("low_pct", 90)
        return quotacli.snapshot(pool(*names), seen, now, **kw)

    def test_an_empty_pool_is_an_empty_view(self):
        from corral import quotacli

        view = quotacli.snapshot([], ledger(), T0, low_pct=90)
        self.assertEqual(view, {"now": T0, "providers": {}})

    def test_a_fresh_reading_is_ok_with_plain_windows(self):
        view = self.view(
            ("provider-a",), ledger(**{"provider-a": ("reading", reading({"w": win(10)}))})
        )
        entry = view["providers"]["provider-a"]
        self.assertEqual(entry["state"], "ok")
        self.assertEqual(entry["windows"], {"w": {"used_pct": 10, "resets_at": T0 + 3600}})
        self.assertEqual(entry["read_at"], T0)
        self.assertFalse(entry["stale"])
        self.assertIsNone(entry["mark"])
        self.assertIsNone(entry["error"])
        self.assertEqual(entry["reader"], "fake:provider-a")

    def test_a_spent_window_is_exhausted(self):
        view = self.view(
            ("provider-a",), ledger(**{"provider-a": ("reading", reading({"w": win(100)}))})
        )
        self.assertEqual(view["providers"]["provider-a"]["state"], "exhausted")

    def test_an_active_mark_exhausts_without_any_reading(self):
        view = self.view(("provider-a",), ledger(**{"provider-a": ("mark", mark(T0 + 100))}))
        entry = view["providers"]["provider-a"]
        self.assertEqual(entry["state"], "exhausted")
        self.assertEqual(entry["mark"], mark(T0 + 100))
        self.assertIsNone(entry["windows"])
        self.assertIsNone(entry["read_at"])

    def test_a_stale_reading_is_unknown_and_flagged(self):
        seen = ledger(**{"provider-a": ("reading", reading({"w": win(10)}, at=T0 - 2000))})
        view = self.view(("provider-a",), seen)
        entry = view["providers"]["provider-a"]
        self.assertEqual(entry["state"], "unknown")
        self.assertTrue(entry["stale"])

    def test_a_recorded_failure_is_unknown_with_its_error(self):
        seen = quota_ledger.record_failure(ledger(), "provider-a", "boom", T0)
        entry = self.view(("provider-a",), seen)["providers"]["provider-a"]
        self.assertEqual(entry["state"], "unknown")
        self.assertEqual(entry["error"], "boom")
        self.assertIsNone(entry["windows"])

    def test_the_json_keys_are_stable(self):
        view = self.view(("provider-a",), ledger())
        self.assertEqual(set(view), {"now", "providers"})
        self.assertEqual(
            set(view["providers"]["provider-a"]),
            {"state", "windows", "read_at", "stale", "mark", "reader", "error"},
        )
        json.dumps(view)


class Refresh(unittest.TestCase):
    def test_a_successful_read_is_recorded(self):
        from corral import quotacli

        fresh, results = quotacli.refresh(
            pool("provider-a"), ledger(), T0, load_reader=loader({"provider-a": OkReader()})
        )
        self.assertEqual(results, {"provider-a": (True, None)})
        self.assertEqual(
            fresh["readings"]["provider-a"]["windows"],
            {"weekly": {"used_pct": 10.0, "resets_at": None}},
        )
        self.assertEqual(fresh["readings"]["provider-a"]["read_at"], T0)

    def test_a_reader_error_is_recorded_never_raised(self):
        from corral import quotacli

        fresh, results = quotacli.refresh(
            pool("provider-a"),
            ledger(**{"provider-a": ("reading", reading({"w": win(10)}))}),
            T0,
            load_reader=loader({"provider-a": FailingReader()}),
        )
        self.assertEqual(results, {"provider-a": (False, "fake: the quota endpoint refused")})
        entry = fresh["readings"]["provider-a"]
        self.assertEqual(entry["error"], "fake: the quota endpoint refused")
        self.assertNotIn("windows", entry)

    def test_an_unknown_only_name_is_a_quota_error(self):
        from corral import quotacli

        with self.assertRaises(quotacli.QuotaError):
            quotacli.refresh(
                pool("provider-a"),
                ledger(),
                T0,
                only="provider-b",
                load_reader=loader({"provider-a": OkReader()}),
            )

    def test_only_runs_the_named_provider(self):
        from corral import quotacli

        fresh, results = quotacli.refresh(
            pool("provider-a", "provider-b"),
            ledger(),
            T0,
            only="provider-b",
            load_reader=loader({"provider-a": OkReader(), "provider-b": FullReader()}),
        )
        self.assertEqual(set(results), {"provider-b"})
        self.assertNotIn("provider-a", fresh["readings"])

    def test_a_reader_that_does_not_build_leaves_the_ledger_alone(self):
        from corral import quotacli

        seen = ledger()
        with self.assertRaises(accounts.AccountsError):
            quotacli.refresh(pool("provider-a"), seen, T0, load_reader=loader({}))
        self.assertEqual(seen, ledger())


class MarkProvider(unittest.TestCase):
    def test_an_omitted_until_uses_the_cooldown(self):
        from corral import quotacli

        providers = [accounts.Provider(name="provider-a", reader="fake:x", cooldown_s=60.0)]
        fresh, moved = quotacli.mark_provider(ledger(), providers, "provider-a", T0)
        self.assertTrue(moved)
        self.assertEqual(fresh["marks"]["provider-a"]["until"], T0 + 60.0)
        self.assertEqual(fresh["marks"]["provider-a"]["source"], "manual")

    def test_an_earlier_until_than_the_stored_mark_is_a_no_op(self):
        from corral import quotacli

        seen = ledger(**{"provider-a": ("mark", mark(T0 + 600))})
        fresh, moved = quotacli.mark_provider(
            seen, pool("provider-a"), "provider-a", T0, until=T0 + 60
        )
        self.assertFalse(moved)
        self.assertEqual(fresh["marks"]["provider-a"]["until"], T0 + 600)

    def test_an_unknown_provider_is_a_quota_error(self):
        from corral import quotacli

        with self.assertRaises(quotacli.QuotaError):
            quotacli.mark_provider(ledger(), pool("provider-a"), "provider-b", T0)

    def test_a_bad_until_is_a_quota_error(self):
        from corral import quotacli

        with self.assertRaises(quotacli.QuotaError):
            quotacli.mark_provider(ledger(), pool("provider-a"), "provider-a", T0, until="soon")


class NoteLimitError(unittest.TestCase):
    def test_a_parsed_error_posts_a_tail_mark(self):
        from corral import quotacli

        fresh, marked = quotacli.note_limit_error(
            ledger(),
            pool("provider-a"),
            "boom LIMIT provider-a 429",
            T0,
            load_reader=loader({"provider-a": LimitReader()}),
        )
        self.assertEqual(marked, ["provider-a"])
        self.assertEqual(fresh["marks"]["provider-a"]["until"], T0 + 600)
        self.assertEqual(fresh["marks"]["provider-a"]["source"], "tail")

    def test_an_unrecognized_text_posts_nothing(self):
        from corral import quotacli

        fresh, marked = quotacli.note_limit_error(
            ledger(),
            pool("provider-a"),
            "all clear",
            T0,
            load_reader=loader({"provider-a": LimitReader()}),
        )
        self.assertEqual(marked, [])
        self.assertEqual(fresh, ledger())

    def test_every_provider_is_consulted(self):
        from corral import quotacli

        fresh, marked = quotacli.note_limit_error(
            ledger(),
            pool("provider-a", "provider-b"),
            "LIMIT provider-a",
            T0,
            load_reader=loader({"provider-a": LimitReader(), "provider-b": OkReader()}),
        )
        self.assertEqual(marked, ["provider-a"])


class QuotaVerbs(OpsCase):
    """The verbs against temp state and config dirs with fake readers."""

    READER = "tests.test_quotacli:OkReader"

    def setUp(self):
        super().setUp()
        self.write_accounts(self.READER)

    def write_accounts(self, reader, name="provider-a"):
        config = os.path.join(self.tmp, "config")
        os.makedirs(config, exist_ok=True)
        with open(os.path.join(config, "accounts.toml"), "w", encoding="utf-8") as handle:
            handle.write(f'[[provider]]\nname = "{name}"\nreader = "{reader}"\n')

    def run_quota(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["quota", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_quota_shows_the_pool_and_the_ledger(self):
        code, out, err = self.run_quota()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("provider-a", out)
        self.assertIn("unknown", out)

    def test_quota_json_uses_the_snapshot_schema(self):
        code, out, err = self.run_quota("--json")
        self.assertEqual((code, err), (0, ""))
        view = json.loads(out)
        self.assertEqual(set(view), {"now", "providers"})
        self.assertEqual(
            set(view["providers"]["provider-a"]),
            {"state", "windows", "read_at", "stale", "mark", "reader", "error"},
        )

    def test_refresh_records_readings(self):
        code, out, err = self.run_quota("refresh")
        self.assertEqual(code, 0)
        seen = quota_ledger.load()
        self.assertIn("weekly", seen["readings"]["provider-a"]["windows"])

    def test_refresh_json_prints_only_json(self):
        code, out, err = self.run_quota("refresh", "--json")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(set(payload), {"provider-a"})
        self.assertTrue(payload["provider-a"]["ok"])

    def test_refresh_json_flag_works_before_the_verb(self):
        code, out, err = self.run_quota("--json", "refresh")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(set(payload), {"provider-a"})
        self.assertTrue(payload["provider-a"]["ok"])

    def test_refresh_json_keeps_failures_on_stderr(self):
        self.write_accounts("tests.test_quotacli:FailingReader")
        code, out, err = self.run_quota("refresh", "--json")
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertFalse(payload["provider-a"]["ok"])
        self.assertIn("refused", payload["provider-a"]["error"])
        self.assertIn("refused", err)

    def test_refresh_of_an_unknown_provider_is_usage(self):
        self.assertEqual(self.run_quota("refresh", "provider-b")[0], 4)

    def test_refresh_reports_a_dead_reader_as_unreachable(self):
        self.write_accounts("tests.test_quotacli:FailingReader")
        code, out, err = self.run_quota("refresh")
        self.assertEqual(code, 3)
        seen = quota_ledger.load()
        self.assertIn("refused", seen["readings"]["provider-a"]["error"])

    def test_refresh_of_one_dead_reader_is_still_unreachable(self):
        self.write_accounts("tests.test_quotacli:FailingReader")
        code, out, err = self.run_quota("refresh", "provider-a")
        self.assertEqual(code, 3)

    def test_mark_defaults_to_the_cooldown(self):
        code, out, err = self.run_quota("mark", "provider-a", "--reason", "slow")
        self.assertEqual((code, err), (0, ""))
        seen = quota_ledger.load()
        self.assertIn("provider-a", seen["marks"])
        self.assertIn("until", out)

    def test_an_earlier_mark_is_nothing_to_do(self):
        self.assertEqual(self.run_quota("mark", "provider-a", "--until", str(T0 + 600))[0], 0)
        code, out, err = self.run_quota("mark", "provider-a", "--until", str(T0 + 60))
        self.assertEqual(code, 1)
        self.assertIn("provider-a", err)

    def test_mark_of_an_unknown_provider_is_usage(self):
        self.assertEqual(self.run_quota("mark", "provider-b")[0], 4)

    def test_unmark_releases_the_mark(self):
        self.assertEqual(self.run_quota("mark", "provider-a")[0], 0)
        code, out, err = self.run_quota("unmark", "provider-a")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("provider-a", quota_ledger.load()["marks"])

    def test_unmark_without_a_mark_is_nothing_to_do(self):
        code, out, err = self.run_quota("unmark", "provider-a")
        self.assertEqual(code, 1)
        self.assertIn("provider-a", err)

    def test_a_no_op_unmark_or_mark_writes_no_ledger(self):
        self.assertEqual(self.run_quota("unmark", "provider-a")[0], 1)
        self.assertFalse(os.path.exists(quota_ledger.ledger_path()))
        self.assertEqual(self.run_quota("mark", "provider-a", "--until", str(T0 + 600))[0], 0)
        before = open(quota_ledger.ledger_path(), "rb").read()
        self.assertEqual(self.run_quota("mark", "provider-a", "--until", str(T0 + 60))[0], 1)
        self.assertEqual(open(quota_ledger.ledger_path(), "rb").read(), before)

    def test_quota_help_lists_the_verbs(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["quota", "--help"]), 0)
        for verb in ("refresh", "mark", "unmark"):
            self.assertIn(verb, out.getvalue())


class LaunchGate(OpsCase):
    """`corral launch` stays gated: an exhausted chain refuses with exit 75."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, settings.PROJECT_FILE), "w", encoding="utf-8") as handle:
            handle.write(TOML + '[models.default]\nchain = ["a/x", "b/y"]\n')

    def pool(self):
        return [accounts.Provider(name=name, reader="fake:Reader") for name in ("a", "b")]

    def test_an_exhausted_chain_refuses_before_anything_is_made(self):
        seen = ledger(
            a=("reading", reading({"w": win(100, resets_at=T0 + 100)})),
            b=("reading", reading({"w": win(100, resets_at=T0 + 300)})),
        )
        with self.assertRaises(models.Exhausted) as ctx:
            launch.launch(
                self.target("lib", "omp"),
                5,
                "do the thing",
                title="feat: add the thing",
                list_fn=self.system.list_fn,
                run=self.system.run,
                usage=roomy,
                pool_fn=self.pool,
                ledger_fn=lambda: seen,
                now_fn=lambda: T0,
            )
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertIn("default", str(ctx.exception))
        self.assertEqual(self.system.started, [])
        self.assertFalse(os.path.exists(self.session("lib", 5)))


class TailHook(OpsCase):
    """A session tail posts a mark when the tail parses as a limit error."""

    MARKER = "tests.test_quotacli:LimitReader"

    def write_activity(self, *lines):
        session = self.session("lib", 5)
        os.makedirs(session, exist_ok=True)
        with open(os.path.join(session, "chat.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("".join(lines))

    def note(self, readers):
        from corral import quotacli

        return lambda ledger, pool, text, now: quotacli.note_limit_error(
            ledger, pool, text, now, load_reader=loader(readers)
        )

    def test_a_limit_line_in_the_tail_posts_a_mark(self):
        self.write_activity("all clear\n", "LIMIT provider-a 429\n")
        with mock.patch.object(
            accounts,
            "providers",
            return_value=[accounts.Provider(name="provider-a", reader=self.MARKER)],
        ):
            out = io.StringIO()
            launch.tail(
                self.target("lib"),
                5,
                out=out,
                note_fn=self.note({"LimitReader": LimitReader()}),
                now_fn=lambda: T0,
            )
        seen = quota_ledger.load()
        self.assertEqual(seen["marks"]["provider-a"]["until"], T0 + 600)
        self.assertEqual(seen["marks"]["provider-a"]["source"], "tail")

    def test_a_clean_tail_posts_nothing_and_writes_no_ledger(self):
        self.write_activity("all clear\n")
        with mock.patch.object(
            accounts,
            "providers",
            return_value=[accounts.Provider(name="provider-a", reader=self.MARKER)],
        ):
            launch.tail(
                self.target("lib"),
                5,
                out=io.StringIO(),
                note_fn=self.note({"LimitReader": LimitReader()}),
                now_fn=lambda: T0,
            )
        self.assertFalse(os.path.exists(quota_ledger.ledger_path()))
        self.assertEqual(quota_ledger.load(), quota_ledger.empty())

    def test_an_empty_pool_leaves_the_ledger_alone(self):
        self.write_activity("LIMIT provider-a 429\n")
        with mock.patch.object(accounts, "providers", return_value=[]):
            launch.tail(self.target("lib"), 5, out=io.StringIO())
        self.assertFalse(os.path.exists(quota_ledger.ledger_path()))

    def test_a_corrupt_ledger_leaves_the_tail_succeeding(self):
        self.write_activity("all clear\n")
        os.makedirs(os.path.dirname(quota_ledger.ledger_path()), exist_ok=True)
        with open(quota_ledger.ledger_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with mock.patch.object(
            accounts,
            "providers",
            return_value=[accounts.Provider(name="provider-a", reader=self.MARKER)],
        ):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                shown = launch.tail(
                    self.target("lib"),
                    5,
                    out=out,
                    note_fn=self.note({"LimitReader": LimitReader()}),
                    now_fn=lambda: T0,
                )
        self.assertEqual(shown, len("all clear\n"))
        self.assertIn("quota tail hook skipped", err.getvalue())

    def test_a_bad_accounts_file_leaves_the_tail_succeeding(self):
        self.write_activity("all clear\n")
        config = os.path.join(self.tmp, "config")
        os.makedirs(config, exist_ok=True)
        with open(os.path.join(config, "accounts.toml"), "w", encoding="utf-8") as handle:
            handle.write("[[provider]\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            shown = launch.tail(self.target("lib"), 5, out=out, now_fn=lambda: T0)
        self.assertEqual(shown, len("all clear\n"))
        self.assertIn("quota tail hook skipped", err.getvalue())

    def test_a_limit_line_older_than_the_scan_window_posts_no_mark(self):
        filler = "x" * (quotacli.TAIL_SCAN_BYTES + 1024)
        self.write_activity("LIMIT provider-a 429\n", filler + "\n")
        with mock.patch.object(
            accounts,
            "providers",
            return_value=[accounts.Provider(name="provider-a", reader=self.MARKER)],
        ):
            launch.tail(
                self.target("lib"),
                5,
                out=io.StringIO(),
                note_fn=self.note({"LimitReader": LimitReader()}),
                now_fn=lambda: T0,
            )
        self.assertFalse(os.path.exists(quota_ledger.ledger_path()))

    def test_a_limit_line_at_the_end_of_a_big_file_posts_a_mark(self):
        filler = "x" * (quotacli.TAIL_SCAN_BYTES + 1024)
        self.write_activity(filler + "\n", "LIMIT provider-a 429\n")
        with mock.patch.object(
            accounts,
            "providers",
            return_value=[accounts.Provider(name="provider-a", reader=self.MARKER)],
        ):
            launch.tail(
                self.target("lib"),
                5,
                out=io.StringIO(),
                note_fn=self.note({"LimitReader": LimitReader()}),
                now_fn=lambda: T0,
            )
        self.assertEqual(quota_ledger.load()["marks"]["provider-a"]["until"], T0 + 600)

    def test_a_stat_failure_falls_back_to_a_full_read(self):
        self.write_activity("all clear\n", "LIMIT provider-a 429\n")
        with (
            mock.patch.object(
                accounts,
                "providers",
                return_value=[accounts.Provider(name="provider-a", reader=self.MARKER)],
            ),
            mock.patch.object(os, "fstat", side_effect=OSError("no stat")),
        ):
            launch.tail(
                self.target("lib"),
                5,
                out=io.StringIO(),
                note_fn=self.note({"LimitReader": LimitReader()}),
                now_fn=lambda: T0,
            )
        self.assertEqual(quota_ledger.load()["marks"]["provider-a"]["until"], T0 + 600)

    def test_a_seek_failure_falls_back_to_a_full_read(self):
        class Handle:
            def __init__(self):
                self.seeks = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def fileno(self):
                return 0

            def seek(self, offset):
                self.seeks.append(offset)
                if len(self.seeks) == 1:
                    raise OSError("no seek")

            def read(self):
                return b"LIMIT provider-a 429\n" if self.seeks[-1] == 0 else b""

        handle = Handle()
        fake_stat = mock.Mock(st_size=10_000_000)
        with (
            mock.patch("builtins.open", return_value=handle),
            mock.patch.object(os, "fstat", return_value=fake_stat),
        ):
            text = launch._tail_text("ignored")
        self.assertEqual(text, "LIMIT provider-a 429\n")
        self.assertEqual(handle.seeks[-1], 0)


if __name__ == "__main__":
    unittest.main()
