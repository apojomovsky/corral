"""`corral coord`: the coordinator lease, logbook and event queue verbs.

Every verb acts on one project's root (`coord.project_root`), chosen with
`--project` or `CORRAL_PROJECT`. Exit codes follow the public table: 0 ok,
1 nothing to do (no event, unknown event), 2 a race (the lease is held,
or a stale generation), 4 usage.
"""

import argparse
import json
import os
import sys

OK, NO, RACE, USAGE = 0, 1, 2, 4

GENERATION_ENV = "CORRAL_COORD_GENERATION"


def _err(text):
    print(f"corral: {text}", file=sys.stderr)


def _coord_snapshot():
    """The project's snapshot for `brief`; none until a project supplies one."""
    return {}


def _coord_human_prs():
    """PRs waiting on a human, for `brief`; none until a project supplies them."""
    return []


def _generation(args):
    """(generation, exit code): from --coord-generation or CORRAL_COORD_GENERATION."""
    given = getattr(args, "coord_generation", None)
    if given is not None:
        return given, None
    raw = os.environ.get(GENERATION_ENV, "").strip()
    if not raw:
        return None, None
    try:
        return int(raw), None
    except ValueError:
        _err(f"bad {GENERATION_ENV} {raw!r}")
        return None, USAGE


def _fenced(coord, args, root):
    """(generation, exit code): fence log, followup and handoff writes."""
    generation, code = _generation(args)
    if code is not None:
        return None, code
    if generation is not None:
        try:
            coord.require_current(generation, root=root)
        except coord.StaleLeaseError as err:
            _err(str(err))
            return None, RACE
    return generation, OK


def _entry_holder(coord, root):
    """Holder for a new entry: the lease holder, else manual."""
    try:
        holder = coord.describe(root=root).get("holder")
    except Exception:
        holder = None
    return holder or "manual"


def _entry_generation(coord, generation, root):
    """Generation for a new entry: given, else the current lease."""
    if generation is not None:
        return generation
    try:
        return coord.describe(root=root).get("generation", 0)
    except Exception:
        return 0


def cmd_coord(args: argparse.Namespace) -> int:
    from corral import coord

    try:
        root = coord.project_root(args.project)
    except coord.UsageError as err:
        _err(str(err))
        return USAGE
    verb = args.coord_verb
    if verb == "status":
        info = coord.describe(root=root)
        if args.json:
            print(json.dumps(info, sort_keys=True))
        elif info["free"]:
            print(f"free ({info['reason']}, generation {info['generation']})")
        else:
            print(
                f"held by {info['holder']} (runtime {info['runtime']}, "
                f"pid {info['pid']}, heartbeat {info['heartbeat_age_s']}s "
                f"ago, generation {info['generation']})"
            )
        return OK
    if verb == "acquire":
        try:
            lease = coord.acquire(holder=args.holder, root=root)
        except coord.LeaseHeldError as err:
            _err(str(err))
            return RACE
        print(f"acquired lease generation {lease['generation']} (holder {lease['holder']})")
        return OK
    if verb == "heartbeat":
        try:
            lease = coord.heartbeat(args.generation, root=root)
        except coord.StaleLeaseError as err:
            _err(str(err))
            return RACE
        if not args.quiet:
            print(f"heartbeat generation {lease['generation']}")
        return OK
    if verb == "release":
        try:
            lease = coord.release(args.generation, root=root)
        except coord.StaleLeaseError as err:
            _err(str(err))
            return RACE
        print(f"released generation {lease['generation']}")
        return OK
    if verb == "takeover":
        if not args.force:
            _err("takeover needs --force")
            return USAGE
        try:
            lease = coord.takeover(holder=args.holder, force=True, root=root)
        except coord.UsageError as err:
            _err(str(err))
            return USAGE
        print(f"takeover generation {lease['generation']} (holder {lease['holder']})")
        return OK
    if verb == "log":
        generation, code = _fenced(coord, args, root)
        if code != OK:
            return code
        try:
            entry = coord.append_entry(
                args.kind,
                args.text,
                refs=args.ref or (),
                holder=_entry_holder(coord, root),
                generation=_entry_generation(coord, generation, root),
                root=root,
            )
        except coord.UsageError as err:
            _err(str(err))
            return USAGE
        print(f"logged {entry['kind']} at {entry['at']}")
        return OK
    if verb == "followup":
        if args.followup_verb == "list":
            opened = coord.open_followups(coord.read_entries(root=root))
            if not opened:
                print("no open follow-ups")
                return OK
            for entry in opened:
                refs = " [" + ", ".join(entry.get("refs") or []) + "]" if entry.get("refs") else ""
                print(f"- {entry.get('id')}: {entry.get('text')}{refs}")
            return OK
        generation, code = _fenced(coord, args, root)
        if code != OK:
            return code
        holder = _entry_holder(coord, root)
        current = _entry_generation(coord, generation, root)
        if args.followup_verb == "add":
            entry = coord.followup_add(
                args.text, refs=args.ref or (), holder=holder, generation=current, root=root
            )
            print(f"follow-up {entry['id']}")
            return OK
        entry = coord.followup_done(args.followup_id, holder=holder, generation=current, root=root)
        print(f"closed follow-up {entry['id']}")
        return OK
    if verb == "handoff":
        generation, code = _fenced(coord, args, root)
        if code != OK:
            return code
        try:
            entry = coord.append_entry(
                "handoff",
                args.text,
                holder=_entry_holder(coord, root),
                generation=_entry_generation(coord, generation, root),
                root=root,
            )
        except coord.UsageError as err:
            _err(str(err))
            return USAGE
        print(f"handoff at {entry['at']}")
        return OK
    if verb == "brief":
        try:
            count = int(args.entries)
        except (TypeError, ValueError):
            _err("--entries needs an integer")
            return USAGE
        data = coord.brief(
            root=root, entries_n=count, snapshot_fn=_coord_snapshot, prs_fn=_coord_human_prs
        )
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
        else:
            print(coord.render_brief_text(data), end="")
        return OK
    if verb == "wait":
        from corral import coordevents as events

        try:
            timeout = None if args.timeout is None else float(args.timeout)
        except (TypeError, ValueError):
            _err("--timeout needs seconds")
            return USAGE
        try:
            batch = events.wait_for_events(timeout=timeout, root=root)
        except events.UsageError as err:
            _err(str(err))
            return USAGE
        if batch is None:
            if args.json:
                print(json.dumps([]))
            else:
                print("no coordinator events (timeout)", file=sys.stderr)
            return NO
        if args.json:
            print(json.dumps(batch, indent=2, sort_keys=True))
        else:
            for event in batch:
                evidence = " ".join(str(event.get("evidence") or "").split())
                print(
                    f"{event.get('id')} {event.get('kind')} {event.get('ref')} {evidence}".rstrip()
                )
        return OK
    if verb == "ack":
        from corral import coordevents as events

        try:
            row = events.ack(args.event_id, root=root)
        except events.UsageError as err:
            _err(str(err))
            return USAGE
        except events.UnknownEventError as err:
            _err(str(err))
            return NO
        print(f"acked {row['id']}")
        return OK
    _err(f"unknown coord verb {verb!r}")
    return USAGE


def add_parser(sub) -> None:
    """Register `coord` and its verbs on the top-level subparsers."""
    fence_help = f"fence: lease generation (or {GENERATION_ENV})"
    project = argparse.ArgumentParser(add_help=False)
    project.add_argument(
        "--project", default=None, help="the project whose coordinator this is (or CORRAL_PROJECT)"
    )
    coord = sub.add_parser("coord", help="coordinator lease, logbook and events, per project")
    verbs = coord.add_subparsers(dest="coord_verb", required=True)
    p = verbs.add_parser("acquire", parents=[project], help="take the lease when free")
    p.add_argument("--holder", default=None, help="holder id (default <runtime>:<pid>)")
    p = verbs.add_parser(
        "status", parents=[project], help="holder, pid liveness, heartbeat age, generation"
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p = verbs.add_parser(
        "heartbeat", parents=[project], help="refresh the heartbeat when the generation is current"
    )
    p.add_argument("--generation", type=int, required=True, help="generation from acquire/takeover")
    p.add_argument("--quiet", action="store_true", help="no output on success")
    p = verbs.add_parser(
        "release", parents=[project], help="drop the lease when the generation is current"
    )
    p.add_argument("--generation", type=int, required=True, help="generation from acquire/takeover")
    p = verbs.add_parser(
        "takeover", parents=[project], help="take a live lease (a human confirms first)"
    )
    p.add_argument("--force", action="store_true", help="required confirmation flag")
    p.add_argument("--holder", default=None, help="holder id (default <runtime>:<pid>)")
    p = verbs.add_parser("log", parents=[project], help="append one logbook entry")
    p.add_argument("kind", help="decision, action, incident, followup, done, handoff or note")
    p.add_argument("text", help="entry text")
    p.add_argument("--ref", action="append", default=[], help="ticket ref (repeatable)")
    p.add_argument("--coord-generation", type=int, default=None, help=fence_help)
    followup = verbs.add_parser("followup", help="open, close and list follow-ups")
    fverbs = followup.add_subparsers(dest="followup_verb", required=True)
    p = fverbs.add_parser("add", parents=[project], help="open a follow-up")
    p.add_argument("text", help="follow-up text")
    p.add_argument("--ref", action="append", default=[], help="ticket ref (repeatable)")
    p.add_argument("--coord-generation", type=int, default=None, help=fence_help)
    p = fverbs.add_parser("done", parents=[project], help="close a follow-up")
    p.add_argument("followup_id", help="8-hex-char follow-up id")
    p.add_argument("--coord-generation", type=int, default=None, help=fence_help)
    fverbs.add_parser("list", parents=[project], help="list open follow-ups")
    p = verbs.add_parser("handoff", parents=[project], help="a handoff entry for the next instance")
    p.add_argument("text", help="what is in flight")
    p.add_argument("--coord-generation", type=int, default=None, help=fence_help)
    p = verbs.add_parser("brief", parents=[project], help="everything a fresh instance needs")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--entries", default="30", help="last N logbook entries (default 30)")
    p = verbs.add_parser("wait", parents=[project], help="block until a coordinator event exists")
    p.add_argument("--timeout", default=None, help="seconds to wait (default: forever)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p = verbs.add_parser("ack", parents=[project], help="resolve one coordinator event")
    p.add_argument("event_id", help="event id from wait")
    coord.set_defaults(func=cmd_coord)
