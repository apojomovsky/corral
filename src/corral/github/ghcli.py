"""`corral gh-calls`: the summary of the paced `gh` shim's call log.

Exit codes follow the public table: 0 ok, 4 usage (a bad `--since`).
"""

import json
import sys

from corral.github import ghshim

OK, USAGE = 0, 4


def cmd_gh_calls(args) -> int:
    try:
        since = ghshim.parse_since(args.since)
    except ValueError as err:
        print(f"corral gh-calls: {err}", file=sys.stderr)
        return USAGE
    summary = ghshim.summarize(ghshim.log_path(), since=since)
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    elif args.by_shape:
        sys.stdout.write(ghshim.render_by_shape(summary))
    else:
        sys.stdout.write(ghshim.render(summary))
    return OK


def add_parser(sub) -> None:
    """Register `gh-calls` on the top-level subparsers."""
    p = sub.add_parser(
        "gh-calls",
        help="summarize the paced gh calls: per caller and minute, throttled, by query shape",
    )
    p.add_argument("--since", default=None, help="only calls newer than this, like 30m, 1h, 7d")
    p.add_argument("--by-shape", action="store_true", help="per GraphQL query shape only")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_gh_calls)
