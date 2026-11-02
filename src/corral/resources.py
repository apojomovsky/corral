"""Host resource budgets: one pure admission decision over CPU, memory and pids.

A host budget is divided per project and charged per agent role, enforced
by the kernel through nested slices. This module only decides: plain data
in, a verdict with a reason string out, shaped like ``admission.admit``.
No subprocess, no clock reads, no state dir; tests pass every number in.

Each project is guaranteed its floor; the remainder of the host budget
splits by weight; a ceiling caps even an idle neighbor. Each launch is
charged by role, and foreign claims subtract from the pool exactly like a
project share until they expire under ``gone_s``. A project below its
floor starts past a full pool, the same rule as the count floor in
``admission.admit``: the floor is a guarantee, not a reservation.

Every refusal names its level (host, project or role cost) with the
numbers, and maps to exit 75 at the launch seam (no free slot, try later).
No host budget configured means no enforcement: the gate passes everything.
Configuration mistakes raise ``LimitsError`` (exit 4).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Verdict codes. Stable strings: callers branch on them.
OK = "ok"
HOST_FULL = "host-full"
PROJECT_SHARE = "project-share"
PROJECT_CEILING = "project-ceiling"
ROLE_COST = "role-cost"

#: Seconds a foreign claim counts against the pool after it is declared.
DEFAULT_GONE_S = 600.0

#: The resource dimensions, in verdict and reason order.
DIMS = ("cpu_m", "memory_mb", "pids")


class LimitsError(Exception):
    """A budget table is unusable: negative, over-ceiling, or over-host (exit 4)."""

    exit_code = 4


@dataclass(frozen=True)
class Usage:
    """One triple of CPU millicores, memory megabytes and pids."""

    cpu_m: int = 0
    memory_mb: int = 0
    pids: int = 0


@dataclass(frozen=True)
class Share:
    """One project's portion of the host budget.

    ``weight`` splits the remainder after every floor; zero keeps only
    the floor. A missing ceiling means no cap beyond the weight share.
    """

    weight: float = 1.0
    cpu_floor_m: int = 0
    memory_floor_mb: int = 0
    pids_floor: int = 0
    cpu_ceiling_m: int | None = None
    memory_ceiling_mb: int | None = None
    pids_ceiling: int | None = None


@dataclass(frozen=True)
class ForeignClaim:
    """Load corral does not control, declared by hand with an explicit reservation."""

    name: str = ""
    cpu_m: int = 0
    memory_mb: int = 0
    pids: int = 0
    claimed_at: float = 0.0


@dataclass(frozen=True)
class Request:
    """One admission question: who wants to launch, and in which role."""

    project: str = ""
    role: str = "worker"


@dataclass(frozen=True)
class Charged:
    """Charged usage: the summed role costs of live agents, total and per project."""

    total: Usage | Mapping[str, object] = field(default_factory=Usage)
    by_project: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Limits:
    """The host budget, the project shares, the role charges and the foreign claims.

    ``host`` None means no budget is configured: the gate passes
    everything. ``gone_s`` bounds foreign claims: a claim older than it
    is ignored.
    """

    host: Usage | Mapping[str, object] | None = None
    projects: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    foreign: list = field(default_factory=list)
    gone_s: float = DEFAULT_GONE_S


@dataclass(frozen=True)
class Verdict:
    """One admission answer, with the usage it was decided from."""

    allowed: bool
    code: str
    reason: str
    charged: Usage
    project: Usage
    cost: Usage


def _amount(value, what):
    """A whole resource amount of zero or more, else a LimitsError naming it."""
    if isinstance(value, bool):
        raise LimitsError(f"{what} must be a whole number of zero or more, not {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise LimitsError(f"{what} must be a whole number of zero or more, not {value!r}")
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise LimitsError(f"{what} must be a whole number of zero or more, not {value!r}")
    return value


def _weight(value, what):
    """A share weight of zero or more, else a LimitsError naming it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitsError(f"{what} must be a number of zero or more, not {value!r}")
    if not math.isfinite(value) or value < 0:
        raise LimitsError(f"{what} must be a number of zero or more, not {value!r}")
    return value


def _moment(value, what):
    """A timestamp as seconds, else a LimitsError naming it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitsError(f"{what} must be a number of seconds, not {value!r}")
    if not math.isfinite(value):
        raise LimitsError(f"{what} must be a number of seconds, not {value!r}")
    return value


def _usage(value, what):
    """A Usage from a Usage, a plain mapping, or None (zeros)."""
    if value is None:
        return Usage()
    if isinstance(value, Usage):
        for dim in DIMS:
            _amount(getattr(value, dim), f"{what} {dim}")
        return value
    if isinstance(value, Mapping):
        try:
            return Usage(
                cpu_m=_amount(value.get("cpu_m", 0), f"{what} cpu_m"),
                memory_mb=_amount(value.get("memory_mb", 0), f"{what} memory_mb"),
                pids=_amount(value.get("pids", 0), f"{what} pids"),
            )
        except LimitsError:
            raise
        except Exception as err:
            raise LimitsError(f"{what} must be a usage triple, not {value!r}: {err}") from None
    raise LimitsError(f"{what} must be a usage triple, not {value!r}")


def _share(value, what):
    """A Share from a Share or a plain mapping."""
    if isinstance(value, Share):
        _weight(value.weight, f"{what} weight")
        for dim in DIMS:
            _amount(_floor(value, dim), f"{what} {_FLOOR_KEYS[dim]}")
            ceiling = _ceiling_of(value, dim)
            if ceiling is not None:
                _amount(ceiling, f"{what} {_CEIL_KEYS[dim]}")
        return value
    if isinstance(value, Mapping):
        try:
            return Share(
                weight=_weight(value.get("weight", 1), f"{what} weight"),
                cpu_floor_m=_amount(value.get("cpu_floor_m", 0), f"{what} cpu_floor_m"),
                memory_floor_mb=_amount(value.get("memory_floor_mb", 0), f"{what} memory_floor_mb"),
                pids_floor=_amount(value.get("pids_floor", 0), f"{what} pids_floor"),
                cpu_ceiling_m=_optional_amount(value.get("cpu_ceiling_m"), f"{what} cpu_ceiling_m"),
                memory_ceiling_mb=_optional_amount(
                    value.get("memory_ceiling_mb"), f"{what} memory_ceiling_mb"
                ),
                pids_ceiling=_optional_amount(value.get("pids_ceiling"), f"{what} pids_ceiling"),
            )
        except LimitsError:
            raise
        except Exception as err:
            raise LimitsError(f"{what} must be a project share, not {value!r}: {err}") from None
    raise LimitsError(f"{what} must be a project share, not {value!r}")


def _optional_amount(value, what):
    """An optional ceiling: None, else a whole amount of zero or more."""
    if value is None:
        return None
    return _amount(value, what)


def _claim(value, what):
    """A ForeignClaim from a ForeignClaim or a plain mapping."""
    if isinstance(value, ForeignClaim):
        item = value
    elif isinstance(value, Mapping):
        try:
            name = value.get("name", "")
            item = ForeignClaim(
                name=name if isinstance(name, str) else "",
                cpu_m=_amount(value.get("cpu_m", 0), f"{what} cpu_m"),
                memory_mb=_amount(value.get("memory_mb", 0), f"{what} memory_mb"),
                pids=_amount(value.get("pids", 0), f"{what} pids"),
                claimed_at=_moment(value.get("claimed_at", 0.0), f"{what} claimed_at"),
            )
        except LimitsError:
            raise
        except Exception as err:
            raise LimitsError(f"{what} must be a foreign claim, not {value!r}: {err}") from None
    else:
        raise LimitsError(f"{what} must be a foreign claim, not {value!r}")
    if not item.name:
        raise LimitsError(f"{what} needs a non-empty name, not {item.name!r}")
    _moment(item.claimed_at, f"{what} claimed_at")
    for dim in DIMS:
        _amount(getattr(item, dim), f"{what} {dim}")
    return item


def _request(value):
    """A Request from a Request or a plain mapping."""
    if isinstance(value, Request):
        return value
    if isinstance(value, Mapping):
        project = value.get("project", "")
        role = value.get("role", "worker")
        if not isinstance(project, str) or not isinstance(role, str):
            raise LimitsError(f"request must name a project and a role, not {value!r}")
        return Request(project=project, role=role or "worker")
    raise LimitsError(f"request must name a project and a role, not {value!r}")


def _n(value):
    """A number for a reason string: whole floats read as ints."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


#: The settings-style key holding each dimension's floor and ceiling.
_FLOOR_KEYS = {"cpu_m": "cpu_floor_m", "memory_mb": "memory_floor_mb", "pids": "pids_floor"}
_CEIL_KEYS = {"cpu_m": "cpu_ceiling_m", "memory_mb": "memory_ceiling_mb", "pids": "pids_ceiling"}


def _floor(share, dim):
    return getattr(share, _FLOOR_KEYS[dim])


def _ceiling_of(share, dim):
    return getattr(share, _CEIL_KEYS[dim])


def _at(usage, dim):
    return getattr(usage, dim)


def validate_limits(limits):
    """Raise LimitsError when ``limits`` cannot admit anything sane.

    Negative charges or budgets; a floor above its ceiling; floors
    summing above the host budget; a malformed foreign claim; a negative
    ``gone_s``. A weight of zero with a positive floor is allowed: floor
    only, no share of the remainder. Each message names the offending
    numbers.
    """
    if not isinstance(limits, Limits):
        if isinstance(limits, Mapping):
            limits = Limits(
                host=limits.get("host"),
                projects=dict(limits.get("projects") or {}),
                roles=dict(limits.get("roles") or {}),
                foreign=list(limits.get("foreign") or []),
                gone_s=limits.get("gone_s", DEFAULT_GONE_S),
            )
        else:
            raise LimitsError(f"limits must be a Limits table, not {limits!r}")
    gone_s = _moment(limits.gone_s, "gone_s")
    if gone_s < 0:
        raise LimitsError(f"gone_s {limits.gone_s!r} must be zero or more")
    host = None
    if limits.host is not None:
        host = _usage(limits.host, "host budget")
    shares = {}
    for name in limits.projects:
        shares[name] = _share(limits.projects[name], f"project {name}")
    for role in limits.roles:
        _usage(limits.roles[role], f"role {role}")
    for index, entry in enumerate(limits.foreign):
        _claim(entry, f"foreign claim {index}")
    for name in sorted(shares):
        share = shares[name]
        for dim in DIMS:
            ceiling = _ceiling_of(share, dim)
            if ceiling is not None and _floor(share, dim) > ceiling:
                raise LimitsError(
                    f"project {name} {dim} floor {_floor(share, dim)} "
                    f"is above its ceiling {ceiling}"
                )
    if host is not None:
        for dim in DIMS:
            floors = sum(_floor(share, dim) for share in shares.values())
            if floors > _at(host, dim):
                raise LimitsError(
                    f"project floors sum to {floors} {dim}, "
                    f"above host budget {dim} {_at(host, dim)}"
                )


def _live_foreign(foreign, now, gone_s):
    """The claims no older than ``gone_s`` at ``now``."""
    return [entry for entry in foreign if now - entry.claimed_at <= gone_s]


def admit(request, charged, limits, now):
    """Whether ``request`` may start now against ``charged`` under ``limits``.

    Pure: the caller passes the charged usage and the clock reading in,
    so no sum here can go stale. A missing host budget passes everything.
    Ceilings hold always; a project below its floor in every full
    dimension starts past a full pool; a charge bigger than the pool is a
    role-cost refusal even on an empty host.
    """
    request = _request(request)
    if not isinstance(charged, Charged):
        if isinstance(charged, Mapping):
            charged = Charged(
                total=charged.get("total"),
                by_project=dict(charged.get("by_project") or {}),
            )
        else:
            raise LimitsError(f"charged usage must be a Charged table, not {charged!r}")
    if not isinstance(limits, Limits):
        if isinstance(limits, Mapping):
            limits = Limits(
                host=limits.get("host"),
                projects=dict(limits.get("projects") or {}),
                roles=dict(limits.get("roles") or {}),
                foreign=list(limits.get("foreign") or []),
                gone_s=limits.get("gone_s", DEFAULT_GONE_S),
            )
        else:
            raise LimitsError(f"limits must be a Limits table, not {limits!r}")
    now = _moment(now, "now")
    gone_s = _moment(limits.gone_s, "gone_s")
    if gone_s < 0:
        raise LimitsError(f"gone_s {limits.gone_s!r} must be zero or more")
    total = _usage(charged.total, "charged total")
    by_project = {
        name: _usage(charged.by_project[name], f"charged project {name}")
        for name in charged.by_project
    }
    described = f"launch {request.project}/{request.role}"
    if request.role not in limits.roles:
        raise LimitsError(f"unknown role {request.role!r}: no charge is configured for it")
    cost = _usage(limits.roles[request.role], f"role {request.role}")
    foreign = [
        _claim(entry, f"foreign claim {index}") for index, entry in enumerate(limits.foreign)
    ]
    live = _live_foreign(foreign, now, gone_s)
    foreign_used = Usage(
        cpu_m=sum(entry.cpu_m for entry in live),
        memory_mb=sum(entry.memory_mb for entry in live),
        pids=sum(entry.pids for entry in live),
    )
    used = by_project.get(request.project, Usage())
    if limits.host is None:
        return Verdict(
            True,
            OK,
            f"{described} allowed: no host budget configured, the budget gate passes everything",
            total,
            used,
            cost,
        )
    host = _usage(limits.host, "host budget")
    avail = Usage(
        cpu_m=_at(host, "cpu_m") - foreign_used.cpu_m,
        memory_mb=_at(host, "memory_mb") - foreign_used.memory_mb,
        pids=_at(host, "pids") - foreign_used.pids,
    )
    for dim in DIMS:
        if _at(cost, dim) > _at(avail, dim):
            return Verdict(
                False,
                ROLE_COST,
                f"{described} refused: {request.role} costs {_n(_at(cost, dim))} {dim}, "
                f"above {_n(_at(avail, dim))} available "
                f"({_n(_at(host, dim))} host less {_n(_at(foreign_used, dim))} foreign)",
                total,
                used,
                cost,
            )
    table = {name: _share(entry, f"project {name}") for name, entry in limits.projects.items()}
    share = table.get(request.project, Share())
    ceiling_hit = next(
        (
            dim
            for dim in DIMS
            if _ceiling_of(share, dim) is not None
            and _at(used, dim) + _at(cost, dim) > _ceiling_of(share, dim)
        ),
        None,
    )
    if ceiling_hit is not None:
        ceiling = _ceiling_of(share, ceiling_hit)
        return Verdict(
            False,
            PROJECT_CEILING,
            f"{described} refused: {request.project} at {_n(_at(used, ceiling_hit))} "
            f"of {_n(ceiling)} {ceiling_hit} ceiling",
            total,
            used,
            cost,
        )
    weights = sum(entry.weight for entry in table.values())
    if request.project not in table:
        weights += 1.0
    floors = {dim: sum(_floor(entry, dim) for entry in table.values()) for dim in DIMS}
    caps = {}
    for dim in DIMS:
        remainder = _at(host, dim) - floors[dim] - _at(foreign_used, dim)
        cap = _floor(share, dim) + max(0, remainder) * (share.weight / weights if weights else 0)
        ceiling = _ceiling_of(share, dim)
        caps[dim] = min(cap, ceiling) if ceiling is not None else cap
    over_share = next((dim for dim in DIMS if _at(used, dim) + _at(cost, dim) > caps[dim]), None)
    if over_share is not None:
        return Verdict(
            False,
            PROJECT_SHARE,
            f"{described} refused: {request.project} over its {_n(caps[over_share])} "
            f"{over_share} share (floor {_n(_floor(share, over_share))}, "
            f"weight {_n(share.weight)} of {_n(weights)})",
            total,
            used,
            cost,
        )
    full = [dim for dim in DIMS if _at(total, dim) + _at(cost, dim) > _at(avail, dim)]
    if full and not all(_at(used, dim) < _floor(share, dim) for dim in full):
        names = ", ".join(
            f"{dim} ({_n(_at(total, dim))} plus {_n(_at(cost, dim))} "
            f"above {_n(_at(avail, dim))} available)"
            for dim in full
        )
        return Verdict(
            False,
            HOST_FULL,
            f"{described} refused: host full for {names}; "
            f"{request.project} at {_n(_at(used, full[0]))} "
            f"{full[0]}, floor {_n(_floor(share, full[0]))}",
            total,
            used,
            cost,
        )
    detail = ""
    if full:
        detail = (
            "; "
            + request.project
            + " below its floor in "
            + ", ".join(f"{dim} ({_n(_at(used, dim))} of {_n(_floor(share, dim))})" for dim in full)
        )
    return Verdict(
        True,
        OK,
        f"{described} allowed: "
        + ", ".join(
            f"{dim} {_n(_at(used, dim) + _at(cost, dim))} of {_n(_at(avail, dim))}" for dim in DIMS
        )
        + " within host, share and ceiling"
        + detail,
        total,
        used,
        cost,
    )
