"""The `corral` command."""

import argparse
import sys
from collections.abc import Sequence

from corral import deps, paths

# Exit codes are a public interface (AGENTS.md).
OK, NO, USAGE = 0, 1, 4


def cmd_deps(args: argparse.Namespace) -> int:
    text, ok = deps.report(deps.check_all(deps.load()))
    sys.stdout.write(text)
    return OK if ok else NO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corral", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "deps",
        help="check the host programs corral needs; print how to install what is missing",
    )
    p.set_defaults(func=cmd_deps)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    try:
        return args.func(args)
    except paths.PathError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE
