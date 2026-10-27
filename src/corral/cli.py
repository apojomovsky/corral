"""The `corral` command."""

import argparse
import os
import sys
from collections.abc import Sequence

from corral import deps

# Exit codes are a public interface (AGENTS.md).
OK, NO, USAGE = 0, 1, 4


def cmd_deps(args: argparse.Namespace) -> int:
    text, ok = deps.report(deps.check_all(deps.load()))
    sys.stdout.write(text)
    return OK if ok else NO


def default_base(args: argparse.Namespace) -> str:
    return args.base or os.environ.get("BASE_REF", "origin/master")


def cmd_prose(args: argparse.Namespace) -> int:
    from corral import diffscope, prose

    cwd = os.getcwd()
    base = default_base(args)
    try:
        if args.verify:
            found = prose.verify(base, cwd=cwd)
            print(prose.render_violations(found))
            return NO if found else OK
        blocks = prose.extract(base, cwd=cwd)
    except diffscope.DiffError as exc:
        print(f"corral prose: {exc}", file=sys.stderr)
        return USAGE
    print(prose.render_blocks(blocks))
    return OK


def cmd_push(args: argparse.Namespace) -> int:
    from corral import push

    return push.verified_push(os.getcwd(), remote=args.remote, branch=args.branch)


def cmd_budget(args: argparse.Namespace) -> int:
    from corral import budget

    print(budget.dispatch_block(default_base(args)))
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corral", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "deps",
        help="check the host programs corral needs; print how to install what is missing",
    )
    p.set_defaults(func=cmd_deps)
    base_help = "base ref to diff against (default origin/master or $BASE_REF)"
    p = sub.add_parser("prose", help="list and verify added comment blocks")
    p.add_argument(
        "--verify",
        action="store_true",
        help="exit 1 with the fix list when blocks are violating the prose rules",
    )
    p.add_argument("--base", default=None, help=base_help)
    p.set_defaults(func=cmd_prose)
    p = sub.add_parser("push", help="push the branch, verify the remote ref, retry once")
    p.add_argument("branch", nargs="?", default=None, help="branch to push (default: current)")
    p.add_argument("--remote", default="origin", help="remote to push to (default: origin)")
    p.set_defaults(func=cmd_push)
    p = sub.add_parser(
        "budget",
        help="derive a reviewer dispatch's time budget from the diff scope",
        epilog="Prints the dispatch block the review gate asks for: the one "
        "question, the scope, and an expected duration plus hard limit "
        "computed from <base>...HEAD. Paste it into the reviewer prompt "
        "so the budget is reproducible rather than hand-picked.",
    )
    p.add_argument("--base", default=None, help=base_help)
    p.set_defaults(func=cmd_budget)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    return args.func(args)
