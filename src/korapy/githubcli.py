"""`korapy github setup`: bootstrap labels, boards and their mapping.

Runs the idempotent ensure operations on every named ``[[repo]]``
entry: the label bootstrap always runs, and the board ensure runs when
the entry's ``tracker.variant`` is ``board`` (or when the operator
passes ``--board`` for a labels entry to pre-provision before a flip).
After a board ensure, the provisioned identifiers are written into the
ops root's ``korapy.toml`` through ``init``'s TOML machinery, the same
way ``init`` writes that file. ``--populate`` adopts the tracker
repo's open issues onto a provisioned board, and ``--plan`` prints
what setup would do and changes nothing.

Exit codes: 0 something was created (or would be, under ``--plan``)
with nothing refused and nothing failed, 1 something was refused or
everything was already kept, 3 any entry unreachable (even when
another entry created something), 4 usage or configuration error.
"""

import argparse
import functools
import os
import sys

from korapy import init, settings, syncli
from korapy import tracker as iface

OK, NOTHING, UNREACHABLE, USAGE = 0, 1, 3, 4


def _err(text, err):
    print(f"korapy github setup: {text}", file=err)


def _read_text(path):
    """The file's text, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def setup_root(
    root,
    names,
    *,
    entry_factory,
    tracker_factory,
    provisioner_factory,
    out,
    err,
    board=False,
    populate=False,
    plan=False,
    areas=(),
    pools=(),
    board_owner=None,
    board_title=None,
) -> int:
    """Ensure labels, boards and their mapping on every named repo entry.

    ``entry_factory`` maps an entry name to its plain-data
    ``SetupEntry``, ``tracker_factory`` maps the entry to its label-half
    tracker, and ``provisioner_factory`` builds the board provisioner;
    all seams injected. Every named entry is attempted, in entry order,
    and a per-entry failure of any kind is recorded while the loop
    continues with the next entry. A labels entry without ``board``
    runs its label half only, byte-identical to the label bootstrap.
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
    made = 0
    failed = 0
    refused = 0
    usage = 0
    for name in names or wanted:
        try:
            resolved = entry_factory(name)
        except ValueError as problem:
            _err(f"[[repo]] {name}: {problem}", err)
            usage += 1
            continue
        outcome = _setup_entry(
            root,
            resolved,
            tracker_factory=tracker_factory,
            provisioner_factory=provisioner_factory,
            out=out,
            err=err,
            board=board,
            populate=populate,
            plan=plan,
            areas=areas,
            pools=pools,
            board_owner=board_owner,
            board_title=board_title,
        )
        made += outcome[0]
        failed += outcome[1]
        refused += outcome[2]
        usage += outcome[3]
    if usage:
        return USAGE
    if failed:
        return UNREACHABLE
    if refused:
        return NOTHING
    if made:
        return OK
    return NOTHING


def _setup_entry(
    root,
    resolved,
    *,
    tracker_factory,
    provisioner_factory,
    out,
    err,
    board,
    populate,
    plan,
    areas,
    pools,
    board_owner,
    board_title,
):
    """One entry's labels, board, mapping and populate. Returns ``(made, failed, refused, usage)``."""
    try:
        repo_tracker = tracker_factory(resolved)
    except ValueError as problem:
        _err(f"[[repo]] {resolved.name}: {problem}", err)
        return (0, 0, 0, 1)
    made, failed, refused, usage = _setup_labels(resolved, repo_tracker, out, err, plan=plan)
    if failed or usage:
        return (made, failed, refused, usage)
    if resolved.variant == "board" or board:
        return _setup_board(
            root,
            resolved,
            provisioner_factory,
            out,
            err,
            plan=plan,
            populate=populate,
            areas=areas,
            pools=pools,
            board_owner=board_owner,
            board_title=board_title,
            carried=(made, failed, refused, usage),
        )
    if not populate:
        return (made, failed, refused, usage)
    provisioner = _board_provisioner(resolved, provisioner_factory, err)
    if provisioner is None:
        return (made, failed, refused, usage + 1)
    return _setup_populate(
        resolved,
        provisioner,
        None,
        out,
        err,
        plan=plan,
        board_ran=False,
        carried=(made, failed, refused, usage),
    )


def _setup_labels(resolved, repo_tracker, out, err, *, plan):
    """One entry's label half: ensure, or list what is missing under ``--plan``."""
    repo = resolved.tracker_repo
    try:
        if plan:
            missing = repo_tracker.missing_labels(repo)
            for name in missing:
                out.write(f"would create label {name} on {repo}\n")
            return (len(missing), 0, 0, 0)
        report = repo_tracker.ensure_labels(repo)
    except iface.TrackerRejected as problem:
        _err(f"{repo}: {problem}", err)
        return (0, 0, 1, 0)
    except iface.TrackerError as problem:
        _err(f"{repo}: {problem}", err)
        return (0, 1, 0, 0)
    out.write(iface.render_ensure(repo, report))
    return (len(report.created), 0, len(report.refused), 0)


def _board_provisioner(resolved, provisioner_factory, err):
    """The entry's provisioner, or None with the usage error printed."""
    try:
        provisioner = provisioner_factory()
    except ValueError as problem:
        _err(f"[[repo]] {resolved.name}: {problem}", err)
        return None
    ensure = getattr(provisioner, "ensure_board", None)
    if not callable(ensure):
        _err(
            f"[[repo]] {resolved.name}: this tracker does not provision boards",
            err,
        )
        return None
    return provisioner


def _setup_board(
    root,
    resolved,
    provisioner_factory,
    out,
    err,
    *,
    plan,
    populate,
    areas,
    pools,
    board_owner,
    board_title,
    carried,
):
    """One entry's board half: ensure (or preview), mapping write, populate."""
    made, failed, refused, usage = carried
    provisioner = _board_provisioner(resolved, provisioner_factory, err)
    if provisioner is None:
        return (made, failed, refused, usage + 1)
    try:
        spec = iface.effective_spec(
            variant=resolved.variant,
            board=resolved.board,
            repo=resolved.tracker_repo,
            title=board_title,
            owner=board_owner,
            areas=areas,
            pools=pools,
        )
    except ValueError as problem:
        _err(f"[[repo]] {resolved.name}: {problem}", err)
        return (made, failed, refused, usage + 1)
    try:
        if plan:
            preview = getattr(provisioner, "dry_run", None)
            if not callable(preview):
                _err(
                    f"[[repo]] {resolved.name}: this tracker does not preview boards",
                    err,
                )
                return (made, failed, refused, usage + 1)
            provision = preview(spec)
        else:
            provision = provisioner.ensure_board(spec)
    except ValueError as problem:
        _err(f"[[repo]] {resolved.name}: {problem}", err)
        return (made, failed, refused, usage + 1)
    except iface.TrackerError as problem:
        _err(f"{resolved.tracker_repo}: {problem}", err)
        return (made, failed + 1, refused, usage)
    if plan:
        for name in provision.created:
            out.write(f"would create {name} on {resolved.tracker_repo}\n")
        made += len(provision.created)
    else:
        out.write(iface.render_provision(resolved.tracker_repo, provision))
        made += len(provision.created)
    refused += len(provision.refused)
    wrote = _setup_mapping(root, resolved, spec, provision, out, err, plan=plan)
    if wrote is None:
        return (made, failed, refused, usage + 1)
    made += 1 if wrote else 0
    _next_step(resolved, out)
    if not populate:
        return (made, failed, refused, usage)
    return _setup_populate(
        resolved,
        provisioner,
        provision,
        out,
        err,
        plan=plan,
        board_ran=True,
        carried=(made, failed, refused, usage),
    )


def _setup_mapping(root, resolved, spec, provision, out, err, *, plan):
    """Write the provisioned mapping into the ops root's file: True/False, None on refusal.

    Under ``--plan`` the write is shown as the diff and no file byte
    changes. A provision with no address yet (a bootstrap plan against
    a board that does not exist) notes the deferred table instead: its
    number is unknowable until the board exists.
    """
    toml_target = os.path.join(root, settings.PROJECT_FILE)
    if plan and provision.number is None and not provision.project_id:
        out.write(f"would write the [repo.board] table for {resolved.name} once the board exists\n")
        return False
    try:
        new_text, noop = init.board_table_change(toml_target, resolved.name, spec, provision)
    except init.InitError as problem:
        _err(str(problem), err)
        return None
    if plan:
        if not noop:
            current = _read_text(toml_target)
            out.write(init.plan_diff(current, new_text, os.path.basename(toml_target)))
            return True
        return False
    if noop:
        return False
    try:
        with open(toml_target, "w", encoding="utf-8") as handle:
            handle.write(new_text)
    except OSError as problem:
        _err(f"{toml_target}: cannot write the board table: {problem}", err)
        return None
    out.write(f"wrote [repo.board] for {resolved.name}\n")
    return True


def _next_step(resolved, out):
    """The operator's next step after one entry's board half converged."""
    if resolved.variant == "board":
        out.write("next: korapy doctor\n")
    else:
        out.write(
            "next: drain the outbox, backfill the board fields from the labels,"
            ' set tracker.variant = "board", resync, then korapy doctor\n'
        )


def _setup_populate(resolved, provisioner, provision, out, err, *, plan, board_ran, carried):
    """Adopt the entry's open issues onto its board. Returns the running totals.

    Uses the fresh provision's node id when the board half ran, else
    the table's node id; a table carrying only the per-owner number
    cannot populate without an ensure, and neither can a table-less
    labels entry without ``--board``.
    """
    made, failed, refused, usage = carried
    project_id = None
    if board_ran:
        project_id = provision.project_id if provision is not None else None
    elif resolved.board is not None:
        project_id = getattr(resolved.board, "project_id", None)
    if not project_id:
        if plan:
            out.write(f"would populate {resolved.tracker_repo} once the board exists\n")
            return (made, failed, refused, usage)
        _err(
            f"[[repo]] {resolved.name}: cannot populate {resolved.tracker_repo}"
            " with no board node id: pass --board to ensure the board first",
            err,
        )
        return (made, failed, refused, usage + 1)
    populate_call = getattr(provisioner, "populate" if not plan else "preview_populate", None)
    if not callable(populate_call):
        _err(
            f"[[repo]] {resolved.name}: this tracker does not populate boards",
            err,
        )
        return (made, failed, refused, usage + 1)
    try:
        report = populate_call(project_id, resolved.tracker_repo)
    except ValueError as problem:
        _err(f"[[repo]] {resolved.name}: {problem}", err)
        return (made, failed, refused, usage + 1)
    except iface.TrackerError as problem:
        _err(f"{resolved.tracker_repo}: {problem}", err)
        return (made, failed + 1, refused, usage)
    if plan:
        for name in report.added:
            out.write(f"would add {name} to the board\n")
        made += len(report.added)
    else:
        out.write(iface.render_populate(resolved.tracker_repo, report))
        made += len(report.added)
    refused += len(report.refused)
    return (made, failed, refused, usage)


def cmd_setup(args: argparse.Namespace) -> int:
    from korapy import trackers

    root = settings.find_root(os.getcwd())
    if root is None:
        _err("no korapy.toml above here", sys.stderr)
        return USAGE
    return setup_root(
        root,
        args.repo,
        entry_factory=functools.partial(trackers.setup_entry, root, git=syncli._git),
        tracker_factory=trackers.setup_tracker,
        provisioner_factory=trackers.production_board_provisioner,
        out=sys.stdout,
        err=sys.stderr,
        board=args.board,
        populate=args.populate,
        plan=args.plan,
        areas=args.area or (),
        pools=args.pool or (),
        board_owner=args.board_owner,
        board_title=args.board_title,
    )


def add_parser(sub) -> None:
    """Register `github` on the top-level subparsers."""
    parser = sub.add_parser("github", help="tracker-side setup for the board repositories")
    verbs = parser.add_subparsers(dest="github_verb", required=True)
    p = verbs.add_parser(
        "setup",
        help=(
            "create the missing board labels on the tracker, provision the board"
            " for board entries, and write the mapping"
        ),
    )
    p.add_argument(
        "repo",
        nargs="*",
        default=[],
        help="the [[repo]] entries to set up (default: every entry)",
    )
    p.add_argument(
        "--board",
        action="store_true",
        help="provision the board too, for a labels entry pre-provisioning before a flip",
    )
    p.add_argument(
        "--populate",
        action="store_true",
        help="add the tracker repo's open issues missing from the board",
    )
    p.add_argument(
        "--plan",
        action="store_true",
        help="print what setup would do and change nothing",
    )
    p.add_argument(
        "--area",
        action="append",
        default=None,
        help="declare an Area option the provision creates the field with (repeatable)",
    )
    p.add_argument(
        "--pool",
        action="append",
        default=None,
        help="declare a Pool option the provision creates the field with (repeatable)",
    )
    p.add_argument(
        "--board-owner",
        default=None,
        help="the board owner login (default: the config's owner, else the tracker repo's)",
    )
    p.add_argument(
        "--board-title",
        default=None,
        help='the board title (default: "<repo> board" from the tracker repo name)',
    )
    p.set_defaults(func=cmd_setup)
