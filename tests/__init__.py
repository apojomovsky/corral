"""Suite-wide guards: no test reaches the outside world or real state.

Installed at import, so every runner (discover, -k, named modules) is
covered: `suiteguard` puts failing shims for the external CLIs first on PATH,
refuses network git against non-local remotes and non-loopback sockets, and
`load_tests` fails the run on any recorded call. Every tempfile fixture lands
in one per-run root, and the state and session roots point at empty per-run
dirs, so no test can read or dirty the machine's real fleet.
"""

import atexit
import os
import shutil
import tempfile
import unittest

from . import suiteguard

suiteguard.install()

SUITE_TMP_ROOT = tempfile.mkdtemp(prefix="corral-suite-tmp-")
os.environ["TMPDIR"] = SUITE_TMP_ROOT
tempfile.tempdir = SUITE_TMP_ROOT
atexit.register(shutil.rmtree, SUITE_TMP_ROOT, True)

for _var in ("CORRAL_STATE", "CORRAL_SESSIONS", "CORRAL_CONFIG"):
    os.environ[_var] = tempfile.mkdtemp(prefix=_var.lower() + "-")


class _GuardedSuite(unittest.TestSuite):
    """Run the loaded tests, then fail the run if any external call was recorded."""

    def run(self, result, debug=False):
        result = super().run(result, debug)
        suiteguard.report(result)
        return result


def load_tests(loader, standard_tests, pattern):
    here = os.path.dirname(os.path.abspath(__file__))
    return _GuardedSuite(
        [loader.discover(here, pattern=pattern or "test*.py", top_level_dir=os.path.dirname(here))]
    )
