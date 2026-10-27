"""The `corral` command."""

import argparse
import sys
from collections.abc import Sequence

from corral import deps, paths, settings

# Exit codes are a public interface (AGENTS.md).
OK, NO, USAGE = 0, 1, 4


def cmd_deps(args: argparse.Namespace) -> int:
    text, ok = deps.report(deps.check_all(deps.load()))
    sys.stdout.write(text)
    return OK if ok else NO


def cmd_config(args: argparse.Namespace) -> int:
    import os

    root = args.root or settings.find_root(os.getcwd())
    if args.repo and not root:
        print("corral: --repo needs an ops root (corral.toml)", file=sys.stderr)
        return USAGE
    try:
        resolved = settings.load(root=root, repo=args.repo)
    except settings.SettingsError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE
    sys.stdout.write(settings.render(resolved, origin=args.origin))
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corral", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "deps",
        help="check the host programs corral needs; print how to install what is missing",
    )
    p.set_defaults(func=cmd_deps)
    config = sub.add_parser("config", help="resolved settings")
    verbs = config.add_subparsers(dest="config_verb", required=True)
    p = verbs.add_parser("show", help="every setting with its resolved value")
    p.add_argument("--origin", action="store_true", help="name the layer each value came from")
    p.add_argument("--repo", default=None, help="resolve for this [[repo]] entry")
    p.add_argument(
        "--root", default=None, help="the ops root (default: nearest corral.toml upward)"
    )
    p.set_defaults(func=cmd_config)
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
