"""Admission: one pure decision over the host total, floors and ceilings.

A host has a total cap, and each project a floor and a ceiling. With the
total reached by one project, another gets only its floor. Admission is
decided against live state, never a cached count: every start, skip and
resume is explained by the returned verdict.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from corral import settings

#: Slots a host admits in total before floors and forced starts apply.
DEFAULT_TOTAL_CAP = 4
#: Slots admitted per repo; zero means no per-repo cap.
DEFAULT_REPO_CAP = 0

settings.register(
    "admission.total_cap",
    int,
    DEFAULT_TOTAL_CAP,
    "live plus pending agents the host admits in total",
)
settings.register(
    "admission.repo_cap", int, DEFAULT_REPO_CAP, "live plus pending agents per repo (0 means none)"
)

#: Verdict codes. Stable strings: callers branch on them.
OK = "ok"
PROJECT_CEILING = "project-ceiling"
REPO_CAP = "repo-cap"
TOTAL_FULL = "total-full"


class LimitsError(Exception):
    """A limits table is unusable: negative, over-ceiling, or over-total (exit 4)."""

    exit_code = 4


@dataclass(frozen=True)
class Request:
    """One admission question: who wants a slot, for which ticket, and how."""

    project: str
    repo: str
    ticket: int
    kind: str = "start"
    dispatch_now: bool = False


@dataclass(frozen=True)
class State:
    """Live and pending occupants, each a ``(project, repo, ticket)`` triple.

    A ticket in both counts once. A pending resume holds a slot. A ticket
    in review with no agent is in neither list. ``forced`` holds the live
    or pending tickets that started with ``dispatch_now``; each raises the
    effective total by one, so a forced start never consumes a normal slot.
    """

    live: Sequence[tuple[str, str, int]] = ()
    pending: Sequence[tuple[str, str, int]] = ()
    forced: Sequence[tuple[str, str, int]] = ()


@dataclass(frozen=True)
class Limits:
    """The host total, the per-repo cap, and each known project's floor and ceiling.

    ``projects`` maps a name to ``{"floor": int, "ceiling": int | None}``;
    a missing project means floor zero and no ceiling. ``repo_cap`` zero
    means no per-repo cap. Per-project floors and ceilings come from the
    caller; only the host totals live in settings.
    """

    total: int = DEFAULT_TOTAL_CAP
    projects: dict = field(default_factory=dict)
    repo_cap: int = DEFAULT_REPO_CAP


@dataclass(frozen=True)
class Verdict:
    """One admission answer, with the counts it was decided from."""

    allowed: bool
    code: str
    reason: str
    total: int
    effective: int
    project: int
    repo: int


def _occupants(state):
    """Live plus pending tickets, each counted once."""
    return {tuple(entry) for entry in list(state.live) + list(state.pending)}


def _bounds(entry):
    """A project's ``(floor, ceiling)``: zero and none when unknown."""
    entry = entry or {}
    return entry.get("floor", 0), entry.get("ceiling")


def admit(request, state, limits):
    """Whether ``request`` may start now against ``state`` under ``limits``.

    Pure: the caller passes the live state in, so no count here can go
    stale. The project ceiling and the repo cap hold even for
    ``dispatch_now``; only the total check is skipped by it. Past a full
    total, a project below its floor is still allowed: that is the floor.
    """
    occupants = _occupants(state)
    forced = {tuple(entry) for entry in state.forced} & occupants
    used = len(occupants)
    effective = limits.total + len(forced)
    project_used = sum(1 for entry in occupants if entry[0] == request.project)
    repo_used = sum(1 for entry in occupants if entry[1] == request.repo)
    floor, ceiling = _bounds(limits.projects.get(request.project))
    described = f"{request.kind} {request.project} {request.repo}#{request.ticket}"

    if ceiling is not None and project_used >= ceiling:
        return Verdict(
            False,
            PROJECT_CEILING,
            f"{described} refused: {request.project} at {project_used} of {ceiling} ceiling",
            used,
            effective,
            project_used,
            repo_used,
        )
    if limits.repo_cap and repo_used >= limits.repo_cap:
        return Verdict(
            False,
            REPO_CAP,
            f"{described} refused: {request.repo} at {repo_used} of {limits.repo_cap} repo cap",
            used,
            effective,
            project_used,
            repo_used,
        )
    if not request.dispatch_now and used >= effective:
        if project_used < floor:
            return Verdict(
                True,
                OK,
                f"{described} allowed: total full ({used} of {effective}) "
                f"but {request.project} below its floor "
                f"({project_used} of {floor})",
                used,
                effective,
                project_used,
                repo_used,
            )
        return Verdict(
            False,
            TOTAL_FULL,
            f"{described} refused: host full ({used} of {effective}); "
            f"{request.project} at {project_used}, floor {floor}",
            used,
            effective,
            project_used,
            repo_used,
        )
    return Verdict(
        True,
        OK,
        f"{described} allowed: {used} of {effective} slots taken",
        used,
        effective,
        project_used,
        repo_used,
    )


def validate_limits(limits):
    """Raise LimitsError when ``limits`` cannot admit anything sane.

    Floors summing above the total promise slots that do not exist; a
    floor above its ceiling admits nothing; a negative number is never a
    cap. Each message names the offending numbers.
    """
    if limits.total < 0:
        raise LimitsError(f"admission.total {limits.total} must be zero or more")
    if limits.repo_cap < 0:
        raise LimitsError(f"admission.repo_cap {limits.repo_cap} must be zero or more")
    floors = 0
    for name in sorted(limits.projects):
        floor, ceiling = _bounds(limits.projects[name])
        if floor < 0:
            raise LimitsError(f"project {name} floor {floor} must be zero or more")
        if ceiling is not None and ceiling < 0:
            raise LimitsError(f"project {name} ceiling {ceiling} must be zero or more")
        if ceiling is not None and floor > ceiling:
            raise LimitsError(f"project {name} floor {floor} is above its ceiling {ceiling}")
        floors += floor
    if floors > limits.total:
        raise LimitsError(f"project floors sum to {floors}, above admission.total {limits.total}")
