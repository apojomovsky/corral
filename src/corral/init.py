"""`corral init` and `corral uninit` for the inline and split layouts.

`init` detects what it can and asks about the rest through flags: detected
values are used, flags override them, and a value that is neither detected
nor flagged is exit 4 (`--test-cmd` is the only one this layout cannot
default). `--yes` is accepted and is a no-op, reserved for the prompting
ticket.

The inline layout writes everything into the repository itself. The split
layout (`--ops`) keeps the agent side in a separate ops repository that
points at the project's clone: the inline layout is the case where the ops
root and the project clone are the same directory, so both layouts go
through the shared pieces below (`collect`, the entry block, the hook
convergence, the overlay, the registration).

Inline `init` writes, all idempotent and all covered by `--plan`:

- `corral.toml` at the repository root (replaced only with `--force`);
- `agents.overlay.md` next to it, from the packaged template, created only
  when missing: a re-run never touches an existing one, even with `--force`;
- the hook scripts into `<root>/.githooks/` (mode 0755) plus the clone-local
  `git config core.hooksPath .githooks`, converged per file: a missing or
  mismatched wanted hook is installed, modes are repaired, and a file that is
  not a template in the way is refused (exit 4). A `core.hooksPath` pointing
  elsewhere makes `init` write no hooks and print how to chain corral's
  from it. `git.commits.conventional = false` skips the `commit-msg` hook;
- the host registration `~/.config/corral/projects.d/<name>.toml`, the only
  write outside the repository (skipped with a note under `--no-register`).

Split `init --ops PATH` (optionally `--ops-name NAME`) writes the same files
into the ops root instead, and touches the project clone only through local,
untracked git state:

- `corral.toml` in the ops root: created when missing, otherwise the new
  `[[repo]]` entry is appended as a block and nothing else in the file is
  touched. An entry with the same name but different values is refused
  (exit 4), even with `--force`: edit it by hand;
- `agents.overlay.md` next to it, created only when missing;
- the hook scripts under `<ops>/hooks/<clone-name>/`, with the clone-local
  `core.hooksPath` set to that directory's absolute path (same convergence
  and foreign-hook rules as inline);
- one `.worktrees/` line in the clone's `.git/info/exclude`, added when
  absent and never touching any other line.

The host registration is for the ops root (`projects.d/<ops-dir-name>.toml`
with the ops root as its root). A missing ops root is created with a local
`git init -b main`; an existing non-empty non-repository is refused.

`init` never edits `AGENTS.md`, `CLAUDE.md` or any other existing file, and
never runs `gh` or anything outward: it prints `corral github setup` as the
next step for the user and does nothing else.

`uninit` reverses only what `init` can prove it wrote: a hook file goes only
when its bytes still equal the packaged template, the registration only
when its content still equals what `init` would write now. In the split
layout the `[[repo]]` entry, `corral.toml`, `agents.overlay.md` and the ops
registration stay; the registration is reported as kept (shared by the ops
root). Anything modified since stays and is listed as kept, with the reason.
"""

import argparse
import difflib
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Callable
from importlib import resources
from typing import NamedTuple

from corral import detect, gc, paths, projects, runtimes, settings, worktrees

OK, NOTHING, USAGE = 0, 1, 4


class InitError(ValueError):
    """A usage or configuration error (exit 4)."""


#: The `[[repo]]` entry keys always written, in file order.
TRIO = ("clone.path", "clone.default_branch", "checks.test")

#: The overlay file next to `corral.toml`, from the packaged template.
OVERLAY_FILE = "agents.overlay.md"

#: The hook directory in the repository root, and the hooks `init` installs.
HOOKS_DIR = ".githooks"
HOOK_FILES = ("commit-msg", "pre-push")

#: The git key `init` sets (clone-local) to point git at `HOOKS_DIR`.
HOOKS_KEY = "core.hooksPath"

#: The hooks parent inside an ops root: hooks live at `<ops>/hooks/<name>/`.
OPS_HOOKS_PARENT = "hooks"

#: The line `init --ops` adds to the clone's `.git/info/exclude`.
EXCLUDE_LINE = ".worktrees/"

#: `argparse` value for a bare `--ops` (no PATH): the default `<clone>_ops`
#: sibling. A real path is never this value.
OPS_DEFAULT = ""


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


def project_name(root, git):
    """The `[[repo]]` entry name: the origin slug's repo, else the dir."""
    slug = detect.origin_slug(root, git)
    return slug.split("/")[1] if slug else os.path.basename(root)


def registration_name(root):
    """The host registration name: the repository directory name."""
    return os.path.basename(root)


def collect(path, *, git, test_cmd=None, runtime=None, cap=None, sets=(), clone_path="."):
    """`(root, PlanInputs)` for the repository holding `path`.

    Flags win over detection. A missing test command, an unknown
    `--set` key, a mistyped `--set` value and an unknown `--runtime`
    are `InitError`. `clone_path` is the `clone.path` value when no
    `--set clone.path=` override is given: `"."` in the inline layout,
    the clone's absolute path in the split layout.
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
    name = project_name(root, git)
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
        "clone.path": clone_path,
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


def default_ops_name(clone_root):
    """The default ops directory name: `<clone-dir-name>_ops`."""
    return f"{os.path.basename(os.path.realpath(clone_root))}_ops"


def ops_candidate(clone_root, ops, ops_name):
    """The ops root path for the flags, None for the inline layout.

    A `--ops PATH` wins over `--ops-name NAME`; a bare `--ops` or an
    `--ops-name` alone means the sibling of that name next to the clone.
    An ops root strictly inside the clone would put tracked files into the
    project, so it is refused (exit 4); the clone itself is the inline layout.
    """
    candidate = _ops_path(clone_root, ops, ops_name)
    if candidate is not None and worktrees.inside(
        os.path.realpath(candidate), os.path.realpath(clone_root)
    ):
        raise InitError(
            f"--ops {candidate} lies inside the project clone {clone_root}: "
            "the ops root must be outside it (nothing tracked is written into the project)"
        )
    return candidate


def _ops_path(clone_root, ops, ops_name):
    if ops is not None and ops != OPS_DEFAULT:
        return os.path.abspath(ops)
    if ops_name is not None:
        if not ops_name or ops_name in (".", "..") or os.path.basename(ops_name) != ops_name:
            raise InitError(f"--ops-name {ops_name!r}: want a plain directory name")
        parent = os.path.dirname(os.path.realpath(clone_root))
        return os.path.join(parent, ops_name)
    if ops is not None:
        parent = os.path.dirname(os.path.realpath(clone_root))
        return os.path.join(parent, default_ops_name(clone_root))
    return None


def ensure_ops_root(candidate, git, plan):
    """`(root, fresh)` for the ops root at `candidate`.

    A missing root is created with `git init`; an existing empty directory
    is initialised in place. An existing path must be a directory; an
    existing non-empty directory that is not a git repository is exit 4.
    `--plan` creates nothing and runs no `git init`. `fresh` is true when
    apply runs the `git init` (or the plan reports it).
    """
    if os.path.exists(candidate) and not os.path.isdir(candidate):
        raise InitError(f"not a directory: {candidate}")
    exists = os.path.exists(candidate)
    is_repo = False
    if exists:
        try:
            proc = git(["rev-parse", "--show-toplevel"], os.path.abspath(candidate))
        except OSError:
            raise InitError(f"not a git repository: {candidate}") from None
        is_repo = proc is not None and getattr(proc, "returncode", 1) == 0
    if is_repo:
        return os.path.realpath(candidate), False
    if exists and os.listdir(candidate):
        raise InitError(
            f"{candidate} exists, is not empty and is not a git repository:"
            " point --ops at an empty directory or a git repository"
        )
    if plan:
        if exists:
            return os.path.realpath(candidate), True
        return os.path.abspath(candidate), True
    _makedirs(candidate)
    creator = git(["init", "-b", "main"], os.path.abspath(candidate))
    if not _git_ok(creator):
        raise InitError(f"cannot git init {candidate}")
    return os.path.realpath(candidate), True


def resolve_existing_ops_root(candidate, git):
    """The existing ops root at `candidate`, else exit 4 (for `uninit`)."""
    if not os.path.isdir(candidate):
        raise InitError(f"not a git repository: {candidate}")
    try:
        return resolve_root(candidate, git)
    except InitError:
        raise InitError(f"not a git repository: {candidate}") from None


def ops_hooks_dir(ops_root, entry_name):
    """The hook directory for entry `entry_name` in the ops root."""
    return os.path.join(os.path.realpath(ops_root), OPS_HOOKS_PARENT, entry_name)


def split_entry_name(ops_root, clone_root):
    """The `[[repo]]` name already registered for this clone, else None.

    Matches on `clone.path` against the clone's real path: the stored name
    survives a later remote change, where live detection would drift. One
    lookup shared by split `init` (refuse a second entry for the same
    clone) and split `uninit` (find this clone's hooks after a rename).
    """
    raw = _read_text(os.path.join(ops_root, settings.PROJECT_FILE))
    if raw is None:
        return None
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return None
    entries = data.get("repo")
    if not isinstance(entries, list):
        return None
    want = os.path.realpath(clone_root)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        clone = entry.get("clone")
        path = clone.get("path") if isinstance(clone, dict) else None
        if isinstance(path, str) and os.path.realpath(path) == want:
            name = entry.get("name")
            return name if isinstance(name, str) else None
    return None


def hooks_state(hooks_dir, wanted):
    """`(missing, fixable, blocked)` hook files under `hooks_dir`.

    Missing wanted hooks are installed by `init`; a wrong mode is repaired;
    a symlink, a mismatched file or a foreign file in the way blocks with
    exit 4. Shared by both layouts.
    """
    missing: list[tuple[str, str]] = []
    fixable: list[str] = []
    blocked: list[tuple[str, str]] = []
    for name in wanted:
        target = os.path.join(hooks_dir, name)
        installed = _file_bytes(target)
        if os.path.islink(target):
            blocked.append((target, name))
        elif installed is None:
            missing.append((target, name))
        elif installed != hook_template(name):
            blocked.append((target, name))
        elif _hook_needs_chmod(target):
            fixable.append(target)
    return missing, fixable, blocked


def exclude_path(clone_root):
    """The clone's `.git/info/exclude` file."""
    return os.path.join(clone_root, ".git", "info", "exclude")


def _exclude_lines(target):
    """The exclude file's lines (no newlines), None when missing."""
    try:
        with open(target, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise InitError(f"corral: cannot read {target}: {err}") from None


def ensure_exclude_line(target, plan):
    """Add `EXCLUDE_LINE` to the exclude file when absent. Returns changed."""
    lines = _exclude_lines(target)
    if lines is not None and EXCLUDE_LINE in lines:
        return False
    if plan:
        return True
    if lines is None:
        _makedirs(os.path.dirname(target))
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(EXCLUDE_LINE + "\n")
        except OSError as err:
            raise InitError(f"corral: cannot write {target}: {err}") from None
        return True
    current = _read_text(target) or ""
    if current and not current.endswith("\n"):
        current += "\n"
    _write_text(target, current + EXCLUDE_LINE + "\n")
    return True


def remove_exclude_line(target):
    """Drop every exact `EXCLUDE_LINE` line, touching no other line."""
    lines = _exclude_lines(target)
    if lines is None or EXCLUDE_LINE not in lines:
        return False
    kept = [line for line in lines if line != EXCLUDE_LINE]
    _write_text(target, "".join(line + "\n" for line in kept))
    return True


def _toml(value):
    """A registry default or active value as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def _active(inputs):
    """The `[[repo]]` entry values for `inputs`, as flat `prefix.leaf` keys."""
    active = {
        "clone.path": inputs.path,
        "clone.default_branch": inputs.default_branch,
        "checks.test": inputs.test_command,
    }
    active.update(inputs.extra)
    return active


def _entry_lines(name, active):
    """The `[[repo]]` block lines for entry `name` with flat `active` values."""
    lines = ["[[repo]]", f"name = {_toml(name)}", ""]
    groups: dict[str, list[str]] = {}
    for key in active:
        groups.setdefault(key.split(".")[0], []).append(key)
    for prefix in sorted(groups):
        lines.append(f"[repo.{prefix}]")
        for key in sorted(groups[prefix]):
            lines.append(f"{key.split('.', 1)[1]} = {_toml(active[key])}")
        lines.append("")
    return lines


def _entry_block(name, active):
    """The `[[repo]]` block text for entry `name` (trailing newline)."""
    return "\n".join(_entry_lines(name, active)) + "\n"


def _entry_values_equal(entry, name, active):
    """True when the parsed `[[repo]]` table equals the entry we would write."""
    return isinstance(entry, dict) and entry.get("name") == name and _flatten_entry(entry) == active


def render(inputs):
    """The `corral.toml` text for `inputs`: the settings that differ
    from the defaults live in the single `[[repo]]` entry; every other
    registered setting is commented out with its default and one-line
    help, grouped by prefix, so the template never goes stale."""
    settings._load_providers()
    active = _active(inputs)
    lines = [
        "# corral.toml, written by `corral init`. Settings that differ",
        "# from the defaults live in [[repo]]; everything else is",
        "# commented out below with its default.",
    ]
    lines.extend(_entry_lines(inputs.name, active))
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


def _makedirs(path):
    """`os.makedirs` with failures as `InitError`, like every neighbouring write."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as err:
        raise InitError(f"corral: cannot create {path}: {err}") from None


def plan_diff(current, new, name="corral.toml"):
    """The unified diff from `current` (None for a new file) to `new`, for `name`."""
    old = [] if current is None else current.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old,
            new.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


def hook_template(name):
    """The packaged hook script `name` as bytes (`importlib.resources`)."""
    return resources.files("corral").joinpath(f"hooks/{name}").read_bytes()


def overlay_text():
    """The packaged `agents.overlay.md` template."""
    return resources.files("corral").joinpath("overlay_template.md").read_text("utf-8")


def chain_lines(conventional):
    """How to run corral's hooks from already-installed ones (exact lines)."""
    base = resources.files("corral").joinpath("hooks")
    names = list(HOOK_FILES) if conventional else ["pre-push"]
    lines = ["existing git hooks found: leaving them alone."]
    lines.append("To run corral's hooks from yours, add these lines:")
    for name in names:
        lines.append(f'exec "{base.joinpath(name)}" "$@"  # in your {name} hook')
    return lines


def registration_text(name, root, cap):
    """The host registration `init` writes for this repository."""
    return "\n".join(
        [
            "# The host registration for this project, written by `corral init`.",
            "# `corral uninit` removes this file while its content still matches",
            "# what `corral init` would write; edit it and `uninit` leaves it alone.",
            f"name = {_toml(name)}",
            f"unit_prefix = {_toml(f'{name}-agent')}",
            f"root = {_toml(root)}",
            "floor = 0",
            f"ceiling = {_toml(cap)}",
            "",
        ]
    )


def _registration_conflicts(directory, target, name, prefix, root):
    """`InitError` when another registration claims `prefix` or `root`.

    The file for `name` itself is skipped: replacing it is `--force`'s job,
    decided by the caller. Anything unreadable is exit 4 naming the file.
    """
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return
    except OSError as err:
        raise InitError(f"cannot list {directory}: {err}") from None
    for entry in names:
        if not entry.endswith(".toml") or entry == f"{name}.toml":
            continue
        path = os.path.join(directory, entry)
        try:
            with open(path, "rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as err:
            raise InitError(f"{path}: cannot read the registration: {err}") from None
        if not isinstance(raw, dict):
            raise InitError(f"{path}: the file must hold a TOML table")
        stem = entry[: -len(".toml")]
        if raw.get("unit_prefix") == prefix:
            raise InitError(
                f"{target}: unit_prefix {prefix!r} is already "
                f"registered by project {stem!r} ({path})"
            )
        claimed = raw.get("root")
        if isinstance(claimed, str) and claimed and os.path.realpath(claimed) == root:
            raise InitError(
                f"{target}: root {root} is already registered by project {stem!r} ({path})"
            )


def _git_ok(proc):
    """True when a git seam call succeeded."""
    return proc is not None and getattr(proc, "returncode", 1) == 0


def _read_text(target):
    """The file's text, None when missing; other `OSError` is `InitError`."""
    try:
        with open(target, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise InitError(f"corral: cannot read {target}: {err}") from None


def _write_text(target, text):
    """`paths.atomic_write_text` with `InitError` on failure."""
    try:
        paths.atomic_write_text(target, text)
    except OSError as err:
        raise InitError(f"corral: cannot write {target}: {err}") from None


def _file_bytes(target):
    """The file's bytes, None when missing; other `OSError` is `InitError`."""
    try:
        with open(target, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise InitError(f"corral: cannot read {target}: {err}") from None


def _hook_needs_chmod(target):
    """True when an installed hook lost its executable bit (`OSError` is exit 4)."""
    try:
        return os.path.isfile(target) and stat.S_IMODE(os.stat(target).st_mode) != 0o755
    except OSError as err:
        raise InitError(f"corral: cannot stat {target}: {err}") from None


def _expected_ceiling(root, git):
    """The ceiling `uninit` expects: `agents.cap` from the repository's own
    `[[repo]]` entry, else the setting default when the file is missing,
    unparseable or silent on the cap."""
    default = settings.REGISTRY["agents.cap"].default
    if not isinstance(default, int) or isinstance(default, bool):
        raise InitError("internal: agents.cap is not an int setting")
    raw = _read_text(os.path.join(root, settings.PROJECT_FILE))
    if raw is None:
        return default
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return default
    entries = data.get("repo")
    if not isinstance(entries, list):
        return default
    entry_name = project_name(root, git)
    table = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == entry_name:
            table = entry
            break
    if table is None:
        if len(entries) == 1 and isinstance(entries[0], dict):
            table = entries[0]
        else:
            return default
    agents = table.get("agents")
    cap = agents.get("cap") if isinstance(agents, dict) else None
    if isinstance(cap, bool) or not isinstance(cap, int):
        return default
    return cap


class Options(NamedTuple):
    """Parsed `corral init` flags."""

    path: str | None = None
    plan: bool = False
    yes: bool = False
    force: bool = False
    no_register: bool = False
    test_cmd: str | None = None
    runtime: str | None = None
    cap: int | None = None
    sets: tuple = ()
    ops: str | None = None
    ops_name: str | None = None


def run(opts, git=gc.git):
    """Run `init` with an injectable git seam. Returns the exit code."""
    try:
        clone_arg = opts.path or os.getcwd()
        clone_root = resolve_root(clone_arg, git)
        candidate = ops_candidate(clone_root, opts.ops, opts.ops_name)
        ops_created = candidate is not None and not os.path.exists(candidate)
        if candidate is None:
            ops_root, split, ops_fresh = clone_root, False, False
        else:
            ops_root, ops_fresh = ensure_ops_root(candidate, git, opts.plan)
            split = os.path.realpath(ops_root) != os.path.realpath(clone_root)
        _, inputs = collect(
            clone_arg,
            git=git,
            test_cmd=opts.test_cmd,
            runtime=opts.runtime,
            cap=opts.cap,
            sets=opts.sets,
            clone_path="." if not split else os.path.realpath(clone_root),
        )
        return _apply(ops_root, clone_root, inputs, opts, git, split, ops_created, ops_fresh)
    except InitError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE


def _flatten_entry(entry):
    """A parsed `[[repo]]` table as flat `prefix.leaf` values (no `name`)."""
    flat: dict[str, object] = {}
    for prefix, table in entry.items():
        if prefix == "name" or not isinstance(table, dict):
            continue
        for leaf, value in table.items():
            flat[f"{prefix}.{leaf}"] = value
    return flat


def _split_toml_change(toml_target, inputs):
    """`(new_text, is_noop)` for the split layout's append-only `corral.toml`.

    A missing file is created from `render(inputs)`; otherwise the new
    `[[repo]]` entry is appended and every existing byte is preserved. An
    entry with the same name but different values prints its diff and is
    exit 4, even with `--force`: edit it by hand.
    """
    active = _active(inputs)
    current = _read_text(toml_target)
    if current is None:
        return render(inputs), False
    try:
        data = tomllib.loads(current)
    except tomllib.TOMLDecodeError as err:
        raise InitError(f"{toml_target}: cannot parse the ops corral.toml: {err}") from None
    entries = data.get("repo")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise InitError(f"{toml_target}: the file must hold a [[repo]] list to append to")
    matches = [e for e in entries if isinstance(e, dict) and e.get("name") == inputs.name]
    if matches and all(_entry_values_equal(m, inputs.name, active) for m in matches):
        return None, True
    if matches:
        old_block = _entry_block(inputs.name, _flatten_entry(matches[0]))
        sys.stdout.write(
            plan_diff(old_block, _entry_block(inputs.name, active), os.path.basename(toml_target))
        )
        raise InitError(
            f"{toml_target} already holds a [[repo]] entry for {inputs.name!r}"
            " with different values: edit it by hand"
        )
    block = _entry_block(inputs.name, active)
    if current.endswith("\n\n"):
        sep = ""
    elif current.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    return current + sep + block, False


def _apply(ops_root, clone_root, inputs, opts, git, split, ops_created=False, ops_fresh=False):
    """Write everything `init` owns. Raises `InitError` on refusal.

    `ops_root` holds `corral.toml`, the overlay and the hooks; the clone
    holds only local git state in the split layout. Inline is the case
    where both roots are the same directory. `ops_created` is true when the
    ops root did not exist; `ops_fresh` when apply runs `git init` there.
    """
    toml_target = os.path.join(ops_root, settings.PROJECT_FILE)
    if split:
        stored = split_entry_name(ops_root, clone_root)
        if stored is not None and stored != inputs.name:
            raise InitError(
                f"this clone is already registered as {stored!r} in {toml_target}:"
                " edit the entry by hand"
            )
        text, _ = _split_toml_change(toml_target, inputs)
        current = _read_text(toml_target)
        toml_changed = text is not None
    else:
        current = _read_text(toml_target)
        text = render(inputs)
        if current is not None and current != text and not opts.force and not opts.plan:
            sys.stdout.write(plan_diff(current, text))
            print(
                f"corral: {toml_target} exists with different content:"
                " re-run with --force to replace only that file",
                file=sys.stderr,
            )
            return USAGE
        toml_changed = current != text
    extra = dict(inputs.extra)
    cap = extra.get("agents.cap", settings.REGISTRY["agents.cap"].default)
    if not isinstance(cap, int) or isinstance(cap, bool):
        raise InitError("internal: agents.cap is not an int setting")
    conventional = extra.get(
        "git.commits.conventional", settings.REGISTRY["git.commits.conventional"].default
    )
    if not isinstance(conventional, bool):
        raise InitError("internal: git.commits.conventional is not a bool setting")
    overlay_target = os.path.join(ops_root, OVERLAY_FILE)
    overlay_missing = not os.path.isfile(overlay_target)
    if split:
        hooks_dir = ops_hooks_dir(ops_root, inputs.name)
        want_hooks = hooks_dir
    else:
        hooks_dir = os.path.join(clone_root, HOOKS_DIR)
        want_hooks = HOOKS_DIR
    hook_proc = git(["config", HOOKS_KEY], clone_root)
    hooks_path = (getattr(hook_proc, "stdout", "") or "").strip() if _git_ok(hook_proc) else ""
    hooks_path_set = hooks_path == want_hooks
    foreign_hooks = hooks_path != "" and hooks_path != want_hooks
    chained = [n for n in HOOK_FILES if n != "commit-msg" or conventional]
    missing: list[tuple[str, str]] = []
    fixable: list[str] = []
    blocked: list[tuple[str, str]] = []
    if not foreign_hooks:
        missing, fixable, blocked = hooks_state(hooks_dir, chained)
    kept_commit_msg: str | None = None
    if not conventional and not foreign_hooks:
        cm_target = os.path.join(hooks_dir, "commit-msg")
        if os.path.isfile(cm_target) and _file_bytes(cm_target) == hook_template("commit-msg"):
            kept_commit_msg = cm_target
    show_chain = foreign_hooks or bool(blocked)
    if split:
        reg_root = os.path.realpath(ops_root)
        reg_name = registration_name(reg_root)
    else:
        reg_name = registration_name(clone_root)
        reg_root = os.path.realpath(clone_root)
    reg_target = os.path.join(projects.projects_dir(), f"{reg_name}.toml")
    reg_text = registration_text(reg_name, reg_root, cap)
    reg_missing: bool | None = None
    current_reg: str | None = None
    if not opts.no_register:
        current_reg = _read_text(reg_target)
        if current_reg is None:
            reg_missing = True
        elif current_reg == reg_text:
            reg_missing = False
        elif not opts.force and not opts.plan:
            raise InitError(
                f"{reg_target} exists with different content: re-run with --force to replace it"
            )
        else:
            reg_missing = True
    exclude_target = exclude_path(clone_root) if split else ""
    if split:
        exclude_lines = _exclude_lines(exclude_target)
        exclude_need = exclude_lines is None or EXCLUDE_LINE not in exclude_lines
    else:
        exclude_need = False
    if not opts.plan:
        if blocked:
            target, _ = blocked[0]
            raise InitError(f"{target} exists with different content: remove it or chain from it")
        if not opts.no_register and reg_missing:
            _registration_conflicts(
                projects.projects_dir(),
                reg_target,
                reg_name,
                f"{reg_name}-agent",
                reg_root,
            )
    need_hooks = not foreign_hooks and (
        bool(missing) or bool(fixable) or (bool(chained) and not hooks_path_set)
    )
    changed = (
        toml_changed
        or overlay_missing
        or need_hooks
        or bool(blocked)
        or reg_missing is True
        or exclude_need
    )
    if not changed and kept_commit_msg is None:
        sys.stdout.write(f"{toml_target} is up to date\n")
        if show_chain:
            for line in chain_lines(conventional):
                sys.stdout.write(line + "\n")
        if opts.no_register:
            sys.stdout.write("skipped the host registration (--no-register)\n")
        return NOTHING
    if not changed:
        sys.stdout.write(f"{toml_target} is up to date\n")
        if kept_commit_msg is not None:
            sys.stdout.write(
                f"kept {kept_commit_msg} (git.commits.conventional is false);"
                " delete it to stop enforcing\n"
            )
        if show_chain:
            for line in chain_lines(conventional):
                sys.stdout.write(line + "\n")
        if opts.no_register:
            sys.stdout.write("skipped the host registration (--no-register)\n")
        return NOTHING
    if opts.plan:
        if split and ops_created:
            sys.stdout.write(f"would create {ops_root}\n")
        if split and ops_fresh:
            sys.stdout.write(f"would run git init -b main in {ops_root}\n")
        if text is not None and (not split or current is None or text != current):
            sys.stdout.write(plan_diff(current, text))
        if overlay_missing:
            sys.stdout.write(f"would write {overlay_target}\n")
        for target, _ in missing:
            sys.stdout.write(f"would write {target}\n")
        for target in fixable:
            sys.stdout.write(f"would fix the mode of {target} to 0755\n")
        for target, _ in blocked:
            sys.stdout.write(f"would keep {target} (differs: remove it or chain from it)\n")
        if chained and not hooks_path_set and not foreign_hooks:
            sys.stdout.write(f"would set core.hooksPath to {want_hooks}\n")
        if kept_commit_msg is not None:
            sys.stdout.write(
                f"kept {kept_commit_msg} (git.commits.conventional is false);"
                " delete it to stop enforcing\n"
            )
        if split and exclude_need:
            sys.stdout.write(f"would add {EXCLUDE_LINE} to {exclude_target}\n")
        if reg_missing is True:
            if current_reg is not None:
                sys.stdout.write(plan_diff(current_reg, reg_text, os.path.basename(reg_target)))
            sys.stdout.write(f"would write {reg_target}\n")
        if show_chain:
            for line in chain_lines(conventional):
                sys.stdout.write(line + "\n")
        if opts.no_register:
            sys.stdout.write("skipped the host registration (--no-register)\n")
        sys.stdout.write("next: corral github setup\n")
        return OK
    if split and ops_created:
        sys.stdout.write(f"created {ops_root}\n")
    elif split and ops_fresh:
        sys.stdout.write(f"initialised {ops_root}\n")
    if toml_changed and text is not None:
        _write_text(toml_target, text)
        sys.stdout.write(f"wrote {toml_target}\n")
    if overlay_missing:
        _write_text(overlay_target, overlay_text())
        sys.stdout.write(f"wrote {overlay_target}\n")
    if missing or fixable or (chained and not hooks_path_set and not foreign_hooks):
        _makedirs(hooks_dir)
        for target, name in missing:
            _write_text(target, hook_template(name).decode("utf-8"))
            os.chmod(target, 0o755)
            sys.stdout.write(f"wrote {target}\n")
        for target in fixable:
            try:
                os.chmod(target, 0o755)
            except OSError as err:
                raise InitError(f"corral: cannot chmod {target}: {err}") from None
            sys.stdout.write(f"fixed the mode of {target} to 0755\n")
        if chained and not hooks_path_set and not foreign_hooks:
            setter = git(["config", HOOKS_KEY, want_hooks], clone_root)
            if not _git_ok(setter):
                raise InitError(f"cannot set core.hooksPath in {clone_root}")
            sys.stdout.write(f"set core.hooksPath to {want_hooks} in {clone_root}\n")
    if split and exclude_need:
        ensure_exclude_line(exclude_target, False)
        sys.stdout.write(f"added {EXCLUDE_LINE} to {exclude_target}\n")
    if kept_commit_msg is not None:
        sys.stdout.write(
            f"kept {kept_commit_msg} (git.commits.conventional is false);"
            " delete it to stop enforcing\n"
        )
    if show_chain:
        for line in chain_lines(conventional):
            sys.stdout.write(line + "\n")
    if not opts.no_register and reg_missing:
        _makedirs(projects.projects_dir())
        _write_text(reg_target, reg_text)
        try:
            registered = projects.load_all()
        except projects.ProjectsError as err:
            raise InitError(str(err)) from None
        if not any(
            p.name == reg_name
            and p.unit_prefix == f"{reg_name}-agent"
            and p.root == os.path.normpath(reg_root)
            and p.floor == 0
            and p.ceiling == cap
            for p in registered
        ):
            raise InitError(f"{reg_target}: wrote the registration but cannot read it back")
        sys.stdout.write(f"wrote {reg_target}\n")
    elif opts.no_register:
        sys.stdout.write("skipped the host registration (--no-register)\n")
    sys.stdout.write("next: corral github setup\n")
    return OK


def cmd_init(args):
    """The `corral init` verb: flags to `Options`, then `run`."""
    return run(
        Options(
            path=args.path,
            plan=args.plan,
            yes=args.yes,
            force=args.force,
            no_register=args.no_register,
            test_cmd=args.test_cmd,
            runtime=args.runtime,
            cap=args.cap,
            sets=tuple(args.set),
            ops=args.ops,
            ops_name=args.ops_name,
        )
    )


class UnOptions(NamedTuple):
    """Parsed `corral uninit` flags."""

    path: str | None = None
    plan: bool = False
    ops: str | None = None
    ops_name: str | None = None


def production_remove(*roots):
    """The real destruction seam. Only the CLI builds it.

    `remove` deletes one path strictly below one of `roots`; anything
    else, and any symlink, is `InitError`. Symlinks are refused by
    `lstat`: the seam never resolves-then-deletes a link's target. Tests
    pass a fake through `run_un`; the tests that call this one do so only
    against temp dirs.
    """
    real_roots = [os.path.realpath(root) for root in roots]

    def remove(path: str) -> None:
        if os.path.islink(path):
            raise InitError(f"refusing to remove {path}: is a symlink")
        real = os.path.realpath(path)
        if not any(real != parent and worktrees.inside(real, parent) for parent in real_roots):
            raise InitError(f"refusing to remove {path}: escapes the removal roots")
        try:
            if os.path.isdir(path):
                os.rmdir(path)
            else:
                os.remove(path)
        except OSError as err:
            raise InitError(f"cannot remove {path}: {err}") from None

    return remove


def run_un(opts, remove, git=gc.git):
    """Run `uninit` with required seams. `remove` deletes one path and has
    no default; tests inject a fake. Returns the exit code."""
    try:
        return _unapply(opts, remove, git)
    except InitError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE


def _unapply(opts, remove, git):
    """Remove what `init` can prove it wrote. Raises `InitError`.

    In the split layout only this clone's hook files, its `core.hooksPath`
    and its `.worktrees/` exclude line go; the `[[repo]]` entry,
    `corral.toml`, the overlay and the ops registration stay.
    """
    clone_root = resolve_root(opts.path or os.getcwd(), git)
    candidate = ops_candidate(clone_root, opts.ops, opts.ops_name)
    if candidate is None:
        ops_root, split = clone_root, False
    else:
        ops_root = resolve_existing_ops_root(candidate, git)
        split = os.path.realpath(ops_root) != os.path.realpath(clone_root)
    if split:
        entry_name = split_entry_name(ops_root, clone_root) or project_name(clone_root, git)
        hooks_dir = ops_hooks_dir(ops_root, entry_name)
        want_hooks = hooks_dir
    else:
        hooks_dir = os.path.join(clone_root, HOOKS_DIR)
        want_hooks = HOOKS_DIR
        ops_root = clone_root
    removed: list[str] = []
    kept: list[tuple[str, str]] = []
    planned: list[str] = []
    for name in HOOK_FILES:
        target = os.path.join(hooks_dir, name)
        if not os.path.lexists(target):
            continue
        if os.path.islink(target):
            kept.append((target, "is a symlink"))
            continue
        try:
            with open(target, "rb") as handle:
                installed = handle.read()
        except OSError as err:
            raise InitError(f"corral: cannot read {target}: {err}") from None
        if installed != hook_template(name):
            kept.append((target, "modified since `corral init` wrote it"))
        elif opts.plan:
            planned.append(target)
        else:
            _remove(remove, target)
            removed.append(target)
    stays = _remaining_hooks(hooks_dir, planned)
    proc = git(["config", HOOKS_KEY], clone_root)
    hooks_path = (getattr(proc, "stdout", "") or "").strip() if _git_ok(proc) else ""
    unset = False
    if hooks_path == want_hooks and not stays:
        if opts.plan:
            planned.append("core.hooksPath")
        else:
            unset_proc = git(["config", "--unset", HOOKS_KEY], clone_root)
            if not _git_ok(unset_proc):
                raise InitError(f"cannot unset core.hooksPath in {clone_root}")
            unset = True
    elif hooks_path == want_hooks and stays:
        kept.append(("core.hooksPath", "hooks remain"))
    if os.path.isdir(hooks_dir) and not os.path.islink(hooks_dir):
        if not stays:
            if opts.plan:
                planned.append(hooks_dir)
            else:
                _remove(remove, hooks_dir)
                removed.append(hooks_dir)
        else:
            kept.append((hooks_dir, "holds files `corral init` did not write"))
    exclude_target = exclude_path(clone_root) if split else ""
    exclude_removed = False
    if split:
        lines = _exclude_lines(exclude_target)
        if lines is not None and EXCLUDE_LINE in lines:
            if opts.plan:
                planned.append(f"exclude-line:{exclude_target}")
            else:
                exclude_removed = remove_exclude_line(exclude_target)
    if split:
        ops_reg_name = registration_name(os.path.realpath(ops_root))
        ops_reg_target = os.path.join(projects.projects_dir(), f"{ops_reg_name}.toml")
        if os.path.isfile(ops_reg_target):
            kept.append((ops_reg_target, "shared by the ops root"))
    else:
        reg_name = registration_name(clone_root)
        reg_target = os.path.join(projects.projects_dir(), f"{reg_name}.toml")
        expected = registration_text(
            reg_name, os.path.realpath(clone_root), _expected_ceiling(clone_root, git)
        )
        reg_current = _read_text(reg_target)
        if reg_current is not None:
            if reg_current == expected:
                if opts.plan:
                    planned.append(reg_target)
                else:
                    _remove(remove, reg_target)
                    removed.append(reg_target)
            else:
                kept.append((reg_target, "differs from what `corral init` would write now"))
    if opts.plan:
        for target in planned:
            if target == "core.hooksPath":
                sys.stdout.write("would unset core.hooksPath\n")
            elif target.startswith("exclude-line:"):
                sys.stdout.write(
                    f"would remove the {EXCLUDE_LINE} line from {target[len('exclude-line:') :]}\n"
                )
            else:
                sys.stdout.write(f"would remove {target}\n")
        for target, reason in kept:
            sys.stdout.write(f"would keep {target} ({reason})\n")
        return OK if planned else NOTHING
    for target in removed:
        sys.stdout.write(f"removed {target}\n")
    if unset:
        sys.stdout.write("unset core.hooksPath\n")
    if exclude_removed:
        sys.stdout.write(f"removed the {EXCLUDE_LINE} line from {exclude_target}\n")
    for target, reason in kept:
        sys.stdout.write(f"kept {target} ({reason})\n")
    if removed or unset or exclude_removed:
        sys.stdout.write(
            f"left for you to delete: {os.path.join(ops_root, settings.PROJECT_FILE)},"
            f" {os.path.join(ops_root, OVERLAY_FILE)}\n"
        )
    else:
        sys.stdout.write("nothing to remove\n")
    return OK if removed or unset or exclude_removed else NOTHING


def _remaining_hooks(hooks_dir, planned):
    """Paths under `hooks_dir` that stay: everything except `planned` removals."""
    try:
        names = os.listdir(hooks_dir)
    except FileNotFoundError:
        return []
    except OSError as err:
        raise InitError(f"corral: cannot list {hooks_dir}: {err}") from None
    return [
        os.path.join(hooks_dir, name)
        for name in names
        if os.path.join(hooks_dir, name) not in planned
    ]


def _remove(remove, target):
    """One seam deletion with `InitError` on failure."""
    try:
        remove(target)
    except InitError:
        raise
    except OSError as err:
        raise InitError(f"cannot remove {target}: {err}") from None


def cmd_uninit(args):
    """The `corral uninit` verb: flags to `UnOptions`, then `run_un`."""
    try:
        clone_root = resolve_root(args.path or os.getcwd(), gc.git)
        candidate = ops_candidate(clone_root, args.ops, args.ops_name)
        roots = [os.path.realpath(clone_root), os.path.realpath(projects.projects_dir())]
        if candidate is not None:
            ops_root = resolve_existing_ops_root(candidate, gc.git)
            if os.path.realpath(ops_root) != os.path.realpath(clone_root):
                roots.append(os.path.realpath(ops_root))
    except InitError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE
    remove = production_remove(*roots)
    return run_un(
        UnOptions(path=args.path, plan=args.plan, ops=args.ops, ops_name=args.ops_name), remove
    )


def add_parser(sub):
    """Register `corral init` and `corral uninit` on the top-level subparsers."""
    parser = sub.add_parser("init", help="detect the project and write corral.toml")
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="the project clone to onboard (default: the current directory)",
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
        help="replace an existing corral.toml or registration with different content",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="skip the host registration in ~/.config/corral/projects.d/",
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
    parser.add_argument(
        "--ops",
        nargs="?",
        const=OPS_DEFAULT,
        default=None,
        metavar="PATH",
        help="write the agent side to the ops root at PATH"
        " (bare --ops uses the <clone>_ops sibling)",
    )
    parser.add_argument(
        "--ops-name",
        default=None,
        metavar="NAME",
        help="write the agent side to the NAME sibling of the clone (--ops wins)",
    )
    parser.set_defaults(func=cmd_init)
    uninit = sub.add_parser("uninit", help="remove the hooks and registration `corral init` wrote")
    uninit.add_argument(
        "path",
        nargs="?",
        default=None,
        help="the project clone to uninit (default: the current directory)",
    )
    uninit.add_argument("--plan", action="store_true", help="print the plan and change nothing")
    uninit.add_argument(
        "--ops",
        nargs="?",
        const=OPS_DEFAULT,
        default=None,
        metavar="PATH",
        help="the ops root holding this clone's entry (bare --ops uses the sibling)",
    )
    uninit.add_argument(
        "--ops-name",
        default=None,
        metavar="NAME",
        help="the NAME sibling of the clone as the ops root (--ops wins)",
    )
    uninit.set_defaults(func=cmd_uninit)
