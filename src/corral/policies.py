"""Ready-made eligibility policies: startable tickets from one entry line.

A project opts in from its registration file instead of writing policy
code::

    eligibility = "corral.policies:basic"

The policy follows the eligibility contract in `docs/04-dispatch-loop.md`
section 2: it is pure, reading only the rows and `now` it is given, and
returns one `StartRequest` of kind `start` per startable ticket. Weight
carries the board priority rank so the drive loop orders heaviest first.
"""

import re
from collections.abc import Sequence

from corral import eligibility, tracker

#: Labels that hold a ticket for a human. A row carrying any of them is
#: never selected. Module constants on purpose: one place, and no
#: configuration surface in version one.
HOLD_LABELS = ("hold", "needs-decision", "needs-triage")

#: Standalone title word that holds a ticket for a human. The match is a
#: whole word and case sensitive: a title such as "cutover (HOLD for
#: approval)" is held, while "holding pattern" is not.
HOLD_TITLE_WORD = "HOLD"

_HOLD_TITLE_RE = re.compile(r"\b" + re.escape(HOLD_TITLE_WORD) + r"\b")


def _weight(priority: str) -> int:
    """The priority rank as the request weight, highest first. Pure.

    Ranks over tracker.PRIORITIES, the shared highest-first vocabulary;
    anything outside it ranks zero and sorts last, the same rank the
    drive loop computes for unknown priorities.
    """
    if priority in tracker.PRIORITIES:
        return len(tracker.PRIORITIES) - tracker.PRIORITIES.index(priority)
    return 0


def _selectable(row: eligibility.TicketRow) -> bool:
    """Whether basic returns a start request for this row. Pure.

    Open, unclaimed, unheld, unblocked, with no open linked pull: a
    missing or non-string title reads as empty and a non-tuple labels
    value reads as none, so odd data selects or skips without raising.
    """
    if row.state != "open":
        return False
    if row.external:
        return False
    labels = row.labels if isinstance(row.labels, tuple) else ()
    if any(label in HOLD_LABELS for label in labels):
        return False
    title = row.title if isinstance(row.title, str) else ""
    if _HOLD_TITLE_RE.search(title):
        return False
    if row.agent:
        return False
    extra = row.extra if isinstance(row.extra, dict) else {}
    if extra.get("blocked_by"):
        return False
    if "blocked" in labels:
        return False
    pulls = row.pulls if isinstance(row.pulls, (tuple, list)) else ()
    return all(getattr(pull, "state", "") != "open" for pull in pulls)


def basic(rows: Sequence[eligibility.TicketRow], *, now: float) -> list[eligibility.StartRequest]:
    """One start request per startable ticket, in row order. Pure.

    `now` is accepted for the eligibility contract and unused: whether a
    ticket is startable depends only on the rows. Equal weights keep row
    order; the drive loop breaks those ties by (repo, number).
    """
    return [
        eligibility.StartRequest(
            repo=row.repo,
            number=row.number,
            kind="start",
            weight=_weight(row.priority),
        )
        for row in rows
        if _selectable(row)
    ]
