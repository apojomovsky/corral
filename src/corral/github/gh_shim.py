"""The `gh` shim: pace every agent `gh` call through the shared bucket.

Run as `python -m corral.github.gh_shim` by the wrapper that
`corral.github.ghshim.install_shim` writes. The wrapper names its own
directory in `CORRAL_GH_SHIM_DIR` and exports the install-time state dir as
`CORRAL_STATE`. The shim finds the real `gh` on PATH by skipping its own
directory and every other corral wrapper, so it never runs itself. For each
call it takes points from the shared bucket (waiting, with a cap, then
letting the call through, charged), runs the real `gh` with the same
arguments, passes stdout and stderr through as they arrive, starts the shared
cooldown on a rate-limit answer, and appends one redacted line to the call
log.

Output streams: reader threads copy the child's stdout and stderr chunk by
chunk. Only the last `ghshim.TAIL_BYTES` of each stream are kept, for the
rate-limit check after the call. Stdin is inherited. A call still running
after `ghshim.TIMEOUT_S` is stopped (SIGTERM, then SIGKILL) and exits 124.

A gh that calls gh again (an alias or extension) runs one level deeper, with
`CORRAL_GH_SHIM_DEPTH` set to the new depth. A shim that starts at
`ghshim.MAX_DEPTH` or deeper exits 4 instead of running.
"""

import os
import signal
import subprocess
import sys
import threading
import time

from corral.github import gh, ghshim

USAGE = 4
NOT_FOUND = 127
#: Bytes read from a candidate to check for the wrapper marker.
_MARKER_PROBE = 512
#: Largest single read from a child's pipe.
_CHUNK = 64 * 1024
#: Seconds a reader may keep running after the child exits before the group is killed.
_READER_JOIN_S = 5.0


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


def _pump(pipe, sink, tail):
    """Copy `pipe` to `sink` as bytes arrive, keeping the last TAIL_BYTES in `tail`.

    `read1` returns as soon as any bytes are available, so output reaches the
    caller while the child is still running.
    """
    try:
        while True:
            chunk = pipe.read1(_CHUNK)
            if not chunk:
                break
            sink.write(chunk)
            sink.flush()
            tail.extend(chunk)
            del tail[: -ghshim.TAIL_BYTES]
    except OSError:
        # The pipe was closed under us after the shim gave up on it.
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _wait(proc, timeout):
    """True when the child ran past `timeout` and was stopped.

    Escalates like `gh.run`: SIGTERM to the child's process group, then a
    grace period. The group is SIGKILLed whenever the child ran past the
    timeout, even if the leader exited during the grace period, because a
    grandchild may still hold the pipes.
    """
    try:
        proc.wait(timeout=timeout)
        return False
    except subprocess.TimeoutExpired:
        gh._signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=gh._KILL_AFTER_S)
        except subprocess.TimeoutExpired:
            pass
        gh._signal_group(proc, signal.SIGKILL)
        proc.wait()
        return True


def _drain(proc, readers):
    """Join the readers, stopping the group if a grandchild keeps a pipe open.

    A daemonised helper that inherited stdout or stderr keeps the pipe open
    after the leader exits, so the readers never see EOF. Each join is bounded.
    If a reader is still alive, the group is SIGKILLed and the readers are
    joined once more. A reader still alive then is a daemon thread on a pipe
    that a process outside the group holds; the shim does not wait for it, and
    the pipe closes when the shim exits. (Closing a pipe another thread is
    reading is not safe, so the shim does not close it early.)
    """
    for reader in readers:
        reader.join(timeout=_READER_JOIN_S)
    if not any(reader.is_alive() for reader in readers):
        return
    gh._signal_group(proc, signal.SIGKILL)
    for reader in readers:
        reader.join(timeout=_READER_JOIN_S)


def _depth():
    """The nesting depth this shim runs at, from DEPTH_ENV (0 when unset)."""
    raw = os.environ.get(ghshim.DEPTH_ENV, "")
    if not raw:
        return 0
    if not raw.isdigit():
        raise ValueError(raw)
    return int(raw)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        depth = _depth()
    except ValueError:
        sys.stderr.write(f"gh shim: {ghshim.DEPTH_ENV} is not a depth\n")
        return USAGE
    if depth >= ghshim.MAX_DEPTH:
        sys.stderr.write(
            f"gh shim: refusing to run at depth {depth} (a gh calling gh {ghshim.MAX_DEPTH} levels deep)\n"
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
    child_env = dict(os.environ, **{ghshim.DEPTH_ENV: str(depth + 1)})
    start = time.monotonic()
    proc = subprocess.Popen(
        [real_gh] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=child_env,
    )
    out_tail, err_tail = bytearray(), bytearray()
    readers = [
        threading.Thread(
            target=_pump, args=(proc.stdout, sys.stdout.buffer, out_tail), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(proc.stderr, sys.stderr.buffer, err_tail), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()
    timed_out = _wait(proc, ghshim.TIMEOUT_S)
    _drain(proc, readers)
    duration = time.monotonic() - start
    code = ghshim.TIMED_OUT_EXIT if timed_out else proc.returncode
    if timed_out:
        sys.stderr.buffer.write(
            f"gh shim: gh timed out after {ghshim.TIMEOUT_S:g}s and was stopped\n".encode()
        )
        sys.stderr.buffer.flush()
    # A library-paced call already cooled down upstream; setting it here
    # would double the backoff for the same event.
    hit = False
    if not library_paced:
        try:
            hit = ghshim.pace_after(bytes(err_tail).decode("utf-8", "replace"))
            if not hit:
                hit = ghshim.pace_after_payload(bytes(out_tail).decode("utf-8", "replace"), code)
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
