"""`corral new`: scaffold a minimal project directory, locally only.

Ticket 1 of `docs/06-corral-new.md`, section 8: templates plus `--plan`.
`new` creates the target directory, runs `git init -b` with the
effective branch, and copies the packaged template files. Every write is
create-only: missing files are written, identical files are kept, and a
file that differs from the render refuses (exit 4) unless `--force`
leaves it alone and converges the rest. Later tickets wire `init`
(onboarding, the initial commit, the next-step report), the forge side
(`--create-remote`), the label bootstrap, and `--template-dir`.
"""

import argparse
import os
import re
import sys
from importlib import resources
from typing import NamedTuple

from corral import gc, paths, settings
from corral.init import plan_diff

OK, NOTHING, USAGE = 0, 1, 4


class NewError(ValueError):
    """A usage or configuration error (exit 4)."""


#: The only placeholder templates may hold: `{{name}}`, the project name.
PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")


def _template_root():
    """The packaged templates directory, read with `importlib.resources`."""
    return resources.files("corral").joinpath("templates")


def names():
    """The packaged template names, sorted. Every reader goes through here."""
    try:
        entries = [entry for entry in _template_root().iterdir() if entry.is_dir()]
    except OSError as err:
        raise NewError(f"corral: cannot list the packaged templates: {err}") from None
    return sorted(entry.name for entry in entries if entry.name != "__pycache__")


def files(template):
    """The template's raw files: `relpath -> bytes`, sorted by path.

    The file set is the template directory recursively, minus nothing.
    An unknown name is exit 4 listing the known names.
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
            if entry.is_dir():
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


def default_branch():
    """The branch `git init -b` uses: the registry default for `clone.default_branch`.

    The same fallback `init.collect` uses, so the two agree when the
    default changes.
    """
    settings._load_providers()
    try:
        default = settings.REGISTRY["clone.default_branch"].default
    except KeyError:
        raise NewError("internal: clone.default_branch is not registered") from None
    if not isinstance(default, str) or not default:
        raise NewError("internal: clone.default_branch has no usable default")
    return default


class Options(NamedTuple):
    """Parsed `corral new` flags."""

    name: str
    path: str | None = None
    template: str = "blank"
    plan: bool = False
    force: bool = False


def _present_differs(target, rel, want):
    """True when a file at the scaffold path exists and differs from the render."""
    full = os.path.join(target, rel)
    if not os.path.exists(full):
        return False
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


def _write_file(full, blob):
    """One scaffold file, creating parent directories. Failures are exit 4."""
    parent = os.path.dirname(full)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise NewError(f"corral: cannot create {parent}: {err}") from None
    try:
        with open(full, "wb") as handle:
            handle.write(blob)
    except OSError as err:
        raise NewError(f"corral: cannot write {full}: {err}") from None


def _kept_line(target, rel, template):
    return (
        f"kept {os.path.join(target, rel)} (differs from the {template}"
        " template render; left alone)\n"
    )


def run(opts, git=gc.git):
    """Run `new` with an injectable git seam. Returns the exit code."""
    try:
        return _apply(opts, git)
    except NewError as err:
        print(f"corral: {err}", file=sys.stderr)
        return USAGE


def _apply(opts, git):
    """Create the tree, init the repo, write the missing scaffold files."""
    if not paths.plain_repo_name(opts.name):
        raise NewError(f"bad project name {opts.name!r}: want one path part without a .git suffix")
    rendered = render(opts.template, opts.name)
    branch = default_branch()
    parent = os.path.abspath(opts.path) if opts.path else os.getcwd()
    target = os.path.join(parent, opts.name)
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
    needs_init = _needs_init(target, git)
    if not missing and not needs_init:
        for rel in kept:
            sys.stdout.write(_kept_line(target, rel, opts.template))
        if not kept:
            sys.stdout.write(f"{target} is up to date\n")
        return NOTHING
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
        _write_file(full, rendered[rel])
        sys.stdout.write(f"wrote {full}\n")
    for rel in kept:
        sys.stdout.write(_kept_line(target, rel, opts.template))
    return OK


def _plan(target, rendered, branch, opts, git):
    """Print the full diff of every file `new` would write, and write nothing."""
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
            sys.stdout.write(plan_diff(None, text, rel))
    for rel in kept:
        sys.stdout.write(_kept_line(target, rel, opts.template).replace("kept ", "would keep ", 1))
    if not missing and not needs_init:
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
    parser.set_defaults(func=cmd_new)
