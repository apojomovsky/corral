"""`korapy heavy`: the heavy lane verbs.

`rollup` regenerates the portable per-key cost summary from the raw run
ledger: stdout always carries the JSON document, and `--out PATH`
additionally writes the same bytes atomically for the operator to
commit. The operator commits the copy it points at; korapy never
commits anything itself. Exit 0 with an empty key set when nothing was
learned yet (not a failure), 4 on usage or configuration.
"""

import argparse
import json
import sys

OK, USAGE = 0, 4


def _err(text):
    print(f"korapy heavy: {text}", file=sys.stderr)


def cmd_rollup(args, *, state_dir=None, now=None):
    """Run the verb. The ledger lives in the state dir; tests pass fakes."""
    from korapy import heavy, paths

    try:
        rows, sources, skipped = heavy.collect_ledger_rows(state_dir)
    except heavy.HeavyError as err:
        _err(str(err))
        return USAGE
    document = heavy.build_rollup(rows, sources, now=now)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text)
    if skipped:
        print(f"korapy heavy: skipped {skipped} corrupt ledger rows", file=sys.stderr)
    if getattr(args, "out", None):
        try:
            paths.atomic_write_text(args.out, text)
        except OSError as err:
            _err(f"cannot write the rollup to {args.out}: {err}")
            return USAGE
    return OK


def cmd_heavy(args):
    """Run the verb. The seams default to the live ones; tests pass fakes."""
    if args.heavy_verb == "rollup":
        return cmd_rollup(args)
    raise AssertionError(f"unknown heavy verb {args.heavy_verb!r}")


def add_parser(sub) -> None:
    """Register `heavy` and its verbs on the top-level subparsers."""
    heavy_parser = sub.add_parser(
        "heavy",
        help="regenerate the lane's portable cost summary from the run ledger",
        description="Regenerate the portable per-key cost summary from the "
        "raw run ledger. Stdout always carries the JSON document; --out "
        "writes the same bytes for the operator to commit.",
    )
    verbs = heavy_parser.add_subparsers(dest="heavy_verb", required=True)
    rollup = verbs.add_parser(
        "rollup",
        help="regenerate the portable per-key cost summary",
    )
    rollup.add_argument(
        "--out",
        default=None,
        help="additionally write the same JSON document to PATH",
    )
    heavy_parser.set_defaults(func=cmd_heavy)
