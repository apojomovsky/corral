"""The directories corral keeps on a host, one resolver each.

Every reader of a corral directory goes through the function here, so a
path is decided in exactly one place (AGENTS.md, "One fact, one place").
Defaults follow the XDG base directory spec. An override names an
absolute path; a relative one is refused rather than resolved against
whatever the cwd happens to be.
"""

import os


class PathError(ValueError):
    """A directory override is unusable (exit 4, configuration error)."""


def _resolve(override_var, xdg_var, xdg_default, leaf, env):
    env = os.environ if env is None else env
    override = env.get(override_var)
    if override:
        path = os.path.expanduser(override)
        if not os.path.isabs(path):
            raise PathError(f"{override_var}={override!r} must be an absolute path")
        return path
    base = env.get(xdg_var)
    if not base or not os.path.isabs(base):
        # The XDG spec says a relative value is invalid and must be ignored.
        base = os.path.expanduser(xdg_default)
    return os.path.join(base, leaf)


def state_dir(env=None):
    """Ledgers, the board store and logs: `CORRAL_STATE`, else XDG state."""
    return _resolve("CORRAL_STATE", "XDG_STATE_HOME", "~/.local/state", "corral", env)


def config_dir(env=None):
    """Host config, project registrations, the API token: `CORRAL_CONFIG`."""
    return _resolve("CORRAL_CONFIG", "XDG_CONFIG_HOME", "~/.config", "corral", env)


def cache_dir(env=None):
    """Scratch files, such as PR body drafts: `CORRAL_CACHE`, else XDG cache."""
    return _resolve("CORRAL_CACHE", "XDG_CACHE_HOME", "~/.cache", "corral", env)


def sessions_dir(env=None):
    """One directory per agent session: `CORRAL_SESSIONS`, else `<state>/sessions`."""
    env = os.environ if env is None else env
    override = env.get("CORRAL_SESSIONS")
    if override:
        path = os.path.expanduser(override)
        if not os.path.isabs(path):
            raise PathError(f"CORRAL_SESSIONS={override!r} must be an absolute path")
        return path
    return os.path.join(state_dir(env), "sessions")


def atomic_write_text(target, text):
    """Write `text` to `target` atomically: temp file plus rename.

    A crash or SIGKILL mid-write would otherwise leave a truncated state
    file; `os.replace` makes each save all-or-nothing. The temp file lives
    in the target directory so the rename never crosses filesystems, and
    it is removed when the write fails. Raises OSError; the caller creates
    the parent directory first.
    """
    import tempfile

    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
