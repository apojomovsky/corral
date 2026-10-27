"""The directories corral keeps on a host, one resolver each.

Every reader of a corral directory goes through the function here, so a
path is decided in exactly one place (AGENTS.md, "One fact, one place").
Defaults follow the XDG base directory spec. An override names an
absolute path; a relative one is refused rather than resolved against
whatever the cwd happens to be. An empty override counts as unset.
Every result is normalized, so containment checks against it agree with
paths built by joining onto it.
"""

import os
import re


class PathError(ValueError):
    """A directory override is unusable (exit 4, configuration error)."""


def _override(var, env):
    """The normalized absolute path in `var`, None when unset or empty."""
    value = env.get(var)
    if not value:
        return None
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise PathError(f"{var}={value!r} must be an absolute path")
    return os.path.normpath(path)


def _resolve(override_var, xdg_var, xdg_default, env):
    env = os.environ if env is None else env
    override = _override(override_var, env)
    if override:
        return override
    base = env.get(xdg_var)
    if not base or not os.path.isabs(base):
        # The XDG spec says a relative value is invalid and must be ignored.
        base = os.path.expanduser(xdg_default)
    return os.path.normpath(os.path.join(base, "corral"))


def state_dir(env=None):
    """Ledgers, the board store and logs: `CORRAL_STATE`, else XDG state."""
    return _resolve("CORRAL_STATE", "XDG_STATE_HOME", "~/.local/state", env)


def config_dir(env=None):
    """Host config, project registrations, the API token: `CORRAL_CONFIG`."""
    return _resolve("CORRAL_CONFIG", "XDG_CONFIG_HOME", "~/.config", env)


def cache_dir(env=None):
    """Scratch files, such as PR body drafts: `CORRAL_CACHE`, else XDG cache."""
    return _resolve("CORRAL_CACHE", "XDG_CACHE_HOME", "~/.cache", env)


def sessions_dir(env=None):
    """One directory per agent session: `CORRAL_SESSIONS`, else `<state>/sessions`."""
    env = os.environ if env is None else env
    return _override("CORRAL_SESSIONS", env) or os.path.join(state_dir(env), "sessions")


REPO_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def plain_repo_name(repo):
    """True for a plain repository name: one path part, not a `.git` name.

    The one rule every repo-named path or unit name shares.
    """
    return isinstance(repo, str) and bool(REPO_NAME.fullmatch(repo)) and not repo.endswith(".git")


def ticket_session_dir(repo, number, env=None, root=None):
    """One ticket's session dir, `<sessions>/<repo>-<number>`.

    `root` replaces the sessions dir (tests, a reviewer outside the
    host's session tree). A repo name that could leave the root, or a
    number that is not a positive integer, is a usage error.
    """
    repo = str(repo or "")
    if not plain_repo_name(repo):
        raise PathError(f"repo name {repo!r} is not a plain repository name")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise PathError(f"ticket number {number!r} must be a positive integer")
    return os.path.join(root or sessions_dir(env), f"{repo}-{number}")


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
