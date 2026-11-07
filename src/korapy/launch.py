"""Launch, resume, tail and stop one ticket's agent on this host.

This composes the pieces below into one path. Settings come from
`korapy.settings`; the session dir from `korapy.paths`; the unit names,
spawn lock and start/stop from `korapy.units`; the worktree from
`korapy.worktrees`; the runtime adapter from `korapy.runtimes`; the cap
accounting from `korapy.registry`.

Every subprocess and every listing goes through an injected seam:
`list_fn(prefix)` lists units, `run(argv, timeout)` starts one, and
`stop_fn(unit)` stops one. `stop_fn` has no default. Only the CLI passes
the production stop (see `korapy.launchcli`).

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

import json
import os
import socket
import sqlite3
import sys
import time
from dataclasses import dataclass, replace

from korapy import accounts, admission, board, disk, host_ledger, models, paths, profiles
from korapy import projects, prompts, quota, quota_ledger, registry, resources, runtimes
from korapy import pushenv, settings, units, worktrees
from korapy.runtimes import muse
from korapy.runtimes import tail as runtime_tail

#: The run time limit handed to the runtime, in seconds for the unit's
#: RuntimeMaxSec. It is the same limit as runtimes.DEFAULT_MAX_TIME ("3h");
#: a test asserts the two agree.
RUNTIME_MAX_S = 3 * 60 * 60

#: Appended to an admission error when the live agent is a registered one.
REGISTERED_NOTE = "(registered agent, counts until its heartbeat goes stale)"

#: The unit's environment entry that names the agent, read by the agent's hooks.
AGENT_ID_VAR = "KORAPY_AGENT_ID"

#: The session file holding the prompt role the launch ran with, so a
#: resume runs the same reviewer mode. Absent for prompt-file launches
#: and for sessions launched before roles were recorded.
ROLE_FILE = "role"

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
class Budget:
    """The budget context one admit decided from: host, share and project.

    `host` is the machine total and `share` this project's fair share of
    it now; the launch wires both into slices. The launch never
    recomputes them. When no host budget is configured `host` and
    `share` are None while `project` still names the key: the gate
    passes everything and the launch sets no slice or limit properties.
    """

    host: resources.Usage | None
    share: resources.Usage | None
    project: str


@dataclass(frozen=True)
class Target:
    """One repository's resolved launch settings. Built by `target`."""

    root: str
    repo: str
    clone: str
    default_branch: str
    prefix: str
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
        raise LaunchError(f"repo {repo!r} has no clone.path in korapy.toml")
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
        prefix=projects.unit_prefix(root, resolved),
        cap=resolved["agents.cap"],
        branch_template=resolved["git.branch"],
        worktrees_root=resolved["git.worktrees"],
        resolved=resolved,
    )


def _leaves(tgt, number):
    """Every leaf session of the ticket, through the one session resolver."""
    try:
        return paths.ticket_sessions(tgt.repo, number)
    except paths.PathError as err:
        raise LaunchError(str(err)) from None


def _newest(tgt, number, role):
    """The ticket's newest session dir of `role`, or None."""
    try:
        return paths.newest_session(tgt.repo, number, role)
    except paths.PathError as err:
        raise LaunchError(str(err)) from None


def _next_role_gen(leaves, role):
    """The generation a fresh launch of `role` takes: past its highest."""
    gens = [session.gen for session in leaves if session.role == role]
    return (max(gens) + 1) if gens else 0


def _next_unit_gen(leaves):
    """The generation the next unit takes: past the highest ticket-wide.

    Unit names carry no role, so generations stay unique across roles:
    the max over every leaf session, 0 when none launched yet.
    """
    gens = [units.next_gen(session.path) for session in leaves]
    return max(gens) if gens else 0


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


def _charged_usage(key, own_prefix, registered, list_fn, roles, cfg, now):
    """Charged usage: the summed role costs of live agents, total and per project.

    Live means listed by the manager under a registered prefix: an entry
    whose unit is gone holds no share, so a crash frees the pool at
    once. The listing comes from `resources.live_units_by_owner` and
    the sum itself is `resources.charge_live_units`, the same helpers
    the snapshot reads, so the gate and the report agree.
    """
    live_by_owner = resources.live_units_by_owner(
        [(project.name, project.unit_prefix) for project in registered],
        key,
        own_prefix,
        list_fn,
    )
    names = {project.name for project in registered}
    charged, _per_unit = resources.charge_live_units(
        registry.visible(now, cfg), live_by_owner, names, roles
    )
    return charged


def _slice_props(decided, roles, role, controllers_read):
    """The `unit_argv` slice and limit keywords for a decided budget.

    {} when no host budget is configured: the launch then runs exactly
    as before, with no slice and no limit property. Otherwise the
    delegated controllers are read (refusing when a required one is
    missing) and the role's charge prices the limits.
    """
    if decided.host is None:
        return {}
    available = units.delegated_controllers(read=controllers_read)
    charge = roles.get(prompts.DEFAULT_ROLE if role is None else role) or resources.Usage()
    return {
        "slice": units.project_slice(decided.project),
        "memory_max": charge.memory_mb,
        "cpu_quota": charge.cpu_m,
        "tasks_max": charge.pids,
        "controllers": available,
    }


def _ensure_slices(decided, run):
    """Create the host slice then the project slice with their limits, in order.

    Nothing is created when no host budget is configured. An existing
    slice reads as created; parents always precede their children.
    """
    if decided.host is None:
        return
    units.ensure_slice(
        units.HOST_SLICE,
        memory_max=decided.host.memory_mb,
        cpu_quota=decided.host.cpu_m,
        run=run,
    )
    assert decided.share is not None
    units.ensure_slice(
        units.project_slice(decided.project),
        memory_max=decided.share.memory_mb,
        cpu_quota=decided.share.cpu_m,
        run=run,
    )


def _require_role_price(tgt, role, where):
    """Refuse an unpriced role while a host budget is set (exit 4).

    With a host budget every launch is charged by role: a role with no
    ``[resources.role.<name>]`` table would run free and set no per-agent
    limits, so the launch refuses instead, naming the role, the missing
    table and the host config file. With no host budget the gate passes
    everything and unpriced roles launch untouched. Unknown roles are
    not priced either, but they stay a ``LimitsError`` from the budget
    gate: no table could exist for them.
    """
    name = prompts.DEFAULT_ROLE if role is None else role
    if name not in prompts.ROLES:
        return
    tables = tgt.resolved.resources
    if tables is None or name not in tables.roles:
        raise LaunchError(
            f"role {name!r} has no [resources.role.{name}] price in {where}: "
            "the host budget is set, so every launch is charged; "
            "add the table to price this role"
        )


def admit(
    tgt,
    number,
    *,
    list_fn,
    usage,
    host_ledger_fn,
    role=None,
    now_fn=None,
    pressure_fn=None,
    controllers_read=None,
):
    """Refuse a ticket that is already live, a pressured host, or a host with no free slot.

    Gates run in this order and the first refusal wins: disk pressure,
    agent count, resource budget, provider quota (the quota gate runs in
    `launch`, after this returns). The count decision comes from
    `admission.admit` and the budget one from `resources.admit`: the
    project key is the registered name of the ops root, else the unit
    prefix on a host with no registration. Raises units.UnitExists
    (exit 2) for the ticket, CapFull (exit 75) carrying the verdict's
    reason when a gate refuses, or the disk refusal when the pressure is
    critical (`low` and `unknown` never block, and a forced start does
    not bypass the disk gate). Config errors in any layer raise exit 4
    and are evaluated before any gate. `usage(path)` is the required
    disk seam and `host_ledger_fn()` the required host ledger seam (the
    ensured ledger dict): neither has a default. `pressure_fn()` reads
    the host pressure, None reading as unknown (never blocking): only
    the CLI passes the production reader. With no host budget the gate
    passes everything and the pressure seam is never called. `role` is
    the prompt role being launched, None reading as worker: the budget
    prices its charge. `controllers_read(path)` reads the delegated
    controllers, None reading the real file: with a host budget a
    missing required controller refuses here (exit 4), before the
    launch makes any worktree or session. With no host budget the
    delegation is never read. Returns the decided `Budget`.
    """
    registered = projects.load_all()
    cfg = registry.config(tgt.resolved)
    project = projects.for_root(tgt.root, registered)
    key = project.name if project is not None else tgt.prefix
    own_prefix = tgt.prefix
    from_units = set(units.live_tickets(own_prefix, list_fn(own_prefix)))
    from_registry = registry.counted_tickets(cfg=cfg) - from_units
    if (tgt.repo, number) in from_units:
        raise units.UnitExists(f"#{number} of {tgt.repo} already has a live agent")
    if (tgt.repo, number) in from_registry:
        raise units.UnitExists(
            f"#{number} of {tgt.repo} already has a live agent {REGISTERED_NOTE}"
        )
    base = resources.config(tgt.resolved)
    ledger = host_ledger_fn()
    limits = None
    now = None
    where = os.path.join(paths.config_dir(), settings.HOST_CONFIG_NAME)
    if base.host is not None:
        shares = {entry.name: (entry.resources or {}) for entry in registered}
        limits = resources.join(base, shares, ledger.get("foreign", []), where=where)
        now = (now_fn or time.time)()
        _require_role_price(tgt, role, where)
        units.delegated_controllers(read=controllers_read)
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
    if limits is None or now is None:
        return Budget(host=None, share=None, project=key)
    charged = _charged_usage(key, own_prefix, registered, list_fn, limits.roles, cfg, now)
    budget = resources.admit(
        resources.Request(project=key, role=prompts.DEFAULT_ROLE if role is None else role),
        charged,
        limits,
        now,
        pressure=(pressure_fn() if pressure_fn is not None else None),
    )
    if not budget.allowed:
        raise CapFull(budget.reason)
    return Budget(host=limits.host, share=resources.share_cap(limits, key, now), project=key)


def _ticket_root(tgt, number):
    """The ticket's session root, for the no-leaf fallback below."""
    try:
        return paths.ticket_session_dir(tgt.repo, number)
    except paths.PathError as err:
        raise LaunchError(str(err)) from None


def unproven(tgt, number, *, list_fn):
    """Live units that parse to this ticket but that no session log proves ours.

    A unit counts as proven when any leaf session logs it. With no leaf
    at all the ticket root stands in: its missing log proves nothing,
    so every live unit reads unproven, and the listing still runs, so
    an unreachable manager still fails closed. `stop` leaves these
    running; the CLI reports them. Every leaf filters one shared live
    snapshot: re-listing per leaf could mix views of the manager.
    """
    leaves = _leaves(tgt, number)
    if not leaves:
        return units.unproven(
            tgt.prefix, tgt.repo, number, session_dir=_ticket_root(tgt, number), list_fn=list_fn
        )
    listing = list_fn(tgt.prefix)

    def snapshot(prefix):
        return listing

    logged = set()
    for session in leaves:
        logged.update(units.started_units(session.path))
    seen = []
    for session in leaves:
        for name in units.unproven(
            tgt.prefix, tgt.repo, number, session_dir=session.path, list_fn=snapshot
        ):
            if name not in logged and name not in seen:
                seen.append(name)
    return seen


def _env(adapter, session, unit, extra=None):
    env = dict(adapter.env(session))
    env[AGENT_ID_VAR] = unit
    if extra:
        env.update(extra)
    return env


def _push_extra(tgt, *, git, ssh_probe=None, gh_login=None):
    """The unit's extra push environment for the target's clone.

    The one resolver (`pushenv.launch_env`) decides from the clone's
    origin URL: the HTTPS fallback entries when SSH cannot work, {}
    otherwise. A clone nothing can push raises LaunchError (exit 4)
    naming both fixes, before the launch makes anything.
    """
    try:
        return pushenv.launch_env(tgt.clone, git=git, ssh_probe=ssh_probe, gh_login=gh_login)
    except pushenv.PushRefused as err:
        raise LaunchError(str(err)) from None


def _model_chains(tgt, pool, ledger, now, chains):
    """The profile's validated, re-routed chains, or {} when it declares none.

    Every entry must name a provider from the pool (exit 4 naming role and
    entry); each chain keeps only its live providers, and a required role
    with none left refuses the launch (exit 75 naming role and reset). A
    stale reading never blocks, so the strictest freshness bound wins.
    `chains` is the resolved profile's chains: profiles own the chains
    since the cutover, so there is no settings fallback here.
    """
    chains = dict(chains)
    if not chains:
        return {}
    models.validate(chains, pool)
    return models.check(
        chains,
        ledger,
        now,
        stale_after=quota.stale_after_for(chains, pool),
        low_pct=tgt.resolved["quota.low_pct"],
        unknown_blocks=tgt.resolved["quota.unknown_blocks"],
    )


@dataclass(frozen=True)
class SelectedProfile:
    """The profile one launch or resume runs on, after flag refinement.

    `runtime` is the profile's runtime, or the `--runtime` refinement;
    `model` is the `--model` refinement, or None for the runtime default;
    `chains` are the profile's re-routed model chains; `provenance` names
    the deciding layer and source, with any `--model`/`--runtime`
    refinement noted in its source. `launch` writes this to the session
    dir next to the runtime record, and a resume without flags runs the
    recorded profile instead of resolving again.
    """

    profile: profiles.Profile
    runtime: str
    model: str | None
    chains: dict
    provenance: profiles.Provenance


def _store_assignment(store, tracker, tgt, number, now):
    """The ticket's shared assignment as `(name, source)`, None when withdrawn.

    Reads the store row with a live re-read past the freshness bound,
    exactly like the claim paths. A withdrawn ticket (no row) reads as no
    assignment: the launch proceeds on the lower layers. A truncated
    database or corrupt content maps to LaunchError (exit 4), mirroring
    `drive.refresh_row`; an unreachable tracker raises TrackerError
    (exit 3) unchanged.
    """
    stale_after = tgt.resolved["board.item_max_age_s"]
    try:
        if store is None:
            with board.open_store() as real:
                name, source, _skip = profiles.load_tracker_assignment(
                    real,
                    tracker,
                    repo=tgt.repo,
                    number=number,
                    now=now,
                    stale_after=stale_after,
                )
        else:
            name, source, _skip = profiles.load_tracker_assignment(
                store,
                tracker,
                repo=tgt.repo,
                number=number,
                now=now,
                stale_after=stale_after,
            )
    except (board.StoreError, sqlite3.Error) as err:
        raise LaunchError(
            f"cannot read the ticket's profile assignment for {tgt.repo}#{number}: {err}"
        ) from None
    return name, source


def select_profile(
    tgt,
    number,
    *,
    role,
    flag=None,
    model=None,
    runtime=None,
    selections=None,
    store=None,
    tracker=None,
    pool=None,
    ledger=None,
    now=None,
) -> SelectedProfile:
    """The profile for one launch or resume: always a selection, never None.

    The declarations validate (exit 4), the six layers resolve, and the
    `--model`/`--runtime` refinements apply to the winner for this run
    only, noted in the provenance source. A `--runtime` refinement
    re-validates the profile's options against the effective adapter's
    schema: keys from one runtime never land in another's config, and
    unknown keys fail exit 4 naming the key and the runtime. With no
    `[profiles.*]` table declared and no flag, the layers fall through
    to the built-in default without touching the board store. The
    resolver runs before the caller makes anything, so a bad profile
    fails before any worktree or unit exists. `store` is the board store
    (or a fake with `.item`); `tracker` re-reads stale rows live, and
    None trusts the cached row.
    """
    pool = accounts.providers() if pool is None else pool
    parsed = profiles.validate(tgt.resolved.profiles, pool)
    moment = time.time() if now is None else now
    ledger = quota_ledger.load() if ledger is None else ledger
    if selections is None:
        selections = profiles.load_selections()
    if not tgt.resolved.profiles and flag is None:
        # No profiles declared and no flag names one: the built-in
        # default wins without reading the board store, so a corrupt
        # store never blocks a launch that resolves no layer from it.
        name, source = None, ""
        tracker = None
    elif isinstance(flag, str) and flag in parsed:
        name, source = None, ""
        tracker = None
    else:
        name, source = _store_assignment(store, tracker, tgt, number, moment)
    profile, provenance = profiles.resolve(
        parsed,
        selections,
        repo=tgt.repo,
        role=role,
        number=number,
        now=moment,
        flag=flag,
        tracker=name,
        tracker_source=source,
        committed=tgt.resolved.profile_committed,
        selections_source=profiles.selections_path(),
    )
    refined = runtime if runtime is not None else profile.runtime
    _adapter(refined)
    if runtime is not None:
        # The options validated against the declared runtime; the refined
        # run validates them again against the effective adapter, so a
        # `--runtime` switch never writes one runtime's keys verbatim into
        # another's config. Unknown keys fail exit 4 naming the key and
        # the runtime.
        profiles.validate_options(profile.name, refined, profile.options)
    bits = []
    if runtime is not None:
        bits.append(f"--runtime {runtime}")
    if model is not None:
        bits.append(f"--model {model}")
    if bits:
        provenance = replace(
            provenance, source=f"{provenance.source} (refined by {' '.join(bits)})"
        )
    chains = _model_chains(tgt, pool, ledger, moment, chains=profile.models)
    return SelectedProfile(
        profile=profile, runtime=refined, model=model, chains=chains, provenance=provenance
    )


def _close_previous(tgt, number, list_fn):
    """Mark done the registry entries of this ticket's earlier generations.

    Only entries whose units are no longer live are closed: a unit still
    live keeps its entry, and admission still refuses the ticket for it.
    Unknown or already finished ids are skipped, so a session logged
    before launch wrote registry entries relaunches cleanly. Returns the
    units closed.
    """
    logged = []
    for session in _leaves(tgt, number):
        for unit in units.started_units(session.path):
            if unit not in logged:
                logged.append(unit)
    if not logged:
        return []
    live = {name for name, active, _sub in list_fn(tgt.prefix) if active in units.LIVE_STATES}
    closed = []
    for unit in logged:
        if unit in live:
            continue
        try:
            registry.done(unit)
        except registry.RegistryError:
            continue
        except OSError as err:
            raise LaunchError(f"cannot close the previous agent {unit}: {err}") from None
        else:
            closed.append(unit)
    return closed


def _register_started(tgt, unit, number, model, role=None, *, runtime, profile):
    """Register the unit before it starts, so agent list and snapshot show it.

    The ticket names the registered project of the ops root, so the
    agent counts for the same project its unit prefix counts for. The
    role is recorded too, so snapshot shows reviewer vs worker. `runtime`
    is the resolved (possibly refined) runtime and `profile` the resolved
    profile's name, so snapshot shows the recorded profile. Both are
    required keyword arguments: every launch resolves one profile since
    the cutover, and registering a None runtime would fail closed in
    the registry instead of running the recorded setup.
    Raises LaunchError naming the unit when the registry cannot record
    it; the unit is not running yet.
    """
    project = projects.for_root(tgt.root)
    ticket = registry.make_ticket(tgt.repo, number, project.name if project is not None else None)
    try:
        registry.register(
            unit,
            runtime,
            ticket=ticket,
            model=model,
            role=role,
            profile=profile,
        )
    except (registry.RegistryError, OSError) as err:
        raise LaunchError(f"cannot register the agent {unit}: {err}") from None


def _close_never_started(unit):
    """Best-effort close of an entry whose unit never started.

    The start failed after the entry was registered; without this the
    entry would hold the ticket until its heartbeat goes stale. A close
    failure warns on stderr; the original start error still propagates.
    """
    try:
        registry.done(unit)
    except registry.RegistryError:
        pass
    except OSError as err:
        print(f"korapy: cannot close the never-started agent {unit}: {err}", file=sys.stderr)


def _check_role(role):
    """The launch role, or a LaunchError for anything but a known role or None."""
    if role is not None and role not in prompts.ROLES:
        raise LaunchError(f"unknown role {role!r}: expected one of {', '.join(prompts.ROLES)}")
    return role


def launch(
    tgt,
    number,
    prompt,
    *,
    title=None,
    model=None,
    runtime_config=None,
    role=None,
    profile=None,
    runtime=None,
    list_fn,
    run,
    usage,
    host_ledger_fn,
    git=worktrees.git,
    pool_fn=None,
    ledger_fn=None,
    now_fn=None,
    selections=None,
    store=None,
    tracker=None,
    controllers_read=None,
    pressure_fn=None,
    ssh_probe=None,
    gh_login=None,
):
    """Start a new agent for ticket `number`. Returns `(unit, workdir)`.

    Admission runs under the spawn lock, once before the worktree is made
    and once right before the unit starts, so two launches never race on
    a slot. The worktree is made under the same lock, as its module asks.
    The project's model chains are validated and re-routed before anything
    is made; an exhausted chain refuses the launch. Earlier generations'
    registry entries are closed first, and the unit is registered before
    it starts (a failed start closes the entry again), so agent list and
    snapshot show it while it runs and never without it.

    `prompt` is the text, or a `(workdir, branch) -> text` callable rendered
    after the worktree is made, so the rendered prompt names the worktree
    and branch the agent actually gets. Either form gets the registry
    heartbeat instructions appended before it is written.

    `role` is the prompt role (`--role`), or None for a prompt file. The
    reviewer role selects both the reviewer prompt and the runtime's
    read-only mode (`reviewer=True` in `adapter.argv`); it is recorded in
    the session and the registry entry, so a resume keeps the same mode
    and snapshot shows reviewer vs worker. A launch without a role runs
    as the worker role and writes its own worker generation, so it never
    touches the reviewer's record either.

    `profile` is the `--profile` flag: the layer-1 profile name. `model`
    and `runtime` are one-shot refinements of the resolved profile for
    this launch only. The profile's `options` lay under `--runtime-config`
    in the per-launch config (the flag file wins shared keys), through
    `_session_config`. The profile resolves before anything is made (no
    worktree, no unit on a bad profile); with no `[profiles.*]`
    declaration and no flag, the layers fall through to the built-in
    default. Every launch records the resolved profile next to the
    runtime record, so a resume without flags reruns it.
    """
    factory = prompt if callable(prompt) else None
    if factory is None:
        _check_text(prompt, "prompt")
    role = _check_role(role)
    reviewer = role == "reviewer"
    session_role = prompts.DEFAULT_ROLE if role is None else role
    model = _check_model(model)
    if title is not None and not isinstance(title, str):
        raise LaunchError("title must be a string")
    if runtime_config is not None and not isinstance(runtime_config, dict):
        raise LaunchError("runtime config must be a JSON object")
    pool = (pool_fn or accounts.providers)()
    ledger = (ledger_fn or quota_ledger.load)()
    moment = (now_fn or time.time)()
    # The profile resolves before any gate: a bad profile fails exit 4
    # before a full host (75) or a live ticket (2) refuses, and nothing
    # is made for it. The provider quota refuses only after the count
    # and budget gates do, so an exhausted chain waits for the first
    # admission: its refusal wins there instead. Closing previous
    # generations only drops dead entries, so it stays outside the
    # spawn locks with the profile.
    try:
        selection = select_profile(
            tgt,
            number,
            role=session_role,
            flag=profile,
            model=model,
            runtime=runtime,
            selections=selections,
            store=store,
            tracker=tracker,
            pool=pool,
            ledger=ledger,
            now=moment,
        )
    except models.Exhausted:
        with units.spawn_lock():
            admit(
                tgt,
                number,
                list_fn=list_fn,
                usage=usage,
                host_ledger_fn=host_ledger_fn,
                role=role,
                now_fn=now_fn,
                pressure_fn=pressure_fn,
                controllers_read=controllers_read,
            )
        raise
    adapter = _adapter(selection.runtime)
    session_runtime = selection.runtime
    # How this launch pushes is decided before the first lock: a clone
    # nothing can push refuses here (exit 4), before any worktree, unit
    # or registry entry exists for it.
    push_extra = _push_extra(tgt, git=git, ssh_probe=ssh_probe, gh_login=gh_login)
    _close_previous(tgt, number, list_fn)

    with units.spawn_lock():
        decided = admit(
            tgt,
            number,
            list_fn=list_fn,
            usage=usage,
            host_ledger_fn=host_ledger_fn,
            role=role,
            now_fn=now_fn,
            pressure_fn=pressure_fn,
            controllers_read=controllers_read,
        )
        chains = selection.chains
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
        decided = admit(
            tgt,
            number,
            list_fn=list_fn,
            usage=usage,
            host_ledger_fn=host_ledger_fn,
            role=role,
            now_fn=now_fn,
            pressure_fn=pressure_fn,
            controllers_read=controllers_read,
        )
        prompt = prompts.with_heartbeat(prompt)
        leaves = _leaves(tgt, number)
        try:
            session = paths.role_session_dir(
                tgt.repo, number, session_role, _next_role_gen(leaves, session_role)
            )
        except paths.PathError as err:
            raise LaunchError(str(err)) from None
        session_id = _write_session(
            session,
            adapter,
            session_runtime,
            prompt,
            runtime_config,
            chains,
            role,
            options=selection.profile.options,
        )
        try:
            profiles.record_profile(
                session,
                selection.profile,
                selection.provenance,
                model=selection.model,
                runtime=selection.runtime,
            )
        except (profiles.ProfileError, OSError) as err:
            _drop_session_id(session)
            raise LaunchError(f"cannot write the agent's profile record: {err}") from None
        try:
            unit = units.unit_name(tgt.prefix, tgt.repo, number, _next_unit_gen(leaves))
            try:
                if session_id is None:
                    command = adapter.argv(
                        session,
                        prompt,
                        model=model,
                        max_time=runtimes.DEFAULT_MAX_TIME,
                        reviewer=reviewer,
                    )
                else:
                    command = adapter.argv(
                        session,
                        prompt,
                        model=model,
                        max_time=runtimes.DEFAULT_MAX_TIME,
                        reviewer=reviewer,
                        session_id=session_id,
                    )
                env = _env(adapter, session, unit, extra=push_extra)
            except ValueError as err:
                raise LaunchError(str(err)) from None
            argv = units.unit_argv(
                unit,
                workdir,
                session,
                command,
                env,
                RUNTIME_MAX_S,
                **_slice_props(
                    decided,
                    resources.config(tgt.resolved).roles,
                    role,
                    controllers_read,
                ),
            )
        except Exception:
            if session_id is not None:
                _drop_session_id(session)
            raise
        _register_started(
            tgt,
            unit,
            number,
            model,
            role,
            runtime=session_runtime,
            profile=selection.profile.name,
        )
        try:
            _ensure_slices(decided, run)
            units.start(argv, session, unit, run=run)
        except Exception:
            if session_id is not None:
                _drop_session_id(session)
            _close_never_started(unit)
            raise
    return unit, workdir


def _drop_session_id(session):
    """Best-effort removal of a just-recorded muse session id after a failed launch.

    Only the `muse_session_id` file goes: the prompt, the runtime record and
    the config stay, so a later resume refuses cleanly instead of resuming
    a session that never ran.
    """
    try:
        os.remove(os.path.join(session, muse.SESSION_ID_FILE))
    except OSError:
        pass


def _recorded_role(session):
    """The prompt role the session was launched with, or None.

    Sessions launched from a prompt file record no role, and neither do
    sessions from before roles were recorded; both read as None, the
    worker behaviour. A role file that exists but cannot be read or
    holds an unknown role raises LaunchError: failing open to writable
    would silently resume a reviewer with write access.
    """
    try:
        with open(os.path.join(session, ROLE_FILE), encoding="utf-8") as handle:
            role = handle.read().strip()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise LaunchError(f"cannot read the agent's role: {err}") from None
    if role not in prompts.ROLES:
        raise LaunchError(f"unknown role {role!r}: expected one of {', '.join(prompts.ROLES)}")
    return role


#: The session file holding the launch's raw `--runtime-config`, so a
#: resume with `--profile` or `--runtime` can re-apply the operator's
#: overrides under the chains. One fact, one place: this module is the
#: only writer and reader of the file.
RUNTIME_CONFIG_FILE = "runtime_config.json"


def _record_runtime_config(session, runtime_config):
    """Record the launch's raw `--runtime-config` in the session dir.

    A launch without overrides removes any record, so a rebuilt config
    never inherits another run's overrides. Records the raw dict, not
    the merged config, so the resume can lay it over the new profile's
    options through `_session_config` again.
    """
    target = os.path.join(session, RUNTIME_CONFIG_FILE)
    if runtime_config:
        paths.atomic_write_text(target, json.dumps(runtime_config, indent=1, sort_keys=True) + "\n")
    else:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass


def _recorded_runtime_config(session):
    """The launch's raw `--runtime-config`, or None when it had none.

    A missing file reads as no overrides. A corrupt one fails closed
    (exit 4): silently dropping overrides would run the agent on the
    wrong setup.
    """
    target = os.path.join(session, RUNTIME_CONFIG_FILE)
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        raise LaunchError(f"cannot read the agent's runtime config record: {err}") from None
    if not isinstance(raw, dict):
        raise LaunchError(f"the runtime config record {target} must hold a JSON object")
    return raw


def _session_config(adapter, options, runtime_config, chains):
    """The per-launch config: profile options under `--runtime-config`, chains on top.

    The resolved profile's `options` lay first; the command line's
    `--runtime-config` wins on every shared key, and the live chains lay
    over both through the adapter's `with_models`, the way chains
    already did. One helper owns the order, so a launch and a resume
    with flags build the same config.
    """
    config = {**(options or {}), **(runtime_config or {})}
    if chains:
        config = adapter.with_models(config, chains)
    return config


def _write_session(
    session, adapter, runtime, prompt, runtime_config, chains=None, role=None, options=None
):
    """The prompt, the runtime and role records and the per-launch config.

    Writes into a fresh role generation dir, so no record is ever
    inherited from another role's session. The per-launch config lays
    the profile's `options` under `--runtime-config` (see
    `_session_config`), and the raw `--runtime-config` is recorded
    beside it for a later flagged resume. Returns the fresh muse
    session id for a muse launch, else None. The id is generated here,
    recorded here and passed to `argv` by the caller, so the recorded
    id and the run's id cannot differ.
    """
    try:
        os.makedirs(session, exist_ok=True)
        paths.atomic_write_text(os.path.join(session, "prompt.md"), prompt)
        runtimes.record(session, runtime)
        if role is not None:
            paths.atomic_write_text(os.path.join(session, ROLE_FILE), role + "\n")
        config = _session_config(adapter, options, runtime_config, chains)
        adapter.write_config(session, config)
        _record_runtime_config(session, runtime_config)
        if adapter.name == muse.NAME:
            session_id = muse.new_session_id()
            muse.record_session_id(session, session_id)
            return session_id
        return None
    except (ValueError, TypeError, OSError) as err:
        raise LaunchError(f"cannot write the agent's session: {err}") from None


def _no_session(tgt, number, role, verb):
    """The refusal when the ticket holds no session of `role` to resume or tail."""
    return LaunchError(
        f"#{number} of {tgt.repo} has no {runtimes.DEFAULT_RUNTIME} {verb} for role {role!r}"
    )


def resume(
    tgt,
    number,
    message,
    *,
    hold_reason_fn,
    role=None,
    model=None,
    profile=None,
    runtime=None,
    list_fn,
    run,
    usage,
    host_ledger_fn,
    git=worktrees.git,
    pool_fn=None,
    ledger_fn=None,
    now_fn=None,
    pressure_fn=None,
    selections=None,
    store=None,
    tracker=None,
    controllers_read=None,
    ssh_probe=None,
    gh_login=None,
):
    """Resume ticket `number`'s newest session of `role` in a new unit.

    Returns `(unit, workdir)`. `role` None resumes the worker session.
    Without flags the run reuses the profile the session recorded at
    launch (runtime and model head), so a session started under an
    override keeps its setup after the override expires; a session from
    before profiles were recorded falls back to its recorded runtime.
    With `--profile` (or `--model`/`--runtime`) the flag layer decides
    for the resumed run instead, the same way it does at launch, and the
    new profile is recorded over the old one. A resume with `--profile`
    or `--runtime` also rebuilds the session config from the new
    profile's options, the launch's recorded `--runtime-config` (still
    winning over the new options) and chains through `_session_config`;
    any other resume keeps the recorded setup. The new adapter's resume
    handle is checked before anything is rewritten, so a refused flagged
    resume leaves the session untouched. A lone `--runtime` without
    any declared profile still switches the run to that runtime.
    The reviewer mode is the one the launch ran with: a resumed reviewer
    stays read-only.
    Raises LaunchError when there is no session to resume, and HeldError
    when the ticket is held for a human. `hold_reason_fn(tgt, number)`
    returns the hold text, or None when the ticket is free to resume; it
    is required, and it runs before the spawn lock is taken or any
    worktree is touched.
    """
    reason = hold_reason_fn(tgt, number)
    if reason is not None:
        raise HeldError(reason)
    _check_text(message, "message")
    model = _check_model(model)
    model_given = model is not None
    role = _check_role(role) or prompts.DEFAULT_ROLE
    session = _newest(tgt, number, role)
    if session is None:
        raise _no_session(tgt, number, role, "session to resume")
    selection = None
    if profile is not None or model is not None or runtime is not None:
        selection = select_profile(
            tgt,
            number,
            role=role,
            flag=profile,
            model=model,
            runtime=runtime,
            selections=selections,
            store=store,
            tracker=tracker,
            pool=(pool_fn or accounts.providers)(),
            ledger=(ledger_fn or quota_ledger.load)(),
            now=(now_fn or time.time)(),
        )
    if selection is None:
        recorded = profiles.recorded_profile(session)
        profile_name = recorded.name if recorded is not None else None
        if recorded is not None:
            adapter = _adapter(recorded.runtime)
            session_runtime = recorded.runtime
            if not model_given:
                model = recorded.model
        else:
            session_runtime = runtimes.recorded(session)
            adapter = _adapter(session_runtime)
    else:
        adapter = _adapter(selection.runtime)
        session_runtime = selection.runtime
        model = selection.model
        profile_name = selection.profile.name
    recorded = _recorded_role(session)
    handle = adapter.resume_handle(session)
    if handle is None:
        raise LaunchError(
            f"#{number} of {tgt.repo} has no {adapter.name} session to resume for role {role!r}"
        )
    _close_previous(tgt, number, list_fn)
    # Decided like at launch, before the first lock: a clone nothing
    # can push refuses here (exit 4), before any worktree or unit exists.
    push_extra = _push_extra(tgt, git=git, ssh_probe=ssh_probe, gh_login=gh_login)

    with units.spawn_lock():
        admit(
            tgt,
            number,
            list_fn=list_fn,
            usage=usage,
            host_ledger_fn=host_ledger_fn,
            role=role,
            now_fn=now_fn,
            pressure_fn=pressure_fn,
            controllers_read=controllers_read,
        )
        workdir, _branch = worktrees.ensure_worktree(
            tgt.clone,
            number,
            default_branch=tgt.default_branch,
            branch_template=tgt.branch_template,
            root=tgt.worktrees_root,
            git=git,
        )

    with units.spawn_lock():
        decided = admit(
            tgt,
            number,
            list_fn=list_fn,
            usage=usage,
            host_ledger_fn=host_ledger_fn,
            role=role,
            now_fn=now_fn,
            pressure_fn=pressure_fn,
            controllers_read=controllers_read,
        )
        # A concurrent launch of this ticket may have added a newer
        # generation between the pre-lock read and this lock: resolve
        # the newest session again here, so the mode started is the
        # mode recorded.
        session = _newest(tgt, number, role)
        if session is None:
            raise _no_session(tgt, number, role, "session to resume")
        if selection is not None:
            adapter = _adapter(selection.runtime)
            # The new adapter's handle decides before anything is
            # rewritten: a refused flagged resume leaves the config and
            # the profile record byte-identical, so a later flagless
            # resume still runs the recorded setup.
            handle = adapter.resume_handle(session)
            if handle is None:
                raise LaunchError(
                    f"#{number} of {tgt.repo} has no {adapter.name} session to resume for role {role!r}"
                )
            if profile is not None or runtime is not None:
                try:
                    kept = _recorded_runtime_config(session)
                    adapter.write_config(
                        session,
                        _session_config(adapter, selection.profile.options, kept, selection.chains),
                    )
                except (ValueError, TypeError, OSError) as err:
                    raise LaunchError(f"cannot write the agent's session: {err}") from None
            try:
                profiles.record_profile(
                    session,
                    selection.profile,
                    selection.provenance,
                    model=selection.model,
                    runtime=selection.runtime,
                )
            except (profiles.ProfileError, OSError) as err:
                raise LaunchError(f"cannot write the agent's profile record: {err}") from None
        else:
            reread = profiles.recorded_profile(session)
            profile_name = reread.name if reread is not None else None
            if reread is not None:
                adapter = _adapter(reread.runtime)
                session_runtime = reread.runtime
                if not model_given:
                    model = reread.model
            else:
                session_runtime = runtimes.recorded(session)
                adapter = _adapter(session_runtime)
            handle = adapter.resume_handle(session)
            if handle is None:
                raise LaunchError(
                    f"#{number} of {tgt.repo} has no {adapter.name} session to resume for role {role!r}"
                )
        recorded = _recorded_role(session)
        if adapter.name == muse.NAME:
            try:
                muse.write_message(session, message)
            except OSError as err:
                raise LaunchError(f"cannot write the agent's session: {err}") from None
        unit = units.unit_name(
            tgt.prefix, tgt.repo, number, max(_next_unit_gen(_leaves(tgt, number)), 1)
        )
        try:
            command = adapter.argv(
                session,
                message,
                model=model,
                resume=handle,
                max_time=runtimes.DEFAULT_MAX_TIME,
                reviewer=(recorded == "reviewer"),
            )
            env = _env(adapter, session, unit, extra=push_extra)
        except ValueError as err:
            raise LaunchError(str(err)) from None
        argv = units.unit_argv(
            unit,
            workdir,
            session,
            command,
            env,
            RUNTIME_MAX_S,
            **_slice_props(
                decided,
                resources.config(tgt.resolved).roles,
                role,
                controllers_read,
            ),
        )
        _register_started(
            tgt,
            unit,
            number,
            model,
            recorded,
            runtime=session_runtime,
            profile=profile_name,
        )
        try:
            _ensure_slices(decided, run)
            units.start(argv, session, unit, run=run)
        except Exception:
            _close_never_started(unit)
            raise
    return unit, workdir


def stop(tgt, number, *, list_fn, stop_fn):
    """Stop every live unit of ticket `number` that a session log proves it started.

    Returns the unit names stopped, possibly none. `list_fn` and `stop_fn`
    are required; see `units.stop`. Every leaf filters one shared live
    snapshot. An unreachable manager propagates as `Unreachable`
    (exit 3), never wrapped into a usage error. A stopped unit's
    registry entry is marked done, so the ticket is startable again at
    once; an entry the registry does not know is left alone. Every
    stopped unit is closed even when one close fails; the failures raise
    once at the end.
    """
    stopped = []
    leaves = _leaves(tgt, number)
    # One live snapshot shared by every leaf: re-listing per leaf could
    # mix views of the manager. With no leaves at all the listing never
    # runs: there is nothing proven to stop.
    listing = list_fn(tgt.prefix) if leaves else []

    def snapshot(prefix):
        return listing

    for session in leaves:
        try:
            stopped.extend(
                units.stop(
                    tgt.prefix,
                    tgt.repo,
                    number,
                    session_dir=session.path,
                    list_fn=snapshot,
                    stop_fn=stop_fn,
                )
            )
        except units.Unreachable:
            raise
        except units.UnitsError as err:
            if not stopped:
                raise
            raise units.UnitsError(f"stopped {', '.join(stopped)}; {err}") from err
    failed = []
    for unit in stopped:
        try:
            registry.done(unit)
        except registry.RegistryError:
            continue
        except OSError as err:
            failed.append(f"{unit}: {err}")
    if failed:
        raise LaunchError("stopped but cannot close the registry entries: " + "; ".join(failed))
    return stopped


def _tail_text(path):
    """The last ``TAIL_SCAN_BYTES`` of the activity file, as text.

    Seeks past the head instead of reading it: a stale quota line from
    long ago must not mark a provider after the error scrolled past.
    A file that will not stat or seek still reads fully.
    """
    from korapy import quotacli

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


def tail(tgt, number, last=None, out=None, *, role=None, pool_fn=None, note_fn=None, now_fn=None):
    """Print the ticket's agent activity from its newest session of `role`.

    Returns the new offset. `role` None tails the worker session.
    Raises LaunchError when the session has no activity file yet. The tail
    also feeds the recent session output to each quota reader: a provider
    whose reader parses it as a limit error gets a reactive mark, through
    ``korapy.quotacli.note_limit_error`` under the ledger lock, so a
    concurrent refresh cannot lose the mark. An empty pool skips the
    ledger, and quota trouble (an unreadable pool or ledger, a failing
    reader) warns once on stderr and still returns what was shown.
    """
    from korapy import quotacli

    role = _check_role(role) or prompts.DEFAULT_ROLE
    session = _newest(tgt, number, role)
    if session is None:
        raise _no_session(tgt, number, role, "activity yet")
    adapter = _adapter(runtimes.recorded(session))
    path = adapter.activity_file(session)
    if path is None:
        raise LaunchError(
            f"#{number} of {tgt.repo} has no {adapter.name} activity yet for role {role!r}"
        )
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
        print(f"korapy: quota tail hook skipped: {err}", file=sys.stderr)
    return shown
