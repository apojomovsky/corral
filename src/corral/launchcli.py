"""`corral launch|resume|stop|tail`: the agent verbs over `corral.launch`.

Exit codes: 0 ok; 1 nothing to stop; 2 already live or race lost; 3 systemd
or a host unreachable; 4 usage or configuration error; 75 no free slot.

The production seams are built in one place, `production_seams`, and passed
to `add_parser` as a required argument. A test passes fakes instead.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import NamedTuple

from corral import launch, paths, runtimes, settings, units, worktrees

OK, NOTHING, RACE, UNREACHABLE, USAGE, NO_SLOT = 0, 1, 2, 3, 4, 75


class Seams(NamedTuple):
    """The subprocess seams the verbs use: a listing, a start and a stop."""

    list_fn: Callable[[str], list]
    run: Callable[..., object]
    stop_fn: Callable[[str], object]


def production_seams() -> Seams:
    """The real seams. Only the CLI builds them; tests never call this."""
    return Seams(
        list_fn=lambda prefix: units.list_units(prefix, run=units.default_run),
        run=units.default_run,
        stop_fn=units.systemctl_stop,
    )


#: Errors the verbs report as one line on stderr. Anything else is a bug.
KNOWN = (
    launch.LaunchError,
    units.UnitsError,
    worktrees.WorktreeError,
    runtimes.UnknownRuntime,
    settings.SettingsError,
    paths.PathError,
)


def _exit_code(err):
    if isinstance(err, (launch.LaunchError, units.UnitsError)):
        return err.exit_code
    return USAGE


def _root(args):
    root = args.root or settings.find_root(os.getcwd())
    if not root:
        raise launch.LaunchError(
            f"no {settings.PROJECT_FILE} above the working directory: pass --root"
        )
    return root


def _target(args, flags=None):
    return launch.target(_root(args), args.repo, flags=flags)


def _read_prompt(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        raise launch.LaunchError(f"cannot read the prompt file: {err.strerror}") from None
    if not text.strip():
        raise launch.LaunchError(f"the prompt file {path} is empty")
    return text


def _read_config(path):
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as err:
        raise launch.LaunchError(f"cannot read the runtime config {path}: {err}") from None
    if not isinstance(config, dict):
        raise launch.LaunchError(f"the runtime config {path} must hold a JSON object")
    return config


def cmd_launch(args, seams: Seams) -> int:
    prompt = _read_prompt(args.prompt_file)
    config = _read_config(args.runtime_config)
    flags = {"agents.runtime": args.runtime} if args.runtime else None
    tgt = _target(args, flags)
    unit, workdir = launch.launch(
        tgt,
        args.number,
        prompt,
        title=args.title,
        model=args.model,
        runtime_config=config,
        list_fn=seams.list_fn,
        run=seams.run,
    )
    print(f"launched {unit} in {workdir}")
    return OK


def cmd_resume(args, seams: Seams) -> int:
    tgt = _target(args)
    unit, workdir = launch.resume(
        tgt,
        args.number,
        args.message,
        model=args.model,
        list_fn=seams.list_fn,
        run=seams.run,
    )
    print(f"resumed as {unit} in {workdir}")
    return OK


def cmd_stop(args, seams: Seams) -> int:
    tgt = _target(args)
    stopped = launch.stop(tgt, args.number, list_fn=seams.list_fn, stop_fn=seams.stop_fn)
    if not stopped:
        print("nothing to stop")
        return NOTHING
    for unit in stopped:
        print(f"stopped {unit}")
    return OK


def cmd_tail(args, seams: Seams) -> int:
    if args.last is not None and args.last < 1:
        raise launch.LaunchError("--last must be 1 or more")
    tgt = _target(args)
    launch.tail(tgt, args.number, last=args.last)
    return OK


def _verb(fn, seams: Seams):
    """The verb as `args -> exit code`, one stderr line and the code for a known error."""

    def run(args):
        try:
            return fn(args, seams)
        except KNOWN as err:
            print(f"corral: {err}", file=sys.stderr)
            return _exit_code(err)

    return run


def _common(p):
    p.add_argument("repo", help="the repository's name in corral.toml")
    p.add_argument("number", type=int, help="the ticket number")
    p.add_argument(
        "--root", default=None, help="the ops root (default: nearest corral.toml upward)"
    )


def add_parser(sub, seams: Seams) -> None:
    """Register launch, resume, stop and tail on the top-level subparsers."""
    p = sub.add_parser("launch", help="start an agent on a ticket in its own worktree")
    _common(p)
    p.add_argument("--prompt-file", required=True, help="the prompt the agent starts with")
    p.add_argument("--title", default=None, help="the ticket title, to name a new branch")
    p.add_argument("--model", default=None, help="the model the runtime should use")
    p.add_argument("--runtime", default=None, help="the runtime (default: agents.runtime)")
    p.add_argument("--runtime-config", default=None, help="a JSON object file, the runtime config")
    p.set_defaults(func=_verb(cmd_launch, seams))

    p = sub.add_parser("resume", help="resume the ticket's newest session in a new unit")
    _common(p)
    p.add_argument("--message", required=True, help="what the resumed agent is told")
    p.add_argument("--model", default=None, help="the model the runtime should use")
    p.set_defaults(func=_verb(cmd_resume, seams))

    p = sub.add_parser("stop", help="stop the ticket's live agents")
    _common(p)
    p.set_defaults(func=_verb(cmd_stop, seams))

    p = sub.add_parser("tail", help="show the ticket's agent activity")
    _common(p)
    p.add_argument("--last", type=int, default=None, help="only the last K lines")
    p.set_defaults(func=_verb(cmd_tail, seams))


def run_verb(argv: Sequence[str], seams: Seams) -> int:
    """Parse and run one verb with the given seams. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="corral")
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub, seams)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    return args.func(args)
