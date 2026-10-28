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

A launch or resume writes nothing into the ticket's session until its
second admission has passed, under the same spawn lock as the start. A
launch that loses the race leaves the live agent's session as it was.

Stop ends only the ticket's live units its session log proves it started.
A live unit the log does not prove ours is left running and named (see
`unproven`); the CLI exits 2 for it, and 1 when no unit of the ticket
is live at all.
"""

import os
import sys
import time
from dataclasses import dataclass

from corral import accounts, models, paths, quota_ledger, registry, runtimes, settings, units
from corral import worktrees
from corral.runtimes import tail as runtime_tail

#: The run time limit handed to the runtime, in seconds for the unit's
#: RuntimeMaxSec. It is the same limit as runtimes.DEFAULT_MAX_TIME ("3h");
#: a test asserts the two agree.
RUNTIME_MAX_S = 3 * 60 * 60

#: Appended to an admission error when the live agent is a registered one.
REGISTERED_NOTE = "(registered agent, counts until its heartbeat goes stale)"

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
    #: The settings this target was resolved from; admission reads the registry's keys here.
    resolved: settings.Resolved


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
        resolved=resolved,
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


def _live(tgt, list_fn):
    """(from_units, from_registry): the tickets live as units, and as counted registered agents."""
    from_units = units.live_tickets(tgt.prefix, list_fn(tgt.prefix))
    from_registry = registry.counted_tickets(cfg=registry.config(tgt.resolved)) - from_units
    return from_units, from_registry


def admit(tgt, number, *, list_fn):
    """Refuse a ticket that is already live, or a host with no free slot.

    Live means a live unit of this prefix (`list_fn(prefix)`) or a live
    registered worker on this host that holds the ticket, counted under the
    target's own resolved registry settings. Raises units.UnitExists (exit
    2) for the ticket, CapFull (exit 75) when the live count has reached
    the cap. A live agent that is only registered says so in the message.
    """
    from_units, from_registry = _live(tgt, list_fn)
    live = from_units | from_registry
    if (tgt.repo, number) in from_units:
        raise units.UnitExists(f"#{number} of {tgt.repo} already has a live agent")
    if (tgt.repo, number) in from_registry:
        raise units.UnitExists(
            f"#{number} of {tgt.repo} already has a live agent {REGISTERED_NOTE}"
        )
    if len(live) >= tgt.cap:
        note = f" {REGISTERED_NOTE}" if from_registry else ""
        raise CapFull(f"{len(live)} of {tgt.cap} agent slots are taken{note}: try later")


def unproven(tgt, number, *, list_fn):
    """Live units that parse to this ticket but that its units.log does not prove ours.

    `stop` leaves these running; the CLI reports them.
    """
    return units.unproven(
        tgt.prefix, tgt.repo, number, session_dir=_session(tgt, number), list_fn=list_fn
    )


def _env(adapter, session, unit):
    env = dict(adapter.env(session))
    env[AGENT_ID_VAR] = unit
    return env


def _model_chains(tgt, pool, ledger, now):
    """The target's validated, re-routed chains, or {} when none are declared.

    Every entry must name a provider from the pool (exit 4 naming role and
    entry); each chain keeps only its live providers, and a required role
    with none left refuses the launch (exit 75 naming role and reset). A
    stale reading never blocks, so the strictest freshness bound wins.
    """
    chains = dict(tgt.resolved.models)
    if not chains:
        return {}
    models.validate(chains, pool)
    bounds = [
        s
        for s in (getattr(provider, "stale_after_s", None) for provider in pool or ())
        if isinstance(s, (int, float)) and not isinstance(s, bool)
    ] or [accounts.DEFAULT_STALE_AFTER_S]
    return models.check(
        chains,
        ledger,
        now,
        stale_after=min(bounds),
        low_pct=tgt.resolved["quota.low_pct"],
        unknown_blocks=tgt.resolved["quota.unknown_blocks"],
    )


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
    pool_fn=None,
    ledger_fn=None,
    now_fn=None,
):
    """Start a new agent for ticket `number`. Returns `(unit, workdir)`.

    Admission runs under the spawn lock, once before the worktree is made
    and once right before the unit starts, so two launches never race on
    a slot. The worktree is made under the same lock, as its module asks.
    The project's model chains are validated and re-routed before anything
    is made; an exhausted chain refuses the launch.
    """
    _check_text(prompt, "prompt")
    model = _check_model(model)
    if title is not None and not isinstance(title, str):
        raise LaunchError("title must be a string")
    if runtime_config is not None and not isinstance(runtime_config, dict):
        raise LaunchError("runtime config must be a JSON object")
    adapter = _adapter(tgt.runtime)
    session = _session(tgt, number)
    chains = _model_chains(
        tgt,
        (pool_fn or accounts.providers)(),
        (ledger_fn or quota_ledger.load)(),
        (now_fn or time.time)(),
    )

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

    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn)
        _write_session(session, adapter, tgt.runtime, prompt, runtime_config, chains)
        unit = units.unit_name(tgt.prefix, tgt.repo, number, units.next_gen(session))
        command = adapter.argv(session, prompt, model=model, max_time=runtimes.DEFAULT_MAX_TIME)
        argv = units.unit_argv(
            unit, workdir, session, command, _env(adapter, session, unit), RUNTIME_MAX_S
        )
        units.start(argv, session, unit, run=run)
    return unit, workdir


def _write_session(session, adapter, runtime, prompt, runtime_config, chains=None):
    """The prompt, the runtime record and the per-launch config, into the session dir."""
    try:
        os.makedirs(session, exist_ok=True)
        paths.atomic_write_text(os.path.join(session, "prompt.md"), prompt)
        runtimes.record(session, runtime)
        config = runtime_config or {}
        if chains:
            config = adapter.with_models(config, chains)
        adapter.write_config(session, config)
    except (ValueError, TypeError, OSError) as err:
        raise LaunchError(f"cannot write the agent's session: {err}") from None


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

    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn)
        unit = units.unit_name(tgt.prefix, tgt.repo, number, max(units.next_gen(session), 1))
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


def _tail_text(path):
    """The last ``TAIL_SCAN_BYTES`` of the activity file, as text.

    Seeks past the head instead of reading it: a stale quota line from
    long ago must not mark a provider after the error scrolled past.
    A file that will not stat or seek still reads fully.
    """
    from corral import quotacli

    with open(path, "rb") as handle:
        try:
            size = os.fstat(handle.fileno()).st_size
        except OSError:
            size = None
        if size is not None:
            try:
                handle.seek(max(0, size - quotacli.TAIL_SCAN_BYTES))
            except OSError:
                pass
        return handle.read().decode("utf-8", "replace")


def tail(tgt, number, last=None, out=None, *, pool_fn=None, note_fn=None, now_fn=None):
    """Print the ticket's agent activity from its session file. Returns the new offset.

    Raises LaunchError when the session has no activity file yet. The tail
    also feeds the recent session output to each quota reader: a provider
    whose reader parses it as a limit error gets a reactive mark, through
    ``corral.quotacli.note_limit_error`` under the ledger lock, so a
    concurrent refresh cannot lose the mark. An empty pool skips the
    ledger, and quota trouble (an unreadable pool or ledger, a failing
    reader) warns once on stderr and still returns what was shown.
    """
    from corral import quotacli

    session = _session(tgt, number)
    adapter = _adapter(runtimes.recorded(session))
    path = adapter.activity_file(session)
    if path is None:
        raise LaunchError(f"#{number} of {tgt.repo} has no {adapter.name} activity yet")
    shown = runtime_tail.show(path, last=last, out=out)
    try:
        pool = (pool_fn or accounts.providers)()
        if pool:
            text = _tail_text(path)
            now = (now_fn or time.time)()

            def change(ledger):
                fresh, marked = (note_fn or quotacli.note_limit_error)(ledger, pool, text, now)
                return fresh if marked else None

            quota_ledger.update(change)
    except Exception as err:  # AccountsError, LedgerError, or a reader surprise
        print(f"corral: quota tail hook skipped: {err}", file=sys.stderr)
    return shown
