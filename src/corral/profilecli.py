"""`corral profile`: the operator's view and switches over agent profiles.

Four verbs: `list` names every declared profile with its runtime, role
chains and winning source (a shadowed name is marked); `show` resolves
one ticket and role through the six layers with provenance; `set` writes
a local selection or a temporary override; `reset` removes one, exiting 1
when there was nothing stored. Nothing here touches the tracker or the
store except `show --ticket`, which reads the shared assignment the way
the launch path does (a stale row re-reads live; an unreachable tracker
is exit 3). A `--temp`/`--until` with `--ticket` is refused with exit 4:
layer 3 can never beat the layer-2 ticket assignment.

Exit codes: 0 ok; 1 reset with nothing stored; 3 the ticket layer needs
a live tracker read that cannot be reached; 4 usage or configuration.

The library functions take plain data (parsed profiles, selections, an
explicit now), so tests drive them without files; the verbs below take a
`Seams` naming the production readers, and tests pass fakes instead.
"""

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from typing import NamedTuple

from corral import accounts, board, paths, profiles, prompts, settings
from corral import tracker as iface

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4


class EmptyReset(ValueError):
    """A `reset` with nothing stored (exit 1), naming the missing piece."""


#: Scope names for `set` and `reset`, as the verbs spell them.
SCOPES = ("global", "project", "role", "ticket")

#: Stand-in repo for a host-scope `show` (no `--repo`, no `--ticket`).
#: The project maps are stripped first, so it never matches a selection
#: and never renders: only absent layers mention it, and those print
#: without a source.
_HOST_SCOPE = "<host>"


def list_rows(parsed, sources, shadowed) -> list[dict]:
    """One dict per declared profile, sorted by name.

    Each row carries the name, runtime, role chains, options, winning
    source path, and the losing source path when a local table shadows
    a committed one (else None).
    """
    rows = []
    for name in sorted(parsed):
        profile = parsed[name]
        rows.append(
            {
                "name": name,
                "runtime": profile.runtime,
                "models": {role: list(chain) for role, chain in profile.models.items()},
                "options": dict(profile.options),
                "source": sources.get(name, ""),
                "shadowed": shadowed.get(name),
            }
        )
    return rows


def render_list(rows, json_mode: bool = False) -> str:
    """The `list` view: one profile per block, or one JSON document."""
    import json

    if json_mode:
        return json.dumps({"profiles": rows}, indent=2, sort_keys=True) + "\n"
    if not rows:
        return "no profiles declared\n"
    lines = []
    for row in rows:
        head = f"{row['name']}: runtime={row['runtime']} from {row['source'] or 'built-in'}"
        if row["shadowed"] is not None:
            head += f" (shadows {row['shadowed']})"
        lines.append(head)
        for role in sorted(row["models"]):
            lines.append(f"  models.{role}: {', '.join(row['models'][role])}")
        for key in sorted(row["options"]):
            lines.append(f"  options.{key}: {row['options'][key]}")
    return "\n".join(lines) + "\n"


def show_document(
    parsed,
    selections,
    *,
    repo: str,
    role: str,
    number: int,
    now,
    flag: str | None = None,
    tracker: str | None = None,
    tracker_source: str = "",
    committed: dict | None = None,
    selections_source: str = profiles.SELECTIONS_FILE,
) -> dict:
    """The `show` view as plain data: the winner, its provenance, every layer.

    `repo`, `role` and `number` are the ticket and role context; callers
    without a ticket strip the pin (and, without a repo, the project)
    maps first, so those layers read absent instead of guessing. A
    winning unknown name refuses closed (exit 4) naming the value.
    """
    if committed is None:
        committed = {"default": None, "repos": {}, "roles": {}}
    if selections is None:
        selections = profiles.empty_selections()
    args = {
        "repo": repo,
        "role": role,
        "number": number,
        "now": now,
        "flag": flag,
        "tracker": tracker,
        "tracker_source": tracker_source,
        "committed": committed,
        "selections_source": selections_source,
    }
    chain = profiles.explain(parsed, selections, **args)
    profile, provenance = profiles.resolve(parsed, selections, **args)
    return {
        "profile": profile.name,
        "runtime": profile.runtime,
        "models": dict(profile.models),
        "options": dict(profile.options),
        "provenance": {
            "layer": provenance.layer,
            "layer_name": provenance.layer_name,
            "source": provenance.source,
            "profile": provenance.profile,
            "until": provenance.until,
        },
        "layers": [
            {
                "layer": considered.layer,
                "name": considered.layer_name,
                "source": considered.source,
                "profile": considered.profile,
                "state": considered.state,
            }
            for considered in chain
        ],
    }


def _layer_line(entry) -> str:
    """One `show` layer as text: its value, source and what happened to it."""
    head = f"  {entry['layer']} {entry['name']}:"
    if entry["state"] == "absent":
        return f"{head} absent"
    if entry["profile"] is None:
        return f"{head} (built-in default) [{entry['state']}] from {entry['source']}"
    line = f"{head} {entry['profile']!r} [{entry['state']}]"
    if entry["source"]:
        line += f" from {entry['source']}"
    return line


def render_show(doc, json_mode: bool = False) -> str:
    """The `show` view: the winner with its provenance, then every layer."""
    import json

    if json_mode:
        return json.dumps(doc, indent=2, sort_keys=True) + "\n"
    provenance = doc["provenance"]
    lines = [
        f"profile: {doc['profile']}",
        f"runtime: {doc['runtime']}",
        f"decided by layer {provenance['layer']} ({provenance['layer_name']})"
        f" from {provenance['source']}",
        "layers:",
    ]
    lines.extend(_layer_line(entry) for entry in doc["layers"])
    return "\n".join(lines) + "\n"


def _need_repo(value, where: str) -> str:
    """`value` as a non-empty repo name, else exit 4 naming the flag."""
    if not isinstance(value, str) or not value:
        raise profiles.ProfileError(f"{where}: needs a non-empty repo name")
    return value


def _need_role(value, where: str) -> str:
    """`value` as a non-empty role name, else exit 4 naming the flag."""
    if not isinstance(value, str) or not value:
        raise profiles.ProfileError(f"{where}: needs a non-empty role name")
    return value


def _need_number(value, where: str) -> int:
    """`value` as a positive ticket number, else exit 4 naming the value."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise profiles.ProfileError(f"{where}: needs a positive ticket number, not {value!r}")
    return value


def _store_override(
    selections: profiles.Selections, slot: str, name: str, moment: float, reason: str
) -> tuple[profiles.Selections, str]:
    """`selections` with a temporary override for `slot`; the message names
    what stood, so replacing an override never happens silently."""
    override = profiles.Override(scope=slot, profile=name, until=moment, reason=reason)
    replaced = (
        f" (replaced {selections.override.profile!r} for scope {selections.override.scope})"
        if selections.override is not None
        else ""
    )
    message = f"temporary override: profile {name!r} for {slot} until {moment!r}{replaced}"
    if reason:
        message += f": {reason}"
    return replace(selections, override=override), message


def set_selection(
    selections: profiles.Selections | None,
    *,
    scope: str,
    name: str,
    repo: str | None = None,
    role: str | None = None,
    number: int | None = None,
    temp=None,
    until=None,
    reason: str = "",
    now: float,
) -> tuple[profiles.Selections, str]:
    """`selections` with the new value; returns `(selections, message)`.

    Without `temp`/`until` the scope's slot takes `name` (a ticket pin
    for the ticket scope). With either, the scope takes a temporary
    override expiring at the parsed instant, replacing a standing one
    (the message names what stood). A duration with the ticket scope is
    refused: layer 3 can never beat the layer-2 ticket assignment, and
    the error names the fix. Pure: the caller saves the result.
    """
    if selections is None:
        selections = profiles.empty_selections()
    if not isinstance(name, str) or not name:
        raise profiles.ProfileError("set: profile names must be non-empty strings")
    if not isinstance(reason, str):
        raise profiles.ProfileError("set: --reason must be a string")
    moment = profiles.override_until(temp, until, now=now, where="set")
    if moment is not None and scope == "ticket":
        raise profiles.ProfileError(
            "set: --temp/--until with --ticket is refused: a temporary "
            "override (layer 3) can never beat the layer 2 ticket "
            "assignment; clear the ticket assignment or pin first"
        )
    if scope == "global":
        if moment is not None:
            return _store_override(selections, profiles.GLOBAL_SCOPE, name, moment, reason)
        return replace(selections, default=name), (f"selected the global default profile {name!r}")
    if scope == "project":
        repo = _need_repo(repo, "set --project")
        if moment is not None:
            return _store_override(selections, f"project:{repo}", name, moment, reason)
        projects = dict(selections.projects)
        projects[repo] = name
        return replace(selections, projects=projects), (
            f"selected profile {name!r} for project {repo!r}"
        )
    if scope == "role":
        role = _need_role(role, "set --role")
        if moment is not None:
            return _store_override(selections, f"role:{role}", name, moment, reason)
        roles = dict(selections.roles)
        roles[role] = name
        return replace(selections, roles=roles), (f"selected profile {name!r} for role {role!r}")
    if scope == "ticket":
        repo = _need_repo(repo, "set --ticket")
        number = _need_number(number, "set --ticket")
        pins = dict(selections.pins)
        pins[profiles.pin_key(repo, number)] = name
        return replace(selections, pins=pins), (
            f"pinned ticket {repo}#{number} to profile {name!r}"
        )
    raise profiles.ProfileError(f"set: unknown scope {scope!r}: want one of {', '.join(SCOPES)}")


def reset_selection(
    selections: profiles.Selections | None,
    *,
    scope: str,
    repo: str | None = None,
    role: str | None = None,
    number: int | None = None,
) -> tuple[profiles.Selections, str]:
    """`selections` with the scope's slot removed; returns `(selections, message)`.

    Raises `EmptyReset` (exit 1) naming the missing piece when the slot
    is already empty. Pure: the caller saves the result.
    """
    if selections is None:
        selections = profiles.empty_selections()
    if scope == "global":
        if selections.default is None:
            raise EmptyReset("no global default profile stored: nothing to reset")
        return replace(selections, default=None), "cleared the global default profile"
    if scope == "project":
        repo = _need_repo(repo, "reset --project")
        if repo not in selections.projects:
            raise EmptyReset(f"no profile selection stored for project {repo!r}: nothing to reset")
        projects = dict(selections.projects)
        del projects[repo]
        return replace(selections, projects=projects), (
            f"cleared the profile selection for project {repo!r}"
        )
    if scope == "role":
        role = _need_role(role, "reset --role")
        if role not in selections.roles:
            raise EmptyReset(f"no profile selection stored for role {role!r}: nothing to reset")
        roles = dict(selections.roles)
        del roles[role]
        return replace(selections, roles=roles), (
            f"cleared the profile selection for role {role!r}"
        )
    if scope == "ticket":
        repo = _need_repo(repo, "reset --ticket")
        number = _need_number(number, "reset --ticket")
        slot = profiles.pin_key(repo, number)
        if slot not in selections.pins:
            raise EmptyReset(f"no profile pin stored for ticket {slot}: nothing to reset")
        pins = dict(selections.pins)
        del pins[slot]
        return replace(selections, pins=pins), (f"cleared the profile pin for ticket {slot}")
    if scope == "temp":
        if selections.override is None:
            raise EmptyReset("no temporary override stored: nothing to reset")
        return replace(selections, override=None), "cleared the temporary override"
    raise profiles.ProfileError(
        f"reset: unknown scope {scope!r}: want one of {', '.join((*SCOPES, 'temp'))}"
    )


class Seams(NamedTuple):
    """The readers and writers the verbs use. Tests pass fakes instead."""

    #: The resolved settings with an ops root: `(resolved, root)`.
    settings_fn: Callable[[], tuple]
    #: The local selections with their source: `(selections, source)`.
    selections_fn: Callable[[], tuple[profiles.Selections, str]]
    #: Write selections; returns the path written.
    save_fn: Callable[[profiles.Selections], str]
    #: The account pool the declarations validate against.
    pool_fn: Callable[[], list]
    #: The clock for `--temp` and expiry.
    now_fn: Callable[[], float]
    #: The board store (an object with `.item`), or None to open the real one.
    store: object | None = None
    #: The tracker for a live re-read, or None to build the production one.
    tracker: object | None = None
    #: `(root, repo) -> tracker` when no fake tracker is set, or None for
    #: the production builder. Tests pass a fake.
    tracker_for: Callable | None = None


def _production_settings() -> tuple:
    """The resolved settings with their ops root. Only the CLI builds this."""
    root = settings.find_root(os.getcwd())
    return settings.load(root=root), root


def _production_selections() -> tuple:
    """The local selections with their source file. Only the CLI builds this."""
    return profiles.load_selections(), profiles.selections_path()


def production_seams() -> Seams:
    """The real seams. Only the CLI builds them; tests never call this."""
    return Seams(
        settings_fn=_production_settings,
        selections_fn=_production_selections,
        save_fn=profiles.save_selections,
        pool_fn=accounts.providers,
        now_fn=time.time,
    )


def _production_tracker(root, repo):
    """The live tracker for one repo. Only the CLI builds it; tests pass fakes."""
    if root is None:
        raise profiles.ProfileError(
            f"show: --ticket needs an ops root holding [[repo]] {repo!r}: "
            "run it inside a project clone"
        )
    from corral import trackers, worktrees

    mapping, problem = trackers.production_trackers(root, [repo], git=worktrees.git)
    if problem is not None:
        raise profiles.ProfileError(f"show: {problem}")
    return mapping[repo].tracker


def _read_assignment(seams: Seams, root, repo: str, number: int, stale_after, now):
    """The ticket's shared assignment as `(name, source)`.

    Reads the store row with a live re-read past the freshness bound,
    exactly like the launch path. A withdrawn ticket (no row) reads as
    no assignment. Corrupt store content refuses (exit 4); an
    unreachable tracker raises `TrackerError` (exit 3) unchanged, while
    a validation rejection raises `TrackerRejected` (exit 1, refused).
    """
    tracker = seams.tracker
    if tracker is None:
        builder = seams.tracker_for or _production_tracker
        tracker = builder(root, repo)
    try:
        if seams.store is None:
            with board.open_store() as real:
                name, source, _skip = profiles.load_tracker_assignment(
                    real, tracker, repo=repo, number=number, now=now, stale_after=stale_after
                )
        else:
            name, source, _skip = profiles.load_tracker_assignment(
                seams.store, tracker, repo=repo, number=number, now=now, stale_after=stale_after
            )
    except (board.StoreError, sqlite3.Error) as err:
        raise profiles.ProfileError(
            f"show: cannot read the ticket's profile assignment for {repo}#{number}: {err}"
        ) from None
    return name, source


def _show_context(args) -> tuple:
    """The `(repo, role, number)` context for `show`, validated (exit 4)."""
    role = args.role or prompts.DEFAULT_ROLE
    if args.role is not None and not args.role:
        raise profiles.ProfileError("show: --role must be a non-empty role name")
    if args.ticket is not None and args.repo is None:
        raise profiles.ProfileError("show: --ticket N needs --repo R naming the ticket")
    if args.repo is not None and not args.repo:
        raise profiles.ProfileError("show: --repo must be a non-empty repo name")
    return args.repo, role, args.ticket


def cmd_list(args: argparse.Namespace, seams: Seams) -> int:
    resolved, _root = seams.settings_fn()
    parsed = profiles.validate(resolved.profiles, seams.pool_fn())
    rows = list_rows(parsed, resolved.profile_sources, resolved.profile_shadowed)
    sys.stdout.write(render_list(rows, json_mode=args.json))
    return OK


def cmd_show(args: argparse.Namespace, seams: Seams) -> int:
    resolved, root = seams.settings_fn()
    parsed = profiles.validate(resolved.profiles, seams.pool_fn())
    selections, source = seams.selections_fn()
    repo, role, number = _show_context(args)
    moment = seams.now_fn()
    if number is not None:
        name, origin = _read_assignment(
            seams, root, repo, number, resolved["board.item_max_age_s"], moment
        )
        work, committed = selections, resolved.profile_committed
    else:
        name, origin = None, ""
        work = replace(selections, pins={})
        committed = dict(resolved.profile_committed)
        if repo is None:
            work = replace(work, projects={})
            committed = {**committed, "repos": {}}
            repo = _HOST_SCOPE
        number = 1
    doc = show_document(
        parsed,
        work,
        repo=repo,
        role=role,
        number=number,
        now=moment,
        tracker=name,
        tracker_source=origin,
        committed=committed,
        selections_source=source,
    )
    sys.stdout.write(render_show(doc, json_mode=args.json))
    return OK


def cmd_set(args: argparse.Namespace, seams: Seams) -> int:
    if args.all:
        scope, repo, role, number = "global", None, None, None
    elif args.project is not None:
        scope, repo, role, number = "project", args.project, None, None
    elif args.role is not None:
        scope, repo, role, number = "role", None, args.role, None
    else:
        scope, repo_name, raw_number = "ticket", args.ticket[0], args.ticket[1]
        try:
            converted = int(raw_number, 10)
        except (TypeError, ValueError):
            raise profiles.ProfileError(
                f"set: --ticket needs a positive ticket number, not {raw_number!r}"
            ) from None
        repo, role, number = repo_name, None, converted
    selections, _source = seams.selections_fn()
    updated, message = set_selection(
        selections,
        scope=scope,
        name=args.name,
        repo=repo,
        role=role,
        number=number,
        temp=args.temp,
        until=args.until,
        reason=args.reason,
        now=seams.now_fn(),
    )
    seams.save_fn(updated)
    print(message)
    return OK


def cmd_reset(args: argparse.Namespace, seams: Seams) -> int:
    if args.all:
        scope, repo, role, number = "global", None, None, None
    elif args.project is not None:
        scope, repo, role, number = "project", args.project, None, None
    elif args.role is not None:
        scope, repo, role, number = "role", None, args.role, None
    elif args.ticket is not None:
        try:
            converted = int(args.ticket[1], 10)
        except (TypeError, ValueError):
            raise profiles.ProfileError(
                f"reset: --ticket needs a positive ticket number, not {args.ticket[1]!r}"
            ) from None
        scope, repo, role, number = "ticket", args.ticket[0], None, converted
    else:
        scope, repo, role, number = "temp", None, None, None
    selections, _source = seams.selections_fn()
    updated, message = reset_selection(selections, scope=scope, repo=repo, role=role, number=number)
    seams.save_fn(updated)
    print(message)
    return OK


def _verb(fn, seams: Seams):
    """The verb as `args -> exit code`, one stderr line and the code for a known error."""

    def run(args):
        try:
            return fn(args, seams)
        except EmptyReset as err:
            print(f"corral: {err}", file=sys.stderr)
            return NOTHING
        except iface.TrackerRejected as err:
            print(f"corral: {err}", file=sys.stderr)
            return NOTHING
        except iface.TrackerError as err:
            print(f"corral: {err}", file=sys.stderr)
            return UNREACHABLE
        except (
            settings.SettingsError,
            accounts.AccountsError,
            paths.PathError,
        ) as err:
            print(f"corral: {err}", file=sys.stderr)
            return USAGE

    return run


def add_parser(sub, seams: Seams) -> None:
    """Register `profile` and its verbs on the top-level subparsers."""
    parser = sub.add_parser("profile", help="operator profile selections and their provenance")
    verbs = parser.add_subparsers(dest="profile_verb", required=True)
    p = verbs.add_parser("list", help="every named profile with its runtime and chains")
    p.add_argument("--json", action="store_true", help="one JSON document on stdout")
    p.set_defaults(func=_verb(cmd_list, seams))
    p = verbs.add_parser("show", help="the resolved profile for a ticket and role, with provenance")
    p.add_argument("--repo", default=None, help="the repository holding the ticket")
    p.add_argument("--role", default=None, help="the role to resolve for (default: worker)")
    p.add_argument("--ticket", type=int, default=None, metavar="N", help="the ticket number")
    p.add_argument("--json", action="store_true", help="one JSON document on stdout")
    p.set_defaults(func=_verb(cmd_show, seams))
    p = verbs.add_parser("set", help="write a local selection or temporary override")
    p.add_argument("name", help="the profile to select")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--global", dest="all", action="store_true", help="the global default")
    scope.add_argument("--project", default=None, metavar="REPO", help="one repo's selection")
    scope.add_argument("--role", default=None, metavar="ROLE", help="one role's selection")
    scope.add_argument(
        "--ticket", nargs=2, default=None, metavar=("REPO", "N"), help="one ticket's pin"
    )
    p.add_argument("--temp", default=None, help="a duration (2h): override until now plus this")
    p.add_argument("--until", default=None, help="epoch seconds: override until this instant")
    p.add_argument("--reason", default="", help="why the selection or override was set")
    p.set_defaults(func=_verb(cmd_set, seams))
    p = verbs.add_parser("reset", help="remove a local selection, pin or override")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--global", dest="all", action="store_true", help="the global default")
    scope.add_argument("--project", default=None, metavar="REPO", help="one repo's selection")
    scope.add_argument("--role", default=None, metavar="ROLE", help="one role's selection")
    scope.add_argument(
        "--ticket", nargs=2, default=None, metavar=("REPO", "N"), help="one ticket's pin"
    )
    scope.add_argument("--temp", action="store_true", help="the standing temporary override")
    p.set_defaults(func=_verb(cmd_reset, seams))


def run_verb(argv, seams: Seams) -> int:
    """Parse and run `profile ...` with the given seams. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="corral")
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub, seams)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    return args.func(args)
