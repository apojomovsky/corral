"""Provider accounts and their quota readers: one pool per host.

The accounts file lists the providers every project on this host shares:
each `[[provider]]` names a provider and the reader that reports its
quota, plus how long a failure mark covers it (`cooldown`) and how fresh
a reading must be to count (`stale_after`). A top-level `reader_path`
list names the directories readers live in. A missing file means an
empty pool, never an error. Later slices add the quota gate that reads
through these readers.
"""

import contextlib
import importlib
import os
import re
import sys
import tomllib
from collections.abc import Mapping
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
    """One quota window: its used percent (0..100) and reset epoch, if known."""

    used_pct: float
    resets_at: float | None


class Reading(NamedTuple):
    """What one reader call saw: window id to usage."""

    windows: Mapping[str, Window]


class Reader(Protocol):
    """A quota reader: reports one provider's windows, or raises ReaderError."""

    def read(self, now: float) -> Reading:
        """Current quota windows; raises ReaderError when unreadable."""
        ...

    def parse_limit_error(self, text: str, now: float) -> float | None:
        """Epoch the text blocks this provider until, or None when it is not a limit error."""
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


def _reader_paths(raw: Mapping, path: str) -> list[str]:
    """The expanded reader directories in `raw`, the one resolver for this fact.

    Each entry expands `~` and environment variables, then must be
    absolute: a relative entry stays relative to nothing, so it is a
    configuration error rather than resolved against the cwd. Each
    directory must exist and be readable, named in the error when not.
    """
    entries = raw.get("reader_path", [])
    if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
        raise AccountsError(f"{path}: reader_path must be a list of directories")
    resolved = []
    for entry in entries:
        expanded = os.path.expanduser(os.path.expandvars(entry))
        if not os.path.isabs(expanded):
            raise AccountsError(f"{path}: reader_path {entry!r} must be absolute after expansion")
        resolved.append(os.path.normpath(expanded))
    for expanded in resolved:
        if not os.path.isdir(expanded):
            raise AccountsError(f"{path}: reader_path {expanded!r} is missing or not a directory")
        if not os.access(expanded, os.R_OK | os.X_OK):
            raise AccountsError(f"{path}: reader_path {expanded!r} is not readable")
    return resolved


def _load(path: str) -> tuple[list[Provider], list[str]]:
    """The providers and reader directories in the file at `path`.

    A missing file is an empty pool with no reader dirs, never an
    error. Every reader of either fact goes through here.
    """
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return [], []
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
    return found, _reader_paths(raw, path)


@contextlib.contextmanager
def _with_reader_path(dirs: list[str]):
    """`dirs` prepended to `sys.path`, restored afterwards.

    The only place that touches `sys.path`: readers import while the
    dirs are present, and every other module never sees them.
    """
    saved = list(sys.path)
    try:
        sys.path[:] = [*dirs, *[entry for entry in saved if entry not in dirs]]
        yield
    finally:
        sys.path[:] = saved


def pool(config_dir: str | None = None) -> tuple[list[Provider], list[str]]:
    """The providers and reader dirs in the accounts file, from one read.

    A missing file is an empty pool with no reader dirs, never an error.
    Callers that list the pool and then load its readers take both from
    here, so the listing and the import cannot disagree.
    """
    return _load(accounts_path(config_dir))


def providers(config_dir: str | None = None) -> list[Provider]:
    """Every provider in the accounts file, in file order; [] when there is no file."""
    found, _ = pool(config_dir)
    return found


def _resolve(entry: str, where: str) -> type:
    module_name, sep, attr_path = entry.partition(":")
    if not sep or not module_name or not attr_path:
        raise AccountsError(f"{where}: reader {entry!r} must look like module:Class")
    try:
        target: object = importlib.import_module(module_name)
    except Exception as err:
        raise AccountsError(f"{where}: reader {entry!r} does not import: {err}") from None
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError:
            raise AccountsError(f"{where}: reader {entry!r} has no {attr!r}") from None
    if not isinstance(target, type):
        raise AccountsError(f"{where}: reader {entry!r} is not a class")
    return target


def load_reader(
    entry: str, config_dir: str | None = None, *, reader_dirs: list[str] | None = None
) -> Reader:
    """Build the reader named by `module:Class`; never a silent skip.

    The reader dirs are on `sys.path` only while the module imports
    and the class builds, restored before returning. Pass the dirs
    from `pool` when the caller already holds them, so the listing and
    the import come from one read; otherwise they are read here.
    Raises AccountsError when the file or a reader dir is unusable,
    when the entry does not import, or when the class cannot be built.
    A ReaderError from `read()` stays a ReaderError: the reader
    loaded, the provider just did not answer.
    """
    dirs = reader_dirs if reader_dirs is not None else pool(config_dir)[1]
    with _with_reader_path(dirs):
        cls = _resolve(entry, "accounts")
        try:
            return cls()
        except Exception as err:
            raise AccountsError(f"accounts: reader {entry!r} failed to build: {err}") from None


def check(config_dir: str | None = None) -> list[str]:
    """Problems with the accounts file: parse, names, reader dirs, and imports.

    [] means the pool is usable (a missing file counts as usable: the
    pool is just empty). Importing never calls `read`, and `sys.path`
    is restored before returning.
    """
    path = accounts_path(config_dir)
    try:
        found, dirs = _load(path)
    except AccountsError as err:
        return [str(err)]
    problems = []
    with _with_reader_path(dirs):
        for provider in found:
            where = f"{path} [[provider]] {provider.name!r}"
            try:
                _resolve(provider.reader, where)
            except AccountsError as err:
                problems.append(str(err))
    return problems
