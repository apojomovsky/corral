"""Doctor `resources*` rows: the host budget against the live host.

One read-only check per aspect of section 6 of
docs/09-host-resource-governance.md plus the slice-drift row: delegation
and the budget tables (`resources`), ledger health (`resources-ledger`),
the single-instance guard marker (`resources-marker`), pressure sensors
(`resources-pressure`), foreign claims (`resources-foreign`), and the
kernel limits of existing slices (`resources-slices`).

Pure core plus three injected seams: ``load_live_roles`` lists units
through ``list_fn`` and resolves roles from registry entries,
``load_slice_actual`` reads one slice dump per slice through ``run``, and
``load_sensor_errors`` reads the pressure files through ``read``. Tests
inject fakes at all three; production wires the real seams in
``doctor.load_resource_readings``.
"""

import math
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from korapy import heavy, host_ledger, pressure, prompts, resources, units

#: The pressure files the doctor probes. An unreadable one reads as
#: unknown: admission continues on charged tags and the pressure row warns.
#: The load and memory paths resolve in `pressure`, next to the reader
#: that parses them; the stall files have no other reader, so they live here.
SENSOR_FILES = (
    pressure.LOADAVG_PATH,
    pressure.MEMINFO_PATH,
    "/proc/pressure/cpu",
    "/proc/pressure/memory",
)


#: The saturation line when no `[resources.pressure]` table sets one,
#: resolved next to the reader that parses the values (`pressure`).
_DEFAULT_PRESSURE_CAPS = (
    pressure.DEFAULT_MAX_LOAD_PER_CPU,
    pressure.DEFAULT_MIN_MEM_AVAILABLE_PCT,
)


class ResourceReadings(NamedTuple):
    """Everything the resources rows read, gathered once by production.

    ``host`` None means no host budget is configured: the gate passes
    everything. ``limits_error`` carries an unusable budget table;
    ``controllers_error``, ``ledger_error`` and ``marker_error`` carry
    their unreadable or mismatched file. ``slices`` maps existing slice
    names to their kernel-reported properties, empty when no queried
    slice exists yet, None when the listing itself failed. ``pressure``
    is the measured host pressure as ``(load_per_cpu, mem_available_pct)``,
    Nones when unknown; None means not gathered, and then the pressure
    row skips saturation. ``pressure_caps`` is the saturation line as
    ``(max_load_per_cpu, min_mem_available_pct)``. ``foreign_ignored``
    names the drop-in dir entries no claim holds (a typo such as
    ``desk.jsnm``): never counted by the gate, warned by the row.
    Tests build this directly; only production reads disk.
    """

    host: resources.Usage | None
    shares: Mapping[str, object]
    foreign: Sequence[Mapping[str, object]]
    gone_s: float
    now: float
    limits_error: str | None
    priced_roles: frozenset
    live_roles: tuple
    controllers: frozenset | None
    controllers_error: str | None
    ledger: dict | None
    ledger_error: str | None
    ledger_path: str
    marker: str | None
    marker_error: str | None
    state_dir: str
    marker_path: str
    sensor_errors: tuple
    slices: Mapping[str, Mapping[str, str]] | None
    config_path: str
    pressure: tuple[float | None, float | None] | None = None
    pressure_caps: tuple[float, float] = _DEFAULT_PRESSURE_CAPS
    foreign_ignored: tuple = ()
    #: The heavy lane file as read for the report. None with no error
    #: reads as an empty lane (a missing file is not a failure); a
    #: present-but-corrupt file lands in `heavy_error` instead and the
    #: lane row fails with the documented move-aside repair.
    heavy_lane: Mapping[str, object] | None = None
    heavy_error: str | None = None
    heavy_path: str = ""
    heavy_bytes: int | None = None
    heavy_mtime: float | None = None
    heavy_now: float = 0.0
    heavy_estimates: Mapping[str, object] | None = None
    heavy_wait_factor: float = heavy.DEFAULT_WAIT_FACTOR


class ResourceRow(NamedTuple):
    """One resources check row; ``doctor`` renders it as a ``Check``."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str


def _plural(count, singular, plural=None):
    """`count` with its noun, singular only for one."""
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def check(state: ResourceReadings | None) -> list[ResourceRow]:
    """Every resources row, in report order. No reading, no rows."""
    if state is None:
        return []
    rows = [
        _check_config(state),
        _check_ledger(state),
        _check_marker(state),
        _check_pressure(state),
        _check_foreign(state),
        _check_heavy(state),
    ]
    stuck = _check_heavy_stuck(state)
    if stuck is not None:
        rows.append(stuck)
    slices = _check_slices(state)
    if slices is not None:
        rows.append(slices)
    return rows


def _check_config(state: ResourceReadings) -> ResourceRow:
    """Delegation, table validity, the unset budget and unpriced live roles."""
    if state.limits_error is not None:
        if "foreign claim" in state.limits_error:
            fix = f"fix the foreign claims in {state.ledger_path}, then run `korapy doctor` again"
        else:
            fix = (
                f"fix the [resources] tables in {state.config_path}, then run `korapy doctor` again"
            )
        return ResourceRow("resources", "fail", state.limits_error, fix)
    if state.host is None:
        return ResourceRow(
            "resources",
            "warn",
            "no host budget configured: the budget gate passes everything"
            " and launches set no slice or limit properties",
            f"set [resources.host] in {state.config_path} to enforce budgets,"
            " then run `korapy doctor` again",
        )
    fails: list[tuple[str, str]] = []
    warns: list[tuple[str, str]] = []
    ok_detail: str | None = None
    if state.controllers_error is not None:
        fails.append(
            (
                state.controllers_error,
                "make the delegation file readable, then run `korapy doctor` again",
            )
        )
    else:
        available = set(state.controllers or ())
        try:
            units.check_controllers(available)
        except units.UnitsError as err:
            fails.append(
                (
                    str(err),
                    "delegate the missing controllers to the user slice,"
                    " then run `korapy doctor` again",
                )
            )
        else:
            missing_optional = sorted(
                name for name in units.OPTIONAL_CONTROLLERS if name not in available
            )
            ok_detail = f"delegation ok ({', '.join(sorted(available))})"
            if missing_optional:
                ok_detail += f"; unavailable: {', '.join(missing_optional)}"
    unpriced = sorted(
        role
        for role in set(state.live_roles)
        if isinstance(role, str) and role and role not in state.priced_roles
    )
    for role in unpriced:
        warns.append(
            (
                f"role {role!r} is live with no [resources.role.{role}] price"
                f" in {state.config_path}: it runs with no charge"
                " while the host budget is set",
                f"add the missing [resources.role.{role}] table"
                f" to {state.config_path}, then run `korapy doctor` again",
            )
        )
    if fails:
        return ResourceRow(
            "resources",
            "fail",
            "; ".join(detail for detail, _ in (*fails, *warns)),
            fails[0][1],
        )
    if warns:
        return ResourceRow(
            "resources", "warn", "; ".join(detail for detail, _ in warns), warns[0][1]
        )
    assert ok_detail is not None
    return ResourceRow("resources", "ok", ok_detail, "")


def _check_ledger(state: ResourceReadings) -> ResourceRow:
    """The ledger file: corrupt reads as a failure, never as an empty pool."""
    if state.ledger_error is not None:
        return ResourceRow(
            "resources-ledger",
            "fail",
            state.ledger_error,
            f"repair the host ledger at {state.ledger_path}: a corrupt file"
            " is never reset automatically; restore it,"
            " then run `korapy doctor` again",
        )
    ledger = state.ledger or {}
    projects = ledger.get("projects", {})
    claims = ledger.get("foreign", [])
    return ResourceRow(
        "resources-ledger",
        "ok",
        f"the host ledger at {state.ledger_path} reads"
        f" ({_plural(len(projects), 'project')},"
        f" {_plural(len(claims), 'foreign claim')})",
        "",
    )


def _check_marker(state: ResourceReadings) -> ResourceRow:
    """The single-instance guard: the marker beside the ledger names this host's fleet."""
    fix = (
        f"stop every fleet on this host, move the marker at {state.marker_path}"
        " by hand, then run `korapy doctor` again"
    )
    if state.marker_error is not None:
        return ResourceRow("resources-marker", "fail", state.marker_error, fix)
    if state.marker is None:
        return ResourceRow(
            "resources-marker",
            "ok",
            f"no ledger marker yet: the next launch records {state.state_dir}",
            "",
        )
    if os.path.normpath(state.marker) != os.path.normpath(state.state_dir):
        return ResourceRow(
            "resources-marker",
            "fail",
            f"host ledger {state.ledger_path} belongs to state dir {state.marker},"
            f" not {state.state_dir}: this host already runs a fleet"
            " under another state dir",
            fix,
        )
    return ResourceRow("resources-marker", "ok", "the ledger marker names this state dir", "")


def _check_pressure(state: ResourceReadings) -> ResourceRow:
    """The pressure sensors: unreadable warns, saturation fails the row.

    A saturated reading fails even when shares are free: only new
    launches wait, running agents are never paused or killed. Unknown
    readings never trip the line.
    """
    if state.sensor_errors:
        return ResourceRow(
            "resources-pressure",
            "warn",
            f"pressure readings unreadable: {'; '.join(state.sensor_errors)}",
            "check /proc is mounted and readable, then run `korapy doctor` again",
        )
    if state.pressure is not None:
        measured = pressure.Reading(
            load_per_cpu=state.pressure[0], mem_available_pct=state.pressure[1]
        )
        hot, detail = pressure.saturated(
            measured,
            max_load_per_cpu=state.pressure_caps[0],
            min_mem_available_pct=state.pressure_caps[1],
        )
        if hot:
            return ResourceRow(
                "resources-pressure",
                "fail",
                f"host saturated ({detail}): launches wait",
                "wait for the load to drop, then retry the launch",
            )
    return ResourceRow("resources-pressure", "ok", "host pressure readings are available", "")


def _parse_claim(entry: object, index: int) -> tuple[str, float]:
    """(name, claimed_at) for one raw ledger claim, else a ValueError naming it.

    A missing claimed_at reads as `resources.DEFAULT_CLAIMED_AT`, the
    same default the budget gate validates, so the row reports the
    claim as expired and ignored rather than failing it.
    """
    if not isinstance(entry, Mapping):
        raise ValueError(f"foreign claim {index} must be an object, not {entry!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"foreign claim {index} needs a non-empty name, not {name!r}")
    if "claimed_at" not in entry:
        return name, resources.DEFAULT_CLAIMED_AT
    claimed_at = entry.get("claimed_at")
    if (
        isinstance(claimed_at, bool)
        or not isinstance(claimed_at, (int, float))
        or not math.isfinite(claimed_at)
    ):
        raise ValueError(f"foreign claim {index} needs a claimed_at in seconds, not {claimed_at!r}")
    return name, claimed_at


def _check_foreign(state: ResourceReadings) -> ResourceRow:
    """Foreign claims: malformed fails, expired or ignored warns, live reads ok.

    An unreadable ledger warns unknown: the load was never read, so an
    empty claim list is unknown, not zero. A claim without claimed_at
    reads as `resources.DEFAULT_CLAIMED_AT` like the budget gate, so
    it reports as expired and ignored. Entries in the drop-in dir that
    are not .json are never counted; they warn naming the entries.
    """
    if state.limits_error is not None and "foreign claim" in state.limits_error:
        return ResourceRow(
            "resources-foreign",
            "fail",
            state.limits_error,
            f"fix the foreign claims in {state.ledger_path}, then run `korapy doctor` again",
        )
    if state.ledger_error is not None:
        return ResourceRow(
            "resources-foreign",
            "warn",
            "foreign claims unknown (ledger unreadable): the load is unknown, not zero",
            f"repair the host ledger at {state.ledger_path}, then run `korapy doctor` again",
        )
    live: list[str] = []
    expired: list[str] = []
    for index, entry in enumerate(state.foreign or []):
        try:
            name, claimed_at = _parse_claim(entry, index)
        except ValueError as err:
            return ResourceRow(
                "resources-foreign",
                "fail",
                str(err),
                f"fix the foreign claims in {state.ledger_path}, then run `korapy doctor` again",
            )
        if state.now - claimed_at > state.gone_s:
            expired.append(name)
        else:
            live.append(name)
    warns: list[tuple[str, str]] = []
    if expired:
        warns.append(
            (
                f"foreign {', '.join(sorted(expired))} expired"
                f" under gone_s ({state.gone_s:g}s) and {_plural(len(expired), 'is', 'are')}"
                " ignored by the budget gate",
                "renew or remove the expired claims, then run `korapy doctor` again",
            )
        )
    ignored = sorted(set(state.foreign_ignored or ()))
    if ignored:
        claims_dir = host_ledger.claims_dir(state.ledger_path)
        warns.append(
            (
                f"foreign-claim entries ignored (not .json, never counted): {', '.join(ignored)}",
                f"remove or rename the ignored entries in {claims_dir},"
                " then run `korapy doctor` again",
            )
        )
    if warns:
        return ResourceRow(
            "resources-foreign",
            "warn",
            "; ".join(detail for detail, _ in warns),
            "; ".join(fix for _, fix in warns),
        )
    if live:
        return ResourceRow(
            "resources-foreign",
            "ok",
            f"{_plural(len(live), 'foreign claim')} live: {', '.join(sorted(live))}",
            "",
        )
    return ResourceRow("resources-foreign", "ok", "no foreign claims", "")


def _heavy_lane_of(state: ResourceReadings) -> Mapping[str, object]:
    """The lane for the heavy rows: the read file, else an empty lane."""
    lane = state.heavy_lane
    if isinstance(lane, Mapping):
        return lane
    return {"holders": [], "waiters": [], "costs": {}, "recent": []}


def _check_heavy(state: ResourceReadings) -> ResourceRow:
    """The heavy lane file: corrupt fails, the queue reads as counts.

    A present-but-corrupt file refuses with the documented repair (move
    it aside, never delete it; the next join recreates an empty lane).
    A missing file reads as an empty lane, never a failure. Bypassed
    runs count in the detail but never block: the bypass is visible,
    not prevented.
    """
    path = state.heavy_path or "lane.json"
    if state.heavy_error is not None:
        size = state.heavy_bytes if isinstance(state.heavy_bytes, int) else "?"
        return ResourceRow(
            "heavy",
            "fail",
            heavy.corrupt_lane_detail(size, state.heavy_mtime),
            f"move {path} aside to {path}.corrupt-<UTC timestamp>"
            " in the same directory (archived, never deleted),"
            " then run `korapy doctor` again",
        )
    lane = _heavy_lane_of(state)
    holders = lane.get("holders")
    waiters = lane.get("waiters")
    recent = lane.get("recent")
    counts = heavy.count_outcomes(recent)
    bypassed = counts.get("bypassed", 0)
    n_holders = len(holders) if isinstance(holders, list) else 0
    n_waiters = len(waiters) if isinstance(waiters, list) else 0
    detail = f"heavy: {n_holders} holder(s), {n_waiters} waiter(s)" + (
        f", {bypassed} bypassed" if bypassed else ""
    )
    return ResourceRow("heavy", "ok", detail, "")


def _check_heavy_stuck(state: ResourceReadings) -> ResourceRow | None:
    """Waiters past their deadline and holders past the scaled typical.

    A waiter past its stored deadline would have left with 75, so the
    row belongs to a dead process or a wedged wait; a holder past wait
    factor times its duration p90 outruns what the ledger learned. Both
    only warn: the next admit pass reaps the dead weight, and pressure
    never touches a holder. A corrupt lane reports nothing here: the
    `heavy` row already fails. Unknown estimates never accuse.
    """
    if state.heavy_error is not None:
        return None
    lane = _heavy_lane_of(state)
    stuck_waiters = heavy.stuck_waiters(lane, state.heavy_now)
    stuck_holders = heavy.stuck_holders(
        lane, state.heavy_estimates or {}, state.heavy_now, state.heavy_wait_factor
    )
    if not stuck_waiters and not stuck_holders:
        return ResourceRow("heavy-stuck", "ok", "heavy: no stuck waiters or holders", "")
    parts = []
    for waiter in stuck_waiters:
        wid = waiter.get("id", "?") if isinstance(waiter, Mapping) else "?"
        parts.append(f"waiter {str(wid)[:8]} past its wait deadline")
    for holder, elapsed, p90 in stuck_holders:
        hid = holder.get("id", "?") if isinstance(holder, Mapping) else "?"
        parts.append(
            f"holder {str(hid)[:8]} running {elapsed:.0f}s"
            f" past {state.heavy_wait_factor:.1f}x{p90:.0f}s typical"
        )
    return ResourceRow(
        "heavy-stuck",
        "warn",
        "heavy: stuck lane rows: " + "; ".join(parts),
        "check the stuck processes, then run `korapy doctor` again",
    )


def parse_show(text: str) -> dict:
    """The `KEY=value` lines of one slice dump, last wins."""
    props = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            props[key] = value.strip()
    return props


#: systemd timespan units to microseconds. `systemctl show` renders
#: usec properties as a timespan (`CPUQuotaPerSecUSec=500ms` for a 50%
#: quota), so the quota reader parses that form back to microseconds.
_TIMESPAN_US = {
    "us": 1,
    "usec": 1,
    "usecs": 1,
    "ms": 1_000,
    "msec": 1_000,
    "msecs": 1_000,
    "s": 1_000_000,
    "sec": 1_000_000,
    "secs": 1_000_000,
    "second": 1_000_000,
    "seconds": 1_000_000,
    "min": 60_000_000,
    "mins": 60_000_000,
    "minute": 60_000_000,
    "minutes": 60_000_000,
    "h": 3_600_000_000,
    "hr": 3_600_000_000,
    "hrs": 3_600_000_000,
    "hour": 3_600_000_000,
    "hours": 3_600_000_000,
    "d": 86_400_000_000,
    "day": 86_400_000_000,
    "days": 86_400_000_000,
    "w": 604_800_000_000,
    "week": 604_800_000_000,
    "weeks": 604_800_000_000,
    "month": 2_592_000_000_000,
    "months": 2_592_000_000_000,
    "year": 31_536_000_000_000,
    "years": 31_536_000_000_000,
}

_TIMESPAN_PART = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)")


def _timespan_us(text: str) -> float | None:
    """Microseconds for a systemd timespan (`500ms`, `1min 30s`), else None."""
    total = 0.0
    rest = text.strip().lower()
    if not rest:
        return None
    while rest:
        match = _TIMESPAN_PART.match(rest)
        if match is None:
            return None
        try:
            factor = _TIMESPAN_US[match.group(2)]
        except KeyError:
            return None
        total += float(match.group(1)) * factor
        rest = rest[match.end() :].strip()
    return total


def _actual_number(raw: str | None, divisor: float) -> float | None:
    """The kernel-reported limit in budget units, None when unset or unreadable."""
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in ("", "infinity", "max"):
        return None
    try:
        return int(text) / divisor
    except ValueError:
        pass
    parsed = _timespan_us(text)
    if parsed is None:
        return None
    return parsed / divisor


#: (Usage dimension, show property, divisor to budget units, unit word).
#: Slices carry only memory and CPU: the launch sets TasksMax on the
#: agent unit inside the slice, never on the slice itself.
_READERS = (
    ("cpu_m", "CPUQuotaPerSecUSec", 1000.0, "millicores"),
    ("memory_mb", "MemoryMax", 1048576.0, "MB"),
)


def _want(value: object) -> float | None:
    """The configured limit in budget units, None when no limit is set."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return None if number == 0 else number


def _show_number(number: float, word: str) -> str:
    return f"{number:g} {word}"


def slice_drift(expected: resources.Usage, props: Mapping[str, str]) -> list[str]:
    """Per-dimension drift between a budget and kernel-reported properties.

    Both sides round to whole units before comparing: the launch renders
    limits with six significant digits, so the kernel value is the rounded
    one, and only a real operator change reads as drift.
    """
    drifts = []
    for dim, prop, divisor, word in _READERS:
        want = _want(getattr(expected, dim, None))
        have = _actual_number(props.get(prop), divisor)
        if want is None and have is None:
            continue
        if want is not None and have is not None and round(want) == round(have):
            continue
        drifts.append(
            f"{prop} is "
            + ("unset" if have is None else _show_number(have, word))
            + " but the budget sets "
            + ("unset" if want is None else _show_number(want, word))
        )
    return drifts


def _check_slices(state: ResourceReadings) -> ResourceRow | None:
    """Existing slices against their budgets; no row without a budget to compare.

    Launches create missing slices and reconcile existing ones on the next
    launch of that project, so a difference warns only between the
    operator's change and that launch. A missing slice is no drift: the
    next launch creates it. A slice the manager never created
    (exit 0 with every limit unset, no fragment, inactive) reads as missing.
    A cleared budget applies nothing: stale limits stay until the operator
    stops the slice by hand.
    """
    if state.host is None or state.limits_error is not None:
        return None
    if state.slices is None:
        return ResourceRow(
            "resources-slices",
            "warn",
            "could not read the resource slices: launches may have drifted"
            " from their budgets without notice",
            "check the user manager answers `systemctl --user show`,"
            " then run `korapy doctor` again",
        )
    limits = resources.Limits(
        host=state.host,
        projects=dict(state.shares),
        roles={},
        foreign=list(state.foreign or []),
        gone_s=state.gone_s,
    )
    if not state.slices:
        return ResourceRow(
            "resources-slices",
            "ok",
            "no resource slices exist yet: launches create them with their budgets",
            "",
        )
    expected = {units.HOST_SLICE: state.host}
    for name in sorted(state.shares):
        try:
            slice_name = units.project_slice(name)
        except units.UnitsError:
            continue
        expected[slice_name] = resources.share_cap(limits, name, state.now)
    drifts: list[str] = []
    matched = 0
    missing = 0
    for name in sorted(expected):
        props = state.slices.get(name)
        if props is None:
            missing += 1
            continue
        matched += 1
        drifts.extend(f"slice {name}: {drift}" for drift in slice_drift(expected[name], props))
    if drifts:
        return ResourceRow(
            "resources-slices",
            "warn",
            "; ".join(drifts) + "; limits reconcile on the next launch of that project",
            "launch an agent of that project, then run `korapy doctor` again"
            " (a cleared budget applies nothing: stop the slice by hand)",
        )
    if matched == 1:
        detail = "1 resource slice matches its budget"
    else:
        detail = f"{matched} resource slices match their budget"
    if missing:
        detail += f"; {missing} missing (created on next launch)"
    return ResourceRow("resources-slices", "ok", detail, "")


def load_live_roles(
    prefixes, *, list_fn: Callable[[str], list], entries: Mapping[str, Mapping]
) -> tuple:
    """Roles of live agents under `prefixes`, sorted and unique.

    Roles resolve like the budget charge: the registry entry's role, worker
    when the unit was never registered. Non-live units and names that do
    not parse are ignored; an unreachable manager reads as no roles.
    Pure over `list_fn` and `entries`.
    """
    roles = set()
    for prefix in prefixes:
        try:
            found = list_fn(prefix)
        except (units.Unreachable, units.UnitsError):
            continue
        for name, active, _sub in found:
            try:
                parsed = units.parse_unit(prefix, name)
            except units.UnitsError:
                continue
            if parsed is None or active not in units.LIVE_STATES:
                continue
            role = (entries.get(name) or {}).get("role") or prompts.DEFAULT_ROLE
            if isinstance(role, str) and role:
                roles.add(role)
    return tuple(sorted(roles))


def _never_created(props: Mapping[str, str]) -> bool:
    """Whether a slice dump is a unit the manager never created.

    A never-created slice still answers exit 0, with every limit unset,
    no fragment backing it and an inactive state. It is absent, not a
    failure: the next launch creates it.
    """
    if props.get("FragmentPath") != "":
        return False
    if props.get("ActiveState", "") != "inactive":
        return False
    return all(_actual_number(props.get(prop), divisor) is None for _, prop, divisor, _ in _READERS)


def load_slice_actual(
    names: Sequence[str], *, run: Callable[[Sequence[str]], object]
) -> dict | None:
    """Kernel-reported properties per existing slice; None when unreadable.

    One manager dump per slice through the injected `run` seam. A slice
    the manager never created answers exit 0 with every limit unset, no
    fragment and an inactive state: absent, not a failure, and the next
    launch creates it. Empty when no queried slice exists yet; None when
    the listing itself failed: the seam raised, or no dump succeeded.
    Pure over `run`.
    """
    actual = {}
    listed = False
    for name in names:
        try:
            proc = run(["systemctl", "--user", "show", name])
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(proc, "returncode", 1) != 0:
            continue
        listed = True
        props = parse_show(getattr(proc, "stdout", "") or "")
        if _never_created(props):
            continue
        actual[name] = props
    if not listed:
        return None
    return actual


def _read_text(path: str) -> str:
    """One file read; tests inject a fake, production reads the real file."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def load_sensor_errors(*, read: Callable[[str], str] | None = None) -> tuple:
    """`(path: error)` for every unreadable pressure file, empty when all read."""
    reader = read or _read_text
    errors = []
    for path in SENSOR_FILES:
        try:
            reader(path)
        except OSError as err:
            errors.append(f"{path}: {err}")
    return tuple(errors)
