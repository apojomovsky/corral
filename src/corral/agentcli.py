"""`corral agent`: register, heartbeat, finish and list agents on this host.

Exit codes: 0 ok, 4 usage (a bad id, runtime, ticket or outcome, an
unknown agent, an unusable registry setting).
"""

import argparse
import json
import sys
import time

OK, USAGE = 0, 4


def _err(text):
    print(f"corral: {text}", file=sys.stderr)


def _ticket(args):
    from corral import registry

    return registry.make_ticket(args.repo, args.number, args.project)


def _ticket_text(entry):
    from corral import registry

    held = registry.ticket_of(entry)
    if held is None:
        return "-"
    project, repo, number = held
    return f"{project + '/' if project else ''}{repo} {number}"


def cmd_agent(args: argparse.Namespace) -> int:
    from corral import registry, settings

    verb = args.agent_verb
    try:
        if verb == "register":
            entry = registry.register(
                args.agent_id,
                args.runtime,
                ticket=_ticket(args),
                model=args.model,
                parent=args.parent,
                pid=args.pid,
                transcript=args.transcript,
                note=args.note,
                cfg=registry.config(),
            )
            print(f"registered {entry['id']}")
            return OK
        if verb == "heartbeat":
            registry.heartbeat(args.agent_id, note=args.note, ticket=_ticket(args))
            return OK
        if verb == "done":
            entry = registry.done(args.agent_id, outcome=args.outcome, pr=args.pr)
            print(f"{entry['id']} done ({entry['outcome']})")
            return OK
        rows = registry.visible(time.time(), cfg=registry.config())
    except (registry.RegistryError, settings.SettingsError) as err:
        _err(str(err))
        return USAGE
    if args.json:
        print(json.dumps([dict(entry, status=state) for entry, state in rows], sort_keys=True))
        return OK
    if not rows:
        print("no registered agents")
        return OK
    for entry, state in rows:
        note = f"  {entry['note']}" if entry.get("note") else ""
        print(f"{entry['id']}  {entry['runtime']}  {state}  {_ticket_text(entry)}{note}")
    return OK


def _ticket_args(p):
    p.add_argument("--project", default=None, help="the ticket's project")
    p.add_argument("--repo", default=None, help="the ticket's repository")
    p.add_argument("--number", type=int, default=None, help="the ticket number")


def add_parser(sub) -> None:
    """Register `agent` and its verbs on the top-level subparsers."""
    from corral import registry

    agent = sub.add_parser("agent", help="the agent registry: who is running on this host")
    verbs = agent.add_subparsers(dest="agent_verb", required=True)
    p = verbs.add_parser("register", help="record a live agent (idempotent)")
    p.add_argument("agent_id", help="a stable id for this agent")
    p.add_argument("--runtime", required=True, choices=registry.RUNTIMES, help="the agent runtime")
    _ticket_args(p)
    p.add_argument("--model", default=None, help="the model it runs")
    p.add_argument("--parent", default=None, help="the agent that started it")
    p.add_argument("--pid", type=int, default=None, help="its process id on this host")
    p.add_argument("--transcript", default=None, help="its transcript file")
    p.add_argument("--note", default=None, help="what it is doing")
    p = verbs.add_parser("heartbeat", help="refresh last-seen and the activity note")
    p.add_argument("agent_id", help="the registered id")
    p.add_argument("--note", default=None, help="what it is doing now")
    _ticket_args(p)
    p = verbs.add_parser("done", help="close the entry")
    p.add_argument("agent_id", help="the registered id")
    p.add_argument("--outcome", default="ok", choices=registry.OUTCOMES, help="how it ended")
    p.add_argument("--pr", default=None, help="the PR it opened")
    p = verbs.add_parser("list", help="agents that are not gone")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    agent.set_defaults(func=cmd_agent)
