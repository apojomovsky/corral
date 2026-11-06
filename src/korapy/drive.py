"""`korapy drive`: one dispatch pass over the board store.

Each pass syncs the board store from the tracker, releases
provably-dead claims, joins every registered project's eligibility
requests to the store rows, runs the section 3 gate order over the
candidates, claims the proceeding ones through the store transaction,
hands the starts to the existing launch path, drains the mirror
backlog, and prints the verdict table. In hub mode (`hub.url` set)
the pass reconciles with the hub first and each fresh claim is
proposed to the hub before the store adopts the receipt's epoch;
with no hub configured the pass makes zero hub calls.
`--plan` is the dry run: it holds no lock and writes nothing, running
no sync, hub contact, claim, mirror or launch. The non-plan pass holds the
single-instance lock (exit 2 when another pass holds it).

Exit codes: 0 completed, 2 another pass holds the lock, 3 the tracker,
the hub, or live host state (units or the registry) could not be
reached, 4 the store could not be read, the lock could not be set up,
or a project failed to load. A full host that starts nothing routine
is still 0: saturation is routine dispatch state, not an error.
"""

import argparse
import contextlib
import datetime
import errno
import fcntl
import hashlib
import io
import json
import os
import socket
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from korapy import (
    accounts,
    admission,
    board,
    disk,
    eligibility,
    hold,
    host_ledger,
    launch,
    mirror,
    paths,
    profiles,
    projects,
    prompts,
    quota,
    quota_ledger,
    registry,
    reviewrecord,
    settings,
    sync,
    trackers,
    units,
    worktrees,
)
from korapy import syncli
from korapy import tracker as iface
from korapy.eligibility import LinkedPull, StartRequest, TicketRow

OK, NO, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4

settings.register(
    "drive.max_starts_per_day",
    int,
    50,
    "real starts one dispatch pass may launch per calendar day (plans never count)",
)

settings.register(
    "drive.agent_host",
    str,
    "",
    "display name for the host segment of minted agent ids (empty hashes the hostname)",
)

settings.register(
    "drive.fix_round_threshold",
    str,
    reviewrecord.DEFAULT_FIX_THRESHOLD,
    "lowest review finding severity that still opens a fix round "
    "(high, normal or low): an approving review below it is merge-ready",
    check=reviewrecord.check_threshold,
)

#: The lock file inside the state dir.
LOCK_NAME = "drive.lock"

#: The agent id prefix the loop mints. Claim routing trusts only this
#: prefix to tell a managed holder (an id this loop minted) from a
#: foreign one (an operator or another host, adopted through resync).
#: The only parser for it; the minter lands with the claims pass,
#: reusing this prefix.
MANAGED_PREFIX = "drive-"

#: Required kinds first: resume and fix rounds for already-claimed
#: tickets before new starts, so in-flight work is not starved by new
#: tickets. Review waits its turn with the starts.
KIND_GROUP = {"resume": 0, "fix": 0, "start": 1, "review": 1}


def is_managed_agent(agent) -> bool:
    """Whether `agent` is an id this loop minted. Pure."""
    return isinstance(agent, str) and agent.startswith(MANAGED_PREFIX)


def agent_host_segment(configured="", *, hostname="") -> str:
    """The host segment for minted agent ids. Pure.

    A configured display name is used with `/` replaced by `-`; an
    empty or blank one falls back to a short stable hash of the
    hostname, never the hostname itself: claim agent values are
    mirrored to the tracker, so a raw hostname there would publish the
    deployment. Either way the segment is opaque and slash-free.
    """
    text = str(configured).strip() if configured else ""
    if text:
        return text.replace("/", "-")
    return hashlib.sha256(str(hostname or "").encode("utf-8")).hexdigest()[:8]


def mint_agent(repo, number, *, host, now) -> str:
    """One fresh agent id for (`repo`, `number`). Pure.

    Opaque to every reader except debugging: only `is_managed_agent`
    parses it, and only for the prefix. The id is `drive-<hash8>-<n>`
    where the hash covers the host segment, the repo and the epoch
    seconds and `n` is the ticket number, so it carries no path or
    name and stays short enough for a tracker label: `agent:<id>` fits
    32 characters for ticket numbers up to 11 digits. `Claim.epoch`
    stays the fencing token, never this id. `host` is the display
    segment from
    `agent_host_segment`, never the raw hostname: the claim is mirrored
    to the tracker, so a raw hostname here would publish the
    deployment.
    """
    digest = hashlib.sha256(f"{host}\0{repo}\0{int(now)}".encode("utf-8")).hexdigest()[:8]
    return f"{MANAGED_PREFIX}{digest}-{int(number)}"


def priority_rank(priority) -> int:
    """The board priority's rank, higher first. Pure.

    Follows the tracker's highest-first priority order; anything outside
    it (including no priority) ranks zero and sorts last.
    """
    if priority in iface.PRIORITIES:
        return len(iface.PRIORITIES) - iface.PRIORITIES.index(priority)
    return 0


def route_kind(kind, holder) -> str:
    """The effective kind for `kind` against the row's holder. Pure.

    A resume or fix naming an unclaimed ticket means its holder went
    away between eligibility and gating; the request is reclassified as
    a fresh start on the same ticket. Everything else passes through.
    """
    if not holder and kind in ("resume", "fix"):
        return "start"
    return kind


@dataclass(frozen=True)
class GateInput:
    """One candidate entering the gate order: who wants it, and the row.

    `request` is None for an open claimed row no project requested this
    pass: withdrawn by this pass's eligibility, which the first gate
    skips. `row` is None when the request names a ticket with no store
    row, which the first gate also skips. `wait` is the board row's
    `tracker_updated_at`, annotating the candidate with how long the
    ticket has waited.
    """

    project: str
    request: StartRequest | None
    row: TicketRow | None
    wait: float = 0.0


def order(entries: Sequence[GateInput]) -> list[GateInput]:
    """The candidates in section 3 order. Pure.

    Required kinds first, then board priority higher first, then longest
    wait oldest first, then the project, repo and number for
    determinism. Withdrawn rows (no request) sort after every requested
    kind; missing rows sort with unknown priority and zero wait.
    """
    return sorted(
        entries,
        key=lambda item: (
            KIND_GROUP.get(item.request.kind, 2) if item.request is not None else 2,
            -priority_rank(item.row.priority if item.row is not None else ""),
            item.wait,
            item.project,
            item.request.repo
            if item.request is not None
            else (item.row.repo if item.row is not None else ""),
            item.request.number
            if item.request is not None
            else (item.row.number if item.row is not None else 0),
        ),
    )


@dataclass(frozen=True)
class QuotaSource:
    """One project's quota input for the gate: its model chains plus the
    thresholds the gate judges the shared ledger by. Plain data; the pass
    resolves each project's entry from its own settings and the account
    pool, through the shared freshness helper.
    """

    chains: dict = field(default_factory=dict)
    stale_after: float = accounts.DEFAULT_STALE_AFTER_S
    low_pct: float = quota.DEFAULT_LOW_PCT
    unknown_blocks: bool = quota.DEFAULT_UNKNOWN_BLOCKS


#: The quota input for rows without a project (withdrawn rows and safety
#: fallbacks): no chains, so the quota gate always passes them.
EMPTY_QUOTA = QuotaSource()


@dataclass(frozen=True)
class Gated:
    """One candidate after the gate order: proceed or skip, with the words.

    `kind` is the effective kind after reclassification; `verdict` is that
    kind when the candidate proceeds and `"skip"` otherwise; `code` names
    the failing gate (`"row"`, `"claim"`, `"hold"`, an admission code, or
    `"quota`) and is `""` when the candidate proceeds.
    """

    project: str
    repo: str
    number: int
    kind: str
    verdict: str
    reason: str
    code: str


def _skip(candidate, kind, code, reason) -> Gated:
    """The skip verdict for a candidate that names its repo and number."""
    if candidate.request is not None:
        repo, number = candidate.request.repo, candidate.request.number
    elif candidate.row is not None:
        repo, number = candidate.row.repo, candidate.row.number
    else:
        repo, number = "?", 0
    return Gated(
        project=candidate.project,
        repo=repo,
        number=number,
        kind=kind,
        verdict="skip",
        reason=reason,
        code=code,
    )


def gate(
    candidate,
    *,
    read_labels,
    hold_label,
    live,
    limits,
    quota_source,
    ledger,
    now,
    finished=None,
    agent_roles=None,
    agent_states=None,
    agent_tickets=None,
    reviews=None,
    fix_threshold=None,
) -> Gated:
    """The section 3 gate order for one candidate, cheapest first. Pure.

    Row state first (missing, non-open, external, withdrawn), then claim
    routing by request kind against the row's holder, then the reviewer
    and fix-round readiness from `finished` ({(repo, number): (agent id,
    role, outcome)} for the newest finished entry per ticket), then the
    review threshold and the linked-pull check, then the hold and
    decision labels, then admission, then the quota gate. A review
    proceeds only for PR-ready rows: unclaimed with a finished worker
    behind them, whose id the reviewer passes as its author, or claimed
    by that finished worker itself, which the reviewer takes over. Rows
    reviewed since the last work are merge-ready and ignored, and a fix
    round requested after the fix already landed is skipped. An
    unclaimed row whose latest review record (`reviews`, keyed by
    (repo, number)) approves with no finding at or above
    `fix_threshold` is merge-ready too: no fix round opens for lows,
    and no fresh start launches on approved work. None (the default)
    reads the shared default threshold, so the gate follows it past
    import time; a falsy value falls back the same way. A finished reviewer
    with outcome `ok` and no review record for the ticket is a protocol
    failure, never a finding: the row skips with code `review-missing`
    for every kind, so no worker, fix round or re-review starts on it.
    With no review map (`reviews` None) the check cannot run and the
    row gates as before. A fresh start on an
    unclaimed row with an open linked pull (`row.pulls`, read once per
    pass through the tracker when the rows were built) is refused: the
    pull, not a new worker, carries the ticket. The row pulls are the
    one linked-pull source the policies read too, so the two never
    disagree and the check costs no second lookup. A fix or
    resume behind a reviewer holder (from `agent_roles`, resolved to
    its registry entry by ticket through the one resolver, since a
    drive-id holder never names a unit-name entry) skips with the
    claim kept, matching the claiming pass, so `--plan` prints the
    verdict the real pass would print. Every input is plain data or an
    injected seam (`read_labels`); nothing is launched,
    claimed or written. With no pulls on the row the linked-pull check
    is skipped, which is what `--plan` builds: it never contacts the
    tracker. Unreadable pulls (None) refuse the start instead, failing
    closed like the hold labels.
    """
    if candidate.request is None:
        row = candidate.row
        ref = f"{row.repo}#{row.number}" if row is not None else "unknown"
        return _skip(
            candidate, "start", "row", f"{ref}: withdrawn by this pass's eligibility, skipped"
        )
    request = candidate.request
    ref = f"{request.repo}#{request.number}"
    row = candidate.row
    if row is None:
        return _skip(candidate, request.kind, "row", f"{ref}: no store row, skipped as withdrawn")
    if row.state != "open":
        return _skip(
            candidate, request.kind, "row", f"{ref}: state is {row.state}, not open, skipped"
        )
    if row.external:
        return _skip(candidate, request.kind, "row", f"{ref}: externally owned, skipped")

    holder = row.agent or ""
    kind = route_kind(request.kind, holder)
    if holder:
        newest = (finished or {}).get((row.repo, row.number))
        takes_over = (
            request.kind in ("start", "review")
            and is_managed_agent(holder)
            and newest is not None
            and _finished_agent(newest) == holder
            and _finished_role(newest) != "reviewer"
        )
        if takes_over:
            kind = "review"
        elif request.kind == "start":
            return _skip(
                candidate,
                kind,
                "claim",
                f"{ref}: already claimed by `{holder}`, start refused",
            )
        elif request.kind == "review":
            return _skip(
                candidate,
                kind,
                "claim",
                f"{ref}: worker `{holder}` still holds the claim, review waits for release",
            )
        elif not is_managed_agent(holder):
            return _skip(
                candidate,
                kind,
                "claim",
                f"{ref}: foreign claim by `{holder}`, never touched",
            )

    newest = (finished or {}).get((row.repo, row.number))
    if _review_missing(holder, newest, reviews, (row.repo, row.number)):
        return _skip(
            candidate,
            kind,
            "review-missing",
            f"{ref}: reviewer finished without a review record, review missing, skipped",
        )

    if not holder and request.kind in ("review", "fix", "resume"):
        newest = (finished or {}).get((row.repo, row.number))
        if request.kind == "review":
            if newest is None:
                return _skip(
                    candidate,
                    kind,
                    "row",
                    f"{ref}: no finished worker, nothing to review, skipped",
                )
            if _finished_role(newest) == "reviewer":
                return _skip(
                    candidate,
                    kind,
                    "row",
                    f"{ref}: already reviewed since the last work, merge-ready, skipped",
                )
        elif newest is not None and _finished_role(newest) == "worker":
            return _skip(
                candidate,
                kind,
                "row",
                f"{ref}: fix already landed, skipped",
            )

    if not holder and request.kind in ("start", "review", "fix", "resume"):
        newest = (finished or {}).get((row.repo, row.number))
        if newest is None or _finished_role(newest) == "reviewer":
            record = (reviews or {}).get((row.repo, row.number))
            if (
                record is not None
                and reviewrecord.approves(record)
                and not reviewrecord.fix_wanted(
                    record, fix_threshold or reviewrecord.DEFAULT_FIX_THRESHOLD
                )
            ):
                return _skip(
                    candidate,
                    kind,
                    "row",
                    f"{ref}: latest review {record.get('verdict')} wants no fix round, "
                    "merge-ready, skipped",
                )

    if not holder and kind == "start":
        if row.pulls is None:
            return _skip(candidate, kind, "row", f"{ref}: linked pulls unreadable, start refused")
        opened = [pull for pull in row.pulls or () if getattr(pull, "state", "") == "open"]
        if opened:
            return _skip(
                candidate,
                kind,
                "row",
                f"{ref}: pull #{opened[0].number} is open, not a fresh start, skipped",
            )

    held = hold.reason(
        read_labels,
        (row.repo, row.number),
        [(row.repo, pull.number) for pull in row.pulls or ()],
        hold_label=hold_label,
    )
    if held is not None:
        return _skip(candidate, kind, "hold", held)
    if iface.NEEDS_DECISION in (row.labels or ()):
        return _skip(
            candidate,
            kind,
            "hold",
            f"{ref}: carries {iface.NEEDS_DECISION}, waiting on a human, never started",
        )

    decided = admission.admit(
        admission.Request(project=candidate.project, repo=row.repo, ticket=row.number, kind=kind),
        admission.State(live=sorted(live)),
        limits,
    )
    if not decided.allowed:
        return _skip(candidate, kind, decided.code, decided.reason)

    shut = quota.gate(
        roles=sorted(quota_source.chains),
        chains=quota_source.chains,
        ledger=ledger,
        now=now,
        stale_after=quota_source.stale_after,
        low_pct=quota_source.low_pct,
        unknown_blocks=quota_source.unknown_blocks,
    )
    if shut["shut"]:
        blocked = shut["blocked"]
        roles = ", ".join(f"role {name!r} ({blocked[name]['state']})" for name in sorted(blocked))
        return _skip(candidate, kind, "quota", f"{ref}: quota refuses {roles}: skipped")

    if (
        kind in ("resume", "fix")
        and holder_role(
            holder,
            row.repo,
            row.number,
            agent_states=agent_states,
            agent_tickets=agent_tickets,
            agent_roles=agent_roles,
        )
        == "reviewer"
    ):
        return _skip(
            candidate,
            kind,
            "claim",
            f"{ref}: reviewer `{holder}` still holds the claim, fix waits for release",
        )

    if kind == "start" and request.kind in ("resume", "fix"):
        reason = f"{ref}: {request.kind} on an unclaimed ticket, fresh start instead"
    elif kind == "start":
        reason = f"{ref}: unclaimed, fresh start"
    elif kind == "review":
        if holder:
            reason = f"{ref}: worker `{holder}` finished, reviewer takes over"
        else:
            reason = f"{ref}: unclaimed, review slot"
    elif kind == "resume":
        reason = f"{ref}: resume on managed holder `{holder}`"
    else:
        reason = f"{ref}: fix round on managed holder `{holder}`"
    if decided.total >= decided.effective:
        reason += f"; total full but {candidate.project} below its floor"
    return Gated(
        project=candidate.project,
        repo=row.repo,
        number=row.number,
        kind=kind,
        verdict=kind,
        reason=reason,
        code="",
    )


class LockHeld(Exception):
    """Another pass holds the drive lock (exit 2)."""

    exit_code = 2


class LockError(Exception):
    """The lock could not be set up: bad directory or file (exit 4)."""

    exit_code = 4


@dataclass(frozen=True)
class Candidate:
    """One row of the candidate table: what the pass would consider."""

    project: str
    repo: str
    number: int
    kind: str
    verdict: str
    reason: str


def lock_path():
    """The single-instance lock file: inside the state dir.

    The only path join for it (AGENTS.md, "One fact, one place").
    """
    return os.path.join(paths.state_dir(), LOCK_NAME)


#: Safety and observability state. The journal lives in its own
#: directory; the pause file and the day counter sit beside the lock in
#: the state dir, each resolved here and nowhere else.
DRIVE_DIRNAME = "drive"
JOURNAL_NAME = "journal.jsonl"
JOURNAL_ARCHIVE_PREFIX = "journal-"
JOURNAL_MAX = 500
ARCHIVE_KEEP_DAYS = 14
PAUSE_NAME = "drive.pause"
DAY_NAME = "drive.starts.json"


class JournalError(Exception):
    """Drive safety state is missing or unreadable (exit 4 at the loop)."""


def drive_dir():
    """The drive journal directory: inside the state dir."""
    return os.path.join(paths.state_dir(), DRIVE_DIRNAME)


def journal_path(base=None):
    """The live drive journal file, under `base` or the drive dir."""
    return os.path.join(base if base is not None else drive_dir(), JOURNAL_NAME)


def pause_path():
    """The pause file: inside the state dir."""
    return os.path.join(paths.state_dir(), PAUSE_NAME)


def day_path():
    """The day counter file: inside the state dir."""
    return os.path.join(paths.state_dir(), DAY_NAME)


def _day_string(now):
    """The UTC calendar date of `now`, the day counter's key."""
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date().isoformat()


def _archive_stamp(entry_at, today):
    """The archive date for one journal line: its own date, else today."""
    if isinstance(entry_at, bool):
        return today
    if isinstance(entry_at, (int, float)):
        try:
            return (
                datetime.datetime.fromtimestamp(entry_at, datetime.timezone.utc).date().isoformat()
            )
        except (OverflowError, OSError, ValueError):
            return today
    return today


def _rotate_journal(live, directory, today):
    """Rotation inside the append: past the max, oldest move to archives.

    Archives are `journal-YYYY-MM-DD.jsonl` by entry date; archives older
    than two weeks are deleted. Unparseable lines keep today's date, so
    rotation never drops what it cannot read.
    """
    pruned: list[str] = []
    if len(live) <= JOURNAL_MAX:
        return live, pruned
    rest = live[: len(live) - JOURNAL_MAX]
    kept = live[len(live) - JOURNAL_MAX :]
    by_date: dict[str, list[str]] = {}
    for line in rest:
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        stamp = _archive_stamp(entry.get("at"), today) if isinstance(entry, dict) else today
        by_date.setdefault(stamp, []).append(line)
    for stamp, rows in sorted(by_date.items()):
        path = os.path.join(directory, f"{JOURNAL_ARCHIVE_PREFIX}{stamp}.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(row + "\n")
        pruned.append(os.path.basename(path))
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in names:
        if not (name.startswith(JOURNAL_ARCHIVE_PREFIX) and name.endswith(".jsonl")):
            continue
        stamp = name[len(JOURNAL_ARCHIVE_PREFIX) : -len(".jsonl")]
        try:
            file_day = datetime.date.fromisoformat(stamp)
        except ValueError:
            continue
        if (datetime.date.fromisoformat(today) - file_day).days > ARCHIVE_KEEP_DAYS:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                continue
            pruned.append(name)
    return kept, pruned


def log_event(path, event):
    """Append one JSON object line to the drive journal, rotating as needed."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        with open(path, encoding="utf-8") as handle:
            live = [line for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        live = []
    today = _archive_stamp(event.get("at"), _day_string(time.time()))
    live.append(json.dumps(event, sort_keys=True))
    live, _ = _rotate_journal(live, directory or ".", today)
    paths.atomic_write_text(path, "".join(line + "\n" for line in live))


def pause_reason(path):
    """The pause file's first line, None when the file is absent."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    return first if first else "paused by operator"


def read_day_count(path, today):
    """Starts already launched on `today`: 0 when absent or a prior date."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as err:
        raise JournalError(f"day counter {path} is unreadable: {err}") from None
    if not isinstance(data, dict) or data.get("date") != today:
        return 0
    starts = data.get("starts")
    if isinstance(starts, bool) or not isinstance(starts, int) or starts < 0:
        raise JournalError(f"day counter {path} is corrupt")
    return starts


def write_day_count(path, today, count):
    """Record `count` starts launched on `today`, atomically."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = json.dumps({"date": today, "starts": count}, sort_keys=True) + "\n"
    paths.atomic_write_text(path, payload)


def check_lock(path):
    """Whether another pass holds the drive lock now: read-only.

    A missing lock file means no holder. Only contention reads held;
    any other lock failure raises.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return True
            raise
    finally:
        os.close(fd)
    return False


def drive_status(*, now, max_starts, journal_path, pause_path, day_path, lock_path):
    """The `snapshot` drive section from host state (pure reads).

    Raises JournalError when the journal is missing or corrupt, so the
    section reports a section error instead of crashing. A missing day
    counter reads as zero starts; a corrupt one raises too.
    """
    try:
        with open(journal_path, encoding="utf-8") as handle:
            raw = handle.read().splitlines()
    except FileNotFoundError:
        raise JournalError(f"no drive passes recorded yet ({journal_path})") from None
    entries = []
    for lineno, line in enumerate(raw, 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            raise JournalError(f"drive journal {journal_path} line {lineno} is not JSON") from None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("event"), str)
            or isinstance(data.get("at"), bool)
            or not isinstance(data.get("at"), (int, float))
        ):
            raise JournalError(
                f"drive journal {journal_path} line {lineno} is not a drive entry"
            ) from None
        if data["event"] == "pass_end":
            exit_code = data.get("exit")
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise JournalError(
                    f"drive journal {journal_path} line {lineno} pass_end has no exit"
                ) from None
        entries.append(data)
    starts = [index for index, item in enumerate(entries) if item["event"] == "pass_start"]
    if not starts:
        raise JournalError(f"drive journal {journal_path} records no pass")
    tail = entries[starts[-1] :]
    ends = [item for item in tail if item["event"] == "pass_end"]
    end = ends[-1] if ends else None
    skipped: dict[str, int] = {}
    for item in tail:
        if item["event"] == "skip":
            code = item.get("code") or "skip"
            skipped[str(code)] = skipped.get(str(code), 0) + 1
    today = _day_string(now)
    used = read_day_count(day_path, today)
    reason = pause_reason(pause_path)
    return {
        "last_pass_at": tail[0]["at"],
        "last_exit": end["exit"] if end is not None else None,
        "lock_held": check_lock(lock_path),
        "paused": reason is not None,
        "pause_reason": reason,
        "starts_today": used,
        "starts_remaining": max(0, max_starts - used),
        "max_starts_per_day": max_starts,
        "started": sum(1 for item in tail if item["event"] == "start"),
        "skipped_by_reason": skipped,
        "released": sum(1 for item in tail if item["event"] == "release"),
        "gave_up": sum(1 for item in tail if item["event"] == "give_up"),
    }


#: The flock seam: production locks with fcntl, tests assign a fake.
_flock: Callable[[int, int], None] = fcntl.flock


@contextlib.contextmanager
def drive_lock(path):
    """Hold a non-blocking exclusive flock on `path` for the pass.

    Creates the lock file when missing. Raises `LockHeld` only for
    lock contention (`EWOULDBLOCK`, `EAGAIN`, `EACCES`); setup, open
    and other lock failures raise `LockError`. The lock releases when
    the context exits.
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
        _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        os.close(fd)
        if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise LockHeld(f"another drive pass is already running (lock {path})")
        raise LockError(f"cannot lock {path}: {err}")
    try:
        yield
    finally:
        os.close(fd)


#: The user timer that repeats the dispatch pass, and the service it
#: starts. One fact, one place: the doctor names the same timer, and the
#: install step writes these exact file names.
TIMER_NAME = "korapy-drive.timer"
SERVICE_NAME = "korapy-drive.service"

#: How often the installed timer wakes a pass: the default `drive.period`,
#: baked into the unit at install time. Changing the setting takes a
#: reinstall; a hand-edited unit keeps working until one overwrites it.
TIMER_ON_CALENDAR = "*:0/5"


def _nonblank_period(value):
    """One timer cadence's shape, for the settings layer: a non-blank string.

    The settings layer names the file and key around this rule. The
    value renders verbatim into the installed `OnCalendar=` line, so a
    blank cadence would install a timer that never wakes, and a control
    character would bake a malformed unit that fails only later at
    daemon-reload: both fail here, exit 4, before anything is written.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"must be a non-blank OnCalendar cadence, not {value!r}")
    if any(_is_control_or_separator(ch) for ch in value):
        raise ValueError(
            f"must be a non-blank OnCalendar cadence without control characters, not {value!r}"
        )
    return value


def _is_control_or_separator(char):
    """Whether `char` could break the single `OnCalendar=` line it renders into.

    Every Unicode control or format character (category C) is refused,
    and so are the line and paragraph separators (U+2028 is Zl, not C,
    but splits the unit the same way a newline does): the seam fails
    closed on the whole class instead of an ASCII enumeration.
    """
    category = unicodedata.category(char)
    return category.startswith("C") or category in ("Zl", "Zp")


settings.register(
    "drive.period",
    str,
    TIMER_ON_CALENDAR,
    "systemd OnCalendar cadence at which the installed timer wakes a pass",
    check=_nonblank_period,
)

#: The korapy binary the shipped service starts, in systemd specifier
#: form: the `make install` location. An install elsewhere edits the
#: installed unit, never this default.
TIMER_KORAPY_BIN = "%h/.local/bin/korapy"

#: Timeout for the read-only `systemctl is-enabled` query and for the
#: install step's `daemon-reload` and `enable` calls.
TIMER_TIMEOUT_S = 30


class TimerError(ValueError):
    """A timer unit could not be written, read back or enabled (exit 4)."""

    exit_code = 4


def service_text(korapy_bin=TIMER_KORAPY_BIN) -> str:
    """The shipped service unit: one `korapy drive` pass, then exit. Pure.

    `korapy_bin` is the binary the unit starts; the default is the
    `make install` location. The pass itself never installs or enables
    anything: only the explicit `install-timer` step writes units.
    """
    return (
        "[Unit]\n"
        "Description=korapy dispatch pass (one shot, exits when done)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={korapy_bin} drive\n"
    )


def timer_text(period=None) -> str:
    """The shipped timer unit: wake the pass, catching up one missed tick. Pure.

    `period` is the `OnCalendar=` cadence; None renders the shipped
    default. A blank period, or one with control characters, raises
    `TimerError` (exit 4): the seam fails closed no matter which caller
    passes it.
    """
    if period is not None:
        try:
            _nonblank_period(period)
        except ValueError as err:
            raise TimerError(str(err)) from None
    return (
        "[Unit]\n"
        "Description=run the korapy dispatch pass on its schedule\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={TIMER_ON_CALENDAR if period is None else period}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def timer_dir(env=None) -> str:
    """The systemd user unit dir: `systemd/user` under the XDG config home.

    The base resolves through `paths.config_home`, so the fallback and
    the relative-value rule agree with every other XDG dir in one
    place. `env` maps `XDG_CONFIG_HOME`. This is systemd's dir, not one
    of korapy's, so only this join lives here.
    """
    return os.path.normpath(os.path.join(paths.config_home(env), "systemd", "user"))


def unit_paths(env=None) -> tuple:
    """(service path, timer path) the install step writes. Pure."""
    directory = timer_dir(env)
    return os.path.join(directory, SERVICE_NAME), os.path.join(directory, TIMER_NAME)


def timer_state(run=units.default_run) -> str:
    """Whether the user timer is enabled: one of "enabled", "disabled",
    "masked" or "missing". Pure over the `run` seam.

    The enabled words read as "enabled" and the masked words as
    "masked"; "disabled", "static", "indirect", "generated" and
    "transient" read as "disabled"; the known not-installed answers
    read as "missing". A manager that cannot be asked raises
    `units.Unreachable` (exit 3): a missing `systemctl`, a dead bus,
    or an answered state outside those known words, success or not.
    An unknown answer is never guessed at.
    """
    import subprocess

    try:
        proc = run(["systemctl", "--user", "is-enabled", TIMER_NAME], timeout=TIMER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as err:
        raise units.Unreachable(f"cannot ask whether {TIMER_NAME} is enabled: {err}") from err
    if proc.returncode == 127:
        detail = (proc.stderr or "").strip() or "systemctl not found (exit 127)"
        raise units.Unreachable(f"cannot ask whether {TIMER_NAME} is enabled: {detail}")
    out = ((proc.stdout or "").strip().split() or [""])[0]
    err_text = (proc.stderr or "").strip()
    if proc.returncode == 0 and out in ("enabled", "enabled-runtime"):
        return "enabled"
    low = f"{out} {err_text}".lower()
    if "failed to connect to bus" in low or "no medium found" in low:
        raise units.Unreachable(f"cannot ask whether {TIMER_NAME} is enabled: {err_text or out}")
    if out in ("masked", "masked-runtime"):
        return "masked"
    if out in (
        "disabled",
        "static",
        "indirect",
        "generated",
        "transient",
    ):
        return "disabled"
    if (
        "failed to get unit file state" in low
        or "could not be found" in low
        or "no such file" in low
    ):
        return "missing"
    raise units.Unreachable(
        f"cannot read the {TIMER_NAME} state: {(err_text or out or proc.returncode)}"
    )


def _timer_systemctl(args, run):
    """Run one `systemctl --user` step of the install: raise on failure.

    A dead bus or a missing binary raises `units.Unreachable` (exit 3);
    any other failure raises `TimerError` (exit 4) naming the step.
    """
    import subprocess

    argv = ["systemctl", "--user", *args]
    try:
        proc = run(argv, timeout=TIMER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as err:
        raise units.Unreachable(f"cannot run {' '.join(argv)}: {err}") from err
    if proc.returncode == 127:
        detail = (proc.stderr or "").strip() or "systemctl not found (exit 127)"
        raise units.Unreachable(f"cannot run {' '.join(argv)}: {detail}")
    if proc.returncode != 0:
        tail = ((proc.stderr or "").strip().splitlines() or ["failed"])[-1]
        if "failed to connect to bus" in tail.lower() or "no medium found" in tail.lower():
            raise units.Unreachable(f"cannot run {' '.join(argv)}: {tail}")
        raise TimerError(f"cannot run {' '.join(argv)}: {tail}")


def install_timer(
    *, env=None, run=units.default_run, korapy_bin=TIMER_KORAPY_BIN, period=None
) -> tuple:
    """Install and enable the user timer: the one explicit operator step.

    Writes the service and timer units, reloads the manager, then
    `enable --now` the timer. Returns the (service, timer) paths. Only
    this function writes or enables units: passes never call it, and the
    CLI reaches it only through `korapy drive install-timer`. `period`
    is the `OnCalendar=` cadence baked into the timer unit (None keeps
    the shipped default); the install-timer step resolves it from the
    `drive.period` setting. A blank period raises `TimerError` before
    anything is written.
    """
    timer = timer_text(period)
    directory = timer_dir(env)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as err:
        raise TimerError(f"cannot create {directory}: {err}") from None
    service_path, timer_path = unit_paths(env)
    try:
        paths.atomic_write_text(service_path, service_text(korapy_bin))
        paths.atomic_write_text(timer_path, timer)
    except OSError as err:
        raise TimerError(f"cannot write the timer units: {err}") from None
    _timer_systemctl(["daemon-reload"], run)
    _timer_systemctl(["enable", "--now", TIMER_NAME], run)
    return service_path, timer_path


def _default_live(registered, *, mapping=None, git=None):
    """Live (project, repo, number) triples from units and the registry.

    Repos are store keys through the one short-to-tracker resolver, so
    the admission repo cap counts the same repo the gate request
    names. A short name no entry maps keeps itself, exactly as before.
    `mapping` is the pass's one short-to-tracker map; with none, this
    reads its own from the project roots through `git` (the origin
    seam, defaulting to the real runner).
    """
    mapping = _resolved_map(registered, mapping, syncli._git if git is None else git)
    return {
        (project, *_tracker_key((repo, number), mapping))
        for project, repo, number in launch.admission_state(
            registered, units.list_units, registry.config(), ""
        )
    }


def _default_host_inputs(registered):
    """The admission limits and hold label from the host settings."""
    resolved = settings.load(root=None)
    return (
        projects.limits(
            registered,
            total_cap=resolved["admission.total_cap"],
            repo_cap=resolved["admission.repo_cap"],
        ),
        resolved["board.hold_label"],
    )


def _project_quota(project, pool):
    """One project's quota input from its declared profiles and the pool.

    The declarations validate here (exit 4 naming the value), then the
    gate reads the union of every declared profile's chains per role:
    the pre-launch check cannot resolve per-ticket layers, so it covers
    every chain any ticket could resolve to, and the launch re-resolves
    exactly (exit 75 when the ticket's own profile is exhausted). With
    no profiles declared the chains are empty, so the gate passes them.
    The freshness bound comes from the shared helper over the pool
    providers those chains name, never a second resolver.
    """
    resolved = settings.load(root=project.root)
    parsed = profiles.validate(resolved.profiles, pool)
    chains: dict[str, list[str]] = {}
    for profile in parsed.values():
        for role, chain in profile.models.items():
            known = chains.setdefault(role, [])
            for entry in chain:
                if entry not in known:
                    known.append(entry)
    return QuotaSource(
        chains=chains,
        stale_after=quota.stale_after_for(chains, pool),
        low_pct=resolved["quota.low_pct"],
        unknown_blocks=resolved["quota.unknown_blocks"],
    )


def _store_labels(store, now):
    """A `read_labels` seam over cached store rows: reads, never a tracker.

    Only issue rows exist in the store, so a pull read fails closed as
    unreadable, like a missing row: the hold gate skips instead of
    passing a ticket whose pull labels it could not see. Linked pulls
    themselves never live in the store; the pass reads them through the
    `pulls_for` seam, which queries the tracker, and the rows carry them.
    """

    def read(kind, repo, number):
        if kind != "issue":
            raise board.StoreError(f"unknown item {repo}#{number}: no pull rows in the store")
        found, _ = store.item(repo, number, now=now)
        if found is None:
            raise board.StoreError(f"unknown item {repo}#{number}")
        return list(found.labels or [])

    return read


def review_records_for(items, *, read, err):
    """{(repo, number): latest review record} for rows with one on file.

    `read` is the injected review-record reader ((repo, number) ->
    record or None, for example a `reviewrecord.store_reader` over the
    pass's short-to-tracker map); a row it cannot read counts as
    having no record, exactly as before. A row it refuses (an
    unresolvable repository, any other lookup failure) is one loud
    line on `err` naming the key, never a silent gap: an empty result
    means no records, not swallowed exceptions. The pass builds this
    map once from its rows and the gate reads it, so the record files
    are listed once per pass, not once per candidate.
    """
    found = {}
    for item in items or ():
        try:
            record = read(item.repo, item.number)
        except Exception as exc:
            print(
                f"korapy drive: cannot read the review record for "
                f"{item.repo}#{item.number} ({exc})",
                file=err,
            )
            continue
        if record is not None:
            found[(item.repo, item.number)] = record
    return found


def production_review_reader(registered, *, git, mapping=None):
    """A `review_records_for` read over the store's tracker-repo keys.

    Returns the reader that finds each row's record under its short
    session dir, so the loop sees what the reviewer wrote. `mapping`
    is the pass's one short-to-tracker map (`production_repo_map`):
    every builder in one pass gets the same one, so a config edit
    between them cannot make two joins disagree; with none, the
    builder resolves its own. Raises SettingsError for bad registry
    or project settings, like the sibling production builders: the
    pass refuses instead of gating blind.
    """
    mapping = _resolved_map(registered, mapping, git)
    return reviewrecord.store_reader(mapping)


def _review_missing(holder, newest, reviews, key) -> bool:
    """Whether the gate skips this ticket as `review-missing`. Pure.

    The gate's skip and `missing_review_records` share this check, so
    the skip verdicts and the once-per-pass log line agree: the row is
    unheld, the newest finished entry names a reviewer with outcome
    `ok`, the review map exists, and the ticket has no record. Short
    entries read as unreviewed, never as missing.
    """
    return (
        not holder
        and newest is not None
        and _finished_role(newest) == "reviewer"
        and _finished_outcome(newest) == "ok"
        and reviews is not None
        and key not in reviews
    )


def missing_review_records(finished, reviews, rows) -> list:
    """[(repo, number, agent)] finished reviewers (outcome ok) with no record. Pure.

    `finished` is the newest finished entry per ticket, `reviews` the
    latest record per ticket, `rows` the {(repo, number): row} map with
    the pass's releases applied. Only rows the gate could have skipped
    as `review-missing` list: the row must be known, open, internally
    owned and unheld, so a kept claim (a live unit, a failed release
    write), a non-open or externally owned row, and a ticket with no
    row never log. With no review map (`reviews` None) the check cannot
    run and nothing is missing. A non-`ok` outcome is not a protocol
    failure and never lists. Sorted for a stable once-per-pass log line.
    """
    if finished is None or reviews is None:
        return []
    found = []
    for (repo, number), newest in finished.items():
        row = (rows or {}).get((repo, number))
        if row is None or row.state != "open" or row.external:
            continue
        if not _review_missing(row.agent or "", newest, reviews, (repo, number)):
            continue
        found.append((repo, number, _finished_agent(newest)))
    return sorted(found)


def log_missing_reviews(out, finished, reviews, rows) -> int:
    """Print the once-per-pass missing-record lines. Returns the count.

    One line per ticket from `missing_review_records`, naming the
    ticket and the finished reviewer id. Both passes call this after
    gating with the pass's released rows, so the line and the
    `review-missing` skip verdicts agree.
    """
    count = 0
    for repo, number, agent in missing_review_records(finished, reviews, rows):
        print(
            f"{repo}#{number}: reviewer finished without a review record (`{agent}`), "
            "review missing, skipped",
            file=out,
        )
        count += 1
    return count


def tracker_pulls_for(tracker_factory):
    """A `pulls_for` seam over the pass's per-repo trackers.

    Each call reads the ticket's linked pull requests through its
    tracker's `linked_pulls` and keeps their numbers and states as
    `LinkedPull` rows for the pass's row building. A repo with no
    tracker reads as no linked pulls; a tracker failure propagates,
    and the pass map marks the ticket unreadable instead of starting
    blind.
    """

    def pulls_for(repo, number):
        try:
            tracker = tracker_factory(repo)
        except KeyError:
            return ()
        return tuple(
            LinkedPull(number=item.number, state=item.state)
            for item in tracker.linked_pulls(repo, number)
            if getattr(item, "kind", "pr") == "pr"
        )

    return pulls_for


def linked_pulls_map(items, pulls_for):
    """{(repo, number): linked pulls} for these store items, one read each.

    The pass's one linked-pull source: the row builders (eligibility
    and gating) read from this map, so the policies and the gate see
    the same pulls with no second lookup. An unreadable ticket maps to
    None, and readers fail that ticket closed; a ticket the seam reads
    as empty maps to ().
    """
    found = {}
    for item in items:
        key = (item.repo, item.number)
        if key in found:
            continue
        try:
            found[key] = tuple(pulls_for(item.repo, item.number) or ())
        except Exception:
            found[key] = None
    return found


#: The proceeding kinds the summary counts after starts, in order.
SUMMARY_KINDS = ("resume", "fix", "review")

#: The past tense each proceeding kind takes in a non-plan summary.
PAST = {"resume": "resumed", "fix": "fixed", "review": "reviewed"}


def render(rows, *, label, plan=True):
    """The candidate table plus the summary line under `label`.

    Each row names its project, so two projects requesting the same
    ticket print distinguishable lines. The summary counts only
    proceeding `start` rows as starts; other proceeding kinds are named
    separately, and skipped rows count toward no kind.
    """
    lines = []
    for row in rows:
        head = f"{row.project} " if row.project else ""
        lines.append(f"{head}{row.repo}#{row.number}: {row.verdict} ({row.reason})")
    proceeding = [row for row in rows if row.verdict != "skip"]
    starts = sum(1 for row in proceeding if row.kind == "start")
    started = "would start" if plan else "started"
    summary = f"{label}: {len(rows)} candidate(s), {started} {starts}"
    for kind in SUMMARY_KINDS:
        count = sum(1 for row in proceeding if row.kind == kind)
        if count:
            summary += f", would {kind} {count}" if plan else f", {PAST[kind]} {count}"
    lines.append(summary)
    return "\n".join(lines) + "\n"


def _read_inputs(
    store,
    out,
    err,
    *,
    now,
    projects_fn,
    live,
    limits,
    quotas,
    ledger,
    read_labels,
    hold_label,
    pool,
    repo_map=None,
):
    """Shared setup for both passes: rows, projects, live state, ledger, limits.

    Returns ((items, registered, live_set, limits, hold_label,
    read_labels, ledger_data, quota_for, pool), None) on success, or
    (None, exit code) when the pass cannot proceed. A project whose
    policy or quota settings fail is not fatal here; that surfaces per
    project in `_gate_all`. `repo_map` is the pass's one
    short-to-tracker map for the admission live set; with none, the
    live set resolves its own.
    """
    try:
        items = store.items()
    except board.StoreError as problem:
        print(f"korapy drive: {problem}", file=err)
        return None, USAGE
    except sqlite3.Error as problem:
        print(f"korapy drive: cannot read the store: {problem}", file=err)
        return None, USAGE
    try:
        registered = (projects_fn or projects.load_all)()
    except projects.ProjectsError as problem:
        print(f"korapy drive: {problem}", file=err)
        return None, USAGE
    try:
        live_set = set(live) if live is not None else _default_live(registered, mapping=repo_map)
    except settings.SettingsError as problem:
        print(f"korapy drive: {problem}", file=err)
        return None, USAGE
    except Exception as problem:
        print(f"korapy drive: cannot read live state: {problem}", file=err)
        return None, UNREACHABLE
    try:
        ledger_data = ledger if ledger is not None else quota_ledger.load()
    except quota_ledger.LedgerError as problem:
        print(f"korapy drive: {problem}", file=err)
        return None, USAGE
    if limits is None or hold_label is None:
        try:
            default_limits, default_hold_label = _default_host_inputs(registered)
        except (settings.SettingsError, admission.LimitsError) as problem:
            print(f"korapy drive: {problem}", file=err)
            return None, USAGE
        if limits is None:
            limits = default_limits
        if hold_label is None:
            hold_label = default_hold_label
    if read_labels is None:
        read_labels = _store_labels(store, now)
    quota_for = dict(quotas) if quotas is not None else {}
    if pool is None and any(project.name not in quota_for for project in registered):
        try:
            pool = accounts.providers()
        except accounts.AccountsError as problem:
            print(f"korapy drive: {problem}", file=err)
            return None, USAGE
    return (
        items,
        registered,
        live_set,
        limits,
        hold_label,
        read_labels,
        ledger_data,
        quota_for,
        pool,
    ), None


def _released_rows(items, released, *, pulls_by_key=None):
    """{(repo, number): row} with this pass's releases applied. Pure.

    The gate and the missing-record log read the same map, so a freed
    row gates and logs as unheld while a kept claim gates and logs as
    held. One builder keeps the two from disagreeing on the holder.
    `pulls_by_key` carries the linked pulls the pass read through the
    tracker (None marks an unreadable ticket); with none every row
    reads empty. The gate reads the same row pulls, so the two never
    disagree.
    """
    by_key = {
        (row.repo, row.number): row
        for row in eligibility.rows_from_items(items, pulls_by_key=pulls_by_key)
    }
    for key in released or ():
        prior = by_key.get(key)
        if prior is not None:
            by_key[key] = replace(prior, agent="")
    return by_key


def _gate_all(
    items,
    registered,
    out,
    *,
    now,
    live_set,
    limits,
    quota_for,
    pool,
    ledger_data,
    read_labels,
    hold_label,
    released=None,
    finished=None,
    agent_roles=None,
    agent_states=None,
    agent_tickets=None,
    reviews=None,
    fix_threshold=None,
    pulls_for=None,
):
    """Eligibility plus the section 3 gate order over every candidate.

    Returns (gated_rows, problems, requested): one `Gated` per candidate in section
    3 order, one error line per project whose policy or quota
    settings failed, and the set of (repo, number) any project requested
    this pass, which the stuck sweep reads for withdrawals. A project with no eligibility entry prints its
    logbook line to `out`. `released` ({(repo, number): reviewer id}
    freed this pass) simulates the post-release rows, so a plan over
    still-claimed rows gates what the real pass would gate after the
    release; `finished` carries the newest finished entry per ticket for
    the reviewer and fix-round readiness; `agent_roles` ({agent id:
    role} for visible entries), resolved through `agent_states` and
    `agent_tickets` by ticket, feeds the gate's reviewer-holder skip,
    so a plan matches the claiming pass. `reviews` ({(repo, number):
    latest review record}) feeds the gate's fix-round threshold;
    `fix_threshold` (None reads the `drive.fix_round_threshold` host
    setting) is the lowest finding severity that still opens a fix
    round; `pulls_for` (None builds rows with no pulls, which is what
    `--plan` does: it never contacts the tracker) feeds the pass's one
    linked-pull map, which the eligibility rows and the gate rows share,
    so the policies and the gate read the same row pulls with no second
    lookup. Nothing is written.
    """
    if fix_threshold is None:
        try:
            fix_threshold = settings.load(root=None)["drive.fix_round_threshold"]
        except settings.SettingsError as problem:
            problems = [f"korapy drive: {problem}"]
            fix_threshold = reviewrecord.DEFAULT_FIX_THRESHOLD
        else:
            problems = []
    else:
        problems = []
    pulls_by_key = linked_pulls_map(items, pulls_for) if pulls_for is not None else None
    by_key = _released_rows(items, released, pulls_by_key=pulls_by_key)
    waits = {(item.repo, item.number): item.tracker_updated_at for item in items}
    candidates = []
    for project in registered:
        if project.name not in quota_for:
            try:
                quota_for[project.name] = _project_quota(project, pool)
            except settings.SettingsError as problem:
                problems.append(f"korapy drive: {problem}")
                continue
        try:
            outcome = eligibility.for_project(project, items, now=now, pulls_by_key=pulls_by_key)
        except eligibility.EligibilityError as problem:
            problems.append(f"korapy drive: {problem}")
            continue
        if outcome.skipped is not None:
            print(f"project {project.name}: {outcome.skipped}", file=out)
            continue
        for requested in outcome.requests:
            key = (requested.repo, requested.number)
            candidates.append(
                GateInput(project.name, requested, by_key.get(key), waits.get(key, 0.0))
            )
    requested_keys = {
        (candidate.request.repo, candidate.request.number)
        for candidate in candidates
        if candidate.request is not None
    }
    for item in items:
        if item.state != "open" or not item.agent:
            continue
        if (item.repo, item.number) in requested_keys:
            continue
        if released is not None and (item.repo, item.number) in released:
            continue
        key = (item.repo, item.number)
        candidates.append(GateInput("", None, by_key.get(key), waits.get(key, 0.0)))

    counted: dict = {}
    for entry_project, _, _ in live_set:
        counted[entry_project] = counted.get(entry_project, 0) + 1

    def floor_of(name):
        return admission.bounds(limits.projects.get(name))[0]

    stop_starts = False
    gated_rows = []
    for candidate in order(candidates):
        if candidate.request is not None and candidate.row is not None:
            holder_now = candidate.row.agent if candidate.row is not None else ""
            if (
                stop_starts
                and route_kind(candidate.request.kind, holder_now) == "start"
                and counted.get(candidate.project, 0) >= floor_of(candidate.project)
            ):
                ref = f"{candidate.request.repo}#{candidate.request.number}"
                gated_rows.append(
                    Gated(
                        project=candidate.project,
                        repo=candidate.request.repo,
                        number=candidate.request.number,
                        kind="start",
                        verdict="skip",
                        reason=f"{ref}: host full, new starts stop here",
                        code=admission.TOTAL_FULL,
                    )
                )
                continue
        verdict = gate(
            candidate,
            read_labels=read_labels,
            hold_label=hold_label,
            live=live_set,
            limits=limits,
            quota_source=quota_for.get(candidate.project, EMPTY_QUOTA),
            ledger=ledger_data,
            now=now,
            finished=finished,
            agent_roles=agent_roles,
            agent_states=agent_states,
            agent_tickets=agent_tickets,
            reviews=reviews,
            fix_threshold=fix_threshold,
        )
        if verdict.code == admission.TOTAL_FULL:
            stop_starts = True
        gated_rows.append(verdict)
    return gated_rows, problems, set(requested_keys)


def _pass(
    store,
    out,
    err,
    *,
    label,
    now,
    projects_fn,
    live,
    limits,
    quotas,
    ledger,
    read_labels,
    hold_label,
    pool,
    agent_states=None,
    finished_for=None,
    agent_roles=None,
    agent_tickets=None,
    live_units=None,
    repo_map=None,
    review_reader=None,
    pulls_for=None,
) -> int:
    """One read-only pass: join eligibility to store rows, gate, print.

    Nothing is synced, claimed, enqueued, started, mirrored or journaled.
    A project whose policy or quota settings fail is skipped with one
    error line; the pass still gates every other project and exits 4.
    `agent_states`, `agent_tickets`, `finished_for` and `agent_roles`
    feed the reviewer and fix-round readiness exactly like the claiming
    pass, so the plan shows the verdicts the real pass would print;
    all are reads only. `live_units` filters the finished-reviewer
    simulation the same way the claiming pass filters its release, so
    a live unit's row gates as still claimed here too; without it every
    finished reviewer row simulates as released. The production CLI
    supplies the host units. `repo_map` is the pass's one
    short-to-tracker map, threaded to the admission live set.
    `review_reader` feeds the gate's fix-round threshold from the
    session dirs (reads only, like the registry); with none the
    threshold sees no records, exactly as before. Both passes also
    log each unheld finished reviewer (outcome `ok`) with no record
    once, naming the ticket and the reviewer, before the table, so a
    kept claim never logs a line the gate did not skip on. `pulls_for`
    feeds the pass's one linked-pull map, which the eligibility rows
    and the gate rows share; with none every row reads empty, which is
    what `--plan` builds: it never contacts the tracker.
    """
    ctx, fatal = _read_inputs(
        store,
        out,
        err,
        now=now,
        projects_fn=projects_fn,
        live=live,
        limits=limits,
        quotas=quotas,
        ledger=ledger,
        read_labels=read_labels,
        hold_label=hold_label,
        pool=pool,
        repo_map=repo_map,
    )
    if fatal is not None:
        return fatal
    (
        items,
        registered,
        live_set,
        limits,
        hold_label,
        read_labels,
        ledger_data,
        quota_for,
        pool,
    ) = ctx
    plan_live = frozenset(live_units or ())
    authors = _capture_finished_holders(
        store, agent_states=agent_states, agent_tickets=agent_tickets, err=err
    )
    finished = resolve_finished(
        finished_for, authors, agent_states=agent_states, agent_tickets=agent_tickets
    )
    reviews = (
        review_records_for(items, read=review_reader, err=err)
        if review_reader is not None
        else None
    )
    released = reviewed_claims(authors, finished, live_units=plan_live)
    gated_rows, problems, _ = _gate_all(
        items,
        registered,
        out,
        now=now,
        live_set=live_set,
        limits=limits,
        quota_for=quota_for,
        pool=pool,
        ledger_data=ledger_data,
        read_labels=read_labels,
        hold_label=hold_label,
        released=released,
        finished=finished,
        agent_roles=agent_roles,
        agent_states=agent_states,
        agent_tickets=agent_tickets,
        reviews=reviews,
        pulls_for=pulls_for,
    )
    log_missing_reviews(out, finished, reviews, _released_rows(items, released))
    table = [
        Candidate(
            project=gated.project,
            repo=gated.repo,
            number=gated.number,
            kind=gated.kind,
            verdict=gated.verdict,
            reason=gated.reason,
        )
        for gated in gated_rows
    ]
    out.write(render(table, label=label))
    for problem in problems:
        print(problem, file=err)
    return USAGE if problems else OK


def production_operators(registered, git, *, resolved=None):
    """`{tracker_repo: operators}` across every registered project's root.

    Resolves through the shared helper, the same way sync and mirror
    do, so the pass's sync, freshness re-reads and mirror agree with
    both CLIs on which authors are internal. `resolved` maps each
    registered root to its already-resolved `{name: (tracker repo,
    operators)}` entries: with it the pass shares its one resolution
    instead of resolving again (every root in `registered` must have
    an entry); with none, the builder resolves its own. Returns
    (mapping, None), or (None, problem) on the first configuration
    error.
    """
    mapping = {}
    for project in registered:
        if resolved is None:
            try:
                names = settings.repo_names(project.root)
            except settings.SettingsError as err:
                return None, str(err)
            entries, problem = syncli.repo_operators(project.root, names, git)
            if problem is not None:
                return None, problem
        else:
            entries = resolved[project.root]
        for _name, (tracker_repo, ops) in entries.items():
            mapping.setdefault(tracker_repo, ops)
    return mapping, None


def sync_all(store, tracker_factory, *, operators, now):
    """Sync every repo with cached rows, each through its own tracker.

    ``tracker_factory`` maps a tracker repo to its tracker (labels or
    board, built by the one factory); board repos run the feed step then
    the board step, and a complete board poll also adopts the tracker's
    missing issues onto the board through the same reader. Returns
    (seen, errors, store_errors, refused, adopt_errors): the rows
    applied, one line per tracker/outage failure, one line per corrupt
    stored row, one line per refused read (a validation rejection the
    tracker answered, toward exit 1, never 3), and one line per failed
    board add (retried next pass, never counted toward an exit). A
    failed repo keeps its rows and its cursor (that is `sync_repo`'s
    contract) and the pass moves on to the next repo; a repo the
    factory does not know is an error line, never a silent skip. With
    no factory there is nothing to sync from: no rows move and no
    errors are reported.

    The repo list itself parses no row JSON (`Store.repos`), so one
    corrupt row cannot hide the healthy repos; each repo's rows are then
    enumerated inside the pass, and a repo whose rows cannot be parsed
    is reported as one store line and skipped while the other repos
    still sync. The other stored enumerations in the drive pass need no
    guard here: orphan release, reviewed-claim release and stuck
    handling are already caught at their call sites, and the shared
    read setup aborts earlier with exit 4 on a corrupt row anyway.
    """
    if tracker_factory is None:
        return 0, [], [], [], []
    seen = 0
    errors = []
    store_errors = []
    refused = []
    adopt_errors: list = []
    try:
        repos = store.repos()
    except (board.StoreError, sqlite3.Error) as err:
        return 0, [], [f"store repo listing unreadable: {err}"], [], []
    for repo in sorted(repos):
        try:
            tracker = tracker_factory(repo)
        except KeyError:
            errors.append(f"{repo}: no tracker for this repo, skipped")
            continue
        try:
            store.items(repo=repo)
        except board.StoreError as err:
            store_errors.append(str(err))
            continue
        ops = list(mirror._ops_for(operators, repo))
        result = sync.sync_repo(
            store,
            tracker,
            repo,
            operators=ops,
            now=now,
            cursor=store.get_cursor(repo, sync.CURSOR_NAME),
        )
        seen += result.items_seen
        errors.extend(result.errors)
        refused.extend(result.refused)
        store_errors.extend(result.store_errors)
        if not trackers.is_board_tracker(tracker):
            continue
        try:
            schema = tracker.board_schema()
        except iface.TrackerRejected as err:
            refused.append(f"{repo}: {err}")
            continue
        except (iface.TrackerError, settings.SettingsError) as err:
            errors.append(f"{repo}: {err}")
            continue
        board_result = sync.sync_board_repo(
            store,
            tracker,
            schema,
            repo,
            operators=ops,
            now=now,
            max_pages=tracker.board_poll_pages,
        )
        seen += board_result.items_seen
        errors.extend(board_result.errors)
        refused.extend(board_result.refused)
        store_errors.extend(board_result.store_errors)
        adopt_errors.extend(board_result.adopt_errors)
    return seen, errors, store_errors, refused, adopt_errors


def refresh_row(store, tracker_factory, repo, number, *, operators, now, stale_after):
    """The row to decide from: cached when fresh, live-checked when stale.

    Returns (item, None) on the row to use, or (None, reason) when a
    stale row could not be re-read live, or when the store itself could
    not be read: the claim path fails closed instead of deciding from
    the stale row. With no factory there is no live check to run, so
    the cached row stands. The live check goes through the repo's own
    tracker from the factory, so a board row re-reads through the board.
    """
    ref = f"{repo}#{number}"
    try:
        found, fresh = store.item(repo, number, now=now, max_age=stale_after)
    except (board.StoreError, sqlite3.Error) as err:
        return None, f"{ref}: store unreadable, claim refused ({err})"
    if fresh or tracker_factory is None:
        return found, None
    try:
        tracker = tracker_factory(repo)
    except KeyError:
        return None, f"{ref}: no tracker for this repo, claim refused"
    try:
        live = tracker.item(repo, number)
    except iface.TrackerRejected as err:
        return None, f"{ref}: tracker rejected the read, row stale, claim refused ({err})"
    except iface.TrackerError as err:
        return None, f"{ref}: tracker unreachable, row stale, claim refused ({err})"
    try:
        store.upsert_item(
            sync._reading(live),
            operators=list(mirror._ops_for(operators, repo)),
            now=now,
        )
    except (board.StoreError, sqlite3.Error) as err:
        return None, f"{ref}: store unreadable, claim refused ({err})"
    try:
        found, _ = store.item(repo, number, now=now)
    except (board.StoreError, sqlite3.Error) as err:
        return None, f"{ref}: store unreadable, claim refused ({err})"
    if found is None:
        return None, f"{ref}: no store row, skipped as withdrawn"
    return found, None


def is_orphan(
    claim,
    *,
    now,
    gone_s,
    present_agents=None,
    live_units=None,
    agent_states=None,
    agent_tickets=None,
) -> bool:
    """Whether `claim` is provably dead under the section 4 rule. Pure.

    Dead means the store names a managed holder (an id this loop
    minted), the registry shows no live or stale entry for the
    ticket, no live unit exists for the ticket, and the claim is older
    than twice `gone_s`. Presence resolves through the one resolver
    (`entry_for_claim`, by ticket with `agent_tickets`, never by holder
    id): a live or stale entry for the ticket leaves the claim alone.
    `present_agents` is the legacy same-namespace set (ids with a live
    or stale entry) kept for callers without a ticket index; a holder
    found there is also left alone. `live_units` is the (repo, number)
    pairs with a live unit. Anything less certain (a present agent, a
    live unit, a young claim) is left for stuck handling, and a foreign
    holder is never an orphan: the loop never touches a foreign claim.
    """
    if claim is None or not claim.agent:
        return False
    if not is_managed_agent(claim.agent):
        return False
    present = present_agents if present_agents is not None else frozenset()
    if claim.agent in present:
        return False
    live = live_units if live_units is not None else frozenset()
    if (claim.repo, claim.number) in live:
        return False
    if agent_states is not None:
        seen = entry_for_claim(claim, agent_states=agent_states, agent_tickets=agent_tickets)
        if seen is not None and seen[1] in (registry.LIVE, registry.STALE):
            return False
    return now - claim.claimed_at > 2 * gone_s


def _orphan_claims(
    store,
    *,
    now,
    gone_s,
    present_agents=None,
    live_units=None,
    agent_states=None,
    agent_tickets=None,
):
    """The provably-dead claims, in store order. Pure reads.

    The scan half of `release_orphans`: each store row whose claim the
    orphan rule calls dead, resolved through the one resolver. Runs
    through the shared retry policy; a persistent failure propagates
    for the caller to skip the step with every claim kept.
    """

    def scan():
        found = []
        for item in store.items():
            claim = store.claim_of(item.repo, item.number)
            if not is_orphan(
                claim,
                now=now,
                gone_s=gone_s,
                present_agents=present_agents,
                live_units=live_units,
                agent_states=agent_states,
                agent_tickets=agent_tickets,
            ):
                continue
            found.append(claim)
        return found

    return _retry_scan(scan)


def release_orphans(
    store,
    *,
    now,
    gone_s,
    present_agents=None,
    live_units=None,
    out,
    events=None,
    agent_states=None,
    agent_tickets=None,
) -> int:
    """Release every provably-dead claim, one logbook line each.

    Returns the released count. The scan runs first, through the
    shared retry policy; each found claim's agent and epoch then go
    straight from the scan into `Store.release`, which releases
    only on a matching agent plus epoch and raises `Claimed`
    otherwise. A `Claimed` there means the row changed under the pass
    (re-claimed elsewhere), so it is no longer an orphan: the row is
    skipped with a logbook line, never retried in the same pass. A
    failed write likewise propagates, never retried here: the caller
    skips the step with the claim kept. A successful release clears
    the holder and leaves the epoch for the next claim to mint, which
    fences a stale holder still acting on the old epoch; the worktree
    and branch stay untouched. Rows with no claim are left alone:
    adoption happens only in resync, never here. When `events` is a
    list, each release appends its journal entry.
    """
    orphans = _orphan_claims(
        store,
        now=now,
        gone_s=gone_s,
        present_agents=present_agents,
        live_units=live_units,
        agent_states=agent_states,
        agent_tickets=agent_tickets,
    )
    released = 0
    for claim in orphans:
        ref = f"{claim.repo}#{claim.number}"
        try:
            store.release(claim.repo, claim.number, claim.agent, claim.epoch, now=now)
        except board.Claimed:
            print(f"{ref}: orphan release raced a re-claim, left held", file=out)
            continue
        print(
            f"{ref}: released dead claim by `{claim.agent}`, epoch {claim.epoch} kept",
            file=out,
        )
        if events is not None:
            events.append({"event": "release", "at": now, "ticket": ref, "agent": claim.agent})
        released += 1
    return released


def drop_stale_claim(store, repo, number, *, stop_unit, out, now) -> bool:
    """Stop the units and drop a local claim the hub superseded.

    The reconnect rule from docs/08-hub-multihost.md section 2: stop
    the ticket's units through the injected stop seam, drop the local
    claim state without touching the hub row, and log. Never resumes
    and never re-proposes; the next pass may propose only tickets the
    hub shows as unclaimed. A failing row defers that ticket instead of
    aborting the whole ordered reconcile. Returns True when the ticket is clear (or
    was already clear) and False when the stop or the drop failed, in
    which case the claim is kept for the next pass. A release race
    (the row changed under the pass) is left held, never retried here.
    """
    ref = f"{repo}#{number}"
    try:
        claim = store.claim_of(repo, number)
    except (board.StoreError, sqlite3.Error) as problem:
        print(f"{ref}: stale-claim read refused ({problem}), left held", file=out)
        return False
    if claim is None or not claim.agent:
        print(f"{ref}: no local claim, nothing to drop", file=out)
        return True
    try:
        stop_unit(repo, number)
    except Exception as problem:
        print(f"{ref}: stale-claim stop failed ({problem}), claim kept", file=out)
        return False
    try:
        store.release(repo, number, claim.agent, claim.epoch, now=now)
    except board.Claimed:
        print(f"{ref}: stale-claim drop raced a re-claim, left held", file=out)
        return False
    except (board.StoreError, sqlite3.Error) as problem:
        print(f"{ref}: stale-claim drop refused ({problem}), left held", file=out)
        return False
    print(
        f"{ref}: stopped units and dropped the stale claim by `{claim.agent}`"
        f" (local epoch {claim.epoch} superseded)",
        file=out,
    )
    return True


class HubGone(Exception):
    """The hub is down for this pass: skip every claim-needing row."""


class HubConflict(Exception):
    """The hub refused a claim: another holder owns the ticket.

    Raised by the hub facade (which translates the hub's conflict)
    so the pass never names the hub module: `holder` is the hub's
    holder mapping, and the pass reports the owning agent from it.
    """

    def __init__(self, holder):
        super().__init__("hub refused the claim: another holder owns the ticket")
        self.holder = holder


class HubConfigError(Exception):
    """The hub is misconfigured or answered garbage (exit 4).

    Raised by the hub facade (which translates the hub's usage
    error) so the pass never names the hub module. A dead hub is
    `HubGone` instead: unreachable skips claims, misconfiguration
    refuses the pass.
    """


#: Automatic restarts per ticket in any sliding window.
RESTART_BUDGET = 3
RESTART_WINDOW_S = 24 * 60 * 60

#: Store scans retried before a step gives up: a transient sqlite lock
#: (a lock a concurrent writer holds) retries this many times before
#: the scan raises for the caller to skip the step. Anything else
#: (a corrupt row, a failed disk write) raises at once instead: a
#: retry cannot heal it. Writes never retry: a failed write skips the
#: row or the step with the claim kept, never retried in the same pass.
SCAN_READ_ATTEMPTS = 3


def _transient_lock(error) -> bool:
    """Whether a store error is a transient sqlite lock, safe to retry. Pure.

    Only a locked or busy database retries: another writer holds the
    lock, so the next attempt may proceed. Every other store error
    fails closed at once.
    """
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _retry_scan(read):
    """The value `read()` returns, retrying transient locks.

    The shared scan policy for the stuck sweep, the finished-claims
    scan and the orphan scan: transient locks retry up to
    `SCAN_READ_ATTEMPTS`, then the last error propagates for the
    caller to skip the step with every claim kept; any other store
    error raises at once.
    """
    for _ in range(SCAN_READ_ATTEMPTS - 1):
        try:
            return read()
        except sqlite3.Error as error:
            if not _transient_lock(error):
                raise
    return read()


#: The restart-budget state file inside the state dir.
RESTARTS_NAME = "drive-restarts.json"


def restarts_path():
    """The restart-budget state file: inside the state dir.

    The only path join for it (AGENTS.md, "One fact, one place").
    """
    return os.path.join(paths.state_dir(), RESTARTS_NAME)


def prune_restarts(stamps, *, now):
    """The stamps still inside the sliding window. Pure."""
    return [stamp for stamp in stamps if now - stamp < RESTART_WINDOW_S]


def restart_allowed(stamps, *, now):
    """Whether another stop-plus-launch restart fits the budget. Pure.

    At most RESTART_BUDGET restarts per ticket in any sliding 24 hour
    window. Only stop-plus-launch restarts and fresh fix rounds are
    recorded, so only they count: a fix-round resume reuses the session
    and never consumes the budget.
    """
    return len(prune_restarts(stamps, now=now)) < RESTART_BUDGET


def record_restart(stamps, *, now):
    """The stamps after recording one stop-plus-launch restart. Pure.

    Prunes expired stamps, then appends now. Call only for restarts that
    stopped and launched, and for fresh fix rounds that launched;
    anything else never consumes the budget.
    """
    return [*prune_restarts(stamps, now=now), now]


def load_restarts(path):
    """The budget stamps by `repo#number`, tolerant of a missing file.

    A missing or corrupt file reads as empty: the budget self-heals on
    the next recorded restart.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    found = {}
    for key, stamps in raw.items():
        if isinstance(key, str) and isinstance(stamps, list):
            found[key] = [
                stamp
                for stamp in stamps
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
            ]
    return found


def save_restarts(path, budgets):
    """Persist the budget stamps, atomically. Raises OSError."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    paths.atomic_write_text(path, json.dumps(budgets, sort_keys=True))


def classify_stuck(*, known, age, unit_live, stale_s, gone_s):
    """`healthy` | `watch` | `restart` for one claimed ticket. Pure.

    `known` is whether the holder has any registry entry; `age` is the
    seconds since its last heartbeat. A heartbeat older than stale_s
    with a live unit watches once more; older than gone_s or a dead
    unit restarts. A fresh heartbeat with a dead unit also watches: the
    agent is alive, so the dead unit may be a listing the pass missed.
    A live unit with no registry entry at all watches too, never
    restarts: the agent may have just started and not heartbeated yet,
    so a live unit is never restarted on a missing heartbeat alone.
    Only a dead unit with no entry restarts.
    """
    if known and age < stale_s and unit_live:
        return "healthy"
    if not known and unit_live:
        return "watch"
    if age >= gone_s or (not unit_live and age >= stale_s):
        return "restart"
    return "watch"


def _stuck_rows(store):
    """The store rows for the stuck sweep, tolerating transient locks.

    Scans through the shared retry policy, then raises for the caller
    to skip the sweep with every claim kept. Store errors (corrupt
    rows) raise at once: a retry cannot heal them.
    """
    return list(_retry_scan(store.items))


def give_up_body(repo, number, *, observed, restarts):
    """The decision-protocol comment for a ticket past the restart budget.

    Pure. The body carries the tail of what the pass observed, so the
    human taking over sees why the loop walked away. It never names the
    agent id: the id is opaque (a hash over the host segment, the repo
    and the epoch seconds) and this body is posted to
    the tracker, so it stays out of every give-up comment.
    """
    ref = f"{repo}#{number}"
    return (
        f"korapy drive gave up on {ref}: {observed}. "
        f"It used {restarts} stop-plus-launch restarts in the last 24h "
        f"(budget {RESTART_BUDGET}). A human should take it from here; "
        "the loop released its claim."
    )


@dataclass(frozen=True)
class _HolderClaim:
    """The (repo, number, agent) `entry_for_claim` reads off a claim."""

    repo: str
    number: int
    agent: str


def _holder_claim(holder, repo, number) -> _HolderClaim:
    """The claim view for a bare holder id, for `entry_for_claim`."""
    return _HolderClaim(repo=repo, number=number, agent=holder or "")


def entry_for_claim(claim, *, agent_states, agent_tickets=None):
    """`(entry id, state, last_seen)` of the registry entry for this claim. Pure.

    The one resolver for which registry entry belongs to a claim, read
    by the stuck sweep, the finished-claims scan and the fix-round
    readiness: every one of them goes through here, never a second
    copy. A claim's holder is an opaque id this loop minted
    (`drive-<hash8>-<n>`); registry entries are keyed by unit name
    (`<prefix>-<repo>-<n>`, `-r<gen>` on later generations), so the
    holder id never names an entry. The link is the ticket:
    `agent_tickets` maps entry id to the claim's (repo, number) store
    key, built through the one short-to-tracker resolver, and the
    newest entry for the claim's ticket wins by last heartbeat (a
    relaunch heartbeats past the generation it replaced; a reviewer
    heartbeats past the worker it took over from). With no ticket
    index the holder id itself is the only key available: production
    always passes the index, so that fallback never fires outside
    tests. A ticket with no entry resolves to None.
    """
    states = agent_states or {}
    if agent_tickets is None:
        seen = states.get(claim.agent)
        if seen is None:
            return None
        return (claim.agent, seen[0], seen[1])
    newest = None
    for entry_id, ticket in agent_tickets.items():
        if ticket != (claim.repo, claim.number):
            continue
        seen = states.get(entry_id)
        if seen is None:
            continue
        if newest is None or seen[1] > newest[2]:
            newest = (entry_id, seen[0], seen[1])
    return newest


def holder_role(holder, repo, number, *, agent_states, agent_tickets, agent_roles):
    """The registry role behind this claim holder, or None. Pure.

    The only reader of `agent_roles` by holder: it resolves the
    holder to its registry entry through `entry_for_claim` (by
    ticket, never by holder id) and reads that entry's role, so a
    unit-name-keyed role map is never keyed by an opaque drive id.
    Unknown holders and entries without a recorded role read as
    None, which every caller treats as not a reviewer.
    """
    entry = entry_for_claim(
        _holder_claim(holder, repo, number),
        agent_states=agent_states,
        agent_tickets=agent_tickets,
    )
    if entry is None:
        return None
    return (agent_roles or {}).get(entry[0])


def _finished_claims(store, agent_states, agent_tickets=None):
    """Managed claims with a finished registry entry. Pure reads.

    The scan `finished_holders` reads: each store row whose holder is
    a managed agent whose ticket has a finished entry, resolved through
    `entry_for_claim`. A missing state map reads as nothing finished.
    The scan runs through the shared retry policy; a persistent
    failure propagates for the caller to skip the step with every
    claim kept.
    """

    def scan():
        found = []
        for item in store.items():
            claim = store.claim_of(item.repo, item.number)
            if claim is None or not claim.agent or not is_managed_agent(claim.agent):
                continue
            seen = entry_for_claim(claim, agent_states=agent_states, agent_tickets=agent_tickets)
            if seen is None or seen[1] != registry.FINISHED:
                continue
            found.append(claim)
        return found

    return _retry_scan(scan)


def reviewed_claims(authors, finished, live_units=None) -> dict:
    """{(repo, number): agent} finished holders a reviewer already reviewed. Pure.

    `authors` is this pass's {(repo, number): worker id} capture and
    `finished` the newest finished entry per ticket: a ticket whose
    current holder matches a finished reviewer entry is merge-ready, so
    its row is free for a fix round. Whether the round opens is the
    gate's call: a finished reviewer with outcome `ok` and no review
    record skips as `review-missing` instead. A finished worker entry
    (or an unknown role, which reads as unreviewed work) keeps the
    claim for the reviewer takeover instead. Tickets in `live_units` are never
    freed: the real pass keeps a live unit's claim whatever the
    registry says, and the plan pass filters the same way so both
    passes gate those rows as still claimed. Missing maps read as
    nothing reviewed.
    """
    live = live_units if live_units is not None else frozenset()
    found = {}
    for ticket, agent in (authors or {}).items():
        if ticket in live:
            continue
        newest = (finished or {}).get(ticket)
        if newest is not None and newest[0] == agent and newest[1] == "reviewer":
            found[ticket] = agent
    return found


def release_reviewed(store, *, claims, live_units, now, out, events=None) -> dict:
    """Release reviewed claims, never touching a live unit. Returns the freed map.

    `claims` is {(repo, number): agent} from `reviewed_claims`: rows a
    reviewer already reviewed, free for a fix round. Each claim is
    re-read live first: a changed holder is left held, and a ticket
    with a live unit keeps its claim with a logbook line, whatever the
    registry says, so a reviewer still shutting down is never
    unclaimed. A successful release clears the holder and leaves the
    epoch for the next claim to mint. A `Claimed` there means the row
    changed under the pass, so it is left held with a logbook line,
    never retried in the same pass. A failed write is likewise left
    held with a logbook line while the rest of the sweep moves on, so
    one bad row never blocks the others. When `events` is a list, each
    release appends its journal entry. The returned {(repo, number): agent}
    names only the rows actually freed, so the gate can simulate the
    post-release rows the store reads still show as claimed (their
    queued mirror write drains on a later pass).
    """
    live = live_units if live_units is not None else frozenset()
    released = {}
    for (repo, number), agent in (claims or {}).items():
        ref = f"{repo}#{number}"
        if (repo, number) in live:
            print(f"{ref}: finished reviewer `{agent}` with a live unit, claim kept", file=out)
            continue
        try:
            current = store.claim_of(repo, number)
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"{ref}: reviewer claim unreadable ({problem}), left held", file=out)
            continue
        if current is None or current.agent != agent:
            print(f"{ref}: reviewer claim changed under the pass, left held", file=out)
            continue
        try:
            store.release(repo, number, agent, current.epoch, now=now)
        except board.Claimed:
            print(f"{ref}: reviewer release raced a re-claim, left held", file=out)
            continue
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"{ref}: finished release refused ({problem}), left held", file=out)
            continue
        print(
            f"{ref}: released finished reviewer `{agent}`, row open for a fix round",
            file=out,
        )
        if events is not None:
            events.append({"event": "release", "at": now, "ticket": ref, "agent": agent})
        released[(repo, number)] = agent
    return released


def finished_holders(store, *, agent_states, agent_tickets=None) -> dict:
    """{(repo, number): worker id} for finished managed holders. Pure reads.

    The pass captures the pre-takeover worker ids before any reviewer
    takes over, so a reviewer start in the same pass can pass the
    worker id as its author. Scans with `_finished_claims`, resolving
    each holder through `entry_for_claim`; a missing state map reads
    as nothing finished, so the map is empty.
    """
    return {
        (claim.repo, claim.number): claim.agent
        for claim in _finished_claims(store, agent_states, agent_tickets)
    }


def _capture_finished_holders(store, *, agent_states, agent_tickets=None, err) -> dict:
    """The pre-takeover worker ids for this pass, never raising.

    Both passes read this capture: the launching pass before any
    reviewer takes over, the plan pass to show the verdicts the real
    pass would print. A missing state map reads as nothing finished.
    A persistent store failure skips the capture with one log line and
    reads as nothing finished, so the pass continues with every claim
    kept instead of failing the pass.
    """
    if agent_states is None:
        return {}
    try:
        return finished_holders(store, agent_states=agent_states, agent_tickets=agent_tickets)
    except (board.StoreError, sqlite3.Error) as problem:
        print(f"korapy drive: finished holder capture skipped ({problem})", file=err)
        return {}


def _finished_outcome(value) -> str | None:
    """The outcome of one finished entry value, or None when unrecorded. Pure.

    Production values are (agent id, role, outcome) triples; an older
    injected (agent id, role) pair reads as no outcome, which never
    triggers the missing-record check.
    """
    if value is None or len(value) < 3:
        return None
    return value[2]


def _finished_role(value) -> str | None:
    """The role of one finished entry value, or None when unrecorded. Pure.

    Production values are (agent id, role, outcome) triples; an older
    injected (agent id, role) pair still carries its role, while a
    shorter value reads as no role, which never takes a role branch.
    """
    if value is None or len(value) < 2:
        return None
    return value[1]


def _finished_agent(value) -> str | None:
    """The agent id of one finished entry value, or None when unrecorded. Pure."""
    if value is None or len(value) < 1:
        return None
    return value[0]


def resolve_finished(finished_for, released, *, agent_states=None, agent_tickets=None) -> dict:
    """{(repo, number): (agent id, role, outcome)} newest finished entry. Pure.

    `finished_for` is the cross-pass map from finished registry entries
    ((agent id, role, outcome), `None` when the entry records none of
    them; an older (agent id, role) pair reads as no outcome);
    `released` is this pass's {(repo, number): worker id} capture. A
    same-pass capture always wins: it just happened, so it is the
    newest event. It keeps the cross-pass role and outcome only for
    the same ticket's entry: the cross-pass agent id is a unit name
    while the capture holds the opaque drive id, so raw ids never
    compare equal. The capture holder is resolved to its registry entry
    through the one resolver (`entry_for_claim`, by ticket) and the
    role and outcome carry over only when that entry is the cross-pass
    one; anything newer reads as unknown (`None`), which the gate
    reads as unreviewed work. Without a ticket index only raw ids
    compare, which keeps the same-namespace unit tests exact.
    """

    def carried(prior):
        role = prior[1] if len(prior) > 1 else None
        return role, _finished_outcome(prior)

    resolved = {}
    for ticket, prior in (finished_for or {}).items():
        role, outcome = carried(prior)
        resolved[ticket] = (prior[0], role, outcome)
    for ticket, agent in (released or {}).items():
        role, outcome = None, None
        prior = resolved.get(ticket)
        if prior is not None:
            if prior[0] == agent:
                role, outcome = prior[1], prior[2]
            else:
                entry = entry_for_claim(
                    _holder_claim(agent, ticket[0], ticket[1]),
                    agent_states=agent_states,
                    agent_tickets=agent_tickets,
                )
                if entry is not None and entry[0] == prior[0]:
                    role, outcome = prior[1], prior[2]
        resolved[ticket] = (agent, role, outcome)
    return resolved


def fix_mode(holder, *, repo, number, agent_states, live_units, agent_tickets=None) -> str:
    """`"resume"` when the holder is still usable, else `"fresh"`. Pure.

    A fix round resumes the holder's session when the ticket's registry
    entry (resolved through `entry_for_claim`: the holder id never
    names an entry) is live or stale, or a live unit exists for the
    ticket; either proves something usable survived. Anything else
    restarts fresh on the same ticket and branch. A fresh restart
    counts against the restart budget; a resume reuses the session and
    never does.
    """
    entry = entry_for_claim(
        _holder_claim(holder, repo, number),
        agent_states=agent_states,
        agent_tickets=agent_tickets,
    )
    if entry is not None and entry[1] in (registry.LIVE, registry.STALE):
        return "resume"
    if (repo, number) in (live_units or frozenset()):
        return "resume"
    return "fresh"


def reviewer_author_block(author) -> str:
    """The prompt block naming the change's author for the reviewer. Pure.

    The reviewer learns the pre-takeover worker id here and passes it as
    `review record --author`: the record refuses author==reviewer, so a
    reviewer that cannot name its author cannot record. A blank author
    raises ValueError before any launch.
    """
    worker = str(author or "").strip()
    if not worker:
        raise ValueError("a reviewer launch needs the pre-takeover worker id as author")
    return (
        f"\nThe change under review was written by agent `{worker}`. When you record "
        "your verdict (`korapy review record ... --author ...`), pass that id as "
        "`--author`: a record naming its own reviewer is refused. Never record a "
        "verdict with your own agent id as the author."
    )


def fix_message(repo, number) -> str:
    """The message a fix-round resume carries into the worker's session. Pure."""
    return (
        f"Address the review findings for {repo}#{number}: read the latest review "
        "record for this ticket, fix every finding it states, and push the branch "
        "so a later pass can schedule a re-review."
    )


def production_finished_for(
    registered, *, now, cfg=None, path=None, git=None, mapping=None
) -> dict:
    """{(repo, number): (agent id, role, outcome)} for the newest entry. Reads only.

    Built from the host registry: finished entries kept for RETAIN_S,
    newest first by end time, entries without a ticket skipped. Keys
    are store keys through the one short-to-tracker resolver, so the
    gate and the finished scans join them straight to claimed rows.
    `cfg` and `path` are the registry seams tests inject; production
    reads the host registry. `git` names the origin-lookup seam for
    entries without an explicit tracker repo. `mapping` is the pass's
    one short-to-tracker map (`production_repo_map`): every builder in
    one pass gets the same one, so a config edit between them cannot
    make two joins disagree; with none, the builder resolves its own.
    The role is `""` for entries that record none; the gate treats
    only a known reviewer role as reviewed. The outcome is the `done`
    outcome (`ok`, `failed` or `stopped`, `None` when the entry never
    recorded one); the missing-record check fires only on `ok`.
    """
    mapping = _resolved_map(registered, mapping, syncli._git if git is None else git)
    found: dict = {}
    for entry, state in registry.visible(now, cfg, path):
        if state != registry.FINISHED:
            continue
        ticket = registry.ticket_of(entry)
        if ticket is None:
            continue
        key = _tracker_key(ticket, mapping)
        at = entry.get("ended_at") or entry.get("last_seen") or 0
        prior = found.get(key)
        if prior is not None and prior[3] >= at:
            continue
        found[key] = (entry["id"], entry.get("role") or "", entry.get("outcome"), at)
    return {key: (agent, role, outcome) for key, (agent, role, outcome, _) in found.items()}


def cap_label(day_cap) -> str:
    """The cap suffix for day-cap lines. Pure.

    Names the cap as `(N/day)`; with no cap configured it reads
    `(day cap)`, so a line never prints `None`.
    """
    return f"({day_cap}/day)" if day_cap is not None else "(day cap)"


def restart_refusal(item, *, read_labels, hold_label, requested, linked=()):
    """Why a stuck row must not restart: (reason, keep) or None. Pure reads.

    The row-state subset of the section 3 gate, re-checked at restart
    time through the same `hold.reason` resolver the gate uses: a
    non-open or externally owned row, a held row, a row waiting on a
    human, or a row no project requested this pass is not restarted.
    `requested` is the set of (repo, number) any project requested this
    pass, or None when unknown (then the withdrawn check is skipped).
    `linked` holds the (repo, number) pairs of the pull requests the
    restart acts on, threaded into the hold re-check the way the gate
    threads the row's linked pulls. `read_labels` is the injected label
    seam and is required: with no reader the hold cannot be re-checked,
    so the restart is deferred with the claim kept. `hold.reason` fails
    closed on an unreadable read (the claim is kept) and names a hold
    otherwise (the claim is released); the restart wording appended
    here keeps every refusal greppable for "not restarted".
    """
    ref = f"{item.repo}#{item.number}"
    if item.state != "open":
        return f"{ref}: state is {item.state}, not open, not restarted", False
    if item.external:
        return f"{ref}: externally owned, not restarted", False
    if read_labels is None:
        return (
            f"{ref}: hold labels unreadable (no label reader), restart deferred, not restarted",
            True,
        )
    held = hold.reason(read_labels, (item.repo, item.number), linked, hold_label=hold_label)
    if held is not None:
        if "unreadable" in held:
            return f"{held}, restart deferred, not restarted", True
        return f"{held}, not restarted", False
    if iface.NEEDS_DECISION in (item.labels or ()):
        return f"{ref}: carries {iface.NEEDS_DECISION}, waiting on a human, not restarted", False
    if requested is not None and (item.repo, item.number) not in requested:
        return f"{ref}: withdrawn by this pass's eligibility, not restarted", False
    return None


def handle_stuck(
    store,
    *,
    agent_states,
    stale_s,
    gone_s,
    live_units,
    now,
    host,
    stuck_project,
    stop_unit,
    restarter,
    tracker_factory,
    restarts_path,
    out,
    err,
    events=None,
    start_budget=None,
    day_cap=None,
    hub_epoch_of=None,
    read_labels=None,
    hold_label=hold.HOLD_LABEL,
    requested=None,
    agent_tickets=None,
):
    """Restart or give up on stuck managed claims. Returns (restarted, capped).

    Only managed holders older than this pass are considered: the pass
    never restarts, watches or gives up on what it just claimed, and
    foreign claims are never touched. A holder with no registry entry
    but a live unit only watches, never restarts. Before any budget is
    spent, each restart verdict re-checks row eligibility
    (`restart_refusal` over the row state, the ticket and linked pull
    hold labels and the needs-decision label, plus the `requested` set
    for withdrawals): an
    ineligible row is not
    restarted and its claim is released (the epoch is kept, so the next
    claim fences correctly), except unreadable labels, which fail
    closed with the claim kept, and except rows with a live unit, which
    keep their claim with a logbook line whatever the eligibility says.
    A ticket with a finished registry entry (from `entry_for_claim`,
    by ticket with `agent_tickets`, never by holder id) is never
    restarted: the claim is kept for the reviewer takeover. Finished
    rows still enter the sweep, so a withdrawn or held finished row
    with a dead unit releases through the eligibility re-check above.
    In hub mode `hub_epoch_of` maps
    (repo, number) to the hub row's fencing epoch (None when the hub
    shows no row); a local epoch the hub superseded skips instead of
    restarting, and an unreadable hub epoch skips without stopping, so
    the pass never stop-plus-launches work it no longer owns. With no
    seam the sweep behaves as today (single-host). A stuck row refreshes its claim
    heartbeat on every restart attempt (success or failure) before any
    stop or launch, so the orphan rule defers to stuck handling and a
    failing ticket can no longer escape into a fresh agent id. The
    budget stamp is written before the stop-plus-launch and rolled back
    when the stop or launch fails, so a failed stop or launch leaves
    the budget alone; a crash between the write and the stop can still
    leave a stamp for a restart that never launched. A stamp the pass
    cannot persist aborts the restart and keeps the claim. While the
    budget allows it the row
    restarts (stop through the stop seam, launch again on the same
    claim and epoch reusing the identical agent id); past the budget
    the loop posts through the decision-write seam, then releases,
    except rows with a live unit, which keep their claim with a
    logbook line. `tracker_factory` maps a tracker repo to its tracker (labels or
    board, built by the one factory): the give-up decision writes use
    the tracker the factory returns for the claim's repo. With no
    factory, or for a repo the factory does not know, the give-up is
    deferred and the claim is kept. A failed decision write keeps the
    claim for a later pass, and only stop-plus-launch restarts (plus
    fresh fix rounds in the claim path) are recorded, so resumes never
    consume the budget. `start_budget` is the day's remaining starts
    (None means no day cap): each stop-plus-launch restart spends one.
    The day cap gates after the restart budget, the project gate and
    the seam gates: a row past its restart budget still gives up and
    releases at the cap, and only a row that would actually have
    restarted is left held with a day-cap line. `day_cap` names the
    cap in that line (`cap_label` renders it when it is None).
    `capped` is True when at least one would-be restart was refused
    by the day cap. When `events` is a
    list, each restart and give-up appends its journal entry through
    the pass journal writer. The loop-head listing retries transient
    sqlite locks and a single unreadable claim is left held with a
    logbook line while the rest of the sweep moves on; anything else
    store-shaped propagates to the caller, like orphans.
    """
    live = live_units if live_units is not None else frozenset()
    budgets = load_restarts(restarts_path) if restarts_path is not None else {}
    restarted = 0
    capped = False
    for item in _stuck_rows(store):
        try:
            claim = store.claim_of(item.repo, item.number)
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"{item.repo}#{item.number}: claim unreadable ({problem}), left held", file=out)
            continue
        if claim is None or not claim.agent or not is_managed_agent(claim.agent):
            continue
        if claim.claimed_at >= now:
            continue
        ref = f"{claim.repo}#{claim.number}"
        if hub_epoch_of is not None:
            try:
                hub_epoch = hub_epoch_of(claim.repo, claim.number)
            except Exception as problem:
                print(f"{ref}: hub epoch unreadable ({problem}), restart skipped", file=out)
                continue
            if hub_epoch is not None and hub_epoch != claim.epoch:
                print(
                    f"{ref}: hub epoch {hub_epoch} supersedes local epoch {claim.epoch},"
                    " restart skipped",
                    file=out,
                )
                continue
        seen = entry_for_claim(claim, agent_states=agent_states, agent_tickets=agent_tickets)
        if seen is None or not isinstance(seen[2], (int, float)):
            known, age = False, float("inf")
            observed = "no registry entry for the holder"
        else:
            known, age = True, now - seen[2]
            observed = f"last heartbeat {int(age)}s ago"
        unit_live = (claim.repo, claim.number) in live
        observed += " with a live unit" if unit_live else " and no live unit"
        verdict = classify_stuck(
            known=known, age=age, unit_live=unit_live, stale_s=stale_s, gone_s=gone_s
        )
        if verdict == "healthy":
            continue
        if verdict == "watch":
            print(f"{ref}: {observed}, watching once more", file=out)
            continue
        refusal = restart_refusal(
            item,
            read_labels=read_labels,
            hold_label=hold_label,
            requested=requested,
            linked=[(item.repo, pull.number) for pull in getattr(item, "pulls", None) or ()],
        )
        if refusal is not None:
            reason, keep = refusal
            if keep:
                print(f"{reason}, claim kept", file=out)
                continue
            if (claim.repo, claim.number) in live:
                print(f"{reason}, live unit, claim kept", file=out)
                continue
            try:
                store.release(claim.repo, claim.number, claim.agent, claim.epoch, now=now)
            except board.Claimed:
                print(f"{reason}, release raced a re-claim, left held", file=out)
                continue
            except (board.StoreError, sqlite3.Error) as problem:
                print(f"{reason}, release refused ({problem}), left held", file=out)
                continue
            print(f"{reason}, claim released", file=out)
            if events is not None:
                events.append({"event": "release", "at": now, "ticket": ref, "agent": claim.agent})
            continue
        if seen is not None and seen[1] == registry.FINISHED:
            print(f"{ref}: worker `{claim.agent}` finished, not stuck, claim kept", file=out)
            continue
        key = f"{claim.repo}#{claim.number}"
        stamps = budgets.get(key, [])
        if not restart_allowed(stamps, now=now):
            if (claim.repo, claim.number) in live:
                print(f"{ref}: past the restart budget, live unit, claim kept", file=out)
                continue
            tracker = None
            if tracker_factory is not None:
                try:
                    tracker = tracker_factory(claim.repo)
                except KeyError:
                    print(
                        f"{ref}: no tracker for this repo, give-up deferred, claim kept",
                        file=out,
                    )
                    continue
            give_up(
                store,
                claim,
                tracker,
                observed=observed,
                restarts=len(prune_restarts(stamps, now=now)),
                now=now,
                out=out,
                events=events,
            )
            continue
        project = stuck_project(claim.repo, claim.number) if stuck_project is not None else None
        if project is None:
            print(f"{ref}: stuck but the owning project is unknown, left held", file=out)
            continue
        launch = restarter
        if stop_unit is None or launch is None:
            print(f"{ref}: stuck but the stop or launch seam is missing, left held", file=out)
            continue
        if start_budget is not None and start_budget - restarted <= 0:
            print(f"{ref}: day cap reached {cap_label(day_cap)}, not restarted", file=out)
            capped = True
            continue
        try:
            claim = store.refresh_claim(claim.repo, claim.number, claim.agent, claim.epoch, now=now)
        except board.Claimed:
            print(f"{ref}: restart raced a re-claim, left held", file=out)
            continue
        except (board.ExternalItem, board.StoreError, sqlite3.Error) as problem:
            print(f"{ref}: restart claim refused ({problem}), left held", file=out)
            continue
        had = key in budgets
        stamped = record_restart(stamps, now=now)
        prior_text = None
        prior_exists = False
        if restarts_path is not None:
            try:
                with open(restarts_path, encoding="utf-8") as handle:
                    prior_text = handle.read()
                prior_exists = True
            except (OSError, UnicodeError):
                prior_text = None
                prior_exists = False
        budgets[key] = stamped
        if restarts_path is not None:
            try:
                save_restarts(restarts_path, budgets)
            except OSError as problem:
                if had:
                    budgets[key] = stamps
                else:
                    budgets.pop(key, None)
                print(f"korapy drive: cannot save the restart budget: {problem}", file=err)
                print(f"{ref}: restart budget save failed ({problem}), claim kept", file=out)
                continue

        def unrecord():
            if had:
                budgets[key] = stamps
            else:
                budgets.pop(key, None)
            if restarts_path is None:
                return
            try:
                save_restarts(restarts_path, budgets)
                return
            except OSError as problem:
                print(f"korapy drive: cannot save the restart budget: {problem}", file=err)
            try:
                if prior_exists and prior_text is not None:
                    paths.atomic_write_text(restarts_path, prior_text)
                else:
                    os.remove(restarts_path)
            except OSError as problem:
                print(f"korapy drive: cannot restore the restart budget: {problem}", file=err)
                print(f"{ref}: restart budget rollback failed, the stamp stands", file=out)
                budgets[key] = stamped

        try:
            stop_unit(claim.repo, claim.number)
        except Exception as problem:
            unrecord()
            print(f"{ref}: restart stop failed ({problem}), claim kept", file=out)
            continue
        title = item.title if item.title else ref
        try:
            unit = launch(project, claim.repo, claim.number, "start", claim.agent, title, now=now)
        except Exception as problem:
            unrecord()
            print(f"{ref}: restart launch failed ({problem}), claim kept", file=out)
            continue
        print(
            f"{ref}: restarted {unit} with the same agent id"
            f" (restart {len(budgets[key])} of {RESTART_BUDGET} in 24h);"
            " to keep it stopped, use `korapy stop`, never stop the unit directly",
            file=out,
        )
        if events is not None:
            events.append({"event": "restart", "at": now, "ticket": ref, "agent": claim.agent})
        restarted += 1
    return restarted, capped


def give_up(store, claim, tracker, *, observed, restarts, now, out, events=None):
    """Post the give-up label and comment, then release the claim.

    The idempotent label goes first, then the comment: a comment
    failure keeps the claim with the label already in place, so the
    retry cannot duplicate a comment that was never posted, and a
    label failure posts nothing at all. A failed decision write or a
    lost release race keeps the claim for a later pass. With no tracker
    the give-up is deferred, never released blind. When `events` is a
    list, a completed give-up appends its journal entry. One row only;
    the caller decides which rows give up.
    """
    ref = f"{claim.repo}#{claim.number}"
    if tracker is None:
        print(
            f"{ref}: past the restart budget, give-up deferred (no tracker), claim kept", file=out
        )
        return
    try:
        tracker.add_label(claim.repo, claim.number, iface.NEEDS_DECISION)
        tracker.comment(
            claim.repo,
            claim.number,
            give_up_body(claim.repo, claim.number, observed=observed, restarts=restarts),
        )
    except iface.TrackerError as problem:
        print(f"{ref}: give-up write failed ({problem}), claim kept", file=out)
        return
    try:
        store.release(claim.repo, claim.number, claim.agent, claim.epoch, now=now)
    except board.Claimed:
        print(f"{ref}: give-up release raced a re-claim, left held", file=out)
        return
    print(f"{ref}: past the restart budget, gave up and released the claim", file=out)
    if events is not None:
        events.append({"event": "give_up", "at": now, "ticket": ref, "agent": claim.agent})


def production_repo_map(registered, *, git=None, resolved=None):
    """`{short [[repo]] name: tracker repo}` across registered roots.

    The one resolver for the tracker repo behind a registry entry's
    short ticket repo: each root resolves through the shared
    `syncli.root_short_map`, and the first project naming a short
    name wins.
    A configuration problem raises SettingsError, so the pass refuses
    with exit 4 instead of joining against a guess. `resolved` maps
    each registered root to its already-resolved `{name: (tracker
    repo, operators)}` entries: with it the pass shares its one
    resolution instead of resolving again (every root in `registered`
    must have an entry); with none, the builder resolves its own. The
    CLI builds it once per pass and threads it into the admission live
    set, the stuck, finished and orphan builders and `run_pass`, so a
    `korapy.toml` edit between two of them cannot make two joins
    disagree, and each repo lacking an explicit `tracker.repo` shells
    git once per pass. `git` names the origin-lookup seam; None is the
    real runner. A builder given no map builds its own, which is the
    seam tests fake.
    """
    git = syncli._git if git is None else git
    found = {}
    for project in registered:
        if resolved is None:
            shorts = syncli.root_short_map(project.root, git)
        else:
            shorts = {
                name: tracker_repo for name, (tracker_repo, _ops) in resolved[project.root].items()
            }
        for name, tracker_repo in shorts.items():
            found.setdefault(name, tracker_repo)
    return found


def _resolved_map(registered, mapping, git):
    """The pass's map when given, else one built from `git`. The test seam."""
    if mapping is not None:
        return mapping
    return production_repo_map(registered, git=git)


def _tracker_key(ticket, mapping):
    """The store key `(tracker repo, number)` for one registry ticket. Pure.

    A ticket the mapping knows takes its tracker repo; an unknown
    short name keeps itself, so entries from a removed project behave
    exactly as before (they never match a store row either way). Every
    registry-to-store join builds its keys through here, never a raw
    `(ticket, (claim.repo, claim.number))` comparison elsewhere.
    """
    repo, number = ticket
    return (mapping.get(repo, repo), number)


def production_stuck_inputs(registered, *, now, path=None, git=None, mapping=None):
    """`(agent_states, stale_s, agent_tickets)` from the registry for the stuck sweep.

    `agent_states` maps every visible agent id to (state, last_seen);
    an id with no entry reads as gone. `agent_tickets` maps every
    visible agent id naming a ticket to its store key: the ticket
    index `entry_for_claim` resolves holders through, since a claim's
    holder id never names its unit-name entry. Keys go through the
    one short-to-tracker resolver, so a short registry repo never
    misses its tracker-repo claim. `path` is the registry dir seam
    tests inject; production reads the host registry. `git` names the
    origin-lookup seam for entries without an explicit tracker repo;
    tests pass a fake, production the real runner. `mapping` is the
    pass's one short-to-tracker map; with none, the builder resolves
    its own. Raises SettingsError for bad registry or project settings.
    """
    cfg = registry.config()
    mapping = _resolved_map(registered, mapping, syncli._git if git is None else git)
    states = {}
    tickets = {}
    for entry, state in registry.visible(now, cfg, path):
        last = entry.get("last_seen") or entry.get("started_at") or 0
        states[entry["id"]] = (state, float(last))
        ticket = registry.ticket_of(entry)
        if ticket is not None:
            tickets[entry["id"]] = _tracker_key(ticket, mapping)
    return states, cfg["stale_s"], tickets


def production_agent_roles(*, now):
    """`{agent id: role}` for visible registry entries that record one.

    Reads only. The claim path consults it to serialize a fix behind a
    reviewer holder; entries without a role read as unknown and never
    block a fix. Raises SettingsError for bad registry settings.
    """
    cfg = registry.config()
    return {
        entry["id"]: entry["role"]
        for entry, _state in registry.visible(now, cfg)
        if entry.get("role")
    }


def production_stuck_project(registered, repo, *, mapping=None, git=None):
    """The registered project name owning tracker repo `repo`, or None.

    The spelling resolves once through `reviewrecord.local_repo` over
    the pass map (the same map the stuck, orphan and finished builders
    join against), so the stop path and the joins never disagree; the
    owner is then the first project whose root feeds that spelling.
    The pass map cannot name the owning root, so the stop path keeps
    its own per-root owner lookup: only an exact tracker-repo feed
    names a project, and a short or otherwise unmapped spelling names
    none, instead of stopping a stranger's units under a guessed
    spelling. A key the map cannot translate falls back to the
    per-root scan, so a tracker repo shadowed out of the pass map by
    an earlier project sharing its short name still stops under its
    unambiguous owner. `mapping` is the pass's one short-to-tracker
    map; with none, the builder resolves its own. `git` names the
    origin-lookup seam for entries without an explicit tracker repo;
    None is the real runner. Pure settings reads, no subprocess when
    every entry names its tracker repo explicitly.
    """
    runner = syncli._git if git is None else git
    mapping = _resolved_map(registered, mapping, runner)
    try:
        short = reviewrecord.local_repo(repo, mapping)
    except ValueError:
        for project in registered:
            try:
                _local_repo(project.root, repo, git=runner)
            except launch.LaunchError:
                continue
            return project.name
        return None
    for project in registered:
        try:
            root_map = syncli.root_short_map(project.root, runner)
        except settings.SettingsError:
            continue
        if root_map.get(short) == repo:
            return project.name
    return None


def production_stopper(registered, *, mapping=None, git=None):
    """The stuck step's stop seam over `launch.stop` with production I/O.

    Resolves the owning project per ticket over the pass map, then
    stops the ticket's logged live units. Every argument the stop path
    needs is fixed here; tests inject a fake instead. `mapping` is the
    pass's one short-to-tracker map; with none, each stop resolves its
    own. `git` names the origin-lookup seam; None is the real runner.
    """
    runner = syncli._git if git is None else git
    listings = lambda prefix: units.list_units(prefix, run=units.default_run)

    def stop_unit(repo, number):
        name = production_stuck_project(registered, repo, mapping=mapping, git=runner)
        if name is None:
            raise units.UnitsError(f"no registered project feeds tracker repo {repo!r}")
        entry = next(item for item in registered if item.name == name)
        local = _local_repo(entry.root, repo, git=runner)
        tgt = launch.target(entry.root, local)
        return launch.stop(tgt, number, list_fn=listings, stop_fn=units.systemctl_stop)

    return stop_unit


def _skip_as(gated, kind, reason, code):
    """The skip verdict for a gated row under an effective kind."""
    return Gated(
        project=gated.project,
        repo=gated.repo,
        number=gated.number,
        kind=kind,
        verdict="skip",
        reason=reason,
        code=code,
    )


def _refresh_title(store, gated, *, tracker_factory, operators, now, stale_after, kind=None):
    """The board title to launch under, or the skip row telling why not.

    Refreshes the stale row live first, failing closed when it cannot
    be re-read: a claim path never decides from a stale row. `kind`
    overrides the reported kind for effective-kind rows (a resume that
    restarts reports as a fix).
    """
    title_kind = kind if kind is not None else gated.kind
    fresh, problem = refresh_row(
        store,
        tracker_factory,
        gated.repo,
        gated.number,
        operators=operators,
        now=now,
        stale_after=stale_after,
    )
    if problem is not None:
        return None, _skip_as(gated, title_kind, problem, "claim")
    ref = f"{gated.repo}#{gated.number}"
    title = fresh.title if fresh is not None and fresh.title else ref
    return title, None


def _mint_claim(store, gated, *, host, now, kind=None, hub=None):
    """The fresh id claimed for a gated row, or the skip row telling why not.

    Mints with `mint_agent` and claims it. In hub mode the hub claim
    comes first and the local store adopts the receipt's epoch instead
    of minting, so there is still one minter; a hub conflict skips as
    a lost race naming the holder, and a dead hub skips as hub
    unreachable. A lost local race names the holder; any other refusal
    names itself. When the hub granted first and the local claim then
    loses the race, the just-granted hub row is released best-effort
    with the receipt's agent and epoch, so the next pass cannot fence
    the innocent local holder; a local refusal after a grant releases
    the same way. `kind` overrides the reported kind for
    effective-kind rows.
    """
    title_kind = kind if kind is not None else gated.kind
    ref = f"{gated.repo}#{gated.number}"
    agent = mint_agent(gated.repo, gated.number, host=host, now=now)
    adopted = None
    if hub is not None:
        try:
            receipt = hub.propose(gated.repo, gated.number, agent)
        except HubGone as gone:
            return None, _skip_as(
                gated, title_kind, f"{ref}: hub unreachable, the pass moves on ({gone})", "hub"
            )
        except HubConflict as err:
            holder = err.holder.get("agent", "?")
            return None, _skip_as(
                gated,
                title_kind,
                f"{ref}: race lost to `{holder}`, the pass moves on",
                "claim",
            )
        adopted = receipt.epoch
    try:
        if adopted is None:
            store.claim(gated.repo, gated.number, agent, host, now=now)
        else:
            store.claim(gated.repo, gated.number, agent, host, now=now, epoch=adopted)
    except board.Claimed:
        if hub is not None and adopted is not None:
            try:
                hub.release(gated.repo, gated.number, agent, adopted)
            except (HubGone, HubConflict, HubConfigError):
                pass
        try:
            current, _ = store.item(gated.repo, gated.number, now=now)
        except (board.StoreError, sqlite3.Error) as err:
            return None, _skip_as(gated, title_kind, f"{ref}: claim refused ({err})", "claim")
        seen = current.agent if current is not None else ""
        return None, _skip_as(
            gated, title_kind, f"{ref}: race lost to `{seen}`, the pass moves on", "claim"
        )
    except (board.ExternalItem, board.StoreError, sqlite3.Error) as err:
        if hub is not None and adopted is not None:
            try:
                hub.release(gated.repo, gated.number, agent, adopted)
            except (HubGone, HubConflict, HubConfigError):
                pass
        return None, _skip_as(gated, title_kind, f"{ref}: claim refused ({err})", "claim")
    return agent, None


def _fresh_claim(store, gated, *, tracker_factory, operators, host, now, stale_after, hub=None):
    """The (agent, title) claimed for a gated row, or the skip row telling why not.

    Refreshes the stale row live first (failing closed when it cannot
    be re-read), then mints a fresh id and claims it. A fix round that
    replaces its holder refreshes first, releases, and only then mints,
    so it never claims against its own held row. In hub mode the fresh
    id is proposed to the hub first (see `_mint_claim`).
    """
    title, skipped = _refresh_title(
        store,
        gated,
        tracker_factory=tracker_factory,
        operators=operators,
        now=now,
        stale_after=stale_after,
    )
    if skipped is not None:
        return None, None, skipped
    agent, skipped = _mint_claim(store, gated, host=host, now=now, hub=hub)
    if skipped is not None:
        return None, None, skipped
    return agent, title, None


def _release_own_claim(store, repo, number, agent, *, now, role) -> str:
    """The `; ...` suffix releasing the claim this pass minted.

    A claim minted seconds ago that no launch will use must not strand:
    a claimed row is skipped by the gate until the stuck or orphan
    sweeps reclaim it. Releases only the claim this pass minted (a
    matching agent) through the existing release path, which keeps the
    epoch so the next claim fences correctly. Anything else is kept
    with a suffix saying so: a failed release never raises,
    fail-closed.
    """
    try:
        current = store.claim_of(repo, number)
    except (board.StoreError, sqlite3.Error) as err:
        return f"; {role} release unreadable ({err}), claim kept"
    if current is None:
        return f"; {role} claim already gone, row open for a later pass"
    if current.agent != agent:
        return f"; {role} claim changed under the pass, left held"
    try:
        store.release(repo, number, agent, current.epoch, now=now)
    except board.Claimed:
        return f"; {role} release raced a re-claim, left held"
    except (board.ExternalItem, board.StoreError, sqlite3.Error) as err:
        return f"; {role} release refused ({err}), claim kept"
    return f"; {role} claim released, row open for a later pass"


def _release_reviewer_claim(store, repo, number, agent, *, now) -> str:
    """The `; ...` suffix releasing our reviewer claim after a failed launch.

    A reviewer launch that never started must not strand its claim: the
    stuck sweep would later restart it as a worker under the reviewer
    id. Releases only the claim this pass minted (a matching agent); the
    finished entry behind the review persists, so a later pass schedules
    it again. Anything else is kept with a suffix saying so: a failed
    release never raises, fail-closed.
    """
    return _release_own_claim(store, repo, number, agent, now=now, role="reviewer")


def _start_unit(launcher, *, project, repo, number, kind, agent, title, now, author=None):
    """`(unit, note, code, blocked, race)` for one launch-handoff attempt.

    `unit` is None when the launch failed; `note` appends to the gate
    reason, `code` is `""` on success else `"launch"`, `blocked` is
    True only when the service manager was unreachable, and `race` is
    True only when a live unit already holds the ticket (a lost race,
    not a refusal: another party may own the ticket, so the caller
    keeps the claim). Every launcher call carries the author (None
    except for reviewers), so the seam stays one shape for every kind.
    """
    try:
        unit = launcher(project, repo, number, kind, agent, title, now=now, author=author)
    except units.UnitExists as err:
        return None, f"race lost, already live ({err})", "launch", False, True
    except units.Unreachable as err:
        return None, f"not started: {err}", "launch", True, False
    except Exception as err:
        return None, f"not started: {err}", "launch", False, False
    return unit, f"started {unit}", "", False, False


def _claim_and_launch(
    store,
    gated_rows,
    *,
    tracker_factory,
    operators,
    launcher,
    host,
    now,
    stale_after,
    start_budget=None,
    day_cap=None,
    released=None,
    finished=None,
    agent_states=None,
    agent_tickets=None,
    live_units=None,
    restarts_path=None,
    agent_roles=None,
    out=None,
    err=None,
    events=None,
    hub=None,
):
    """Claim each proceeding candidate, launch each claim.

    Returns (rows, started, blocked, launched): the verdict rows to
    print, the start launches, whether a launch failed because the
    service manager was unreachable, and the (project, repo, number) of
    every row the launcher actually launched. `start` claims a fresh id
    with `mint_agent` and launches a worker; `review` takes over the
    finished worker's claim (release, then a fresh reviewer id) and
    launches a reviewer with the pre-takeover worker id
    as its author, releasing the claim again when the launch never
    starts so the stuck sweep cannot restart it as a worker; `resume`
    reuses the holder's claim (refreshed, so
    the stuck sweep defers to it) and relaunches its session; `fix`
    resumes when the holder is still usable and otherwise releases and
    reclaims before restarting on the same branch, which the launch
    path reuses. A fix or resume behind a reviewer holder waits: when the
    current holder's role (from `agent_roles`, resolved to its
    registry entry by ticket through the one resolver) is a reviewer,
    the row is skipped with the claim kept, and the fix is scheduled
    on a later pass after the reviewer finishes and is released. A fresh
    restart first gates on the day cap, then on the restart budget:
    past the budget the row routes to the give-up path exactly as the
    stuck sweep does (decision writes through `tracker_factory`, then
    release; a failed write or a missing tracker keeps the claim). A
    fresh restart that fits the budget persists one stamp before it
    releases, reclaims or launches anything, exactly as the stuck
    sweep does: a stamp the pass cannot persist skips the row with
    the claim kept and never launches, and a release, reclaim or
    launch failure rolls the stamp back, so only launches consume the
    budget. A resume reuses the session and never touches either
    budget. `restarts_path` persists those stamps (in memory when
    None); `out`, `err` and `events` carry the give-up lines,
    budget warnings and journal entries. A lost race (exit 2 from the
    store) becomes a skip naming the holder and the pass moves on; a
    launch through `launcher` appends the unit or the failure to the
    row's reason. A failed fresh `start` launch releases the fresh
    claim in the same pass (except a lost race to a live unit, which
    keeps it) and appends the release to the row's reason, so the
    ticket is pickable again with the epoch kept. `released`
    ({(repo, number): worker id} captured
    this pass before any reviewer takes over) and `finished` (the
    newest finished entry per ticket)
    feed the reviewer author. With no `launcher` the pass stops after
    claiming and records that, releasing a reviewer claim again like a
    failed launch, so no row counts as launched. The day
    cap counts `start`, `review` and fresh `fix` launches: each one
    starts a new agent. A `resume` reuses the holder's claim and stays
    exempt. When `start_budget` is not None, at most that many launch;
    further ones become `cap` skips naming `day_cap`, and each launch
    spends one from the local budget. In hub mode (`hub` given) every
    fresh claim is proposed to the hub first and the local store
    adopts the receipt's epoch; a dead hub skips every claim-needing
    row with code `"hub"` while resumes proceed on their local claims.
    """
    rows: list = []
    started = 0
    blocked = False
    launched: set = set()
    budgets = load_restarts(restarts_path) if restarts_path is not None else {}
    stream = out if out is not None else io.StringIO()
    trouble = err if err is not None else io.StringIO()
    for gated in gated_rows:
        if gated.verdict == "skip":
            rows.append(gated)
            continue
        ref = f"{gated.repo}#{gated.number}"
        key = (gated.repo, gated.number)
        if gated.kind == "review":
            author = (released or {}).get(key)
            if author is None:
                newest = (finished or {}).get(key)
                author = _finished_agent(newest)
            if not author:
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=f"{ref}: finished worker id lost, review skipped",
                        code="claim",
                    )
                )
                continue
            if start_budget is not None and start_budget <= 0:
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=f"{ref}: day cap reached {cap_label(day_cap)}, not started",
                        code="cap",
                    )
                )
                continue
            try:
                taking = store.claim_of(gated.repo, gated.number)
            except (board.StoreError, sqlite3.Error) as err:
                rows.append(
                    _skip_as(
                        gated, "review", f"{ref}: claim unreadable, review refused ({err})", "claim"
                    )
                )
                continue
            holder_now = taking.agent if taking is not None else ""
            holder_epoch = taking.epoch if taking is not None else 0
            if holder_now and holder_now != author:
                rows.append(
                    _skip_as(
                        gated, "review", f"{ref}: claim changed under the pass, left held", "claim"
                    )
                )
                continue
            if holder_now and launcher is None:
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=f"{gated.reason}; launch skipped (no launcher); worker claim kept",
                        code="launch",
                    )
                )
                continue
            title, skipped = _refresh_title(
                store,
                gated,
                tracker_factory=tracker_factory,
                operators=operators,
                now=now,
                stale_after=stale_after,
            )
            if skipped is not None:
                rows.append(skipped)
                continue
            if holder_now:
                try:
                    store.release(gated.repo, gated.number, holder_now, holder_epoch, now=now)
                except board.Claimed:
                    rows.append(
                        _skip_as(
                            gated,
                            "review",
                            f"{ref}: reviewer takeover raced a re-claim, left held",
                            "claim",
                        )
                    )
                    continue
                except (board.ExternalItem, board.StoreError, sqlite3.Error) as err:
                    rows.append(
                        _skip_as(
                            gated,
                            "review",
                            f"{ref}: reviewer takeover refused ({err}), left held",
                            "claim",
                        )
                    )
                    continue
                if events is not None:
                    events.append(
                        {"event": "release", "at": now, "ticket": ref, "agent": holder_now}
                    )
            agent, skipped = _mint_claim(store, gated, host=host, now=now, hub=hub)
            if skipped is not None:
                rows.append(skipped)
                continue
            if launcher is None:
                suffix = _release_reviewer_claim(store, gated.repo, gated.number, agent, now=now)
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=f"{gated.reason}; launch skipped (no launcher){suffix}",
                        code="launch",
                    )
                )
                continue
            unit, note, code, unreachable, _ = _start_unit(
                launcher,
                project=gated.project,
                repo=gated.repo,
                number=gated.number,
                kind="review",
                agent=agent,
                title=title,
                now=now,
                author=author,
            )
            blocked = blocked or unreachable
            if unit is None:
                suffix = _release_reviewer_claim(store, gated.repo, gated.number, agent, now=now)
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=f"{gated.reason}; {note}{suffix}",
                        code=code,
                    )
                )
                continue
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict=gated.kind,
                    reason=f"{gated.reason}; {note}",
                    code="",
                )
            )
            launched.add((gated.project, gated.repo, gated.number))
            started += 1
            if start_budget is not None:
                start_budget -= 1
            continue
        if gated.kind in ("resume", "fix"):
            try:
                claim = store.claim_of(gated.repo, gated.number)
            except (board.StoreError, sqlite3.Error) as err:
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=f"{ref}: claim unreadable, fix refused ({err})",
                        code="claim",
                    )
                )
                continue
            holder = claim.agent if claim is not None else ""
            if not holder or not is_managed_agent(holder):
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=f"{ref}: claim changed under the pass, left held",
                        code="claim",
                    )
                )
                continue
            if (
                holder_role(
                    holder,
                    gated.repo,
                    gated.number,
                    agent_states=agent_states,
                    agent_tickets=agent_tickets,
                    agent_roles=agent_roles,
                )
                == "reviewer"
            ):
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=gated.kind,
                        verdict="skip",
                        reason=(
                            f"{ref}: reviewer `{holder}` still holds the claim, "
                            "fix waits for release"
                        ),
                        code="claim",
                    )
                )
                continue
            mode = fix_mode(
                holder,
                repo=gated.repo,
                number=gated.number,
                agent_states=agent_states,
                live_units=live_units,
                agent_tickets=agent_tickets,
            )
            effective = "resume" if mode == "resume" else "fix"
            key = f"{gated.repo}#{gated.number}"
            stamps = budgets.get(key, [])
            unrecord = None
            if mode == "resume":
                try:
                    store.refresh_claim(gated.repo, gated.number, holder, claim.epoch, now=now)
                except board.Claimed:
                    rows.append(
                        Gated(
                            project=gated.project,
                            repo=gated.repo,
                            number=gated.number,
                            kind=effective,
                            verdict="skip",
                            reason=f"{ref}: resume raced a re-claim, left held",
                            code="claim",
                        )
                    )
                    continue
                except (board.ExternalItem, board.StoreError, sqlite3.Error) as err:
                    rows.append(
                        Gated(
                            project=gated.project,
                            repo=gated.repo,
                            number=gated.number,
                            kind=effective,
                            verdict="skip",
                            reason=f"{ref}: resume claim refused ({err}), left held",
                            code="claim",
                        )
                    )
                    continue
                try:
                    current, _ = store.item(gated.repo, gated.number, now=now)
                except (board.StoreError, sqlite3.Error):
                    current = None
                title = current.title if current is not None and current.title else ref
                agent = holder
            else:
                if start_budget is not None and start_budget <= 0:
                    rows.append(
                        Gated(
                            project=gated.project,
                            repo=gated.repo,
                            number=gated.number,
                            kind=effective,
                            verdict="skip",
                            reason=f"{ref}: day cap reached {cap_label(day_cap)}, not started",
                            code="cap",
                        )
                    )
                    continue
                if not restart_allowed(stamps, now=now):
                    tracker = None
                    if tracker_factory is not None:
                        try:
                            tracker = tracker_factory(gated.repo)
                        except KeyError:
                            print(
                                f"{ref}: no tracker for this repo, give-up deferred, claim kept",
                                file=stream,
                            )
                            rows.append(
                                Gated(
                                    project=gated.project,
                                    repo=gated.repo,
                                    number=gated.number,
                                    kind=effective,
                                    verdict="skip",
                                    reason=(
                                        f"{ref}: no tracker for this repo, "
                                        "give-up deferred, claim kept"
                                    ),
                                    code="claim",
                                )
                            )
                            continue
                    give_up(
                        store,
                        claim,
                        tracker,
                        observed="fix round found no usable holder",
                        restarts=len(prune_restarts(stamps, now=now)),
                        now=now,
                        out=stream,
                        events=events,
                    )
                    try:
                        current = store.claim_of(gated.repo, gated.number)
                    except (board.StoreError, sqlite3.Error):
                        current = claim
                    if current is None:
                        reason = f"{ref}: past the restart budget, gave up and released the claim"
                    else:
                        reason = f"{ref}: past the restart budget, give-up deferred, claim kept"
                    rows.append(
                        Gated(
                            project=gated.project,
                            repo=gated.repo,
                            number=gated.number,
                            kind=effective,
                            verdict="skip",
                            reason=reason,
                            code="claim",
                        )
                    )
                    continue
                title, skipped = _refresh_title(
                    store,
                    gated,
                    tracker_factory=tracker_factory,
                    operators=operators,
                    now=now,
                    stale_after=stale_after,
                    kind=effective,
                )
                if skipped is not None:
                    rows.append(skipped)
                    continue
                had = key in budgets
                stamped = record_restart(stamps, now=now)
                budgets[key] = stamped
                if restarts_path is not None:
                    try:
                        save_restarts(restarts_path, budgets)
                    except OSError as problem:
                        if had:
                            budgets[key] = stamps
                        else:
                            budgets.pop(key, None)
                        print(
                            f"korapy drive: cannot save the restart budget: {problem}",
                            file=trouble,
                        )
                        rows.append(
                            Gated(
                                project=gated.project,
                                repo=gated.repo,
                                number=gated.number,
                                kind=effective,
                                verdict="skip",
                                reason=f"{ref}: restart budget save failed ({problem}), claim kept",
                                code="claim",
                            )
                        )
                        continue

                def unrecord():
                    if had:
                        budgets[key] = stamps
                    else:
                        budgets.pop(key, None)
                    if restarts_path is None:
                        return
                    try:
                        save_restarts(restarts_path, budgets)
                    except OSError as problem:
                        print(
                            f"korapy drive: cannot save the restart budget: {problem}",
                            file=trouble,
                        )
                        budgets[key] = stamped

                try:
                    store.release(gated.repo, gated.number, holder, claim.epoch, now=now)
                except board.Claimed:
                    unrecord()
                    rows.append(
                        Gated(
                            project=gated.project,
                            repo=gated.repo,
                            number=gated.number,
                            kind=effective,
                            verdict="skip",
                            reason=f"{ref}: fix release raced a re-claim, left held",
                            code="claim",
                        )
                    )
                    continue
                except (board.ExternalItem, board.StoreError, sqlite3.Error) as err:
                    unrecord()
                    rows.append(
                        Gated(
                            project=gated.project,
                            repo=gated.repo,
                            number=gated.number,
                            kind=effective,
                            verdict="skip",
                            reason=f"{ref}: fix release refused ({err}), left held",
                            code="claim",
                        )
                    )
                    continue
                agent, skipped = _mint_claim(
                    store, gated, host=host, now=now, kind=effective, hub=hub
                )
                if skipped is not None:
                    unrecord()
                    rows.append(skipped)
                    continue
            if launcher is None:
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=effective,
                        verdict=effective,
                        reason=f"{gated.reason}; launch skipped (no launcher)",
                        code="",
                    )
                )
                continue
            unit, note, code, unreachable, race = _start_unit(
                launcher,
                project=gated.project,
                repo=gated.repo,
                number=gated.number,
                kind=effective,
                agent=agent,
                title=title,
                now=now,
            )
            blocked = blocked or unreachable
            if unit is None:
                suffix = ""
                if unrecord is not None:
                    # A fix round minted a fresh claim above: a refusal
                    # after it (admission, quota, a launch error)
                    # releases the fresh claim in the same pass, like a
                    # fresh start claim, so the ticket is pickable again
                    # with the epoch kept. A lost race keeps it: a live
                    # unit may own the ticket. A resume failure mints
                    # nothing, so its claim stays held.
                    unrecord()
                    if not race:
                        suffix = _release_own_claim(
                            store, gated.repo, gated.number, agent, now=now, role="fresh"
                        )
                rows.append(
                    Gated(
                        project=gated.project,
                        repo=gated.repo,
                        number=gated.number,
                        kind=effective,
                        verdict="skip",
                        reason=f"{gated.reason}; {note}{suffix}",
                        code=code,
                    )
                )
                continue
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=effective,
                    verdict=effective,
                    reason=f"{gated.reason}; {note}",
                    code="",
                )
            )
            launched.add((gated.project, gated.repo, gated.number))
            if effective == "fix":
                started += 1
                if start_budget is not None:
                    start_budget -= 1
            continue
        if start_budget is not None and start_budget <= 0:
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict="skip",
                    reason=f"{ref}: day cap reached {cap_label(day_cap)}, not started",
                    code="cap",
                )
            )
            continue
        agent, title, skipped = _fresh_claim(
            store,
            gated,
            tracker_factory=tracker_factory,
            operators=operators,
            host=host,
            now=now,
            stale_after=stale_after,
            hub=hub,
        )
        if skipped is not None:
            rows.append(skipped)
            continue
        if launcher is None:
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict=gated.verdict,
                    reason=f"{gated.reason}; launch skipped (no launcher)",
                    code=gated.code,
                )
            )
            continue
        unit, note, code, unreachable, race = _start_unit(
            launcher,
            project=gated.project,
            repo=gated.repo,
            number=gated.number,
            kind=gated.kind,
            agent=agent,
            title=title,
            now=now,
        )
        blocked = blocked or unreachable
        if unit is None:
            # A refusal after the claim (admission, quota, a launch
            # error) releases the fresh claim in the same pass, so the
            # ticket is pickable again; the release keeps the epoch so
            # the next claim fences correctly. A lost race keeps it: a
            # live unit may own the ticket.
            suffix = (
                ""
                if race
                else _release_own_claim(
                    store, gated.repo, gated.number, agent, now=now, role="fresh"
                )
            )
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict="skip",
                    reason=f"{gated.reason}; {note}{suffix}",
                    code=code,
                )
            )
            continue
        rows.append(
            Gated(
                project=gated.project,
                repo=gated.repo,
                number=gated.number,
                kind=gated.kind,
                verdict=gated.kind,
                reason=f"{gated.reason}; {note}",
                code="",
            )
        )
        launched.add((gated.project, gated.repo, gated.number))
        started += 1
        if start_budget is not None:
            start_budget -= 1
    return rows, started, blocked, launched


def _local_repo(root, repo, *, git=None):
    """The `[[repo]]` entry name behind tracker repo `repo`.

    The reverse of the one short-to-tracker resolver: the spelling
    itself is decided by `reviewrecord.local_repo` over the root's
    shared short-to-tracker map, so this never grows a second copy
    of the tracker-to-short join. Only the entry whose resolved
    tracker repo names it wins, an entry name that merely equals
    `repo` is not a mapping, and a reported problem is surfaced,
    never masked. Anything else is a `LaunchError` naming the root
    and the repo. `git` names the origin-lookup seam for entries
    without an explicit tracker repo; None is the real runner.
    """
    runner = worktrees.git if git is None else git
    try:
        mapping = syncli.root_short_map(root, runner)
    except settings.SettingsError as problem:
        raise launch.LaunchError(str(problem)) from None
    try:
        short = reviewrecord.local_repo(repo, mapping)
    except ValueError as err:
        raise launch.LaunchError(f"{root}: {err}") from None
    if mapping.get(short) != repo:
        raise launch.LaunchError(f"{root}: no [[repo]] entry feeds tracker repo {repo!r}")
    return short


def _overlay_text(root):
    """The ops root's `agents.overlay.md`, or "" when it is absent."""
    path = os.path.join(root, "agents.overlay.md")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as err:
        raise launch.LaunchError(f"cannot read the agent overlay: {err}") from None


def _worker_prompt(root, tgt, number, title):
    """A `(workdir, branch) -> text` renderer for the worker role.

    Validated before the launch makes anything, so a bad override never
    leaves a stray branch behind. The heartbeat lines go on in
    `launch.launch`, not here.
    """
    agents_md = os.path.join(tgt.clone, "AGENTS.md")
    base = {
        "issue": str(number),
        "title": title,
        "checks.test": tgt.resolved["checks.test"],
        "overlay": _overlay_text(root),
        "project_agents_md": (agents_md if os.path.isfile(agents_md) else "AGENTS.md (none found)"),
    }
    try:
        prompts.render("worker", {**base, "worktree": "-", "branch": "-"}, ops_root=root)
    except prompts.PromptsError as err:
        raise launch.LaunchError(str(err)) from None

    def render_prompt(workdir, branch):
        try:
            return prompts.render(
                "worker", {**base, "worktree": workdir, "branch": branch}, ops_root=root
            )
        except prompts.PromptsError as err:
            raise launch.LaunchError(str(err)) from None

    return render_prompt


def production_orphan_inputs(registered, *, now, path=None, git=None, mapping=None):
    """`(present_agents, live_units, gone_s)` from the registry and units.

    `present_agents` is every agent id with a live or stale registry
    entry; `live_units` is every (repo, number) with a live unit under
    any registered prefix, as store keys through the one
    short-to-tracker resolver, so the orphan and stuck checks join
    them straight to claimed rows; `gone_s` is the registry setting
    of the same name, the single resolver for it. `path` is the
    registry dir seam tests inject; `git` names the origin-lookup
    seam for entries without an explicit tracker repo. `mapping` is
    the pass's one short-to-tracker map; with none, the builder
    resolves its own. Raises the settings error when the registry or
    project settings are bad, and `units.Unreachable` when a unit
    listing fails.
    """
    cfg = registry.config()
    mapping = _resolved_map(registered, mapping, syncli._git if git is None else git)
    present = {
        entry["id"]
        for entry, state in registry.visible(now, cfg, path)
        if state in (registry.LIVE, registry.STALE)
    }
    tickets = set()
    for project in registered:
        listed = units.list_units(project.unit_prefix, run=units.default_run)
        tickets |= units.live_tickets(project.unit_prefix, listed)
    return present, {_tracker_key(ticket, mapping) for ticket in tickets}, cfg["gone_s"]


def _reviewer_prompt(root, tgt, number, title, author):
    """A `(workdir, branch) -> text` renderer for the reviewer role.

    The worker id rides in the prompt text: the reviewer passes it as
    `review record --author`, and the record refuses a self-review. A
    blank author raises LaunchError before anything is launched, since
    a reviewer that cannot record is a wasted run. The no-claim
    guarantee is the author skip in the claim path, not this guard:
    this renderer runs at launch time, after the claim. Validated
    before the launch makes anything, so a bad override never leaves a
    stray branch behind. The heartbeat lines go on in `launch.launch`,
    not here.
    """
    worker = str(author or "").strip()
    if not worker:
        raise launch.LaunchError(
            f"{tgt.repo}#{number}: a reviewer launch needs the pre-takeover worker id as author"
        )
    block = reviewer_author_block(worker)
    agents_md = os.path.join(tgt.clone, "AGENTS.md")
    base = {
        "issue": str(number),
        "title": title,
        "checks.test": tgt.resolved["checks.test"],
        "overlay": _overlay_text(root),
        "project_agents_md": (agents_md if os.path.isfile(agents_md) else "AGENTS.md (none found)"),
    }
    try:
        prompts.render("reviewer", {**base, "worktree": "-", "branch": "-"}, ops_root=root)
    except prompts.PromptsError as err:
        raise launch.LaunchError(str(err)) from None

    def render_prompt(workdir, branch):
        try:
            text = prompts.render(
                "reviewer", {**base, "worktree": workdir, "branch": branch}, ops_root=root
            )
        except prompts.PromptsError as err:
            raise launch.LaunchError(str(err)) from None
        return text + block

    return render_prompt


def production_launcher(registered, *, store=None, hold_label=None):
    """The launch handoff over the existing launch path.

    Returns `launch_one(project, repo, number, kind, agent, title, *,
    now, author=None)`: the unit name of the started agent. `start` and
    `fix` launch a worker (a fix reuses the ticket's branch through the
    launch path); `review` launches with the reviewer role and the
    worker id as its author, which the prompt carries to record time;
    `resume` relaunches the holder's session with the fix message. The
    target resolves from the project's ops root, and the unit starts
    through `launch.launch` (or `launch.resume`) with the production
    seams. A review without an author, a resume without the store-backed
    hold check, and an unknown project all raise `LaunchError` before
    anything is made. Failures raise with the public exit code.
    """

    def launch_one(project, repo, number, kind, agent, title, *, now, author=None):
        _ = (agent, now)
        if kind not in ("start", "review", "resume", "fix"):
            raise launch.LaunchError(f"{repo}#{number}: unknown drive kind {kind!r}")
        if kind == "review" and not (author or "").strip():
            raise launch.LaunchError(
                f"{repo}#{number}: a reviewer launch needs the pre-takeover worker id as author"
            )
        if kind == "resume" and (store is None or hold_label is None):
            raise launch.LaunchError(f"{repo}#{number}: a resume needs the store hold check")
        entry = next((item for item in registered if item.name == project), None)
        if entry is None:
            raise launch.LaunchError(f"unknown project {project!r} for {repo}#{number}")
        local = _local_repo(entry.root, repo)
        tgt = launch.target(entry.root, local)

        def list_fn(prefix):
            return units.list_units(prefix, run=units.default_run)

        if kind == "review":
            factory = _reviewer_prompt(entry.root, tgt, number, title, author)
            unit, _ = launch.launch(
                tgt,
                number,
                factory,
                title=title,
                role="reviewer",
                list_fn=list_fn,
                run=units.default_run,
                usage=disk.production_usage,
                host_ledger_fn=host_ledger.ensure,
            )
            return unit
        if kind == "resume":
            # Validated above: a resume always has the store hold check.

            def hold_reason_fn(resume_tgt, resume_number):
                _ = (resume_tgt, resume_number)
                return hold.reason(
                    _store_labels(store, now), (repo, number), (), hold_label=hold_label
                )

            unit, _ = launch.resume(
                tgt,
                number,
                fix_message(repo, number),
                hold_reason_fn=hold_reason_fn,
                list_fn=list_fn,
                run=units.default_run,
                usage=disk.production_usage,
                host_ledger_fn=host_ledger.ensure,
                git=worktrees.git,
            )
            return unit
        factory = _worker_prompt(entry.root, tgt, number, title)
        unit, _ = launch.launch(
            tgt,
            number,
            factory,
            title=title,
            role="worker",
            list_fn=list_fn,
            run=units.default_run,
            usage=disk.production_usage,
            host_ledger_fn=host_ledger.ensure,
        )
        return unit

    return launch_one


def _launching_pass(
    store,
    out,
    err,
    *,
    now,
    projects_fn,
    live,
    limits,
    quotas,
    ledger,
    read_labels,
    hold_label,
    pool,
    tracker_factory,
    operators,
    launcher,
    host,
    stale_after,
    present_agents,
    live_units,
    gone_s,
    agent_states,
    stale_s,
    stop_unit,
    restarter,
    restarts_path,
    stuck_project,
    agent_roles=None,
    agent_tickets=None,
    finished_for=None,
    journal_path=None,
    day_path=None,
    max_starts=None,
    hub_epoch_of=None,
    hub=None,
    repo_map=None,
    review_reader=None,
    pulls_for=None,
) -> int:
    """One claiming pass: sync, release, gate, claim, launch, mirror, print.

    Reviewed-claim release and orphan recovery run on store, registry
    and unit state right after the sync, so freed rows gate as unclaimed
    in the same pass. Finished worker claims are kept for the reviewer
    takeover instead of released. In hub mode the pass first reconciles with the hub
    (fetch, fence the stale, re-adopt the live), so fencing lands before
    any new proposal; a dead hub skips every claim-needing candidate and
    exits 3 while resumes, the stuck sweep's skips, and the mirror
    drain continue. A unit listing failure skips recovery only: the
    pass continues, since a claim the pass cannot prove dead is left
    held. A tracker outage aborts the sync step only: prior rows and the
    old cursor stand, and the pass continues on store data. A read the
    tracker answers with a validation rejection is refused, not
    unreachable: its line is reported and the pass continues on store
    data. A claim whose stale row cannot be re-read live fails closed
    instead. The stuck
    sweep restarts or gives up on managed claims the registry has lost,
    after the fresh claims launch, spending the day budget the fresh
    claims leave over (floored at zero): each
    restart counts toward the cap and an exhausted cap leaves the row
    held. The mirror drains whatever the
    outbox holds, old backlog and new claims alike; what it cannot push
    stays queued for the next healthy pass. A pass that synced nothing
    at all exits 3 on tracker failures, 1 when the only failures are
    refused reads, and 4 when only corrupt stored rows failed; a pass
    that launched nothing because the service
    manager was unreachable exits 3 too. The stuck sweep re-checks
    row eligibility before restarting (a held, closed, externally owned,
    needs-decision or withdrawn row is released, never restarted).
    When `journal_path` is set, the
    pass appends its start, every start, skip, release, restart and
    give-up, and its end with verdict counts; a journal write failure
    warns and the pass continues. When `day_path` and `max_starts` are
    set, at most the remaining starts launch and the pass ends with exit
    1 once the cap stops a start. `hub` is the injected hub facade
    (built CLI-side: `drive` never names the hub module); None means
    single-host, zero hub calls. A facade with an empty host id is
    refused with the usage error (exit 4): a host that cannot name
    itself must never reconcile. `repo_map` is the pass's one
    short-to-tracker map, threaded to the admission live set and to
    orphan recovery.
    """
    events: list[dict] = []
    released = 0

    def note(entry):
        if journal_path is not None:
            events.append(entry)

    def journal_write(entry):
        if journal_path is None:
            return
        try:
            log_event(journal_path, entry)
        except OSError as problem:
            print(f"korapy drive: cannot write the journal: {problem}", file=err)

    def finish(code, started=0, skipped=None):
        if journal_path is not None:
            for entry in events:
                journal_write(entry)
            journal_write(
                {
                    "event": "pass_end",
                    "at": now,
                    "exit": code,
                    "started": started,
                    "skipped_by_reason": skipped or {},
                    "released": released,
                    "gave_up": sum(1 for entry in events if entry.get("event") == "give_up"),
                }
            )
        return code

    if journal_path is not None:
        journal_write({"event": "pass_start", "at": now, "host": host or ""})
    try:
        seen, sync_errors, sync_store_errors, sync_refused, sync_adopt_errors = sync_all(
            store, tracker_factory, operators=operators, now=now
        )
    except board.StoreError as problem:
        print(f"korapy drive: {problem}", file=err)
        return finish(USAGE)
    except sqlite3.Error as problem:
        print(f"korapy drive: cannot read the store: {problem}", file=err)
        return finish(USAGE)
    for line in sync_errors + sync_store_errors + sync_refused + sync_adopt_errors:
        print(f"korapy drive: {line}", file=err)
    if hub is not None:
        if not getattr(hub, "host_id", ""):
            print("korapy drive: hub host id is empty: register this host first", file=err)
            return finish(USAGE)
        try:
            hub.reconcile(store, stop_unit=stop_unit, out=out, err=err, now=now)
        except HubGone as gone:
            print(f"korapy drive: hub unreachable, new claims skipped ({gone})", file=err)
        except HubConfigError as problem:
            print(f"korapy drive: {problem}", file=err)
            return finish(USAGE)
        if hub_epoch_of is None:
            hub_epoch_of = hub.epoch_of
    ctx, fatal = _read_inputs(
        store,
        out,
        err,
        now=now,
        projects_fn=projects_fn,
        live=live,
        limits=limits,
        quotas=quotas,
        ledger=ledger,
        read_labels=read_labels,
        hold_label=hold_label,
        pool=pool,
        repo_map=repo_map,
    )
    if fatal is not None:
        return finish(fatal)
    (
        _items,
        registered,
        live_set,
        limits,
        hold_label,
        read_labels,
        ledger_data,
        quota_for,
        pool,
    ) = ctx
    day_today = None
    day_used = 0
    start_budget = None
    if day_path is not None and max_starts is not None:
        if max_starts < 0:
            print("korapy drive: drive.max_starts_per_day must not be negative", file=err)
            return finish(USAGE)
        day_today = _day_string(now)
        try:
            day_used = read_day_count(day_path, day_today)
        except JournalError as problem:
            print(f"korapy drive: {problem}", file=err)
            return finish(USAGE)
        start_budget = max_starts - day_used
    skip_orphans = False
    if gone_s is None:
        try:
            present_agents, live_units, gone_s = production_orphan_inputs(
                registered, now=now, mapping=repo_map
            )
        except settings.SettingsError as problem:
            print(f"korapy drive: {problem}", file=err)
            return finish(USAGE)
        except units.Unreachable as problem:
            print(
                f"korapy drive: cannot read live units, orphan recovery skipped ({problem})",
                file=err,
            )
            skip_orphans = True
    else:
        present_agents = frozenset(present_agents or ())
        live_units = frozenset(live_units or ())
    authors = _capture_finished_holders(
        store, agent_states=agent_states, agent_tickets=agent_tickets, err=err
    )
    finished = resolve_finished(
        finished_for, authors, agent_states=agent_states, agent_tickets=agent_tickets
    )
    freed: dict = {}
    if agent_states is not None:
        try:
            freed = release_reviewed(
                store,
                claims=reviewed_claims(authors, finished),
                live_units=live_units,
                now=now,
                out=out,
                events=events if journal_path is not None else None,
            )
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"korapy drive: finished reconciliation skipped ({problem})", file=err)
        try:
            _items = store.items()
        except board.StoreError as problem:
            print(f"korapy drive: {problem}", file=err)
            return USAGE
        except sqlite3.Error as problem:
            print(f"korapy drive: cannot read the store: {problem}", file=err)
            return USAGE
    if not skip_orphans:
        try:
            released = release_orphans(
                store,
                now=now,
                gone_s=gone_s,
                present_agents=present_agents,
                live_units=live_units,
                out=out,
                events=events if journal_path is not None else None,
                agent_states=agent_states,
                agent_tickets=agent_tickets,
            )
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"korapy drive: orphan recovery skipped ({problem})", file=err)
        try:
            _items = store.items()
        except board.StoreError as problem:
            print(f"korapy drive: {problem}", file=err)
            return finish(USAGE)
        except sqlite3.Error as problem:
            print(f"korapy drive: cannot read the store: {problem}", file=err)
            return finish(USAGE)
    if review_reader is not None:
        reviews = review_records_for(_items, read=review_reader, err=err)
    else:
        reviews = None
    gated_rows, problems, eligible_keys = _gate_all(
        _items,
        registered,
        out,
        now=now,
        live_set=live_set,
        limits=limits,
        quota_for=quota_for,
        pool=pool,
        ledger_data=ledger_data,
        read_labels=read_labels,
        hold_label=hold_label,
        released=freed,
        finished=finished,
        agent_roles=agent_roles,
        agent_states=agent_states,
        agent_tickets=agent_tickets,
        reviews=reviews,
        pulls_for=pulls_for,
    )
    log_missing_reviews(out, finished, reviews, _released_rows(_items, freed))
    if host is None:
        try:
            configured = settings.load(root=None)["drive.agent_host"]
        except settings.SettingsError as problem:
            print(f"korapy drive: {problem}", file=err)
            return finish(USAGE)
        host = agent_host_segment(configured, hostname=socket.gethostname())
    if stale_after is None and tracker_factory is not None:
        try:
            stale_after = settings.load(root=None)["board.item_max_age_s"]
        except settings.SettingsError as problem:
            print(f"korapy drive: {problem}", file=err)
            return finish(USAGE)
    try:
        final_rows, started, blocked, launched = _claim_and_launch(
            store,
            gated_rows,
            tracker_factory=tracker_factory,
            operators=operators,
            launcher=launcher,
            host=host,
            now=now,
            stale_after=stale_after,
            start_budget=start_budget,
            day_cap=max_starts,
            released=authors,
            finished=finished,
            agent_states=agent_states,
            agent_tickets=agent_tickets,
            live_units=live_units,
            restarts_path=restarts_path,
            agent_roles=agent_roles,
            out=out,
            err=err,
            events=events if journal_path is not None else None,
            hub=hub,
        )
    except HubConfigError as problem:
        print(f"korapy drive: {problem}", file=err)
        return finish(USAGE)
    stuck_capped = False
    if agent_states is not None:
        defaults = registry.default_config()
        stale = stale_s if stale_s is not None else defaults["stale_s"]
        gone = gone_s if gone_s is not None else defaults["gone_s"]
        launch = restarter if restarter is not None else launcher
        stuck_budget = None
        if start_budget is not None:
            stuck_budget = max(0, start_budget - started)
        try:
            stuck_restarted, stuck_capped = handle_stuck(
                store,
                agent_states=agent_states,
                stale_s=stale,
                gone_s=gone,
                live_units=live_units,
                now=now,
                host=host,
                stuck_project=stuck_project,
                stop_unit=stop_unit,
                restarter=launch,
                tracker_factory=tracker_factory,
                restarts_path=restarts_path,
                out=out,
                err=err,
                events=events if journal_path is not None else None,
                start_budget=stuck_budget,
                day_cap=max_starts,
                hub_epoch_of=hub_epoch_of,
                read_labels=read_labels,
                hold_label=hold_label,
                requested=eligible_keys,
                agent_tickets=agent_tickets,
            )
            started += stuck_restarted
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"korapy drive: stuck handling skipped ({problem})", file=err)
    for row in final_rows:
        ref = f"{row.repo}#{row.number}"
        key = (row.project, row.repo, row.number)
        if row.verdict == "skip" or key not in launched:
            note(
                {
                    "event": "skip",
                    "at": now,
                    "project": row.project,
                    "ticket": ref,
                    "code": row.code,
                    "reason": row.reason,
                }
            )
        else:
            note(
                {
                    "event": "start",
                    "at": now,
                    "project": row.project,
                    "ticket": ref,
                    "kind": row.kind,
                }
            )
    capped = any(row.code == "cap" for row in final_rows) or stuck_capped
    if capped:
        note({"event": "cap", "at": now, "max": max_starts})
    if day_today is not None and day_path is not None:
        try:
            write_day_count(day_path, day_today, day_used + started)
        except OSError as problem:
            print(f"korapy drive: cannot write the day counter: {problem}", file=err)
    skipped: dict[str, int] = {}
    for row in final_rows:
        if row.verdict == "skip":
            key = row.code or "skip"
            skipped[key] = skipped.get(key, 0) + 1
    if tracker_factory is not None:
        try:
            queued_repos = sorted({row.repo for row in store.queued_writes()})
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"korapy drive: {problem}", file=err)
            return USAGE
        pairs = []
        for repo in queued_repos:
            try:
                pairs.append((repo, tracker_factory(repo)))
            except KeyError:
                print(f"korapy drive: {repo}: no tracker for this repo, skipped", file=err)
                continue
        try:
            outcome = mirror.push_repos(store, pairs, operators=operators, now=now)
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"korapy drive: {problem}", file=err)
            return finish(USAGE, started=started, skipped=skipped)
        for line in outcome.errors:
            print(f"korapy drive: {line}", file=err)
    table = [
        Candidate(
            project=gated.project,
            repo=gated.repo,
            number=gated.number,
            kind=gated.kind,
            verdict=gated.verdict,
            reason=gated.reason,
        )
        for gated in final_rows
    ]
    out.write(render(table, label="pass", plan=False))
    for problem in problems:
        print(problem, file=err)
    if problems:
        return finish(USAGE, started=started, skipped=skipped)
    if sync_store_errors and seen == 0:
        return finish(USAGE, started=started, skipped=skipped)
    if sync_errors and seen == 0:
        return finish(UNREACHABLE, started=started, skipped=skipped)
    if hub is not None and hub.down:
        return finish(UNREACHABLE, started=started, skipped=skipped)
    if blocked and started == 0:
        return finish(UNREACHABLE, started=started, skipped=skipped)
    if sync_refused and seen == 0:
        return finish(NO, started=started, skipped=skipped)
    if capped:
        return finish(NO, started=started, skipped=skipped)
    return finish(OK, started=started, skipped=skipped)


def run_pass(
    *,
    plan,
    store,
    lock_path,
    out,
    err,
    now=None,
    projects_fn=None,
    live=None,
    limits=None,
    quotas=None,
    ledger=None,
    read_labels=None,
    hold_label=None,
    pool=None,
    tracker_factory=None,
    operators=None,
    launcher=None,
    host=None,
    stale_after=None,
    present_agents=None,
    live_units=None,
    gone_s=None,
    agent_states=None,
    stale_s=None,
    stop_unit=None,
    restarter=None,
    restarts_path=None,
    stuck_project=None,
    agent_roles=None,
    agent_tickets=None,
    finished_for=None,
    journal_path=None,
    pause_path=None,
    day_path=None,
    max_starts=None,
    hub_epoch_of=None,
    hub=None,
    repo_map=None,
    review_reader=None,
    pulls_for=None,
) -> int:
    """One pass: gate and print, or sync, claim, launch, mirror and print.

    `--plan` holds no lock and writes nothing: no sync, orphan or
    finished release, hub contact, claim, mirror, launch, restart,
    give-up, journal or day-counter write, whatever seams are passed.
    The non-plan pass takes
    the drive lock, releases reviewed claims, releases
    provably-dead claims, and claims through the store transaction, then
    launches through `launcher` (the existing launch path in production;
    with no launcher the pass stops after claiming and records that),
    handles stuck claims, and drains the mirror backlog through the
    per-repo trackers. `tracker_factory` maps a tracker repo to its
    tracker (labels or board, built by the one factory); with none
    there is no sync, no freshness re-read, no mirror and no give-up
    post. `operators` maps tracker repos to operator logins for the sync
    and mirror steps; `host` is the agent-id host segment on its claims
    (None resolves `drive.agent_host` once, hashing the hostname when
    empty, so the raw hostname never reaches the tracker); `stale_after`
    bounds the cached rows a claim may use without a live re-read.
    `present_agents` (ids with a live or stale registry entry),
    `live_units` ((repo, number) pairs with a live unit) and `gone_s`
    feed orphan recovery; `gone_s=None` reads all three from the host
    instead. The plan pass filters its finished-reviewer simulation
    by `live_units` exactly like the claiming pass filters its
    release; the production CLI supplies the host units. The stuck
    sweep runs only when `agent_states` (ids to
    (state, last_seen)) is given: `stale_s` bounds its heartbeats,
    `stop_unit(repo, number)` stops a ticket, the restarter (or
    `launcher` when None) relaunches it, `restarts_path` persists the
    budget (in memory when None), and `stuck_project(repo, number)`
    names the owning project. With no stop or launch seam a stuck row is
    left held; a failed seam keeps the claim. `hub_epoch_of` maps
    (repo, number) to the hub row's fencing epoch for the stuck epoch
    check (None means single-host: no check); an explicit seam wins
    over the hub table the pass builds. `hub` is the injected hub
    facade for the claim path and the reconnect (None means
    single-host: zero hub calls). `finished_for`
    ({(repo, number): (agent id, role, outcome)} for the newest finished
    entry per ticket) feeds the reviewer and fix-round readiness
    alongside the same-pass capture, and the missing-record check.
    `agent_roles` ({agent id: role} for visible
    entries) serializes a fix behind a reviewer holder; unknown
    holders proceed as before. `agent_tickets` ({agent id: (repo,
    number)} for visible entries naming a ticket) is the ticket index
    the stuck sweep, the finished-claims scan and the fix-round
    readiness resolve holders through: a claim's holder id never names
    its unit-name entry. The plan pass reads it too, so `--plan`
    prints the same reviewer-holder skips. When `pause_path` names an
    existing pause file, either pass reports it and exits 1, starting
    nothing. When `journal_path` is set, the non-plan pass journals its
    decisions, restarts and give-ups included; when `day_path` and
    `max_starts` are set, starts beyond the day's budget become `cap`
    skips and the pass ends with exit 1. `review_reader`
    feeds the gate's fix-round threshold from the ticket session dirs
    (reads only, like the registry); with none the threshold sees no
    records, exactly as before. `pulls_for` feeds the pass's one
    linked-pull map, which the eligibility rows and the gate rows share;
    with none every row reads empty, which is what `--plan` builds: it
    never contacts the tracker. `now` defaults to the wall
    clock; every other None means the host default, so tests inject
    plain data or fakes at those seams and
    production reads the host. `repo_map` is the pass's one
    short-to-tracker map ({short [[repo]] name: tracker repo}), built
    by `production_repo_map` once per production pass; with none, the
    readers that need it resolve their own. Returns the exit code.
    """
    moment = time.time() if now is None else now
    if pause_path is not None:
        try:
            reason = pause_reason(pause_path)
        except OSError as problem:
            print(f"korapy drive: cannot read the pause file: {problem}", file=err)
            return USAGE
        if reason is not None:
            print(f"korapy drive: paused: {reason}", file=err)
            return NO
    args = {
        "now": moment,
        "projects_fn": projects_fn,
        "live": live,
        "limits": limits,
        "quotas": quotas,
        "ledger": ledger,
        "read_labels": read_labels,
        "hold_label": hold_label,
        "pool": pool,
    }
    if plan:
        return _pass(
            store,
            out,
            err,
            label="plan",
            agent_states=agent_states,
            finished_for=finished_for,
            agent_roles=agent_roles,
            agent_tickets=agent_tickets,
            live_units=live_units,
            repo_map=repo_map,
            review_reader=review_reader,
            pulls_for=pulls_for,
            **args,
        )
    try:
        with drive_lock(lock_path):
            return _launching_pass(
                store,
                out,
                err,
                tracker_factory=tracker_factory,
                operators=operators,
                launcher=launcher,
                host=host,
                stale_after=stale_after,
                present_agents=present_agents,
                live_units=live_units,
                gone_s=gone_s,
                agent_states=agent_states,
                stale_s=stale_s,
                stop_unit=stop_unit,
                restarter=restarter,
                restarts_path=restarts_path,
                stuck_project=stuck_project,
                agent_roles=agent_roles,
                agent_tickets=agent_tickets,
                finished_for=finished_for,
                journal_path=journal_path,
                day_path=day_path,
                max_starts=max_starts,
                hub_epoch_of=hub_epoch_of,
                hub=hub,
                repo_map=repo_map,
                review_reader=review_reader,
                pulls_for=pulls_for,
                **args,
            )
    except LockHeld as held:
        print(f"korapy drive: {held}", file=err)
        return RACE
    except LockError as problem:
        print(f"korapy drive: {problem}", file=err)
        return USAGE


def cmd_install_timer(args: argparse.Namespace) -> int:
    """Install and enable the user timer: the explicit operator step.

    The only path that writes or enables units. Passes never reach it:
    the `drive` verb calls it only for `install-timer`. The cadence
    comes from the `drive.period` setting, resolved here: a reinstall
    picks up a changed period.
    """
    _ = args
    try:
        period = settings.load(root=None)["drive.period"]
    except settings.SettingsError as problem:
        print(f"korapy drive: {problem}", file=sys.stderr)
        return USAGE
    try:
        service_path, timer_path = install_timer(period=period)
    except units.Unreachable as problem:
        print(f"korapy drive: {problem}", file=sys.stderr)
        return UNREACHABLE
    except TimerError as problem:
        print(f"korapy drive: {problem}", file=sys.stderr)
        return USAGE
    sys.stdout.write(f"installed {service_path}\ninstalled {timer_path}\nenabled {TIMER_NAME}\n")
    return OK
