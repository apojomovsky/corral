"""One worktree per ticket: the checkout a ticket's agent works in.

The caller resolves the `git.branch` and `git.worktrees` settings once,
through `corral.settings`, and passes the values in. This module never
reads the registry, so it cannot disagree with the caller about a default.
Every git call goes through the one seam `git`, which runs with
`gitenv.clean_env()`.

`ensure_worktree` picks a ticket's branch: the one that already has a
worktree, else the one ticket branch that exists (local or on origin), else
a branch rendered from the title. Several candidates with no clear winner
are refused, never picked by name. A stale local branch is fast-forwarded to
origin's when it is an ancestor and checked out nowhere; diverged branches
are refused. The worktree lives under the root, which must sit inside the
main clone. Removal belongs to host hygiene, not here.
"""

import os
import re
import subprocess

from corral.gitenv import clean_env

TYPES = ("feat", "fix", "docs", "test", "ci", "build", "chore", "refactor", "perf", "revert")

#: The type used when a title has no Conventional prefix of a known type.
DEFAULT_TYPE = "feat"

#: The longest slug a branch name carries; a longer title is cut at a word.
MAX_SLUG = 40

#: The fallback slug for a title with nothing left after its prefix.
FALLBACK_SLUG = "ticket"

GIT_TIMEOUT_S = 300

TITLE_TYPE_RE = re.compile(r"^([A-Za-z][\w.-]*)(?:\([^)]*\))?!?:")

#: A ticket branch: `<type>/<n>-<slug>`. The type grammar is the one
#: scripts/pre_pr_check.py enforces (lowercase letters); the number has no
#: leading zero, so `feat/5-x` and `feat/50-x` never alias one ticket.
TICKET_BRANCH_RE = re.compile(r"^(?P<type>[a-z]+)/(?P<number>[1-9][0-9]*)-(?P<slug>[\w.-]+)$")


class WorktreeError(RuntimeError):
    """A worktree cannot be made or reused (exit 4, configuration or usage error)."""


def git(args, cwd):
    """The one git seam here: a CompletedProcess; a failed command is not an error."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise WorktreeError(f"cannot run git in {cwd}: {err}") from None


def _tail(proc):
    lines = (proc.stderr or "").strip().splitlines()
    return lines[-1] if lines else "failed"


def _out(run, args, cwd):
    """stdout of a git command that must succeed."""
    proc = run(args, cwd)
    if proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args[:2])} failed in {cwd}: {_tail(proc)}")
    return proc.stdout


def _ok(run, args, cwd):
    return run(args, cwd).returncode == 0


def _check_number(number):
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise WorktreeError(f"ticket number {number!r} must be a positive integer")


def _check_branch(branch, number):
    """The TICKET_BRANCH_RE match for `branch`, which must be ticket `number`'s."""
    match = TICKET_BRANCH_RE.match(branch or "")
    if not match:
        raise WorktreeError(
            f"branch {branch!r} is not <type>/<n>-<slug> (lowercase type, "
            "number without a leading zero)"
        )
    if int(match.group("number")) != number:
        raise WorktreeError(f"branch {branch!r} is not ticket #{number}")
    return match


def _inside(path, parent):
    """True when `path` is strictly below `parent`; both are real paths."""
    if path == parent:
        return False
    return os.path.commonpath([path, parent]) == parent


def root(main_clone, root):
    """The real path of the worktree root. The one resolver for it.

    `root` is the `git.worktrees` setting value, relative to the main
    clone or absolute, and must resolve strictly inside the clone.
    Refused with `WorktreeError` when it escapes.
    """
    return _resolve_root(os.path.realpath(main_clone), root)


def slugify(title):
    """The title without its Conventional prefix, as lowercase hyphenated words.

    Cut at the last whole word within `MAX_SLUG` characters; a single word
    longer than that is cut as is. Falls back to `FALLBACK_SLUG`.
    """
    text = (title or "").strip()
    match = TITLE_TYPE_RE.match(text)
    if match:
        text = text[match.end() :]
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > MAX_SLUG:
        cut = slug[: MAX_SLUG + 1].rfind("-")
        slug = slug[:cut] if cut > 0 else slug[:MAX_SLUG]
    return slug.rstrip("-") or FALLBACK_SLUG


def branch_for(number, title, *, template):
    """The ticket's branch, rendered from `template` (the resolved `git.branch`).

    `{type}` comes from a Conventional title prefix when it is one of
    `TYPES`, else `DEFAULT_TYPE`; `{issue}` is the number and `{slug}` is
    `slugify(title)`. The rendered name must match TICKET_BRANCH_RE for
    this number, else WorktreeError.
    """
    _check_number(number)
    match = TITLE_TYPE_RE.match((title or "").strip())
    kind = match.group(1).lower() if match else ""
    if kind not in TYPES:
        kind = DEFAULT_TYPE
    try:
        branch = template.format(type=kind, issue=number, slug=slugify(title))
    except (KeyError, IndexError, ValueError) as err:
        raise WorktreeError(f"git.branch {template!r} is not a valid template: {err}") from None
    _check_branch(branch, number)
    return branch


def parse_worktree_list(text):
    """{abspath: branch} from `git worktree list --porcelain`.

    A detached or bare worktree maps to "".
    """
    trees, path = {}, None
    for line in (text or "").splitlines():
        if line.startswith("worktree "):
            path = os.path.normpath(line[len("worktree ") :].strip())
            trees[path] = ""
        elif line.startswith("branch refs/heads/") and path is not None:
            trees[path] = line[len("branch refs/heads/") :].strip()
    return trees


def ticket_branches(number, names):
    """The names among `names` that are ticket `number`'s branches, sorted."""
    out = []
    for name in names:
        match = TICKET_BRANCH_RE.match(name.strip())
        if match and int(match.group("number")) == number:
            out.append(name.strip())
    return sorted(out)


def _resolve_root(main, root):
    """The real path of the worktree root; it must sit strictly inside the clone."""
    absolute = root if os.path.isabs(root) else os.path.join(main, root)
    real = os.path.realpath(absolute)
    if not _inside(real, main):
        raise WorktreeError(
            f"worktree root {root!r} resolves to {real}, outside the main clone {main}"
        )
    return real


def _ticket_names(number, run, main):
    """Ticket `number`'s branches, local or on origin, sorted and unique."""
    local = _out(run, ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], main)
    remote = _out(
        run,
        ["for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin/"],
        main,
    )
    return sorted(set(ticket_branches(number, local.splitlines() + remote.splitlines())))


def _sync_local(branch, checked_out, main, run):
    """Bring a stale local `branch` up to origin's, when that is safe.

    A branch checked out anywhere is never moved. A local branch that is an
    ancestor of origin's is fast-forwarded; one ahead of origin is kept; one
    that has diverged is refused.
    """
    if branch in checked_out:
        return
    if not _ok(run, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], main):
        return
    if not _ok(run, ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], main):
        return
    local = _out(run, ["rev-parse", f"refs/heads/{branch}"], main).strip()
    remote = _out(run, ["rev-parse", f"refs/remotes/origin/{branch}"], main).strip()
    if local == remote:
        return
    if _ok(run, ["merge-base", "--is-ancestor", local, remote], main):
        _out(run, ["branch", "-f", branch, remote], main)
        return
    if _ok(run, ["merge-base", "--is-ancestor", remote, local], main):
        return
    raise WorktreeError(
        f"local {branch} and origin/{branch} have diverged in {main}: reconcile them, then retry"
    )


def ensure_worktree(
    main_clone,
    number,
    *,
    default_branch,
    branch_template,
    root,
    title=None,
    branch=None,
    git=git,
):
    """``(workdir, branch)`` for ticket `number`, created if it does not exist.

    The caller resolves `branch_template` (`git.branch`) and `root`
    (`git.worktrees`) through `corral.settings` and passes them in. `root`
    is relative to `main_clone` or absolute, and must resolve inside it.
    `branch` pins the branch and must be ticket `number`'s. Without it, the
    ticket's branch is the one that already has a worktree; failing that the
    only ticket branch (local or on origin); failing that a branch rendered
    from `title`. Several candidates with no worktree are refused, never
    picked by name.

    Callers run this under `corral.units.spawn_lock()`, so two launches never
    race on a ticket.

    Refused with `WorktreeError` when the root escapes the clone, the main
    clone sits on the ticket's branch, a registered worktree for it is
    missing, outside the root or has no `.git` file, a leftover directory is
    not a worktree on it, local and origin have diverged, origin cannot be
    fetched, or there is no branch and no title to derive one.
    """
    _check_number(number)
    if not os.path.isdir(main_clone):
        raise WorktreeError(f"no main clone at {main_clone}")
    main = os.path.realpath(main_clone)
    real_root = _resolve_root(main, root)
    fetched = git(["fetch", "--quiet", "origin"], main)
    if fetched.returncode != 0:
        raise WorktreeError(f"cannot fetch origin in {main}: {_tail(fetched)}")

    trees = parse_worktree_list(_out(git, ["worktree", "list", "--porcelain"], main))
    checked_out = set(trees.values())
    if branch is not None:
        _check_branch(branch, number)
    else:
        names = _ticket_names(number, git, main)
        with_tree = [name for name in names if name in checked_out]
        if len(with_tree) > 1:
            raise WorktreeError(
                f"#{number} has worktrees on several ticket branches: "
                f"{', '.join(with_tree)}; pass branch= to choose one"
            )
        if with_tree:
            branch = with_tree[0]
        elif len(names) > 1:
            raise WorktreeError(
                f"#{number} has several ticket branches and no worktree: "
                f"{', '.join(names)}; pass branch= to choose one"
            )
        elif names:
            branch = names[0]
        elif title:
            branch = branch_for(number, title, template=branch_template)
        else:
            raise WorktreeError(f"#{number}: no ticket branch and no title to derive one")
    match = _check_branch(branch, number)

    _sync_local(branch, checked_out, main, git)

    for path, current in trees.items():
        if current != branch:
            continue
        if os.path.realpath(path) == main:
            raise WorktreeError(
                f"the main clone {main} is on {branch}: switch it back to "
                f"{default_branch}; agents never work there"
            )
        if not os.path.isdir(path):
            raise WorktreeError(
                f"{path} is registered for {branch} but missing: run `git -C {main} worktree prune`"
            )
        if not _inside(os.path.realpath(path), real_root):
            raise WorktreeError(
                f"{path} is registered for {branch} but outside the worktree root {real_root}"
            )
        if not os.path.exists(os.path.join(path, ".git")):
            raise WorktreeError(f"{path} is registered for {branch} but has no .git file")
        return path, branch

    dest = os.path.join(real_root, f"{number}-{match.group('slug')}")
    if os.path.lexists(dest):
        raise WorktreeError(
            f"{dest} exists but is not a worktree on {branch}: remove it or pass the right branch"
        )
    if _ok(git, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], main):
        _out(git, ["worktree", "add", dest, branch], main)
    elif _ok(git, ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], main):
        _out(git, ["worktree", "add", "-b", branch, dest, f"origin/{branch}"], main)
    else:
        _out(git, ["worktree", "add", "-b", branch, dest, f"origin/{default_branch}"], main)
    return dest, branch
