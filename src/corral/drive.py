"""`corral drive`: one dispatch pass over the board store.

Ticket 1 is the skeleton: read the store, print the candidate table,
and write nothing. The non-plan pass holds the single-instance lock
(exit 2 when another pass holds it). `--plan` is the dry run: it never
takes the lock, never syncs, claims, enqueues, starts, mirrors or
journals. There is no eligibility hook yet, so every open item is
listed with verdict "no policy". Later tickets add the policy call,
the gate order, claims and the real pass behind the same lock.

Exit codes: 0 completed, 2 another pass holds the lock, 4 the store
could not be read or the lock could not be set up.
"""

import argparse
import contextlib
import fcntl
import os
import sqlite3
import sys
from dataclasses import dataclass

from corral import board, paths

OK, RACE, USAGE = 0, 2, 4

#: The lock file inside the state dir.
LOCK_NAME = "drive.lock"

#: Verdict for every candidate while no eligibility policy is registered.
NO_POLICY = "no policy"


class LockHeld(Exception):
    """Another pass holds the drive lock (exit 2)."""

    exit_code = 2


class LockError(Exception):
    """The lock could not be set up: bad directory or file (exit 4)."""

    exit_code = 4


@dataclass(frozen=True)
class Candidate:
    """One row of the candidate table: what the pass would consider."""

    repo: str
    number: int
    verdict: str
    reason: str


def lock_path():
    """The single-instance lock file: inside the state dir.

    The only path join for it (AGENTS.md, "One fact, one place").
    """
    return os.path.join(paths.state_dir(), LOCK_NAME)


@contextlib.contextmanager
def drive_lock(path):
    """Hold a non-blocking exclusive flock on `path` for the pass.

    Creates the lock file when missing. Raises `LockHeld` only when
    another pass holds the lock; setup and open failures raise
    `LockError`. The lock releases when the context exits.
    """
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise LockError(f"cannot set up the drive lock: {err}")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as err:
        raise LockError(f"cannot open the drive lock {path}: {err}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise LockHeld(f"another drive pass is already running (lock {path})")
    try:
        yield
    finally:
        os.close(fd)


def candidates(items):
    """Every open item as a "no policy" candidate, in (repo, number) order.

    Pure: no store, no clock, no settings. Later tickets replace the
    verdict with the eligibility hook and the gate order.
    """
    rows = [
        Candidate(
            repo=item.repo,
            number=item.number,
            verdict=NO_POLICY,
            reason="no eligibility policy registered",
        )
        for item in items
        if item.state == "open"
    ]
    rows.sort(key=lambda row: (row.repo, row.number))
    return rows


def render(rows):
    """The candidate table plus the summary line."""
    lines = [f"{row.repo}#{row.number}: {row.verdict} ({row.reason})" for row in rows]
    lines.append(f"plan: {len(rows)} candidate(s), would start 0")
    return "\n".join(lines) + "\n"


def _read_and_print(store, out, err) -> int:
    """Read the store and print the candidate table, writing nothing."""
    try:
        items = store.items(state="open")
    except board.StoreError as problem:
        print(f"corral drive: {problem}", file=err)
        return USAGE
    except sqlite3.Error as problem:
        print(f"corral drive: cannot read the store: {problem}", file=err)
        return USAGE
    out.write(render(candidates(items)))
    return OK


def run_pass(*, plan, store, lock_path, out, err) -> int:
    """One pass: read the store, print the table, write nothing.

    Only the non-plan pass takes the drive lock; `--plan` never does.
    All seams injected (`store`, the lock path, the streams), so tests
    use a temporary store and lock. Returns the exit code.
    """
    if plan:
        return _read_and_print(store, out, err)
    try:
        with drive_lock(lock_path):
            return _read_and_print(store, out, err)
    except LockHeld as held:
        print(f"corral drive: {held}", file=err)
        return RACE
    except LockError as problem:
        print(f"corral drive: {problem}", file=err)
        return USAGE


def cmd_drive(args: argparse.Namespace) -> int:
    try:
        store = board.open_store()
    except board.StoreError as problem:
        print(f"corral drive: {problem}", file=sys.stderr)
        return USAGE
    with store:
        return run_pass(
            plan=args.plan,
            store=store,
            lock_path=lock_path(),
            out=sys.stdout,
            err=sys.stderr,
        )


def add_parser(sub) -> None:
    """Register `drive` on the top-level subparsers."""
    p = sub.add_parser("drive", help="run one dispatch pass over the board store")
    p.add_argument(
        "--plan",
        action="store_true",
        help="print the candidate table and change nothing",
    )
    p.set_defaults(func=cmd_drive)
