"""Which files a gate should look at for `<base>...HEAD`, rename-aware.

A file that only moved has no new content to lint. Listing by new name alone
makes git report a move as an addition, so every existing comment block and
function in a moved file gets charged to the change. This resolves each
change once with rename and copy detection, so a pure move drops out and a
moved file with edits carries its old path for a paired diff.

Copy detection matters when the old path survives: a move that leaves a
compatibility symlink behind never pairs as a rename, so without `-C` the
new path reads as wholly added. `--find-copies-harder` also catches a copy
whose source is untouched in the range.
"""

import os
import re

from typing import NamedTuple, Optional


#: The base a branch diffs against when nobody names one.
DEFAULT_BASE = "origin/master"
BASE_ENV = "BASE_REF"
#: The help text every --base flag shows.
BASE_HELP = f"default {DEFAULT_BASE} or ${BASE_ENV}"


def default_base(given=None, env=None):
    """The base ref: `given` (a --base flag), else $BASE_REF, else origin/master."""
    env = os.environ if env is None else env
    return given or env.get(BASE_ENV) or DEFAULT_BASE


# Vendored and generated paths no first-party gate lints. One pattern lives
# here so first-party gates cannot drift; each filters on is_excluded()
# and names it in its rename context.
EXCLUDED = re.compile(r"(^|/)(vendor|third_party|target|build(?:[_-][^/]*)?|\.worktrees)/")


def is_excluded(path):
    """True when a path is vendored, generated, or otherwise not linted."""
    return bool(EXCLUDED.search(path))


class DiffError(RuntimeError):
    """git could not produce the diff: a gate must fail, not pass empty."""


class Change(NamedTuple):
    path: str
    old_path: Optional[str]  # set only for a rename/copy with edits
    pure_rename: bool  # also true for a pure copy: no new content either way


def changes(base, git, cwd=None, pathspec=()):
    """Added, copied, modified and renamed files in base...HEAD.

    `git` is the caller's runner, so each gate keeps its own test seam.
    """
    args = [
        "diff",
        "-M",
        "-C",
        "--find-copies-harder",
        "-z",
        "--name-status",
        "--diff-filter=ACMR",
        f"{base}...HEAD",
    ]
    if pathspec:
        args += ["--", *pathspec]
    proc = git(args, cwd=cwd)
    if proc.returncode != 0:
        raise DiffError(f"git {' '.join(args)} failed: {(proc.stderr or '').strip()[:300]}")
    out = proc.stdout
    fields = [f for f in out.split("\0") if f]
    result = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if status[0] in "RC":
            old, new = fields[i + 1], fields[i + 2]
            i += 3
            result.append(Change(new, old, status[1:] == "100"))
        else:
            result.append(Change(fields[i + 1], None, False))
            i += 2
    return result


def content_changes(base, git, cwd=None, pathspec=(), context=None):
    """changes() without the files whose content did not change.

    A pure move was already linted at its old path, but only under the
    rules of that path. `context(path)` names those rules; a move into a
    different context (a .txt becoming .c, vendor/ becoming src/, tests/
    becoming src/) is charged whole, like a new file.
    """
    result = []
    for c in changes(base, git, cwd=cwd, pathspec=pathspec):
        if not c.pure_rename:
            result.append(c)
        elif context is not None and context(c.old_path) != context(c.path):
            result.append(Change(c.path, None, False))
    return result
