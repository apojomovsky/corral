"""The heavy lane: weighted slots with limits, scope runs and the raw ledger.

Ticket 1 of the heavy-lane design: the lane budget check, the transient
scope argv on the ``units`` precedent, per-command routing for takeoff
with role ``takeoff``, and one ledger row per run with the host
descriptor. Weights still come from config; learned estimates arrive in
a later ticket, which also adds the queue file, the status view and the
pressure gate. There is no CLI verb here yet.

Config lives in the project's top-level ``[heavy]`` table, carried raw
on the resolved settings (the ``[profiles]`` precedent) and validated
here, the one owner::

    [heavy]
    cpu_m = 4000
    memory_mb = 8192
    [heavy.default_weight]
    cpu_m = 1000
    memory_mb = 1024
    [heavy.commands."make check"]
    cpu_m = 3000
    memory_mb = 6144

``commands`` names the commands that run through the lane; a listed
command with an empty table queues at the default weight. The caps
default per dimension to the host budget; a cap above the host budget
is refused. ``starved_ratio`` and ``ledger_period`` default to 0.2 and
``"month"``; only ``"month"`` periods are supported so far.

Every subprocess goes through one injectable seam per operation.
``run_heavy`` takes ``run`` (start the scope and wait), ``teardown``
(release the scope) and ``scope_cgroup`` (resolve the unit's cgroup);
all three are required, so a missed fake fails closed instead of
leaking a scope. The file reader, clock, host and pressure readers
default to the live ones; they only read. A vanished cgroup records a
row with null measurements: unknown never blocks.
"""

import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NamedTuple

from korapy import gitenv, paths, resources, settings

#: The version stamped on every ledger row.
SCHEMA_VERSION = 1

#: Command exits a run reports unchanged; any other exit maps to 1.
#: The public exit-code set, mirrored here so the lane answers without
#: importing the CLI.
PUBLIC_CODES = frozenset({0, 1, 2, 3, 4, 75})

#: The conservative first-run weight: unknown jobs queue safely, and
#: the first measurement corrects them.
DEFAULT_WEIGHT_CPU_M = 1000
DEFAULT_WEIGHT_MEMORY_MB = 1000

#: The share of wall time a job may spend runnable but not running
#: before it reads as starved.
DEFAULT_STARVED_RATIO = 0.2

#: The only ledger period so far: one file per host per calendar month.
DEFAULT_LEDGER_PERIOD = "month"

#: The lane state: host id, lock sidecar and queue file live directly
#: under it in later tickets; the raw runs already do.
HEAVY_DIR = "heavy"
RUNS_DIR = "runs"
HOST_ID_FILE = "host-id"

#: The unit name prefix for lane scopes.
UNIT_PREFIX = "heavy-"

#: A stored host label is 32 lowercase hex chars (a uuid4 hex): anything
#: else in the label file is corruption or a hand edit, refused fail-closed.
LABEL_RE = re.compile(r"[0-9a-f]{32}")

#: A ledger period is one calendar month: only a ``YYYY-MM`` value stays a
#: filename, so a corrupt or hand-edited period never escapes the runs dir.
PERIOD_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


class HeavyError(Exception):
    """The lane run failed: bad usage, bad config or a failed scope (exit 4)."""

    exit_code = 4


class ScopeUnreachable(HeavyError):
    """The user manager could not be reached for the scope (exit 3)."""

    exit_code = 3


@dataclass(frozen=True)
class Weight:
    """One lane weight: CPU millicores and memory megabytes."""

    cpu_m: int = DEFAULT_WEIGHT_CPU_M
    memory_mb: int = DEFAULT_WEIGHT_MEMORY_MB


@dataclass(frozen=True)
class Config:
    """The validated ``[heavy]`` table, or None when the project sets none.

    ``cpu_m`` and ``memory_mb`` are the lane caps, None per dimension
    when unset (that dimension defaults to the host budget).
    ``commands`` maps each listed command to its weight; an empty entry
    queues at ``default``.
    """

    cpu_m: int | None = None
    memory_mb: int | None = None
    default: Weight = field(default_factory=Weight)
    commands: dict = field(default_factory=dict)
    starved_ratio: float = DEFAULT_STARVED_RATIO
    ledger_period: str = DEFAULT_LEDGER_PERIOD


class RunResult(NamedTuple):
    """One finished lane run: the raw and mapped exits plus the row keys."""

    command_exit: int
    exit_code: int
    output: str
    weight: Weight
    wall_s: float
    unit: str
    argv_hash: str


def _count(value, what, where):
    """A whole resource amount of one or more, else a SettingsError naming it."""
    if isinstance(value, bool):
        raise settings.SettingsError(f"{where}: {what} must be a whole number, not {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise settings.SettingsError(f"{where}: {what} must be a whole number, not {value!r}")
        value = int(value)
    if not isinstance(value, int) or value < 1:
        raise settings.SettingsError(f"{where}: {what} must be a whole number, not {value!r}")
    return value


def _weight_table(value, what, where):
    """A Weight from a ``{cpu_m, memory_mb}`` table, else a SettingsError."""
    if not isinstance(value, Mapping):
        raise settings.SettingsError(f"{where}: {what} must be a table, not {value!r}")
    for key in value:
        if key not in ("cpu_m", "memory_mb"):
            raise settings.SettingsError(f"{where}: {what} has no key {key!r}")
    if "cpu_m" not in value or "memory_mb" not in value:
        raise settings.SettingsError(
            f"{where}: {what} needs cpu_m and memory_mb, not {sorted(value)!r}"
        )
    return Weight(
        cpu_m=_count(value["cpu_m"], f"{what} cpu_m", where),
        memory_mb=_count(value["memory_mb"], f"{what} memory_mb", where),
    )


def check_table(raw, where):
    """The validated ``[heavy]`` table, or None when the project sets none.

    ``raw`` is the carried table (the ``[profiles]`` precedent); unknown
    keys, misshapen weights and out-of-range ratios are SettingsError
    (exit 4) naming the project file. An empty command entry queues at
    the default weight.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise settings.SettingsError(f"{where}: [heavy] must be a table")
    for key in raw:
        if key not in (
            "cpu_m",
            "memory_mb",
            "default_weight",
            "commands",
            "starved_ratio",
            "ledger_period",
        ):
            raise settings.SettingsError(f"{where}: [heavy] has no key {key!r}")
    default = Weight()
    if "default_weight" in raw:
        default = _weight_table(raw["default_weight"], "[heavy.default_weight]", where)
    commands = {}
    entries = raw.get("commands", {})
    if not isinstance(entries, Mapping):
        raise settings.SettingsError(f"{where}: [heavy.commands] must be a table")
    for name, entry in entries.items():
        if not isinstance(name, str) or not name:
            raise settings.SettingsError(f"{where}: [heavy.commands] needs a command name")
        if entry is None or (isinstance(entry, Mapping) and not entry):
            commands[name] = default
        else:
            commands[name] = _weight_table(entry, f"[heavy.commands] {name!r}", where)
    ratio = raw.get("starved_ratio", DEFAULT_STARVED_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise settings.SettingsError(
            f"{where}: [heavy] starved_ratio must be a number, not {ratio!r}"
        )
    if not 0 <= ratio <= 1:
        raise settings.SettingsError(
            f"{where}: [heavy] starved_ratio must be between 0 and 1, not {ratio!r}"
        )
    period = raw.get("ledger_period", DEFAULT_LEDGER_PERIOD)
    if period != DEFAULT_LEDGER_PERIOD:
        raise settings.SettingsError(
            f"{where}: [heavy] ledger_period only supports 'month' so far, not {period!r}"
        )
    caps = {}
    for key in ("cpu_m", "memory_mb"):
        if key in raw:
            caps[key] = _count(raw[key], f"[heavy] {key}", where)
    return Config(
        cpu_m=caps.get("cpu_m"),
        memory_mb=caps.get("memory_mb"),
        default=default,
        commands=commands,
        starved_ratio=float(ratio),
        ledger_period=period,
    )


def is_heavy(cfg, command):
    """Whether ``command`` is listed in the lane table. None never matches."""
    return cfg is not None and command in cfg.commands


def weight_for(cfg, command):
    """The config weight for ``command``: its entry (an empty entry queues
    at the default weight, resolved in ``check_table``).

    A missing config or an unlisted command is HeavyError (exit 4): a
    caller that forgot the ``is_heavy`` guard gets a clean refusal,
    never a silent default.
    """
    if cfg is None:
        raise HeavyError(f"command {command!r} is not listed: the lane is off")
    try:
        return cfg.commands[command]
    except (KeyError, TypeError):
        raise HeavyError(f"command {command!r} is not listed in [heavy.commands]") from None


def lane_budget(cfg, host, *, where):
    """The lane cap as a Usage, or None when nothing is enforced.

    Pure: the validated config and the host budget in, the cap out. No
    config means the lane is off; no host budget means every request
    fits and runs unlimited in a scope. Each set cap dimension must fit
    inside the host budget, else SettingsError (exit 4) naming the
    numbers; unset dimensions default to the host budget. Pids stay 0:
    the lane prices CPU and memory only, never pids.
    """
    if cfg is None:
        return None
    if host is None:
        return None
    if not isinstance(host, resources.Usage):
        raise settings.SettingsError(f"{where}: host budget must be a resource Usage, not {host!r}")
    cpu_m = host.cpu_m if cfg.cpu_m is None else cfg.cpu_m
    memory_mb = host.memory_mb if cfg.memory_mb is None else cfg.memory_mb
    if cfg.cpu_m is not None and cfg.cpu_m > host.cpu_m:
        raise settings.SettingsError(
            f"{where}: [heavy] cpu_m cap {cfg.cpu_m} is above the host budget {host.cpu_m}"
        )
    if cfg.memory_mb is not None and cfg.memory_mb > host.memory_mb:
        raise settings.SettingsError(
            f"{where}: [heavy] memory_mb cap {cfg.memory_mb} "
            f"is above the host budget {host.memory_mb}"
        )
    return resources.Usage(cpu_m=cpu_m, memory_mb=memory_mb, pids=0)


def argv_hash(argv):
    """The sha256 hex of the NUL-joined argv, one NUL per element.

    Each element carries its own terminator, so ``["a b", "c"]`` and
    ``["a", "b c"]`` hash differently and a changed command cold-starts.
    """
    digest = hashlib.sha256()
    for element in argv:
        digest.update(os.fsencode(element) + b"\0")
    return digest.hexdigest()


def _checkout_top(cwd):
    """The checkout holding `cwd`: the directory with `.git` at or above it."""
    here = os.path.realpath(cwd)
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return os.path.realpath(cwd)
        here = parent


def repo_key(cwd):
    """The ledger repo key for `cwd`: the checkout's repo entry, else its name.

    The enclosing checkout's ``korapy.toml`` repo entry name when
    present (a linked worktree resolves through its main checkout),
    else the main checkout's directory name. The key never consults
    git: worktree resolution reads the ``.git`` file only, so takeoff
    and a hand run resolve it identically. A corrupt checkout file is
    a SettingsError, like any other config failure.
    """
    top = _checkout_top(cwd)
    main = gitenv.worktree_main_checkout(top) or top
    names = settings.repo_names(main)
    if names:
        return names[0]
    return os.path.basename(main)


def _check_unit(unit):
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_@.:-]*", unit):
        raise HeavyError(f"unit name {unit!r} is not a well-formed scope unit name")
    return unit


def _check_command(command):
    if isinstance(command, (str, bytes)):
        raise HeavyError(f"command {command!r} must be a list of argument strings")
    try:
        items = list(command)
    except TypeError:
        raise HeavyError(f"command {command!r} must be a list of argument strings") from None
    if not items or any(not isinstance(item, str) for item in items):
        raise HeavyError(f"command {command[:1]!r} must be a list of argument strings")
    if items[0].startswith("-"):
        raise HeavyError(f"command {items[:1]!r} must start with a program name")
    return items


def _limit(value, what):
    """The numeric limit, or None when unset; zero reads as unset too."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HeavyError(f"{what} {value!r} must be a positive number")
    if value == 0:
        return None
    if value < 0:
        raise HeavyError(f"{what} {value!r} must be a positive number")
    return value


def scope_argv(unit, command, workdir, env, *, memory_max=None, cpu_quota=None, controllers=None):
    """The `systemd-run --user --scope` argv running `command` with lane limits.

    On the `units.unit_argv` precedent: the scope waits for the command
    (`--wait`, so the runner blocks and the exit is the command's), the
    working directory and environment ride as explicit flags, and the
    limits render the same way (`MemoryMax=` megabytes, `CPUQuota=`
    millicores as a percent). Differences from an agent unit: no
    `--collect` (the unit stays loaded so the post-exit reads land, and
    the required teardown seam releases it after), no runtime cap (the
    run seam's timeout kills a hanging command), and no slice. An unset
    or zero limit sets no property; `controllers`, when given, is the
    delegated controller set and a limit whose controller is missing
    from it is skipped.
    """
    _check_unit(unit)
    items = _check_command(command)
    if not isinstance(workdir, str) or not workdir:
        raise HeavyError(f"workdir {workdir!r} must be a non-empty directory name")
    if isinstance(env, Mapping):
        env_items = [f"{key}={value}" for key, value in env.items()]
    else:
        env_items = list(env)
    props = []
    for value, controller, render in (
        (memory_max, "memory", lambda v: f"MemoryMax={v:g}M"),
        (cpu_quota, "cpu", lambda v: f"CPUQuota={v / 10:g}%"),
    ):
        limit = _limit(value, controller)
        if limit is None:
            continue
        if controllers is not None and controller not in controllers:
            continue
        props.append(f"--property={render(limit)}")
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--wait",
        f"--unit={unit}",
        *props,
        f"--working-directory={workdir}",
        *[f"--setenv={item}" for item in env_items],
        *items,
    ]


def default_run(argv, cwd, timeout):
    """The production subprocess seam.

    A program that cannot start raises OSError for the caller to bucket
    (a missing scope program is a lane failure, never a command exit);
    a past-timeout run re-raises TimeoutExpired (the run never
    completed, so no exit may stand in for it); any other spawn failure
    reads as exit 127.
    """
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise
    except subprocess.SubprocessError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def show_cgroup(unit, run=None):
    """The unit's cgroup path, or None when it cannot be resolved.

    Through the manager (`systemctl --user show <unit> -p ControlGroup`);
    a gone scope reads as None, so the run still records a row with null
    measurements. `run` is injectable for tests; production runs it live.
    """
    _check_unit(unit)
    runner = run or default_run
    try:
        proc = runner(["systemctl", "--user", "show", unit, "-p", "ControlGroup"], None, 30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return None
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ControlGroup" and value.strip():
            return value.strip()
    return None


def stop_scope(unit, run):
    """Release a lane scope. A manager failure raises, never silently leaks.

    ``run`` is required: a missed fake fails closed instead of reaching
    the live manager.
    """
    _check_unit(unit)
    try:
        proc = run(["systemctl", "--user", "stop", unit], None, 60)
    except (OSError, subprocess.SubprocessError) as err:
        raise ScopeUnreachable(f"cannot stop scope {unit}: {err}") from None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        tail = ((getattr(proc, "stderr", "") or "").strip().splitlines() or ["failed"])[-1]
        low = tail.lower()
        if "failed to connect to bus" in low or "no medium found" in low:
            raise ScopeUnreachable(f"cannot stop scope {unit}: {tail}")
        raise HeavyError(f"cannot stop scope {unit}: {tail}")


def _check_label(label, path):
    """A stored host label, or HeavyError when it is not fresh hex.

    Only 32 lowercase hex chars stay a label, so a corrupt or hand-edited
    `host-id` (a path escape, an absolute path, a truncation) fails closed
    instead of redirecting ledger writes out of the lane directory.
    """
    if not isinstance(label, str) or LABEL_RE.fullmatch(label) is None:
        raise HeavyError(f"the host label at {path} is not 32 lowercase hex chars")
    return label


def host_label(state_dir=None):
    """The opaque stable host label, created once under the lane dir.

    Random hex in `<state>/heavy/host-id`, never a hostname or address;
    two hosts never share a ledger file. The one resolver for it. A
    present label that is not fresh hex is refused, never repaired here.
    """
    root = os.path.join(state_dir or paths.state_dir(), HEAVY_DIR)
    path = os.path.join(root, HOST_ID_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            label = handle.read().strip()
        if label:
            return _check_label(label, path)
    except FileNotFoundError:
        pass
    except OSError as err:
        raise HeavyError(f"cannot read the host label at {path}: {err}") from None
    label = uuid.uuid4().hex
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(label)
    except FileExistsError:
        with open(path, encoding="utf-8") as handle:
            label = handle.read().strip()
        if not label:
            raise HeavyError(f"the host label at {path} is empty") from None
        return _check_label(label, path)
    except OSError as err:
        raise HeavyError(f"cannot write the host label at {path}: {err}") from None
    return label


def ledger_path(label=None, period=None, state_dir=None):
    """The raw run ledger file: `<state>/heavy/runs/<host>-<period>.jsonl`.

    One file per host per period, so two hosts never write the same
    file. The one resolver for it. An explicit label is validated like a
    stored one and the period must be a calendar month, so a hostile
    value never becomes a path.
    """
    base = state_dir or paths.state_dir()
    if label is None:
        label = host_label(base)
    else:
        _check_label(label, os.path.join(base, HEAVY_DIR, HOST_ID_FILE))
    if period is None:
        period = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    if PERIOD_RE.fullmatch(period) is None:
        raise HeavyError(f"the ledger period {period!r} is not a calendar month (YYYY-MM)")
    return os.path.join(base, HEAVY_DIR, RUNS_DIR, f"{label}-{period}.jsonl")


def append_ledger_row(path, row):
    """Append one ledger row as a JSON line, creating the runs dir."""
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as err:
        raise HeavyError(f"cannot append the ledger row at {path}: {err}") from None
    return row


def host_info(*, read=None, cpu_count=None, python_version=None):
    """The host descriptor for a ledger row: cores, memory and interpreter.

    A missing core count defaults to the live count, so production rows
    carry cores from day one; unknown (undetermined, boolean or
    non-positive) reads as None, never raising: a row with an unknown
    host still records the run. The reader seam is injectable for tests.
    """
    reader = read or _read_text
    try:
        meminfo = reader("/proc/meminfo")
    except OSError:
        meminfo = None
    total_mb = None
    if isinstance(meminfo, str):
        for line in meminfo.splitlines():
            key, _, rest = line.partition(":")
            if key.strip() == "MemTotal":
                try:
                    total_mb = round(int(rest.split()[0]) / 1024, 1)
                except (ValueError, IndexError):
                    total_mb = None
                break
    if cpu_count is None:
        cpu_count = os.cpu_count()
    if isinstance(cpu_count, bool):
        cores = None
    else:
        try:
            cores = int(cpu_count) if cpu_count is not None else None
        except (TypeError, ValueError):
            cores = None
        if cores is not None and cores <= 0:
            cores = None
    return {
        "cores": cores,
        "mem_mb": total_mb,
        "python": python_version if python_version is not None else platform.python_version(),
    }


def _read_text(path):
    """One file's text, through a reader the tests can replace."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _whole(text):
    """A non-negative int from file text, else None."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text if text >= 0 else None
    if not isinstance(text, str):
        return None
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_cpu_stat(text):
    """`(usage_usec, nr_throttled, throttled_usec)` from `cpu.stat` text."""
    usage = throttled = throttled_us = None
    if isinstance(text, str):
        for line in text.splitlines():
            key, _, value = line.partition(" ")
            key = key.strip()
            if key == "usage_usec" and usage is None:
                usage = _whole(value)
            elif key == "nr_throttled" and throttled is None:
                throttled = _whole(value)
            elif key == "throttled_usec" and throttled_us is None:
                throttled_us = _whole(value)
    return usage, throttled, throttled_us


def _parse_pressure_total(text):
    """The `total=` microseconds of the `some` line of a pressure file."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        if line.startswith("some"):
            for token in line.split():
                if token.startswith("total="):
                    return _whole(token[len("total=") :])
    return None


def _capture(unit, *, scope_cgroup, read, starved_ratio, wall_s):
    """The post-exit measurement for `unit`, or nulls when the cgroup is gone.

    Resolves the unit cgroup through the manager, then reads the five
    per-job counters through the file-reader seam. Garbage reads as
    null fields, never an error: a corrupt cgroup never blocks the row.
    """
    nulls = {
        "cpu_s": None,
        "peak_mb": None,
        "pids_peak": None,
        "avg_cores": None,
        "throttled_us": None,
        "cpu_pressure_us": None,
        "mem_pressure_us": None,
        "starved": False,
    }
    cgroup = scope_cgroup(unit)
    if not cgroup:
        return nulls
    texts = {}
    for name in ("cpu.stat", "memory.peak", "pids.peak", "cpu.pressure", "memory.pressure"):
        try:
            texts[name] = read(os.path.join(cgroup, name))
        except OSError:
            texts[name] = None
    if all(text is None for text in texts.values()):
        return nulls
    usage, _, throttled_us = _parse_cpu_stat(texts["cpu.stat"])
    peak = _whole(texts["memory.peak"])
    pids = _whole(texts["pids.peak"])
    cpu_pressure = _parse_pressure_total(texts["cpu.pressure"])
    mem_pressure = _parse_pressure_total(texts["memory.pressure"])
    cpu_s = usage / 1_000_000 if usage is not None else None
    starved = False
    if cpu_pressure is not None and wall_s > 0:
        starved = (cpu_pressure / (wall_s * 1_000_000)) > starved_ratio
    return {
        "cpu_s": cpu_s,
        "peak_mb": round(peak / (1024 * 1024), 1) if peak is not None else None,
        "pids_peak": pids,
        "avg_cores": round(cpu_s / wall_s, 3) if cpu_s is not None and wall_s > 0 else None,
        "throttled_us": throttled_us,
        "cpu_pressure_us": cpu_pressure,
        "mem_pressure_us": mem_pressure,
        "starved": starved,
    }


def _manager_failure(stderr):
    """The lane-failure bucket for a nonzero scope start, else None.

    A scope that never started is a lane failure, not a command
    failure: the user manager unreachable reads as exit 3, any other
    start failure as exit 4. A command's own stderr can only collide
    when it names a manager failure verbatim, the same twin the units
    start path accepts.
    """
    low = (stderr or "").lower()
    if "failed to connect to bus" in low or "no medium found" in low:
        return "unreachable"
    if "failed to start transient scope" in low:
        return "failed"
    return None


def run_heavy(
    argv,
    cwd,
    env,
    *,
    role,
    repo,
    weight,
    budget,
    run,
    teardown,
    scope_cgroup,
    read=None,
    clock=None,
    host=None,
    pressure=None,
    timeout,
    starved_ratio=DEFAULT_STARVED_RATIO,
):
    """Run one heavy command in a transient scope and record its ledger row.

    `argv` is the command (takeoff wraps its shell strings as
    `["/bin/sh", "-c", cmd]` so the scope runs exactly what the plain
    path would); `weight` is the request weight from config;
    `budget` is the lane budget, None when nothing is enforced (the
    scope is still created for measurement but carries no limits).
    `run`, `teardown` and `scope_cgroup` are required seams with no
    default. `read`, `clock`, `host` and `pressure` default to the live
    readers. `timeout` caps this run only and is required.
    `starved_ratio` decides the row's starved flag; the caller passes
    the configured ratio.

    Returns a RunResult: the raw command exit, the mapped exit (a
    public code passthrough, anything else 1), the combined output for
    the caller's failure rows, and the row keys. Raises
    subprocess.TimeoutExpired past `timeout` (no row: the run never
    completed), ScopeUnreachable when the manager cannot be reached,
    HeavyError naming the program when the scope spawn itself fails
    (no row, no teardown: the scope never started), and HeavyError on
    other lane failures. Teardown runs after the row lands, so a failed
    release is loud without losing the measurement.
    """
    if not isinstance(role, str) or not role:
        raise HeavyError("the ledger role must be a non-empty name")
    if not isinstance(repo, str) or not repo:
        raise HeavyError("the ledger repo must be a non-empty name")
    if not isinstance(weight, Weight):
        raise HeavyError(f"weight must be a lane Weight, not {weight!r}")
    if budget is not None and not isinstance(budget, resources.Usage):
        raise HeavyError(f"budget must be a resource Usage, not {budget!r}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise HeavyError(f"timeout {timeout!r} must be a positive number of seconds")
    now = clock or time.time
    start = now()
    unit = UNIT_PREFIX + uuid.uuid4().hex
    if budget is None:
        memory_max, cpu_quota = None, None
    else:
        memory_max, cpu_quota = weight.memory_mb, weight.cpu_m
    scope = scope_argv(unit, argv, cwd, env, memory_max=memory_max, cpu_quota=cpu_quota)
    try:
        proc = run(scope, cwd, timeout)
    except OSError as err:
        raise HeavyError(f"cannot start scope {unit}: {scope[0]} could not run: {err}") from None
    except Exception:
        teardown(unit)
        raise
    code = getattr(proc, "returncode", 1)
    output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    bucket = _manager_failure(getattr(proc, "stderr", "") or "") if code != 0 else None
    if bucket == "unreachable":
        raise ScopeUnreachable(f"cannot start scope {unit}: manager unreachable")
    if bucket == "failed":
        raise HeavyError(f"cannot start scope {unit}: the scope failed to start")
    end = now()
    wall_s = round(end - start, 3)
    reader = read or _read_text
    measured = _capture(
        unit,
        scope_cgroup=scope_cgroup,
        read=reader,
        starved_ratio=starved_ratio,
        wall_s=wall_s,
    )
    descriptor = host if host is not None else host_info()
    reading = resources.pressure_dict(pressure)
    label = host_label()
    row = {
        "schema_version": SCHEMA_VERSION,
        "at": datetime.datetime.fromtimestamp(start, datetime.timezone.utc).isoformat(),
        "host": {"label": label, **descriptor},
        "repo": repo,
        "argv_hash": argv_hash(list(argv)),
        "role": role,
        "argv": list(argv),
        "wall_s": wall_s,
        "cpu_s": measured["cpu_s"],
        "peak_mb": measured["peak_mb"],
        "pids_peak": measured["pids_peak"],
        "avg_cores": measured["avg_cores"],
        "throttled_us": measured["throttled_us"],
        "cpu_pressure_us": measured["cpu_pressure_us"],
        "mem_pressure_us": measured["mem_pressure_us"],
        "starved": measured["starved"],
        "exit": code,
        "waited_s": 0.0,
        "weight": {"cpu_m": weight.cpu_m, "memory_mb": weight.memory_mb},
        "host_pressure": reading,
    }
    period = datetime.datetime.fromtimestamp(start, datetime.timezone.utc).strftime("%Y-%m")
    try:
        append_ledger_row(ledger_path(label=label, period=period), row)
    finally:
        teardown(unit)
    return RunResult(
        command_exit=code,
        exit_code=code if code in PUBLIC_CODES else 1,
        output=output,
        weight=weight,
        wall_s=wall_s,
        unit=unit,
        argv_hash=row["argv_hash"],
    )
