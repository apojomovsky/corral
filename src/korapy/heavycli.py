"""`korapy heavy`: queue heavy commands in the lane, plan them, read the lane.

`run` joins the lane with the request's weight, waits the turn, runs
the command in a transient scope with the lane's limits and releases
the slot on exit. Past the wait deadline the waiter leaves with 75,
keeping the public meaning (no free slot, try later). `run --plan`
reports the verdict without queueing or running anything. `status`
shows holders, waiters and learned costs. `rollup` regenerates the
portable per-key cost summary from the raw run ledger: stdout always
carries the JSON document, and `--out PATH` additionally writes the
same bytes for the operator to commit. Exit codes follow the public
table: 0 ok, 1 nothing to do (or a wait that would block, for `--plan`),
3 the scope could not be created, 4 usage or configuration, 75 no slot.
"""

import argparse
import functools
import json
import os
import subprocess
import sys

OK, NO, UNREACHABLE, USAGE, NO_SLOT = 0, 1, 3, 4, 75


def _err(text):
    print(f"korapy heavy: {text}", file=sys.stderr)


def _lane_exit(err):
    """The CLI exit for a lane failure: 75 a full lane, 3 a lost scope, else 4."""
    from korapy import heavy

    if isinstance(err, (heavy.LaneLocked, heavy.LaneTimeout)):
        return NO_SLOT
    if isinstance(err, heavy.ScopeUnreachable):
        return UNREACHABLE
    return USAGE


def _resolve(root):
    """(resolved, cfg, budget) for `root`, else a raiser.

    Loads the project settings, validates the ``[heavy]`` table, and
    resolves the live lane budget. Settings and budget failures raise
    for the callers to map to exit 4; the queue wait re-resolves the
    live budget every admit pass through `budget_fn`, and the next lane
    write refreshes the cached copy.
    """
    from korapy import heavy, paths, resources, settings

    resolved = settings.load(root=root)
    where = os.path.join(root, settings.PROJECT_FILE)
    cfg = heavy.check_table(resolved.heavy or None, where)
    host = resources.config(resolved).host
    host_where = os.path.join(paths.config_dir(), settings.HOST_CONFIG_NAME)
    budget = heavy.lane_budget(cfg, host, where=host_where)
    return resolved, cfg, budget


def _live_pressure():
    """The live queue pressure reading; unknown when unreadable, never raising."""
    from korapy import resources

    try:
        reading, _notes = resources.read_pressure()
    except Exception:  # noqa: BLE001 - an unreadable host reads as unknown
        return None
    return reading


def _pressure_caps(resolved):
    """The shared `[resources.pressure]` line, else the conservative built-ins."""
    from korapy import resources

    try:
        line = resources.config(resolved).pressure
    except Exception:  # noqa: BLE001 - unreadable config reads as built-ins
        return resources.PressureCaps()
    return line if line is not None else resources.PressureCaps()


def cmd_status(args, *, state_dir=None, now=None):
    """Run the verb. The lane lives in the state dir; tests pass a fake `now`."""
    from korapy import heavy, settings

    try:
        lane = heavy.read_lane(state_dir)
    except (heavy.HeavyError, settings.SettingsError) as err:
        _err(str(err))
        return _lane_exit(err)
    report = heavy.status_report(lane, now)
    if args.json:
        print(json.dumps(report, sort_keys=True))
        return OK
    budget = report["budget"]
    if budget is None:
        print("heavy: no lane budget (every request fits at once)")
    else:
        print(f"heavy: budget cpu {budget['cpu_m']}m mem {budget['memory_mb']}MB")
    if not report["holders"] and not report["waiters"]:
        print("heavy: lane empty")
    for holder in report["holders"]:
        weight = holder["weight"]
        print(
            f"heavy: holder {holder['id'][:8]} {' '.join(holder['command'])} "
            f"cpu {weight['cpu_m']}m mem {weight['memory_mb']}MB since {holder['since']}"
        )
    for waiter in report["waiters"]:
        weight = waiter["weight"]
        print(
            f"heavy: waiter {waiter['id'][:8]} {' '.join(waiter['command'])} "
            f"cpu {weight['cpu_m']}m mem {weight['memory_mb']}MB "
            f"waited {waiter['waited_s']}s"
        )
    if report["costs"]:
        for key in sorted(report["costs"]):
            print(f"heavy: cost {key}: {json.dumps(report['costs'][key], sort_keys=True)}")
    else:
        print("heavy: costs: none yet")
    return OK


def cmd_run(
    args,
    *,
    run_lane=None,
    root=None,
    now=None,
    pressure=None,
    pressure_caps=None,
    pressure_fn=None,
    budget_fn=None,
):
    """Run the verb. `run_lane` defaults to the live queue; tests pass a fake.

    `pressure` is the admission-time reading the plan verdict reads
    (live when None); `pressure_caps` is the shared `[resources.pressure]`
    line (resolved when None). `pressure_fn` reads the live queue
    pressure every admit pass and `budget_fn` re-resolves the live lane
    budget every pass; both default to live readers, and tests pass
    fakes. The ledger row carries the admitting pass's reading.
    """
    from korapy import gitenv, heavy, settings

    argv = list(args.command or [])
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        _err("a command is required: korapy heavy run -- CMD [ARGS...]")
        return USAGE
    root = root or settings.find_root(os.getcwd()) or os.getcwd()
    try:
        resolved, cfg, budget = _resolve(root)
        heavy.repo_key(root)
        name = heavy.command_name(argv)
        weight = heavy.hand_weight(cfg, name)
    except (heavy.HeavyError, settings.SettingsError) as err:
        _err(str(err))
        return _lane_exit(err)
    raw_timeout = args.timeout
    if raw_timeout is None:
        wait_s = float(cfg.wait_timeout_s if cfg is not None else heavy.DEFAULT_WAIT_TIMEOUT_S)
    else:
        try:
            wait_s = float(raw_timeout)
        except (TypeError, ValueError):
            _err("--timeout needs seconds")
            return USAGE
        if isinstance(raw_timeout, bool) or not wait_s >= 0:
            _err("--timeout needs seconds")
            return USAGE
    caps = pressure_caps if pressure_caps is not None else _pressure_caps(resolved)
    if args.plan:
        try:
            lane = heavy.read_lane()
        except (heavy.HeavyError, settings.SettingsError) as err:
            _err(str(err))
            return _lane_exit(err)
        reading = _live_pressure() if pressure is None else pressure
        report = heavy.plan_report(weight, lane, budget, pressure=reading, pressure_caps=caps)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        elif report["admitted"]:
            print(f"heavy: would run now ({report['reason']})")
        else:
            print(f"heavy: would wait at position {report['position']} ({report['reason']})")
        return OK if report["admitted"] else NO
    cmd_timeout = resolved["checks.timeout_s"]
    if isinstance(cmd_timeout, bool) or not isinstance(cmd_timeout, int) or cmd_timeout <= 0:
        _err("checks.timeout_s must be a positive number of seconds")
        return USAGE
    live_budget_fn = budget_fn
    if live_budget_fn is None:

        def live_budget_fn():
            _resolved, _cfg, _budget = _resolve(root)
            return _budget

    live_pressure_fn = pressure_fn
    if live_pressure_fn is None:

        def live_pressure_fn():
            return _live_pressure()

    runner = run_lane or functools.partial(
        heavy.run_through_lane,
        run=heavy.default_run,
        teardown=functools.partial(heavy.stop_scope, run=heavy.default_run),
        scope_cgroup=heavy.show_cgroup,
        max_skips=cfg.max_skips if cfg is not None else heavy.DEFAULT_MAX_SKIPS,
        scope_exists=heavy.scope_exists,
        is_alive=heavy.pid_alive,
        pressure_caps=caps,
        pressure_fn=live_pressure_fn,
        budget_fn=live_budget_fn,
    )
    try:
        lane_run = runner(
            argv,
            root,
            gitenv.clean_env(),
            role="hand",
            weight=weight,
            budget=budget,
            timeout_s=wait_s,
            timeout=cmd_timeout,
            starved_ratio=cfg.starved_ratio if cfg is not None else heavy.DEFAULT_STARVED_RATIO,
            now=now,
        )
    except subprocess.TimeoutExpired as err:
        _err(f"{' '.join(argv)} timed out after {err.timeout}s")
        return NO
    except (heavy.HeavyError, settings.SettingsError) as err:
        _err(str(err))
        return _lane_exit(err)
    result = lane_run.result
    verdict = {
        "schema_version": heavy.SCHEMA_VERSION,
        "command": argv,
        "weight": {"cpu_m": weight.cpu_m, "memory_mb": weight.memory_mb},
        "waited_s": lane_run.waited_s,
        "admitted": True,
        "bypassed": False,
        "exit_code": result.exit_code,
        "measured": result.measured,
    }
    if args.json:
        sys.stdout.write(result.stdout + result.stderr)
        sys.stderr.write(json.dumps(verdict, sort_keys=True) + "\n")
    else:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.stderr.write(
            f"heavy: ran in {result.wall_s}s after a {lane_run.waited_s}s wait, "
            f"exit {result.exit_code}\n"
        )
    return result.exit_code


def cmd_rollup(args, *, state_dir=None, now=None):
    """Run the verb. The ledger lives in the state dir; tests pass fakes."""
    from korapy import heavy, paths

    try:
        rows, sources, skipped = heavy.collect_ledger_rows(state_dir)
    except heavy.HeavyError as err:
        _err(str(err))
        return USAGE
    document = heavy.build_rollup(rows, sources, now=now)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text)
    if skipped:
        print(f"korapy heavy: skipped {skipped} corrupt ledger rows", file=sys.stderr)
    if getattr(args, "out", None):
        try:
            paths.atomic_write_text(args.out, text)
        except OSError as err:
            _err(f"cannot write the rollup to {args.out}: {err}")
            return USAGE
    return OK


def cmd_heavy(args):
    """Run the verb. The seams default to the live ones; tests pass fakes."""
    if args.heavy_verb == "status":
        return cmd_status(args)
    if args.heavy_verb == "rollup":
        return cmd_rollup(args)
    return cmd_run(args)


def add_parser(sub) -> None:
    """Register `heavy` and its verbs on the top-level subparsers."""
    heavy_parser = sub.add_parser(
        "heavy",
        help="queue heavy commands in the lane, or inspect it",
        description="Queue one heavy command, run it in a transient scope with "
        "CPU and memory limits, and release on exit. Exit 0 when the command "
        "ran, 1 when a planned run would wait or the command failed, 3 when "
        "a lane scope could not be created, 4 on usage or configuration, "
        "75 past the wait deadline.",
    )
    verbs = heavy_parser.add_subparsers(dest="heavy_verb", required=True)
    run = verbs.add_parser(
        "run",
        help="queue one heavy command and run it in a transient scope",
    )
    run.add_argument(
        "--timeout",
        default=None,
        help="cap this queue wait in seconds (default: heavy.wait_timeout_s)",
    )
    run.add_argument(
        "--plan",
        action="store_true",
        help="report the verdict without queueing or running anything",
    )
    run.add_argument(
        "--json",
        action="store_true",
        help="machine-readable verdict on stderr; the command keeps stdout",
    )
    run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="-- CMD [ARGS...]: the command to queue",
    )
    status = verbs.add_parser(
        "status",
        help="holders, waiters and learned costs",
    )
    status.add_argument("--json", action="store_true", help="machine-readable output")
    rollup = verbs.add_parser(
        "rollup",
        help="regenerate the portable per-key cost summary",
    )
    rollup.add_argument(
        "--out",
        default=None,
        help="additionally write the same JSON document to PATH",
    )
    heavy_parser.set_defaults(func=cmd_heavy)
