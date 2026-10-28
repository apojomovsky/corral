"""The machine-local quota ledger: usage readings and reactive failure marks.

One JSON file per host, ``quota.json`` under the host state dir. Readers
record their latest usage windows here and stamp reactive marks when a
provider fails without a usable reading; later slices decide from the
ledger instead of re-reading. The ledger never guesses: a failed read
stores its error, never a fabricated number.
"""

import contextlib
import fcntl
import json
import math
import os

from corral import paths

LEDGER_NAME = "quota.json"
LOCK_SUFFIX = ".lock"
#: Stored failure text is trimmed to this; an unparseable payload could be
#: large, and the ledger is served to status readers as-is.
ERROR_TEXT_LIMIT = 500


class LedgerError(ValueError):
    """The ledger file is corrupt or a ledger argument is invalid."""


def ledger_path(state_dir: str | None = None) -> str:
    """Full path of the ledger file; the directory comes from ``corral.paths``."""
    return os.path.join(state_dir or paths.state_dir(), LEDGER_NAME)


def empty() -> dict:
    """A ledger with no readings and no marks."""
    return {"readings": {}, "marks": {}}


def trim_error(text: object) -> str:
    """Failure text as one trimmed line, capped at ``ERROR_TEXT_LIMIT``."""
    return " ".join(str(text).split())[:ERROR_TEXT_LIMIT]


def _as_epoch(value: object, label: str) -> float:
    """`value` as finite epoch seconds; raises ``LedgerError`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LedgerError(f"{label} must be epoch seconds, got {value!r}")
    if not math.isfinite(value):
        raise LedgerError(f"{label} must be finite, got {value!r}")
    return float(value)


def _check_provider(provider: object) -> str:
    if not isinstance(provider, str) or not provider:
        raise LedgerError(f"provider must be a non-empty string, got {provider!r}")
    return provider


def _check_now(now: object) -> float:
    return _as_epoch(now, "now")


def _check_windows(windows: object) -> dict:
    if not isinstance(windows, dict):
        raise LedgerError(f"windows must be an object, got {windows!r}")
    checked = {}
    for window_id, entry in windows.items():
        if not isinstance(window_id, str) or not window_id:
            raise LedgerError(f"window id must be a non-empty string, got {window_id!r}")
        if not isinstance(entry, dict):
            raise LedgerError(f"window {window_id!r} must be an object")
        used = _as_epoch(entry.get("used_pct"), f"window {window_id!r} used_pct")
        resets_at = entry.get("resets_at")
        resets_at = (
            None if resets_at is None else _as_epoch(resets_at, f"window {window_id!r} resets_at")
        )
        kept = {"used_pct": used, "resets_at": resets_at}
        for key, value in entry.items():
            if key not in kept:
                kept[key] = value
        checked[window_id] = kept
    return checked


def _check_reading(provider: str, entry: object) -> dict:
    if not isinstance(entry, dict):
        raise LedgerError(f"reading for {provider!r} must be an object")
    if "windows" in entry:
        read_at = _as_epoch(entry.get("read_at"), f"reading for {provider!r} read_at")
        checked = {"windows": _check_windows(entry["windows"]), "read_at": read_at}
    elif "error" in entry:
        error = entry.get("error")
        if not isinstance(error, str):
            raise LedgerError(f"reading failure for {provider!r} needs a str error")
        at = _as_epoch(entry.get("at"), f"reading failure for {provider!r} at")
        checked = {"error": error, "at": at}
    else:
        raise LedgerError(f"reading for {provider!r} has neither windows nor error")
    for key, value in entry.items():
        if key not in checked:
            checked[key] = value
    return checked


def _check_mark(provider: str, entry: object) -> dict:
    if not isinstance(entry, dict):
        raise LedgerError(f"mark for {provider!r} must be an object")
    until = _as_epoch(entry.get("until"), f"mark for {provider!r} until")
    checked = {"until": until}
    for key in ("reason", "source"):
        value = entry.get(key, "")
        if not isinstance(value, str):
            raise LedgerError(f"mark {key} for {provider!r} must be a string")
        checked[key] = value
    for key, value in entry.items():
        if key not in checked:
            checked[key] = value
    return checked


def _check_ledger(ledger: object) -> dict:
    """The ledger as a validated dict; raises ``LedgerError`` when unusable."""
    if not isinstance(ledger, dict):
        raise LedgerError(f"ledger must be an object, got {type(ledger).__name__}")
    readings = ledger.get("readings", {})
    marks = ledger.get("marks", {})
    if not isinstance(readings, dict):
        raise LedgerError("ledger readings must be an object")
    if not isinstance(marks, dict):
        raise LedgerError("ledger marks must be an object")
    checked = dict(ledger)
    checked["readings"] = {
        _check_provider(provider): _check_reading(provider, entry)
        for provider, entry in readings.items()
    }
    checked["marks"] = {
        _check_provider(provider): _check_mark(provider, entry) for provider, entry in marks.items()
    }
    return checked


def record_reading(ledger: dict, provider: str, windows: dict, now: float) -> dict:
    """Store a successful read for `provider`, replacing any recorded failure.

    Returns a new ledger; the input is unchanged.
    """
    moment = _check_now(now)
    _check_provider(provider)
    checked = _check_ledger(ledger)
    fresh = dict(checked)
    readings = dict(checked["readings"])
    readings[provider] = {"windows": _check_windows(windows), "read_at": moment}
    fresh["readings"] = readings
    return fresh


def record_failure(ledger: dict, provider: str, error: object, now: float) -> dict:
    """Store a failed read for `provider`: its trimmed error, never a number.

    Returns a new ledger; the input is unchanged.
    """
    moment = _check_now(now)
    _check_provider(provider)
    checked = _check_ledger(ledger)
    fresh = dict(checked)
    readings = dict(checked["readings"])
    readings[provider] = {"error": trim_error(error), "at": moment}
    fresh["readings"] = readings
    return fresh


def post_mark(
    ledger: dict, provider: str, until: float, reason: str = "", source: str = ""
) -> tuple:
    """Stamp a failure mark for `provider`; it only ever moves `until` later.

    Returns ``(ledger, moved)``: posting an `until` at or before the
    stored one is a no-op returning the ledger unchanged and moved False.
    """
    _check_provider(provider)
    limit = _as_epoch(until, "mark until")
    for label, value in (("reason", reason), ("source", source)):
        if not isinstance(value, str):
            raise LedgerError(f"mark {label} must be a string, got {value!r}")
    checked = _check_ledger(ledger)
    old = checked["marks"].get(provider)
    if old is not None and not limit > old["until"]:
        return checked, False
    fresh = dict(checked)
    marks = dict(checked["marks"])
    marks[provider] = {"until": limit, "reason": reason, "source": source}
    fresh["marks"] = marks
    return fresh, True


def release_mark(ledger: dict, provider: str) -> tuple:
    """Drop the stored mark for `provider`. Returns ``(ledger, released)``.

    Releasing a provider with no stored mark is a no-op returning the
    ledger unchanged and released False.
    """
    _check_provider(provider)
    checked = _check_ledger(ledger)
    if provider not in checked["marks"]:
        return checked, False
    fresh = dict(checked)
    marks = dict(checked["marks"])
    del marks[provider]
    fresh["marks"] = marks
    return fresh, True


def active_mark(ledger: dict, provider: str, now: float) -> dict | None:
    """The stored mark for `provider` while `now` is still before its `until`."""
    moment = _check_now(now)
    _check_provider(provider)
    entry = _check_ledger(ledger)["marks"].get(provider)
    if entry is not None and moment < entry["until"]:
        return dict(entry)
    return None


def prune(ledger: dict, now: float) -> tuple:
    """Drop marks whose `until` has passed. Returns ``(ledger, dropped)``."""
    moment = _check_now(now)
    checked = _check_ledger(ledger)
    marks = checked["marks"]
    kept = {provider: entry for provider, entry in marks.items() if moment < entry["until"]}
    fresh = dict(checked)
    fresh["marks"] = kept
    return fresh, sorted(set(marks) - set(kept))


@contextlib.contextmanager
def _locked(target: str):
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    with open(target + LOCK_SUFFIX, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write(target: str, ledger: dict) -> dict:
    checked = _check_ledger(ledger)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    paths.atomic_write_text(target, json.dumps(checked, sort_keys=True))
    return checked


def load(path: str | None = None) -> dict:
    """The ledger at `path` (default: the host state dir's ``quota.json``).

    A missing file reads as an empty ledger. Anything unreadable or
    wrong-shaped raises ``LedgerError``: a corrupt file never reads as an
    empty ledger that would silently clear stored marks.
    """
    target = path or ledger_path()
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as exc:
        raise LedgerError(f"quota ledger {target} is corrupt: {exc}") from exc
    try:
        return _check_ledger(raw)
    except LedgerError as exc:
        raise LedgerError(f"quota ledger {target} is corrupt: {exc}") from exc


def save(ledger: dict, path: str | None = None) -> dict:
    """Persist `ledger` atomically under the ledger lock. Returns it checked."""
    target = path or ledger_path()
    with _locked(target):
        return _write(target, ledger)


def update(change, path: str | None = None) -> dict:
    """Locked read-modify-write: `change` maps the ledger to its replacement.

    `change` returns None for no change, which writes nothing: a caller
    that found nothing to record leaves the file (and its absence) alone.
    Concurrent writers serialize on the lock, so every update lands;
    a corrupt file raises instead of being overwritten.
    """
    target = path or ledger_path()
    with _locked(target):
        ledger = load(target)
        result = change(ledger)
        if result is None:
            return ledger
        return _write(target, result)
