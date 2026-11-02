"""`corral new`: scaffold a minimal project, then onboard it through `init`.

Tickets 1 and 2 of `docs/06-corral-new.md`, section 8: templates plus
`--plan`, then the `init` writer. `new` creates the target directory,
runs `git init -b` with the effective branch, copies the packaged
template files, then onboards the tree through `init.collect` and
`init.apply` with the same flags `init` takes, commits the result, and
reports the next step. Every scaffold write is create-only: missing
files are written, identical files are kept, and a file that differs
from the render refuses (exit 4) unless `--force` leaves it alone and
converges the rest. Later tickets wire the forge side
(`--create-remote`), the label bootstrap, and `--template-dir`.
"""

import argparse
import os
import re
import stat
import sys
from importlib import resources
from typing import NamedTuple

from corral import gc, init, paths

OK, NOTHING, USAGE = 0, 1, 4


class NewError(ValueError):
    """A usage or configuration error (exit 4)."""


#: The only placeholder templates may hold: `{{name}}`, the project name.
PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")


def _template_root():
    """The packaged templates directory, read with `importlib.resources`."""
    return resources.files("corral").joinpath("templates")


def names():
    """The packaged template names, sorted. Every reader goes through here.

    `__pycache__` directories never name a template: they are interpreter
    residue, not author content.
    """
    try:
        entries = [entry for entry in _template_root().iterdir() if entry.is_dir()]
    except OSError as err:
        raise NewError(f"corral: cannot list the packaged templates: {err}") from None
    return sorted(entry.name for entry in entries if entry.name != "__pycache__")


def files(template):
    """The template's raw files: `relpath -> bytes`, sorted by path.

    The file set is the template directory recursively, minus the
    `__pycache__` directories the interpreter may leave behind: what the
    author puts there is what lands. An unknown name is exit 4 listing
    the known names.
    """
    known = names()
    if template not in known:
        listing = ", ".join(known) if known else "(none)"
        raise NewError(f"unknown template {template!r}: want one of {listing}")
    root = _template_root().joinpath(template)
    found: dict[str, bytes] = {}

    def visit(node, rel):
        try:
            entries = sorted(node.iterdir(), key=lambda entry: entry.name)
        except OSError as err:
            raise NewError(f"corral: cannot read packaged template {template!r}: {err}") from None
        for entry in entries:
            if entry.name == "__pycache__":
                continue
            child = f"{rel}/{entry.name}" if rel else entry.name
            try:
                is_dir = entry.is_dir()
            except OSError as err:
                raise NewError(
                    f"corral: cannot read packaged template {template!r}: {err}"
                ) from None
            if is_dir:
                visit(entry, child)
            else:
                try:
                    found[child] = entry.read_bytes()
                except OSError as err:
                    raise NewError(
                        f"corral: cannot read packaged template {template!r}: {err}"
                    ) from None

    visit(root, "")
    return found


def substitute(relpath, text, name):
    """`text` with every `{{name}}` replaced. Anything else `{{...}}` is exit 4."""
    parts = []
    pos = 0
    for match in PLACEHOLDER_RE.finditer(text):
        if match.group(1) != "name":
            raise NewError(
                f"{relpath}: unknown placeholder {match.group(0)!r}:"
                " only '{{name}}' is substituted"
            )
        parts.append(text[pos : match.start()])
        parts.append(name)
        pos = match.end()
    parts.append(text[pos:])
    return "".join(parts)


def render(template, name):
    """The template's files rendered for `name`: `relpath -> bytes`.

    UTF-8-decodable files go through strict `{{name}}` substitution;
    anything else is copied byte-identical.
    """
    out: dict[str, bytes] = {}
    for rel, blob in files(template).items():
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            out[rel] = blob
        else:
            out[rel] = substitute(rel, text, name).encode("utf-8")
    return out


class Options(NamedTuple):
    """Parsed `corral new` flags."""

    name: str
    path: str | None = None
    template: str = "blank"
    plan: bool = False
    force: bool = False
    default_branch: str | None = None
    sets: tuple = ()
    test_cmd: str | None = None
    runtime: str | None = None
    cap: int | None = None
    ops: str | None = None
    ops_name: str | None = None


def _refuse_non_directory(target):
    """Exit 4 when `target` exists but is not a directory.

    Both `run` and `--plan` go through here, so the plan never promises
    a `git init` the real run would refuse.
    """
    if os.path.lexists(target) and not os.path.isdir(target):
        raise NewError(f"not a directory: {target}")


def _present_differs(target, rel, want):
    """True when the scaffold path exists and differs from the render.

    Anything that is not a regular file holding the rendered bytes (a
    directory, a symlink, a fifo, different content) fails the ownership
    proof. The path is never followed or opened in that case: `lstat`
    decides without touching what the link points at or blocking on a
    fifo.
    """
    full = os.path.join(target, rel)
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return False
    except OSError as err:
        raise NewError(f"cannot stat {full}: {err}") from None
    if not stat.S_ISREG(st.st_mode):
        return True
    try:
        with open(full, "rb") as handle:
            current = handle.read()
    except OSError as err:
        raise NewError(f"corral: cannot read {full}: {err}") from None
    return current != want


def _needs_init(target, git):
    """True when `target` is not itself the root of a git repository."""
    try:
        proc = git(["rev-parse", "--show-toplevel"], target)
    except OSError as err:
        raise NewError(f"corral: cannot run git in {target}: {err}") from None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return True
    root = (getattr(proc, "stdout", "") or "").strip()
    if not root:
        return True
    return os.path.realpath(root) != os.path.realpath(target)


def _check_writable(target, rel):
    """Exit 4 when the scaffold path cannot take an exclusive create.

    A symlink or a non-regular file already at the path is refused, and
    a parent directory escaping `target` through a symlink is refused
    too. Runs before `git init` (so a refusal leaves nothing behind)
    and again inside `_write_file` (so a file appearing between the two
    still fails instead of being clobbered).
    """
    full = os.path.join(target, rel)
    parent = os.path.dirname(full)
    if parent:
        real_target = os.path.realpath(target)
        real_parent = os.path.realpath(parent)
        if real_parent != real_target and not real_parent.startswith(real_target + os.sep):
            raise NewError(f"refusing to write {full}: escapes {target}")
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return
    except OSError as err:
        raise NewError(f"cannot stat {full}: {err}") from None
    if stat.S_ISLNK(st.st_mode):
        raise NewError(f"refusing to write {full}: is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise NewError(f"refusing to write {full}: not a regular file")
    raise NewError(f"refusing to write {full}: already exists")


def _write_file(target, rel, blob):
    """One scaffold file, created exclusively. Failures are exit 4.

    Parent directories are created, the path is re-checked (a file
    appearing after the pre-check still fails), and `O_CREAT | O_EXCL`
    opens the file, so nothing already there is ever clobbered and no
    write ever lands outside the target.
    """
    full = os.path.join(target, rel)
    parent = os.path.dirname(full)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise NewError(f"corral: cannot create {parent}: {err}") from None
    _check_writable(target, rel)
    try:
        fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise NewError(f"refusing to write {full}: already exists") from None
    except OSError as err:
        raise NewError(f"corral: cannot write {full}: {err}") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
    except OSError as err:
        raise NewError(f"corral: cannot write {full}: {err}") from None


def _onboard_inputs(target, opts, git):
    """`(ops_root, inputs, split, ops_created, ops_fresh)` for onboarding `target`.

    The `--set` flags pass through; when `--default-branch` supplied the
    flag, its value is injected as `--set clone.default_branch=`, so the
    checked-out branch and `corral.toml` can never disagree. Collect
    takes no other input from `new` and changes no `init` behavior.
    """
    onboard_sets = list(opts.sets)
    if opts.default_branch is not None:
        onboard_sets.append(f"clone.default_branch={opts.default_branch}")
    candidate = init.ops_candidate(target, opts.ops, opts.ops_name)
    ops_created = candidate is not None and not os.path.exists(candidate)
    if candidate is None:
        ops_root, split, ops_fresh = target, False, False
    else:
        ops_root, ops_fresh = init.ensure_ops_root(candidate, git, opts.plan)
        split = os.path.realpath(ops_root) != os.path.realpath(target)
    _, inputs = init.collect(
        target,
        git=git,
        test_cmd=opts.test_cmd,
        runtime=opts.runtime,
        cap=opts.cap,
        sets=tuple(onboard_sets),
        clone_path="." if not split else os.path.realpath(target),
    )
    return ops_root, inputs, split, ops_created, ops_fresh


def _dirty(target, git):
    """True when `git status --porcelain` reports changes. Failures are exit 4."""
    try:
        proc = git(["status", "--porcelain"], target)
    except OSError as err:
        raise NewError(f"cannot run git in {target}: {err}") from None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        raise NewError(f"cannot git status {target}")
    return bool((getattr(proc, "stdout", "") or "").strip())


def _commit(target, name, git):
    """Commit the onboarded tree; the message goes through the installed hook."""
    message = f"chore: scaffold the {name} project"
    try:
        add = git(["add", "-A"], target)
    except OSError as err:
        raise NewError(f"cannot run git in {target}: {err}") from None
    if add is None or getattr(add, "returncode", 1) != 0:
        raise NewError(f"cannot git add {target}")
    try:
        made = git(["commit", "-m", message], target)
    except OSError as err:
        raise NewError(f"cannot run git in {target}: {err}") from None
    if made is None or getattr(made, "returncode", 1) != 0:
        raise NewError(f"cannot git commit {target}")
    sys.stdout.write(f"committed in {target}: {message}\n")


def _kept_line(target, rel, template):
    return (
        f"kept {os.path.join(target, rel)} (differs from the {template}"
        " template render; left alone)\n"
    )


def run(opts, git=gc.git):
    """Run `new` with an injectable git seam. Returns the exit code."""
    try:
        return _apply(opts, git)
    except (NewError, init.InitError) as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE


def _apply(opts, git):
    """Create the tree, init the repo, write the scaffold, onboard, commit."""
    if not paths.plain_repo_name(opts.name):
        raise NewError(f"bad project name {opts.name!r}: want one path part without a .git suffix")
    rendered = render(opts.template, opts.name)
    branch = init.resolve_branch(None, flag=opts.default_branch, sets=opts.sets)
    parent = os.path.abspath(opts.path) if opts.path else os.getcwd()
    target = os.path.join(parent, opts.name)
    _refuse_non_directory(target)
    if opts.plan:
        return _plan(target, rendered, branch, opts, git)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as err:
        raise NewError(f"corral: cannot create {target}: {err}") from None
    differs = [rel for rel, want in rendered.items() if _present_differs(target, rel, want)]
    if differs:
        first = os.path.join(target, differs[0])
        if not opts.force:
            raise NewError(
                f"{first} differs from the {opts.template} template render:"
                " re-run with --force to leave it alone and converge the rest"
            )
        kept = differs
    else:
        kept = []
    missing = [rel for rel in rendered if not os.path.exists(os.path.join(target, rel))]
    for rel in missing:
        _check_writable(target, rel)
    needs_init = _needs_init(target, git)
    scaffold_changed = bool(missing) or needs_init
    if needs_init:
        try:
            made = git(["init", "-b", branch], target)
        except OSError as err:
            raise NewError(f"corral: cannot run git in {target}: {err}") from None
        if made is None or getattr(made, "returncode", 1) != 0:
            raise NewError(f"cannot git init {target}")
        sys.stdout.write(f"ran git init -b {branch} in {target}\n")
    for rel in missing:
        full = os.path.join(target, rel)
        _write_file(target, rel, rendered[rel])
        sys.stdout.write(f"wrote {full}\n")
    for rel in kept:
        sys.stdout.write(_kept_line(target, rel, opts.template))
    ops_root, inputs, split, ops_created, ops_fresh = _onboard_inputs(target, opts, git)
    init_code = init.apply(
        ops_root,
        target,
        inputs,
        init.Options(plan=False, force=opts.force),
        git,
        split,
        ops_created,
        ops_fresh,
    )
    if init_code == USAGE:
        return USAGE
    committed = False
    if _dirty(target, git):
        _commit(target, opts.name, git)
        committed = True
    sys.stdout.write("next: corral doctor\n")
    if scaffold_changed or init_code == OK or committed:
        return OK
    return NOTHING


def _plan(target, rendered, branch, opts, git):
    """Print the full diff of every file `new` would write, and write nothing."""
    _refuse_non_directory(target)
    if os.path.isdir(target):
        differs = [rel for rel, want in rendered.items() if _present_differs(target, rel, want)]
        if differs and not opts.force:
            first = os.path.join(target, differs[0])
            raise NewError(
                f"{first} differs from the {opts.template} template render:"
                " re-run with --force to leave it alone and converge the rest"
            )
        kept = differs if opts.force else []
        missing = [rel for rel in rendered if not os.path.exists(os.path.join(target, rel))]
        for rel in missing:
            _check_writable(target, rel)
        needs_init = _needs_init(target, git)
    else:
        kept, missing, needs_init = [], sorted(rendered), True
    if needs_init:
        sys.stdout.write(f"would run git init -b {branch} in {target}\n")
    for rel in missing:
        try:
            text = rendered[rel].decode("utf-8")
        except UnicodeDecodeError:
            sys.stdout.write(f"would write {os.path.join(target, rel)} (binary)\n")
        else:
            sys.stdout.write(init.plan_diff(None, text, rel))
    for rel in kept:
        sys.stdout.write(_kept_line(target, rel, opts.template).replace("kept ", "would keep ", 1))
    if os.path.isdir(target) and not needs_init:
        ops_root, inputs, split, ops_created, ops_fresh = _onboard_inputs(target, opts, git)
        init_code = init.apply(
            ops_root,
            target,
            inputs,
            init.Options(plan=True, force=opts.force),
            git,
            split,
            ops_created,
            ops_fresh,
        )
        dirty = _dirty(target, git)
        would_commit = bool(missing) or init_code == OK or dirty
        changed = would_commit
    else:
        init.ops_candidate(target, opts.ops, opts.ops_name)
        sys.stdout.write(
            f"would onboard {target} through corral init"
            " (collect, corral.toml, hooks, registration)\n"
        )
        sys.stdout.write("next: corral github setup\n")
        would_commit, changed = True, True
    if would_commit:
        sys.stdout.write(f"would commit chore: scaffold the {opts.name} project in {target}\n")
    sys.stdout.write("next: corral doctor\n")
    if not changed:
        if not kept:
            sys.stdout.write(f"{target} is up to date\n")
        return NOTHING
    return OK


def cmd_new(args):
    """The `corral new` verb: flags to `Options`, then `run`."""
    return run(
        Options(
            name=args.name,
            path=args.path,
            template=args.template,
            plan=args.plan,
            force=args.force,
            default_branch=args.default_branch,
            sets=tuple(args.set),
            test_cmd=args.test_cmd,
            runtime=args.runtime,
            cap=args.cap,
            ops=args.ops,
            ops_name=args.ops_name,
        )
    )


def add_parser(sub) -> None:
    """Register `corral new` on the top-level subparsers."""
    parser = sub.add_parser("new", help="scaffold a minimal project directory")
    parser.add_argument("name", help="the project and repository name")
    parser.add_argument(
        "--path",
        default=None,
        help="the parent directory for the new project (default: the current directory)",
    )
    parser.add_argument(
        "--template",
        default="blank",
        help="the packaged template to scaffold from (default: blank)",
    )
    parser.add_argument("--plan", action="store_true", help="print the diff and write nothing")
    parser.add_argument(
        "--force",
        action="store_true",
        help="leave differing scaffold files alone instead of refusing (never overwrites them)",
    )
    parser.add_argument(
        "--default-branch",
        default=None,
        help="the initial branch (default: the clone.default_branch setting)",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="key=value",
        help="override a setting (repeatable)",
    )
    parser.add_argument("--test-cmd", default=None, help="the test command (default: detected)")
    parser.add_argument("--runtime", default=None, help="the agent runtime")
    parser.add_argument("--cap", type=int, default=None, help="this project's agent ceiling")
    parser.add_argument(
        "--ops",
        nargs="?",
        const=init.OPS_DEFAULT,
        default=None,
        metavar="PATH",
        help="write the agent side to the ops root at PATH"
        " (bare --ops uses the <name>_ops sibling)",
    )
    parser.add_argument(
        "--ops-name",
        default=None,
        metavar="NAME",
        help="write the agent side to the NAME sibling of the clone (--ops wins)",
    )
    parser.set_defaults(func=cmd_new)
