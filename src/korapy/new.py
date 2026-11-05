"""`korapy new`: scaffold a minimal project, then onboard it through `init`.

Tickets 1 to 5 of `docs/06-korapy-new.md`, section 8: templates plus
`--plan`, then the `init` writer, then `--create-remote` through the
forge interface, then the label bootstrap through the tracker
interface, then user template directories. `new` creates the target
directory, runs `git init -b` with the effective branch, copies the
template files (packaged, or a user directory with `--template-dir`),
then onboards the tree through `init.collect` and `init.apply` with the
same flags `init` takes, commits the result, and reports the next step.
Every scaffold write is create-only: missing files are written,
identical files are kept, and a file that differs from the render
refuses (exit 4) unless `--force` leaves it alone and converges the
rest. With `--create-remote`, `new` pre-checks the intended remote
through the forge, creates it after the commit, sets it as origin,
bootstraps the board labels through the tracker, then ensures the
board and writes its mapping (the same operation `korapy github setup`
performs, leaving the new entry on the inert labels variant); without
it, `new` prints the planned create command and runs nothing outward.
"""

import argparse
import os
import re
import stat
import sys
from importlib import resources
from pathlib import Path
from typing import NamedTuple

from korapy import forge as iface
from korapy import gc, init, paths, settings
from korapy import tracker as tracker_iface

OK, NOTHING, RACE, UNREACHABLE, USAGE = 0, 1, 2, 3, 4


class NewError(ValueError):
    """A usage or configuration error (exit 4)."""


#: The only placeholder templates may hold: `{{name}}`, the project name.
PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")

#: The packaged template used when neither `--template` nor
#: `--template-dir` names a source.
DEFAULT_TEMPLATE = "blank"


def _template_root():
    """The packaged templates directory, read with `importlib.resources`."""
    return resources.files("korapy").joinpath("templates")


def names():
    """The packaged template names, sorted. Every reader goes through here.

    `__pycache__` directories never name a template: they are interpreter
    residue, not author content.
    """
    try:
        entries = [entry for entry in _template_root().iterdir() if entry.is_dir()]
    except OSError as err:
        raise NewError(f"korapy: cannot list the packaged templates: {err}") from None
    return sorted(entry.name for entry in entries if entry.name != "__pycache__")


def _collect(node, rel, found, label, *, root=None):
    """Every file under `node` into `found`, keyed by path relative to it.

    `label` names the source in failures. `__pycache__` directories are
    skipped: interpreter residue, never author content. A symlink entry
    is refused (exit 4 naming it), like the write path's symlink
    refusal: following one could loop forever on a self-referential
    link or copy bytes from outside the template directory.
    """
    root = node if root is None else root
    try:
        entries = sorted(node.iterdir(), key=lambda entry: entry.name)
    except OSError as err:
        raise NewError(f"korapy: cannot read {label}: {err}") from None
    for entry in entries:
        if entry.name == "__pycache__":
            continue
        child = f"{rel}/{entry.name}" if rel else entry.name
        try:
            is_link = entry.is_symlink()
        except AttributeError:
            is_link = False
        except OSError as err:
            raise NewError(f"korapy: cannot read {label}: {err}") from None
        if is_link:
            raise NewError(
                f"korapy: cannot read {label}: {child} is a symlink: refusing to follow it"
            )
        if isinstance(entry, Path):
            real_root = os.path.realpath(root)
            real_entry = os.path.realpath(entry)
            if real_entry != real_root and not real_entry.startswith(real_root + os.sep):
                raise NewError(
                    f"korapy: cannot read {label}: {child} escapes the template directory"
                )
        try:
            is_dir = entry.is_dir()
        except OSError as err:
            raise NewError(f"korapy: cannot read {label}: {err}") from None
        if is_dir:
            _collect(entry, child, found, label, root=root)
        else:
            try:
                found[child] = entry.read_bytes()
            except OSError as err:
                raise NewError(f"korapy: cannot read {label}: {err}") from None


def files(template):
    """The template's raw files: `relpath -> bytes`, sorted by path.

    The file set is the template directory recursively: what the
    author puts there is what lands. An unknown name is exit 4 listing
    the known names.
    """
    known = names()
    if template not in known:
        listing = ", ".join(known) if known else "(none)"
        raise NewError(f"unknown template {template!r}: want one of {listing}")
    label = f"packaged template {template!r}"
    found: dict[str, bytes] = {}
    _collect(_template_root().joinpath(template), "", found, label)
    return found


def user_files(directory):
    """A user template directory's raw files: `relpath -> bytes`.

    Copied like a packaged template, with the same file set rules and
    the same strict `{{name}}` substitution. A missing path, a
    non-directory, or an unreadable entry is exit 4.
    """
    try:
        is_dir = Path(directory).is_dir()
    except OSError as err:
        raise NewError(f"korapy: cannot read user template {directory!r}: {err}") from None
    if not is_dir:
        raise NewError(f"unknown template directory {directory!r}: not a directory")
    label = f"user template {directory!r}"
    found: dict[str, bytes] = {}
    _collect(Path(directory), "", found, label)
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


def _render_blobs(blobs, name):
    """`relpath -> bytes` with strict `{{name}}` substitution applied.

    UTF-8-decodable files go through `substitute`; anything else is
    copied byte-identical.
    """
    out: dict[str, bytes] = {}
    for rel, blob in blobs.items():
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            out[rel] = blob
        else:
            out[rel] = substitute(rel, text, name).encode("utf-8")
    return out


def render(template, name):
    """The template's files rendered for `name`: `relpath -> bytes`."""
    return _render_blobs(files(template), name)


def render_dir(directory, name):
    """A user template directory's files rendered for `name`.

    Same `{{name}}` rule and strictness as a packaged template.
    """
    return _render_blobs(user_files(directory), name)


def _source(opts):
    """`(label, rendered)`: the scaffold source and its files for `name`.

    `--template-dir` copies a user directory with the packaged rules;
    otherwise `--template` picks a packaged template (`blank` when the
    flag is absent). Both flags together are exit 4.
    """
    if opts.template_dir is not None:
        if opts.template is not None:
            raise NewError("--template and --template-dir are mutually exclusive")
        return opts.template_dir, render_dir(opts.template_dir, opts.name)
    template = DEFAULT_TEMPLATE if opts.template is None else opts.template
    return template, render(template, opts.name)


class Options(NamedTuple):
    """Parsed `korapy new` flags."""

    name: str
    path: str | None = None
    template: str | None = None
    template_dir: str | None = None
    plan: bool = False
    force: bool = False
    default_branch: str | None = None
    sets: tuple = ()
    test_cmd: str | None = None
    runtime: str | None = None
    cap: int | None = None
    ops: str | None = None
    ops_name: str | None = None
    create_remote: bool = False
    public: bool = False
    owner: str | None = None
    resume: bool = False


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
        raise NewError(f"korapy: cannot read {full}: {err}") from None
    return current != want


def _needs_init(target, git):
    """True when `target` is not itself the root of a git repository."""
    try:
        proc = git(["rev-parse", "--show-toplevel"], target)
    except OSError as err:
        raise NewError(f"korapy: cannot run git in {target}: {err}") from None
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

    The path is checked, parent directories are created, the path is
    checked again, then `O_CREAT | O_EXCL` opens the file. The checks
    refuse a symlinked parent that exists at the checks; a concurrent
    swap in the operator's own tree is not defended. A file appearing
    between the checks still fails instead of being clobbered.
    """
    full = os.path.join(target, rel)
    _check_writable(target, rel)
    parent = os.path.dirname(full)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as err:
            raise NewError(f"korapy: cannot create {parent}: {err}") from None
    _check_writable(target, rel)
    try:
        fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise NewError(f"refusing to write {full}: already exists") from None
    except OSError as err:
        raise NewError(f"korapy: cannot write {full}: {err}") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
    except OSError as err:
        raise NewError(f"korapy: cannot write {full}: {err}") from None


def _onboard_inputs(target, opts, git):
    """`(ops_root, inputs, split, ops_created, ops_fresh)` for onboarding `target`.

    The `--set` flags pass through; when `--default-branch` supplied the
    flag, its value is injected as `--set clone.default_branch=`, so the
    checked-out branch and `korapy.toml` can never disagree. Collect
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


def _production_forge():
    """The live forge over the production `gh` runner."""
    from korapy.github import forge as ghforge
    from korapy.github.gh import paced_run

    return ghforge.GithubForge(paced_run)


def _production_tracker():
    """The live tracker over the production `gh` runner."""
    from korapy.github import tracker as ghtracker
    from korapy.github.gh import paced_run

    return ghtracker.GithubTracker(paced_run)


def _production_provisioner_factory():
    """The live board provisioner over the production `gh` runner.

    The factory shape is the one `github setup` passes in: called once
    per entry, it builds the provisioner the board ensure runs through.
    """
    from korapy import trackers

    return trackers.production_board_provisioner()


def _resolve_owner(opts, forge):
    """The remote owner: `--owner` when given, else the forge current user.

    Returned unchecked: the one shape check lives in the forge-side
    `describe_create`, which `_plan_text` maps to exit 4. A failed
    current-user call propagates as `ForgeError` (exit 3).
    """
    if opts.owner is not None:
        return opts.owner
    return forge.current_user()


def _plan_text(forge, name, owner, private):
    """The planned create command, printed verbatim.

    Pure: `describe_create` never calls the runner, so this runs before
    any local write and (without `--owner`) without any network read.
    A `ForgeError` here is a shape error (a bad name or owner), so it
    maps to exit 4, never to exit 3.
    """
    try:
        return forge.describe_create(name, owner, private=private)
    except iface.ForgeError as err:
        raise NewError(f"unresolvable remote owner {owner!r} for {name!r}: {err}") from None


def _remote_precheck(opts, forge, owner):
    """`(repo, plan_text, adopted)`: the remote gate before any local write.

    A present repo refuses (exit 4) unless `--resume` adopts it, and only
    on positive proof that it is empty. Every forge failure propagates
    as `ForgeError` (exit 3); nothing is written in any of these paths.
    """
    plan_text = _plan_text(forge, opts.name, owner, not opts.public)
    repo = f"{owner}/{opts.name}"
    if not forge.repo_exists(repo):
        return repo, plan_text, False
    if not opts.resume:
        raise NewError(f"remote {repo} already exists: refusing to adopt it")
    if not forge.repo_empty(repo):
        raise NewError(f"remote {repo} is not empty: refusing to adopt it")
    return repo, plan_text, True


def _remote_created(repo, forge):
    """Whether the remote exists after a failed create.

    A create that succeeded but whose clone-URL read failed leaves the
    repository behind; a create that never happened leaves nothing. A
    probe that errors reads as not created: without positive proof the
    report stays fail-closed.
    """
    try:
        return bool(forge.repo_exists(repo))
    except iface.ForgeError:
        return False


def _bootstrap_labels(repo, tracker):
    """Ensure the board labels, printing the created/kept/refused report.

    Returns `(changed, refused)`, or None when the tracker is
    unreachable: the manual `github setup` command is printed as the
    next step, and the caller exits 3. A validation rejection (a 422
    the tracker answered) is refused, not unreachable: only the reason
    line is printed here and `(False, True)` is returned, so the caller
    takes its refused branch with the same next step (exit 1).
    """
    try:
        report = tracker.ensure_labels(repo)
    except tracker_iface.TrackerRejected as err:
        sys.stdout.write(f"labels on {repo} not bootstrapped: {err}\n")
        return False, True
    except tracker_iface.TrackerError as err:
        sys.stdout.write(f"labels on {repo} not bootstrapped: {err}\n")
        sys.stdout.write("next: korapy github setup\n")
        return None
    sys.stdout.write(tracker_iface.render_ensure(repo, report))
    return bool(report.created), bool(report.refused)


def _commit_mapping(target, entry_name, toml_target, git):
    """Commit exactly the new board mapping after a successful bootstrap.

    The scaffold commit already ran, so the mapping write dirtied the
    tree; a follow-up commit of exactly that file leaves the tree clean
    with the mapping recorded. In the split layout the mapping lives
    outside the new project's tree (the ops root owns it), so there is
    nothing to commit here. Failures are exit 4.
    """
    rel = os.path.relpath(toml_target, target)
    if rel == ".." or rel.startswith(".." + os.sep):
        return
    message = f"chore: record the {entry_name} board mapping"
    try:
        add = git(["add", "--", rel], target)
    except OSError as err:
        raise NewError(f"cannot run git in {target}: {err}") from None
    if add is None or getattr(add, "returncode", 1) != 0:
        raise NewError(f"cannot git add {toml_target} in {target}")
    try:
        made = git(["commit", "-m", message], target)
    except OSError as err:
        raise NewError(f"cannot run git in {target}: {err}") from None
    if made is None or getattr(made, "returncode", 1) != 0:
        raise NewError(f"cannot git commit {target}")
    sys.stdout.write(f"committed {rel} in {target}: {message}\n")


def _bootstrap_board(repo, entry_name, ops_root, provisioner_factory, target, git):
    """Ensure the board for a fresh `--create-remote` repo and write its mapping.

    The same ensure operation `korapy github setup` runs: the spec comes
    from `effective_spec` over the new labels entry (no table yet, so the
    board is found by title or created, and the table stays inert), and
    the provisioner comes from the injected factory. Returns
    `(changed, refused)`, or None when the board is unreachable: the
    manual `github setup` command is printed as the next step, and the
    caller exits 3. A refused provision writes nothing (the mapping would
    omit refused concepts) and returns `(changed, True)` with the same
    next step, so the caller exits 1 leaving a clean tree with no
    mapping. On success the mapping is committed as a follow-up of
    exactly that file, after the scaffold commit. A spec or adapter
    `ValueError` is a usage error, raised as `NewError` (exit 4). The
    mapping write raises `init.InitError` (exit 4) the same way.
    """
    try:
        spec = tracker_iface.effective_spec(variant="labels", board=None, repo=repo)
    except ValueError as err:
        raise NewError(f"korapy: {err}") from None
    try:
        provisioner = provisioner_factory()
    except ValueError as err:
        raise NewError(f"korapy: {err}") from None
    ensure = getattr(provisioner, "ensure_board", None)
    if not callable(ensure):
        raise NewError("korapy: this tracker does not provision boards")
    try:
        provision = ensure(spec)
    except ValueError as err:
        raise NewError(f"korapy: {err}") from None
    except tracker_iface.TrackerError as err:
        sys.stdout.write(f"board on {repo} not bootstrapped: {err}\n")
        sys.stdout.write("next: korapy github setup\n")
        return None
    sys.stdout.write(tracker_iface.render_provision(repo, provision))
    if provision.refused:
        return bool(provision.created), True
    toml_target = os.path.join(ops_root, settings.PROJECT_FILE)
    new_text, noop = init.board_table_change(toml_target, entry_name, spec, provision)
    if not noop:
        try:
            with open(toml_target, "w", encoding="utf-8") as handle:
                handle.write(new_text)
        except OSError as err:
            raise NewError(f"korapy: cannot write {toml_target}: {err}") from None
        sys.stdout.write(f"wrote [repo.board] for {entry_name}\n")
        _commit_mapping(target, entry_name, toml_target, git)
    return bool(provision.created) or not noop, False


def _ensure_origin(target, url, git):
    """Set `origin` to `url` in `target`, unless it already points there.

    An origin pointing elsewhere is refused (exit 4): overwriting it
    could repoint a foreign checkout. Returns whether anything changed.
    """
    try:
        proc = git(["remote", "get-url", "origin"], target)
    except OSError as err:
        raise NewError(f"cannot run git in {target}: {err}") from None
    if proc is not None and getattr(proc, "returncode", 1) == 0:
        current = (getattr(proc, "stdout", "") or "").strip()
        if current == url:
            return False
        raise NewError(f"origin in {target} already points at {current}: refusing to overwrite it")
    try:
        added = git(["remote", "add", "origin", url], target)
    except OSError as err:
        raise NewError(f"cannot run git in {target}: {err}") from None
    if added is None or getattr(added, "returncode", 1) != 0:
        raise NewError(f"cannot set origin in {target} to {url}")
    sys.stdout.write(f"set origin to {url} in {target}\n")
    return True


def run(opts, git=gc.git, forge=None, tracker=None, provisioner_factory=None):
    """Run `new` with injectable seams. Returns the exit code."""
    try:
        return _apply(
            opts,
            git,
            forge if forge is not None else _production_forge(),
            tracker if tracker is not None else _production_tracker(),
            provisioner_factory
            if provisioner_factory is not None
            else _production_provisioner_factory,
        )
    except iface.ForgeExists as err:
        print(f"korapy: {err}", file=sys.stderr)
        return RACE
    except iface.ForgeError as err:
        print(f"korapy: {err}", file=sys.stderr)
        return UNREACHABLE
    except (NewError, init.InitError) as err:
        print(f"korapy: {err}", file=sys.stderr)
        return USAGE


def _apply(opts, git, forge, tracker, provisioner_factory):
    """Create the tree, init the repo, write the scaffold, onboard, commit."""
    if opts.public and not opts.create_remote:
        raise NewError("--public needs --create-remote: it would mean nothing")
    if opts.resume and not opts.create_remote:
        raise NewError("--resume needs --create-remote: it would mean nothing")
    if not paths.plain_repo_name(opts.name):
        raise NewError(f"bad project name {opts.name!r}: want one path part without a .git suffix")
    label, rendered = _source(opts)
    branch = init.resolve_branch(None, flag=opts.default_branch, sets=opts.sets)
    if opts.create_remote:
        owner = _resolve_owner(opts, forge)
        repo, plan_text, adopted = _remote_precheck(opts, forge, owner)
    else:
        owner, repo, adopted = None, None, False
        plan_text = _plan_text(forge, opts.name, opts.owner, True)
    parent = os.path.abspath(opts.path) if opts.path else os.getcwd()
    target = os.path.join(parent, opts.name)
    _refuse_non_directory(target)
    if opts.plan:
        return _plan(target, rendered, label, branch, opts, git, plan_text, repo, adopted)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as err:
        raise NewError(f"korapy: cannot create {target}: {err}") from None
    differs = [rel for rel, want in rendered.items() if _present_differs(target, rel, want)]
    if differs:
        first = os.path.join(target, differs[0])
        if not opts.force:
            raise NewError(
                f"{first} differs from the {label} template render:"
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
            raise NewError(f"korapy: cannot run git in {target}: {err}") from None
        if made is None or getattr(made, "returncode", 1) != 0:
            raise NewError(f"cannot git init {target}")
        sys.stdout.write(f"ran git init -b {branch} in {target}\n")
    for rel in missing:
        full = os.path.join(target, rel)
        _write_file(target, rel, rendered[rel])
        sys.stdout.write(f"wrote {full}\n")
    for rel in kept:
        sys.stdout.write(_kept_line(target, rel, label))
    if not opts.create_remote:
        sys.stdout.write(plan_text + "\n")
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
    remote_changed = False
    origin_changed = False
    if opts.create_remote:
        if adopted:
            try:
                url = forge.clone_url(repo)
            except iface.ForgeError as err:
                sys.stdout.write(f"remote {repo} adopted but not wired: {err}\n")
                sys.stdout.write("next: re-run with --resume to adopt it\n")
                return UNREACHABLE
            sys.stdout.write(f"adopted empty remote {repo}\n")
        else:
            try:
                url = forge.create_repo(owner, opts.name, private=not opts.public)
            except iface.ForgeExists as err:
                print(
                    f"korapy: remote {repo} appeared during setup (race lost): {err}."
                    " The repository may exist but be invisible to this account:"
                    " check `gh auth status`",
                    file=sys.stderr,
                )
                return RACE
            except iface.ForgeError as err:
                if _remote_created(repo, forge):
                    sys.stdout.write(f"remote {repo} created but not wired: {err}\n")
                    sys.stdout.write("next: re-run with --resume to adopt it\n")
                else:
                    sys.stdout.write(f"remote {repo} not created: {err}\n")
                    sys.stdout.write(f"next: {plan_text}\n")
                return UNREACHABLE
            remote_changed = True
            sys.stdout.write(f"created remote {repo}\n")
        origin_changed = _ensure_origin(target, url, git)
        bootstrapped = _bootstrap_labels(repo, tracker)
        if bootstrapped is None:
            return UNREACHABLE
        labels_changed, refused = bootstrapped
        boarded = _bootstrap_board(repo, opts.name, ops_root, provisioner_factory, target, git)
        if boarded is None:
            return UNREACHABLE
        board_changed, board_refused = boarded
        refused = refused or board_refused
    else:
        labels_changed, board_changed, refused = False, False, False
    if refused:
        sys.stdout.write("next: korapy github setup\n")
        return NOTHING
    if (
        scaffold_changed
        or init_code == OK
        or committed
        or remote_changed
        or origin_changed
        or labels_changed
        or board_changed
    ):
        sys.stdout.write("next: korapy doctor\n")
        return OK
    return NOTHING


def _plan(target, rendered, label, branch, opts, git, plan_text, repo, adopted):
    """Print the full diff of every file `new` would write, and write nothing.

    `plan_text` is the planned create command: printed verbatim without
    `--create-remote`, `would run` with it, or `would adopt` when
    `--resume` adopts the remote instead of creating one. The remote
    pre-check already ran in `_apply`, so `--plan` never promises a
    create the real run would refuse.
    """
    _refuse_non_directory(target)
    if os.path.isdir(target):
        differs = [rel for rel, want in rendered.items() if _present_differs(target, rel, want)]
        if differs and not opts.force:
            first = os.path.join(target, differs[0])
            raise NewError(
                f"{first} differs from the {label} template render:"
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
        sys.stdout.write(_kept_line(target, rel, label).replace("kept ", "would keep ", 1))
    if opts.create_remote:
        if adopted:
            sys.stdout.write(f"would adopt {repo} when empty, then set it as origin\n")
        else:
            sys.stdout.write(f"would run {plan_text}\n")
        sys.stdout.write(f"would ensure labels on {repo}\n")
        sys.stdout.write(f"would ensure board on {repo}\n")
    else:
        sys.stdout.write(plan_text + "\n")
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
            f"would onboard {target} through korapy init"
            " (collect, korapy.toml, hooks, registration)\n"
        )
        sys.stdout.write("next: korapy github setup\n")
        would_commit, changed = True, True
    if would_commit:
        sys.stdout.write(f"would commit chore: scaffold the {opts.name} project in {target}\n")
    if not changed:
        if not kept:
            sys.stdout.write(f"{target} is up to date\n")
        return NOTHING
    sys.stdout.write("next: korapy doctor\n")
    return OK


def cmd_new(args):
    """The `korapy new` verb: flags to `Options`, then `run`."""
    return run(
        Options(
            name=args.name,
            path=args.path,
            template=args.template,
            template_dir=args.template_dir,
            plan=args.plan,
            force=args.force,
            default_branch=args.default_branch,
            sets=tuple(args.set),
            test_cmd=args.test_cmd,
            runtime=args.runtime,
            cap=args.cap,
            ops=args.ops,
            ops_name=args.ops_name,
            create_remote=args.create_remote,
            public=args.public,
            owner=args.owner,
            resume=args.resume,
        )
    )


def add_parser(sub) -> None:
    """Register `korapy new` on the top-level subparsers."""
    parser = sub.add_parser("new", help="scaffold a minimal project directory")
    parser.add_argument("name", help="the project and repository name")
    parser.add_argument(
        "--path",
        default=None,
        help="the parent directory for the new project (default: the current directory)",
    )
    parser.add_argument(
        "--template",
        default=None,
        help=f"the packaged template to scaffold from (default: {DEFAULT_TEMPLATE})",
    )
    parser.add_argument(
        "--template-dir",
        default=None,
        metavar="DIR",
        help="scaffold from a user template directory instead of a packaged"
        " template (mutually exclusive with --template)",
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
    parser.add_argument(
        "--create-remote",
        action="store_true",
        help="create the repository on the forge and set it as origin"
        " (default: print the create command and run nothing outward)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="create a public repository (needs --create-remote; private by default)",
    )
    parser.add_argument(
        "--owner",
        default=None,
        metavar="OWNER",
        help="the remote owner (default: the forge current user)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="adopt an existing empty remote instead of refusing (needs --create-remote)",
    )
    parser.set_defaults(func=cmd_new)
