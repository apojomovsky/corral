"""The `corral` command."""

import argparse
import os
import sys
from collections.abc import Sequence

from corral import accounts, agentcli, coordcli, deps, launchcli, paths, reviewcli, settings
from corral.github import ghcli

# Exit codes are a public interface (AGENTS.md).
OK, NO, RACE, UNREACHABLE, USAGE, NO_SLOT = 0, 1, 2, 3, 4, 75


def cmd_deps(args: argparse.Namespace) -> int:
    text, ok = deps.report(deps.check_all(deps.load()))
    sys.stdout.write(text)
    return OK if ok else NO


def cmd_config(args: argparse.Namespace) -> int:
    if args.root and not os.path.isfile(os.path.join(args.root, settings.PROJECT_FILE)):
        print(f"corral: no {settings.PROJECT_FILE} in --root {args.root}", file=sys.stderr)
        return USAGE
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


PATHS = {
    "state": paths.state_dir,
    "config": paths.config_dir,
    "cache": paths.cache_dir,
    "sessions": paths.sessions_dir,
}


def cmd_paths(args: argparse.Namespace) -> int:
    names = [args.name] if args.name else list(PATHS)
    resolved = {name: PATHS[name]() for name in names}
    if args.mkdir:
        for path in resolved.values():
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as err:
                print(f"corral: cannot create {path}: {err.strerror}", file=sys.stderr)
                return USAGE
    if args.name:
        print(resolved[args.name])
    else:
        for name, path in resolved.items():
            print(f"{name} = {path}")
    return OK


def cmd_prose(args: argparse.Namespace) -> int:
    from corral import diffscope, prose

    cwd = os.getcwd()
    base = diffscope.default_base(args.base)
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

    from corral import diffscope

    print(budget.dispatch_block(diffscope.default_base(args.base)))
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
    p = sub.add_parser("paths", help="the directories corral keeps on this host")
    p.add_argument("name", nargs="?", choices=list(PATHS), help="print only this one")
    p.add_argument("--mkdir", action="store_true", help="create the directories first")
    p.set_defaults(func=cmd_paths)
    from corral.diffscope import BASE_HELP

    base_help = f"base ref to diff against ({BASE_HELP})"
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
    coordcli.add_parser(sub)
    reviewcli.add_parser(sub)
    agentcli.add_parser(sub)
    ghcli.add_parser(sub)
    launchcli.add_parser(sub, launchcli.production_seams())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    try:
        return args.func(args)
    except (paths.PathError, accounts.AccountsError) as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE
