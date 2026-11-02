"""Derive a reviewer dispatch's time budget from the diff it will read.

The review gate asks the dispatcher to state an expected duration and a
hard limit, but a hand-picked number is unenforceable and varies by agent.
This module computes both from the scope of the diff, using the same
`<base>...HEAD` revision range the takeoff ritual diffs against, so the
budget is reproducible on any machine and a reviewer that overruns its
stated limit can be cancelled on a rule rather than a hunch.

Everything is in whole minutes, and the limit is derived from the rounded
expectation rather than the other way round: the gate's floor (the limit
is at least the configured multiple of the expectation) has to hold for
the numbers that get printed, not merely for the seconds behind them.

The hard limit is capped below the machine ceiling, because a limit at or
above it is not a limit: the agent runtime would kill the reviewer first,
and the dispatcher would have no moment at which to cancel deliberately.
When a diff is too large to review inside that cap, the honest answer is
to split it, which this module says outright instead of handing back a
budget the dispatcher cannot enforce.

The five numbers are `review.budget.*` settings, so each project tunes
its own; the defaults are corral's own review gate. `numbers()` is the
one resolver: every reader goes through it.
"""

import argparse
import os
import subprocess
import sys
import textwrap
from typing import NamedTuple

from corral import settings

# Minutes a reviewer needs before the diff size matters: loading context,
# building, and running the repository's fast gates at least once. This
# alone keeps the hard limit above the gate's 30-minute floor.
BASE_MINUTES = 15

# Seconds of review per changed line. A line count is a proxy for how much
# reasoning the diff demands; it is not a reading-speed estimate.
SECONDS_PER_LINE = 6

# The gate requires the hard limit to be at least this multiple of the
# expectation, so a reviewer has room to recover from one wrong turn.
LIMIT_MULTIPLE = 3

# The gate never allows a limit below this, however small the diff.
GATE_MIN_LIMIT_MINUTES = 30

# The agent runtime's own ceiling for a dispatched subagent, in minutes
# (for oh-my-pi, `task.maxRuntimeMs` = 7200000 ms). A stated limit at or
# above this is unreachable.
MACHINE_CEILING_MINUTES = 120

# A limit has to stay strictly below the kill point, or the dispatcher
# never gets to act on it. The cap on the expectation below derives from
# that one bound, so the cap and the split threshold cannot drift apart.

# The defaults above are corral's own review gate (the 3x multiple and the
# 30-minute floor in AGENTS.md). A project configures its own numbers
# through these settings; the gate rule stays corral's, not the project's.


def _positive_int(value):
    """One budget number's range, for the settings layer: a positive int.

    The settings layer names the file and key around this rule; numbers()
    below keeps only the checks that compare two numbers.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"must be a positive integer, not {value!r}")
    return value


settings.register(
    "review.budget.base_minutes",
    int,
    BASE_MINUTES,
    "reviewer minutes before the diff size matters (loading, build, fast gates)",
    check=_positive_int,
)
settings.register(
    "review.budget.seconds_per_line",
    int,
    SECONDS_PER_LINE,
    "reviewer seconds per changed line in the diff",
    check=_positive_int,
)
settings.register(
    "review.budget.limit_multiple",
    int,
    LIMIT_MULTIPLE,
    "hard limit as a multiple of the expected duration",
    check=_positive_int,
)
settings.register(
    "review.budget.floor_minutes",
    int,
    GATE_MIN_LIMIT_MINUTES,
    "hard limit floor in minutes, however small the diff",
    check=_positive_int,
)
settings.register(
    "review.budget.ceiling_minutes",
    int,
    MACHINE_CEILING_MINUTES,
    "agent runtime ceiling in minutes for one dispatched reviewer",
    check=_positive_int,
)

# Standing checks every bug-fix review makes beyond its one question: the
# two kinds of miss that recur in merged fixes (a fact resolved in two
# places that disagree; a key that can hold two live states). The launch
# and resume prompts quote this text, so the reviewer hears it from every
# surface.
BUGFIX_REVIEW_CHECKS = (
    "For a bug fix, the reviewer also checks two things beyond its "
    "question: whether the fact the fix touches (a path, a setting, a "
    "state) is resolved anywhere else, and the two agree; and whether "
    "two or more of the thing the code keys on can exist, and that case "
    "is tested."
)


class Numbers(NamedTuple):
    """The five budget numbers for one project: the one resolved record.

    Every reader below goes through `numbers()`; nothing reads the module
    constants or the settings registry directly.
    """

    base_minutes: int
    seconds_per_line: int
    limit_multiple: int
    floor_minutes: int
    ceiling_minutes: int


#: The registered keys, in Numbers field order.
NUMBERS_KEYS = (
    "review.budget.base_minutes",
    "review.budget.seconds_per_line",
    "review.budget.limit_multiple",
    "review.budget.floor_minutes",
    "review.budget.ceiling_minutes",
)

#: What the `corral budget` verb prints for --help: the same text serves the
#: standalone `python -m corral.budget`, so the two spellings agree.
BUDGET_HELP = "derive a reviewer dispatch's time budget from the diff scope"
BUDGET_EPILOG = (
    "Prints the dispatch block the review gate asks for: the one "
    "question, the scope, and an expected duration plus hard limit "
    "computed from <base>...HEAD. Paste it into the reviewer prompt "
    "so the budget is reproducible rather than hand-picked. The "
    "numbers come from the review.budget.* settings, so --repo prints "
    "that entry's budget."
)


def _setting_int(raw, key):
    """The resolved value for `key` as an int.

    The settings layer only stores type-checked ints here, so this is a
    narrowing guard for hand-built inputs: a non-int is a configuration
    error (exit 4), never an arithmetic traceback. The range check lives
    where the value is set; only the type is reaffirmed here.
    """
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise settings.SettingsError(f"{key} must be an integer, not {value!r}")
    return value


def numbers(resolved=None):
    """The budget numbers, from `resolved` or from the built-in defaults.

    With no resolved settings the result is today's constants, so current
    behavior is byte-identical. Each number's own range (a positive int) is
    checked where the value is set, naming the file and key. The checks
    that compare two numbers stay here, and each names the layer behind
    the offending values, which the resolved settings already record.
    """
    if resolved is None:
        raw = {key: settings.REGISTRY[key].default for key in NUMBERS_KEYS}
        origins = dict.fromkeys(NUMBERS_KEYS, settings.DEFAULT)
    else:
        raw = {key: resolved[key] for key in NUMBERS_KEYS}
        origins = {key: resolved.values[key].origin for key in NUMBERS_KEYS}
    cfg = Numbers(
        base_minutes=_setting_int(raw, "review.budget.base_minutes"),
        seconds_per_line=_setting_int(raw, "review.budget.seconds_per_line"),
        limit_multiple=_setting_int(raw, "review.budget.limit_multiple"),
        floor_minutes=_setting_int(raw, "review.budget.floor_minutes"),
        ceiling_minutes=_setting_int(raw, "review.budget.ceiling_minutes"),
    )
    floor_key = "review.budget.floor_minutes"
    ceiling_key = "review.budget.ceiling_minutes"
    if cfg.floor_minutes >= cfg.ceiling_minutes:
        raise settings.SettingsError(
            f"{floor_key} ({cfg.floor_minutes}, from {origins[floor_key]}) must be "
            f"below {ceiling_key} ({cfg.ceiling_minutes}, from {origins[ceiling_key]})"
        )
    base_key = "review.budget.base_minutes"
    multiple_key = "review.budget.limit_multiple"
    smallest = min(cfg.base_minutes, _max_expected(cfg)) * cfg.limit_multiple
    if smallest < cfg.floor_minutes:
        raise settings.SettingsError(
            "review.budget leaves no printable budget: the smallest limit "
            f"({smallest} minutes, from base_minutes {cfg.base_minutes} set by "
            f"{origins[base_key]} and limit_multiple {cfg.limit_multiple} set by "
            f"{origins[multiple_key]}) is below {floor_key} "
            f"({cfg.floor_minutes}, from {origins[floor_key]}); raise {base_key} "
            f"or {multiple_key}, or lower {floor_key}"
        )
    return cfg


def load_numbers(root=None, repo=None):
    """The budget numbers for this project, from the files on disk.

    `root` is the ops root (None reads host config plus defaults); `repo`
    selects the `[[repo]]` entry that wins for that repository.
    """
    return numbers(settings.load(root=root, repo=repo))


def _max_expected(cfg):
    """Largest expectation whose multiple still stays under the ceiling."""
    return (cfg.ceiling_minutes - 1) // cfg.limit_multiple


def _git(args, cwd=None):
    from .gitenv import clean_env

    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=60, env=clean_env()
    )


def changed_lines(base, cwd=None):
    """Added-plus-removed lines in `base...HEAD`, or None when git cannot tell.

    Returns None (not zero) when the range does not resolve, so a caller can
    say "unknown" instead of reporting a budget computed from nothing.
    """
    result = _git(["diff", "--numstat", f"{base}...HEAD"], cwd=cwd)
    if result.returncode != 0:
        return None
    total = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, removed = parts[0], parts[1]
        # A binary file reports `-` for both counts: it carries no reviewable
        # lines, so it contributes nothing to the budget.
        if added.isdigit():
            total += int(added)
        if removed.isdigit():
            total += int(removed)
    return total


def natural_expected_minutes(lines, cfg=None):
    """What the diff alone asks for, before the ceiling shortens it."""
    cfg = numbers() if cfg is None else cfg
    seconds = cfg.base_minutes * 60 + cfg.seconds_per_line * max(0, lines)
    return (seconds + 59) // 60


def needs_splitting(lines, cfg=None):
    """Whether one review cannot honestly cover this diff."""
    cfg = numbers() if cfg is None else cfg
    return natural_expected_minutes(lines, cfg) > _max_expected(cfg)


def max_reviewable_lines(cfg=None):
    """Largest diff that still gets a full-length expectation.

    Derived from the same bound as the budget, so a diff flagged for
    splitting is exactly one whose expectation had to be shortened.
    """
    cfg = numbers() if cfg is None else cfg
    # natural_expected_minutes(n) <= max_expected
    #   <=> base*60 + seconds_per_line*n <= max_expected*60
    room = _max_expected(cfg) * 60 - cfg.base_minutes * 60
    return max(0, room // cfg.seconds_per_line)


def budget(lines, cfg=None):
    """(expected_minutes, limit_minutes) for a diff of `lines` lines.

    Both are whole minutes, so the printed pair satisfies the gate's floor:
    the limit is the multiple of the rounded expectation, never the ceiling.
    """
    cfg = numbers() if cfg is None else cfg
    expected = min(natural_expected_minutes(lines, cfg), _max_expected(cfg))
    limit = expected * cfg.limit_multiple
    # The floor holds because the smallest printable limit clears it; the
    # check keeps that a fact rather than a coincidence for a hand-built
    # Numbers that numbers() never saw. A SettingsError is exit 4, not a
    # traceback, since a bad budget is a configuration error.
    if limit < cfg.floor_minutes:
        raise settings.SettingsError(
            f"budget {expected}m/{limit}m is below the {cfg.floor_minutes}-minute "
            "floor; raise review.budget.base_minutes or review.budget.limit_multiple"
        )
    return expected, limit


def describe(base, cwd=None, cfg=None):
    """Lines of report for a reviewer dispatch, or a reason it cannot be computed."""
    cfg = numbers() if cfg is None else cfg
    lines = changed_lines(base, cwd=cwd)
    if lines is None:
        return [f"cannot compute a budget: {base}...HEAD does not resolve"]
    expected, limit = budget(lines, cfg)
    report = [
        f"diff scope: {lines} changed lines against {base}",
        f"expected duration: {expected} minutes",
        f"hard limit: {limit} minutes",
    ]
    if needs_splitting(lines, cfg):
        report.append(
            f"this diff is {lines} lines; one review covers about "
            f"{max_reviewable_lines(cfg)} inside the {cfg.ceiling_minutes}-minute "
            "machine ceiling. Split it into that many reviews, or scope this one "
            "to the part that carries the risk."
        )
    return report


def dispatch_block(base, cwd=None, cfg=None):
    """The paste-able dispatch text, budget plus the gate's own requirements."""
    cfg = numbers() if cfg is None else cfg
    body = [
        "Review dispatch (canonical rule: AGENTS.md, Review gate):",
        "",
        "  question: <the ONE thing this review must answer>",
        "  scope: the diff only, against this repository's own conventions",
    ]
    lines = changed_lines(base, cwd=cwd)
    if lines is None:
        body.append("  budget: unknown, set one by hand (see the reason below)")
    else:
        expected, limit = budget(lines, cfg)
        body.append(f"  budget: expected {expected} minutes, hard limit {limit} minutes")
    body += textwrap.wrap(
        BUGFIX_REVIEW_CHECKS, width=72, initial_indent="  bug fix: ", subsequent_indent=" " * 11
    )
    body += [
        "  wrap-up: a wrap-up signal may arrive; finalize immediately then, and",
        "           flag anything unfinished as PARTIAL rather than continuing.",
        "  headless: never background a command and end your turn to wait",
        "           for it; run long steps (builds, full test suites) in",
        "           the foreground and wait. A background job dies with",
        "           your turn and never reports back.",
        "",
        "Report findings as they land, not only at the end, so a reviewer that",
        "runs out of budget still leaves the work it did.",
        "",
    ]
    body += describe(base, cwd=cwd)
    return "\n".join(body)


def add_flags(parser):
    """--base/--root/--repo on `parser`, shared by the two budget spellings.

    The one definition of the flags: the `corral budget` verb and the
    standalone parser below both build from here, so a flag means the same
    thing however the budget is run.
    """
    from corral.diffscope import BASE_HELP

    parser.add_argument("--base", default=None, help=f"base ref to diff against ({BASE_HELP})")
    parser.add_argument(
        "--root", default=None, help="the ops root (default: nearest corral.toml upward)"
    )
    parser.add_argument("--repo", default=None, help="resolve for this [[repo]] entry")


class _Exit4Parser(argparse.ArgumentParser):
    """An ArgumentParser that exits 4 on a usage error instead of 2.

    Exit codes are a public interface: 4 is the configuration-or-usage
    error, whatever the spelling. --help still exits 0.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(4, f"{self.prog}: error: {message}\n")


def build_parser():
    """The standalone budget parser: the same flags as the `corral budget` verb.

    Unknown or valueless flags exit 4, the usage-error code, like every
    other corral spelling.
    """
    parser = _Exit4Parser(prog="corral budget", description=BUDGET_HELP, epilog=BUDGET_EPILOG)
    add_flags(parser)
    return parser


def resolve_root_repo(root, repo):
    """The (root, repo) pair for a budget run, else a SettingsError (exit 4).

    The one validation of the two spellings' --root/--repo pair: a --root
    must hold a corral.toml, and --repo needs the ops root that file lives
    in. The messages match the `corral config` verb's, so every spelling
    agrees word for word.
    """
    if root and not os.path.isfile(os.path.join(root, settings.PROJECT_FILE)):
        raise settings.SettingsError(f"no {settings.PROJECT_FILE} in --root {root}")
    root = root or settings.find_root(os.getcwd())
    if repo and not root:
        raise settings.SettingsError("--repo needs an ops root (corral.toml)")
    return root, repo


def main_cli(argv):
    """`python -m corral.budget [--base <ref>]`: print the dispatch block.

    Built from the same parser and root/repo check as the `corral budget`
    verb, so the two spellings cannot disagree about flags, a missing
    project file, or bad values.
    """
    from corral.diffscope import default_base

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as err:
        return 0 if err.code == 0 else 4
    try:
        root, repo = resolve_root_repo(args.root, args.repo)
        cfg = load_numbers(root=root, repo=repo)
        text = dispatch_block(default_base(args.base), cfg=cfg)
    except settings.SettingsError as err:
        print(f"corral budget: {err}", file=sys.stderr)
        return 4
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main_cli(sys.argv[1:]))
