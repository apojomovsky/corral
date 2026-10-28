"""`corral prompts`: show, eject and check the role prompt templates.

Exit codes: 0 ok; 1 `check` found a stale ejection or an invalid
override; 4 usage or configuration error.
"""

import argparse
import os
import sys
from collections.abc import Sequence

from corral import paths, prompts, settings

OK, STALE, USAGE = 0, 1, 4

#: Errors the verbs report as one line on stderr. Anything else is a bug.
KNOWN = (prompts.PromptsError, settings.SettingsError, paths.PathError)


def _root(args):
    return args.ops or settings.find_root(os.getcwd())


def cmd_show(args: argparse.Namespace) -> int:
    text, _ = prompts.effective(args.role, _root(args))
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return OK


def cmd_eject(args: argparse.Namespace) -> int:
    root = _root(args)
    if not root:
        raise prompts.PromptsError("no ops root found: pass --ops DIR")
    print(prompts.eject(args.role, root, force=args.force))
    return OK


def cmd_check(args: argparse.Namespace) -> int:
    result = prompts.check(_root(args))
    for role in prompts.ROLES:
        if role in result.invalid:
            print(f"{role}: invalid: {result.invalid[role]}")
    for role in result.stale:
        print(f"{role}: stale (built-in changed since eject)")
    if result.clean:
        print("prompts ok")
        return OK
    return STALE


def _verb(fn):
    """The verb as `args -> exit code`, one stderr line and the code for a known error."""

    def run(args):
        try:
            return fn(args)
        except KNOWN as err:
            print(f"corral: {err}", file=sys.stderr)
            return USAGE

    return run


def _role_arg(p):
    p.add_argument("role", choices=list(prompts.ROLES), help="worker, reviewer or triage")
    p.add_argument("--ops", default=None, help="the ops root (default: nearest corral.toml upward)")


def add_parser(sub) -> None:
    """Register `prompts` and its verbs on the top-level subparsers."""
    p = sub.add_parser("prompts", help="show, eject and check the role prompt templates")
    verbs = p.add_subparsers(dest="prompts_verb", required=True)
    s = verbs.add_parser("show", help="print the effective template, unrendered")
    _role_arg(s)
    s.set_defaults(func=_verb(cmd_show))
    e = verbs.add_parser("eject", help="copy the built-in template out for editing")
    _role_arg(e)
    e.add_argument("--force", action="store_true", help="overwrite an existing ejected file")
    e.set_defaults(func=_verb(cmd_eject))
    c = verbs.add_parser("check", help="validate overrides and report stale ejections")
    c.add_argument("--ops", default=None, help="the ops root (default: nearest corral.toml upward)")
    c.set_defaults(func=_verb(cmd_check))


def run_verb(argv: Sequence[str]) -> int:
    """Parse and run one verb. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="corral")
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    return args.func(args)
