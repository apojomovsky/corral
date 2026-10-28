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


def unit_argv(unit, workdir, session_dir, command, env, runtime_max_s):
    """The `systemd-run --user` argv that starts `command` as a transient unit.

    stdout and stderr append to the session's out.log and err.log. `env` is
    `K=V` strings, or a mapping; each item becomes one `--setenv`. PATH and
    HOME are passed through explicitly. The command is a program and its
    arguments; a first element that is an option is refused.
    """
    _check_int(runtime_max_s, "runtime_max_s", 1)
    command = list(command)
    if not command or not isinstance(command[0], str) or command[0].startswith("-"):
        raise UnitsError(f"command {command[:1]!r} must start with a program name")
    if isinstance(env, Mapping):
        items = [f"{key}={value}" for key, value in env.items()]
    else:
        items = list(env)
    return [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        f"--unit={unit}",
        f"--property=RuntimeMaxSec={runtime_max_s}",
        f"--property=StandardOutput=append:{os.path.join(session_dir, 'out.log')}",
        f"--property=StandardError=append:{os.path.join(session_dir, 'err.log')}",
        f"--working-directory={workdir}",
        f"--setenv=PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
        f"--setenv=HOME={os.path.expanduser('~')}",
        *[f"--setenv={item}" for item in items],
        *command,
    ]


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
        low = tail.lower()
        if "already exists" in low or "already loaded" in low:
            raise UnitExists(f"{unit}: {tail}")
        if "failed to connect to bus" in low or "no medium found" in low:
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
