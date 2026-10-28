"""Read-only health report: `corral doctor`.

One report over the checks that already exist, plus the host checks that do
not. It changes nothing and installs nothing; a failing check prints the
command a human would run, in the style of `corral deps`.

Every external call comes through `Env`: `run` reads `loginctl`,
`list_fn` lists the systemd user units, and the rest arrives as plain data
assembled by `production_env`. Only the CLI builds that; tests inject fakes.
"""

import argparse
import functools
import getpass
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from corral import accounts, deps, paths, registry, settings, units

OK, UNREACHABLE, USAGE = 0, 3, 4

SCHEMA_VERSION = 1


class Check(NamedTuple):
    """One row of the report: what was looked at, and how it reads."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str


class Env(NamedTuple):
    """Everything `run_checks` reads. All seams are required arguments."""

    dep_results: Sequence[deps.Result]
    account_problems: Sequence[str]
    settings_error: str | None
    state_error: str | None
    user: str
    unit_prefix: str
    run: Callable[[Sequence[str]], object]
    list_fn: Callable[[str], list]
    reader_errors: Sequence[tuple[str, str, str]]
    registry_rows: Sequence[tuple[Mapping, str]]


def _check_deps(results: Sequence[deps.Result]) -> list[Check]:
    failed = [r for r in results if r.problem and not r.dep.optional]
    if not failed:
        total = len([r for r in results if not r.dep.optional])
        return [Check("deps", "ok", f"{total} required programs present", "")]
    checks = []
    for result in failed:
        fix = " ".join(deps.fixes([result]))
        checks.append(Check(f"deps:{result.dep.name}", "fail", result.problem or "", fix))
    return checks


def _check_accounts(problems: Sequence[str]) -> Check:
    if not problems:
        return Check("accounts", "ok", "the accounts file is usable", "")
    return Check(
        "accounts",
        "fail",
        "; ".join(problems),
        "correct the named [[provider]] entry in accounts.toml (see `corral deps`)",
    )


def _check_settings(error: str | None) -> Check:
    if error is None:
        return Check("settings", "ok", "the host config and corral.toml resolve", "")
    return Check(
        "settings",
        "fail",
        error,
        "fix the named file, then `corral config show` to confirm it resolves",
    )


def _check_state_dirs(error: str | None) -> Check:
    if error is None:
        return Check("state-dir", "ok", "the state and config dirs are writable", "")
    return Check("state-dir", "fail", error, "create the directory or fix its permissions")


def _check_linger(user: str, run: Callable[[Sequence[str]], object]) -> Check:
    argv = ["loginctl", "show-user", user, "--property=Linger"]
    try:
        proc = run(argv)
    except OSError as err:
        return Check("linger", "fail", f"loginctl cannot be reached: {err}", _linger_fix())
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or "").strip()
        detail = f"loginctl cannot be reached{(': ' + err) if err else ''}"
        return Check("linger", "fail", detail, _linger_fix())
    out = getattr(proc, "stdout", "") or ""
    if "Linger=yes" in out:
        return Check("linger", "ok", f"linger is on for {user}", "")
    return Check(
        "linger",
        "warn",
        f"linger is off for {user}; agents die at logout",
        'run `loginctl enable-linger "$USER"`',
    )


def _linger_fix() -> str:
    return "log in on the host console so the user manager starts"


def _check_manager(prefix: str, list_fn: Callable[[str], list]) -> Check:
    try:
        found = list_fn(prefix)
    except units.Unreachable as err:
        return Check(
            "user-manager",
            "fail",
            str(err),
            "log in again so the user manager starts, then list the user units",
        )
    return Check("user-manager", "ok", f"the user manager answers ({len(found)} units)", "")


def _check_readers(errors: Sequence[tuple[str, str, str]]) -> list[Check]:
    if not errors:
        return [Check("quota-readers", "ok", "every configured reader loads", "")]
    return [
        Check(
            f"quota-readers:{provider or entry}",
            "fail",
            error,
            "correct the reader entry in accounts.toml (see `corral deps`)",
        )
        for provider, entry, error in errors
    ]


def _check_registry(rows: Sequence[tuple[Mapping, str]]) -> Check:
    counts = {"live": 0, "stale": 0, "finished": 0}
    unticketed = 0
    for entry, state in rows:
        counts[state] = counts.get(state, 0) + 1
        if state == registry.LIVE and registry.ticket_of(entry) is None:
            unticketed += 1
    total = len(rows)
    detail = (
        f"{total} entries (live={counts.get('live', 0)}, "
        f"stale={counts.get('stale', 0)}, finished={counts.get('finished', 0)})"
    )
    if not unticketed:
        return Check("registry", "ok", detail, "")
    return Check(
        "registry",
        "warn",
        f"{detail}; {unticketed} live agents hold no ticket",
        "the agent should register its ticket (`corral agent register --repo REPO --number N`)",
    )


def run_checks(env: Env) -> list[Check]:
    """Every check, in report order. Pure over the seams in `env`."""
    return [
        *_check_deps(env.dep_results),
        _check_accounts(env.account_problems),
        _check_settings(env.settings_error),
        _check_state_dirs(env.state_error),
        _check_linger(env.user, env.run),
        _check_manager(env.unit_prefix, env.list_fn),
        *_check_readers(env.reader_errors),
        _check_registry(env.registry_rows),
    ]


def exit_code(checks: Sequence[Check]) -> int:
    """0 ok or warn only, 1 at least one fail, 3 the manager bus is gone.

    Only linger and the user manager fail on an unreachable bus, so their
    failure alone selects 3.
    """
    if any(c.status == "fail" and c.name in ("linger", "user-manager") for c in checks):
        return UNREACHABLE
    if any(c.status == "fail" for c in checks):
        return 1
    return OK


_SYMBOLS = {"ok": "✓", "warn": "!", "fail": "✗"}


def render(checks: Sequence[Check], json_mode: bool = False) -> str:
    """The report: one line per check, or one JSON document."""
    if json_mode:
        document = {
            "schema_version": SCHEMA_VERSION,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                for c in checks
            ],
        }
        return json.dumps(document, indent=2) + "\n"
    lines = []
    for check in checks:
        lines.append(f"{_SYMBOLS[check.status]} {check.name}: {check.detail}")
        if check.status != "ok":
            lines.append(f"  fix: {check.fix}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ production


def check_state_dirs(env: Mapping[str, str] | None = None) -> str | None:
    """None when the state and config dirs exist and take a probe write.

    The probe goes through the one atomic-write helper and is removed
    after, so a passing check leaves nothing behind.
    """
    try:
        targets = (paths.state_dir(env), paths.config_dir(env))
    except paths.PathError as err:
        return str(err)
    for target in targets:
        try:
            os.makedirs(target, exist_ok=True)
            probe = os.path.join(target, ".doctor-probe")
            paths.atomic_write_text(probe, "ok")
            os.unlink(probe)
        except OSError as err:
            return f"{target} is not writable: {err}"
    return None


def check_settings(start: str, env: Mapping[str, str] | None = None) -> str | None:
    """None when the host config and the ops root's file resolve."""
    try:
        settings.load(root=settings.find_root(start), env=env)
    except (settings.SettingsError, paths.PathError) as err:
        return str(err)
    return None


def load_readers(config_dir: str | None = None) -> list[tuple[str, str, str]]:
    """(provider, entry, error) for every reader that does not load."""
    try:
        found = accounts.providers(config_dir)
    except accounts.AccountsError as err:
        return [("", "", str(err))]
    errors = []
    for provider in found:
        try:
            accounts.load_reader(provider.reader)
        except accounts.AccountsError as err:
            errors.append((provider.name, provider.reader, str(err)))
    return errors


def production_run(argv: Sequence[str]) -> object:
    """The real loginctl seam. Only the CLI builds it; tests never call this."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10)


def production_list_fn(prefix: str) -> list:
    """The real unit listing. Only the CLI builds it; tests never call this."""
    return units.list_units(prefix)


def production_env() -> Env:
    """The live environment. Only the CLI calls this; tests never call this."""
    root = settings.find_root(os.getcwd())
    try:
        resolved = settings.load(root=root)
        settings_error: str | None = None
    except (settings.SettingsError, paths.PathError) as err:
        resolved = None
        settings_error = str(err)
    prefix = (
        resolved["agents.unit_prefix"]
        if resolved is not None
        else settings.REGISTRY["agents.unit_prefix"].default
    )
    return Env(
        dep_results=deps.check_all(deps.load()),
        account_problems=accounts.check(),
        settings_error=settings_error,
        state_error=check_state_dirs(),
        user=getpass.getuser(),
        unit_prefix=str(prefix),
        run=production_run,
        list_fn=production_list_fn,
        reader_errors=load_readers(),
        registry_rows=registry.visible(time.time()),
    )


# ------------------------------------------------------------------- CLI


def cmd_doctor(args: argparse.Namespace, make_env: Callable[[], Env]) -> int:
    checks = run_checks(make_env())
    sys.stdout.write(render(checks, json_mode=args.json))
    return exit_code(checks)


def add_parser(sub, make_env: Callable[[], Env]) -> None:
    """Register `corral doctor` on the top-level subparsers."""
    parser = sub.add_parser(
        "doctor", help="read-only health report for the core's own prerequisites"
    )
    parser.add_argument("--json", action="store_true", help="one JSON document on stdout")
    parser.set_defaults(func=functools.partial(cmd_doctor, make_env=make_env))


def run_verb(argv: Sequence[str], make_env: Callable[[], Env]) -> int:
    """Parse and run `doctor` with the given environment. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="corral")
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub, make_env)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as err:
        return OK if err.code == 0 else USAGE
    return args.func(args)
