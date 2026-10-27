"""Launch, resume, tail and stop one ticket's agent on this host.

This composes the pieces below into one path. Settings come from
`corral.settings`; the session dir from `corral.paths`; the unit names,
spawn lock and start/stop from `corral.units`; the worktree from
`corral.worktrees`; the runtime adapter from `corral.runtimes`; the cap
accounting from `corral.registry`.

Every subprocess and every listing goes through an injected seam:
`list_fn(prefix)` lists units, `run(argv, timeout)` starts one, and
`stop_fn(unit)` stops one. `stop_fn` has no default. Only the CLI passes
the production stop (see `corral.launchcli`).

Errors carry the public exit code: LaunchError 4, CapFull 75. The units
errors (UnitExists 2, Unreachable 3, UnitsError 4) pass through unchanged.
"""

import os
from dataclasses import dataclass

from corral import paths, registry, runtimes, settings, units, worktrees
from corral.runtimes import tail as runtime_tail

#: The run time limit handed to the runtime, in seconds for the unit's
#: RuntimeMaxSec. It is the same limit as runtimes.DEFAULT_MAX_TIME ("3h");
#: a test asserts the two agree.
RUNTIME_MAX_S = 3 * 60 * 60

#: The unit's environment entry that names the agent, read by the agent's hooks.
AGENT_ID_VAR = "CORRAL_AGENT_ID"

settings.register("clone.path", str, "", "the repository's main clone (relative to the ops root)")
settings.register("clone.default_branch", str, "main", "the default branch of the clone")


class LaunchError(Exception):
    """A launch, resume, tail or stop cannot proceed: usage or configuration (exit 4)."""

    exit_code = 4


class CapFull(LaunchError):
    """Every agent slot on this host is taken: try later (exit 75)."""

    exit_code = 75


@dataclass(frozen=True)
class Target:
    """One repository's resolved launch settings. Built by `target`."""

    root: str
    repo: str
    clone: str
    default_branch: str
    prefix: str
    runtime: str
    cap: int
    branch_template: str
    worktrees_root: str


def target(root, repo, env=None, flags=None):
    """The launch target for `repo` in the ops root `root`.

    Raises LaunchError when the repository has no `clone.path`, the path
    does not name a git clone, or a relative path has no ops root to be
    relative to. An unknown repo is a SettingsError from `settings.load`.
    """
    if not paths.plain_repo_name(repo):
        raise LaunchError(f"repo name {repo!r} is not a plain repository name")
    resolved = settings.load(root=root, repo=repo, env=env, flags=flags)
    path = resolved["clone.path"]
    if not path:
        raise LaunchError(f"repo {repo!r} has no clone.path in corral.toml")
    if os.path.isabs(path):
        clone = os.path.normpath(path)
    elif root:
        clone = os.path.normpath(os.path.join(root, path))
    else:
        raise LaunchError(f"clone.path {path!r} is relative and there is no ops root")
    if not os.path.exists(os.path.join(clone, ".git")):
        raise LaunchError(f"{clone} is not a git clone (repo {repo!r})")
    return Target(
        root=root,
        repo=repo,
        clone=clone,
        default_branch=resolved["clone.default_branch"],
        prefix=resolved["agents.unit_prefix"],
        runtime=resolved["agents.runtime"],
        cap=resolved["agents.cap"],
        branch_template=resolved["git.branch"],
        worktrees_root=resolved["git.worktrees"],
    )


def _session(tgt, number):
    try:
        return paths.ticket_session_dir(tgt.repo, number)
    except paths.PathError as err:
        raise LaunchError(str(err)) from None


def _adapter(name):
    try:
        return runtimes.get(name)
    except runtimes.UnknownRuntime as err:
        raise LaunchError(str(err)) from None


def _check_text(value, name):
    """A prompt or message: a non-empty string that a runtime will not read as a flag."""
    if not isinstance(value, str) or not value.strip():
        raise LaunchError(f"{name} must be a non-empty string")
    try:
        runtimes.check_prompt(value)
    except ValueError as err:
        raise LaunchError(str(err)) from None
    return value


def _check_model(model):
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip() or model.startswith("-"):
        raise LaunchError(f"model {model!r} must be a non-empty name that does not start with '-'")
    return model


def admit(tgt, number, *, list_fn):
    """Refuse a ticket that is already live, or a host with no free slot.

    Live means a live unit of this prefix (`list_fn(prefix)`) or a live
    registered worker on this host that holds the ticket. Raises
    units.UnitExists (exit 2) for the ticket, CapFull (exit 75) when the
    live count has reached the cap.
    """
    live = units.live_tickets(tgt.prefix, list_fn(tgt.prefix)) | registry.counted_tickets()
    if (tgt.repo, number) in live:
        raise units.UnitExists(f"#{number} of {tgt.repo} already has a live agent")
    if len(live) >= tgt.cap:
        raise CapFull(f"{len(live)} of {tgt.cap} agent slots are taken: try later")


def _env(adapter, session, unit):
    env = dict(adapter.env(session))
    env[AGENT_ID_VAR] = unit
    return env


def launch(
    tgt,
    number,
    prompt,
    *,
    title=None,
    model=None,
    runtime_config=None,
    list_fn,
    run,
    git=worktrees.git,
):
    """Start a new agent for ticket `number`. Returns `(unit, workdir)`.

    Admission runs under the spawn lock, once before the worktree is made
    and once right before the unit starts, so two launches never race on
    a slot. The worktree is made under the same lock, as its module asks.
    """
    _check_text(prompt, "prompt")
    model = _check_model(model)
    if title is not None and not isinstance(title, str):
        raise LaunchError("title must be a string")
    if runtime_config is not None and not isinstance(runtime_config, dict):
        raise LaunchError("runtime config must be a JSON object")
    adapter = _adapter(tgt.runtime)
    session = _session(tgt, number)

    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn)
        workdir, _branch = worktrees.ensure_worktree(
            tgt.clone,
            number,
            default_branch=tgt.default_branch,
            branch_template=tgt.branch_template,
            root=tgt.worktrees_root,
            title=title,
            git=git,
        )

    os.makedirs(session, exist_ok=True)
    try:
        paths.atomic_write_text(os.path.join(session, "prompt.md"), prompt)
        runtimes.record(session, tgt.runtime)
        adapter.write_config(session, runtime_config or {})
    except (ValueError, TypeError, OSError) as err:
        raise LaunchError(f"cannot write the agent's session: {err}") from None

    unit = units.unit_name(tgt.prefix, tgt.repo, number, units.next_gen(session))
    command = adapter.argv(session, prompt, model=model, max_time=runtimes.DEFAULT_MAX_TIME)
    argv = units.unit_argv(
        unit, workdir, session, command, _env(adapter, session, unit), RUNTIME_MAX_S
    )
    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn)
        units.start(argv, session, unit, run=run)
    return unit, workdir


def resume(
    tgt,
    number,
    message,
    *,
    model=None,
    list_fn,
    run,
    git=worktrees.git,
):
    """Resume ticket `number`'s newest runtime session in a new unit. Returns `(unit, workdir)`.

    The runtime is the one the session recorded at launch. Raises LaunchError
    when there is no session to resume.
    """
    _check_text(message, "message")
    model = _check_model(model)
    session = _session(tgt, number)
    adapter = _adapter(runtimes.recorded(session))
    handle = adapter.resume_handle(session)
    if handle is None:
        raise LaunchError(f"#{number} of {tgt.repo} has no {adapter.name} session to resume")

    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn)
        workdir, _branch = worktrees.ensure_worktree(
            tgt.clone,
            number,
            default_branch=tgt.default_branch,
            branch_template=tgt.branch_template,
            root=tgt.worktrees_root,
            git=git,
        )

    gen = max(units.next_gen(session), 1)
    unit = units.unit_name(tgt.prefix, tgt.repo, number, gen)
    command = adapter.argv(
        session,
        message,
        model=model,
        resume=handle,
        max_time=runtimes.DEFAULT_MAX_TIME,
    )
    argv = units.unit_argv(
        unit, workdir, session, command, _env(adapter, session, unit), RUNTIME_MAX_S
    )
    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn)
        units.start(argv, session, unit, run=run)
    return unit, workdir


def stop(tgt, number, *, list_fn, stop_fn):
    """Stop every live unit of ticket `number` that its session's log proves it started.

    Returns the unit names stopped, possibly none. `list_fn` and `stop_fn`
    are required; see `units.stop`.
    """
    return units.stop(
        tgt.prefix,
        tgt.repo,
        number,
        session_dir=_session(tgt, number),
        list_fn=list_fn,
        stop_fn=stop_fn,
    )


def tail(tgt, number, last=None, out=None):
    """Print the ticket's agent activity from its session file. Returns the new offset.

    Raises LaunchError when the session has no activity file yet.
    """
    session = _session(tgt, number)
    adapter = _adapter(runtimes.recorded(session))
    path = adapter.activity_file(session)
    if path is None:
        raise LaunchError(f"#{number} of {tgt.repo} has no {adapter.name} activity yet")
    return runtime_tail.show(path, last=last, out=out)
