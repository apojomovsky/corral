"""The host-wide resource ledger and the single-instance guard.

One JSON file per host, resolved by ``paths.host_ledger_path`` and shared
by every project on the machine, keyed by project with a host budget and
self-reported foreign claims. A marker file beside it records the state
dir that created this host's ledger; a launch from another state dir on
the same host refuses, so two fleets never share a host unknowingly.

Every read-modify-write holds a timed flock plus an atomic write, the
quota ledger precedent with a deadline: a lock still busy past
``LOCK_TIMEOUT_S`` refuses with exit 75 instead of blocking forever. A
missing file is tolerated only by creating it under the lock; a
present-but-corrupt file is never reinitialized, the launch refuses with
exit 4. The only public writers are ``ensure`` and ``update``: both take
the state dir and check the marker first, so no writer bypasses the
guard. Drop-in claim files beside the ledger merge into every read and
are stripped from every write, so the file never stores them. Number
semantics (floors, weights, ceilings, role charges) live in
``resources``; this module only stores the envelope and the marker.
"""

import contextlib
import errno
import fcntl
import json
import math
import os
import time
from collections.abc import Callable

from corral import paths

#: The ledger version this corral reads and writes. A file with another
#: version refuses: it was written by a newer corral, or it is corrupt.
LEDGER_VERSION = 1

#: The marker beside the ledger holding the state dir that owns this host.
MARKER_NAME = "host-state-dir"

#: The directory beside the ledger holding drop-in foreign-claim JSON files.
#: An external fleet declares a claim by dropping one file per claim here;
#: the files merge with the ledger's own entries at read and are never
#: written back into the ledger.
FOREIGN_CLAIMS_DIR = "foreign-claims.d"

#: The keys one drop-in claim file may hold; anything else is a typo.
CLAIM_KEYS = ("name", "cpu_m", "memory_mb", "pids", "claimed_at")

#: Suffix of the lock sidecar beside the ledger file.
LOCK_SUFFIX = ".lock"

#: How long an updater waits for the ledger lock before refusing (exit 75).
LOCK_TIMEOUT_S = 5.0

#: Pause between lock retries.
LOCK_POLL_S = 0.05


class LedgerError(ValueError):
    """The host ledger file is corrupt or a ledger argument is invalid (exit 4)."""

    exit_code = 4


class StateDirError(LedgerError):
    """This host's ledger belongs to another state dir (exit 4)."""

    exit_code = 4


class LedgerLocked(Exception):
    """The ledger lock stayed busy past the timeout (exit 75, try later).

    A sibling of ``LedgerError``, never a subclass: an ``except
    LedgerError`` handler must never turn a lock timeout into exit 4.
    """

    exit_code = 75


#: The lock seams: production locks with fcntl, waits on the monotonic
#: clock and sleeps between retries; tests assign fakes.
_flock = fcntl.flock
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic


def ledger_path() -> str:
    """Full path of the host ledger file; the directory comes from ``corral.paths``."""
    return paths.host_ledger_path()


def marker_path(path: str | None = None) -> str:
    """Full path of the state-dir marker beside the ledger file."""
    target = path or ledger_path()
    return os.path.join(os.path.dirname(target) or ".", MARKER_NAME)


def claims_dir(path: str | None = None) -> str:
    """The drop-in foreign-claim directory beside the ledger file.

    Beside the ledger, so a shared pool never moves under a running
    fleet: the same reason the ledger path itself ignores the state-dir
    override. Every reader of drop-in claims goes through this helper.
    """
    target = path or ledger_path()
    return os.path.join(os.path.dirname(target) or ".", FOREIGN_CLAIMS_DIR)


def _claim_amount(value: object, what: str, target: str) -> int:
    """A whole claim amount of zero or more, else a LedgerError naming it."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerError(f"host ledger {target} is corrupt: {what} must be zero or more")
    return value


def _drop_in(path: str, mtime: float) -> dict:
    """One drop-in file as a claim dict; anything else is a LedgerError naming it."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise LedgerError(f"host ledger {path} is corrupt: cannot read the claim: {exc}") from exc
    if not isinstance(raw, dict):
        raise LedgerError(f"host ledger {path} is corrupt: a claim must be an object")
    for key in raw:
        if key not in CLAIM_KEYS:
            raise LedgerError(f"host ledger {path} is corrupt: a claim has no key {key!r}")
    name = raw.get("name", "")
    if not isinstance(name, str) or not name:
        raise LedgerError(f"host ledger {path} is corrupt: a claim needs a non-empty name")
    claimed_at = raw.get("claimed_at", mtime)
    if isinstance(claimed_at, bool) or not isinstance(claimed_at, (int, float)):
        raise LedgerError(f"host ledger {path} is corrupt: claimed_at must be a number of seconds")
    if not math.isfinite(claimed_at):
        raise LedgerError(f"host ledger {path} is corrupt: claimed_at must be a finite number")
    return {
        "name": name,
        "cpu_m": _claim_amount(raw.get("cpu_m", 0), "cpu_m", path),
        "memory_mb": _claim_amount(raw.get("memory_mb", 0), "memory_mb", path),
        "pids": _claim_amount(raw.get("pids", 0), "pids", path),
        "claimed_at": claimed_at,
    }


def drop_in_claims(directory: str | None = None) -> list[dict]:
    """The drop-in foreign claims, sorted by file name.

    A missing directory reads as no claims. Anything else unreadable, or
    any malformed file, is a LedgerError naming it: a claim the host
    cannot prove must refuse rather than admit over capacity.
    """
    target = directory or claims_dir()
    try:
        names = sorted(os.listdir(target))
    except FileNotFoundError:
        return []
    except OSError as err:
        raise LedgerError(f"cannot list the foreign-claim dir at {target}: {err}") from None
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(target, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError as err:
            raise LedgerError(f"host ledger {path} is corrupt: cannot stat it: {err}") from None
        found.append(_drop_in(path, mtime))
    return found


def ignored_drop_ins(directory: str | None = None) -> list[str]:
    """The drop-in dir entry names no claim file holds, sorted.

    A typo such as ``desk.jsnm`` never counts as a claim; the doctor
    names these entries so the miss is visible. A missing directory reads
    as none. Anything else unreadable is a LedgerError naming it: like a
    malformed claim, an unlistable dir must refuse, never read as empty.
    """
    target = directory or claims_dir()
    try:
        names = sorted(os.listdir(target))
    except FileNotFoundError:
        return []
    except OSError as err:
        raise LedgerError(f"cannot list the foreign-claim dir at {target}: {err}") from None
    return [name for name in names if not name.endswith(".json")]


def _with_drop_ins(ledger: dict, target: str) -> dict:
    """`ledger` with the drop-in claims merged after its own entries.

    A duplicate name across the ledger and the drop-ins refuses: two
    entries would charge the pool twice for one declarer.
    """
    return _merge_drop_ins(ledger, drop_in_claims(claims_dir(target)), target)


def _merge_drop_ins(ledger: dict, drops: list[dict], target: str) -> dict:
    """`ledger` with `drops` merged after its own entries, duplicates refused."""
    names = {entry["name"] for entry in ledger["foreign"]}
    for entry in drops:
        if entry["name"] in names:
            raise LedgerError(
                f"host ledger {target} is corrupt: "
                f"duplicate foreign claim {entry['name']!r} in the drop-in dir"
            )
        names.add(entry["name"])
    merged = dict(ledger)
    merged["foreign"] = [*ledger["foreign"], *drops]
    return merged


def _without_drop_ins(ledger: dict, drops: list[dict]) -> dict:
    """`ledger` with only the entries the ledger file itself owns.

    The file never stores drop-in claims: merging happens at read, so a
    read-modify-write strips the entries merged at read, by identity and
    by value. Value matters because a change may return copies built by
    value: a copy of a drop-in is still a drop-in. A drop-in that appears
    after the read is not stripped: stripping by a fresh name listing
    would mistake a ledger-owned claim of the same name for a drop-in
    and lose it from the persisted file.
    """
    merged = {id(entry) for entry in drops}
    stripped = dict(ledger)
    stripped["foreign"] = [
        entry
        for entry in ledger.get("foreign", [])
        if id(entry) not in merged and entry not in drops
    ]
    return stripped


def empty() -> dict:
    """A ledger with version one, no host budget, no projects and no claims."""
    return {"version": LEDGER_VERSION, "host_budget": None, "projects": {}, "foreign": []}


def _check_ledger(ledger: object, target: str) -> dict:
    """The ledger as a validated dict; raises ``LedgerError`` when unusable."""
    if not isinstance(ledger, dict):
        raise LedgerError(
            f"host ledger {target} is corrupt: expected an object, got {type(ledger).__name__}"
        )
    version = ledger.get("version")
    if version != LEDGER_VERSION:
        raise LedgerError(
            f"host ledger {target} has version {version!r}; "
            f"this corral reads version {LEDGER_VERSION}"
        )
    host_budget = ledger.get("host_budget")
    if host_budget is not None and not isinstance(host_budget, dict):
        raise LedgerError(f"host ledger {target} is corrupt: host_budget must be an object or null")
    projects = ledger.get("projects", {})
    if (
        not isinstance(projects, dict)
        or any(not isinstance(name, str) for name in projects)
        or any(not isinstance(entry, dict) for entry in projects.values())
    ):
        raise LedgerError(f"host ledger {target} is corrupt: projects must map names to objects")
    foreign = ledger.get("foreign", [])
    if not isinstance(foreign, list) or any(not isinstance(entry, dict) for entry in foreign):
        raise LedgerError(f"host ledger {target} is corrupt: foreign must be a list of objects")
    seen = set()
    for index, entry in enumerate(foreign):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise LedgerError(
                f"host ledger {target} is corrupt: foreign claim {index} needs a non-empty name"
            )
        if name in seen:
            raise LedgerError(f"host ledger {target} is corrupt: duplicate foreign claim {name}")
        seen.add(name)
    checked = dict(ledger)
    checked["version"] = LEDGER_VERSION
    checked["host_budget"] = host_budget
    checked["projects"] = dict(projects)
    checked["foreign"] = [dict(entry) for entry in foreign]
    return checked


@contextlib.contextmanager
def _locked(target: str, timeout: float = LOCK_TIMEOUT_S):
    """Hold an exclusive flock on `target`, waiting up to `timeout` seconds.

    The first try is immediate; while another process holds the lock the
    wait retries every ``LOCK_POLL_S``. Contention past the deadline
    raises ``LedgerLocked``; setup and unexpected lock failures raise
    ``LedgerError``. The kernel releases the lock when the holder dies,
    so a dead holder is already reclaimed when the next waiter tries.
    """
    directory = os.path.dirname(target) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as err:
        raise LedgerError(f"cannot set up the host ledger lock for {target}: {err}") from None
    try:
        handle = open(target + LOCK_SUFFIX, "a+")
    except OSError as err:
        raise LedgerError(f"cannot open the host ledger lock for {target}: {err}") from None
    with handle:
        deadline = _monotonic() + timeout
        while True:
            try:
                _flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as err:
                if err.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise LedgerError(f"cannot lock the host ledger at {target}: {err}") from None
            if _monotonic() >= deadline:
                raise LedgerLocked(
                    f"host ledger {target} is locked by another process: "
                    f"waited {timeout}s, try later"
                ) from None
            _sleep(LOCK_POLL_S)
        try:
            yield
        finally:
            _flock(handle, fcntl.LOCK_UN)


def _write(target: str, ledger: dict) -> dict:
    checked = _check_ledger(ledger, target)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    paths.atomic_write_text(target, json.dumps(checked, sort_keys=True))
    return checked


def _read_marker(target: str) -> str | None:
    """The state dir recorded beside the ledger, None when never created."""
    try:
        with open(marker_path(target), encoding="utf-8") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise LedgerError(
            f"cannot read the host ledger marker at {marker_path(target)}: {err}"
        ) from None


def _write_marker(target: str, state_dir: str) -> None:
    marker = marker_path(target)
    os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
    paths.atomic_write_text(marker, state_dir)


def _load_file(target: str) -> dict:
    """The ledger the file at `target` holds, without drop-in claims."""
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as exc:
        raise LedgerError(f"host ledger {target} is corrupt: {exc}") from exc
    return _check_ledger(raw, target)


def _prepare(target: str, state_dir: str) -> tuple[dict, list[dict]]:
    """The merged ledger at `target` under the lock, with its drop-ins.

    Created with its marker when missing: a missing file starts empty
    and is written back by the caller; a corrupt one raises and is never
    reset. A missing marker adopts the caller's state dir; a marker
    naming another dir raises ``StateDirError`` naming both dirs and the
    marker. The returned drops are the entries merged into the ledger;
    only the file's own entries are ever written back.
    """
    try:
        stored = _load_file(target)
    except LedgerError:
        raise
    created = not os.path.exists(target)
    marker = _read_marker(target)
    if marker is None:
        _write_marker(target, state_dir)
    elif marker != state_dir:
        raise StateDirError(
            f"host ledger {target} belongs to state dir {marker}, not {state_dir}; "
            f"this host already runs a fleet under another state dir: "
            f"move the marker at {marker_path(target)} by hand to move this host"
        )
    if created:
        _write(target, stored)
    drops = drop_in_claims(claims_dir(target))
    return _merge_drop_ins(stored, drops, target), drops


def load(path: str | None = None) -> dict:
    """The ledger at `path` (default: the host ledger file), drop-ins merged.

    A missing file reads as an empty ledger. Anything unreadable or
    wrong-shaped raises ``LedgerError``: a corrupt file never reads as an
    empty ledger that would silently clear stored shares.
    """
    target = path or ledger_path()
    return _with_drop_ins(_load_file(target), target)


def ensure(
    *, state_dir: str | None = None, path: str | None = None, timeout: float = LOCK_TIMEOUT_S
) -> dict:
    """The ledger with this host's guard in place, creating both under the lock.

    First creation writes the ledger and records `state_dir` in the
    marker beside it. A later call from another state dir raises
    ``StateDirError``; the marker is never updated by a call. A
    caller-supplied `state_dir` is normalized, so a trailing-slash
    spelling names the same fleet. All three arguments are keyword-only.
    """
    target = path or ledger_path()
    want = os.path.normpath(state_dir) if state_dir else paths.state_dir()
    with _locked(target, timeout):
        merged, _drops = _prepare(target, want)
        return merged


def update(
    change,
    *,
    path: str | None = None,
    state_dir: str | None = None,
    timeout: float = LOCK_TIMEOUT_S,
) -> dict:
    """Locked read-modify-write: `change` maps the ledger to its replacement.

    The argument to `change` is read-only: mutate it and return None and
    the mutation is dropped on disk while the mutated object is returned,
    so a change that should land must return its replacement. The guard
    runs first: a foreign state dir raises ``StateDirError`` before
    `change` is called. A missing file starts empty and is always
    materialized, even when `change` returns None for no change: readers
    tolerate a missing file only by creating it under the lock. A
    corrupt file raises instead of being overwritten. All arguments but
    `change` are keyword-only.
    """
    target = path or ledger_path()
    want = os.path.normpath(state_dir) if state_dir else paths.state_dir()
    with _locked(target, timeout):
        ledger, drops = _prepare(target, want)
        result = change(ledger)
        if result is None:
            return ledger
        return _write(target, _without_drop_ins(result, drops))
