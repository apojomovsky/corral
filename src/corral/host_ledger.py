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
guard. Number semantics (floors, weights, ceilings, role charges) live
in ``resources``; this module only stores the envelope and the marker.
"""

import contextlib
import errno
import fcntl
import json
import os
import time
from collections.abc import Callable

from corral import paths

#: The ledger version this corral reads and writes. A file with another
#: version refuses: it was written by a newer corral, or it is corrupt.
LEDGER_VERSION = 1

#: The marker beside the ledger holding the state dir that owns this host.
MARKER_NAME = "host-state-dir"

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


def _prepare(target: str, state_dir: str) -> dict:
    """The ledger at `target` under the lock, created with its marker when missing.

    A missing file starts empty and is written back by the caller; a
    corrupt one raises and is never reset. A missing marker adopts the
    caller's state dir; a marker naming another dir raises
    ``StateDirError`` naming both dirs and the marker.
    """
    try:
        ledger = load(target)
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
        _write(target, ledger)
    return ledger


def load(path: str | None = None) -> dict:
    """The ledger at `path` (default: the host ledger file).

    A missing file reads as an empty ledger. Anything unreadable or
    wrong-shaped raises ``LedgerError``: a corrupt file never reads as an
    empty ledger that would silently clear stored shares.
    """
    target = path or ledger_path()
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as exc:
        raise LedgerError(f"host ledger {target} is corrupt: {exc}") from exc
    return _check_ledger(raw, target)


def ensure(
    state_dir: str | None = None, path: str | None = None, timeout: float = LOCK_TIMEOUT_S
) -> dict:
    """The ledger with this host's guard in place, creating both under the lock.

    First creation writes the ledger and records `state_dir` in the
    marker beside it. A later call from another state dir raises
    ``StateDirError``; the marker is never updated by a call.
    """
    target = path or ledger_path()
    want = state_dir or paths.state_dir()
    with _locked(target, timeout):
        return _prepare(target, want)


def update(
    change,
    path: str | None = None,
    state_dir: str | None = None,
    timeout: float = LOCK_TIMEOUT_S,
) -> dict:
    """Locked read-modify-write: `change` maps the ledger to its replacement.

    The guard runs first: a foreign state dir raises ``StateDirError``
    before `change` is called. A missing file starts empty and is always
    materialized, even when `change` returns None for no change: readers
    tolerate a missing file only by creating it under the lock. A
    corrupt file raises instead of being overwritten.
    """
    target = path or ledger_path()
    want = state_dir or paths.state_dir()
    with _locked(target, timeout):
        ledger = _prepare(target, want)
        result = change(ledger)
        if result is None:
            return ledger
        return _write(target, result)
