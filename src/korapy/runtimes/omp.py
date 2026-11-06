"""The omp adapter: `omp -p`, one headless run per session.

`omp -p` prints nothing until the session ends, so the activity is read from
the session `.jsonl` files in the session directory, not from the unit's
out.log. The per-launch config is `overlay.yml`, written by hand (stdlib
only, no YAML library) from a nested dict shaped like omp's own setting ids.
`reviewer` is a documented no-op: omp has no read-only flag, so the reviewer
prompt's read-only rule is the only guard.
"""

import functools
import json
import math
import os
import re
import shutil

from korapy import paths
from korapy.runtimes import DEFAULT_MAX_TIME, check_prompt

NAME = "omp"
#: Where omp installs for a user, when PATH does not have it.
FALLBACK = "~/.local/bin/omp"
CONFIG_NAME = "overlay.yml"
#: The profile options the last `write_config` recorded, read back by `argv`.
OPTIONS_NAME = "options.json"
SESSION_SUFFIX = ".jsonl"
#: The approval mode without a recorded option: headless runs approve tools.
DEFAULT_AUTO_APPROVE = True
#: A headless turn kills background jobs, so omp never backgrounds one. omp
#: backgrounds bash calls over a minute, and runs task sub-agents as background
#: jobs while `async.enabled` holds; in `-p` mode the agent would end its turn to
#: wait and the session would end with it. The keys are omp's own setting ids,
#: nested as YAML (a bare `autoBackground: false` is not read).
FORCED = {"bash": {"autoBackground": {"enabled": False}}, "async": {"enabled": False}}

_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
#: Plain scalars YAML 1.1 reads as a boolean or null (any case). Quoted, they stay strings.
_YAML_WORDS = frozenset({"y", "yes", "n", "no", "on", "off", "true", "false", "null", "~"})


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
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"cannot write {value!r} to the overlay: not a finite number")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a valid YAML double-quoted scalar.
        return json.dumps(value)
    raise TypeError(f"cannot write {type(value).__name__} as an overlay value")


def _key(key):
    text = str(key)
    if "." in text:
        # A dotted key would be read as a literal name, beside the nested
        # block, and could slip past the forced keys. Callers nest instead.
        raise ValueError(f"overlay key {text!r} has a dot: nest it as a mapping")
    plain = _PLAIN_KEY.fullmatch(text) is not None and text.lower() not in _YAML_WORDS
    return text if plain else json.dumps(text)


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


def options_schema():
    """The profile options omp accepts: the approval mode for the headless run."""
    return {"auto_approve": (bool, "whether to pass --auto-approve for the headless run")}


def _read_options(session_dir):
    """The recorded profile options, or {} when none were recorded.

    A corrupt file, an unreadable one, or a non-object raises ValueError
    naming the file, so a launch never silently runs on the wrong setup.
    """
    path = os.path.join(session_dir, OPTIONS_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            options = json.load(handle)
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ValueError(f"cannot read {path}: {err}") from err
    except ValueError as err:
        raise ValueError(f"cannot parse {path}: {err}") from err
    if not isinstance(options, dict):
        raise ValueError(f"cannot parse {path}: want a JSON object")
    return options


def _auto_approve_of(session_dir):
    """Whether the run passes --auto-approve: the recorded option, else the default."""
    options = _read_options(session_dir)
    if "auto_approve" not in options:
        return DEFAULT_AUTO_APPROVE
    if not isinstance(options["auto_approve"], bool):
        path = os.path.join(session_dir, OPTIONS_NAME)
        raise ValueError(f"cannot parse {path}: want auto_approve to be a boolean")
    return options["auto_approve"]


def with_models(config, chains):
    """`config` with each live chain laid over it: head as the role's model,
    tail as its fallback chain. Other keys and roles are kept; an empty
    chain sets nothing."""
    out = dict(config or {})
    live = {role: chain for role, chain in (chains or {}).items() if chain}
    if not live:
        return out
    roles = dict(out.get("modelRoles") or {})
    retry = dict(out.get("retry") or {})
    fallbacks = dict(retry.get("fallbackChains") or {})
    for role, chain in live.items():
        roles[role] = chain[0]
        if len(chain) > 1:
            fallbacks[role] = list(chain[1:])
        else:
            fallbacks.pop(role, None)
    out["modelRoles"] = roles
    out["retry"] = {**retry, "fallbackChains": fallbacks}
    return out


def write_config(session_dir, config):
    """Write `overlay.yml` into the session dir, atomically.

    Profile options travel beside it in `options.json`, so `argv` can read
    them back; they never land in the overlay, which holds omp settings.
    A rewrite without options removes a stale `options.json`, so a resume
    onto a profile without options cannot inherit the old profile's.
    """
    options = {}
    rest = dict(config or {})
    if "auto_approve" in rest:
        options["auto_approve"] = rest.pop("auto_approve")
    os.makedirs(session_dir, exist_ok=True)
    paths.atomic_write_text(os.path.join(session_dir, CONFIG_NAME), overlay_text(rest))
    target = os.path.join(session_dir, OPTIONS_NAME)
    if options:
        text = json.dumps(options, indent=1) + "\n"
        paths.atomic_write_text(target, text)
    else:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass


def argv(
    session_dir, prompt, *, model=None, resume=None, max_time=DEFAULT_MAX_TIME, reviewer=False
):
    """The headless run form. `reviewer` is accepted and ignored (see the module docstring)."""
    check_prompt(prompt)
    command = [
        binary(),
        "-p",
        "--session-dir",
        session_dir,
        "--max-time",
        max_time,
        "--config",
        os.path.join(session_dir, CONFIG_NAME),
    ]
    if _auto_approve_of(session_dir):
        command += ["--auto-approve"]
    if resume:
        command += ["-r", resume]
    if model:
        command += ["--model", model]
    return command + [prompt]


def env(session_dir):
    return {}


def _sessions(session_dir: str) -> list[str]:
    """The session files in the dir: regular files ending in `.jsonl`."""
    try:
        names: list[str] = os.listdir(session_dir)
    except OSError:
        return []
    found = [os.path.join(session_dir, n) for n in names if n.endswith(SESSION_SUFFIX)]
    return [p for p in found if os.path.isfile(p)]


def resume_handle(session_dir):
    """Absolute path of the newest session `.jsonl`, or None.

    Equal mtimes go to the name that sorts last, so the pick never depends on
    directory order.
    """
    found = _sessions(session_dir)
    if not found:
        return None
    newest: str = max(found, key=lambda p: (os.path.getmtime(p), os.path.basename(p)))
    return os.path.abspath(newest)


def activity_file(session_dir):
    return resume_handle(session_dir)


class Omp:
    name = NAME
    argv = staticmethod(argv)
    env = staticmethod(env)
    write_config = staticmethod(write_config)
    with_models = staticmethod(with_models)
    resume_handle = staticmethod(resume_handle)
    activity_file = staticmethod(activity_file)
    options_schema = staticmethod(options_schema)


RUNTIME = Omp()
