"""Verified push with a pre-push suite stamp.

Pushes die after a long pre-push hook: git opens the SSH connection,
the hook runs the suite for minutes, then the pack goes over the idle
connection the server already dropped. Two halves fix it:

- `corral push` pushes, then compares `git ls-remote origin
  <branch>` with `HEAD`. On mismatch it retries once. Exit codes follow
  the public table: 0 landed and verified, 1 hook refusal or a missing
  ref after retry, 3 unreachable remote, 4 usage error.
- The retry must not pay for the suite twice. The pre-push hook stamps
  the tree it already passed (`.git/corral-pre-push-stamp`, holding
  `<tree> <hook-digest>`); a stamp hit skips the suite, and any tree or
  hook change invalidates the stamp so the suite runs again.

Seams: every git call goes through `run`, stamp reads/writes through
`read_text`/`write_text`, so tests run with fakes and no network.
"""

import os
import subprocess

OK, FAILED, API, CONFIG = 0, 1, 3, 4

STAMP_NAME = "corral-pre-push-stamp"

#: Push stderr fragments that mean the remote was never reached. A dead
#: connection retries cleanly; anything else (hook refusal, rejection)
#: must not loop.
UNREACHABLE_MARKERS = (
    "could not read from remote",
    "could not resolve",
    "connection timed out",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "operation timed out",
    "temporary failure",
    "failed to connect",
    "unable to connect",
    "ssh: connect",
    "ssh: Could not resolve",
)


#: `git push` runs the pre-push hook, which runs the full suite (minutes
#: on a typical host); every other git call is quick.
GIT_TIMEOUT_S = 300
PUSH_TIMEOUT_S = 3600


def default_run(argv, timeout=None):
    if timeout is None:
        timeout = PUSH_TIMEOUT_S if "push" in argv[3:4] else GIT_TIMEOUT_S
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _git(cwd, args, run):
    return run(["git", "-C", cwd, *args])


def abs_git_dir(cwd, run=None):
    """Absolute git dir of this checkout (per-worktree, never the common)."""
    run = run or default_run
    proc = _git(cwd, ["rev-parse", "--absolute-git-dir"], run)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def hook_path(cwd, run=None):
    """The pre-push hook git runs here, honouring `core.hooksPath`.

    `--git-path` resolves the hooks dir the way git itself does: a
    relative `core.hooksPath` is taken from the top of the working tree,
    and with no setting it is the common git dir's `hooks/`.
    """
    run = run or default_run
    proc = _git(cwd, ["rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"], run)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def hook_digest(cwd, run=None):
    """Content id of the installed pre-push hook, via `git hash-object`.

    The hook hashes its own installed file the same way, so both sides
    always agree with no shared constant to drift. Empty when unreadable:
    the stamp then never hits and the suite always runs (safe default).
    """
    run = run or default_run
    path = hook_path(cwd, run)
    if not path:
        return ""
    proc = run(["git", "hash-object", path])
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def tree_hash(cwd, run=None):
    run = run or default_run
    proc = _git(cwd, ["rev-parse", "HEAD^{tree}"], run)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def stamp_path(cwd, run=None):
    directory = abs_git_dir(cwd, run)
    if not directory:
        return ""
    return os.path.join(directory, STAMP_NAME)


def read_stamp(cwd, run=None, read=None):
    read = read or read_text
    path = stamp_path(cwd, run)
    if not path:
        return ""
    try:
        return (read(path) or "").strip()
    except OSError:
        return ""


def record_pass(cwd, run=None, write=None, tree=None, digest=None):
    """Stamp the tree the suite just passed. Returns True when written."""
    write = write or write_text
    tree = tree if tree is not None else tree_hash(cwd, run)
    digest = digest if digest is not None else hook_digest(cwd, run)
    path = stamp_path(cwd, run)
    if not tree or not digest or not path:
        return False
    try:
        write(path, f"{tree} {digest}\n")
    except OSError:
        return False
    return True


def stamp_valid(cwd, run=None, read=None, tree=None, digest=None):
    """True when the stamp covers this tree under this hook version."""
    tree = tree if tree is not None else tree_hash(cwd, run)
    digest = digest if digest is not None else hook_digest(cwd, run)
    if not tree or not digest:
        return False
    return read_stamp(cwd, run, read) == f"{tree} {digest}"


def suite_needed(cwd, run=None, read=None):
    """The hook's skip decision: run the suite unless the stamp hits."""
    return not stamp_valid(cwd, run, read)


def current_branch(cwd, run=None):
    run = run or default_run
    proc = _git(cwd, ["branch", "--show-current"], run)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def local_head(cwd, run=None):
    run = run or default_run
    proc = _git(cwd, ["rev-parse", "HEAD"], run)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def remote_head(cwd, remote, branch, run=None):
    """(sha, proc) for the remote ref; sha is "" when missing or unreadable."""
    run = run or default_run
    proc = _git(cwd, ["ls-remote", remote, f"refs/heads/{branch}"], run)
    if proc.returncode != 0:
        return "", proc
    for line in (proc.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == f"refs/heads/{branch}":
            return fields[0], proc
    return "", proc


def looks_unreachable(text):
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in UNREACHABLE_MARKERS)


def hook_refused(push_proc):
    """True when the push died in our own pre-push hook: retrying would just
    re-run the failing suite, so the helper stops after the first attempt."""
    combined = (push_proc.stdout or "") + "\n" + (push_proc.stderr or "")
    return "pre-push:" in combined


def do_push(cwd, remote, branch, run):
    return _git(cwd, ["push", remote, branch], run)


def verified_push(cwd, remote="origin", branch=None, run=None, out=None):
    """Push, verify the remote ref, retry once on mismatch. Returns exit code."""
    run = run or default_run
    emit = out or print
    branch = branch or current_branch(cwd, run)
    if not branch:
        emit("corral push: no current branch (detached HEAD); pass a branch name")
        return CONFIG
    head = local_head(cwd, run)
    if not head:
        emit("corral push: cannot resolve HEAD")
        return CONFIG

    def verify():
        sha, proc = remote_head(cwd, remote, branch, run)
        return sha, proc

    first = do_push(cwd, remote, branch, run)
    if first.returncode != 0:
        if hook_refused(first):
            emit((first.stderr or "").strip() or "corral push: pre-push hook refused the push")
            return FAILED
        if looks_unreachable((first.stdout or "") + "\n" + (first.stderr or "")):
            emit(
                f"corral push: remote unreachable ({remote}); "
                f"{((first.stderr or '').strip() or 'push failed')[:200]}"
            )
            return API
    sha, ls_proc = verify()
    if ls_proc.returncode != 0:
        emit(
            f"corral push: cannot verify {remote}/{branch} "
            f"({((ls_proc.stderr or '').strip() or 'ls-remote failed')[:200]})"
        )
        return API
    if sha == head:
        emit(f"corral push: {branch} verified at {head[:12]}")
        return OK
    emit(
        f"corral push: {remote}/{branch} is "
        f"{(sha[:12] if sha else '(missing)')}, HEAD is {head[:12]}; "
        "retrying once"
    )
    second = do_push(cwd, remote, branch, run)
    if second.returncode != 0 and looks_unreachable(
        (second.stdout or "") + "\n" + (second.stderr or "")
    ):
        emit(f"corral push: remote unreachable on retry ({remote})")
        return API
    sha, ls_proc = verify()
    if ls_proc.returncode != 0:
        emit(f"corral push: cannot verify {remote}/{branch} after retry")
        return API
    if sha == head:
        emit(f"corral push: {branch} verified at {head[:12]} after retry")
        return OK
    emit(
        f"corral push: {remote}/{branch} still "
        f"{(sha[:12] if sha else '(missing)')} after retry; "
        f"HEAD is {head[:12]}"
    )
    return FAILED
