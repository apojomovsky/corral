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
import glob
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
#: The corral session file holding the uuid this ticket's runs share.
SESSION_ID_FILE = "muse_session_id"
#: The unit's stdout capture: the `--json` event stream, the run's activity.
OUT_LOG = "out.log"
#: The per-run log muse itself writes under its home state dir.
SESSION_FILE = "session.jsonl"


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
    session_dir,
    prompt,
    *,
    model=None,
    resume=None,
    max_time=DEFAULT_MAX_TIME,
    reviewer=False,
    session_id=None,
):
    """The command line for one headless run. `resume` is an existing session id.

    A fresh run takes its id from `session_id`: the launch generates it,
    records it and passes it, so the recorded id and the run's id cannot
    differ. A fresh run without one is refused; a resume uses `resume`
    and ignores `session_id`.
    """
    from corral import models

    check_prompt(prompt)
    config = _read_config(session_dir)
    if resume:
        run_id = resume
        prompt_file = os.path.join(session_dir, MESSAGE_NAME)
        chosen = model if model else None
    else:
        if not session_id:
            raise ValueError(f"a fresh muse run needs a session id recorded in {SESSION_ID_FILE}")
        run_id = session_id
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
        run_id,
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


def sessions_root():
    """Where muse keeps its own session logs. Corral reads here, never writes."""
    return os.path.expanduser(os.path.join("~", ".local", "share", "muse", "sessions"))


def record_session_id(session_dir, session_id):
    """Record `session_id` as this session's muse id, atomically. Raises OSError."""
    os.makedirs(session_dir, exist_ok=True)
    paths.atomic_write_text(os.path.join(session_dir, SESSION_ID_FILE), session_id + "\n")


def resume_handle(session_dir):
    """The recorded muse session id, or None when nothing was recorded."""
    try:
        with open(os.path.join(session_dir, SESSION_ID_FILE), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def resolve(session_id, root=None):
    """The muse session file for `session_id`, or None when absent.

    Scans `<root>/YYYY/MM/DD/<id>/session.jsonl` and takes the newest by
    `(mtime, path)`. Every `session.jsonl` basename is the same, so the
    path is the tie-break, and the pick never depends on directory order.
    `root` defaults to muse's own sessions dir; an id that could escape
    the root finds nothing.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    if session_id in (".", "..") or os.sep in session_id:
        return None
    if os.altsep and os.altsep in session_id:
        return None
    base = root if root is not None else sessions_root()
    pattern = os.path.join(base, "*", "*", "*", glob.escape(session_id), SESSION_FILE)
    found = [found_path for found_path in glob.glob(pattern) if os.path.isfile(found_path)]
    if not found:
        return None
    return os.path.abspath(max(found, key=lambda p: (os.path.getmtime(p), os.path.abspath(p))))


def activity_file(session_dir):
    """The session's `out.log` when present, else None: tail reads the stream."""
    path = os.path.join(session_dir, OUT_LOG)
    return path if os.path.exists(path) else None


def write_message(session_dir, message):
    """Write `message` as `message.md`: the resume run reads it back. Raises OSError."""
    os.makedirs(session_dir, exist_ok=True)
    paths.atomic_write_text(os.path.join(session_dir, MESSAGE_NAME), message)


class Muse:
    name = NAME
    argv = staticmethod(argv)
    env = staticmethod(env)
    write_config = staticmethod(write_config)
    with_models = staticmethod(with_models)
    resume_handle = staticmethod(resume_handle)
    activity_file = staticmethod(activity_file)


RUNTIME = Muse()
