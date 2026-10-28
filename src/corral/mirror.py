"""The mirror pass: drain the board store's outbox into the tracker.

Reads and sync fill the store; this is the write side. Each pass takes
the queued outbox rows oldest first, groups them by item, keeps only the
last value per field, and pushes what is left through the tracker. A
write stays queued until it is sent, dropped with a reason, or
superseded: nothing is lost.
"""

from collections.abc import Mapping, Sequence
from typing import NamedTuple

from corral import board, tracker as iface


class MirrorResult(NamedTuple):
    sent: int  # writes the tracker accepted
    dropped: int  # writes discarded with a reason (superseded, external, closed)
    dirty: int  # writes whose push failed and stay queued for the next pass
    stopped: float | None  # retry_after seconds when a rate limit stopped the pass
    errors: tuple[str, ...]  # one line per failed write; "" never appears


def _ops_for(operators, repo: str) -> Sequence[str]:
    """The operator logins for ``repo``: the per-repo mapping entry, or the
    shared list. An empty set reads as the repo owner, never as nobody."""
    if isinstance(operators, Mapping):
        names = operators.get(repo, ())
    else:
        names = operators or ()
    kept = [name for name in names if isinstance(name, str) and name]
    return kept or [repo.split("/")[0]]


def push(
    store: board.Store,
    tracker: iface.Tracker,
    *,
    operators,
    now: float,
    limit: int | None = None,
) -> MirrorResult:
    """Push the queued outbox writes into the tracker.

    ``operators`` maps tracker repos to their operator logins (or is one
    shared list): a write to an item that is external under them is
    dropped, in defence of the store's own refusal. ``limit`` caps how
    many queued rows the pass takes; the rest wait for the next pass.
    """
    sent = dropped = dirty = 0
    errors: list[str] = []
    stopped: float | None = None
    halted = False

    rows = store.queued_writes(limit)
    by_item: dict[tuple[str, int], list] = {}
    for row in rows:
        by_item.setdefault((row.repo, row.number), []).append(row)

    for (repo, number), item_rows in by_item.items():
        if halted:
            break
        last: dict[str, int] = {}
        for row in item_rows:
            last[row.field] = row.id
        kept = [row for row in item_rows if last[row.field] == row.id]
        for row in item_rows:
            if last[row.field] != row.id:
                store.mark_dropped(row.id, "superseded")
                dropped += 1
        if not kept:
            continue
        found, _ = store.item(repo, number, now=now)
        if found is None:
            for row in kept:
                store.mark_dirty(row.id, f"unknown item {repo}#{number}")
                dirty += 1
                errors.append(f"{repo}#{number}: unknown item")
            continue
        if found.external or board.is_external(found.author, _ops_for(operators, repo), False):
            for row in kept:
                store.mark_dropped(row.id, "external")
                dropped += 1
            continue
        if found.state != "open":
            for row in kept:
                store.mark_dropped(row.id, "closed")
                dropped += 1
            continue
        for row in kept:
            try:
                tracker.write(repo, number, row.field, row.value)
            except iface.TrackerRateLimited as err:
                store.mark_dirty(row.id, str(err))
                dirty += 1
                errors.append(f"{repo}#{number}: {err}")
                stopped = err.retry_after
                halted = True
                break
            except iface.TrackerError as err:
                store.mark_dirty(row.id, str(err))
                dirty += 1
                errors.append(f"{repo}#{number}: {err}")
                break
            else:
                store.mark_sent(row.id)
                sent += 1

    return MirrorResult(sent, dropped, dirty, stopped, tuple(errors))
