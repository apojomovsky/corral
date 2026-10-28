"""`corral init` and `corral uninit` for the inline layout.

`init` detects what it can and asks about the rest through flags: detected
values are used, flags override them, and a value that is neither detected
nor flagged is exit 4 (`--test-cmd` is the only one this layout cannot
default). `--yes` is accepted and is a no-op, reserved for the prompting
ticket.

Inline `init` writes, all idempotent and all covered by `--plan`:

- `corral.toml` at the repository root (replaced only with `--force`);
- `agents.overlay.md` next to it, from the packaged template, created only
  when missing: a re-run never touches an existing one, even with `--force`;
- the hook scripts into `<root>/.githooks/` (mode 0755) plus the clone-local
  `git config core.hooksPath .githooks`, but only when the repository has no
  hooks of its own. With hooks already there it writes nothing and prints
  how to chain corral's hooks from them. `git.commits.conventional = false`
  skips the `commit-msg` hook;
- the host registration `~/.config/corral/projects.d/<name>.toml`, the only
  write outside the repository (skipped with a note under `--no-register`).

`init` never edits `AGENTS.md`, `CLAUDE.md` or any other existing file, and
never runs `gh` or anything outward: it prints `corral github setup` as the
next step for the user and does nothing else.

`uninit` reverses only what `init` can prove it wrote: a hook file goes only
when its bytes still equal the packaged template, the registration only
when its content still equals what `init` would write now. Anything modified
since stays and is listed as kept, with the reason. `corral.toml` and
`agents.overlay.md` are never removed.
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
    slug = detect.origin_slug(root, git)
    entry_name = slug.split("/")[1] if slug else None
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
        return _apply(root, inputs, opts, git)
    except InitError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE


def _apply(root, inputs, opts, git):
    """Write everything `init` owns. Raises `InitError` on refusal."""
    toml_target = os.path.join(root, settings.PROJECT_FILE)
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
    extra = dict(inputs.extra)
    cap = extra.get("agents.cap", settings.REGISTRY["agents.cap"].default)
    if not isinstance(cap, int) or isinstance(cap, bool):
        raise InitError("internal: agents.cap is not an int setting")
    conventional = extra.get(
        "git.commits.conventional", settings.REGISTRY["git.commits.conventional"].default
    )
    if not isinstance(conventional, bool):
        raise InitError("internal: git.commits.conventional is not a bool setting")
    overlay_target = os.path.join(root, OVERLAY_FILE)
    overlay_missing = not os.path.isfile(overlay_target)
    hook_proc = git(["config", HOOKS_KEY], root)
    hooks_path = (getattr(hook_proc, "stdout", "") or "").strip() if _git_ok(hook_proc) else ""
    hooks_path_set = hooks_path == HOOKS_DIR
    foreign_hooks = hooks_path != "" and hooks_path != HOOKS_DIR
    chained = [n for n in HOOK_FILES if n != "commit-msg" or conventional]
    missing: list[tuple[str, str]] = []
    fixable: list[str] = []
    blocked: list[tuple[str, str]] = []
    if not foreign_hooks:
        for name in chained:
            target = os.path.join(root, HOOKS_DIR, name)
            installed = _file_bytes(target)
            if installed is None:
                missing.append((target, name))
            elif installed != hook_template(name):
                blocked.append((target, name))
            elif _hook_needs_chmod(target):
                fixable.append(target)
    kept_commit_msg: str | None = None
    if not conventional and not foreign_hooks:
        cm_target = os.path.join(root, HOOKS_DIR, "commit-msg")
        if os.path.isfile(cm_target) and _file_bytes(cm_target) == hook_template("commit-msg"):
            kept_commit_msg = cm_target
    show_chain = foreign_hooks or bool(blocked)
    reg_name = registration_name(root)
    reg_target = os.path.join(projects.projects_dir(), f"{reg_name}.toml")
    reg_text = registration_text(reg_name, os.path.realpath(root), cap)
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
                os.path.realpath(root),
            )
    need_hooks = not foreign_hooks and (
        bool(missing) or bool(fixable) or (bool(chained) and not hooks_path_set)
    )
    changed = (
        current != text or overlay_missing or need_hooks or bool(blocked) or reg_missing is True
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
        if current != text:
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
            sys.stdout.write(f"would set core.hooksPath to {HOOKS_DIR}\n")
        if kept_commit_msg is not None:
            sys.stdout.write(
                f"kept {kept_commit_msg} (git.commits.conventional is false);"
                " delete it to stop enforcing\n"
            )
        if reg_missing is True:
            if current_reg is not None:
                sys.stdout.write(plan_diff(current_reg, reg_text))
            sys.stdout.write(f"would write {reg_target}\n")
        if show_chain:
            for line in chain_lines(conventional):
                sys.stdout.write(line + "\n")
        if opts.no_register:
            sys.stdout.write("skipped the host registration (--no-register)\n")
        sys.stdout.write("next: corral github setup\n")
        return OK
    if current != text:
        _write_text(toml_target, text)
        sys.stdout.write(f"wrote {toml_target}\n")
    if overlay_missing:
        _write_text(overlay_target, overlay_text())
        sys.stdout.write(f"wrote {overlay_target}\n")
    if missing or fixable or (chained and not hooks_path_set and not foreign_hooks):
        os.makedirs(os.path.join(root, HOOKS_DIR), exist_ok=True)
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
            setter = git(["config", HOOKS_KEY, HOOKS_DIR], root)
            if not _git_ok(setter):
                raise InitError(f"cannot set core.hooksPath in {root}")
            sys.stdout.write(f"set core.hooksPath to {HOOKS_DIR} in {root}\n")
    if kept_commit_msg is not None:
        sys.stdout.write(
            f"kept {kept_commit_msg} (git.commits.conventional is false);"
            " delete it to stop enforcing\n"
        )
    if show_chain:
        for line in chain_lines(conventional):
            sys.stdout.write(line + "\n")
    if not opts.no_register and reg_missing:
        os.makedirs(projects.projects_dir(), exist_ok=True)
        _write_text(reg_target, reg_text)
        try:
            registered = projects.load_all()
        except projects.ProjectsError as err:
            raise InitError(str(err)) from None
        if not any(
            p.name == reg_name
            and p.unit_prefix == f"{reg_name}-agent"
            and p.root == os.path.normpath(os.path.realpath(root))
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
        )
    )


class UnOptions(NamedTuple):
    """Parsed `corral uninit` flags."""

    path: str | None = None
    plan: bool = False


def production_remove(*roots):
    """The real destruction seam. Only the CLI builds it.

    `remove` deletes one path strictly below one of `roots`; anything
    else, and any symlink, is `InitError`. Symlinks are refused by
    `lstat`: the seam never resolves-then-deletes a link's target. Tests
    pass a fake through `run_un`; only the test named
    `test_real_production_remove_contained_to_temp_dir` calls this, only
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
    """Remove what `init` can prove it wrote. Raises `InitError`."""
    root = resolve_root(opts.path or os.getcwd(), git)
    removed: list[str] = []
    kept: list[tuple[str, str]] = []
    planned: list[str] = []
    hooks_dir = os.path.join(root, HOOKS_DIR)
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
    proc = git(["config", HOOKS_KEY], root)
    hooks_path = (getattr(proc, "stdout", "") or "").strip() if _git_ok(proc) else ""
    unset = False
    if hooks_path == HOOKS_DIR and not stays:
        if opts.plan:
            planned.append("core.hooksPath")
        else:
            unset_proc = git(["config", "--unset", HOOKS_KEY], root)
            if not _git_ok(unset_proc):
                raise InitError(f"cannot unset core.hooksPath in {root}")
            unset = True
    elif hooks_path == HOOKS_DIR and stays:
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
    reg_name = registration_name(root)
    reg_target = os.path.join(projects.projects_dir(), f"{reg_name}.toml")
    expected = registration_text(reg_name, os.path.realpath(root), _expected_ceiling(root, git))
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
            else:
                sys.stdout.write(f"would remove {target}\n")
        for target, reason in kept:
            sys.stdout.write(f"would keep {target} ({reason})\n")
        return OK if planned else NOTHING
    for target in removed:
        sys.stdout.write(f"removed {target}\n")
    if unset:
        sys.stdout.write("unset core.hooksPath\n")
    for target, reason in kept:
        sys.stdout.write(f"kept {target} ({reason})\n")
    if removed or unset:
        sys.stdout.write(
            f"left for you to delete: {os.path.join(root, settings.PROJECT_FILE)},"
            f" {os.path.join(root, OVERLAY_FILE)}\n"
        )
    else:
        sys.stdout.write("nothing to remove\n")
    return OK if removed or unset else NOTHING


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
        root = resolve_root(args.path or os.getcwd(), gc.git)
    except InitError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE
    remove = production_remove(os.path.realpath(root), os.path.realpath(projects.projects_dir()))
    return run_un(UnOptions(path=args.path, plan=args.plan), remove)


def add_parser(sub):
    """Register `corral init` and `corral uninit` on the top-level subparsers."""
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
    parser.set_defaults(func=cmd_init)
    uninit = sub.add_parser("uninit", help="remove the hooks and registration `corral init` wrote")
    uninit.add_argument(
        "path",
        nargs="?",
        default=None,
        help="the repository to uninit (default: the current directory)",
    )
    uninit.add_argument("--plan", action="store_true", help="print the plan and change nothing")
    uninit.set_defaults(func=cmd_uninit)
