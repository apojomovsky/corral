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

The same triples feed the read-only side: host pressure parsed from the
kernel files, per-slice use parsed from the leaf files, and the capacity
report the hub learns through. The report is built from plain data only
and never touches the ledger: the hub never reads or writes it.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from corral import pressure, prompts, settings
from corral.pressure import saturated as _saturated_reading

#: Verdict codes. Stable strings: callers branch on them.
OK = "ok"
HOST_FULL = "host-full"
PROJECT_SHARE = "project-share"
PROJECT_CEILING = "project-ceiling"
ROLE_COST = "role-cost"
PRESSURE = "pressure"

#: Seconds a foreign claim counts against the pool after it is declared.
DEFAULT_GONE_S = 600.0

settings.register(
    "resources.gone_s",
    int,
    int(DEFAULT_GONE_S),
    "seconds a foreign claim counts against the pool",
)

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


#: A foreign claim without a `claimed_at` reads as epoch zero: older
#: than any positive `gone_s`, so the gate expires it at once. The one
#: owner of the default the dataclass, the gate (`_claim`), and the
#: doctor row (`resourcedoctor._parse_claim`) all read through.
DEFAULT_CLAIMED_AT = 0.0


@dataclass(frozen=True)
class ForeignClaim:
    """Load corral does not control, declared by hand with an explicit reservation."""

    name: str = ""
    cpu_m: int = 0
    memory_mb: int = 0
    pids: int = 0
    claimed_at: float = DEFAULT_CLAIMED_AT


@dataclass(frozen=True)
class Request:
    """One admission question: who wants to launch, and in which role."""

    project: str = ""
    role: str = "worker"


@dataclass(frozen=True)
class PressureCaps:
    """The saturation line: refuse new launches past it, even with shares free.

    Unknown readings never trip it. Both numbers come from the
    ``[resources.pressure]`` table, else the conservative built-ins.
    """

    max_load_per_cpu: float = pressure.DEFAULT_MAX_LOAD_PER_CPU
    min_mem_available_pct: float = pressure.DEFAULT_MIN_MEM_AVAILABLE_PCT


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
    is ignored. ``pressure`` is the saturation line, None reading as the
    conservative built-ins.
    """

    host: Usage | Mapping[str, object] | None = None
    projects: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    foreign: list = field(default_factory=list)
    gone_s: float = DEFAULT_GONE_S
    pressure: PressureCaps | Mapping[str, object] | None = None


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


def _table(value, what):
    """A project or role table: a mapping of names to entries, else LimitsError.

    A missing table reads as empty; any other non-mapping (a list, a
    string, a number, None) is a configuration error, never the bare
    TypeError `dict` would raise.
    """
    if not isinstance(value, Mapping):
        raise LimitsError(f"limits {what} must be a mapping, not {value!r}")
    return dict(value)


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
        except Exception as err:  # noqa: BLE001 - a malformed table reads as LimitsError
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
        except Exception as err:  # noqa: BLE001 - a malformed table reads as LimitsError
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
                claimed_at=_moment(
                    value.get("claimed_at", DEFAULT_CLAIMED_AT), f"{what} claimed_at"
                ),
            )
        except LimitsError:
            raise
        except Exception as err:  # noqa: BLE001 - a malformed table reads as LimitsError
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
        role = value.get("role", prompts.DEFAULT_ROLE)
        if not isinstance(project, str) or not isinstance(role, str):
            raise LimitsError(f"request must name a project and a role, not {value!r}")
        return Request(project=project, role=role)
    raise LimitsError(f"request must name a project and a role, not {value!r}")


def _threshold(value, what, *, low, high=None, above_zero=False):
    """A pressure threshold: a finite number inside its range, else LimitsError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitsError(f"{what} must be a number, not {value!r}")
    if not math.isfinite(value):
        raise LimitsError(f"{what} must be a finite number, not {value!r}")
    number = float(value)
    if above_zero and number <= 0:
        raise LimitsError(f"{what} must be above zero, not {value!r}")
    if number < low or (high is not None and number > high):
        bound = f"{low} to {high}" if high is not None else f"{low} or more"
        raise LimitsError(f"{what} must be {bound}, not {value!r}")
    return number


def _pressure_caps(value, what="pressure caps"):
    """PressureCaps from caps, a plain mapping, or None (the built-ins)."""
    if value is None:
        return PressureCaps()
    if isinstance(value, PressureCaps):
        _threshold(value.max_load_per_cpu, f"{what} max_load_per_cpu", low=0, above_zero=True)
        _threshold(value.min_mem_available_pct, f"{what} min_mem_available_pct", low=0, high=100)
        return value
    if isinstance(value, Mapping):
        for key in value:
            if key not in ("max_load_per_cpu", "min_mem_available_pct"):
                raise LimitsError(f"{what} has no key {key!r}")
        return PressureCaps(
            max_load_per_cpu=_threshold(
                value.get("max_load_per_cpu", pressure.DEFAULT_MAX_LOAD_PER_CPU),
                f"{what} max_load_per_cpu",
                low=0,
                above_zero=True,
            ),
            min_mem_available_pct=_threshold(
                value.get("min_mem_available_pct", pressure.DEFAULT_MIN_MEM_AVAILABLE_PCT),
                f"{what} min_mem_available_pct",
                low=0,
                high=100,
            ),
        )
    raise LimitsError(f"{what} must be pressure caps, not {value!r}")


def caps(value=None):
    """The saturation line: `value` parsed, None reading as the built-ins."""
    return _pressure_caps(value)


def _reading(value):
    """A pressure Reading from None (unknown), a Reading, or a plain mapping."""
    if value is None:
        return pressure.Reading()
    if isinstance(value, pressure.Reading):
        item = value
    elif isinstance(value, Mapping):
        for key in value:
            if key not in ("load_per_cpu", "mem_available_pct"):
                raise LimitsError(f"pressure reading has no key {key!r}")
        item = pressure.Reading(
            load_per_cpu=value.get("load_per_cpu"),
            mem_available_pct=value.get("mem_available_pct"),
        )
    else:
        raise LimitsError(f"pressure reading must be a reading, not {value!r}")
    for dim in ("load_per_cpu", "mem_available_pct"):
        side = getattr(item, dim)
        if side is None:
            continue
        if isinstance(side, bool) or not isinstance(side, (int, float)):
            raise LimitsError(f"pressure reading {dim} must be a number, not {side!r}")
        if not math.isfinite(side) or side < 0:
            raise LimitsError(f"pressure reading {dim} must be zero or more, not {side!r}")
    return item


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
                projects=_table(limits.get("projects", {}), "projects"),
                roles=_table(limits.get("roles", {}), "roles"),
                foreign=list(limits.get("foreign") or []),
                gone_s=limits.get("gone_s", DEFAULT_GONE_S),
                pressure=limits.get("pressure"),
            )
        else:
            raise LimitsError(f"limits must be a Limits table, not {limits!r}")
    gone_s = _moment(limits.gone_s, "gone_s")
    if gone_s < 0:
        raise LimitsError(f"gone_s {limits.gone_s!r} must be zero or more")
    _pressure_caps(limits.pressure)
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


def _caps(table, host, foreign_used, project):
    """The fair-share caps and the total weight for `project`.

    Each cap is the project's floor plus its weight of the remainder of
    the host budget after every floor and the live foreign claims, with a
    ceiling capping it. An unknown project splits the remainder at the
    default weight. ``admit`` and ``share_cap`` both decide from here, so
    the slice limits a launch sets match the numbers the verdict used.
    """
    weights = sum(entry.weight for entry in table.values())
    if project not in table:
        weights += 1.0
    share = table.get(project, Share())
    floors = {dim: sum(_floor(entry, dim) for entry in table.values()) for dim in DIMS}
    caps = {}
    for dim in DIMS:
        remainder = _at(host, dim) - floors[dim] - _at(foreign_used, dim)
        cap = _floor(share, dim) + max(0, remainder) * (share.weight / weights if weights else 0)
        ceiling = _ceiling_of(share, dim)
        caps[dim] = min(cap, ceiling) if ceiling is not None else cap
    return caps, weights


def join(base, shares, foreign=(), *, where):
    """The limits with project shares and foreign claims filled in, validated.

    `base` is the host part from ``config`` (host, roles, ``gone_s``);
    `shares` maps project names to ``Share`` values, plain mappings, or
    None (floor zero, default weight); `foreign` is the ledger's claim
    list. A floor sum above the host budget raises ``SettingsError``
    naming `where` (the host config file) and the ``[resources.host]``
    key; any other malformed number raises ``LimitsError``. Both read as
    exit 4 at the launch seam.
    """
    if not isinstance(base, Limits):
        raise LimitsError(f"limits must be a Limits table, not {base!r}")
    table = {}
    for name, entry in dict(shares or {}).items():
        table[name] = Share() if entry is None else _share(entry, f"project {name}")
    host = None
    if base.host is not None:
        host = _usage(base.host, "host budget")
        for dim in DIMS:
            floors = sum(_floor(share, dim) for share in table.values())
            if floors > _at(host, dim):
                detail = ", ".join(
                    f"{name}: {_floor(share, dim)}"
                    for name, share in sorted(table.items())
                    if _floor(share, dim)
                )
                raise settings.SettingsError(
                    f"{where}: [resources.host] {dim} budget {_at(host, dim)} "
                    f"is below project floors summing to {floors} ({detail})"
                )
    joined = Limits(
        host=host,
        projects=table,
        roles=dict(base.roles),
        foreign=list(foreign or []),
        gone_s=base.gone_s,
        pressure=_pressure_caps(base.pressure),
    )
    validate_limits(joined)
    return joined


def share_cap(limits, project, now):
    """`project`'s fair share now: its floor plus its weight of the remainder.

    Ceilings cap it and live foreign claims shrink it, the same formula
    ``admit`` enforces, so the project-slice limits a launch sets match
    the numbers the verdict was decided from. None when no host budget
    is configured, where the gate passes everything.
    """
    if not isinstance(limits, Limits):
        if isinstance(limits, Mapping):
            limits = Limits(
                host=limits.get("host"),
                projects=_table(limits.get("projects", {}), "projects"),
                roles=_table(limits.get("roles", {}), "roles"),
                foreign=list(limits.get("foreign") or []),
                gone_s=limits.get("gone_s", DEFAULT_GONE_S),
                pressure=limits.get("pressure"),
            )
        else:
            raise LimitsError(f"limits must be a Limits table, not {limits!r}")
    now = _moment(now, "now")
    gone_s = _moment(limits.gone_s, "gone_s")
    if gone_s < 0:
        raise LimitsError(f"gone_s {limits.gone_s!r} must be zero or more")
    _pressure_caps(limits.pressure)
    if limits.host is None:
        return None
    host = _usage(limits.host, "host budget")
    table = {name: _share(entry, f"project {name}") for name, entry in limits.projects.items()}
    foreign = [
        _claim(entry, f"foreign claim {index}") for index, entry in enumerate(limits.foreign)
    ]
    live = _live_foreign(foreign, now, gone_s)
    foreign_used = Usage(
        cpu_m=sum(entry.cpu_m for entry in live),
        memory_mb=sum(entry.memory_mb for entry in live),
        pids=sum(entry.pids for entry in live),
    )
    caps, _weights = _caps(table, host, foreign_used, project)
    return Usage(**caps)


def _live_foreign(foreign, now, gone_s):
    """The claims no older than ``gone_s`` at ``now``."""
    return [entry for entry in foreign if now - entry.claimed_at <= gone_s]


def admit(request, charged, limits, now, pressure=None):
    """Whether ``request`` may start now against ``charged`` under ``limits``.

    Pure: the caller passes the charged usage, the clock reading and the
    pressure reading in, so no sum here can go stale. A missing host
    budget passes everything. A saturated pressure reading refuses even
    when shares are free; an unknown reading (None, the default) never
    blocks. Ceilings hold always; a project below its floor in every full
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
                projects=_table(limits.get("projects", {}), "projects"),
                roles=_table(limits.get("roles", {}), "roles"),
                foreign=list(limits.get("foreign") or []),
                gone_s=limits.get("gone_s", DEFAULT_GONE_S),
                pressure=limits.get("pressure"),
            )
        else:
            raise LimitsError(f"limits must be a Limits table, not {limits!r}")
    now = _moment(now, "now")
    gone_s = _moment(limits.gone_s, "gone_s")
    if gone_s < 0:
        raise LimitsError(f"gone_s {limits.gone_s!r} must be zero or more")
    reading = _reading(pressure)
    line = _pressure_caps(limits.pressure)
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
    hot, detail = _saturated_reading(
        reading,
        max_load_per_cpu=line.max_load_per_cpu,
        min_mem_available_pct=line.min_mem_available_pct,
    )
    if hot:
        return Verdict(
            False,
            PRESSURE,
            f"{described} refused: host saturated ({detail}): launches wait for the load to drop",
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
    caps, weights = _caps(table, host, foreign_used, request.project)
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


@dataclass(frozen=True)
class Tables:
    """The ``[resources]`` tables exactly as the host config sets them.

    ``host`` is the ``[resources.host]`` machine total, None when the
    file sets no budget: no enforcement, the gate passes everything.
    ``roles`` holds only the roles with a ``[resources.role.<name>]``
    table; ``config`` prices every other known role at zero, and the
    launch refuses an unpriced role while a host budget is set. ``pressure``
    is the ``[resources.pressure]`` saturation line, None when the file
    sets none: the conservative built-ins decide. Tables never come
    from the project file or a repo entry: the host budget is one machine
    fact, and those layers read as unknown settings instead.
    """

    host: Usage | None
    roles: dict = field(default_factory=dict)
    pressure: PressureCaps | None = None


def _checked_amount(value, what, where):
    """A whole amount of zero or more, else a SettingsError naming it."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise settings.SettingsError(
            f"{where}: {what} must be an int of zero or more, not {value!r}"
        )
    return value


def _checked_triple(raw, what, where, *, complete):
    """A Usage from a settings table: strict keys, missing dims read as zero.

    A complete triple (the host budget) needs every dimension; a missing
    one is a typo, not a zero. Unknown keys are errors, never ignored.
    """
    if not isinstance(raw, dict):
        raise settings.SettingsError(f"{where}: {what} must be a table")
    if not raw:
        raise settings.SettingsError(f"{where}: {what} is an empty table")
    for key in raw:
        if key not in DIMS:
            raise settings.SettingsError(f"{where}: {what} has no key {key!r}")
    if complete:
        for key in DIMS:
            if key not in raw:
                raise settings.SettingsError(f"{where}: {what} needs {key!r}")
    return Usage(
        cpu_m=_checked_amount(raw.get("cpu_m", 0), f"{what} cpu_m", where),
        memory_mb=_checked_amount(raw.get("memory_mb", 0), f"{what} memory_mb", where),
        pids=_checked_amount(raw.get("pids", 0), f"{what} pids", where),
    )


def _checked_pressure(raw, where):
    """The ``[resources.pressure]`` table as PressureCaps, with defaults.

    A missing key reads as the conservative built-in; an unknown key, a
    non-number, or a number outside its range is a SettingsError naming
    the file and the key. Whole numbers read as thresholds too.
    """
    if not isinstance(raw, dict):
        raise settings.SettingsError(f"{where}: [resources.pressure] must be a table")
    if not raw:
        raise settings.SettingsError(f"{where}: [resources.pressure] is an empty table")
    for key in raw:
        if key not in ("max_load_per_cpu", "min_mem_available_pct"):
            raise settings.SettingsError(f"{where}: [resources.pressure] has no key {key!r}")
    try:
        return _pressure_caps(
            {
                "max_load_per_cpu": raw.get("max_load_per_cpu", pressure.DEFAULT_MAX_LOAD_PER_CPU),
                "min_mem_available_pct": raw.get(
                    "min_mem_available_pct", pressure.DEFAULT_MIN_MEM_AVAILABLE_PCT
                ),
            },
            "pressure caps",
        )
    except LimitsError as err:
        raise settings.SettingsError(f"{where}: [resources.pressure] {err}") from None


def check_tables(raw, where):
    """The host config's ``[resources]`` value as Tables, None when absent.

    Strict on the ``[models]`` precedent: unknown tables (there is no
    project or instance layer at host level), unknown keys, unknown role
    names, and partial host triples are SettingsError naming the file
    and the key. A missing role table prices that role at zero later,
    and the launch refuses that role while a host budget is set.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise settings.SettingsError(f"{where}: [resources] must be a table")
    if not raw:
        raise settings.SettingsError(f"{where}: [resources] is an empty table")
    for key in raw:
        if key not in ("host", "role", "pressure"):
            raise settings.SettingsError(f"{where}: [resources] has no table {key!r}")
    host = None
    if "host" in raw:
        host = _checked_triple(raw["host"], "[resources.host]", where, complete=True)
    entries = raw.get("role", {})
    if not isinstance(entries, dict):
        raise settings.SettingsError(
            f"{where}: [resources.role] must be a table of per-role charges"
        )
    roles = {}
    for name, table in entries.items():
        if name not in prompts.ROLES:
            raise settings.SettingsError(
                f"{where}: [resources.role] unknown role {name!r}: "
                f"expected one of {', '.join(prompts.ROLES)}"
            )
        roles[name] = _checked_triple(table, f"[resources.role.{name}]", where, complete=False)
    line = None
    if "pressure" in raw:
        line = _checked_pressure(raw["pressure"], where)
    return Tables(host=host, roles=roles, pressure=line)


def config(resolved=None):
    """The effective host-level budget as Limits: the budget or None, every role's charge.

    Unpriced roles cost nothing here; the launch refuses them while a
    host budget is set, so a zero charge never runs free. Project shares
    come from the project registry and foreign claims from the ledger
    ticket; this is the host part only. Raises SettingsError when
    ``resources.gone_s`` is out of range.
    """
    resolved = resolved if resolved is not None else settings.load()
    tables = resolved.resources
    gone_s = resolved["resources.gone_s"]
    if isinstance(gone_s, bool) or not isinstance(gone_s, int) or gone_s < 0:
        raise settings.SettingsError(f"resources.gone_s {gone_s!r} must be zero or more")
    roles = {name: Usage() for name in prompts.ROLES}
    if tables is not None:
        roles.update(tables.roles)
    return Limits(
        host=tables.host if tables is not None else None,
        roles=dict(roles),
        gone_s=gone_s,
        pressure=(tables.pressure if tables is not None else None),
    )


#: Schema version of the read-only capacity report. The hub learns
#: capacity and load through this shape; within a version only
#: additive changes are compatible.
REPORT_SCHEMA_VERSION = 1

#: Kernel files read for host pressure. A missing file reads as
#: unknown and never blocks; the stall files are informational only.
LOADAVG_PATH = "/proc/loadavg"
MEMINFO_PATH = "/proc/meminfo"
PRESSURE_CPU_PATH = "/proc/pressure/cpu"
PRESSURE_MEMORY_PATH = "/proc/pressure/memory"

#: Saturation thresholds over load per CPU and MemAvailable percent.
#: Conservative defaults; promoting them to settings belongs to the
#: pressure-backoff ticket, which owns the saturation refusal.
DEFAULT_MAX_LOAD_PER_CPU = 2.0
DEFAULT_MIN_MEM_AVAILABLE_PCT = 10.0

#: Leaf files read for per-slice use, for the snapshot only.
MEMORY_CURRENT = "memory.current"
PIDS_CURRENT = "pids.current"
CPU_STAT = "cpu.stat"


@dataclass(frozen=True)
class Pressure:
    """Host pressure: load per CPU, available memory percent, saturation.

    Unknown readings are None, never a refusal: measurement informs the
    snapshot while admission prices from the static role tags.
    """

    load_per_cpu: float | None = None
    mem_available_pct: float | None = None
    saturated: bool = False
    cpu_some_avg10: float | None = None
    memory_some_avg10: float | None = None


@dataclass(frozen=True)
class Measured:
    """Measured slice use: the CPU counter, memory megabytes, pids.

    The CPU counter is cumulative (`cpu.stat` `usage_usec`): a single
    reading cannot price a rate, so it stays a raw counter.
    """

    cpu_usage_usec: int | None = None
    memory_mb: float | None = None
    pids: int | None = None


def _first_float(text):
    """The first whitespace-separated number, or None when missing."""
    if not isinstance(text, str):
        return None
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


def _meminfo_pct(text):
    """MemAvailable over MemTotal as a percent, or None when missing."""
    if not isinstance(text, str):
        return None
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().split()
    try:
        total = float(fields["MemTotal"][0])
        available = float(fields["MemAvailable"][0])
    except (KeyError, ValueError, IndexError):
        return None
    if total <= 0:
        return None
    return round(available / total * 100, 1)


def _some_avg10(text):
    """The `avg10=` value of a pressure-stall file, or None when missing."""
    if not isinstance(text, str):
        return None
    for token in text.split():
        if token.startswith("avg10="):
            try:
                return float(token[len("avg10=") :])
            except ValueError:
                return None
    return None


def parse_pressure(
    loadavg_text,
    meminfo_text,
    cpu_count,
    *,
    cpu_stall=None,
    memory_stall=None,
    max_load_per_cpu=DEFAULT_MAX_LOAD_PER_CPU,
    min_mem_available_pct=DEFAULT_MIN_MEM_AVAILABLE_PCT,
):
    """A Pressure from file texts and a CPU count (pure, total).

    Unreadable or unparsable files read as unknown fields, and unknown
    never saturates. Thresholds are explicit keyword arguments so the
    saturation rule stays testable without settings.
    """
    load = _first_float(loadavg_text)
    per_cpu = None
    if load is not None and isinstance(cpu_count, int) and not isinstance(cpu_count, bool):
        if cpu_count > 0:
            per_cpu = round(load / cpu_count, 2)
    pct = _meminfo_pct(meminfo_text)
    saturated = (per_cpu is not None and per_cpu > max_load_per_cpu) or (
        pct is not None and pct < min_mem_available_pct
    )
    return Pressure(
        load_per_cpu=per_cpu,
        mem_available_pct=pct,
        saturated=saturated,
        cpu_some_avg10=_some_avg10(cpu_stall),
        memory_some_avg10=_some_avg10(memory_stall),
    )


def read_pressure(*, read=None, cpu_count=None):
    """`(Pressure, notes)` from the kernel files through `read(path)`.

    The default reads the real files. An unreadable loadavg or meminfo
    reads unknown with a note naming the file; absent stall files stay
    quiet. Never raises for missing files: unknown never blocks.
    """
    notes = []
    texts = {}
    for path in (LOADAVG_PATH, MEMINFO_PATH):
        try:
            texts[path] = read(path) if read is not None else open(path, encoding="utf-8").read()
        except OSError as err:
            notes.append(f"pressure file {path} unreadable: {err}")
            texts[path] = None
    stalls = {}
    for path in (PRESSURE_CPU_PATH, PRESSURE_MEMORY_PATH):
        try:
            stalls[path] = read(path) if read is not None else open(path, encoding="utf-8").read()
        except OSError:
            stalls[path] = None
    if cpu_count is None:
        cpu_count = os.cpu_count()
    reading = parse_pressure(
        texts[LOADAVG_PATH],
        texts[MEMINFO_PATH],
        cpu_count,
        cpu_stall=stalls[PRESSURE_CPU_PATH],
        memory_stall=stalls[PRESSURE_MEMORY_PATH],
    )
    return reading, notes


def _whole(text):
    """A non-negative int from file text or a whole number, else None."""
    if isinstance(text, bool):
        return None
    if isinstance(text, float):
        return int(text) if text.is_integer() and text >= 0 else None
    if isinstance(text, int):
        return text if text >= 0 else None
    if not isinstance(text, str):
        return None
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_slice_use(memory_text, pids_text, cpu_stat_text):
    """A Measured from the three leaf file texts (pure, total).

    Memory bytes read as megabytes, pids as a count, and the `usage_usec`
    line of `cpu.stat` as the raw counter. Missing or garbage files read
    as unknown fields.
    """
    memory_mb = None
    raw = _whole(memory_text)
    if raw is not None:
        memory_mb = round(raw / (1024 * 1024), 1)
    use = None
    if isinstance(cpu_stat_text, str):
        for line in cpu_stat_text.splitlines():
            key, _, value = line.partition(" ")
            if key.strip() == "usage_usec":
                use = _whole(value)
                break
    return Measured(cpu_usage_usec=use, memory_mb=memory_mb, pids=_whole(pids_text))


def read_slice_use(slice_dir, *, read=None):
    """`(Measured, notes)` for one slice directory through `read(path)`.

    The default reads the real leaf files. A missing file reads as an
    unknown field with a note naming it; a missing directory reads as
    all unknown. Never raises for missing files.
    """
    notes = []
    texts = {}
    for name in (MEMORY_CURRENT, PIDS_CURRENT, CPU_STAT):
        path = os.path.join(slice_dir, name)
        try:
            texts[name] = read(path) if read is not None else open(path, encoding="utf-8").read()
        except OSError as err:
            notes.append(f"slice file {path} unreadable: {err}")
            texts[name] = None
    return (
        parse_slice_use(texts[MEMORY_CURRENT], texts[PIDS_CURRENT], texts[CPU_STAT]),
        notes,
    )


def usage_dict(value):
    """A Usage (or plain mapping) as a JSON-ready triple dict."""
    usage = _usage(value, "usage")
    return {"cpu_m": usage.cpu_m, "memory_mb": usage.memory_mb, "pids": usage.pids}


def _optional_number(value, what):
    """A number or None, else a LimitsError naming it."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitsError(f"{what} must be a number or null, not {value!r}")
    if not math.isfinite(value):
        raise LimitsError(f"{what} must be a number or null, not {value!r}")
    return value


def pressure_dict(value):
    """A Pressure (or plain mapping, or None) as a JSON-ready dict.

    `saturated` keeps only real booleans: anything else is a
    LimitsError (exit 4), so the string "false" never reads saturated.
    """
    if value is None:
        value = Pressure()
    if isinstance(value, Pressure):
        item = value
    elif isinstance(value, Mapping):
        saturated = value.get("saturated", False)
        if not isinstance(saturated, bool):
            raise LimitsError(f"saturated must be a boolean, not {saturated!r}")
        item = Pressure(
            load_per_cpu=_optional_number(value.get("load_per_cpu"), "load_per_cpu"),
            mem_available_pct=_optional_number(value.get("mem_available_pct"), "mem_available_pct"),
            saturated=saturated,
            cpu_some_avg10=_optional_number(value.get("cpu_some_avg10"), "cpu_some_avg10"),
            memory_some_avg10=_optional_number(value.get("memory_some_avg10"), "memory_some_avg10"),
        )
    else:
        raise LimitsError(f"pressure must be a Pressure or a mapping, not {value!r}")
    if not isinstance(item.saturated, bool):
        raise LimitsError(f"saturated must be a boolean, not {item.saturated!r}")
    return {
        "load_per_cpu": item.load_per_cpu,
        "mem_available_pct": item.mem_available_pct,
        "saturated": item.saturated,
        "cpu_some_avg10": item.cpu_some_avg10,
        "memory_some_avg10": item.memory_some_avg10,
    }


def measured_dict(value):
    """A Measured (or plain mapping) as a JSON-ready dict; None stays None."""
    if value is None:
        return None
    if isinstance(value, Measured):
        item = value
    elif isinstance(value, Mapping):
        item = Measured(
            cpu_usage_usec=_whole(value.get("cpu_usage_usec")),
            memory_mb=_optional_number(value.get("memory_mb"), "memory_mb"),
            pids=_whole(value.get("pids")),
        )
    else:
        raise LimitsError(f"measured use must be a Measured or a mapping, not {value!r}")
    return {
        "cpu_usage_usec": item.cpu_usage_usec,
        "memory_mb": item.memory_mb,
        "pids": item.pids,
    }


def live_units_by_owner(entries, key, own_prefix, list_fn):
    """`{owner: [live unit names]}`, keyed by project name.

    The one listing the charged sum intersects the registry with, for
    the launch gate and the snapshot both: only live, well-formed
    units count. `entries` is `[(name, prefix)]` for every registered
    project, so every registered project gets a key even with no live
    units and its name stays attributable. `key` is the asking
    project's name and `own_prefix` the prefix its units list under:
    when no registration owns `own_prefix`, the fallback lists under
    `key`, never an empty name, and only then. An already-listed
    prefix is never read twice. `list_fn(prefix)` is the injected
    manager seam.
    """
    from corral import units

    prefixes = dict(entries)
    if own_prefix not in prefixes.values():
        prefixes.setdefault(key, own_prefix)
    return {
        owner: [
            name
            for name, active, _sub in list_fn(prefix)
            if active in units.LIVE_STATES and units.parse_unit(prefix, name)
        ]
        for owner, prefix in prefixes.items()
    }


def charge_live_units(view, live_by_owner, names, roles):
    """`(Charged, charge_by_unit)`: the charged usage of the live units.

    The one sum the launch gate and the snapshot both read, so their
    totals and per-project attribution are identical. `view` is
    `[(entry, state)]` as `registry.visible` returns, of any state:
    live means listed by the manager in `live_by_owner`, which maps an
    owning project to its live unit names as `live_units_by_owner`
    builds it. `names` holds the registered project names, passed
    explicitly rather than derived from the listing keys, and `roles`
    maps role names to charges.

    A unit counts for the project its registry ticket names when that
    project is registered, else the project whose listing owns it: an
    agent with no ticket project pays its prefix owner, never an empty
    name. A listed unit with no registry entry reads as the default
    role with no ticket, so it still charges its owner. A missing role
    reads as the default role, and an unpriced role charges zero (the
    launch refuses those instead, so zero never runs free). A crashed
    agent, registry live but unit gone, holds no share.
    """
    entries = {}
    for entry, _state in view:
        entries.setdefault(entry.get("id"), entry)
    if not isinstance(live_by_owner, Mapping):
        raise LimitsError(
            f"live units must be a mapping of owner to unit names, not {live_by_owner!r}"
        )
    if not isinstance(roles, Mapping):
        raise LimitsError(f"roles must be a mapping of role names to charges, not {roles!r}")
    total = Usage()
    by_project: dict = {}
    per_unit: dict = {}
    for owner, unit_names in live_by_owner.items():
        if not isinstance(unit_names, (list, tuple)) or any(
            not isinstance(unit, str) for unit in unit_names
        ):
            raise LimitsError(
                f"live units for {owner!r} must be a list of unit names, not {unit_names!r}"
            )
        for unit in unit_names:
            entry = entries.get(unit) or {}
            role = entry.get("role") or prompts.DEFAULT_ROLE
            ticket = entry.get("ticket") or {}
            named = ticket.get("project") if isinstance(ticket, dict) else None
            charged_for = named if isinstance(named, str) and named in names else owner
            found = roles.get(role)
            cost = _usage(found if found is not None else Usage(), f"role {role}")
            total = Usage(
                cpu_m=total.cpu_m + cost.cpu_m,
                memory_mb=total.memory_mb + cost.memory_mb,
                pids=total.pids + cost.pids,
            )
            base = by_project.get(charged_for, Usage())
            by_project[charged_for] = Usage(
                cpu_m=base.cpu_m + cost.cpu_m,
                memory_mb=base.memory_mb + cost.memory_mb,
                pids=base.pids + cost.pids,
            )
            per_unit[unit] = (charged_for, cost)
    return Charged(total=total, by_project=by_project), per_unit


def capacity_report(*, host_budget, charged, pressure):
    """The read-only capacity report for the hub, from plain data.

    The section 2 shape: schema version, host budget (or null when no
    budget is configured), charged totals, per-project charged usage,
    and pressure. Pure: every input is passed in, so the hub path that
    builds it can never read or write the ledger. A non-mapping
    `by_project` is a LimitsError (exit 4), never a raw TypeError.
    """
    if not isinstance(charged, Charged):
        if isinstance(charged, Mapping):
            if "by_project" in charged:
                projects = charged["by_project"]
            else:
                projects = {}
            if not isinstance(projects, Mapping):
                raise LimitsError(f"charged by_project must be a mapping, not {projects!r}")
            charged = Charged(
                total=charged.get("total"),
                by_project=dict(projects),
            )
        else:
            raise LimitsError(f"charged usage must be a Charged table, not {charged!r}")
    elif not isinstance(charged.by_project, Mapping):
        raise LimitsError(f"charged by_project must be a mapping, not {charged.by_project!r}")
    total = _usage(charged.total, "charged total")
    by_project = {
        name: _usage(charged.by_project[name], f"charged project {name}")
        for name in charged.by_project
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "host_budget": None if host_budget is None else usage_dict(host_budget),
        "charged": usage_dict(total),
        "by_project": {name: usage_dict(by_project[name]) for name in sorted(by_project)},
        "pressure": pressure_dict(pressure),
    }
