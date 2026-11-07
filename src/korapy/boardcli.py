"""`korapy board sweep`: bring every card back in step with its issue.

Runs the board step's own Status sync and missing-issue adopt to
completion, without the per-pass caps: ``--plan`` (the default) lists
each card, the issue state and the move, and changes nothing;
``--apply`` writes. REPO names ``[[repo]]`` entries (or their tracker
spelling); with none, every board entry is swept. Exit codes: 0
nothing to do or applied, 1 drift found in plan (or only refused
reads on apply), 3 tracker unreachable, 4 usage or configuration
error.
"""

import argparse
import functools
import os
import sqlite3
import sys
import time

from korapy import board, boardsweep, boardstatus, settings, sync
from korapy import tracker as iface
from korapy import trackers
from korapy import syncli

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4


def _err(text, err):
    print(f"korapy board sweep: {text}", file=err)


def _resolve_names(root, names, *, git):
    """Entry names to sweep, in order, or (None, problem).

    Accepts ``[[repo]]`` entry names and their tracker spellings (the
    doctor names tracker repos); unknown spellings are a usage error.
    """
    try:
        wanted = settings.repo_names(root)
    except settings.SettingsError as problem:
        return None, str(problem)
    if not wanted:
        return None, f"{root}: no [[repo]] entries to sweep"
    if not names:
        return list(wanted), None
    try:
        reverse = syncli.short_to_tracker(root, git)
    except ValueError:
        # An entry the tracker spelling cannot resolve (no explicit
        # repo, no clone origin) degrades to entry names only; the
        # entry itself still reports its own usage error below.
        reverse = {}
    by_tracker = {tracker: name for name, tracker in reverse.items()}
    selected = []
    for name in names:
        if name in wanted:
            entry = name
        elif name in by_tracker:
            entry = by_tracker[name]
        else:
            return None, f"unknown repo {name!r} (not a [[repo]] entry in {root})"
        if entry not in selected:
            selected.append(entry)
    return selected, None


def _tracker_for(entry, tracker_factory):
    """The entry's board tracker, or None with the reason it cannot sweep."""
    if entry.variant != "board" or entry.board is None:
        return None, (
            f"[[repo]] {entry.name}: tracker.variant is {entry.variant!r}:"
            " the sweep needs a board entry with a [repo.board] table"
        )
    entry_tracker = tracker_factory(entry)
    if not trackers.is_board_tracker(entry_tracker):
        return None, (
            f"[[repo]] {entry.name}: the tracker for {entry.tracker_repo} has no board reader"
        )
    return entry_tracker, None


def _plan_repo(entry, entry_tracker, *, store, now, pulls_max_calls):
    """List the repo's drift moves without writing.

    Returns ``(moves, notes, blocked, kind)``: ``blocked`` is the
    outage or rejection line when the sweep cannot run for this repo
    at all (``moves`` and ``notes`` are empty then), and ``kind`` is
    ``"outage"``, ``"refused"`` or ``""``.
    """
    repo = entry.tracker_repo
    try:
        schema = entry_tracker.board_schema()
    except iface.TrackerRejected as err:
        return (), (), f"{repo}: {err}", "refused"
    except (iface.TrackerError, ValueError) as err:
        return (), (), f"{repo}: {err}", "outage"
    cards = []
    after = None
    try:
        while True:
            page, resume = entry_tracker.poll(schema, after=after, max_pages=entry.max_pages)
            cards.extend(page)
            if resume is None:
                break
            after = resume
    except iface.TrackerRejected as err:
        return (), (), f"{repo}: {err}", "refused"
    except iface.TrackerError as err:
        return (), (), f"{repo}: {err}", "outage"
    # NOTE: the store reads below raise board.StoreError on corrupt
    # rows; sweep_root turns those into the repo's usage error.
    setup = sync._status_setup(entry_tracker, schema, uncapped=True)
    normed = boardsweep.poll_cards(cards)
    present = {(card.repo, card.number) for card in normed}
    claims = set()
    records = {}
    for card in normed:
        key = (card.repo, card.number)
        if store.claim_of(card.repo, card.number) is not None:
            claims.add(key)
        records[key] = store.status_set_of(card.repo, card.number)
    open_cards = boardsweep.store_cards(store.items(repo=repo, state="open"))
    missing = boardsweep.missing_moves(open_cards, present=present, now=now)
    if setup is None:
        return tuple(missing), (), "", ""
    names = boardstatus.option_names(setup[3].options)
    ids = dict(setup[2])
    try:
        pulls, pulls_error = sync._resolve_status_pulls(
            entry_tracker, repo, max_calls=sync._pulls_budget(pulls_max_calls)
        )
    except iface.TrackerRateLimited as err:
        pulls, pulls_error = (
            None,
            (f"board pulls rate limited, retry after {err.retry_after:g}s ({err})"),
        )
    except iface.TrackerError as err:
        pulls, pulls_error = None, str(err)
    moves, notes = boardsweep.status_moves(
        normed,
        names=names,
        ids=ids,
        claims=claims,
        records=records,
        pulls=pulls,
        pulls_error=pulls_error,
        now=now,
    )
    return tuple(moves) + tuple(missing), tuple(notes), "", ""


def _apply_repo(entry, entry_tracker, *, store, out, err, now, pulls_max_calls):
    """Sweep one repo to completion. Returns ``(applied, problem)``.

    Runs the board step uncapped until a round fixes nothing: the
    recorded drift flags reconcile every round, so the last round
    proves the repo clean. ``applied`` counts the cards fixed;
    ``problem`` is ``"outage"``, ``"refused"``, ``"failed"`` (only
    per-card failures, retried next pass) or ``""`` after the printed
    lines.
    """
    repo = entry.tracker_repo
    try:
        schema = entry_tracker.board_schema()
    except iface.TrackerRejected as problem:
        _err(f"{repo}: {problem}", err)
        return 0, "refused"
    except (iface.TrackerError, ValueError) as problem:
        _err(f"{repo}: {problem}", err)
        return 0, "outage"
    applied = 0
    problem = ""
    failed = False
    seen_lines: set = set()
    while True:
        # NOTE: row failures land in the result lines, never raise; a
        # store error here escapes to sweep_root, which counts usage.
        result = sync.sync_board_repo(
            store,
            entry_tracker,
            schema,
            repo,
            operators=list(entry.operators),
            now=now,
            max_pages=entry.max_pages,
            pulls_max_calls=pulls_max_calls,
            uncapped=True,
        )
        for line in result.errors:
            if line not in seen_lines:
                seen_lines.add(line)
                _err(line, err)
        for line in result.refused + result.adopt_errors + result.status_errors:
            if line not in seen_lines:
                seen_lines.add(line)
                _err(line, err)
        for line in result.store_errors:
            if line not in seen_lines:
                seen_lines.add(line)
                _err(line, err)
        for ref in result.adopted:
            out.write(f"added {ref} to the board\n")
        for ref in result.status_written:
            out.write(f"set Status on {ref}\n")
        applied += len(result.adopted) + len(result.status_written)
        failed = failed or bool(result.adopt_errors or result.status_errors)
        if result.errors or result.refused or result.store_errors:
            problem = "outage" if (result.errors or result.store_errors) else "refused"
            break
        if not result.adopted and not result.status_written:
            break
    if not problem and failed:
        problem = "failed"
    return applied, problem


def sweep_root(
    root,
    names,
    *,
    entry_factory,
    tracker_factory,
    open_store,
    out,
    err,
    now,
    plan,
    pulls_max_calls=None,
    git=None,
) -> int:
    """Sweep every named board entry. All seams injected; exit codes follow the module docstring.

    ``entry_factory`` maps an entry name to its plain-data
    ``SetupEntry`` (``ValueError`` names the entry's usage error),
    ``tracker_factory`` builds its board tracker, and ``open_store``
    opens the board store once for the whole run. Every named entry is
    attempted, in entry order; a per-entry failure of any kind is
    recorded while the loop continues with the next entry. A usage
    error anywhere exits 4; an outage with nothing applied exits 3;
    only per-card failures with nothing applied, a refused read with
    nothing applied, or drift in plan mode exits 1.
    """
    selected, problem = _resolve_names(root, names, git=git)
    if problem is not None:
        _err(problem, err)
        return USAGE
    try:
        store = open_store()
    except board.StoreError as exc:
        _err(str(exc), err)
        return USAGE
    usage = False
    outage = False
    refused = False
    failed = False
    drifted = False
    applied_total = 0
    with store:
        for name in selected:
            try:
                entry = entry_factory(name)
            except ValueError as exc:
                _err(str(exc), err)
                usage = True
                continue
            entry_tracker, reason = _tracker_for(entry, tracker_factory)
            if reason is not None:
                _err(reason, err)
                usage = True
                continue
            try:
                if plan:
                    moves, notes, blocked, kind = _plan_repo(
                        entry,
                        entry_tracker,
                        store=store,
                        now=now,
                        pulls_max_calls=pulls_max_calls,
                    )
                    if blocked:
                        _err(blocked, err)
                        if kind == "refused":
                            refused = True
                        else:
                            outage = True
                        continue
                    for move in moves:
                        out.write(boardsweep.format_move(move) + "\n")
                    for note in notes:
                        out.write(note + "\n")
                    if moves:
                        drifted = True
                else:
                    applied, problem = _apply_repo(
                        entry,
                        entry_tracker,
                        store=store,
                        out=out,
                        err=err,
                        now=now,
                        pulls_max_calls=pulls_max_calls,
                    )
                    applied_total += applied
                    if problem == "outage":
                        outage = True
                    elif problem == "refused":
                        refused = True
                    elif problem == "failed":
                        failed = True
            except (board.StoreError, sqlite3.Error) as exc:
                _err(f"{entry.tracker_repo}: {exc}", err)
                usage = True
                continue
    if usage:
        return USAGE
    if outage and applied_total == 0 and not drifted:
        return UNREACHABLE
    if drifted or refused or (failed and applied_total == 0):
        return NOTHING
    return OK


def cmd_sweep(args: argparse.Namespace) -> int:
    root = settings.find_root(os.getcwd())
    if root is None:
        _err("no korapy.toml above here", sys.stderr)
        return USAGE
    return sweep_root(
        root,
        args.repo,
        entry_factory=functools.partial(trackers.setup_entry, root, git=syncli._git),
        tracker_factory=trackers.setup_tracker,
        open_store=board.open_store,
        out=sys.stdout,
        err=sys.stderr,
        now=time.time(),
        plan=not args.apply,
        git=syncli._git,
    )


def add_parser(sub) -> None:
    """Register `board` on the top-level subparsers."""
    parser = sub.add_parser("board", help="the project board keeps and fixes")
    verbs = parser.add_subparsers(dest="board_verb", required=True)
    p = verbs.add_parser(
        "sweep",
        help="bring every card back in step with its issue, to completion",
    )
    p.add_argument(
        "repo",
        nargs="*",
        default=[],
        help="the [[repo]] entries to sweep (default: every board entry)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--plan",
        action="store_true",
        help="list each card, the issue state and the move; changes nothing (default)",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="write the moves until the board is clean",
    )
    p.set_defaults(func=cmd_sweep)
