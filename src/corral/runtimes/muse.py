"""The muse adapter: `muse exec --json`, one headless run per session.

The prompt travels by `--prompt-file` only: the worker run reads the
`prompt.md` the launch already wrote, and a resume reads the message
file. The `--model` is the configured model or the `model=` override
with the provider prefix stripped; a resume passes `--model` only when
the caller overrides it. A reviewer runs the worker form plus
`--disable-write`. `max_time` is accepted for the protocol and ignored:
muse has no run limit flag, and the unit still bounds the run.
"""

import functools
import json
import os
import shutil
from uuid import uuid4

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


def _config_model(session_dir):
    """The configured model in the session dir, or None when there is none."""
    try:
        with open(os.path.join(session_dir, CONFIG_NAME), encoding="utf-8") as handle:
            config = json.load(handle)
    except OSError:
        return None
    if not isinstance(config, dict):
        return None
    model = config.get("model")
    return model if isinstance(model, str) and model else None


def argv(
    session_dir, prompt, *, model=None, resume=None, max_time=DEFAULT_MAX_TIME, reviewer=False
):
    """The command line for one headless run. `resume` is an existing session id."""
    from corral import models

    check_prompt(prompt)
    if resume is None:
        session_id = new_session_id()
        prompt_file = os.path.join(session_dir, PROMPT_NAME)
        chosen = model if model is not None else _config_model(session_dir)
    else:
        session_id = resume
        prompt_file = os.path.join(session_dir, MESSAGE_NAME)
        chosen = model
    command = [binary(), "exec", "--json", "--prompt-file", prompt_file]
    if chosen is not None:
        command += ["--model", models.bare_id(chosen)]
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
    """Refused until the per-launch-config ticket builds it: no fake behavior."""
    raise NotImplementedError("muse env lands in T2")


def write_config(session_dir, config):
    """Refused until the per-launch-config ticket builds it: no fake behavior."""
    raise NotImplementedError("muse write_config lands in T2")


def with_models(config, chains):
    """Refused until the per-launch-config ticket builds it: no fake behavior."""
    raise NotImplementedError("muse with_models lands in T2")


def resume_handle(session_dir):
    """Refused until the sessions-and-resume ticket builds it: no fake behavior."""
    raise NotImplementedError("muse resume_handle lands in T3")


class Muse:
    name = NAME
    argv = staticmethod(argv)
    env = staticmethod(env)
    write_config = staticmethod(write_config)
    with_models = staticmethod(with_models)
    resume_handle = staticmethod(resume_handle)


RUNTIME = Muse()
