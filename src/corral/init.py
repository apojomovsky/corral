"""`corral init` for the inline layout: detect, render, write `corral.toml`.

Non-interactive: detected values are used, flags override them, and a
value that is neither detected nor flagged is exit 4 (`--test-cmd` is
the only one this layout cannot default). `--yes` is accepted and is a
no-op, reserved for the prompting ticket.

Exactly one file is ever written: `corral.toml` at the repository root.
`AGENTS.md`, `CLAUDE.md`, hooks and every other file are left alone.
"""

import argparse
import difflib
import json
import os
import re
import sys
from typing import NamedTuple

from corral import detect, gc, paths, runtimes, settings

OK, NOTHING, USAGE = 0, 1, 4


class InitError(ValueError):
    """A usage or configuration error (exit 4)."""


#: The `[[repo]]` entry keys always written, in file order.
TRIO = ("clone.path", "clone.default_branch", "checks.test")


class PlanInputs(NamedTuple):
    """Everything `render` needs: detection plus flag overrides."""

    name: str
    path: str
    default_branch: str
    test_command: str
    extra: tuple  # ((key, value), ...) active settings beyond the entry trio, sorted


def _coerce(key, raw):
    """The `--set key=value` value in the setting's type. Unknown keys
    and wrong types are exit 4 naming the key."""
    setting = settings.REGISTRY.get(key)
    if setting is None:
        raise InitError(f"unknown setting {key!r}: see `corral config show` for the list")
    if setting.type is bool:
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise InitError(f"--set {key}: {raw!r} is not true or false")
    if setting.type is int:
        if not re.fullmatch(r"-?[0-9]+", raw.strip()):
            raise InitError(f"--set {key}: {raw!r} is not an integer")
        return int(raw)
    return raw


def resolve_root(path, git):
    """The repository root holding `path`: symlinks resolved, else exit 4."""
    where = os.path.abspath(path)
    try:
        proc = git(["rev-parse", "--show-toplevel"], where)
    except OSError:
        raise InitError(f"not a git repository: {path}") from None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        raise InitError(f"not a git repository: {path}")
    root = os.path.realpath((getattr(proc, "stdout", "") or "").strip())
    if not root:
        raise InitError(f"not a git repository: {path}")
    here = os.path.realpath(where)
    if here != root and os.path.commonpath([here, root]) != root:
        raise InitError(f"{path} escapes its repository {root}")
    return root


def collect(path, *, git, test_cmd=None, runtime=None, cap=None, sets=()):
    """`(root, PlanInputs)` for the repository holding `path`.

    Flags win over detection. A missing test command, an unknown
    `--set` key, a mistyped `--set` value and an unknown `--runtime`
    are `InitError`.
    """
    settings._load_providers()
    if runtime is not None and runtime not in runtimes.NAMES:
        raise InitError(f"--runtime {runtime!r}: want one of {', '.join(runtimes.NAMES)}")
    root = resolve_root(path, git)
    overrides: dict[str, str | bool | int] = {}
    for raw in sets:
        key, sep, value = raw.partition("=")
        if not sep or not key:
            raise InitError(f"--set {raw!r}: want key=value")
        overrides[key] = _coerce(key, value)
    slug = detect.origin_slug(root, git)
    name = slug.split("/")[1] if slug else os.path.basename(root)
    branch = detect.default_branch(root, git)
    if branch is None:
        fallback = settings.REGISTRY["clone.default_branch"].default
        if not isinstance(fallback, str):
            raise InitError("clone.default_branch has no usable default")
        branch = fallback
    command = test_cmd if test_cmd is not None else detect.test_command(root)
    if command is None:
        raise InitError("no test command detected: pass --test-cmd CMD")
    trio = {
        "clone.path": ".",
        "clone.default_branch": branch,
        "checks.test": command,
    }
    for key in TRIO:
        if key in overrides:
            value = overrides.pop(key)
            if not isinstance(value, str):
                raise InitError(f"internal: {key} is not a str setting")
            trio[key] = value
    rest = {
        "agents.runtime": runtime,
        "agents.cap": cap,
    }
    rest.update(overrides)
    extra = tuple(
        sorted(
            (key, value)
            for key, value in rest.items()
            if value is not None and value != settings.REGISTRY[key].default
        )
    )
    return root, PlanInputs(
        name=name,
        path=trio["clone.path"],
        default_branch=trio["clone.default_branch"],
        test_command=trio["checks.test"],
        extra=extra,
    )


def _toml(value):
    """A registry default or active value as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def render(inputs):
    """The `corral.toml` text for `inputs`: the settings that differ
    from the defaults live in the single `[[repo]]` entry; every other
    registered setting is commented out with its default and one-line
    help, grouped by prefix, so the template never goes stale."""
    settings._load_providers()
    active = {
        "clone.path": inputs.path,
        "clone.default_branch": inputs.default_branch,
        "checks.test": inputs.test_command,
    }
    active.update(inputs.extra)
    lines = [
        "# corral.toml, written by `corral init`. Settings that differ",
        "# from the defaults live in [[repo]]; everything else is",
        "# commented out below with its default.",
        "[[repo]]",
        f"name = {_toml(inputs.name)}",
        "",
    ]
    groups: dict[str, list[str]] = {}
    for key in active:
        groups.setdefault(key.split(".")[0], []).append(key)
    for prefix in sorted(groups):
        lines.append(f"[repo.{prefix}]")
        for key in sorted(groups[prefix]):
            lines.append(f"{key.split('.', 1)[1]} = {_toml(active[key])}")
        lines.append("")
    commented = [key for key in settings.REGISTRY if key not in active]
    lines.append("# Defaults: uncomment to override.")
    groups = {}
    for key in commented:
        groups.setdefault(key.split(".")[0], []).append(key)
    for prefix in sorted(groups):
        lines.append("#")
        lines.append(f"# [{prefix}]")
        for key in sorted(groups[prefix]):
            setting = settings.REGISTRY[key]
            lines.append(f"# {key} = {_toml(setting.default)}  # {setting.help}")
    lines.append("")
    return "\n".join(lines)


def plan_diff(current, new):
    """The unified diff from `current` (None for a new file) to `new`."""
    old = [] if current is None else current.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old,
            new.splitlines(keepends=True),
            fromfile="a/corral.toml",
            tofile="b/corral.toml",
        )
    )


class Options(NamedTuple):
    """Parsed `corral init` flags."""

    path: str | None = None
    plan: bool = False
    yes: bool = False
    force: bool = False
    test_cmd: str | None = None
    runtime: str | None = None
    cap: int | None = None
    sets: tuple = ()


def run(opts, git=gc.git):
    """Run `init` with an injectable git seam. Returns the exit code."""
    try:
        root, inputs = collect(
            opts.path or os.getcwd(),
            git=git,
            test_cmd=opts.test_cmd,
            runtime=opts.runtime,
            cap=opts.cap,
            sets=opts.sets,
        )
    except InitError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE
    text = render(inputs)
    target = os.path.join(root, settings.PROJECT_FILE)
    try:
        with open(target, encoding="utf-8") as handle:
            current = handle.read()
    except FileNotFoundError:
        current = None
    except OSError as err:
        print(f"corral: cannot read {target}: {err}", file=sys.stderr)
        return USAGE
    if current == text:
        sys.stdout.write(f"{target} is up to date\n")
        return NOTHING
    if opts.plan:
        sys.stdout.write(plan_diff(current, text))
        return OK
    if current is not None and not opts.force:
        sys.stdout.write(plan_diff(current, text))
        print(
            f"corral: {target} exists with different content:"
            " re-run with --force to replace only that file",
            file=sys.stderr,
        )
        return USAGE
    try:
        paths.atomic_write_text(target, text)
    except OSError as err:
        print(f"corral: cannot write {target}: {err}", file=sys.stderr)
        return USAGE
    sys.stdout.write(f"wrote {target}\n")
    return OK


def cmd_init(args):
    """The `corral init` verb: flags to `Options`, then `run`."""
    return run(
        Options(
            path=args.path,
            plan=args.plan,
            yes=args.yes,
            force=args.force,
            test_cmd=args.test_cmd,
            runtime=args.runtime,
            cap=args.cap,
            sets=tuple(args.set),
        )
    )


def add_parser(sub):
    """Register `corral init` on the top-level subparsers."""
    parser = sub.add_parser("init", help="detect the project and write corral.toml")
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="the repository to onboard (default: the current directory)",
    )
    parser.add_argument("--plan", action="store_true", help="print the diff and write nothing")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="accept the detected defaults (reserved: init is already non-interactive)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing corral.toml with different content",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="key=value",
        help="override a setting (repeatable)",
    )
    parser.add_argument("--runtime", default=None, help="the agent runtime")
    parser.add_argument("--cap", type=int, default=None, help="this project's agent ceiling")
    parser.add_argument("--test-cmd", default=None, help="the test command (default: detected)")
    parser.set_defaults(func=cmd_init)
