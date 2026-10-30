"""The muse adapter: `muse exec --json`, one headless run per session.

The prompt travels by `--prompt-file` only: the worker run reads the
`prompt.md` the launch already wrote, and a resume reads the message
file. The `--model` is the configured model or the `model=` override
with the provider prefix stripped; a resume passes `--model` only when
the caller overrides it. Every other per-launch config entry travels as
a repeatable `-c KEY=VALUE` pair with a JSON value. A reviewer runs the
worker form plus `--disable-write`. `max_time` is accepted for the
protocol and ignored: muse has no run limit flag, and the unit still
bounds the run.
"""

import functools
import json
import os
import re
import shutil
from uuid import uuid4

from corral import paths
from corral.runtimes import DEFAULT_MAX_TIME, check_prompt

NAME = "muse"
#: Where muse installs for a user, when PATH does not have it.
FALLBACK = "~/.local/bin/muse"
#: The per-launch config in the session dir; `argv` reads its model from here.
CONFIG_NAME = "muse.json"
#: The launch prompt the session setup writes; the worker run reads it back.
PROMPT_NAME = "prompt.md"
#: The resume message file; the resume run reads the message from here.
MESSAGE_NAME = "message.md"


@functools.cache
def binary():
    """The muse binary: on PATH, else the usual install location. Found once."""
    return shutil.which("muse") or os.path.expanduser(FALLBACK)


def new_session_id():
    """A fresh session id for a worker run: muse only accepts a UUID."""
    return str(uuid4())


#: A whole-value `${NAME}` reference: the one string `-c` value passed raw.
ENV_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _read_config(session_dir):
    """The per-launch config in the session dir, or {} when there is none.

    A corrupt file, an unreadable one, or a non-object raises ValueError
    naming the file, so a launch never silently runs on the wrong config.
    Each launch caller converts ValueError to its own error with `from None`
    for one-line CLI output; the file detail survives inside the message text.
    """
    path = os.path.join(session_dir, CONFIG_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ValueError(f"cannot read {path}: {err}") from err
    except ValueError as err:
        raise ValueError(f"cannot parse {path}: {err}") from err
    if not isinstance(config, dict):
        raise ValueError(f"cannot parse {path}: want a JSON object")
    return config


def _model_of(config, session_dir):
    """The `model` entry of a config, or None when absent or empty.

    A present entry that is not a non-empty string raises the same
    `cannot parse` ValueError as a corrupt file.
    """
    if "model" not in config:
        return None
    model = config["model"]
    if isinstance(model, str):
        return model or None
    path = os.path.join(session_dir, CONFIG_NAME)
    raise ValueError(f"cannot parse {path}: want model to be a non-empty string")


def _encode_value(value):
    """A `-c` value: JSON, except a whole `${NAME}` reference passes through raw."""
    if isinstance(value, str) and ENV_REF.fullmatch(value):
        return value
    return json.dumps(value)


def _config_flags(config):
    """Every non-model config entry as `-c KEY=<value>` pairs, sorted by key."""
    flags = []
    for key in sorted(config, key=str):
        if key == "model":
            continue
        flags += ["-c", f"{key}={_encode_value(config[key])}"]
    return flags


def argv(
    session_dir, prompt, *, model=None, resume=None, max_time=DEFAULT_MAX_TIME, reviewer=False
):
    """The command line for one headless run. `resume` is an existing session id."""
    from corral import models

    check_prompt(prompt)
    config = _read_config(session_dir)
    if resume:
        session_id = resume
        prompt_file = os.path.join(session_dir, MESSAGE_NAME)
        chosen = model if model else None
    else:
        session_id = new_session_id()
        prompt_file = os.path.join(session_dir, PROMPT_NAME)
        chosen = model if model else _model_of(config, session_dir)
    command = [binary(), "exec", "--json", "--prompt-file", prompt_file]
    if chosen:
        command += ["--model", models.bare_id(chosen)]
    command += _config_flags(config)
    command += [
        "--contributor-mode",
        "on",
        "--session-id",
        session_id,
        "--yolo",
        "--no-foreign-personal-context",
        "--disable-request-user-input",
        "--reasoning-effort",
        "high",
        "--parallel-tool-calls",
    ]
    if reviewer:
        command += ["--disable-write"]
    return command


def env(session_dir):
    """No entries: `-c` needs no pointer, so the run adds no home paths."""
    return {}


def write_config(session_dir, config):
    """Write `muse.json` into the session dir, atomically: the run's audit record."""
    os.makedirs(session_dir, exist_ok=True)
    text = json.dumps(config, indent=1) + "\n"
    paths.atomic_write_text(os.path.join(session_dir, CONFIG_NAME), text)


def with_models(config, chains):
    """`config` with the default role's head as its model. Other keys are kept."""
    from corral import models

    return {**(config or {}), "model": models.primary(chains)}


def resume_handle(session_dir):
    """Refused until the sessions-and-resume ticket builds it: no fake behavior."""
    raise NotImplementedError("muse resume_handle lands in T3")


def activity_file(session_dir):
    """Refused until the sessions-and-resume ticket builds it: no fake behavior."""
    raise NotImplementedError("muse activity_file lands in T3")


class Muse:
    name = NAME
    argv = staticmethod(argv)
    env = staticmethod(env)
    write_config = staticmethod(write_config)
    with_models = staticmethod(with_models)
    resume_handle = staticmethod(resume_handle)
    activity_file = staticmethod(activity_file)


RUNTIME = Muse()
