"""One worktree per ticket: the checkout a ticket's agent works in.

The branch name comes from the ``git.branch`` setting and the directory
from ``git.worktrees``, both read from the registry (`corral.settings`),
never restated here. Every git call goes through the one seam `git`,
which runs with `gitenv.clean_env()`.

`ensure_worktree` reuses, in order: a worktree already on the ticket's
branch, a local ticket branch, the ticket branch on origin (an open PR's
head). Otherwise it creates the branch from ``origin/<default_branch>``.
A leftover directory that git does not have on that branch is refused,
never reused. Removal belongs to host hygiene, not here.
"""

import os
import re
import subprocess

from corral import settings
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
TICKET_BRANCH_RE = re.compile(r"^(?P<type>[A-Za-z][\w.-]*)/(?P<number>\d+)-(?P<slug>[\w.-]+)$")


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


def branch_for(number, title, template=None):
    """The ticket's branch, rendered from the `git.branch` template.

    `template` defaults to the registry's default for `git.branch`. Its
    `{type}` comes from a Conventional title prefix when it is one of
    `TYPES`, else `DEFAULT_TYPE`; `{issue}` is the number and `{slug}` is
    `slugify(title)`.
    """
    if template is None:
        template = str(settings.REGISTRY["git.branch"].default)
    match = TITLE_TYPE_RE.match((title or "").strip())
    kind = match.group(1).lower() if match else ""
    if kind not in TYPES:
        kind = DEFAULT_TYPE
    try:
        return template.format(type=kind, issue=number, slug=slugify(title))
    except (KeyError, IndexError, ValueError) as err:
        raise WorktreeError(f"git.branch {template!r} is not a valid template: {err}") from None


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


def _check_number(number):
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise WorktreeError(f"ticket number {number!r} must be a positive integer")


def ensure_worktree(
    main_clone,
    number,
    *,
    default_branch,
    title=None,
    branch=None,
    root=None,
    git=git,
):
    """``(workdir, branch)`` for ticket `number`, created if it does not exist.

    `root` is the worktree directory, relative to `main_clone` or absolute;
    it defaults to the registry's `git.worktrees`. `branch` pins the branch;
    when absent it is the ticket's existing branch, else `branch_for(number,
    title)`, so a caller that wants the `git.branch` setting honoured passes
    the branch it rendered with `branch_for(..., template=...)`.

    Refused with `WorktreeError` when the main clone sits on the ticket's
    branch, a registered worktree for it is missing on disk, a leftover
    directory is not a worktree on it, origin cannot be fetched, or there is
    no branch and no title to derive one.
    """
    _check_number(number)
    if not os.path.isdir(main_clone):
        raise WorktreeError(f"no main clone at {main_clone}")
    main = os.path.realpath(main_clone)
    if root is None:
        root = str(settings.REGISTRY["git.worktrees"].default)
    fetched = git(["fetch", "--quiet", "origin"], main)
    if fetched.returncode != 0:
        raise WorktreeError(f"cannot fetch origin in {main}: {_tail(fetched)}")

    trees = parse_worktree_list(_out(git, ["worktree", "list", "--porcelain"], main))
    if branch is None:
        local = _out(git, ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], main)
        found = ticket_branches(number, local.splitlines())
        if not found:
            remote = _out(
                git,
                ["for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin/"],
                main,
            )
            found = ticket_branches(number, remote.splitlines())
        branch = found[0] if found else None
    if branch is None:
        if not title:
            raise WorktreeError(f"#{number}: no ticket branch and no title to derive one")
        branch = branch_for(number, title)

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
        return path, branch

    match = TICKET_BRANCH_RE.match(branch)
    slug = match.group("slug") if match else slugify(branch)
    dest = os.path.join(main, root, f"{number}-{slug}")
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
