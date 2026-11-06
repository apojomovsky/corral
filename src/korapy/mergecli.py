"""`korapy merge`: the serialized, verified merge verb.

Merges one pull request after proving it is mergeable, one merger at
a time per repository, and reports every precondition as data. The
forge-side half of landing; `push` stays the worktree-side half.

The ticket rides along explicitly (the same `REPO NUMBER` positionals
`review show` takes, from the shared parser): the review-record check
uses the same lookup takeoff uses, so both gates agree. The forge repo
and PR number name what is merged; `--head` pins the exact head the
caller approved. The method is `--method` when given, else the
`merge.method` project setting (a squash by default, or a merge
commit). `--skip` waives only `checks`; `--plan` reports the rows
without taking the lock and without merging.

Exit codes: 0 merged (under `--plan`: mergeable), 1 a precondition
failed, the merge was refused, or the forge or the repository settings
disallow the method (the `method` row names `merge.method`),
2 another merger holds the lock, 3 the forge is unreachable, 4 usage
(a bad `--skip`, `--method`, `--head`, or `--forge-repo`, a bad
`merge.method` setting, or a missing `--pr`).
"""

import argparse
import json
import os
import subprocess
import sys

from korapy import reviewcli

OK, NO, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4


def _err(text):
    print(f"korapy: {text}", file=sys.stderr)


def _git(args, cwd):
    """A local git seam for the origin lookup. None when git cannot run."""
    from korapy.gitenv import clean_env

    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _production_forge():
    """The live forge over the production `gh` runner."""
    from korapy.github import forge as ghforge
    from korapy.github.gh import paced_run

    return ghforge.GithubForge(paced_run)


def cmd_merge(args: argparse.Namespace, forge=None) -> int:
    from korapy import forge as iface, merge, reviewrecord, settings, syncli
    from korapy.github import forge as ghforge

    if args.pr is None:
        _err("korapy merge needs --pr N: the pull request number on the forge")
        return USAGE
    want = args.forge_repo or ghforge.origin_repo(os.getcwd(), _git)
    if want is None:
        _err("korapy merge needs --forge-repo owner/name: no forge repo in the origin remote")
        return USAGE
    if not ghforge.is_repo_name(want):
        _err(f"korapy merge needs --forge-repo as owner/name, got {want!r}")
        return USAGE
    try:
        root = settings.find_root(os.getcwd())
        ticket_repo = reviewrecord.local_repo(args.repo, syncli.short_to_tracker(root, _git))
    except ValueError as err:
        _err(f"korapy merge: {err}")
        return USAGE
    try:
        if root is not None and ticket_repo in settings.repo_names(root):
            resolved = settings.load(root=root, repo=ticket_repo)
        else:
            resolved = settings.load(root=root)
    except settings.SettingsError as err:
        _err(f"korapy merge: {err}")
        return USAGE
    try:
        method = merge.resolve_method(flag=args.method, resolved=resolved)
    except merge.MergeError as err:
        _err(f"korapy merge: {err}")
        return USAGE
    adapter = forge if forge is not None else _production_forge()
    try:
        result = merge.run_merge(
            ticket_repo,
            args.number,
            want,
            args.pr,
            head_sha=args.head,
            method=method,
            skips=args.skip or [],
            plan=args.plan,
            forge=adapter,
        )
    except merge.MergeError as err:
        _err(f"korapy merge: {err}")
        return USAGE
    except iface.ForgeRefused as err:
        _err(f"korapy merge refused: {err}")
        return NO
    except iface.ForgeError as err:
        _err(f"korapy merge: forge unreachable: {err}")
        return UNREACHABLE
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": merge.SCHEMA_VERSION,
                    "repo": want,
                    "pr": args.pr,
                    "head": args.head,
                    "method": method,
                    "checks": [
                        {"name": check.name, "status": check.status, "detail": check.detail}
                        for check in result.checks
                    ],
                    "skipped": result.skipped,
                    "merged": result.merged,
                    "merge_sha": result.merge_sha,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return result.exit_code
    for check in result.checks:
        if check.detail:
            print(f"merge {check.name}: {check.status} - {check.detail}")
        else:
            print(f"merge {check.name}: {check.status}")
    print(f"merged {result.merge_sha}" if result.merged else "not merged")
    return result.exit_code


def add_parser(sub) -> None:
    """Register `merge` on the top-level subparsers."""
    p = sub.add_parser(
        "merge",
        parents=[reviewcli.ticket_parser()],
        help="merge one PR after proving it mergeable, one merger per repo",
        description="Merge one pull request after proving it is mergeable, "
        "serially with every precondition reported as data. The ticket rides "
        "along explicitly (the same REPO NUMBER positionals `review show` "
        "takes); the review-record check uses the same lookup takeoff uses. "
        "The method is --method when given, else the merge.method project "
        "setting (a squash by default, or a merge commit). "
        "Exit 0 when merged (under --plan: mergeable), 1 when a precondition "
        "fails, the merge is refused, or the forge or the repository settings "
        "disallow the method, 2 when another merger holds the "
        "per-repo lock, 3 when the forge is unreachable, 4 on usage.",
    )
    p.add_argument(
        "--pr",
        type=int,
        default=None,
        help="the pull request number on the forge (required)",
    )
    p.add_argument(
        "--forge-repo",
        default=None,
        help="owner/name for the forge lookup (default: the clone's origin remote)",
    )
    p.add_argument(
        "--head",
        default=None,
        help="the exact head SHA the caller approved (required)",
    )
    p.add_argument(
        "--method",
        default=None,
        help="the merge method (squash or merge): default from the merge.method "
        "project setting (squash); anything else is exit 4",
    )
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="CHECK",
        help="waive one policy check and record the skip (repeatable): "
        "only checks; lock, open, head, review and fork cannot be skipped",
    )
    p.add_argument("--json", action="store_true", help="print the stable document")
    p.add_argument(
        "--plan",
        action="store_true",
        help="report every row and the lock state without taking the lock and without merging",
    )
    p.set_defaults(func=cmd_merge)
