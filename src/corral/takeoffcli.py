"""`corral takeoff`: the project-level gate before a PR.

Exit codes: 0 every check ok, skipped or planned, 1 a check failed,
3 the remote could not be read, 4 usage or configuration error
(including an unknown or identity `--skip` name), 75 another takeoff
held the full-suite lock past the wait (`--plan` never takes the lock).
"""

import argparse
import json
import os
import sys

OK, NO, UNREACHABLE, USAGE = 0, 1, 3, 4


def cmd_takeoff(args, *, ls_remote=None, run_cmd=None):
    """Run the verb. The seams default to the live ones; tests pass fakes."""
    from corral import takeoff

    if ls_remote is None:
        ls_remote = takeoff.production_ls_remote
    if run_cmd is None:
        run_cmd = takeoff.production_run_cmd
    try:
        result = takeoff.run_takeoff(
            args.path or os.getcwd(),
            body_file=args.body,
            with_test=args.test,
            ops_dir=args.ops,
            skips=args.skip or [],
            plan=args.plan,
            ls_remote=ls_remote,
            run_cmd=run_cmd,
        )
    except takeoff.TakeoffError as err:
        print(f"corral takeoff: {err}", file=sys.stderr)
        return USAGE
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": takeoff.SCHEMA_VERSION,
                    "base": result.base,
                    "checks": [
                        {
                            "name": check.name,
                            "status": check.status,
                            "detail": check.detail,
                            "fix": check.fix,
                            "skipped_by": check.skipped_by,
                        }
                        for check in result.checks
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return result.exit_code
    for check in result.checks:
        if check.status == "fail":
            print(f"takeoff {check.name}: FAIL - {check.detail}")
            print(f"takeoff {check.name} fix: {check.fix}")
        elif check.detail:
            print(f"takeoff {check.name}: {check.status} - {check.detail}")
        else:
            print(f"takeoff {check.name}: {check.status}")
    return result.exit_code


def add_parser(sub, *, ls_remote=None, run_cmd=None) -> None:
    """Register `takeoff` on the top-level subparsers."""
    p = sub.add_parser(
        "takeoff",
        help="the project-level gate before a PR: worktree, commits, body, leak, review, checks",
        description="Run the takeoff ritual over a worktree: the remote base, "
        "the ticket branch, the commit rules, the PR body, the leak guard, "
        "the review record and the configured takeoff commands. Exit 0 when "
        "every check is ok, skipped or planned, 1 when one failed, 3 when "
        "the remote could not be read, 4 on usage or configuration error, "
        "75 when another takeoff holds the full-suite lock past the wait. "
        "A repeatable --skip NAME waives one policy check and records it; "
        "an unknown name or an identity check is exit 4.",
    )
    p.add_argument(
        "--plan",
        action="store_true",
        help="resolve the checks without running anything: each configured "
        "command is reported as planned, the suite lock is never taken",
    )
    p.add_argument("--body", default=None, metavar="FILE", help="the PR body file")
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="NAME",
        help="waive one policy check and record the skip (repeatable): "
        "commits, em-dash, body, leak, review or checks; "
        "base and worktree cannot be skipped",
    )
    p.add_argument(
        "--test",
        action="store_true",
        help="also run checks.test when it is not already in checks.takeoff",
    )
    p.add_argument("--json", action="store_true", help="print the stable document")
    p.add_argument(
        "--ops",
        default=None,
        metavar="DIR",
        help="the ops root holding corral.toml (default: discovered from the clone)",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="the worktree to gate (default: the current directory)",
    )
    p.set_defaults(func=lambda args: cmd_takeoff(args, ls_remote=ls_remote, run_cmd=run_cmd))
