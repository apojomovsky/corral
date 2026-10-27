"""Git environment hygiene for subprocess calls.

A process born inside another git operation (a hook, `git rebase exec`)
inherits that repo's GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE, and
subprocess inherits them by default. Every git call then silently
resolves against the outer repo instead of its own `cwd`, even with an
explicit `cwd` or `-C`: a fixture's `git init <tmp>` re-initializes the
outer repo (observed: flips shared `core.bare` to true), and `git -C`
config/add/commit write into it.

Every helper in this repo that shells out to git passes
`env=clean_env()`; the pre-commit/pre-push hooks unset GIT_* before
running the suite, so both layers agree.
"""

import os


def clean_env(env=None):
    """A copy of `env` (default: os.environ) without GIT_* entries."""
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if not k.startswith("GIT_")}


def worktree_main_checkout(here):
    """Main-checkout root for a linked-worktree tree, without git.

    Reads the `.git` file (`gitdir:` pointer) and the worktree admin
    dir's `commondir` file instead of shelling out, so callers get the
    right root when git itself cannot run (or cannot resolve a mounted
    worktree whose gitdir pointer dangles). Returns None when `here`
    is not a linked worktree or the pointer does not resolve to a
    directory holding a git admin dir.
    """
    # Best effort: any unreadable content (including a test double
    # standing in for open) means "not resolvable", never an error.
    try:
        with open(os.path.join(here, ".git"), "r", encoding="utf-8") as handle:
            line = handle.read().strip()
    except Exception:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    if not gitdir:
        return None
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(here, gitdir))
    try:
        with open(os.path.join(gitdir, "commondir"), "r", encoding="utf-8") as handle:
            common = handle.read().strip()
    except Exception:
        return None
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(gitdir, common))
    if not os.path.isfile(os.path.join(common, "HEAD")):
        return None
    return os.path.dirname(common)
