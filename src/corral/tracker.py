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
        with the seconds to wait. Writing the same value twice leaves one
        label behind: the write is idempotent.
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
