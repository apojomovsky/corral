"""`corral github setup`: bootstrap the board labels on the tracker.

Runs the idempotent ensure operation on every named ``[[repo]]`` entry
(the same operation ``new --create-remote`` runs after creating the
repository): missing labels are created, existing labels are left
byte-identical, refused ones are reported with a fix line. Exit codes:
0 a label was created and none was refused, 1 a label was refused or
everything was already kept, 3 a tracker was unreachable with nothing
created, 4 usage or configuration error.
"""

import argparse
import os
import sys

from corral import settings, syncli
from corral import tracker as iface

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4


def _err(text, err):
    print(f"corral github setup: {text}", file=err)


def setup_root(root, names, *, tracker_factory, git, out, err) -> int:
    """Ensure the bootstrap labels on every named repo entry and report.

    ``tracker_factory`` maps a tracker repo (``owner/name``) to its
    tracker, built by the one factory (labels or board); ``git`` is the
    origin-lookup seam `corral sync` resolves tracker repos with. All
    seams injected; exit codes follow the module docstring: any refused
    label is 1 even when another repo created labels, a pass that
    created something and refused nothing is 0, a pass that created
    nothing but failed somewhere is 3, and a pass that created nothing
    and failed nowhere is 1.
    """
    try:
        wanted = settings.repo_names(root)
    except settings.SettingsError as problem:
        _err(str(problem), err)
        return USAGE
    if not wanted:
        _err(f"{root}: no [[repo]] entries to set up", err)
        return USAGE
    unknown = [name for name in names if name not in wanted]
    if unknown:
        _err(f"unknown repo {unknown[0]!r} (not a [[repo]] entry in {root})", err)
        return USAGE
    entries, problem = syncli.repo_operators(root, names or wanted, git)
    if problem is not None:
        _err(problem, err)
        return USAGE
    made = 0
    failed = 0
    refused = 0
    for name in names or wanted:
        tracker_repo, _ = entries[name]
        try:
            repo_tracker = tracker_factory(tracker_repo)
        except KeyError:
            _err(f"[[repo]] {name}: no tracker for {tracker_repo}", err)
            return USAGE
        try:
            report = repo_tracker.ensure_labels(tracker_repo)
        except iface.TrackerError as problem:
            _err(f"{tracker_repo}: {problem}", err)
            failed += 1
            continue
        out.write(iface.render_ensure(tracker_repo, report))
        made += len(report.created)
        refused += len(report.refused)
    if refused:
        return NOTHING
    if made:
        return OK
    if failed:
        return UNREACHABLE
    return NOTHING


def cmd_setup(args: argparse.Namespace) -> int:
    from corral import trackers

    root = settings.find_root(os.getcwd())
    if root is None:
        _err("no corral.toml above here", sys.stderr)
        return USAGE
    try:
        wanted = settings.repo_names(root)
    except settings.SettingsError as problem:
        _err(str(problem), sys.stderr)
        return USAGE
    mapping, problem = trackers.production_trackers(root, args.repo or wanted, git=syncli._git)
    if problem is not None:
        _err(problem, sys.stderr)
        return USAGE
    by_repo = {}
    for entry in mapping.values():
        by_repo.setdefault(entry.tracker_repo, entry.tracker)
    return setup_root(
        root,
        args.repo,
        tracker_factory=by_repo.__getitem__,
        git=syncli._git,
        out=sys.stdout,
        err=sys.stderr,
    )


def add_parser(sub) -> None:
    """Register `github` on the top-level subparsers."""
    parser = sub.add_parser("github", help="tracker-side setup for the board repositories")
    verbs = parser.add_subparsers(dest="github_verb", required=True)
    p = verbs.add_parser("setup", help="create the missing board labels on the tracker")
    p.add_argument(
        "repo",
        nargs="*",
        default=[],
        help="the [[repo]] entries to set up (default: every entry)",
    )
    p.set_defaults(func=cmd_setup)
