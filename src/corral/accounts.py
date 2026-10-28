"""Provider accounts and their quota readers: one pool per host.

The accounts file lists the providers every project on this host shares:
each `[[provider]]` names a provider and the reader that reports its
quota, plus how long a failure mark covers it (`cooldown`) and how fresh
a reading must be to count (`stale_after`). A missing file means an
empty pool, never an error. Later slices add the quota gate that reads
through these readers.
"""

import importlib
import os
import re
import tomllib
from dataclasses import dataclass
from typing import NamedTuple, Protocol

from corral import paths

ACCOUNTS_FILE = "accounts.toml"

#: Cover for a failure mark, and freshness bound for a reading, when the
#: file names neither.
DEFAULT_COOLDOWN_S = 3600.0
DEFAULT_STALE_AFTER_S = 900.0

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdSMHD])?\s*$")
_DURATION_MULTIPLIER = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class AccountsError(ValueError):
    """The accounts file is unusable (exit 4, configuration error)."""


class ReaderError(Exception):
    """A quota reader failed; the pass records it and treats the provider as unknown."""


class Window(NamedTuple):
    """One quota window: its id, used fraction, and reset epoch (either may be unknown)."""

    id: str
    used_fraction: float | None
    resets_at: float | None


class Reading(NamedTuple):
    """What one reader call saw: the provider's quota windows."""

    windows: tuple[Window, ...]


class Reader(Protocol):
    """A quota reader: reports one provider's windows, or raises ReaderError."""

    def read(self) -> Reading:
        """Current quota windows; raises ReaderError when unreadable."""
        ...


@dataclass(frozen=True)
class Provider:
    """One `[[provider]]` entry: its name, reader entry point, and durations in seconds."""

    name: str
    reader: str
    cooldown_s: float = DEFAULT_COOLDOWN_S
    stale_after_s: float = DEFAULT_STALE_AFTER_S


def accounts_path(config_dir: str | None = None) -> str:
    """The accounts file: `accounts.toml` in the corral config dir."""
    return os.path.join(config_dir or paths.config_dir(), ACCOUNTS_FILE)


def parse_duration(value: object, where: str) -> float:
    """Seconds for a duration: a number, or `<n>[s|m|h|d]` (bare is seconds).

    Raises AccountsError on anything else, or on a value that is not positive.
    """
    if isinstance(value, bool):
        raise AccountsError(f"{where}: want a duration like 15m, 1h, got {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION_RE.match(value)
        if not match:
            raise AccountsError(f"{where}: want a duration like 15m, 1h, got {value!r}")
        seconds = (
            float(match.group(1))
            * _DURATION_MULTIPLIER[match.group(2).lower() if match.group(2) else "s"]
        )
    else:
        raise AccountsError(f"{where}: want a duration like 15m, 1h, got {value!r}")
    if not seconds > 0:
        raise AccountsError(f"{where}: want a positive duration, got {value!r}")
    return seconds


def _provider(raw: object, where: str) -> Provider:
    if not isinstance(raw, dict):
        raise AccountsError(f"{where}: [[provider]] must be a table")
    for key in raw:
        if key not in ("name", "reader", "cooldown", "stale_after"):
            raise AccountsError(f"{where}: unknown key {key!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise AccountsError(f"{where}: [[provider]] needs a non-empty name")
    entry = raw.get("reader")
    if not isinstance(entry, str) or not entry:
        raise AccountsError(f"{where} [[provider]] {name!r} needs a reader entry point")
    cooldown = (
        parse_duration(raw["cooldown"], f"{where} [[provider]] {name!r} cooldown")
        if "cooldown" in raw
        else DEFAULT_COOLDOWN_S
    )
    stale_after = (
        parse_duration(raw["stale_after"], f"{where} [[provider]] {name!r} stale_after")
        if "stale_after" in raw
        else DEFAULT_STALE_AFTER_S
    )
    return Provider(name=name, reader=entry, cooldown_s=cooldown, stale_after_s=stale_after)


def _read_file(path: str) -> list[Provider]:
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise AccountsError(f"{path}: {err}") from None
    if not isinstance(raw, dict):
        raise AccountsError(f"{path}: want a TOML table")
    entries = raw.get("provider", [])
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise AccountsError(f"{path}: [[provider]] must be an array of tables")
    found = [_provider(item, path) for item in entries]
    seen = set()
    for provider in found:
        if provider.name in seen:
            raise AccountsError(f"{path}: [[provider]] {provider.name!r} appears twice")
        seen.add(provider.name)
    return found


def providers(config_dir: str | None = None) -> list[Provider]:
    """Every provider in the accounts file, in file order; [] when there is no file."""
    return _read_file(accounts_path(config_dir))


def _resolve(entry: str, where: str) -> type:
    module_name, sep, attr_path = entry.partition(":")
    if not sep or not module_name or not attr_path:
        raise AccountsError(f"{where}: reader {entry!r} must look like module:Class")
    try:
        target: object = importlib.import_module(module_name)
    except ImportError as err:
        raise AccountsError(f"{where}: reader {entry!r} does not import: {err}") from None
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError:
            raise AccountsError(f"{where}: reader {entry!r} has no {attr!r}") from None
    if not isinstance(target, type):
        raise AccountsError(f"{where}: reader {entry!r} is not a class")
    return target


def load_reader(entry: str) -> Reader:
    """Build the reader named by `module:Class`; never a silent skip.

    Raises AccountsError when the entry does not import or the class
    cannot be built. A ReaderError from `read()` stays a ReaderError:
    the reader loaded, the provider just did not answer.
    """
    cls = _resolve(entry, "accounts")
    try:
        return cls()
    except Exception as err:
        raise AccountsError(f"accounts: reader {entry!r} failed to build: {err}") from None


def check(config_dir: str | None = None) -> list[str]:
    """Problems with the accounts file: parse, names, and reader imports.

    [] means the pool is usable (a missing file counts as usable: the
    pool is just empty). Importing never calls `read`.
    """
    path = accounts_path(config_dir)
    try:
        found = _read_file(path)
    except AccountsError as err:
        return [str(err)]
    problems = []
    for provider in found:
        where = f"{path} [[provider]] {provider.name!r}"
        try:
            _resolve(provider.reader, where)
        except AccountsError as err:
            problems.append(str(err))
    return problems
