"""Transient systemd user units for agents: names, the spawn lock, start, stop.

One unit per ticket generation: `<prefix><repo>-<number>` for the first, and
`<prefix><repo>-<number>-r<gen>` for each later one (a resume). The prefix
comes from `agents.unit_prefix`, so several fleets can share a host. A unit
without the prefix is never listed, counted or stopped here.

Each start is appended to `units.log` in the ticket's session dir. That file
decides the next generation, so a unit name is not reused while the log
keeps it.

Every subprocess goes through one seam. `list_units` and `start` take `run`.
`stop` takes `list_fn` and `stop_fn`, both required, and never falls back to
`systemctl_stop`, the one production stop (AGENTS.md, "Destructive seams
have no default").

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
_GEN = re.compile(r"-r([1-9][0-9]*)$")


class UnitsError(Exception):
    """A unit could not be started or stopped, or the input is unusable (exit 4)."""

    exit_code = 4


class UnitExists(UnitsError):
    """The ticket's unit is already running: race lost or already running (exit 2)."""

    exit_code = 2


class Unreachable(UnitsError):
    """systemctl cannot be asked: fail closed, never read as "no units" (exit 3)."""

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
    if not isinstance(repo, str) or not paths.REPO_NAME.fullmatch(repo):
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
    if not match:
        return None
    return match.group("repo"), int(match.group("number")), int(match.group("gen") or 0)


def _gen_of(unit):
    """The generation a unit name carries: its `-r<gen>` suffix, else 0."""
    match = _GEN.search(unit)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------- listing


def list_units(prefix, run=default_run):
    """[(unit, active state, sub state)] for this prefix's units, from systemctl.

    Raises Unreachable when systemctl cannot run or fails.
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
        fields = line.split()
        if len(fields) >= 4:
            unit = fields[0].removesuffix(".service")
            if parse_unit(prefix, unit):
                out.append((unit, fields[2], fields[3]))
    return out


def live_tickets(prefix, units):
    """{(repo, number)} for every unit in one of LIVE_STATES."""
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
    HOME are passed through explicitly.
    """
    _check_int(runtime_max_s, "runtime_max_s", 1)
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
        *list(command),
    ]


def start(argv, session_dir, unit, run=default_run):
    """Run systemd-run, then record the unit in the session's units.log.

    "already exists" raises UnitExists (exit 2); any other failure raises
    UnitsError (exit 4).
    """
    try:
        proc = run(argv, timeout=START_TIMEOUT_S)
    except OSError as err:
        raise UnitsError(f"cannot start {unit}: {err}") from err
    if proc.returncode != 0:
        tail = ((proc.stderr or "").strip().splitlines() or ["failed"])[-1]
        if "already" in tail or "exists" in tail:
            raise UnitExists(f"{unit}: {tail}")
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
    """The one production stop: `systemctl --user stop <unit>`. Never call it from a test."""
    proc = default_run(["systemctl", "--user", "stop", unit], timeout=STOP_TIMEOUT_S)
    if proc.returncode != 0:
        raise UnitsError(f"cannot stop {unit}: {(proc.stderr or '').strip()}")


def stop(prefix, repo, number, *, list_fn, stop_fn):
    """Stop every live unit of one ticket. Returns the names stopped.

    `list_fn(prefix)` returns the units; `stop_fn(unit)` stops one. Both are
    required, so a caller that forgets one fails with TypeError rather than
    reaching systemctl.
    """
    _check_prefix(prefix)
    stopped = []
    for name, active, _ in list_fn(prefix):
        parsed = parse_unit(prefix, name)
        if parsed and parsed[0] == repo and parsed[1] == number and active in LIVE_STATES:
            stop_fn(name)
            stopped.append(name)
    return stopped
