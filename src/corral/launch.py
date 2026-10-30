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

Errors carry the public exit code: LaunchError 4, CapFull 75, HeldError 1.
The units errors (UnitExists 2, Unreachable 3, UnitsError 4) pass through
unchanged.
A launch or resume writes nothing into the ticket's session until its
second admission has passed, under the same spawn lock as the start. A
launch that loses the race leaves the live agent's session as it was.

Stop ends only the ticket's live units its session log proves it started.
A live unit the log does not prove ours is left running and named (see
`unproven`); the CLI exits 2 for it, and 1 when no unit of the ticket
is live at all.
"""

import os
import socket
import sys
import time
from dataclasses import dataclass

from corral import accounts, admission, disk, models, paths, projects, quota_ledger
from corral import registry, runtimes, settings, units, worktrees
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


class HeldError(LaunchError):
    """The ticket carries the hold label, or its labels are unreadable: no resume (exit 1)."""

    exit_code = 1


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


def _ticket_owners(counted, names, cfg):
    """``{(repo, number): project}`` for counted tickets naming a registered project.

    Only live entries of this host attribute a ticket, the same entries
    `registry.counted_tickets` counts: a stale or finished holder of the
    same ticket cannot steal its attribution.
    """
    owners = {}
    host = socket.gethostname()
    for entry, state in registry.visible(time.time(), cfg, host=host):
        held = registry.ticket_of(entry)
        if state != registry.LIVE or entry.get("host") != host:
            continue
        if held is None or held not in counted or held in owners:
            continue
        ticket = entry.get("ticket") or {}
        if ticket.get("project") in names:
            owners[held] = ticket["project"]
    return owners


def admission_state(registered, list_fn, cfg, key, own=()):
    """``{(project, repo, number)}`` live triples across every project.

    Units count for the project whose prefix names them; registry tickets
    count for the registered project their ticket names, else for `key`,
    the project asking. `own` is the launching prefix's unit and registry
    tickets: with no registration every live ticket counts for `key`,
    today's single-project behaviour.
    """
    names = {project.name for project in registered}
    live = {(key, repo, number) for repo, number in own}
    for project in registered:
        for repo, number in units.live_tickets(project.unit_prefix, list_fn(project.unit_prefix)):
            live.add((project.name, repo, number))
    counted = registry.counted_tickets(cfg=cfg)
    owners = _ticket_owners(counted, names, cfg) if counted else {}
    for repo, number in counted:
        live.add((owners.get((repo, number)) or key, repo, number))
    return live


def project_live_counts(registered, list_fn, cfg):
    """``{name: live}`` per registered project, from the admission state."""
    state = admission_state(registered, list_fn, cfg, "")
    counts = {project.name: 0 for project in registered}
    for project, _repo, _number in state:
        if project in counts:
            counts[project] += 1
    return counts


def admit(tgt, number, *, list_fn, usage):
    """Refuse a ticket that is already live, a critically low disk, or a host with no free slot.

    The decision comes from `admission.admit`: the project key is the
    registered name of the ops root, else the unit prefix on a host with
    no registration, and the limits are the registered floors and
    ceilings (a lone project's ceiling is its `agents.cap`). Raises
    units.UnitExists (exit 2) for the ticket, CapFull (exit 75) carrying
    the verdict's reason when the host is full, or the disk refusal when
    the pressure is critical (`low` and `unknown` never block, and a
    forced start does not bypass the disk gate). `usage(path)` is the
    required disk seam (no default).
    """
    registered = projects.load_all()
    cfg = registry.config(tgt.resolved)
    project = projects.for_root(tgt.root, registered)
    key = project.name if project is not None else tgt.prefix
    own_prefix = project.unit_prefix if project is not None else tgt.prefix
    from_units = set(units.live_tickets(own_prefix, list_fn(own_prefix)))
    from_registry = registry.counted_tickets(cfg=cfg) - from_units
    if (tgt.repo, number) in from_units:
        raise units.UnitExists(f"#{number} of {tgt.repo} already has a live agent")
    if (tgt.repo, number) in from_registry:
        raise units.UnitExists(
            f"#{number} of {tgt.repo} already has a live agent {REGISTERED_NOTE}"
        )
    refusal = disk.refusal_text(tgt.resolved, usage=usage)
    if refusal is not None:
        raise CapFull(refusal)
    entries = list(registered)
    if key not in {entry.name for entry in entries}:
        entries.append(
            projects.Project(
                name=key, unit_prefix=own_prefix, root=tgt.root or "", floor=0, ceiling=tgt.cap
            )
        )
    decided = projects.limits(
        entries,
        total_cap=tgt.resolved["admission.total_cap"],
        repo_cap=tgt.resolved["admission.repo_cap"],
    )
    verdict = admission.admit(
        admission.Request(project=key, repo=tgt.repo, ticket=number, kind="start"),
        admission.State(
            live=sorted(admission_state(registered, list_fn, cfg, key, own=from_units))
        ),
        decided,
    )
    if not verdict.allowed:
        note = ""
        if project is not None and tgt.cap != project.ceiling:
            note = (
                f" (note: agents.cap {tgt.cap} differs from the registered "
                f"ceiling {project.ceiling}; the ceiling wins)"
            )
        raise CapFull(verdict.reason + note)


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
    usage,
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

    `prompt` is the text, or a `(workdir, branch) -> text` callable rendered
    after the worktree is made, so the rendered prompt names the worktree
    and branch the agent actually gets.
    """
    factory = prompt if callable(prompt) else None
    if factory is None:
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
        admit(tgt, number, list_fn=list_fn, usage=usage)
        workdir, branch = worktrees.ensure_worktree(
            tgt.clone,
            number,
            default_branch=tgt.default_branch,
            branch_template=tgt.branch_template,
            root=tgt.worktrees_root,
            title=title,
            git=git,
        )
        if factory is not None:
            prompt = factory(workdir, branch)
            _check_text(prompt, "prompt")

    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn, usage=usage)
        _write_session(session, adapter, tgt.runtime, prompt, runtime_config, chains)
        unit = units.unit_name(tgt.prefix, tgt.repo, number, units.next_gen(session))
        try:
            command = adapter.argv(session, prompt, model=model, max_time=runtimes.DEFAULT_MAX_TIME)
        except ValueError as err:
            raise LaunchError(str(err)) from None
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
    hold_reason_fn,
    model=None,
    list_fn,
    run,
    usage,
    git=worktrees.git,
):
    """Resume ticket `number`'s newest runtime session in a new unit. Returns `(unit, workdir)`.

    The runtime is the one the session recorded at launch. Raises LaunchError
    when there is no session to resume, and HeldError when the ticket is
    held for a human. `hold_reason_fn(tgt, number)` returns the hold text,
    or None when the ticket is free to resume; it is required, and it runs
    before the spawn lock is taken or any worktree is touched.
    """
    reason = hold_reason_fn(tgt, number)
    if reason is not None:
        raise HeldError(reason)
    _check_text(message, "message")
    model = _check_model(model)
    session = _session(tgt, number)
    adapter = _adapter(runtimes.recorded(session))
    handle = adapter.resume_handle(session)
    if handle is None:
        raise LaunchError(f"#{number} of {tgt.repo} has no {adapter.name} session to resume")

    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn, usage=usage)
        workdir, _branch = worktrees.ensure_worktree(
            tgt.clone,
            number,
            default_branch=tgt.default_branch,
            branch_template=tgt.branch_template,
            root=tgt.worktrees_root,
            git=git,
        )

    with units.spawn_lock():
        admit(tgt, number, list_fn=list_fn, usage=usage)
        unit = units.unit_name(tgt.prefix, tgt.repo, number, max(units.next_gen(session), 1))
        try:
            command = adapter.argv(
                session,
                message,
                model=model,
                resume=handle,
                max_time=runtimes.DEFAULT_MAX_TIME,
            )
        except ValueError as err:
            raise LaunchError(str(err)) from None
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
                handle.seek(0)
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
