"""The omp adapter: `omp -p`, one headless run per session.

`omp -p` prints nothing until the session ends, so the activity is read from
the session `.jsonl` files in the session directory, not from the unit's
out.log. The per-launch config is `overlay.yml`, written by hand (stdlib
only, no YAML library) from a dict shaped like omp's own setting ids.
"""

import functools
import json
import os
import re
import shutil

from corral import paths
from corral.runtimes import DEFAULT_MAX_TIME

NAME = "omp"
#: Where omp installs for a user, when PATH does not have it.
FALLBACK = "~/.local/bin/omp"
CONFIG_NAME = "overlay.yml"
SESSION_SUFFIX = ".jsonl"
#: A headless turn kills background jobs, so omp never backgrounds one. omp
#: backgrounds bash calls over a minute, and runs task sub-agents as background
#: jobs while `async.enabled` holds; in `-p` mode the agent would end its turn to
#: wait and the session would end with it. The keys are omp's own setting ids,
#: nested as YAML (a bare `autoBackground: false` is not read).
FORCED = {"bash": {"autoBackground": {"enabled": False}}, "async": {"enabled": False}}

_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


@functools.cache
def binary():
    """The omp binary: on PATH, else the usual install location. Found once."""
    return shutil.which("omp") or os.path.expanduser(FALLBACK)


def _merge(base, forced):
    """`base` with `forced` laid over it, key by key; `forced` always wins."""
    out = dict(base)
    for key, value in forced.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a valid YAML double-quoted scalar.
        return json.dumps(value)
    raise TypeError(f"cannot write {type(value).__name__} as an overlay value")


def _key(key):
    key = str(key)
    return key if _PLAIN_KEY.fullmatch(key) else json.dumps(key)


def _yaml_lines(mapping, depth=0):
    pad = "  " * depth
    lines = []
    for key, value in mapping.items():
        name = _key(key)
        if isinstance(value, dict):
            if value:
                lines.append(f"{pad}{name}:")
                lines += _yaml_lines(value, depth + 1)
            else:
                lines.append(f"{pad}{name}: {{}}")
        elif isinstance(value, (list, tuple)):
            if value:
                lines.append(f"{pad}{name}:")
                lines += [f"{pad}  - {_scalar(item)}" for item in value]
            else:
                lines.append(f"{pad}{name}: []")
        else:
            lines.append(f"{pad}{name}: {_scalar(value)}")
    return lines


def overlay_text(config):
    """The overlay YAML for `config`, with the headless keys forced on top."""
    return "\n".join(_yaml_lines(_merge(config, FORCED))) + "\n"


def write_config(session_dir, config):
    """Write `overlay.yml` into the session dir, atomically."""
    os.makedirs(session_dir, exist_ok=True)
    paths.atomic_write_text(os.path.join(session_dir, CONFIG_NAME), overlay_text(config))


def argv(session_dir, prompt, *, model=None, resume=None, max_time=DEFAULT_MAX_TIME):
    command = [
        binary(),
        "-p",
        "--session-dir",
        session_dir,
        "--max-time",
        max_time,
        "--config",
        os.path.join(session_dir, CONFIG_NAME),
        "--auto-approve",
    ]
    if resume:
        command += ["-r", resume]
    if model:
        command += ["--model", model]
    return command + [prompt]


def env(session_dir):
    return {}


def _sessions(session_dir: str) -> list[str]:
    try:
        names: list[str] = [n for n in os.listdir(session_dir) if n.endswith(SESSION_SUFFIX)]
    except OSError:
        return []
    return [os.path.join(session_dir, n) for n in names]


def resume_handle(session_dir):
    """Absolute path of the newest session `.jsonl`, or None."""
    found = _sessions(session_dir)
    if not found:
        return None
    newest: str = max(found, key=os.path.getmtime)
    return os.path.abspath(newest)


def activity_file(session_dir):
    return resume_handle(session_dir)


class Omp:
    name = NAME
    argv = staticmethod(argv)
    env = staticmethod(env)
    write_config = staticmethod(write_config)
    resume_handle = staticmethod(resume_handle)
    activity_file = staticmethod(activity_file)


RUNTIME = Omp()
