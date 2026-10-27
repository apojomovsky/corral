"""The `gh` shim: pace every agent `gh` call through the shared bucket.

Run as `python -m corral.github.gh_shim` by the wrapper that
`corral.github.ghshim.install_shim` writes. The wrapper names its own
directory in `CORRAL_GH_SHIM_DIR` and exports the install-time state dir as
`CORRAL_STATE`. The shim finds the real `gh` on PATH by skipping its own
directory and every other corral wrapper, so it never runs itself. For each
call it takes points from the shared bucket (waiting, with a cap, then
letting the call through, charged), runs the real `gh` with the same
arguments, passes stdout, stderr and the exit code through, starts the
shared cooldown on a rate-limit answer, and appends one redacted line to the
call log.

Stdout is buffered, not streamed, so the shim can read a JSON error payload
from a failed call. Stdin is inherited. A call still running after
`ghshim.TIMEOUT_S` is terminated (then killed) and exits 124.

A shim that starts with `CORRAL_GH_SHIM_DEPTH` already set exits 4 instead of
running: that is a shim calling itself through PATH.
"""

import os
import signal
import subprocess
import sys
import time

from corral.github import gh, ghshim

USAGE = 4
NOT_FOUND = 127
#: Bytes read from a candidate to check for the wrapper marker.
_MARKER_PROBE = 512


def _is_wrapper(path):
    """Whether `path` is a corral gh wrapper (it carries the marker line)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_MARKER_PROBE)
    except OSError:
        return False
    return ghshim.WRAPPER_MARKER.encode() in head


def find_on_path(name, skip_dir, path=None):
    """First executable `name` on PATH that is neither in `skip_dir` nor a corral wrapper.

    Directories compare by realpath, so a symlink to the shim directory on
    PATH is skipped too. Any corral wrapper is skipped, whichever directory
    it sits in, so a second shim directory cannot loop with this one.
    """
    skip = os.path.realpath(skip_dir)
    search = os.environ.get("PATH", "") if path is None else path
    for entry in search.split(os.pathsep):
        if not entry:
            continue
        if os.path.realpath(entry) == skip:
            continue
        candidate = os.path.join(entry, name)
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if _is_wrapper(candidate):
            continue
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


def _communicate(proc, timeout):
    """(stdout, stderr, timed_out): wait up to `timeout`, then stop the group.

    Escalates like `gh.run`: SIGTERM to the child's process group, then
    SIGKILL after a grace period. The child is a session leader, so its
    grandchildren go with it.
    """
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, False
    except subprocess.TimeoutExpired:
        gh._signal_group(proc, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=gh._KILL_AFTER_S)
        except subprocess.TimeoutExpired:
            gh._signal_group(proc, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        return stdout, stderr, True


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get(ghshim.DEPTH_ENV):
        sys.stderr.write(
            f"gh shim: refusing to run, {ghshim.DEPTH_ENV} is already set (a shim calling itself)\n"
        )
        return USAGE
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
    child_env = dict(os.environ, **{ghshim.DEPTH_ENV: "1"})
    start = time.monotonic()
    proc = subprocess.Popen(
        [real_gh] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=child_env,
    )
    stdout, stderr, timed_out = _communicate(proc, ghshim.TIMEOUT_S)
    duration = time.monotonic() - start
    code = ghshim.TIMED_OUT_EXIT if timed_out else proc.returncode
    # Byte-identical, delivered at exit: the output is buffered so a failed
    # call's JSON error payload can be read.
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    # stderr is buffered rather than teed on purpose: agent units are headless
    # and stdin still inherits, so a prompt reading EOF behaves exactly as
    # without the shim.
    sys.stderr.buffer.write(stderr)
    if timed_out:
        sys.stderr.write(f"gh shim: gh timed out after {ghshim.TIMEOUT_S:g}s and was stopped\n")
    sys.stderr.buffer.flush()
    # A library-paced call already cooled down upstream; setting it here
    # would double the backoff for the same event.
    hit = False
    if not library_paced:
        try:
            hit = ghshim.pace_after(stderr.decode("utf-8", "replace"))
            if not hit:
                hit = ghshim.pace_after_payload(stdout.decode("utf-8", "replace"), code)
        except Exception:
            hit = False
    try:
        ghshim.append_call(
            caller=ghshim.caller_name(),
            argv=args,
            exit_code=code,
            duration_s=duration,
            throttled=capped or hit,
            paced=library_paced,
        )
    except Exception:
        pass
    return _exit_with(code)


if __name__ == "__main__":
    sys.exit(main())
