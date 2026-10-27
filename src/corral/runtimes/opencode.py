"""The opencode adapter: `opencode run --format json`, one headless run.

Under opencode the unit's out.log is the `--format json` event stream, so the
activity is read from out.log and the session id comes from its `sessionID`
fields. The per-launch config is `opencode.json`, pointed at by OPENCODE_CONFIG.

OPENCODE_CONFIG adds this file on top of the user's global config, which
opencode still merges, so a setting in the home config can still apply to a
headless run. corral never writes that home config.
"""

import functools
import json
import os
import shutil

from corral import paths
from corral.runtimes import DEFAULT_MAX_TIME, check_prompt

NAME = "opencode"
#: Where opencode installs for a user, when PATH does not have it.
FALLBACK = "~/.opencode/bin/opencode"
CONFIG_NAME = "opencode.json"
OUT_LOG = "out.log"
#: opencode's bash tool times out after two minutes by default; builds and the
#: takeoff ritual take longer, so the default is an hour.
BASH_TIMEOUT_MS = 3600 * 1000


@functools.cache
def binary():
    """The opencode binary: on PATH, else the usual install location. Found once."""
    return shutil.which("opencode") or os.path.expanduser(FALLBACK)


def argv(session_dir, prompt, *, model=None, resume=None, max_time=DEFAULT_MAX_TIME):
    """`max_time` is accepted for the protocol; opencode has no run limit flag."""
    check_prompt(prompt)
    command = [binary(), "run", "--format", "json", "--auto", "--pure"]
    if resume:
        command += ["--session", resume]
    if model:
        command += ["--model", model]
    return command + [prompt]


def env(session_dir):
    return {
        "OPENCODE_CONFIG": os.path.join(session_dir, CONFIG_NAME),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_SHARE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": str(BASH_TIMEOUT_MS),
    }


def write_config(session_dir, config):
    """Write `opencode.json` into the session dir, atomically."""
    os.makedirs(session_dir, exist_ok=True)
    text = json.dumps(config, indent=1) + "\n"
    paths.atomic_write_text(os.path.join(session_dir, CONFIG_NAME), text)


def resume_handle(session_dir):
    """The last `sessionID` in out.log, or None."""
    found = None
    try:
        with open(os.path.join(session_dir, OUT_LOG), encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    session = json.loads(line).get("sessionID")
                except (ValueError, AttributeError):
                    continue
                if isinstance(session, str) and session:
                    found = session
    except OSError:
        return None
    return found


def activity_file(session_dir):
    path = os.path.join(session_dir, OUT_LOG)
    return path if os.path.exists(path) else None


class Opencode:
    name = NAME
    argv = staticmethod(argv)
    env = staticmethod(env)
    write_config = staticmethod(write_config)
    resume_handle = staticmethod(resume_handle)
    activity_file = staticmethod(activity_file)


RUNTIME = Opencode()
