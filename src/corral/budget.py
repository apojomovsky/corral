"""Derive a reviewer dispatch's time budget from the diff it will read.

The review gate asks the dispatcher to state an expected duration and a
hard limit, but a hand-picked number is unenforceable and varies by agent.
This module computes both from the scope of the diff, using the same
`<base>...HEAD` revision range the takeoff ritual diffs against, so the
budget is reproducible on any machine and a reviewer that overruns its
stated limit can be cancelled on a rule rather than a hunch.

Everything is in whole minutes, and the limit is derived from the rounded
expectation rather than the other way round: the gate's floor (the limit
is at least three times the expectation) has to hold for the numbers that
get printed, not merely for the seconds behind them.

The hard limit is capped below the machine ceiling, because a limit at or
above it is not a limit: the agent runtime would kill the reviewer first,
and the dispatcher would have no moment at which to cancel deliberately.
When a diff is too large to review inside that cap, the honest answer is
to split it, which this module says outright instead of handing back a
budget the dispatcher cannot enforce.
"""

import os
import subprocess
import textwrap

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
# never gets to act on it. Everything else is derived from that one bound,
# so the cap and the split threshold cannot drift apart.
MAX_LIMIT_MINUTES = MACHINE_CEILING_MINUTES - 1
MAX_EXPECTED_MINUTES = MAX_LIMIT_MINUTES // LIMIT_MULTIPLE

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


def natural_expected_minutes(lines):
    """What the diff alone asks for, before the ceiling shortens it."""
    seconds = BASE_MINUTES * 60 + SECONDS_PER_LINE * max(0, lines)
    return (seconds + 59) // 60


def needs_splitting(lines):
    """Whether one review cannot honestly cover this diff."""
    return natural_expected_minutes(lines) > MAX_EXPECTED_MINUTES


def max_reviewable_lines():
    """Largest diff that still gets a full-length expectation.

    Derived from the same bound as the budget, so a diff flagged for
    splitting is exactly one whose expectation had to be shortened.
    """
    # natural_expected_minutes(n) <= MAX_EXPECTED_MINUTES
    #   <=> BASE*60 + SECONDS_PER_LINE*n <= MAX_EXPECTED_MINUTES*60
    room = MAX_EXPECTED_MINUTES * 60 - BASE_MINUTES * 60
    return max(0, room // SECONDS_PER_LINE)


def budget(lines):
    """(expected_minutes, limit_minutes) for a diff of `lines` lines.

    Both are whole minutes, so the printed pair satisfies the gate's floor:
    the limit is three times the rounded expectation, never the ceiling.
    """
    expected = min(natural_expected_minutes(lines), MAX_EXPECTED_MINUTES)
    limit = expected * LIMIT_MULTIPLE
    # The gate also floors the limit at 30 minutes. That holds because the
    # base expectation alone is above a third of it; the check keeps the
    # floor a fact rather than a coincidence if BASE_MINUTES is ever tuned.
    assert limit >= GATE_MIN_LIMIT_MINUTES, (
        f"budget {expected}m/{limit}m is below the gate's {GATE_MIN_LIMIT_MINUTES}-minute "
        "minimum; raise BASE_MINUTES or LIMIT_MULTIPLE"
    )
    return expected, limit


def describe(base, cwd=None):
    """Lines of report for a reviewer dispatch, or a reason it cannot be computed."""
    lines = changed_lines(base, cwd=cwd)
    if lines is None:
        return [f"cannot compute a budget: {base}...HEAD does not resolve"]
    expected, limit = budget(lines)
    report = [
        f"diff scope: {lines} changed lines against {base}",
        f"expected duration: {expected} minutes",
        f"hard limit: {limit} minutes",
    ]
    if needs_splitting(lines):
        report.append(
            f"this diff is {lines} lines; one review covers about "
            f"{max_reviewable_lines()} inside the {MACHINE_CEILING_MINUTES}-minute "
            "machine ceiling. Split it into that many reviews, or scope this one "
            "to the part that carries the risk."
        )
    return report


def dispatch_block(base, cwd=None):
    """The paste-able dispatch text, budget plus the gate's own requirements."""
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
        expected, limit = budget(lines)
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


def main_cli(argv):
    """`python -m corral.budget [--base <ref>]`: print the dispatch block.

    Mirrors the `corral budget` verb's flag so the two spellings cannot
    disagree about how the base ref is given.
    """
    base = os.environ.get("BASE_REF", "origin/master")
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--base" and rest:
            base = rest.pop(0)
        elif arg.startswith("--base="):
            base = arg.split("=", 1)[1]
    print(dispatch_block(base))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main_cli(sys.argv[1:]))
