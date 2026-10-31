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
from typing import NamedTuple


class PathError(ValueError):
    """A directory override is unusable (exit 4, configuration error)."""


class Session(NamedTuple):
    """One leaf session of a ticket: its directory, role and generation.

    A legacy single-directory session reads as generation 0, its role
    from the role record or worker.
    """

    path: str
    role: str
    gen: int


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
    """One ticket's session root, `<sessions>/<repo>-<number>`.

    Each role generation lives in its own leaf below it
    (`role_session_dir`); a legacy single-directory session keeps its
    files directly here and reads as generation 0. Review records live
    here too, beside the leaves. `root` replaces the sessions dir
    (tests, a reviewer outside the host's session tree). A repo name
    that could leave the root, or a number that is not a positive
    integer, is a usage error.
    """
    repo = str(repo or "")
    if not plain_repo_name(repo):
        raise PathError(f"repo name {repo!r} is not a plain repository name")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise PathError(f"ticket number {number!r} must be a positive integer")
    return os.path.join(root or sessions_dir(env), f"{repo}-{number}")


#: One role generation below a ticket root: `<role>-<gen>`.
_ROLE_SESSION_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*-[0-9]+")

#: One ticket root directly under the sessions dir: `<repo>-<number>`.
_TICKET_DIR_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*-[1-9][0-9]*")


def parse_ticket_dirname(name):
    """`(repo, number)` for a ticket root dirname, else None.

    The repo is the longest leading part that leaves a trailing
    `-<number>`, the same split as unit names, so a repo with hyphens
    still splits one way.
    """
    if not isinstance(name, str):
        return None
    if not _TICKET_DIR_RE.fullmatch(name):
        return None
    repo, _, number = name.rpartition("-")
    if not plain_repo_name(repo):
        return None
    return repo, int(number)


def leaf_ticket(path):
    """`(repo, number)` the session leaf at `path` belongs to, else None.

    A `<ticket>/<role>-<gen>` leaf belongs to its parent ticket; a
    legacy leaf is its own ticket root. Hygiene re-verifies the proof
    against this ticket just before deleting. The parent parse wins only
    when the base cannot name a ticket dir itself, or names a known role
    (a genuine leaf): otherwise a sessions root whose own basename parses
    as `<repo>-<m>` would steal a legacy candidate for the parent's
    ticket.
    """
    from corral import prompts  # Lazy: settings imports paths at load time.

    base = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    if _ROLE_SESSION_RE.fullmatch(base):
        parent_ticket = parse_ticket_dirname(parent)
        if parent_ticket is not None and (
            parse_ticket_dirname(base) is None or base.rpartition("-")[0] in prompts.ROLES
        ):
            return parent_ticket
    return parse_ticket_dirname(base)


def role_session_dir(repo, number, role, gen, env=None, root=None):
    """One role generation's session dir, `<ticket>/<role>-<gen>`.

    A launch writes here instead of the ticket root, so a reviewer
    launch never overwrites the worker's record. A bad role or a
    generation that is not an integer of 0 or more is a usage error.
    """
    role = str(role or "")
    if not plain_repo_name(role):
        raise PathError(f"role name {role!r} is not a plain role name")
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
        raise PathError(f"session generation {gen!r} must be an integer of 0 or more")
    return os.path.join(ticket_session_dir(repo, number, env=env, root=root), f"{role}-{gen}")


def _legacy_session(ticket):
    """Whether the ticket root itself holds a legacy session.

    Any direct child file proves it: every launch writes `prompt.md`
    there, and hygiene seeds `activity.jsonl`. An empty root reads as
    an empty legacy session: there is nothing to lose, so the usual
    proof still reclaims it. A root holding only role leaves and the
    reviews dir is not one.
    """
    try:
        names = os.listdir(ticket)
    except OSError:
        return False
    if not names:
        return True
    return any(os.path.isfile(os.path.join(ticket, name)) for name in names)


def _legacy_role(ticket, *, on_error="strict"):
    """The role a legacy ticket root was launched with.

    Missing reads as worker, the worker behaviour. `on_error` decides
    what a damaged record does: "strict" raises a usage error (resume
    and tail fail closed: resuming a reviewer with write access, or a
    worker read-only, must be impossible), "lenient" keeps the record
    as-is, worker when it is missing or empty (stop and listings union
    over every leaf they can prove; a damaged record must not hide a
    session's units). Anything else is a programmer error.
    """
    from corral import prompts  # Lazy: settings imports paths at load time.

    if on_error not in ("strict", "lenient"):
        raise ValueError(f"on_error must be 'strict' or 'lenient', not {on_error!r}")
    try:
        with open(os.path.join(ticket, "role"), encoding="utf-8") as handle:
            role = handle.read().strip()
    except FileNotFoundError:
        return prompts.DEFAULT_ROLE
    except OSError as err:
        if on_error == "lenient":
            return prompts.DEFAULT_ROLE
        raise PathError(f"cannot read the agent's role: {err}") from None
    if role not in prompts.ROLES:
        if on_error == "lenient":
            return role or prompts.DEFAULT_ROLE
        raise PathError(f"unknown role {role!r}: expected one of {', '.join(prompts.ROLES)}")
    return role


def _role_leaves(ticket):
    """`[Session]` for each generation dir holding a session, oldest first."""
    try:
        names = sorted(os.listdir(ticket))
    except OSError:
        return []
    found = []
    for name in names:
        if not _ROLE_SESSION_RE.fullmatch(name):
            continue
        role, _, gen = name.rpartition("-")
        path = os.path.join(ticket, name)
        if not os.path.isdir(path):
            continue
        if not os.path.isfile(os.path.join(path, "prompt.md")):
            continue
        found.append(Session(path, role, int(gen)))
    found.sort(key=lambda s: (s.gen, s.role))
    return found


def ticket_sessions(repo, number, env=None, root=None):
    """Every leaf session of one ticket, legacy first, then oldest first.

    The legacy single-directory session, when present, reads as
    generation 0 with its recorded role. One listing, one place: resume
    picks the newest of a role from it, stop and close-previous union
    over it, hygiene enumerates it.
    """
    ticket = ticket_session_dir(repo, number, env=env, root=root)
    found = []
    if _legacy_session(ticket):
        found.append(Session(ticket, _legacy_role(ticket, on_error="lenient"), 0))
    return found + _role_leaves(ticket)


def newest_session(repo, number, role=None, env=None, root=None):
    """The newest session dir of one ticket and role, or None.

    A filter over `ticket_sessions`: `role` None reads as worker, the
    worker behaviour. A role generation dir wins over the legacy dir;
    the highest generation wins. The legacy dir matches only its
    recorded role, checked strictly, but only when the legacy dir is
    the selected leaf: an unreadable or unknown role record next to a
    healthy role leaf does not block that leaf. On the legacy dir the
    record is a usage error.
    """
    from corral import prompts  # Lazy: settings imports paths at load time.

    want = str(role or "") or prompts.DEFAULT_ROLE
    ticket = ticket_session_dir(repo, number, env=env, root=root)
    best = None
    legacy = False
    for session in ticket_sessions(repo, number, env=env, root=root):
        if session.path == ticket:
            legacy = True
            continue
        if session.role == want and (best is None or session.gen > best.gen):
            best = session
    if best is not None:
        return best.path
    if not legacy:
        return None
    if _legacy_role(ticket, on_error="strict") != want:
        return None
    return ticket


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
