"""`corral quota`: one view of the pool and the ledger, plus refresh and marks.

The library functions take the pool, the ledger and `now` explicitly, so
tests inject fake pools and readers; the verbs below resolve them from
the host. Exit codes: 0 ok, 1 nothing to do (a further mark already
stands, or no mark to release), 3 every requested reader failed, 4 usage
or an unreadable pool or ledger.
"""

import argparse
import json
import math
import sys
import time

from corral import accounts, paths, quota, quota_ledger, settings

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4

#: Only the recent session output is matched: a stale quota line must not
#: mark a provider long after the error scrolled past.
TAIL_SCAN_BYTES = 65536


class QuotaError(ValueError):
    """A quota verb cannot proceed: unknown provider or bad flag (exit 4)."""


def _pool_name(provider) -> str:
    return getattr(provider, "name", provider)


def _stale_after(provider) -> float:
    bound = getattr(provider, "stale_after_s", None)
    if isinstance(bound, bool) or not isinstance(bound, (int, float)):
        return accounts.DEFAULT_STALE_AFTER_S
    return float(bound)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def snapshot(pool, ledger, now, *, low_pct):
    """Status view: ``{"now": now, "providers": {name: {...}}}``.

    Each provider carries its state, plain windows (or None), `read_at`
    (or None), `stale`, the stored mark (or None), its reader entry and
    the recorded failure text (or None). The `--json` verb prints this
    dict as-is: its field names are the stable schema.
    """
    readings = (ledger or {}).get("readings", {}) or {}
    marks = (ledger or {}).get("marks", {}) or {}
    providers = {}
    for provider in pool or ():
        name = _pool_name(provider)
        reading = readings.get(name)
        bound = _stale_after(provider)
        windows = None
        read_at = None
        error = None
        if isinstance(reading, dict):
            if isinstance(reading.get("windows"), dict):
                windows = {}
                for window_id, entry in reading["windows"].items():
                    if isinstance(entry, dict):
                        windows[window_id] = {
                            "used_pct": _number(entry.get("used_pct")),
                            "resets_at": _number(entry.get("resets_at")),
                        }
                    else:
                        windows[window_id] = None
                read_at = _number(reading.get("read_at"))
            elif "error" in reading:
                error = reading.get("error")
        stale = windows is not None and read_at is not None and now - read_at > bound
        mark = marks.get(name)
        providers[name] = {
            "state": quota.provider_state(reading, mark, now, stale_after=bound, low_pct=low_pct),
            "windows": windows,
            "read_at": read_at,
            "stale": stale,
            "mark": dict(mark) if isinstance(mark, dict) else None,
            "reader": getattr(provider, "reader", "") or "",
            "error": error if isinstance(error, str) else None,
        }
    return {"now": now, "providers": providers}


def _plain_windows(reading):
    """A reader's `Reading` as plain ledger windows: id to used and reset."""
    raw = reading["windows"] if isinstance(reading, dict) else reading.windows
    windows = {}
    for window_id, entry in raw.items():
        if isinstance(entry, dict):
            used, resets_at = entry.get("used_pct"), entry.get("resets_at")
        else:
            used, resets_at = entry.used_pct, entry.resets_at
        windows[window_id] = {"used_pct": used, "resets_at": resets_at}
    return windows


def refresh(pool, ledger, now, *, only=None, load_reader=accounts.load_reader):
    """Run each requested reader; record readings or failures.

    Returns `(ledger, results)`: `results` maps each provider name to
    `(ok, detail)`, None detail on success, the trimmed error on failure.
    A `ReaderError` is recorded, never raised. An unknown `only` name
    raises `QuotaError`; a reader that does not build raises
    `AccountsError` before anything is recorded.
    """
    wanted = list(pool or ())
    if only is not None:
        wanted = [provider for provider in wanted if _pool_name(provider) == only]
        if not wanted:
            raise QuotaError(f"unknown provider {only!r}")
    readers = [(provider, load_reader(provider.reader)) for provider in wanted]
    fresh = ledger
    results = {}
    for provider, reader in readers:
        name = _pool_name(provider)
        try:
            reading = reader.read(now)
        except accounts.ReaderError as err:
            fresh = quota_ledger.record_failure(fresh, name, str(err), now)
            results[name] = (False, quota_ledger.trim_error(str(err)))
            continue
        fresh = quota_ledger.record_reading(fresh, name, _plain_windows(reading), now)
        results[name] = (True, None)
    return fresh, results


def _cooldown(provider) -> float:
    cooldown = getattr(provider, "cooldown_s", None)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)):
        return accounts.DEFAULT_COOLDOWN_S
    return float(cooldown)


def mark_provider(ledger, pool, provider, now, *, until=None, reason="", source="manual"):
    """Stamp a mark for `provider`; the default `until` is now plus cooldown.

    Returns `(ledger, moved)`: False when the stored mark already reaches
    further (the verb exits 1 then). An unknown provider or a bad `until`
    raises `QuotaError`.
    """
    match = next((entry for entry in pool or () if _pool_name(entry) == provider), None)
    if match is None:
        raise QuotaError(f"unknown provider {provider!r}")
    moment = _number(now)
    if moment is None:
        raise QuotaError(f"bad timestamp {now!r}")
    if until is None:
        limit = moment + _cooldown(match)
    else:
        try:
            limit = float(until)
        except (TypeError, ValueError):
            raise QuotaError(f"bad --until {until!r}: epoch seconds expected") from None
        if not math.isfinite(limit):
            raise QuotaError(f"bad --until {until!r}: epoch seconds expected")
    return quota_ledger.post_mark(ledger, provider, limit, reason, source)


def note_limit_error(ledger, pool, text, now, *, load_reader=accounts.load_reader, source="tail"):
    """Post marks for providers whose reader parses `text` as a limit error.

    Returns `(ledger, marked)`: the providers whose mark moved. A reader
    that does not recognize the text returns None and is skipped; one
    that does not build raises before anything is recorded.
    """
    readers = [(provider, load_reader(provider.reader)) for provider in pool or ()]
    fresh, marked = ledger, []
    for provider, reader in readers:
        until = reader.parse_limit_error(text, now)
        if until is None:
            continue
        fresh, moved = quota_ledger.post_mark(fresh, _pool_name(provider), until, source=source)
        if moved:
            marked.append(_pool_name(provider))
    return fresh, marked


def _low_pct():
    return settings.load()["quota.low_pct"]


def _err(text):
    print(f"corral: {text}", file=sys.stderr)


def _show(view) -> None:
    if not view["providers"]:
        print("no providers")
        return
    for name, entry in view["providers"].items():
        line = f"{name}: {entry['state']}"
        if entry["windows"]:
            windows = ", ".join(
                f"{window_id} {window['used_pct']}%"
                for window_id, window in entry["windows"].items()
                if isinstance(window, dict)
            )
            if windows:
                line += f" ({windows})"
        if entry["stale"]:
            line += " (stale)"
        if entry["mark"] is not None:
            line += f" (mark until {entry['mark'].get('until')})"
        if entry["error"] is not None:
            line += f" (reader failed: {entry['error']})"
        print(line)


def cmd_show(args: argparse.Namespace) -> int:
    view = snapshot(accounts.providers(), quota_ledger.load(), time.time(), low_pct=_low_pct())
    if getattr(args, "json", False):
        print(json.dumps(view, sort_keys=True))
    else:
        _show(view)
    return OK


def cmd_refresh(args: argparse.Namespace) -> int:
    pool = accounts.providers()
    targets = [
        provider
        for provider in pool
        if args.provider is None or _pool_name(provider) == args.provider
    ]
    as_json = getattr(args, "json", False)
    if not targets:
        if args.provider is not None:
            raise QuotaError(f"unknown provider {args.provider!r}")
        if as_json:
            print("{}")
        else:
            print("no providers")
        return OK
    results: dict = {}

    def change(ledger):
        fresh, seen = refresh(pool, ledger, time.time(), only=args.provider)
        results.update(seen)
        return fresh

    quota_ledger.update(change)
    if as_json:
        for name, (ok, detail) in results.items():
            if not ok:
                _err(f"{name}: {detail}")
        print(
            json.dumps(
                {name: {"ok": ok, "error": detail} for name, (ok, detail) in results.items()},
                sort_keys=True,
            )
        )
    else:
        for name, (ok, detail) in results.items():
            if ok:
                print(f"{name}: ok")
            else:
                _err(f"{name}: {detail}")
    if results and all(not ok for ok, _ in results.values()):
        return UNREACHABLE
    return OK


def cmd_mark(args: argparse.Namespace) -> int:
    saved: list = []

    def change(ledger):
        fresh, moved = mark_provider(
            ledger,
            accounts.providers(),
            args.provider,
            time.time(),
            until=args.until,
            reason=args.reason,
        )
        saved.append((moved, fresh["marks"][args.provider]["until"]))
        return fresh

    quota_ledger.update(change)
    moved, until = saved[0]
    if not moved:
        _err(f"mark for {args.provider!r} already stands until {until}")
        return NOTHING
    print(f"marked {args.provider} until {until}")
    return OK


def cmd_unmark(args: argparse.Namespace) -> int:
    saved: list = []

    def change(ledger):
        fresh, released = quota_ledger.release_mark(ledger, args.provider)
        saved.append(released)
        return fresh

    quota_ledger.update(change)
    if not saved[0]:
        _err(f"no mark for {args.provider!r}")
        return NOTHING
    line = f"released mark for {args.provider}"
    if args.reason:
        line += f": {args.reason}"
    print(line)
    return OK


def cmd_quota(args: argparse.Namespace) -> int:
    """Run the `quota` verb named by `args.quota_verb`; bare `quota` shows."""
    try:
        verb = args.quota_verb or "show"
        if verb == "show":
            return cmd_show(args)
        if verb == "refresh":
            return cmd_refresh(args)
        if verb == "mark":
            return cmd_mark(args)
        if verb == "unmark":
            return cmd_unmark(args)
    except (
        QuotaError,
        accounts.AccountsError,
        quota_ledger.LedgerError,
        settings.SettingsError,
        paths.PathError,
    ) as err:
        _err(str(err))
        return USAGE
    raise AssertionError(f"unknown quota verb {verb!r}")


def add_parser(sub) -> None:
    """Register `quota` and its verbs on the top-level subparsers."""
    parser = sub.add_parser("quota", help="the provider pool and its quota ledger")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    verbs = parser.add_subparsers(dest="quota_verb")
    p = verbs.add_parser("refresh", help="run the readers, record readings or failures")
    p.add_argument("provider", nargs="?", default=None, help="only this provider")
    p.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output",
    )
    p = verbs.add_parser("mark", help="block a provider until a reset")
    p.add_argument("provider", help="the provider to block")
    p.add_argument(
        "--until",
        default=None,
        help="epoch seconds the mark lifts (default: now plus the cooldown)",
    )
    p.add_argument("--reason", default="", help="why the mark was posted")
    p = verbs.add_parser("unmark", help="release a provider's mark early")
    p.add_argument("provider", help="the provider to unblock")
    p.add_argument("--reason", default="", help="why the mark is released")
    parser.set_defaults(func=cmd_quota)
