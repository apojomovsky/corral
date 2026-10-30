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
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple
from corral import accounts, boarddoctor, deps, paths, prompts, registry, settings, units
from corral import board as store

OK, UNREACHABLE, USAGE = 0, 3, 4

SCHEMA_VERSION = 1


class Check(NamedTuple):
    """One row of the report: what was looked at, and how it reads."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str


class Env(NamedTuple):
    """Everything `run_checks` reads. Only the first rows are required arguments."""

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
    registry_error: str | None = None
    deps_error: str | None = None
    #: Roles with invalid prompt overrides, as "role: problem".
    prompts_invalid: Sequence[str] = ()
    #: Roles whose ejected prompt is older than the built-in.
    prompts_stale: Sequence[str] = ()
    #: The prompts check itself failed (usage or configuration error).
    prompts_error: str | None = None
    #: The disk pressure (`ok`, `low`, `critical`, `unknown`), or None when
    #: the caller has no reading: then the report carries no disk row.
    disk: str | None = None
    #: Repos in board mode to check, with the seams that check them:
    #: `board_fetch` resolves one board config to its live schema and
    #: `board_open_store` opens the store parity rows come from. Empty
    #: means no board repos, and then the report carries no board row.
    board_sites: Sequence[boarddoctor.BoardSite] = ()
    board_fetch: Callable[[settings.BoardConfig], object] | None = None
    board_open_store: Callable[[], store.Store] | None = None
    #: The board assembly itself failed: one failing `board` row.
    board_error: str | None = None


def _check_deps(results: Sequence[deps.Result], error: str | None = None) -> list[Check]:
    if error is not None:
        return [
            Check(
                "deps",
                "fail",
                error,
                "reinstall corral so system_deps.toml parses, then run `corral deps`",
            )
        ]
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
    except (OSError, subprocess.SubprocessError) as err:
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


_MANAGER_BUS_FIX = "log in again so the user manager starts, then list the user units"


def _check_manager(prefix: str, list_fn: Callable[[str], list]) -> Check:
    try:
        found = list_fn(prefix)
    except units.Unreachable as err:
        return Check("user-manager", "fail", str(err), _MANAGER_BUS_FIX)
    except units.UnitsError as err:
        return Check(
            "user-manager",
            "fail",
            str(err),
            "fix `agents.unit_prefix` in the host config, then run `corral doctor` again",
        )
    return Check("user-manager", "ok", f"the user manager answers ({len(found)} units)", "")


def _check_readers(errors: Sequence[tuple[str, str, str]]) -> list[Check]:
    if not errors:
        return [Check("quota-readers", "ok", "every configured reader loads", "")]
    named = []
    for provider, entry, error in errors:
        name = provider or entry
        named.append(
            Check(
                f"quota-readers:{name}" if name else "quota-readers",
                "fail",
                error,
                "correct the reader entry in accounts.toml (see `corral deps`)",
            )
        )
    return named


def _check_registry(rows: Sequence[tuple[Mapping, str]], error: str | None = None) -> Check:
    if error is not None:
        return Check(
            "registry",
            "fail",
            error,
            "make the state dir usable, then run `corral doctor` again",
        )
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


def _check_prompts(invalid: Sequence[str], stale_roles: Sequence[str], error: str | None) -> Check:
    if error is not None:
        return Check(
            "prompts",
            "fail",
            error,
            "fix the prompts configuration, then run `corral prompts check`",
        )
    if invalid:
        return Check(
            "prompts",
            "fail",
            f"invalid prompt overrides: {'; '.join(invalid)}",
            "fix the placeholders or re-eject the template",
        )
    if stale_roles:
        return Check(
            "prompts",
            "warn",
            f"built-in changed since eject: {', '.join(stale_roles)}",
            "review the diff and re-eject with --force",
        )
    return Check("prompts", "ok", "the role prompts are built-in or valid overrides", "")


def _check_disk(pressure: str | None) -> list[Check]:
    """The disk row, reusing the report's row shape. No reading, no row."""
    if pressure is None:
        return []
    if pressure == "ok":
        return [Check("disk", "ok", "disk pressure ok", "")]
    if pressure == "low":
        return [
            Check(
                "disk",
                "warn",
                "disk pressure low: free space is under a threshold",
                "reclaim space: `corral disk` plans, `corral disk --apply` removes",
            )
        ]
    if pressure == "critical":
        return [
            Check(
                "disk",
                "fail",
                "disk pressure critical: free space is under half a threshold",
                "reclaim space now: `corral disk` plans, `corral disk --apply` removes",
            )
        ]
    return [
        Check(
            "disk",
            "warn",
            "disk pressure unknown: a filesystem could not be read",
            "check the state, cache and sessions dirs, then run `corral doctor` again",
        )
    ]


def _check_boards(env: Env) -> list[Check]:
    """One row per repo in board mode, mapped from the board slice."""
    if env.board_error is not None:
        return [
            Check(
                "board",
                "fail",
                env.board_error,
                "fix the ops root's corral.toml, then run `corral doctor` again",
            )
        ]
    return [
        Check(row.name, row.status, row.detail, row.fix)
        for row in boarddoctor.check_sites(
            env.board_sites, fetch=env.board_fetch, open_store=env.board_open_store
        )
    ]


def run_checks(env: Env) -> list[Check]:
    """Every check, in report order. Pure over the seams in `env`."""
    return [
        *_check_deps(env.dep_results, env.deps_error),
        _check_accounts(env.account_problems),
        _check_settings(env.settings_error),
        _check_state_dirs(env.state_error),
        _check_linger(env.user, env.run),
        _check_manager(env.unit_prefix, env.list_fn),
        *_check_readers(env.reader_errors),
        _check_registry(env.registry_rows, env.registry_error),
        _check_prompts(env.prompts_invalid, env.prompts_stale, env.prompts_error),
        *_check_boards(env),
        *_check_disk(env.disk),
    ]


def exit_code(checks: Sequence[Check]) -> int:
    """0 ok or warn only, 1 at least one fail, 3 the manager bus is gone.

    Only an unreachable bus selects 3: a linger failure, or a user-manager
    failure with the bus fix. A bad `agents.unit_prefix` is an ordinary
    fail with exit 1.
    """
    for check in checks:
        if check.status != "fail":
            continue
        if check.name == "linger":
            return UNREACHABLE
        if check.name == "user-manager" and check.fix == _MANAGER_BUS_FIX:
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
    after, so a passing check leaves nothing behind. The probe name carries
    the pid so two concurrent doctors do not share it.
    """
    try:
        targets = (paths.state_dir(env), paths.config_dir(env))
    except paths.PathError as err:
        return str(err)
    for target in targets:
        try:
            os.makedirs(target, exist_ok=True)
            probe = os.path.join(target, f".doctor-probe-{os.getpid()}")
            paths.atomic_write_text(probe, "ok")
            try:
                os.unlink(probe)
            except FileNotFoundError:
                pass
        except OSError as err:
            return f"{target} is not writable: {err}"
    return None


def _resolved_settings(
    start: str, env: Mapping[str, str] | None = None
) -> tuple[settings.Resolved | None, str | None]:
    """(resolved, error): the settings and their failure, resolved once."""
    try:
        return settings.load(root=settings.find_root(start), env=env), None
    except (settings.SettingsError, paths.PathError) as err:
        return None, str(err)


def _production_settings(
    env: Mapping[str, str] | None = None,
) -> tuple[settings.Resolved | None, str | None]:
    """The live settings without ever raising on a deleted working directory."""
    try:
        start = os.getcwd()
    except OSError as err:
        return None, f"cannot determine the working directory: {err}"
    return _resolved_settings(start, env)


def check_settings(start: str, env: Mapping[str, str] | None = None) -> str | None:
    """None when the host config and the ops root's file resolve."""
    return _resolved_settings(start, env)[1]


def load_dep_results() -> tuple[list[deps.Result], str | None]:
    """(results, error): the dep probes and their load failure, gathered once."""
    try:
        return deps.check_all(deps.load()), None
    except (
        tomllib.TOMLDecodeError,
        UnicodeDecodeError,
        OSError,
        TypeError,
        KeyError,
        re.error,
    ) as err:
        return [], str(err)


def load_readers(config_dir: str | None = None) -> list[tuple[str, str, str]]:
    """(provider, entry, error) for every reader that does not load."""
    try:
        found = accounts.providers(config_dir)
    except (accounts.AccountsError, paths.PathError) as err:
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


def production_board_fetch(board_config: settings.BoardConfig) -> object:
    """The live board schema fetch. Only the CLI builds it; tests never call this."""
    from corral.github import board as boardreader

    return boardreader.BoardReader(board_config, None, None).schema()


def load_board_sites(settings_error: str | None) -> tuple[tuple, str | None]:
    """Board-mode sites for the live report, or ((), error) that names it.

    Read-only: plain file reads plus the resolved per-repo tables. When
    the settings themselves failed, there is nothing to assemble, and
    the settings row already carries the failure.
    """
    if settings_error is not None:
        return (), None
    try:
        root = settings.find_root(os.getcwd())
    except OSError:
        return (), None
    if root is None:
        return (), None
    sites, error = boarddoctor.sites_from_settings(root)
    return tuple(sites), error


def load_prompts_state() -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """(invalid, stale, error) for the prompt overrides, gathered once.

    Every failure to look is data for a failing row, never a traceback:
    a broken settings file, a relative config path or an unreadable cwd.
    """
    try:
        found = prompts.check(settings.find_root(os.getcwd()))
    except (prompts.PromptsError, settings.SettingsError, paths.PathError, OSError) as err:
        return (), (), str(err)
    invalid = tuple(f"{role}: {found.invalid[role]}" for role in sorted(found.invalid))
    return invalid, tuple(found.stale), None


def load_disk_pressure(resolved) -> str:
    """The live disk pressure, best effort: `unknown` when unreadable."""
    from corral import disk as _disk

    try:
        readings = _disk.read(
            [paths.state_dir(), paths.cache_dir(), paths.sessions_dir()],
            usage=_disk.production_usage,
        )
        if resolved is None:
            return _disk.pressure(
                readings,
                min_free_pct=settings.REGISTRY["disk.min_free_pct"].default,
                min_free_gb=settings.REGISTRY["disk.min_free_gb"].default,
            )
        return _disk.pressure(
            readings,
            min_free_pct=resolved["disk.min_free_pct"],
            min_free_gb=resolved["disk.min_free_gb"],
        )
    except Exception:  # noqa: BLE001 - a broken dir reads as unknown, never a traceback
        return _disk.UNKNOWN


def production_env() -> Env:
    """The live environment. Only the CLI calls this; tests never call this.

    Every fallible gathering degrades to report data: a broken host config
    or an unusable state dir is a failing row, never a traceback.
    """
    resolved, settings_error = _production_settings()
    if resolved is not None:
        prefix = str(resolved["agents.unit_prefix"])
        try:
            registry_cfg = registry.config(resolved)
        except settings.SettingsError as err:
            settings_error = str(err)
            registry_cfg = registry.default_config()
    else:
        prefix = str(settings.REGISTRY["agents.unit_prefix"].default)
        registry_cfg = registry.default_config()
    try:
        account_problems = accounts.check()
    except (paths.PathError, accounts.AccountsError) as err:
        account_problems = [str(err)]
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = "unknown"
    try:
        registry_rows = registry.visible(time.time(), cfg=registry_cfg)
        registry_error = None
    except (paths.PathError, settings.SettingsError, registry.RegistryError) as err:
        registry_rows = ()
        registry_error = str(err)
    dep_results, deps_error = load_dep_results()
    prompts_invalid, prompts_stale, prompts_error = load_prompts_state()
    disk_pressure = load_disk_pressure(resolved)
    board_sites, board_error = load_board_sites(settings_error)
    return Env(
        dep_results=dep_results,
        deps_error=deps_error,
        account_problems=account_problems,
        settings_error=settings_error,
        state_error=check_state_dirs(),
        user=user,
        unit_prefix=prefix,
        run=production_run,
        list_fn=production_list_fn,
        reader_errors=load_readers(),
        registry_rows=registry_rows,
        registry_error=registry_error,
        prompts_invalid=prompts_invalid,
        prompts_stale=prompts_stale,
        prompts_error=prompts_error,
        disk=disk_pressure,
        board_sites=board_sites,
        board_fetch=production_board_fetch,
        board_open_store=store.open_store,
        board_error=board_error,
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
