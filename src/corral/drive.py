"""`corral drive`: one dispatch pass over the board store.

Each pass joins every registered project's eligibility requests to the
store rows, runs the section 3 gate order over the candidates, and
prints the verdict table. The pass only reads: nothing is synced,
claimed, enqueued, started, mirrored or journaled (claims and the
launch handoff land in a later ticket). The non-plan pass holds the
single-instance lock (exit 2 when another pass holds it). `--plan` is
the dry run: it never takes the lock.

Exit codes: 0 completed, 2 another pass holds the lock, 3 live host
state (units or the registry) could not be read, 4 the store could not
be read, the lock could not be set up, or a project failed to load.
"""

import argparse
import contextlib
import errno
import fcntl
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from corral import (
    accounts,
    admission,
    board,
    eligibility,
    hold,
    launch,
    paths,
    projects,
    quota,
    quota_ledger,
    registry,
    settings,
    units,
)
from corral.eligibility import StartRequest, TicketRow
from corral.github.tracker import NEEDS_DECISION, PRIORITIES

OK, RACE, UNREACHABLE, USAGE = 0, 2, 3, 4

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


def priority_rank(priority) -> int:
    """The board priority's rank, higher first. Pure.

    Follows the tracker's highest-first priority order; anything outside
    it (including no priority) ranks zero and sorts last.
    """
    if priority in PRIORITIES:
        return len(PRIORITIES) - PRIORITIES.index(priority)
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
    resolves each project's entry from its own settings.
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


def gate(candidate, *, read_labels, hold_label, live, limits, quota_source, ledger, now) -> Gated:
    """The section 3 gate order for one candidate, cheapest first. Pure.

    Row state first (missing, non-open, external, withdrawn), then claim
    routing by request kind against the row's holder, then the hold and
    decision labels, then admission, then the quota gate. Every input is
    plain data or an injected seam (`read_labels`); nothing is launched,
    claimed or written.
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
        if request.kind == "start":
            return _skip(
                candidate,
                kind,
                "claim",
                f"{ref}: already claimed by `{holder}`, start refused",
            )
        if request.kind == "review":
            return _skip(
                candidate,
                kind,
                "claim",
                f"{ref}: worker `{holder}` still holds the claim, review waits for release",
            )
        if not is_managed_agent(holder):
            return _skip(
                candidate,
                kind,
                "claim",
                f"{ref}: foreign claim by `{holder}`, never touched",
            )

    held = hold.reason(
        read_labels,
        (row.repo, row.number),
        [(row.repo, pull.number) for pull in row.pulls or ()],
        hold_label=hold_label,
    )
    if held is not None:
        return _skip(candidate, kind, "hold", held)
    if NEEDS_DECISION in (row.labels or ()):
        return _skip(
            candidate,
            kind,
            "hold",
            f"{ref}: carries {NEEDS_DECISION}, waiting on a human, never started",
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

    if kind == "start" and request.kind in ("resume", "fix"):
        reason = f"{ref}: {request.kind} on an unclaimed ticket, fresh start instead"
    elif kind == "start":
        reason = f"{ref}: unclaimed, fresh start"
    elif kind == "review":
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

    repo: str
    number: int
    verdict: str
    reason: str


def lock_path():
    """The single-instance lock file: inside the state dir.

    The only path join for it (AGENTS.md, "One fact, one place").
    """
    return os.path.join(paths.state_dir(), LOCK_NAME)


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


def _default_live(registered):
    """Live (project, repo, number) triples from units and the registry."""
    return launch.admission_state(registered, units.list_units, registry.config(), "")


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


def _project_quota(project):
    """One project's quota input from its own settings file."""
    resolved = settings.load(root=project.root)
    return QuotaSource(
        chains=dict(resolved.models),
        low_pct=resolved["quota.low_pct"],
        unknown_blocks=resolved["quota.unknown_blocks"],
    )


def _store_labels(store, now):
    """A `read_labels` seam over cached store rows: reads, never a tracker.

    Linked pulls have no rows yet (the reviewer pass populates them), so
    only issue reads happen; a missing row fails closed as unreadable.
    """

    def read(kind, repo, number):
        if kind != "issue":
            return []
        found, _ = store.item(repo, number, now=now)
        if found is None:
            raise board.StoreError(f"unknown item {repo}#{number}")
        return list(found.labels or [])

    return read


def render(rows, *, label, started):
    """The candidate table plus the summary line under `label`."""
    lines = [f"{row.repo}#{row.number}: {row.verdict} ({row.reason})" for row in rows]
    lines.append(f"{label}: {len(rows)} candidate(s), would start {started}")
    return "\n".join(lines) + "\n"


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
) -> int:
    """One read-only pass: join eligibility to store rows, gate, print.

    Nothing is synced, claimed, enqueued, started, mirrored or journaled.
    A project whose policy or quota settings fail is skipped with one
    error line; the pass still gates every other project and exits 4.
    """
    try:
        items = store.items()
    except board.StoreError as problem:
        print(f"corral drive: {problem}", file=err)
        return USAGE
    except sqlite3.Error as problem:
        print(f"corral drive: cannot read the store: {problem}", file=err)
        return USAGE
    try:
        registered = (projects_fn or projects.load_all)()
    except projects.ProjectsError as problem:
        print(f"corral drive: {problem}", file=err)
        return USAGE
    try:
        live_set = set(live) if live is not None else _default_live(registered)
    except settings.SettingsError as problem:
        print(f"corral drive: {problem}", file=err)
        return USAGE
    except Exception as problem:
        print(f"corral drive: cannot read live state: {problem}", file=err)
        return UNREACHABLE
    try:
        ledger_data = ledger if ledger is not None else quota_ledger.load()
    except quota_ledger.LedgerError as problem:
        print(f"corral drive: {problem}", file=err)
        return USAGE
    if limits is None or hold_label is None:
        try:
            default_limits, default_hold_label = _default_host_inputs(registered)
        except (settings.SettingsError, admission.LimitsError) as problem:
            print(f"corral drive: {problem}", file=err)
            return USAGE
        if limits is None:
            limits = default_limits
        if hold_label is None:
            hold_label = default_hold_label
    if read_labels is None:
        read_labels = _store_labels(store, now)

    rows = eligibility.rows_from_items(items)
    by_key = {(row.repo, row.number): row for row in rows}
    waits = {(item.repo, item.number): item.tracker_updated_at for item in items}
    quota_for = dict(quotas) if quotas is not None else {}
    problems = []
    candidates = []
    for project in registered:
        if project.name not in quota_for:
            try:
                quota_for[project.name] = _project_quota(project)
            except settings.SettingsError as problem:
                problems.append(f"corral drive: {problem}")
                continue
        try:
            outcome = eligibility.for_project(project, items, now=now)
        except eligibility.EligibilityError as problem:
            problems.append(f"corral drive: {problem}")
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
        key = (item.repo, item.number)
        candidates.append(GateInput("", None, by_key.get(key), waits.get(key, 0.0)))

    counted: dict = {}
    for entry_project, _, _ in live_set:
        counted[entry_project] = counted.get(entry_project, 0) + 1

    def floor_of(name):
        entry = limits.projects.get(name) or {}
        return entry.get("floor", 0)

    stop_starts = False
    gated_rows = []
    for candidate in order(candidates):
        if candidate.request is not None:
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
        )
        if verdict.code == admission.TOTAL_FULL:
            stop_starts = True
        gated_rows.append(verdict)
    table = [
        Candidate(repo=gated.repo, number=gated.number, verdict=gated.verdict, reason=gated.reason)
        for gated in gated_rows
    ]
    started = sum(1 for gated in gated_rows if gated.verdict != "skip")
    out.write(render(table, label=label, started=started))
    for problem in problems:
        print(problem, file=err)
    return USAGE if problems else OK


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
) -> int:
    """One pass: join eligibility to store rows, gate, print, write nothing.

    Only the non-plan pass takes the drive lock; `--plan` never does.
    `now` defaults to the wall clock; every other None means the host
    default (registered projects, live units and registry, host limits,
    per-project quota inputs, the shared ledger, store-backed labels,
    the configured hold label), so tests inject plain data or fakes at
    those seams and production reads the host. Returns the exit code.
    """
    moment = time.time() if now is None else now
    args = {
        "now": moment,
        "projects_fn": projects_fn,
        "live": live,
        "limits": limits,
        "quotas": quotas,
        "ledger": ledger,
        "read_labels": read_labels,
        "hold_label": hold_label,
    }
    if plan:
        return _pass(store, out, err, label="plan", **args)
    try:
        with drive_lock(lock_path):
            return _pass(store, out, err, label="pass", **args)
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
