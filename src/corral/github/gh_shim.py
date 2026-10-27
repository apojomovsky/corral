"""The `gh` shim: pace every agent `gh` call through the shared bucket.

Run as `python -m corral.github.gh_shim` by the wrapper that
`corral.github.ghshim.install_shim` writes. The wrapper names its own
directory in `CORRAL_GH_SHIM_DIR`, so the shim finds the real `gh` on PATH
by skipping that directory and never runs itself. For each call it takes
points from the shared bucket (waiting, with a cap, then letting the call
through), runs the real `gh` with the same arguments, passes stdout,
stderr and the exit code through, starts the shared cooldown on a
rate-limit answer, and appends one redacted line to the call log.
"""

import os
import signal
import subprocess
import sys
import time

from corral.github import ghshim

USAGE = 4
NOT_FOUND = 127


def find_on_path(name, skip_dir, path=None):
    """First executable `name` on PATH outside `skip_dir`, else None.

    Directories compare by realpath, so a symlink to the shim directory on
    PATH is skipped too.
    """
    skip = os.path.realpath(skip_dir)
    search = os.environ.get("PATH", "") if path is None else path
    for entry in search.split(os.pathsep):
        if not entry:
            continue
        if os.path.realpath(entry) == skip:
            continue
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _exit_with(returncode):
    """Exit like the child did: a signal death re-raises the signal.

    `sys.exit(negative)` would remap e.g. SIGTERM to exit 241, while an
    exec'd `gh` dies by the signal itself; agents read that difference.
    """
    if returncode is not None and returncode < 0:
        sig = -returncode
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
        return 128 + sig  # only when the signal is ignored or masked
    return returncode


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    shim_dir = os.environ.get(ghshim.SHIM_DIR_ENV, "")
    if not shim_dir:
        # Without the directory the shim cannot tell itself from the real gh.
        sys.stderr.write(f"gh shim: {ghshim.SHIM_DIR_ENV} is not set\n")
        return USAGE
    real_gh = find_on_path("gh", shim_dir)
    if real_gh is None:
        sys.stderr.write("gh shim: no real gh on PATH\n")
        return NOT_FOUND
    # Library-paced callers (`gh.run(paced=True)`) already charged the bucket
    # and cooled it down on errors. Pacing here would charge twice, so those
    # calls are logged but not paced again.
    library_paced = ghshim.paced_marker_set()
    cost = None if library_paced else ghshim.cost_for(args)
    capped = False
    if cost is not None:
        try:
            _, capped = ghshim.pace_before(cost)
        except Exception:
            # Pacing is best-effort: a broken bucket must never break gh.
            capped = False
    start = time.monotonic()
    watch_stdout = ghshim.watches_stdout(args)
    proc = subprocess.Popen(
        [real_gh] + args,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE if watch_stdout else None,
    )
    stdout, stderr = proc.communicate()
    duration = time.monotonic() - start
    if watch_stdout:
        # Byte-identical, delivered at exit: GraphQL errors ride on stdout
        # with exit 0, so the cooldown needs to see them.
        sys.stdout.buffer.write(stdout)
        sys.stdout.buffer.flush()
    # stderr is buffered rather than teed on purpose: agent units are
    # headless and stdin still inherits, so a prompt reading EOF behaves
    # exactly as without the shim.
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()
    text = stderr.decode("utf-8", "replace")
    if watch_stdout:
        text += "\n" + stdout.decode("utf-8", "replace")
    # A library-paced call already cooled down upstream; setting it here
    # would double the backoff for the same event.
    hit = False
    if not library_paced:
        try:
            hit = ghshim.pace_after(text)
        except Exception:
            hit = False
    try:
        ghshim.append_call(
            caller=ghshim.caller_name(),
            argv=args,
            exit_code=proc.returncode,
            duration_s=duration,
            throttled=capped or hit,
            paced=library_paced,
        )
    except Exception:
        pass
    return _exit_with(proc.returncode)


if __name__ == "__main__":
    sys.exit(main())
