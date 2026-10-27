"""System dependencies: what the host needs, and how to get what it lacks.

The list is `system_deps.toml`, next to this module (linked from the
repository root as `system-deps.toml`). Checking runs each probe and changes
nothing. Fixes are printed as copy-pasteable commands and never run, since
most need sudo.
"""

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources

DEFAULT_PATTERN = r"(?P<version>\d+(?:\.\d+)+)"

# The one seam: run a probe argv, return its output, or None when the
# program is missing, fails or hangs. `paths` are fallbacks off PATH.
Probe = Callable[[Sequence[str], Sequence[str]], str | None]


@dataclass(frozen=True)
class Dep:
    name: str
    why: str
    probe: tuple[str, ...]
    pattern: str = DEFAULT_PATTERN
    min: str | None = None
    below: str | None = None
    paths: tuple[str, ...] = ()
    apt: str | None = None
    install: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class Result:
    dep: Dep
    version: str | None
    problem: str | None

    @property
    def ok(self) -> bool:
        return self.problem is None


def load(text: str | None = None) -> list[Dep]:
    if text is None:
        text = resources.files("corral").joinpath("system_deps.toml").read_text("utf-8")
    deps = []
    for entry in tomllib.loads(text).get("dep", []):
        entry = dict(entry)
        entry["probe"] = tuple(entry["probe"])
        entry["paths"] = tuple(entry.get("paths", ()))
        deps.append(Dep(**entry))
    return deps


def run_probe(argv: Sequence[str], paths: Sequence[str]) -> str | None:
    argv = [os.path.expandvars(arg) for arg in argv]
    exe = shutil.which(argv[0])
    if exe is None:
        exe = next((p for p in map(os.path.expanduser, paths) if os.access(p, os.X_OK)), None)
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout + proc.stderr


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text))


def check(dep: Dep, probe: Probe) -> Result:
    out = probe(dep.probe, dep.paths)
    if out is None:
        return Result(dep, None, "missing")
    match = re.search(dep.pattern, out)
    if match is None:
        return Result(dep, None, f"unexpected output from `{' '.join(dep.probe)}`")
    version = match.groupdict().get("version")
    if version and dep.min and _version(version) < _version(dep.min):
        return Result(dep, version, f"version {version}, needs {dep.min} or later")
    if version and dep.below and _version(version) >= _version(dep.below):
        return Result(dep, version, f"version {version}, needs below {dep.below}")
    return Result(dep, version, None)


def check_all(deps: Sequence[Dep], probe: Probe | None = None) -> list[Result]:
    probe = probe or run_probe
    return [check(dep, probe) for dep in deps]


def fixes(results: Sequence[Result]) -> list[str]:
    """Commands for the failed entries: one merged apt line first, then the rest."""
    failed = [r.dep for r in results if not r.ok]
    commands = []
    apt = [dep.apt for dep in failed if dep.apt]
    if apt:
        commands.append("sudo apt install -y " + " ".join(dict.fromkeys(apt)))
    for dep in failed:
        if not dep.apt and dep.install and dep.install not in commands:
            commands.append(dep.install)
    return commands


def report(results: Sequence[Result]) -> tuple[str, bool]:
    """The text to print, and whether every required entry passed."""
    rows = []
    for r in results:
        if r.ok:
            rows.append(("ok", r.dep.name, r.version or "yes", r.dep.why))
        else:
            status = "optional" if r.dep.optional else "MISSING"
            rows.append((status, r.dep.name, r.problem or "", r.dep.why))
    widths = [max((len(row[i]) for row in rows), default=0) for i in range(3)]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row[:3], widths)) + f"  {row[3]}" for row in rows
    ]
    required = [r for r in results if not r.dep.optional]
    optional = [r for r in results if r.dep.optional]
    ok = all(r.ok for r in required)
    for title, group in (("To fix, run:", required), ("Optional:", optional)):
        commands = fixes(group)
        if commands:
            lines += ["", title, ""] + [f"  {c}" for c in commands]
    return "\n".join(lines) + "\n", ok
