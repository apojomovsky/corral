"""The tracker interface: tickets, labels and claims, without a provider in it.

Only the adapters (today: GitHub through ``gh``) know provider URLs or
API shapes. Everything else takes a ``Tracker`` object. Nothing here
imports ``gh`` or knows a provider's URL layout.
"""

import datetime
from typing import Iterator, NamedTuple, Protocol


class TrackerError(Exception):
    """The tracker could not be reached or answered something unusable.

    A missing item is a ``TrackerError`` too, never ``None``: callers
    fail closed. Exit code 3 through the CLI: a remote or service
    unreachable.
    """

    exit_code = 3


class TrackerRateLimited(TrackerError):
    """The tracker answered with a rate limit: stop the pass, retry later.

    A subclass of ``TrackerError`` so callers that do not pace still fail
    closed with exit code 3. ``retry_after`` is the seconds the answer
    asked to wait before the next write.
    """

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


#: Priority names, highest first. The shared vocabulary every adapter
#: derives the plain priority string from, and the order the dispatch
#: pass ranks by. A second adapter reuses this order; it never forks it.
PRIORITIES = ("urgent", "high", "normal", "low", "backlog")

#: The label prefix that carries an issue's rank. The adapter derives the
#: plain priority strings from it; the bootstrap set below derives the
#: priority label names from it. One fact, one place.
PRIORITY_PREFIX = "priority:"


class BootstrapLabel(NamedTuple):
    """One label `github setup` bootstraps: its name plus its description.

    Colors stay in the adapters (a provider detail); the description
    rides along so a fresh label reads the same everywhere.
    """

    name: str
    description: str


#: The only people-owned label corral ever adds. The decision protocol
#: holds a ticket for a human with this label; no adapter exposes a
#: general label write, so any other name is refused.
NEEDS_DECISION = "needs-decision"

#: The fixed board label set `github setup` bootstraps: exactly the
#: labels the rest of corral reads, no more. `ready` is what
#: `board.ready` defaults to; `needs-triage` and `needs-decision` are
#: the triage and decision flow; `hold` blocks `resume`; the
#: `priority:*` names follow `PRIORITIES` in rank order.
BOOTSTRAP_LABELS = (
    BootstrapLabel("ready", "marks an issue an agent may take"),
    BootstrapLabel("needs-triage", "waiting for triage to a repository"),
    BootstrapLabel(NEEDS_DECISION, "waiting on a human decision"),
    BootstrapLabel("hold", "held for a human; resume refuses while present"),
    BootstrapLabel(f"{PRIORITY_PREFIX}urgent", "handled first"),
    BootstrapLabel(f"{PRIORITY_PREFIX}high", "a correctness bug, a broken gate, or a flaky test"),
    BootstrapLabel(f"{PRIORITY_PREFIX}normal", "a feature"),
    BootstrapLabel(f"{PRIORITY_PREFIX}low", "cleanup or docs"),
    BootstrapLabel(f"{PRIORITY_PREFIX}backlog", "someday, when nothing else waits"),
)


class LabelReport(NamedTuple):
    """The idempotent ensure outcome for one repository.

    `created` holds the labels the call made, `kept` the ones already
    present and left byte-identical, `refused` the ones the account may
    not create. A failed call raises `TrackerError` instead.
    """

    created: tuple[str, ...]
    kept: tuple[str, ...]
    refused: tuple[str, ...]


class UnknownOption(TrackerError):
    """A corral-owned write names a concept the board config never mapped.

    A subclass of ``TrackerError`` so callers that do not handle it still
    fail closed with exit code 3. The mirror drops the write with reason
    ``unknown-option`` instead of retrying: corral never invents a mapping.
    """


class TrackerItem(NamedTuple):
    repo: str  # "owner/name" as the tracker names it
    number: int
    kind: str  # "issue" | "pr"
    title: str
    body: str
    state: str  # "open" | "closed"
    author: str
    labels: tuple[str, ...]
    priority: str  # derived from the priority:* label, else ""
    assignees: tuple[str, ...]
    url: str
    updated_at: float  # epoch seconds
    from_fork: bool
    agent: str = ""  # derived from the agent:* label, else ""
    status: str = ""  # derived from the status:* label, else ""
    extra: dict | None = None  # board concepts; normalized to {} on read


class Tracker(Protocol):
    """Tickets and their change feed, provider-neutral."""

    def item(self, repo: str, number: int) -> TrackerItem:
        """One item, fetched fresh. Raises ``TrackerError`` when the
        tracker is unreachable, the answer is unusable, or the item is
        missing."""
        ...

    def changes(self, repo: str, cursor: str | None) -> tuple[list[TrackerItem], str]:
        """Items updated since ``cursor``, and the next cursor.

        ``None`` reads from the beginning of the feed window. The next
        cursor never moves backwards, and an item updated during the
        call is never skipped.
        """
        ...

    def items(self, repo: str) -> Iterator[TrackerItem]:
        """Every item, for a full resync."""
        ...

    def write(self, repo: str, number: int, field: str, value: str) -> None:
        """Set one corral-owned field (``agent`` or ``status``) on an item.

        ``value == ""`` clears the field. Anything outside the corral-owned
        fields is refused with ``ValueError``. A failed call raises
        ``TrackerError``; a rate-limit answer raises ``TrackerRateLimited``
        with the seconds to wait. A board-backed tracker raises
        ``UnknownOption`` for a concept the board config never mapped (the
        mirror drops it), and a plain ``TrackerError`` when the mapped
        field is absent from the live board (the rows stay queued).
        Writing the same value twice leaves one label behind: the write is
        idempotent.
        """
        ...

    def comment(self, repo: str, number: int, body: str) -> None:
        """Post one comment on an item, for the decision protocol only.

        The loop calls this with its own question when a ticket needs a
        human; nothing else posts comments. A failed call raises
        ``TrackerError``; a rate-limit answer raises ``TrackerRateLimited``
        with the seconds to wait.
        """
        ...

    def add_label(self, repo: str, number: int, label: str) -> None:
        """Add one decision label to an item, for the decision protocol only.

        The loop calls this with the single hold label; any other name is
        refused with ``ValueError``. A failed call raises ``TrackerError``;
        a rate-limit answer raises ``TrackerRateLimited`` with the seconds
        to wait.
        """
        ...

    def ensure_labels(self, repo: str) -> LabelReport:
        """Create the missing bootstrap labels on ``repo``, leave the rest alone.

        Idempotent: missing labels are created, existing labels are left
        byte-identical (never overwritten with an edited color or
        description), and labels the account may not create are reported
        as refused. A failed call raises ``TrackerError``; a rate-limit
        answer raises ``TrackerRateLimited`` with the seconds to wait.
        """
        ...


#: Cursor wire shape: the UTC second an item was last updated. ISO by
#: the clock, lexicographic by accident; compare as epochs, never as text.
_CURSOR_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_cursor(cursor: str) -> float:
    """Epoch seconds for an opaque cursor from ``changes``. A cursor from
    anywhere else is a ``TrackerError``, never a guess."""
    try:
        moment = datetime.datetime.strptime(cursor, _CURSOR_FORMAT)
    except (TypeError, ValueError):
        raise TrackerError(f"unusable tracker cursor {cursor!r}") from None
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp()


def format_cursor(moment: float) -> str:
    """The opaque cursor for one ``updated_at`` epoch."""
    return datetime.datetime.fromtimestamp(moment, tz=datetime.timezone.utc).strftime(
        _CURSOR_FORMAT
    )


def render_ensure(repo: str, report: LabelReport) -> str:
    """The ensure report as ``setup`` and ``new`` output lines. Pure.

    One line per created label, one count for the labels left alone,
    and one fix line per refused label (the owner who can create it,
    or moving the board to the ops side).
    """
    lines = []
    for name in report.created:
        lines.append(f"created label {name} on {repo}\n")
    if report.kept:
        lines.append(f"kept {len(report.kept)} labels on {repo}\n")
    for name in report.refused:
        lines.append(
            f"refused label {name} on {repo}: ask an owner to create it,"
            " or move the board to the ops side\n"
        )
    return "".join(lines)
