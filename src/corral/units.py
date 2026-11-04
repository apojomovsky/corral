"""Transient systemd user units for agents: names, the spawn lock, start, stop.

One unit per ticket generation: `<prefix><repo>-<number>` for the first, and
`<prefix><repo>-<number>-r<gen>` for each later one (a resume). The prefix
comes from `agents.unit_prefix`, so several fleets can share a host. A unit
without the prefix is never listed, counted or stopped here.

Each start is appended to `units.log` in the ticket's session dir. That file
decides the next generation, and it is the proof of ownership: a live unit
is stopped only when its name is in the log, because a prefix can parse
another fleet's unit as one of ours.

Every subprocess goes through one seam. `list_units` and `start` take `run`.
`stop` takes `list_fn` and `stop_fn`, both required, and never falls back to
the production stop (AGENTS.md, "Destructive seams have no default").

Errors map to the public exit codes: UnitExists 2, Unreachable 3, UnitsError 4.
"""

import contextlib
import fcntl
import os
import re
import subprocess
from collections.abc import Mapping

from corral import paths, settings

UNITS_LOG = "units.log"
SPAWN_LOCK = "spawn.lock"
LIST_TIMEOUT_S = 30
START_TIMEOUT_S = 60
STOP_TIMEOUT_S = 60

#: ACTIVE states that hold a slot. A unit still starting or stopping counts,
#: so a stopping unit cannot let the cap be exceeded for a moment.
LIVE_STATES = ("active", "activating", "deactivating", "reloading")

settings.register(
    "agents.unit_prefix", str, "corral-agent-", "prefix of transient agent unit names"
)

_PREFIX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_TAIL = re.compile(
    rf"(?P<repo>{paths.REPO_NAME.pattern})-(?P<number>[1-9][0-9]*)"
    r"(?:-r(?P<gen>[1-9][0-9]*))?"
)
_WELL_FORMED = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*-[1-9][0-9]*(?:-r[1-9][0-9]*)?")
_GEN = re.compile(r"-r([1-9][0-9]*)$")


class UnitsError(Exception):
    """A unit could not be started or stopped, or the input is unusable (exit 4)."""

    exit_code = 4


class UnitExists(UnitsError):
    """The ticket's unit is already running: race lost or already running (exit 2)."""

    exit_code = 2


class Unreachable(UnitsError):
    """The service manager cannot be asked: fail closed, never "no units" (exit 3)."""

    exit_code = 3


def default_run(argv, timeout):
    """The production subprocess seam. A program that cannot run reads as a failure."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _check_prefix(prefix):
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise UnitsError(f"unit prefix {prefix!r} must be letters, digits, '_', '.' or '-'")
    return prefix


def _check_repo(repo):
    if not paths.plain_repo_name(repo):
        raise UnitsError(f"repo name {repo!r} is not a plain repository name")


def _check_int(value, name, least):
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise UnitsError(f"{name} {value!r} must be an integer of {least} or more")
    return value


# ---------------------------------------------------------------- names


def unit_name(prefix, repo, number, gen=0):
    """`<prefix><repo>-<number>`, plus `-r<gen>` when gen > 0."""
    _check_prefix(prefix)
    _check_repo(repo)
    _check_int(number, "ticket number", 1)
    _check_int(gen, "generation", 0)
    name = f"{prefix}{repo}-{number}"
    return f"{name}-r{gen}" if gen else name


def parse_unit(prefix, name):
    """(repo, number, gen) for a unit of this prefix, else None.

    Accepts an optional `.service` suffix. It is the inverse of `unit_name`:
    the number is the last digit run before an optional `-r<gen>`, so a repo
    with hyphens or digits in its name still splits the same way.
    """
    _check_prefix(prefix)
    if not isinstance(name, str):
        return None
    if name.endswith(".service"):
        name = name[: -len(".service")]
    if not name.startswith(prefix):
        return None
    match = _TAIL.fullmatch(name[len(prefix) :])
    if not match or not paths.plain_repo_name(match.group("repo")):
        return None
    return match.group("repo"), int(match.group("number")), int(match.group("gen") or 0)


def _gen_of(unit):
    """The generation a unit name carries: its `-r<gen>` suffix, else 0."""
    match = _GEN.search(unit)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------- listing


def list_units(prefix, run=default_run):
    """[(unit, active state, sub state)] for this prefix's units, from the service manager.

    Raises Unreachable when the query cannot run, fails, or prints a line in a
    shape it does not know: a format change is never read as "no units".
    """
    _check_prefix(prefix)
    argv = [
        "systemctl",
        "--user",
        "list-units",
        f"{prefix}*",
        "--all",
        "--no-legend",
        "--no-pager",
        "--plain",
    ]
    try:
        proc = run(argv, timeout=LIST_TIMEOUT_S)
    except OSError as err:
        raise Unreachable(f"cannot list units: {err}") from err
    if proc.returncode != 0:
        raise Unreachable(f"cannot list units: {(proc.stderr or '').strip()}")
    out = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 4:
            raise Unreachable(f"cannot read the unit list, unexpected line: {line!r}")
        unit = fields[0].removesuffix(".service")
        if parse_unit(prefix, unit):
            out.append((unit, fields[2], fields[3]))
    return out


def live_tickets(prefix, units):
    """{(repo, number)} for every unit in one of LIVE_STATES.

    With overlapping prefixes this can over-count, since another fleet's unit
    may parse as one of ours. Over-counting only makes admission more
    conservative, never less.
    """
    out = set()
    for name, active, _ in units:
        parsed = parse_unit(prefix, name)
        if parsed and active in LIVE_STATES:
            out.add((parsed[0], parsed[1]))
    return out


# ------------------------------------------------------------- locking


@contextlib.contextmanager
def spawn_lock():
    """One host-wide flock at `<state>/spawn.lock`. Admission and start run under it.

    Not reentrant: nesting it in the same process deadlocks.
    """
    state = paths.state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, SPAWN_LOCK), "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ------------------------------------------------------------ starting


#: The host-wide slice holding every project's share (MemoryMax, CPUQuota).
HOST_SLICE = "corral.slice"

#: The controllers a budget needs. Every launch with a host budget refuses
#: when one is not delegated; see `delegated_controllers`.
REQUIRED_CONTROLLERS = ("cpu", "memory", "pids")

#: Controllers corral sets no property for. An absent one is silently
#: skipped by `unit_argv`; it never refuses a launch.
OPTIONAL_CONTROLLERS = ("cpuset", "io")


def _limit(value, name):
    """The numeric limit, or None when unset; a bad limit is a usage error.

    None and zero read as unset: an unpriced role costs nothing and sets
    no property. Anything else must be a positive number.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnitsError(f"{name} {value!r} must be a positive number")
    if value == 0:
        return None
    if value < 0:
        raise UnitsError(f"{name} {value!r} must be a positive number")
    return value


def _memory_assignment(value):
    return f"MemoryMax={value:g}M"


def _cpu_assignment(value):
    return f"CPUQuota={value / 10:g}%"


def _tasks_assignment(value):
    return f"TasksMax={value:g}"


def _memory_prop(value):
    return f"--property={_memory_assignment(value)}"


def _cpu_prop(value):
    return f"--property={_cpu_assignment(value)}"


def _tasks_prop(value):
    return f"--property={_tasks_assignment(value)}"


def _limit_props(memory_max, cpu_quota, tasks_max):
    """The `(controller, property)` lines for the set limits, in argv order."""
    props = []
    for value, controller, render, label in (
        (memory_max, "memory", _memory_prop, "memory_max"),
        (cpu_quota, "cpu", _cpu_prop, "cpu_quota"),
        (tasks_max, "pids", _tasks_prop, "tasks_max"),
    ):
        limit = _limit(value, label)
        if limit is not None:
            props.append((controller, render(limit)))
    return props


def _classify(tail):
    """The start-like failure bucket for a stderr tail: exists, unreachable or failed."""
    low = tail.lower()
    if "already exists" in low or "already loaded" in low:
        return "exists"
    if "failed to connect to bus" in low or "no medium found" in low:
        return "unreachable"
    return "failed"


def unit_argv(
    unit,
    workdir,
    session_dir,
    command,
    env,
    runtime_max_s,
    *,
    slice=None,
    memory_max=None,
    cpu_quota=None,
    tasks_max=None,
    controllers=None,
):
    """The `systemd-run --user` argv that starts `command` as a transient unit.

    stdout and stderr append to the session's out.log and err.log. `env` is
    `K=V` strings, or a mapping; each item becomes one `--setenv`. PATH and
    HOME are passed through explicitly. The command is a program and its
    arguments; a first element that is an option is refused.

    `slice` places the unit in a slice, and the limits cap it through the
    kernel: memory megabytes, CPU millicores (as a quota percent), and
    pids. An unset or zero limit sets no property. `controllers`, when
    given, is the delegated controller set: a limit whose controller is
    not in it is skipped, and unknown names are ignored.
    """
    _check_int(runtime_max_s, "runtime_max_s", 1)
    command = list(command)
    if not command or not isinstance(command[0], str) or command[0].startswith("-"):
        raise UnitsError(f"command {command[:1]!r} must start with a program name")
    if isinstance(env, Mapping):
        items = [f"{key}={value}" for key, value in env.items()]
    else:
        items = list(env)
    extra = []
    if slice is not None:
        if not isinstance(slice, str) or not slice:
            raise UnitsError(f"slice {slice!r} must be a non-empty slice name")
        extra.append(f"--property=Slice={slice}")
    available = None if controllers is None else set(controllers)
    for controller, prop in _limit_props(memory_max, cpu_quota, tasks_max):
        if available is not None and controller not in available:
            continue
        extra.append(prop)
    return [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        f"--unit={unit}",
        f"--property=RuntimeMaxSec={runtime_max_s}",
        f"--property=StandardOutput=append:{os.path.join(session_dir, 'out.log')}",
        f"--property=StandardError=append:{os.path.join(session_dir, 'err.log')}",
        *extra,
        f"--working-directory={workdir}",
        f"--setenv=PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
        f"--setenv=HOME={os.path.expanduser('~')}",
        *[f"--setenv={item}" for item in items],
        *command,
    ]


def project_slice(project):
    """`corral-<project>.slice`: the slice holding one project's agents.

    The host slice parents it; the project's agent units run inside it.
    """
    if not isinstance(project, str) or not _PREFIX.fullmatch(project):
        raise UnitsError(f"project {project!r} cannot name a slice")
    return f"corral-{project}.slice"


#: Root of the kernel's cgroup filesystem.
CGROUP_ROOT = "/sys/fs/cgroup"


def _check_uid(uid):
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise UnitsError(f"uid {uid!r} must be an integer of zero or more")
    return uid


def user_service_dir(uid=None):
    """The assumed cgroup dir of the login user's manager.

    The user manager's layout is assumed, not probed: a wrong guess
    only blanks the measured columns, since missing paths read as
    unknown. The caller owns `uid`; None reads as this process's own.
    """
    if uid is None:
        uid = os.getuid()
    _check_uid(uid)
    return os.path.join(CGROUP_ROOT, "user.slice", f"user-{uid}.slice", f"user@{uid}.service")


def project_cgroup(project, uid=None):
    """The assumed cgroup dir of one project's slice, under the host slice."""
    return os.path.join(user_service_dir(uid), HOST_SLICE, project_slice(project))


def agent_cgroup(project, unit, uid=None):
    """The assumed cgroup leaf of one agent unit, under its project slice."""
    if not isinstance(unit, str) or not _WELL_FORMED.fullmatch(unit):
        raise UnitsError(f"unit name {unit!r} is not a well-formed agent unit name")
    return os.path.join(project_cgroup(project, uid), f"{unit}.service")


def controllers_path(uid=None):
    """The user slice's `cgroup.controllers` file: what the manager may enforce with.

    Built from `CGROUP_ROOT` beside `user_service_dir`, never a second
    hard-coded prefix; the uid is validated the same way, so a
    non-integer never renders into a path.
    """
    if uid is None:
        uid = os.getuid()
    _check_uid(uid)
    return os.path.join(CGROUP_ROOT, "user.slice", f"user-{uid}.slice", "cgroup.controllers")


def read_controllers(path=None, *, read=None):
    """The controllers delegated to the user slice, as a frozenset of names.

    `read(path)` is the injected file seam; the default reads the real
    file. An unreadable file raises UnitsError: without proof of
    delegation the launch refuses rather than running unlimited.
    """
    target = path or controllers_path()
    try:
        text = read(target) if read is not None else open(target, encoding="utf-8").read()
    except OSError as err:
        raise UnitsError(f"cannot read the delegated controllers at {target}: {err}") from None
    return frozenset(text.split())


def check_controllers(available):
    """Raise UnitsError naming every required controller missing from `available`."""
    missing = [name for name in REQUIRED_CONTROLLERS if name not in set(available or ())]
    if missing:
        raise UnitsError(
            f"the user slice does not delegate {', '.join(missing)} "
            f"(see {controllers_path()}); the budget cannot be enforced"
        )


def delegated_controllers(path=None, *, read=None):
    """The delegated controllers, requiring every required one.

    Reads through `read_controllers` and refuses when a required
    controller is missing. Optional controllers simply read as absent;
    `unit_argv` skips the properties they would need.
    """
    available = read_controllers(path, read=read)
    check_controllers(available)
    return available


def _check_slice_name(name):
    if not isinstance(name, str) or not name.endswith(".slice") or name == ".slice":
        raise UnitsError(f"slice name {name!r} is not a well-formed slice unit name")
    return name


def slice_start_argv(name):
    """The `systemctl --user start` argv creating (or reusing) one slice.

    Starting an already-active slice is success. A name with dashes
    nests by itself: `corral-web.slice` starts under `corral.slice`
    with no extra flag.
    """
    _check_slice_name(name)
    return ["systemctl", "--user", "start", name]


def slice_set_property_argv(name, *, memory_max=None, cpu_quota=None):
    """The `systemctl --user set-property --runtime` argv for slice limits.

    None when no limit is set: assigning nothing is refused by the
    manager, so the caller skips the call. Tasks never ride on slices:
    the launch sets TasksMax on the agent unit inside the slice.
    """
    _check_slice_name(name)
    props = []
    for value, render, label in (
        (memory_max, _memory_assignment, "memory_max"),
        (cpu_quota, _cpu_assignment, "cpu_quota"),
    ):
        limit = _limit(value, label)
        if limit is not None:
            props.append(render(limit))
    if not props:
        return None
    return ["systemctl", "--user", "set-property", "--runtime", name, *props]


def ensure_slice(name, *, memory_max=None, cpu_quota=None, run):
    """Create one slice with its limits; an existing slice reads as created.

    Start is idempotent, so a slice that is already loaded is success,
    not a race. Limits apply as runtime properties, reconciled on every
    launch. Other failures map like `start`: an unreachable bus is
    Unreachable (exit 3), and anything else, manager text naming a
    refused property included, is UnitsError (exit 4). `run` is
    required: slice creation is a write seam.
    """
    start = slice_start_argv(name)
    props = slice_set_property_argv(name, memory_max=memory_max, cpu_quota=cpu_quota)
    for argv in [start, *([props] if props is not None else [])]:
        try:
            proc = run(argv, timeout=START_TIMEOUT_S)
        except OSError as err:
            raise UnitsError(f"cannot create slice {name}: {err}") from err
        if proc.returncode != 0:
            tail = ((proc.stderr or "").strip().splitlines() or ["failed"])[-1]
            bucket = _classify(tail)
            if bucket == "exists":
                continue
            if bucket == "unreachable":
                raise Unreachable(f"cannot create slice {name}: {tail}")
            raise UnitsError(f"cannot create slice {name}: {tail}")


def start(argv, session_dir, unit, run=default_run):
    """Run the start argv, then record the unit in the session's units.log.

    The argv must carry `--unit=<unit>`, and the unit must be well formed.
    "already exists" or "already loaded" (any case) raises UnitExists (exit
    2); a user manager that cannot be reached raises Unreachable (exit 3);
    any other failure raises UnitsError (exit 4).
    """
    if not isinstance(unit, str) or not _WELL_FORMED.fullmatch(unit):
        raise UnitsError(f"unit name {unit!r} is not a well-formed agent unit name")
    if f"--unit={unit}" not in argv:
        raise UnitsError(f"the start argv does not name {unit}")
    try:
        proc = run(argv, timeout=START_TIMEOUT_S)
    except OSError as err:
        raise UnitsError(f"cannot start {unit}: {err}") from err
    if proc.returncode != 0:
        tail = ((proc.stderr or "").strip().splitlines() or ["failed"])[-1]
        bucket = _classify(tail)
        if bucket == "exists":
            raise UnitExists(f"{unit}: {tail}")
        if bucket == "unreachable":
            raise Unreachable(f"cannot start {unit}: {tail}")
        raise UnitsError(f"cannot start {unit}: {tail}")
    try:
        with open(os.path.join(session_dir, UNITS_LOG), "a", encoding="utf-8") as handle:
            handle.write(unit + "\n")
    except OSError as err:
        raise UnitsError(f"{unit} started but was not recorded: {err}") from err


def started_units(session_dir):
    """The unit names recorded for this session, oldest first. Empty when none."""
    try:
        with open(os.path.join(session_dir, UNITS_LOG), encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]
    except FileNotFoundError:
        return []
    except OSError as err:
        raise UnitsError(f"cannot read {UNITS_LOG}: {err}") from err


def next_gen(session_dir):
    """The generation the next start takes: 0 for the first, then one past the highest."""
    gens = [_gen_of(unit) for unit in started_units(session_dir)]
    return (max(gens) + 1) if gens else 0


# ------------------------------------------------------------ stopping


def systemctl_stop(unit):
    """The one production stop of a unit. Never call it from a test."""
    proc = default_run(["systemctl", "--user", "stop", unit], timeout=STOP_TIMEOUT_S)
    if proc.returncode != 0:
        raise UnitsError(f"cannot stop {unit}: {(proc.stderr or '').strip()}")


def _owned_live(prefix, repo, number, session_dir, list_fn):
    """(owned, unproven): live units that parse to the ticket, split by the log."""
    logged = set(started_units(session_dir))
    owned, unproven = [], []
    for name, active, _ in list_fn(prefix):
        parsed = parse_unit(prefix, name)
        if not parsed or parsed[:2] != (repo, number) or active not in LIVE_STATES:
            continue
        (owned if name in logged else unproven).append(name)
    return owned, unproven


def unproven(prefix, repo, number, *, session_dir, list_fn):
    """Live units that parse to this ticket but are not in its units.log.

    `stop` never touches these. They are reported here so a caller can warn
    about a unit that another fleet, or a stale prefix, may own.
    """
    _check_prefix(prefix)
    _check_repo(repo)
    _check_int(number, "ticket number", 1)
    return _owned_live(prefix, repo, number, session_dir, list_fn)[1]


def stop(prefix, repo, number, *, session_dir, list_fn, stop_fn):
    """Stop every live unit of one ticket that this ticket's log proves it started.

    A unit is stopped only when it is live, parses to exactly (repo, number),
    and its name is in `units.log` under `session_dir`. Live units that parse
    but are not logged are left alone (see `unproven`). Returns the names
    stopped. When `stop_fn` fails partway, the raised error names the units
    already stopped.

    `list_fn(prefix)` returns the units; `stop_fn(unit)` stops one. Every
    argument is required, so a caller that forgets one fails with TypeError.
    """
    _check_prefix(prefix)
    _check_repo(repo)
    _check_int(number, "ticket number", 1)
    owned, _ = _owned_live(prefix, repo, number, session_dir, list_fn)
    stopped = []
    for name in owned:
        try:
            stop_fn(name)
        except UnitsError as err:
            done = ", ".join(stopped) or "none"
            raise type(err)(f"stopped {done}; {name} failed: {err}") from err
        stopped.append(name)
    return stopped
