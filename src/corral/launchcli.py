"""`corral launch|resume|stop|tail`: the agent verbs over `corral.launch`.

Exit codes: 0 ok; 1 nothing to stop (no live agent of the ticket at all),
or a resume refused while the ticket carries the hold label (a human
removes the label); 2 already live or race lost, or, for stop, a live
unit this ticket's log does not prove ours was left running (it is named
on stderr and never stopped); 3 systemd or a host unreachable; 4 usage
or configuration error; 75 no free slot.

The production seams are built in one place, `production_seams`, and passed
to `add_parser` as a required argument. A test passes fakes instead.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import NamedTuple

from corral import (
    admission,
    disk,
    hold,
    launch,
    models,
    paths,
    projects,
    prompts,
    runtimes,
    settings,
    units,
)
from corral import worktrees


def _production_board(repo, number):
    """The board store's item for the ticket, or None. Only the CLI builds this."""
    import time

    from corral import board

    try:
        with board.open_store() as store:
            found, _ = store.item(repo, number, now=time.time())
            return found
    except board.StoreError:
        return None


OK, NOTHING, RACE, USAGE = 0, 1, 2, 4


class Seams(NamedTuple):
    """The subprocess seams the verbs use: a listing, a start, a stop, and the board."""

    list_fn: Callable[[str], list]
    run: Callable[..., object]
    stop_fn: Callable[[str], object]
    #: The board store's item for `(repo, number)`, or None. Read-only; tests pass a fake.
    board_fn: Callable | None = None
    #: The live label read `labels_fn(kind, repo, number) -> list[str]`,
    #: where `kind` is `"issue"` or `"pr"` and `repo` is the tracker repo.
    #: None means there is no way to read labels: the resume refuses
    #: fail-closed, never as "no labels". Tests pass a fake.
    labels_fn: Callable | None = None
    #: `usage(path) -> (total, used, free)` for the disk gate. None means
    #: the production reader. Tests pass a fake.
    usage: Callable | None = None


def production_seams() -> Seams:
    """The real seams. Only the CLI builds them; tests never call this."""
    from corral.github import tracker as ghtracker
    from corral.github.gh import paced_run

    live = ghtracker.GithubTracker(paced_run)

    def labels_fn(kind, repo, number):
        # A PR is an item in the same issues feed, so both kinds read one way.
        return list(live.item(repo, number).labels)

    return Seams(
        list_fn=lambda prefix: units.list_units(prefix, run=units.default_run),
        run=units.default_run,
        stop_fn=units.systemctl_stop,
        labels_fn=labels_fn,
        usage=disk.production_usage,
    )


#: Errors the verbs report as one line on stderr. Anything else is a bug.
KNOWN = (
    admission.LimitsError,
    launch.HeldError,
    launch.LaunchError,
    models.Exhausted,
    projects.ProjectsError,
    prompts.PromptsError,
    units.UnitsError,
    worktrees.WorktreeError,
    runtimes.UnknownRuntime,
    settings.SettingsError,
    paths.PathError,
)


def _exit_code(err):
    if isinstance(
        err,
        (
            admission.LimitsError,
            launch.LaunchError,
            models.Exhausted,
            projects.ProjectsError,
            prompts.PromptsError,
            units.UnitsError,
        ),
    ):
        return err.exit_code
    return USAGE


def _root(args):
    root = args.root or settings.find_root(os.getcwd())
    if not root:
        raise launch.LaunchError(
            f"no {settings.PROJECT_FILE} above the working directory: pass --root"
        )
    return root


def _target(args, flags=None):
    return launch.target(_root(args), args.repo, flags=flags)


def _read_prompt(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        raise launch.LaunchError(f"cannot read the prompt file: {err.strerror}") from None
    if not text.strip():
        raise launch.LaunchError(f"the prompt file {path} is empty")
    return text


def _read_config(path):
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as err:
        raise launch.LaunchError(f"cannot read the runtime config {path}: {err}") from None
    if not isinstance(config, dict):
        raise launch.LaunchError(f"the runtime config {path} must hold a JSON object")
    return config


def _board_title(args, tgt, seams):
    """The ticket title: the board store's item wins, else `--title`.

    An item that is missing, unreadable, or untitled falls through to the
    flag; with neither, the error names the flag.
    """
    board_fn = seams.board_fn or _production_board
    found = board_fn(tgt.repo, args.number)
    title = (found.title if found is not None else "") or args.title
    if not title:
        raise launch.LaunchError(
            f"role {args.role} needs a title: no item {tgt.repo}#{args.number}"
            " in the board store and --title was not given"
        )
    if not isinstance(title, str):
        raise launch.LaunchError("title must be a string")
    return title


def _overlay_text(tgt):
    """The ops root's `agents.overlay.md`, or "" when it is absent."""
    if not tgt.root:
        return ""
    path = os.path.join(tgt.root, "agents.overlay.md")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as err:
        raise launch.LaunchError(f"cannot read the agent overlay: {err}") from None


def _role_prompt(args, tgt, seams):
    """A `(workdir, branch) -> text` renderer for the role prompt, and its title.

    `launch` calls the renderer with the worktree and branch it just made,
    so the prompt names the values the agent actually gets.
    """
    title = _board_title(args, tgt, seams)
    agents_md = os.path.join(tgt.clone, "AGENTS.md")
    base = {
        "issue": str(args.number),
        "title": title,
        "checks.test": tgt.resolved["checks.test"],
        "overlay": _overlay_text(tgt),
        "project_agents_md": agents_md if os.path.isfile(agents_md) else "AGENTS.md (none found)",
    }

    # Validate every placeholder and the override before launch makes a
    # worktree: a bad override must not leave a stray branch behind.
    try:
        prompts.render(args.role, {**base, "worktree": "-", "branch": "-"}, ops_root=tgt.root)
    except prompts.PromptsError as err:
        raise launch.LaunchError(str(err)) from None

    def render_prompt(workdir, branch):
        try:
            return prompts.render(
                args.role, {**base, "worktree": workdir, "branch": branch}, ops_root=tgt.root
            )
        except prompts.PromptsError as err:
            raise launch.LaunchError(str(err)) from None

    return render_prompt, title


def cmd_launch(args, seams: Seams) -> int:
    if bool(args.prompt_file) == bool(args.role):
        raise launch.LaunchError("pass exactly one of --role and --prompt-file")
    config = _read_config(args.runtime_config)
    flags = {"agents.runtime": args.runtime} if args.runtime else None
    tgt = _target(args, flags)
    if args.role:
        prompt, title = _role_prompt(args, tgt, seams)
    else:
        prompt, title = _read_prompt(args.prompt_file), args.title
    unit, workdir = launch.launch(
        tgt,
        args.number,
        prompt,
        title=title,
        model=args.model,
        runtime_config=config,
        role=args.role,
        list_fn=seams.list_fn,
        run=seams.run,
        usage=seams.usage or disk.production_usage,
    )
    print(f"launched {unit} in {workdir}")
    return OK


def cmd_resume(args, seams: Seams) -> int:
    from corral import syncli

    tgt = _target(args)
    tracker_repo, problem = syncli._tracker_repo(tgt.root, tgt.repo, tgt.resolved, worktrees.git)
    if problem is not None:
        raise launch.LaunchError(problem)
    labels_fn = seams.labels_fn

    def read(kind, repo, number):
        if labels_fn is None:
            raise RuntimeError("no label read")
        return labels_fn(kind, repo, number)

    hold_label = tgt.resolved["board.hold_label"]

    def hold_reason_fn(t, n):
        # Linked PRs are not looked up in this ticket.
        return hold.reason(read, (tracker_repo, n), (), hold_label=hold_label)

    unit, workdir = launch.resume(
        tgt,
        args.number,
        args.message,
        hold_reason_fn=hold_reason_fn,
        role=args.role,
        model=args.model,
        list_fn=seams.list_fn,
        run=seams.run,
        usage=seams.usage or disk.production_usage,
    )
    print(f"resumed as {unit} in {workdir}")
    return OK


def cmd_stop(args, seams: Seams) -> int:
    tgt = _target(args)
    stopped = launch.stop(tgt, args.number, list_fn=seams.list_fn, stop_fn=seams.stop_fn)
    left = launch.unproven(tgt, args.number, list_fn=seams.list_fn)
    for unit in stopped:
        print(f"stopped {unit}")
    if left:
        print(
            f"corral: live but not proven ours, not stopped: {', '.join(left)}",
            file=sys.stderr,
        )
        return RACE
    if not stopped:
        print("nothing to stop")
        return NOTHING
    return OK


def cmd_tail(args, seams: Seams) -> int:
    if args.last is not None and args.last < 1:
        raise launch.LaunchError("--last must be 1 or more")
    tgt = _target(args)
    launch.tail(tgt, args.number, last=args.last, role=args.role)
    return OK


def _verb(fn, seams: Seams):
    """The verb as `args -> exit code`, one stderr line and the code for a known error."""

    def run(args):
        try:
            return fn(args, seams)
        except KNOWN as err:
            print(f"corral: {err}", file=sys.stderr)
            return _exit_code(err)

    return run


def _common(p):
    p.add_argument("repo", help="the repository's name in corral.toml")
    p.add_argument("number", type=int, help="the ticket number")
    p.add_argument(
        "--root", default=None, help="the ops root (default: nearest corral.toml upward)"
    )


def add_parser(sub, seams: Seams) -> None:
    """Register launch, resume, stop and tail on the top-level subparsers."""
    p = sub.add_parser("launch", help="start an agent on a ticket in its own worktree")
    _common(p)
    p.add_argument("--prompt-file", default=None, help="the prompt the agent starts with")
    p.add_argument(
        "--role",
        default=None,
        choices=list(prompts.ROLES),
        help="render the built-in role prompt instead of reading --prompt-file",
    )
    p.add_argument("--title", default=None, help="the ticket title, to name a new branch")
    p.add_argument("--model", default=None, help="the model the runtime should use")
    p.add_argument("--runtime", default=None, help="the runtime (default: agents.runtime)")
    p.add_argument("--runtime-config", default=None, help="a JSON object file, the runtime config")
    p.set_defaults(func=_verb(cmd_launch, seams))

    p = sub.add_parser("resume", help="resume the ticket's newest session in a new unit")
    _common(p)
    p.add_argument("--message", required=True, help="what the resumed agent is told")
    p.add_argument("--model", default=None, help="the model the runtime should use")
    p.add_argument(
        "--role",
        default=prompts.DEFAULT_ROLE,
        choices=list(prompts.ROLES),
        help="which role's newest session to resume (default: %(default)s)",
    )
    p.set_defaults(func=_verb(cmd_resume, seams))

    p = sub.add_parser("stop", help="stop the ticket's live agents")
    _common(p)
    p.set_defaults(func=_verb(cmd_stop, seams))

    p = sub.add_parser("tail", help="show the ticket's agent activity")
    _common(p)
    p.add_argument("--last", type=int, default=None, help="only the last K lines")
    p.add_argument(
        "--role",
        default=prompts.DEFAULT_ROLE,
        choices=list(prompts.ROLES),
        help="which role's newest session to show (default: %(default)s)",
    )
    p.set_defaults(func=_verb(cmd_tail, seams))


def run_verb(argv: Sequence[str], seams: Seams) -> int:
    """Parse and run one verb with the given seams. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="corral")
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub, seams)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    return args.func(args)
