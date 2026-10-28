"""Project detection for `corral init`: pure readers over a directory.

Each function takes the directory it reads and, where git is needed, a
`git(args, cwd)` runner returning a CompletedProcess-like object (the
same seam shape as `corral.gc.git`). There is no network and no `gh`:
GitHub URL parsing is `corral.github.forge`'s job and is reused here,
never redefined.
"""

import json
import os
import re
from typing import NamedTuple

from corral import settings
from corral.github import forge as ghforge

settings.register("checks.test", str, "", "the test command the agent and takeoff run")

#: A Makefile line that defines the `test` target (`test:`, `test :`, ...),
#: not a variable assignment (`test := x`, `test ::= x`).
_MAKE_TEST_TARGET = re.compile(r"^test\s*:(?!:?\s*=)", re.MULTILINE)


def remote_slug(url):
    """`owner/name` from a git remote URL, else None.

    The parser is `corral.github.forge.repo_from_remote_url`; this is
    only its short name, so the shape check stays in one place.
    """
    return ghforge.repo_from_remote_url(url)


def origin_slug(directory, git):
    """`owner/name` from the directory's `origin` remote, else None."""
    return ghforge.origin_repo(directory, git)


def default_branch(directory, git):
    """The remote HEAD branch, else the current branch, else None.

    The remote HEAD (`refs/remotes/origin/HEAD`) names the branch the
    remote points at; without a remote, or without an origin HEAD, the
    current branch is the best answer. A detached HEAD is no answer.
    This detects; `diffscope.default_base` separately picks a diff base,
    so the two never resolve the same fact.
    """
    proc = git(["symbolic-ref", "refs/remotes/origin/HEAD"], directory)
    if proc is not None and getattr(proc, "returncode", 1) == 0:
        ref = (getattr(proc, "stdout", "") or "").strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix) and len(ref) > len(prefix):
            return ref[len(prefix) :]
    proc = git(["branch", "--show-current"], directory)
    if proc is not None and getattr(proc, "returncode", 1) == 0:
        return (getattr(proc, "stdout", "") or "").strip() or None
    return None


def test_command(directory):
    """The test command the repository already implies, else None.

    The first match wins: a `Makefile` with a `test` target, then
    `pyproject.toml` (`uv run python -m unittest` when a `tests/` dir
    holds a `test_*.py` file, else `uv run pytest`), `package.json`
    with a `test` script, `Cargo.toml`, `go.mod`.
    """
    try:
        with open(os.path.join(directory, "Makefile"), encoding="utf-8") as handle:
            makefile = handle.read()
    except (OSError, UnicodeError):
        makefile = ""
    if _MAKE_TEST_TARGET.search(makefile):
        return "make test"
    if os.path.isfile(os.path.join(directory, "pyproject.toml")):
        tests = os.path.join(directory, "tests")
        try:
            names = os.listdir(tests)
        except OSError:
            names = []
        for name in names:
            full = os.path.join(tests, name)
            if name.startswith("test_") and name.endswith(".py") and os.path.isfile(full):
                return "uv run python -m unittest"
        return "uv run pytest"
    try:
        with open(os.path.join(directory, "package.json"), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        payload = None
    if isinstance(payload, dict):
        scripts = payload.get("scripts")
        if isinstance(scripts, dict) and scripts.get("test"):
            return "npm test"
    if os.path.isfile(os.path.join(directory, "Cargo.toml")):
        return "cargo test"
    if os.path.isfile(os.path.join(directory, "go.mod")):
        return "go test ./..."
    return None


class Existing(NamedTuple):
    """What `corral init` found but must never touch."""

    agents_md: bool
    claude_md: bool


def existing(directory):
    """Which of `AGENTS.md` and `CLAUDE.md` already exist.

    `init` reports these; it never edits them. Whether the repository has
    hooks is decided in one place, `init`, from `core.hooksPath`.
    """
    return Existing(
        agents_md=os.path.isfile(os.path.join(directory, "AGENTS.md")),
        claude_md=os.path.isfile(os.path.join(directory, "CLAUDE.md")),
    )
