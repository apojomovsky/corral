"""Agent runtime adapters: how corral launches, resumes and reads each runtime.

One adapter per runtime, and corral supports one major version of each at a
time (AGENTS.md, "One version of each runtime"). `omp`, `opencode` and `muse`
are the three adapters; `get` returns one by name, and `record`/`recorded`
keep the runtime a session started on, so a resume uses the same runtime.

Every adapter writes only into the session directory it is given. It never
reads or writes a user's home config (`~/.omp/`, `~/.config/opencode/`); each
launch gets a full per-launch overlay instead.

Binaries: each adapter resolves its binary once per process, from PATH, and
falls back to its usual install location under the home directory when PATH
has none.
"""

import os
from typing import Protocol

from corral import paths, settings

#: The session file that names the runtime a session was launched on.
RUNTIME_FILE = "runtime"
#: A session with no recorded runtime was launched on omp.
DEFAULT_RUNTIME = "omp"
NAMES = ("omp", "opencode", "muse")
#: The run time limit handed to a runtime that takes one.
DEFAULT_MAX_TIME = "3h"


class UnknownRuntime(ValueError):
    """A runtime name corral does not support (exit 4, usage error)."""


#: The runtime a project's agents use; `get` is where the name is checked.
settings.register("agents.runtime", str, DEFAULT_RUNTIME, f"agent runtime: {' or '.join(NAMES)}")


def check_prompt(prompt):
    """Refuse a prompt a runtime would read as a flag: it is the last argument."""
    if isinstance(prompt, str) and prompt.startswith("-"):
        raise ValueError("a prompt must not start with '-': a runtime reads it as a flag")
    return prompt


class Runtime(Protocol):
    """What the rest of corral asks of a runtime adapter."""

    name: str

    def argv(
        self,
        session_dir: str,
        prompt: str,
        *,
        model: str | None = None,
        resume: str | None = None,
        max_time: str = DEFAULT_MAX_TIME,
    ) -> list[str]:
        """The command line for one headless run. `resume` is a `resume_handle`."""
        ...

    def env(self, session_dir: str) -> dict[str, str]:
        """Environment entries the run needs, on top of the unit's own."""
        ...

    def write_config(self, session_dir: str, config: dict) -> None:
        """Write the per-launch config into `session_dir`, never a home config."""
        ...

    def with_models(self, config: dict, chains: dict) -> dict:
        """`config` with the live chains laid over it, for the per-launch overlay."""
        ...

    def resume_handle(self, session_dir: str) -> str | None:
        """The newest session file or id in `session_dir`, or None."""
        ...

    def activity_file(self, session_dir: str) -> str | None:
        """The file tail reads for this session's activity, or None."""
        ...


def get(name: str) -> Runtime:
    """The adapter for `name`; UnknownRuntime for anything else."""
    from corral.runtimes import muse, omp, opencode

    adapters = {"omp": omp.RUNTIME, "opencode": opencode.RUNTIME, "muse": muse.RUNTIME}
    if name not in adapters:
        raise UnknownRuntime(f"unknown runtime {name!r}: want one of {', '.join(NAMES)}")
    return adapters[name]


def record(session_dir: str, name: str) -> None:
    """Write `name` as the runtime of the session in `session_dir`."""
    get(name)
    os.makedirs(session_dir, exist_ok=True)
    paths.atomic_write_text(os.path.join(session_dir, RUNTIME_FILE), name + "\n")


def recorded(session_dir: str) -> str:
    """The runtime the session was launched on; omp when none was recorded."""
    try:
        with open(os.path.join(session_dir, RUNTIME_FILE), encoding="utf-8") as handle:
            return handle.read().strip() or DEFAULT_RUNTIME
    except OSError:
        return DEFAULT_RUNTIME
