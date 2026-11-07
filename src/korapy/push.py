"""Verified push with a pre-push suite stamp.

Pushes die after a long pre-push hook: git opens the SSH connection,
the hook runs the suite for minutes, then the pack goes over the idle
connection the server already dropped. Two halves fix it:

- `korapy push` pushes, then compares `git ls-remote origin
  <branch>` with `HEAD`. On mismatch it retries once. Exit codes follow
  the public table: 0 landed and verified, 1 hook refusal or a missing
  ref after retry, 3 unreachable remote, 4 usage error.
- The retry must not pay for the suite twice. The pre-push hook stamps
  the tree it already passed (`.git/korapy-pre-push-stamp`, holding
  `<tree> <hook-digest>`); a stamp hit skips the suite, and any tree or
  hook change invalidates the stamp so the suite runs again.

Seams: every git call goes through `run`, stamp reads/writes through
`read_text`/`write_text`, so tests run with fakes and no network.
`env` carries a per-launch HTTPS fallback (see `korapy.pushenv`); None
pushes exactly as before.
"""

import os
import subprocess
import time

from korapy import checkstamp

OK, FAILED, API, CONFIG = 0, 1, 3, 4

STAMP_NAME = "korapy-pre-push-stamp"

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


def default_run(argv, timeout=None, env=None):
    if timeout is None:
        timeout = PUSH_TIMEOUT_S if "push" in argv[3:4] else GIT_TIMEOUT_S
    try:
        merged = {**os.environ, **env} if env is not None else None
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=merged)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _git(cwd, args, run, env=None):
    return run(["git", "-C", cwd, *args], env=env)


def abs_git_dir(cwd, run=None, env=None):
    """Absolute git dir of this checkout (per-worktree, never the common)."""
    run = run or default_run
    proc = _git(cwd, ["rev-parse", "--absolute-git-dir"], run, env)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def hook_path(cwd, run=None, env=None):
    """The pre-push hook git runs here, honouring `core.hooksPath`.

    `--git-path` resolves the hooks dir the way git itself does: a
    relative `core.hooksPath` is taken from the top of the working tree,
    and with no setting it is the common git dir's `hooks/`.
    """
    run = run or default_run
    proc = _git(
        cwd, ["rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"], run, env
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def hook_digest(cwd, run=None, env=None):
    """Content id of the installed pre-push hook, via `git hash-object`.

    The hook hashes its own installed file the same way, so both sides
    always agree with no shared constant to drift. Empty when unreadable:
    the stamp then never hits and the suite always runs (safe default).
    """
    run = run or default_run
    path = hook_path(cwd, run, env)
    if not path:
        return ""
    proc = run(["git", "hash-object", path], env=env)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def tree_hash(cwd, run=None, env=None):
    run = run or default_run
    proc = _git(cwd, ["rev-parse", "HEAD^{tree}"], run, env)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def stamp_path(cwd, run=None, env=None):
    directory = abs_git_dir(cwd, run, env)
    if not directory:
        return ""
    return os.path.join(directory, STAMP_NAME)


def read_stamp(cwd, run=None, read=None, env=None):
    read = read or read_text
    path = stamp_path(cwd, run, env)
    if not path:
        return ""
    try:
        return (read(path) or "").strip()
    except OSError:
        return ""


def record_pass(cwd, run=None, write=None, tree=None, digest=None, env=None):
    """Stamp the tree the suite just passed. Returns True when written."""
    write = write or write_text
    tree = tree if tree is not None else tree_hash(cwd, run, env)
    digest = digest if digest is not None else hook_digest(cwd, run, env)
    path = stamp_path(cwd, run, env)
    if not tree or not digest or not path:
        return False
    try:
        write(path, f"{tree} {digest}\n")
    except OSError:
        return False
    return True


def working_dirty(cwd, run=None, env=None):
    """True when the working tree has uncommitted changes.

    An unreadable status reads as dirty: a tree the gate cannot see
    never skips the suite (fail closed).
    """
    run = run or default_run
    proc = _git(cwd, ["status", "--porcelain"], run, env)
    if proc.returncode != 0:
        return True
    return bool((proc.stdout or "").strip())


def proof_decision(own_text, own_expected, matrix_text, *, tree, pythons, now, dirty):
    """One resolver for the hook's skip: both proofs in, one decision out.

    `own_text` is the hook's own stamp file content ("" when missing),
    `own_expected` the `<tree> <hook-digest>` line it must hold (""
    when the tree or digest is unresolvable, which never hits);
    `matrix_text` the green-matrix stamp text; `tree` the current
    `HEAD^{tree}`, `pythons` the interpreters a reused proof must
    cover, `now` epoch seconds, `dirty` whether the working tree has
    uncommitted changes. Returns `(skip, reason)`: `skip` True only for
    an own-stamp hit or a fresh matrix proof for the identical clean
    tree; `reason` is "own-stamp" or "matrix-proof" for a skip, else
    the miss word ("dirty-tree", or the checkstamp verdict) that tells
    the hook to run the suite as before.
    """
    if dirty:
        return False, "dirty-tree"
    if own_expected and (own_text or "").strip() == own_expected:
        return True, "own-stamp"
    verdict = checkstamp.stamp_status(matrix_text, tree, pythons, now)
    if verdict == "fresh":
        return True, "matrix-proof"
    return False, verdict


def hook_decision(cwd=None, run=None, read=None, matrix_read=None, env=None, now=None):
    """Gather the hook's real inputs and resolve the skip.

    Called by the pre-push hook: the tree, hook digest, both stamp
    texts, the dirt flag and the clock arrive from git, the stamp
    files and the cache dir (resolved through `paths`, never trusted
    across trees). Every input stays injectable, so tests run with
    fakes. Returns `(skip, reason)` like `proof_decision`.
    """
    cwd = cwd or os.getcwd()
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    tree = tree_hash(cwd, run, env)
    digest = hook_digest(cwd, run, env)
    own_expected = f"{tree} {digest}" if tree and digest else ""
    return proof_decision(
        read_stamp(cwd, run, read, env),
        own_expected,
        checkstamp.read_stamp(env, matrix_read),
        tree=tree,
        pythons=checkstamp.MATRIX_PYTHONS,
        now=now,
        dirty=working_dirty(cwd, run, env),
    )


def current_branch(cwd, run=None, env=None):
    run = run or default_run
    proc = _git(cwd, ["branch", "--show-current"], run, env)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def local_head(cwd, run=None, env=None):
    run = run or default_run
    proc = _git(cwd, ["rev-parse", "HEAD"], run, env)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def remote_head(cwd, remote, branch, run=None, env=None):
    """(sha, proc) for the remote ref; sha is "" when missing or unreadable."""
    run = run or default_run
    proc = _git(cwd, ["ls-remote", remote, f"refs/heads/{branch}"], run, env)
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


def do_push(cwd, remote, branch, run, env=None):
    return _git(cwd, ["push", remote, branch], run, env)


def verified_push(cwd, remote="origin", branch=None, run=None, out=None, env=None):
    """Push, verify the remote ref, retry once on mismatch. Returns exit code."""
    run = run or default_run
    emit = out or print
    branch = branch or current_branch(cwd, run, env)
    if not branch:
        emit("korapy push: no current branch (detached HEAD); pass a branch name")
        return CONFIG
    head = local_head(cwd, run, env)
    if not head:
        emit("korapy push: cannot resolve HEAD")
        return CONFIG

    def verify():
        sha, proc = remote_head(cwd, remote, branch, run, env)
        return sha, proc

    first = do_push(cwd, remote, branch, run, env)
    if first.returncode != 0:
        if hook_refused(first):
            emit((first.stderr or "").strip() or "korapy push: pre-push hook refused the push")
            return FAILED
        if looks_unreachable((first.stdout or "") + "\n" + (first.stderr or "")):
            emit(
                f"korapy push: remote unreachable ({remote}); "
                f"{((first.stderr or '').strip() or 'push failed')[:200]}"
            )
            return API
    sha, ls_proc = verify()
    if ls_proc.returncode != 0:
        emit(
            f"korapy push: cannot verify {remote}/{branch} "
            f"({((ls_proc.stderr or '').strip() or 'ls-remote failed')[:200]})"
        )
        return API
    if sha == head:
        emit(f"korapy push: {branch} verified at {head[:12]}")
        return OK
    emit(
        f"korapy push: {remote}/{branch} is "
        f"{(sha[:12] if sha else '(missing)')}, HEAD is {head[:12]}; "
        "retrying once"
    )
    second = do_push(cwd, remote, branch, run, env)
    if second.returncode != 0 and looks_unreachable(
        (second.stdout or "") + "\n" + (second.stderr or "")
    ):
        emit(f"korapy push: remote unreachable on retry ({remote})")
        return API
    sha, ls_proc = verify()
    if ls_proc.returncode != 0:
        emit(f"korapy push: cannot verify {remote}/{branch} after retry")
        return API
    if sha == head:
        emit(f"korapy push: {branch} verified at {head[:12]} after retry")
        return OK
    emit(
        f"korapy push: {remote}/{branch} still "
        f"{(sha[:12] if sha else '(missing)')} after retry; "
        f"HEAD is {head[:12]}"
    )
    return FAILED
