"""External-call guard for the test suite.

The suite must never reach GitHub, systemd, docker, omp, opencode, claude, loginctl, tailscale or a
non-loopback network address, whichever runner starts it. ``install()`` runs
from ``tests/__init__.py`` at import, so every entry point gets the same
protection. Three layers:

* shims: failing stand-ins for the external CLIs, first on PATH. Each call is
  recorded and exits 1, the way an unreachable service would.
* git: the real git behind a wrapper that refuses network verbs (push, fetch,
  pull, ls-remote, clone, archive --remote, submodule) against any remote
  that is not a local path.
* sockets: connect, connect_ex, sendto, create_connection and getaddrinfo
  refuse every non-loopback address, which covers urllib (quota readers,
  the fleet API) in-process.

Each record names the running test: ``TestCase.run`` exports its id in
``CORRAL_GUARD_TEST``. The run fails when the log holds records. The check
runs from the ``load_tests`` wrapper in ``tests/__init__.py``; an atexit
fallback covers runners that never call ``load_tests`` (for example
``python -m unittest tests.test_x``).

Run as a script (``python suiteguard.py LOG REAL_GIT ARGS...``) it is the git
wrapper used by the shim.
"""

import atexit
import errno
import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

CALLER_ENV = "CORRAL_GUARD_TEST"
SHIM_TOOLS = (
    "gh",
    "systemctl",
    "systemd-run",
    "omp",
    "opencode",
    "claude",
    "docker",
    "loginctl",
    "tailscale",
    "netbird",
)
GIT_NETWORK_VERBS = frozenset(
    ("push", "fetch", "pull", "ls-remote", "clone", "archive", "submodule")
)
_SUBMODULE_NETWORK_SUBCOMMANDS = frozenset(("add", "update", "sync", "set-url", "foreach"))
_ROOT_PREFIX = "corral-guard-"
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
_SCP = re.compile(r"^[^/:@]+@[^/:]+:")
_HOST_PATH = re.compile(r"^[^/:]+:")
_GIT_VALUE_OPTS = frozenset(("-C", "-c", "--git-dir", "--work-tree", "--namespace"))
_REFUSED = "corral suite: external connection refused"

_LOG = ""  # the run log; install() sets it
_checked = False
_real = {}


def record(call, log=None):
    """Append one call to the run log, attributed to the running test."""
    path = log or _LOG
    caller = os.environ.get(CALLER_ENV, "<no-test>")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{caller}\t{call.replace(chr(10), ' ')}\n")


def read_offenders(path=None):
    """Return the recorded calls of the run, empty when there are none."""
    try:
        with open(path or _LOG, encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def _failure_message(offenders):
    shown = "\n".join(f"  {entry}" for entry in offenders)
    return f"{len(offenders)} external call(s) recorded in this run (tests/suiteguard.py):\n{shown}"


# --- git -------------------------------------------------------------------


def _is_external_url(url):
    match = _SCHEME.match(url)
    if match:
        return match.group(1).lower() != "file"
    # _HOST_PATH, not _SCP: a configured remote may be scp-like without
    # user@ (host:path). Direct operands use _HOST_PATH only in first
    # position (see _git_targets), so push refspecs (HEAD:refs/...) still
    # never count as remotes.
    return bool(_HOST_PATH.match(url))


def _split_global(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_VALUE_OPTS else 1
    return args[:i], args[i:]


def _remote_url(real, global_opts, name):
    proc = subprocess.run(
        [real, *global_opts, "remote", "get-url", name], capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _git_targets(real, global_opts, verb, operands):
    positional = [a for a in operands if not a.startswith("-")]
    if verb == "clone":
        return [p for p in positional if _SCHEME.match(p) or _HOST_PATH.match(p)]
    if verb == "archive":
        # Only --remote reaches the network; a local archive has no targets
        # and must not fall through to the origin check below.
        for i, arg in enumerate(operands):
            if arg == "--remote" and i + 1 < len(operands):
                return [operands[i + 1]]
            if arg.startswith("--remote="):
                return [arg.split("=", 1)[1]]
        return []
    if verb == "submodule":
        sub = positional[0] if positional else None
        if sub not in _SUBMODULE_NETWORK_SUBCOMMANDS:
            # status, summary, init and friends stay local.
            return []
        urls = [p for p in positional[1:] if _SCHEME.match(p) or _HOST_PATH.match(p)]
        if urls or sub in ("add", "set-url"):
            # add/set-url carry their URL; without one they are malformed
            # and the real git errors out below.
            return urls
        # update/sync/foreach consult configured URLs: approximate with the
        # origin check below, as for a bare fetch.
    targets = []
    for index, name in enumerate(positional):
        url = _remote_url(real, global_opts, name)
        if url is not None:
            targets.append(url)
        elif _SCHEME.match(name) or _SCP.match(name) or (index == 0 and _HOST_PATH.match(name)):
            # Only the first positional can be the remote: later ones are
            # refspecs, so a bare host:path matches there but a
            # HEAD:refs/... refspec never does.
            targets.append(name)
    if not targets:
        url = _remote_url(real, global_opts, "origin")
        if url is not None:
            targets.append(url)
    return targets


def git_main(argv):
    """argv: LOG REAL_GIT ARGS... Refuse external network git, exec the rest."""
    log, real, *args = argv
    global_opts, rest = _split_global(args)
    if rest and rest[0] in GIT_NETWORK_VERBS:
        targets = _git_targets(real, global_opts, rest[0], rest[1:])
        if any(_is_external_url(t) for t in targets):
            call = "git " + " ".join(args)
            record(call, log)
            sys.stderr.write(f"corral suite: blocked external git call: {call}\n")
            return 1
    os.execv(real, [real, *args])
    return 1


# --- sockets ---------------------------------------------------------------


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _external_address(address, call):
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if _loopback(host):
        return False
    port = address[1] if len(address) > 1 else ""
    record(f"socket {call} {host}:{port}")
    return True


def _patch_sockets():
    _real["connect"] = socket.socket.connect
    _real["connect_ex"] = socket.socket.connect_ex
    _real["sendto"] = socket.socket.sendto
    _real["create_connection"] = socket.create_connection
    _real["getaddrinfo"] = socket.getaddrinfo

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and _external_address(
            address, "connect"
        ):
            raise OSError(errno.ENETUNREACH, _REFUSED)
        return _real["connect"](self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and _external_address(
            address, "connect_ex"
        ):
            return errno.ENETUNREACH
        return _real["connect_ex"](self, address)

    def sendto(self, data, *args):
        # sendto(data, address) or sendto(data, flags, address).
        address = args[-1] if args else None
        if self.family in (socket.AF_INET, socket.AF_INET6) and _external_address(
            address, "sendto"
        ):
            raise OSError(errno.ENETUNREACH, _REFUSED)
        return _real["sendto"](self, data, *args)

    def create_connection(address, *args, **kwargs):
        if _external_address(address, "create_connection"):
            raise OSError(errno.ENETUNREACH, _REFUSED)
        return _real["create_connection"](address, *args, **kwargs)

    def getaddrinfo(host, port, *args, **kwargs):
        # A bare DNS lookup otherwise bypasses the connect guard. Only
        # loopback (and None, a server bind) goes to the real resolver.
        if host and not _loopback(host):
            record(f"socket getaddrinfo {host}:{port}")
            raise OSError(errno.ENETUNREACH, _REFUSED)
        return _real["getaddrinfo"](host, port, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.create_connection = create_connection  # ty: ignore[invalid-assignment]
    socket.getaddrinfo = getaddrinfo  # ty: ignore[invalid-assignment]


# --- shims -----------------------------------------------------------------


def _write_script(path, body):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)


def _tool_shim(tool, log):
    return (
        f'printf \'%s\\t%s %s\\n\' "${{{CALLER_ENV}:-<no-test>}}" {tool} "$*"'
        f" >> {shlex.quote(log)}\n"
        f"printf 'corral suite: blocked external call: %s %s\\n' {tool} \"$*\" >&2\n"
        "exit 1\n"
    )


def _git_shim(log, real):
    helper = os.path.abspath(__file__)
    return (
        'case " $* " in\n'
        '  *" push "* | *" fetch "* | *" pull "* | *" ls-remote "* | *" clone "*'
        ' | *" archive "* | *" submodule "*)'
        f"    exec {shlex.quote(sys.executable)} {shlex.quote(helper)} "
        f'{shlex.quote(log)} {shlex.quote(real)} "$@" ;;\n'
        "esac\n"
        f'exec {shlex.quote(real)} "$@"\n'
    )


def _outside_guard_path():
    return os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if _ROOT_PREFIX not in p
    )


# --- install ---------------------------------------------------------------


class ExternalCallCheck(unittest.TestCase):
    """Carrier for the run-level failure reported by ``report``."""

    def test_no_external_calls(self):
        """Never run: the failure is attached by ``report``."""


def report(result):
    """Fail ``result`` when any external call was recorded during the run."""
    global _checked
    _checked = True
    offenders = read_offenders()
    if not offenders:
        return
    try:
        raise AssertionError(_failure_message(offenders))
    except AssertionError:
        result.addFailure(ExternalCallCheck("test_no_external_calls"), sys.exc_info())


def _fallback(root):
    """Fail the process for runners that never reached ``report``."""
    if _checked:
        return
    offenders = read_offenders()
    if offenders:
        sys.stdout.flush()
        sys.stderr.write(_failure_message(offenders) + "\n")
        sys.stderr.flush()
        shutil.rmtree(root, True)
        os._exit(1)


def install():
    """Install shims, PATH, socket guard and per-test attribution; return the log path."""
    global _LOG
    root = tempfile.mkdtemp(prefix=_ROOT_PREFIX)
    _LOG = os.path.join(root, "calls.log")
    open(_LOG, "w", encoding="utf-8").close()
    shim_dir = os.path.join(root, "bin")
    os.mkdir(shim_dir)
    for tool in SHIM_TOOLS:
        _write_script(os.path.join(shim_dir, tool), _tool_shim(tool, _LOG))
    real_git = shutil.which("git", path=_outside_guard_path())
    if real_git:
        _write_script(os.path.join(shim_dir, "git"), _git_shim(_LOG, real_git))
    os.environ["PATH"] = shim_dir + os.pathsep + os.environ.get("PATH", "")
    _patch_sockets()

    real_run = unittest.TestCase.run

    def run(self, result=None):
        os.environ[CALLER_ENV] = self.id()
        try:
            return real_run(self, result)
        finally:
            os.environ.pop(CALLER_ENV, None)

    unittest.TestCase.run = run
    # atexit runs last-registered first: the fallback must run before the
    # rmtree, so it is registered after it.
    atexit.register(shutil.rmtree, root, True)
    atexit.register(_fallback, root)
    return _LOG


if __name__ == "__main__":
    sys.exit(git_main(sys.argv[1:]))
