"""`corral drive`: one dispatch pass over the board store.

Each pass syncs the board store from the tracker, releases
provably-dead claims, joins every registered project's eligibility
requests to the store rows, runs the section 3 gate order over the
candidates, claims the proceeding ones through the store transaction,
hands the starts to the existing launch path, drains the mirror
backlog, and prints the verdict table.
`--plan` is the dry run: it holds no lock and writes nothing, running
no sync, claim, mirror or launch. The non-plan pass holds the
single-instance lock (exit 2 when another pass holds it).

Exit codes: 0 completed, 2 another pass holds the lock, 3 the tracker
or live host state (units or the registry) could not be reached, 4 the
store could not be read, the lock could not be set up, or a project
failed to load. A full host that starts nothing routine is still 0:
saturation is routine dispatch state, not an error.
"""

import argparse
import contextlib
import errno
import fcntl
import json
import os
import socket
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from corral import (
    accounts,
    admission,
    board,
    disk,
    eligibility,
    hold,
    launch,
    mirror,
    paths,
    projects,
    prompts,
    quota,
    quota_ledger,
    registry,
    settings,
    sync,
    units,
    worktrees,
)
from corral import syncli
from corral import tracker as iface
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


def mint_agent(repo, number, *, host, now) -> str:
    """One fresh agent id for (`repo`, `number`). Pure.

    Opaque to every reader except debugging: only `is_managed_agent`
    parses it, and only for the prefix. The repo slash becomes a double
    dash so the id itself never contains one, and a host slash becomes a
    dash. `Claim.epoch` stays the fencing token, never this id.
    """
    owner = str(host).replace("/", "-") if host else "host"
    ticket = str(repo).replace("/", "--")
    return f"{MANAGED_PREFIX}{owner}-{int(now)}-{ticket}-{number}"


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


def _project_quota(project, pool):
    """One project's quota input from its own settings file and the pool.

    The freshness bound comes from the shared helper over the pool
    providers the project's chains name, never a second resolver.
    """
    resolved = settings.load(root=project.root)
    chains = dict(resolved.models)
    return QuotaSource(
        chains=chains,
        stale_after=quota.stale_after_for(chains, pool),
        low_pct=resolved["quota.low_pct"],
        unknown_blocks=resolved["quota.unknown_blocks"],
    )


def _store_labels(store, now):
    """A `read_labels` seam over cached store rows: reads, never a tracker.

    Only issue rows exist in the store (the reviewer pass has not
    populated linked pulls yet), so a pull read fails closed as
    unreadable, like a missing row: the hold gate skips instead of
    passing a ticket whose pull labels it could not see.
    """

    def read(kind, repo, number):
        if kind != "issue":
            raise board.StoreError(f"unknown item {repo}#{number}: no pull rows in the store")
        found, _ = store.item(repo, number, now=now)
        if found is None:
            raise board.StoreError(f"unknown item {repo}#{number}")
        return list(found.labels or [])

    return read


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
):
    """Shared setup for both passes: rows, projects, live state, ledger, limits.

    Returns ((items, registered, live_set, limits, hold_label,
    read_labels, ledger_data, quota_for, pool), None) on success, or
    (None, exit code) when the pass cannot proceed. A project whose
    policy or quota settings fail is not fatal here; that surfaces per
    project in `_gate_all`.
    """
    try:
        items = store.items()
    except board.StoreError as problem:
        print(f"corral drive: {problem}", file=err)
        return None, USAGE
    except sqlite3.Error as problem:
        print(f"corral drive: cannot read the store: {problem}", file=err)
        return None, USAGE
    try:
        registered = (projects_fn or projects.load_all)()
    except projects.ProjectsError as problem:
        print(f"corral drive: {problem}", file=err)
        return None, USAGE
    try:
        live_set = set(live) if live is not None else _default_live(registered)
    except settings.SettingsError as problem:
        print(f"corral drive: {problem}", file=err)
        return None, USAGE
    except Exception as problem:
        print(f"corral drive: cannot read live state: {problem}", file=err)
        return None, UNREACHABLE
    try:
        ledger_data = ledger if ledger is not None else quota_ledger.load()
    except quota_ledger.LedgerError as problem:
        print(f"corral drive: {problem}", file=err)
        return None, USAGE
    if limits is None or hold_label is None:
        try:
            default_limits, default_hold_label = _default_host_inputs(registered)
        except (settings.SettingsError, admission.LimitsError) as problem:
            print(f"corral drive: {problem}", file=err)
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
            print(f"corral drive: {problem}", file=err)
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
):
    """Eligibility plus the section 3 gate order over every candidate.

    Returns (gated_rows, problems): one `Gated` per candidate in section
    3 order, and one error line per project whose policy or quota
    settings failed. A project with no eligibility entry prints its
    logbook line to `out`. Nothing is written.
    """
    rows = eligibility.rows_from_items(items)
    by_key = {(row.repo, row.number): row for row in rows}
    waits = {(item.repo, item.number): item.tracker_updated_at for item in items}
    problems = []
    candidates = []
    for project in registered:
        if project.name not in quota_for:
            try:
                quota_for[project.name] = _project_quota(project, pool)
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
        )
        if verdict.code == admission.TOTAL_FULL:
            stop_starts = True
        gated_rows.append(verdict)
    return gated_rows, problems


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
) -> int:
    """One read-only pass: join eligibility to store rows, gate, print.

    Nothing is synced, claimed, enqueued, started, mirrored or journaled.
    A project whose policy or quota settings fail is skipped with one
    error line; the pass still gates every other project and exits 4.
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
    gated_rows, problems = _gate_all(
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
    )
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


def production_operators(registered, git):
    """`{tracker_repo: operators}` across every registered project's root.

    Resolves through the shared helper, the same way sync and mirror
    do, so the pass's sync, freshness re-reads and mirror agree with
    both CLIs on which authors are internal. Returns (mapping, None),
    or (None, problem) on the first configuration error.
    """
    mapping = {}
    for project in registered:
        try:
            names = settings.repo_names(project.root)
        except settings.SettingsError as err:
            return None, str(err)
        entries, problem = syncli.repo_operators(project.root, names, git)
        if problem is not None:
            return None, problem
        for _name, (tracker_repo, ops) in entries.items():
            mapping.setdefault(tracker_repo, ops)
    return mapping, None


def sync_all(store, tracker, *, operators, now):
    """Sync every repo with cached rows from the tracker.

    Returns (seen, errors): the rows applied and one line per failed
    repo. A failed repo keeps its rows and its cursor (that is
    `sync_repo`'s contract) and the pass moves on to the next repo.
    With no tracker there is nothing to sync from: no rows move and no
    errors are reported.
    """
    if tracker is None:
        return 0, []
    seen = 0
    errors = []
    for repo in sorted({item.repo for item in store.items()}):
        result = sync.sync_repo(
            store,
            tracker,
            repo,
            operators=list(mirror._ops_for(operators, repo)),
            now=now,
            cursor=store.get_cursor(repo, sync.CURSOR_NAME),
        )
        seen += result.items_seen
        errors.extend(result.errors)
    return seen, errors


def refresh_row(store, tracker, repo, number, *, operators, now, stale_after):
    """The row to decide from: cached when fresh, live-checked when stale.

    Returns (item, None) on the row to use, or (None, reason) when a
    stale row could not be re-read live, or when the store itself could
    not be read: the claim path fails closed instead of deciding from
    the stale row. With no tracker there is no live check to run, so
    the cached row stands.
    """
    ref = f"{repo}#{number}"
    try:
        found, fresh = store.item(repo, number, now=now, max_age=stale_after)
    except board.StoreError as err:
        return None, f"{ref}: store unreadable, claim refused ({err})"
    if fresh or tracker is None:
        return found, None
    try:
        live = tracker.item(repo, number)
    except iface.TrackerError as err:
        return None, f"{ref}: tracker unreachable, row stale, claim refused ({err})"
    try:
        store.upsert_item(
            sync._reading(live),
            operators=list(mirror._ops_for(operators, repo)),
            now=now,
        )
    except board.StoreError as err:
        return None, f"{ref}: store unreadable, claim refused ({err})"
    try:
        found, _ = store.item(repo, number, now=now)
    except board.StoreError as err:
        return None, f"{ref}: store unreadable, claim refused ({err})"
    if found is None:
        return None, f"{ref}: no store row, skipped as withdrawn"
    return found, None


def is_orphan(claim, *, now, gone_s, present_agents, live_units) -> bool:
    """Whether `claim` is provably dead under the section 4 rule. Pure.

    Dead means the store names a managed holder (an id this loop
    minted), the registry shows no live or stale entry for that id, no
    live unit exists for the ticket, and the claim is older than twice
    `gone_s`. `present_agents` is the ids with a live or stale registry
    entry; `live_units` is the (repo, number) pairs with a live unit.
    Anything less certain (a present agent, a live unit, a young claim)
    is left for stuck handling, and a foreign holder is never an
    orphan: the loop never touches a foreign claim.
    """
    if claim is None or not claim.agent:
        return False
    if not is_managed_agent(claim.agent):
        return False
    if claim.agent in present_agents:
        return False
    if (claim.repo, claim.number) in live_units:
        return False
    return now - claim.claimed_at > 2 * gone_s


def release_orphans(store, *, now, gone_s, present_agents, live_units, out) -> int:
    """Release every provably-dead claim, one logbook line each.

    Returns the released count. Each claim's agent and epoch go
    straight from the store row into `Store.release`, which releases
    only on a matching agent plus epoch and raises `Claimed`
    otherwise. A `Claimed` there means the row changed under the pass
    (re-claimed elsewhere), so it is no longer an orphan: the row is
    skipped with a logbook line, never retried in the same pass. A
    successful release clears the holder and leaves the epoch for the
    next claim to mint, which fences a stale holder still acting on the
    old epoch; the worktree and branch stay untouched. Rows with no
    claim are left alone: adoption happens only in resync, never here.
    """
    released = 0
    for item in store.items():
        claim = store.claim_of(item.repo, item.number)
        if claim is None:
            continue
        if not is_orphan(
            claim,
            now=now,
            gone_s=gone_s,
            present_agents=present_agents,
            live_units=live_units,
        ):
            continue
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
        released += 1
    return released


#: Automatic restarts per ticket in any sliding window.
RESTART_BUDGET = 3
RESTART_WINDOW_S = 24 * 60 * 60

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
    window. Only stop-plus-launch restarts are recorded, so only they
    count: fix-round resumes never consume the budget.
    """
    return len(prune_restarts(stamps, now=now)) < RESTART_BUDGET


def record_restart(stamps, *, now):
    """The stamps after recording one stop-plus-launch restart. Pure.

    Prunes expired stamps, then appends now. Call only for restarts that
    stopped and launched; anything else never consumes the budget.
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
    """
    if known and age < stale_s and unit_live:
        return "healthy"
    if age >= gone_s or (not unit_live and age >= stale_s):
        return "restart"
    return "watch"


def give_up_body(repo, number, agent, *, observed, restarts):
    """The decision-protocol comment for a ticket past the restart budget.

    Pure. The body carries the tail of what the pass observed, so the
    human taking over sees why the loop walked away.
    """
    ref = f"{repo}#{number}"
    return (
        f"corral drive gave up on {ref} (agent `{agent}`): {observed}. "
        f"It used {restarts} stop-plus-launch restarts in the last 24h "
        f"(budget {RESTART_BUDGET}). A human should take it from here; "
        "the loop released its claim."
    )


def release_finished(store, *, agent_states, now, out):
    """Release claims whose managed holder has a finished registry entry.

    Returns the released count. Runs before the orphan rule, so a
    finished worker always takes this completion path, never the orphan
    one. A successful release clears the holder and leaves the epoch for
    the next claim to mint, which fences the finished holder; the branch
    and PR stay untouched for a later review. A `Claimed` there
    means the row changed under the pass, so it is left held with a
    logbook line, never retried in the same pass. Foreign holders never
    reach here. Store errors propagate to the caller, like orphans.
    """
    released = 0
    for item in store.items():
        claim = store.claim_of(item.repo, item.number)
        if claim is None or not claim.agent or not is_managed_agent(claim.agent):
            continue
        seen = agent_states.get(claim.agent)
        if seen is None or seen[0] != registry.FINISHED:
            continue
        ref = f"{claim.repo}#{claim.number}"
        try:
            store.release(claim.repo, claim.number, claim.agent, claim.epoch, now=now)
        except board.Claimed:
            print(f"{ref}: finished release raced a re-claim, left held", file=out)
            continue
        print(
            f"{ref}: released finished worker `{claim.agent}`, branch and PR left for review",
            file=out,
        )
        released += 1
    return released


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
    tracker,
    restarts_path,
    out,
    err,
):
    """Restart or give up on stuck managed claims. Returns restarted count.

    Only managed holders older than this pass are considered: the pass
    never restarts, watches or gives up on what it just claimed, and
    foreign claims are never touched. A stuck row restarts (stop through
    the stop seam, launch again on the same claim and epoch reusing the
    identical agent id) while the restart budget allows it; past the
    budget the loop posts through the decision-write seam, then releases.
    A failed decision write keeps the claim for a later pass, and only
    stop-plus-launch restarts are recorded, so resumes never consume the
    budget. Store errors propagate to the caller, like orphans.
    """
    live = live_units if live_units is not None else frozenset()
    budgets = load_restarts(restarts_path) if restarts_path is not None else {}
    restarted = 0
    for item in store.items():
        claim = store.claim_of(item.repo, item.number)
        if claim is None or not claim.agent or not is_managed_agent(claim.agent):
            continue
        if claim.claimed_at >= now:
            continue
        ref = f"{claim.repo}#{claim.number}"
        seen = agent_states.get(claim.agent)
        if seen is None or not isinstance(seen[1], (int, float)):
            known, age = False, float("inf")
            observed = "no registry entry for the holder"
        else:
            known, age = True, now - seen[1]
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
        key = f"{claim.repo}#{claim.number}"
        stamps = budgets.get(key, [])
        if not restart_allowed(stamps, now=now):
            give_up(
                store,
                claim,
                tracker,
                observed=observed,
                restarts=len(prune_restarts(stamps, now=now)),
                now=now,
                out=out,
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
        try:
            store.claim(claim.repo, claim.number, claim.agent, host, now=now)
        except board.Claimed:
            print(f"{ref}: restart raced a re-claim, left held", file=out)
            continue
        except (board.ExternalItem, board.StoreError) as problem:
            print(f"{ref}: restart claim refused ({problem}), left held", file=out)
            continue
        try:
            stop_unit(claim.repo, claim.number)
        except Exception as problem:
            print(f"{ref}: restart stop failed ({problem}), claim kept", file=out)
            continue
        title = item.title if item.title else ref
        try:
            unit = launch(project, claim.repo, claim.number, "start", claim.agent, title, now=now)
        except Exception as problem:
            print(f"{ref}: restart launch failed ({problem}), claim kept", file=out)
            continue
        budgets[key] = record_restart(stamps, now=now)
        if restarts_path is not None:
            try:
                save_restarts(restarts_path, budgets)
            except OSError as problem:
                print(f"corral drive: cannot save the restart budget: {problem}", file=err)
        print(
            f"{ref}: restarted {unit} with the same agent id"
            f" (restart {len(budgets[key])} of {RESTART_BUDGET} in 24h)",
            file=out,
        )
        restarted += 1
    return restarted


def give_up(store, claim, tracker, *, observed, restarts, now, out):
    """Post the give-up comment and label, then release the claim.

    A failed decision write or a lost release race keeps the claim for a
    later pass. With no tracker the give-up is deferred, never released
    blind. One row only; the caller decides which rows give up.
    """
    ref = f"{claim.repo}#{claim.number}"
    if tracker is None:
        print(
            f"{ref}: past the restart budget, give-up deferred (no tracker), claim kept", file=out
        )
        return
    try:
        tracker.comment(
            claim.repo,
            claim.number,
            give_up_body(
                claim.repo, claim.number, claim.agent, observed=observed, restarts=restarts
            ),
        )
        tracker.add_label(claim.repo, claim.number, NEEDS_DECISION)
    except iface.TrackerError as problem:
        print(f"{ref}: give-up write failed ({problem}), claim kept", file=out)
        return
    try:
        store.release(claim.repo, claim.number, claim.agent, claim.epoch, now=now)
    except board.Claimed:
        print(f"{ref}: give-up release raced a re-claim, left held", file=out)
        return
    print(f"{ref}: past the restart budget, gave up and released the claim", file=out)


def production_stuck_inputs(registered, *, now):
    """`(agent_states, stale_s)` from the registry for the stuck sweep.

    `agent_states` maps every visible agent id to (state, last_seen);
    an id with no entry reads as gone. Raises SettingsError for bad
    registry settings.
    """
    cfg = registry.config()
    states = {}
    for entry, state in registry.visible(now, cfg):
        last = entry.get("last_seen") or entry.get("started_at") or 0
        states[entry["id"]] = (state, float(last))
    return states, cfg["stale_s"]


def production_stuck_project(registered, repo):
    """The registered project name owning tracker repo `repo`, or None.

    The first project whose ops root maps the repo wins; entries that
    do not map it are skipped. Pure settings reads, no subprocess when
    the entry names its tracker repo explicitly.
    """
    for project in registered:
        try:
            _local_repo(project.root, repo)
        except launch.LaunchError:
            continue
        return project.name
    return None


def production_stopper(registered):
    """The stuck step's stop seam over `launch.stop` with production I/O.

    Resolves the owning project per ticket, then stops the ticket's
    logged live units. Every argument the stop path needs is fixed here;
    tests inject a fake instead.
    """
    listings = lambda prefix: units.list_units(prefix, run=units.default_run)

    def stop_unit(repo, number):
        name = production_stuck_project(registered, repo)
        if name is None:
            raise units.UnitsError(f"no registered project feeds tracker repo {repo!r}")
        entry = next(item for item in registered if item.name == name)
        local = _local_repo(entry.root, repo)
        tgt = launch.target(entry.root, local)
        return launch.stop(tgt, number, list_fn=listings, stop_fn=units.systemctl_stop)

    return stop_unit


def _claim_and_launch(store, gated_rows, *, tracker, operators, launcher, host, now, stale_after):
    """Claim each proceeding candidate, launch each claim.

    Returns (rows, started, blocked): the verdict rows to print, the
    launched count, and whether a launch failed because the service
    manager was unreachable. Only `start` claims and launches, minting
    a fresh id with `mint_agent`; any other proceeding kind is left
    unclaimed with a skip line, since its scheduling belongs to a later
    ticket. A lost race (exit 2 from the store) becomes a skip naming
    the holder and the pass moves on; a launch through `launcher`
    appends the unit or the failure to the row's reason. With no
    `launcher` the pass stops after claiming and records that.
    """
    rows = []
    started = 0
    blocked = False
    for gated in gated_rows:
        if gated.verdict == "skip":
            rows.append(gated)
            continue
        ref = f"{gated.repo}#{gated.number}"
        if gated.kind != "start":
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict="skip",
                    reason=(
                        f"{ref}: {gated.kind} handoff is owned by a later ticket, left unclaimed"
                    ),
                    code="claim",
                )
            )
            continue
        fresh, problem = refresh_row(
            store,
            tracker,
            gated.repo,
            gated.number,
            operators=operators,
            now=now,
            stale_after=stale_after,
        )
        if problem is not None:
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict="skip",
                    reason=problem,
                    code="claim",
                )
            )
            continue
        agent = mint_agent(gated.repo, gated.number, host=host, now=now)
        try:
            store.claim(gated.repo, gated.number, agent, host, now=now)
        except board.Claimed:
            current, _ = store.item(gated.repo, gated.number, now=now)
            seen = current.agent if current is not None else ""
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict="skip",
                    reason=f"{ref}: race lost to `{seen}`, the pass moves on",
                    code="claim",
                )
            )
            continue
        except (board.ExternalItem, board.StoreError) as err:
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict="skip",
                    reason=f"{ref}: claim refused ({err})",
                    code="claim",
                )
            )
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
        title = fresh.title if fresh is not None and fresh.title else ref
        try:
            unit = launcher(
                gated.project,
                gated.repo,
                gated.number,
                gated.kind,
                agent,
                title,
                now=now,
            )
        except units.UnitExists as err:
            note = f"{gated.reason}; race lost, already live ({err})"
            code = "launch"
        except units.Unreachable as err:
            blocked = True
            note = f"{gated.reason}; not started: {err}"
            code = "launch"
        except Exception as err:
            note = f"{gated.reason}; not started: {err}"
            code = "launch"
        else:
            rows.append(
                Gated(
                    project=gated.project,
                    repo=gated.repo,
                    number=gated.number,
                    kind=gated.kind,
                    verdict=gated.kind,
                    reason=f"{gated.reason}; started {unit}",
                    code="",
                )
            )
            started += 1
            continue
        rows.append(
            Gated(
                project=gated.project,
                repo=gated.repo,
                number=gated.number,
                kind=gated.kind,
                verdict="skip",
                reason=note,
                code=code,
            )
        )
    return rows, started, blocked


def _local_repo(root, repo):
    """The `[[repo]]` entry name behind tracker repo `repo`.

    Only the entry whose resolved tracker repo names it wins: an entry
    name that merely equals `repo` is not a mapping, and a reported
    problem is surfaced, never masked. Anything else is a `LaunchError`
    naming the root and the repo.
    """
    for name in settings.repo_names(root):
        try:
            resolved = settings.load(root=root, repo=name)
        except settings.SettingsError as err:
            raise launch.LaunchError(str(err)) from None
        tracker_repo, problem = syncli._tracker_repo(root, name, resolved, worktrees.git)
        if problem is not None:
            raise launch.LaunchError(problem)
        if tracker_repo == repo:
            return name
    raise launch.LaunchError(f"{root}: no [[repo]] entry feeds tracker repo {repo!r}")


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


def production_orphan_inputs(registered, *, now):
    """`(present_agents, live_units, gone_s)` from the registry and units.

    `present_agents` is every agent id with a live or stale registry
    entry; `live_units` is every (repo, number) with a live unit under
    any registered prefix; `gone_s` is the registry setting of the same
    name, the single resolver for it. Raises the settings error when
    the registry settings are bad, and `units.Unreachable` when a unit
    listing fails.
    """
    cfg = registry.config()
    present = {
        entry["id"]
        for entry, state in registry.visible(now, cfg)
        if state in (registry.LIVE, registry.STALE)
    }
    tickets = set()
    for project in registered:
        listed = units.list_units(project.unit_prefix, run=units.default_run)
        tickets |= units.live_tickets(project.unit_prefix, listed)
    return present, tickets, cfg["gone_s"]


def production_launcher(registered):
    """The launch handoff over the existing launch path.

    Returns `launch_one(project, repo, number, kind, agent, title, *,
    now)`: the unit name of the started agent. Only `start` launches;
    any other kind stays with a later ticket. The target resolves from
    the project's ops root, the prompt renders the worker role with the
    board title, and the unit starts through `launch.launch` with the
    production seams. Failures raise with the public exit code.
    """

    def launch_one(project, repo, number, kind, agent, title, *, now):
        _ = (agent, now)
        if kind != "start":
            raise launch.LaunchError(
                f"{repo}#{number}: {kind} handoff is owned by a later ticket, claim kept"
            )
        entry = next((item for item in registered if item.name == project), None)
        if entry is None:
            raise launch.LaunchError(f"unknown project {project!r} for {repo}#{number}")
        local = _local_repo(entry.root, repo)
        tgt = launch.target(entry.root, local)
        factory = _worker_prompt(entry.root, tgt, number, title)
        unit, _ = launch.launch(
            tgt,
            number,
            factory,
            title=title,
            list_fn=lambda prefix: units.list_units(prefix, run=units.default_run),
            run=units.default_run,
            usage=disk.production_usage,
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
    tracker,
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
) -> int:
    """One claiming pass: sync, release orphans, gate, claim, launch, mirror, print.

    Orphan recovery runs on store, registry and unit state right after
    the sync, so freed rows gate as unclaimed in the same pass. A unit
    listing failure skips recovery only: the pass continues, since a
    claim the pass cannot prove dead is left held. A tracker outage
    aborts the sync step only: prior rows and the old cursor stand, and
    the pass continues on store data. A claim whose stale row cannot be
    re-read live fails closed instead. The mirror drains whatever the
    outbox holds, old backlog and new claims alike; what it cannot push
    stays queued for the next healthy pass. A pass that synced nothing
    at all exits 3; a pass that launched nothing because the service
    manager was unreachable does too.
    """
    try:
        seen, sync_errors = sync_all(store, tracker, operators=operators, now=now)
    except board.StoreError as problem:
        print(f"corral drive: {problem}", file=err)
        return USAGE
    except sqlite3.Error as problem:
        print(f"corral drive: cannot read the store: {problem}", file=err)
        return USAGE
    for line in sync_errors:
        print(f"corral drive: {line}", file=err)
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
    )
    if fatal is not None:
        return fatal
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
    skip_orphans = False
    if gone_s is None:
        try:
            present_agents, live_units, gone_s = production_orphan_inputs(registered, now=now)
        except settings.SettingsError as problem:
            print(f"corral drive: {problem}", file=err)
            return USAGE
        except units.Unreachable as problem:
            print(
                f"corral drive: cannot read live units, orphan recovery skipped ({problem})",
                file=err,
            )
            skip_orphans = True
    else:
        present_agents = frozenset(present_agents or ())
        live_units = frozenset(live_units or ())
    if agent_states is not None:
        try:
            release_finished(store, agent_states=agent_states, now=now, out=out)
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"corral drive: finished reconciliation skipped ({problem})", file=err)
        try:
            _items = store.items()
        except board.StoreError as problem:
            print(f"corral drive: {problem}", file=err)
            return USAGE
        except sqlite3.Error as problem:
            print(f"corral drive: cannot read the store: {problem}", file=err)
            return USAGE
    if not skip_orphans:
        try:
            release_orphans(
                store,
                now=now,
                gone_s=gone_s,
                present_agents=present_agents,
                live_units=live_units,
                out=out,
            )
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"corral drive: orphan recovery skipped ({problem})", file=err)
        try:
            _items = store.items()
        except board.StoreError as problem:
            print(f"corral drive: {problem}", file=err)
            return USAGE
        except sqlite3.Error as problem:
            print(f"corral drive: cannot read the store: {problem}", file=err)
            return USAGE
    gated_rows, problems = _gate_all(
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
    )
    if host is None:
        host = socket.gethostname()
    if stale_after is None and tracker is not None:
        try:
            stale_after = settings.load(root=None)["board.item_max_age_s"]
        except settings.SettingsError as problem:
            print(f"corral drive: {problem}", file=err)
            return USAGE
    final_rows, started, blocked = _claim_and_launch(
        store,
        gated_rows,
        tracker=tracker,
        operators=operators,
        launcher=launcher,
        host=host,
        now=now,
        stale_after=stale_after,
    )
    if agent_states is not None:
        defaults = registry.default_config()
        stale = stale_s if stale_s is not None else defaults["stale_s"]
        gone = gone_s if gone_s is not None else defaults["gone_s"]
        launch = restarter if restarter is not None else launcher
        try:
            handle_stuck(
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
                tracker=tracker,
                restarts_path=restarts_path,
                out=out,
                err=err,
            )
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"corral drive: stuck handling skipped ({problem})", file=err)
    if tracker is not None:
        try:
            outcome = mirror.push(store, tracker, operators=operators, now=now)
        except board.StoreError as problem:
            print(f"corral drive: {problem}", file=err)
            return USAGE
        for line in outcome.errors:
            print(f"corral drive: {line}", file=err)
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
        return USAGE
    if sync_errors and seen == 0:
        return UNREACHABLE
    if blocked and started == 0:
        return UNREACHABLE
    return OK


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
    tracker=None,
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
) -> int:
    """One pass: gate and print, or sync, claim, launch, mirror and print.

    `--plan` holds no lock and writes nothing: no sync, orphan or
    finished release, claim, mirror, launch, restart or give-up, whatever
    seams are passed. The non-plan pass takes the drive lock, reconciles
    finished claims, releases provably-dead claims, and claims through
    the store transaction, then launches through `launcher` (the
    existing launch path in production; with no launcher the pass stops
    after claiming and records that), handles stuck claims, and drains
    the mirror backlog through `tracker`. `tracker` is the injected
    tracker double; with none there is no sync, no freshness re-read, no
    mirror and no give-up post. `operators` maps tracker repos to
    operator logins for the sync and mirror steps; `host` names this
    host on its claims; `stale_after` bounds the cached rows a claim may
    use without a live re-read. `present_agents` (ids with a live or
    stale registry entry), `live_units` ((repo, number) pairs with a
    live unit) and `gone_s` feed orphan recovery; `gone_s=None` reads
    all three from the host instead. The stuck sweep runs only when
    `agent_states` (ids to (state, last_seen)) is given: `stale_s`
    bounds its heartbeats, `stop_unit(repo, number)` stops a ticket, the
    restarter (or `launcher` when None) relaunches it, `restarts_path`
    persists the budget (in memory when None), and
    `stuck_project(repo, number)` names the owning project. With no stop
    or launch seam a stuck row is left held; a failed seam keeps the
    claim. `now` defaults to the wall clock; every other None means the
    host default, so tests inject plain data or fakes at those seams and
    production reads the host. Returns the exit code.
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
        "pool": pool,
    }
    if plan:
        return _pass(store, out, err, label="plan", **args)
    try:
        with drive_lock(lock_path):
            return _launching_pass(
                store,
                out,
                err,
                tracker=tracker,
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
                **args,
            )
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
        if args.plan:
            return run_pass(
                plan=True,
                store=store,
                lock_path=lock_path(),
                out=sys.stdout,
                err=sys.stderr,
            )
        try:
            registered = projects.load_all()
        except projects.ProjectsError as problem:
            print(f"corral drive: {problem}", file=sys.stderr)
            return USAGE
        operators, problem = production_operators(registered, syncli._git)
        if problem is not None:
            print(f"corral drive: {problem}", file=sys.stderr)
            return USAGE
        try:
            stuck_states, stuck_stale = production_stuck_inputs(registered, now=time.time())
        except settings.SettingsError as problem:
            print(f"corral drive: {problem}", file=sys.stderr)
            return USAGE
        return run_pass(
            plan=False,
            store=store,
            lock_path=lock_path(),
            out=sys.stdout,
            err=sys.stderr,
            projects_fn=lambda: registered,
            tracker=syncli._production_tracker(),
            operators=operators,
            launcher=production_launcher(registered),
            agent_states=stuck_states,
            stale_s=stuck_stale,
            stop_unit=production_stopper(registered),
            restarts_path=restarts_path(),
            stuck_project=lambda repo, number: production_stuck_project(registered, repo),
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
