"""`korapy merge`: the serialized, verified merge verb.

The forge-side half of landing (`push` is the worktree-side half). The
verb merges one pull request after proving it is mergeable, one merger
at a time per repository, and reports every precondition as data.

Checks, in gate order: `lock` (the per-repo lock is free), `open`
(the forge reports the PR open), `head` (the forge head equals the
pinned `--head`), `review` (the same `mergeup.find_record` reader
takeoff uses, so both gates agree, including inheritance across a
clean merge-up), `checks` (the forge check statuses are green; the
only waivable row: `pending` while they are still running, with
the running count, a wait message and no fix advice), `fork` (the
PR is not from a fork;
a maintainer merges those by hand).

`--wait` polls the forge (never the lock) until the `checks` row
leaves `pending`, any row fails, or `--timeout` (default ten
minutes) lands; a timeout still reports `pending` with exit 1.

The verb acquires the per-repo lock, re-reads the PR live, merges,
verifies, and releases. `--plan` reports every row and the lock state
without taking the lock and without merging. A refusal at merge time
(the head moved under the lock) re-reads the PR, so the document
still names the failing row. A post-merge verify mismatch (the merge
happened but is unverified) appends a `verify` row and fails closed;
that row appears in no other case.

The forge object is a required seam: merging changes the remote, so
there is no default that could merge when a caller passes nothing.
"""

import contextlib
import errno
import fcntl
import math
import os
import re
import time
from collections.abc import Callable
from typing import NamedTuple

from korapy import forge as iface, mergeup, paths, reviewrecord, settings

#: The version stamped on every `--json` document.
SCHEMA_VERSION = 1

#: Exit codes: a subset of the public interface (AGENTS.md).
OK, NO, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4

#: The merge methods the verb knows: a squash, or a merge commit that
#: keeps the branch commits. Anything else (including rebase) is
#: refused: rebase support waits until a project asks for it.
METHODS = ("squash", "merge")

#: The default method: a squash.
DEFAULT_METHOD = "squash"

#: The setting naming the project's merge method. One fact, one place:
#: `resolve_method` below is the only reader of the resolved value.
METHOD_SETTING = "merge.method"


def _check_method(value):
    """The `merge.method` range check: squash or merge, else a rule."""
    if value not in METHODS:
        raise ValueError(f"must be one of {list(METHODS)}")
    return value


settings.register(
    METHOD_SETTING,
    str,
    DEFAULT_METHOD,
    "how korapy merge lands a PR: a squash, or a merge commit",
    check=_check_method,
)


def resolve_method(*, flag=None, resolved=None):
    """The effective merge method: the one place that decides it.

    The `--method` flag wins when given, else the resolved
    `merge.method` setting, else the default squash. Anything outside
    `METHODS` is a MergeError naming the setting (exit 4).
    """
    if flag is not None:
        method = flag
    elif resolved is not None:
        method = resolved[METHOD_SETTING]
    else:
        method = DEFAULT_METHOD
    if method not in METHODS:
        raise MergeError(f"{METHOD_SETTING} {method!r} must be one of {list(METHODS)}")
    return method


#: The precondition rows, in gate order.
CHECK_NAMES = ("lock", "open", "head", "review", "checks", "fork")

#: The only check `--skip` may waive.
SKIPPABLE = ("checks",)

#: The identity checks no flag may waive: they establish what is merged.
IDENTITY = ("lock", "open", "head", "review", "fork")

#: Row statuses: a precondition holds, is not yet met, refuses, or
#: was waived by name. `pending` is a wait, not a failure: the detail
#: names the wait and carries no fix advice.
OK_S, PENDING, FAIL, SKIPPED = "ok", "pending", "fail", "skipped"

#: Seconds `--wait` polls a pending forge before giving up: ten minutes.
DEFAULT_WAIT_TIMEOUT_S = 600

#: The `--wait` poll interval: a gentle forge read cadence, not a spin.
WAIT_POLL_S = 30.0

#: A pinned head: a full 40- or 64-digit hex SHA, nothing shorter.
#:
#: The one definition; `review show` reuses it, so a loosened copy can
#: never make `merge` accept a head the forge pin then refuses.
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class MergeError(Exception):
    """Usage or configuration keeps the merge from running (exit 4)."""


class MergeBusy(Exception):
    """The per-repo lock is held by another merger (exit 2, try later)."""


class Check(NamedTuple):
    """One precondition row: its name, status, and human detail."""

    name: str
    status: str
    detail: str


class Result(NamedTuple):
    """The merge outcome: every row, the waived rows, and what happened."""

    checks: list[Check]
    skipped: list[str]
    merged: bool
    merge_sha: str | None
    exit_code: int


#: The lock dir under the state dir; one file per forge repository.
MERGES_DIRNAME = "merges"


def lock_path(repo: str) -> str:
    """The per-repo merge lock file for `owner/name`.

    The only path join for it (AGENTS.md, "One fact, one place"). The
    slash becomes `+`, so no repo name can leave the lock dir.
    """
    return os.path.join(paths.state_dir(), MERGES_DIRNAME, repo.replace("/", "+") + ".lock")


#: The lock seam: production locks with fcntl; tests assign fakes.
_flock: Callable[[int, int], None] = fcntl.flock

#: The wait-loop seams: the monotonic clock and the sleep. Tests assign
#: fakes (or pass `now_fn`/`sleep_fn`); production waits on the wall.
_sleep = time.sleep
_monotonic = time.monotonic


def _resolve_skips(skips) -> frozenset[str]:
    """`skips` as a set, or raise MergeError naming the refused name.

    Unknown names and the identity checks (`lock`, `open`, `head`,
    `review`, `fork`) are refused: skipping identity is not a policy
    choice (mirroring takeoff).
    """
    names = list(skips or [])
    unknown = [name for name in names if name not in SKIPPABLE + IDENTITY]
    if unknown:
        raise MergeError(
            f"unknown check {unknown[0]!r}: only {', '.join(SKIPPABLE)} can be skipped"
        )
    refused = [name for name in names if name in IDENTITY]
    if refused:
        raise MergeError(
            f"cannot skip the {refused[0]} check: "
            "it establishes what is being merged, not a policy choice"
        )
    return frozenset(names)


def _open_lock(path: str) -> int:
    """The lock file descriptor, opened for locking."""
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise MergeError(f"cannot set up the merge lock: {err}") from None
    try:
        return os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as err:
        raise MergeError(f"cannot open the merge lock {path}: {err}") from None


def _acquire(fd: int, path: str) -> None:
    """Take the non-blocking exclusive lock, or raise MergeBusy/MergeError."""
    try:
        _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise MergeBusy(
                f"another merger holds the merge lock for this repo ({path}): try again later"
            ) from None
        raise MergeError(f"cannot lock {path}: {err}") from None


@contextlib.contextmanager
def _locked(path: str):
    """Hold the per-repo merge lock; contention raises MergeBusy.

    The kernel releases the lock when the holder dies, so a dead
    merger leaves nothing to reap. One non-blocking try: a held lock
    is exit 2, never a wait.
    """
    fd = _open_lock(path)
    try:
        _acquire(fd, path)
    except Exception:
        os.close(fd)
        raise
    try:
        yield
    finally:
        os.close(fd)


#: The kernel's live lock table: the source of the lockless probe.
LOCKS_PATH = "/proc/locks"


def _read_locks(path: str) -> str:
    """The lock table text: the probe's seam, so tests inject fixtures."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _lock_key(fd: int) -> str:
    """The `/proc/locks` device key for an open file: `major:minor:inode`."""
    stat = os.fstat(fd)
    return f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"


def _lock_held(text: str, key: str) -> bool:
    """Whether the lock table shows an `FLOCK` lock on the `key` file.

    Pure: only `FLOCK` rows for this device and inode count (`POSIX`
    locks live in another lock space and never block `flock`), and
    unknown rows never match, so a stranger table reads as free.
    """
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        if fields[5] == key:
            return True
    return False


def _lock_free(path: str) -> bool:
    """Whether the merge lock is free: a probe that never holds it.

    Nothing is created (a missing lock file reads as free) and no lock
    is taken: the open is read-only and the state comes from the
    kernel's lock table, so a real merger's non-blocking attempt never
    lands inside a probe window.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return True
    except OSError as err:
        raise MergeError(f"cannot open the merge lock {path}: {err}") from None
    try:
        text = _read_locks(LOCKS_PATH)
        held = _lock_held(text, _lock_key(fd))
    except OSError as err:
        raise MergeError(f"cannot probe the merge lock {path}: {err}") from None
    finally:
        os.close(fd)
    return not held


def _check_review(ticket_repo: str, ticket_number: int, head: str, base: str, cwd: str) -> Check:
    """The head carries an approving review record (takeoff's lookup)."""
    record, found = mergeup.find_record(
        lambda commit: reviewrecord.read_record(ticket_repo, ticket_number, commit),
        head,
        base,
        cwd,
    )
    short = reviewrecord.commit_id(head)
    if record is None:
        return Check(
            "review",
            FAIL,
            f"no approving review record for {ticket_repo} {ticket_number} commit {short}",
        )
    if not reviewrecord.approves(record):
        return Check(
            "review",
            FAIL,
            f"the review record for commit {short} is a block: answer its findings "
            "with a new commit and a fresh review",
        )
    reviewer = record.get("reviewer") or "a reviewer"
    if found and found != head:
        return Check(
            "review",
            OK_S,
            f"approved by {reviewer} (inherited from {reviewrecord.commit_id(found)} "
            "across a clean merge-up)",
        )
    return Check("review", OK_S, f"approved by {reviewer}")


def _evaluate(
    ticket_repo: str,
    ticket_number: int,
    pr: iface.PullRequest,
    head: str,
    skip_set: frozenset[str],
    cwd: str,
    lock_detail: str,
    pending: int | None = None,
) -> list[Check]:
    """Every precondition row for a fresh PR read, in gate order.

    `pending` decorates a pending checks row only: the running count
    from the separate `pending_checks` read, or `None` when that read
    failed or never ran. It never changes the verdict.
    """
    checks = [Check("lock", OK_S, lock_detail)]
    if pr.state == iface.OPEN:
        checks.append(Check("open", OK_S, "the PR is open"))
    else:
        checks.append(Check("open", FAIL, f"the PR is {pr.state}: nothing to do"))
    if pr.head_sha == head:
        checks.append(Check("head", OK_S, f"the head is {reviewrecord.commit_id(head)}"))
    else:
        checks.append(
            Check(
                "head",
                FAIL,
                f"the PR head is {reviewrecord.commit_id(pr.head_sha)}: the approved "
                f"commit {reviewrecord.commit_id(head)} is no longer the tip",
            )
        )
    checks.append(_check_review(ticket_repo, ticket_number, head, pr.base_sha, cwd))
    if "checks" in skip_set:
        checks.append(Check("checks", SKIPPED, "skipped by --skip checks"))
    elif pr.checks_state == "passing":
        checks.append(Check("checks", OK_S, "the checks are passing"))
    elif pr.checks_state == "pending":
        if pending:
            detail = f"the checks are pending ({pending} still running): wait for them to finish"
        else:
            detail = "the checks are pending: waiting for the forge to report check results"
        checks.append(Check("checks", PENDING, detail))
    else:
        checks.append(
            Check(
                "checks",
                FAIL,
                f"the checks are {pr.checks_state}: fix the branch and update the PR",
            )
        )
    if pr.from_fork:
        checks.append(
            Check(
                "fork",
                FAIL,
                "the PR is from a fork: a maintainer merges those by hand",
            )
        )
    else:
        checks.append(Check("fork", OK_S, "the PR is not from a fork"))
    return checks


def _failed(checks: list[Check]) -> bool:
    return any(check.status == FAIL for check in checks)


def _blocked(checks: list[Check]) -> bool:
    """Whether the evaluation stops the merge: any refusal or any wait.

    A `pending` row blocks like a `fail` (exit 1, nothing merged) but
    stays `pending` in the document, so a caller branching on the row
    waits instead of fixing the branch.
    """
    return any(check.status in (FAIL, PENDING) for check in checks)


def _status(checks: list[Check], name: str) -> str:
    """The status of one precondition row; `_evaluate` always emits it."""
    for check in checks:
        if check.name == name:
            return check.status
    raise AssertionError(f"no check named {name!r}")


def _resolve_timeout(timeout) -> float:
    """Seconds `--wait` polls a pending forge: the default or a validated value.

    `None` is the default ten minutes. Anything else must be a finite
    number of seconds, zero or more; anything unusable is a MergeError
    naming `--timeout` (exit 4 through the CLI).
    """
    if timeout is None:
        return float(DEFAULT_WAIT_TIMEOUT_S)
    if isinstance(timeout, bool):
        raise MergeError("--timeout needs seconds")
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        raise MergeError("--timeout needs seconds") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise MergeError("--timeout needs seconds")
    return seconds


def _pending_count(forge, repo: str, sha: str) -> int | None:
    """The running check count for a pending head, or `None` when unknown.

    The separate `pending_checks` read only decorates the pending row:
    a failed or unusable count degrades to the count-less pending
    message instead of an error, so a slow second read can never flip
    the verdict or fail the verb.
    """
    try:
        count = forge.pending_checks(repo, sha)
    except iface.ForgeError:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def _read_and_evaluate(
    forge,
    ticket_repo: str,
    ticket_number: int,
    repo: str,
    number: int,
    head: str,
    skip_set: frozenset[str],
    cwd: str,
    lock_detail: str,
) -> list[Check]:
    """One PR read plus its rows, fetching the pending count only then.

    The count read runs only when the verdict is pending; passing,
    failing, and skipped rows never trigger it, and other verbs that
    read the PR never pay for it.
    """
    pr = forge.pull_request(repo, number)
    pending = _pending_count(forge, repo, pr.head_sha) if pr.checks_state == "pending" else None
    return _evaluate(ticket_repo, ticket_number, pr, head, skip_set, cwd, lock_detail, pending)


def _poll_until_settled(
    ticket_repo: str,
    ticket_number: int,
    repo: str,
    number: int,
    head: str,
    skip_set: frozenset[str],
    path: str,
    held_detail: str,
    cwd: str,
    *,
    timeout: float,
    poll_s: float,
    clock,
    sleep,
    forge,
) -> list[Check]:
    """Poll the forge until the checks row leaves pending, a row fails, or time lands.

    No lock is taken and nothing merges: the caller merges (or reports
    `--plan`) from the returned evaluation. A `fail` anywhere wins at
    once, even beside a pending checks row; a timeout returns the
    pending evaluation. PR-read outages raise through the seam, never
    swallowed into a verdict; a failed pending-count read degrades to
    the count-less pending message.
    """
    start = clock()
    while True:
        if _lock_free(path):
            lock_detail = "the merge lock is free"
        else:
            lock_detail = held_detail
        checks = _read_and_evaluate(
            forge, ticket_repo, ticket_number, repo, number, head, skip_set, cwd, lock_detail
        )
        if _status(checks, "checks") != PENDING or _failed(checks):
            return checks
        if clock() - start >= timeout:
            return checks
        sleep(poll_s)


def run_merge(
    ticket_repo: str,
    ticket_number: int,
    repo: str,
    number: int,
    *,
    head_sha: str,
    method: str | None = None,
    skips=(),
    plan: bool = False,
    wait: bool = False,
    timeout=None,
    poll_s: float = WAIT_POLL_S,
    now_fn=None,
    sleep_fn=None,
    forge,
    cwd: str | None = None,
) -> Result:
    """Merge one PR after proving it is mergeable; returns the verdict.

    `forge` reads the PR and performs the merge: it is required, so no
    default may silently answer for the remote or merge. `head_sha` is
    the exact head the caller approved; `method` is a squash or a merge
    commit, absent for the default squash (anything else is a MergeError
    naming `merge.method`).
    `skips` names waived checks (`checks` alone may be waived). With
    `plan` every row is reported without taking the lock and without
    merging. With `wait` a pending `checks` row polls the forge until it
    leaves pending, any row fails, or `timeout` seconds land (`None` is
    the default ten minutes; a `timeout` without `wait` is a MergeError
    naming `--timeout`). `poll_s` is the poll cadence; `now_fn` and
    `sleep_fn` inject the clock and the sleep for tests. The wait never
    holds the merge lock: a real merge re-reads under the lock after
    the wait settles. Usage problems raise MergeError (exit 4); forge
    outages raise `korapy.forge.ForgeError` (exit 3).

    The post-merge verify ties the reported merge SHA to the pinned
    head: state `merged` with the merge SHA the forge reports. For a
    merge commit the forge seam's merge call additionally requires the
    merge commit's parents to include the pinned head. A merge-time
    refusal with every row still green is the method the forge or the
    repository settings do not allow: a `method` row fails naming
    `merge.method`, and the row appears in no other case.
    """
    if forge is None:
        raise TypeError("run_merge needs a forge seam")
    method = resolve_method(flag=method)
    skip_set = _resolve_skips(skips)
    if not paths.plain_repo_name(ticket_repo):
        raise MergeError(f"ticket repo {ticket_repo!r} is not a plain repository name")
    if isinstance(ticket_number, bool) or not isinstance(ticket_number, int) or ticket_number < 1:
        raise MergeError(f"ticket number {ticket_number!r} must be a positive integer")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise MergeError(f"PR number {number!r} must be a positive integer")
    head = str(head_sha or "").strip()
    if not FULL_SHA.fullmatch(head):
        raise MergeError("--head must be the full head SHA the caller approved")
    head = head.lower()
    cwd = cwd or os.getcwd()
    path = lock_path(repo)
    if timeout is not None and not wait:
        raise MergeError("--timeout needs --wait: it bounds how long --wait polls")
    timeout_s = _resolve_timeout(timeout) if wait else 0.0
    if plan:
        if wait:
            checks = _poll_until_settled(
                ticket_repo,
                ticket_number,
                repo,
                number,
                head,
                skip_set,
                path,
                "the merge lock is held by another merger "
                "(informational: --plan never takes the lock)",
                cwd,
                timeout=timeout_s,
                poll_s=poll_s,
                clock=now_fn or _monotonic,
                sleep=sleep_fn or _sleep,
                forge=forge,
            )
        else:
            free = _lock_free(path)
            if free:
                lock_detail = "the merge lock is free"
            else:
                lock_detail = "the merge lock is held by another merger (informational: --plan never takes the lock)"
            checks = _read_and_evaluate(
                forge, ticket_repo, ticket_number, repo, number, head, skip_set, cwd, lock_detail
            )
        return Result(checks, sorted(skip_set), False, None, OK if not _blocked(checks) else NO)
    if wait:
        checks = _poll_until_settled(
            ticket_repo,
            ticket_number,
            repo,
            number,
            head,
            skip_set,
            path,
            "the merge lock is held by another merger",
            cwd,
            timeout=timeout_s,
            poll_s=poll_s,
            clock=now_fn or _monotonic,
            sleep=sleep_fn or _sleep,
            forge=forge,
        )
        if _blocked(checks):
            return Result(checks, sorted(skip_set), False, None, NO)
    try:
        with _locked(path):
            checks = _read_and_evaluate(
                forge,
                ticket_repo,
                ticket_number,
                repo,
                number,
                head,
                skip_set,
                cwd,
                "the merge lock is free",
            )
            if _blocked(checks):
                return Result(checks, sorted(skip_set), False, None, NO)
            try:
                reported = forge.merge(repo, number, head_sha=head, method=method)
            except iface.ForgeRefused as refused:
                # The head moved under the lock: re-read, so the
                # document names the failing row. A re-read with every
                # row still green is the method the forge or the
                # repository settings do not allow: the `method` row
                # names the setting, and appears in no other case.
                checks = _read_and_evaluate(
                    forge,
                    ticket_repo,
                    ticket_number,
                    repo,
                    number,
                    head,
                    skip_set,
                    cwd,
                    "the merge lock is free",
                )
                if not _blocked(checks):
                    checks.append(
                        Check(
                            "method",
                            FAIL,
                            f"the forge refused {METHOD_SETTING} {method!r}: {refused}; "
                            f"allow the method in the repository settings "
                            f"or choose another {METHOD_SETTING}",
                        )
                    )
                return Result(checks, sorted(skip_set), False, None, NO)
            verified = forge.pull_request(repo, number)
            if verified.state != iface.MERGED or verified.merge_sha != reported:
                detail = (
                    "the merge is unverified: the forge reports "
                    f"{verified.state} with merge commit {verified.merge_sha or 'none'}, "
                    f"want merged with {reported}"
                )
                checks.append(Check("verify", FAIL, detail))
                return Result(checks, sorted(skip_set), False, None, NO)
            return Result(checks, sorted(skip_set), True, reported, OK)
    except MergeBusy as busy:
        return Result([Check("lock", FAIL, str(busy))], sorted(skip_set), False, None, RACE)
