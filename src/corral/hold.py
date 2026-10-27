"""The hold predicate every resume path checks before reviving an agent.

A resume is the most disruptive touch there is (a revived agent runs
builds and tests on the host, the very reason a human holds a ticket),
so every path that can revive an agent asks this module first.

Two readings, one rule:

- ``labels_held(labels, hold_label)`` for a label list already in hand:
  pure.
- ``reason(read_labels, ref, linked, hold_label=...)`` for the live
  re-check at apply time: the ticket's labels, then each linked PR's.
  It fails closed: a label read that raises, or returns anything other
  than a list of strings, is a hold ("unreadable"), never a pass.
  A rate-limited or flaky read therefore pauses the resume until the
  labels read again, instead of reviving a ticket whose hold it could
  not see.

Label reads stay behind the injected ``read_labels(kind, repo, number)``
seam, so this module never calls out to a tracker or forge itself.
"""

from corral import settings

#: Label that marks a ticket as held for a human.
HOLD_LABEL = "hold"

settings.register("board.hold_label", str, HOLD_LABEL, "label that holds a ticket for a human")


def labels_held(labels, hold_label=HOLD_LABEL):
    """Whether a label collection carries the hold. Pure."""
    return any(name == hold_label for name in labels or ())


def reason(read_labels, ref, linked=(), *, hold_label=HOLD_LABEL):
    """Why ``ref`` must not be resumed, or None when it is free to resume.

    ``read_labels(kind, repo, number)`` returns a list of label names
    (``kind`` is ``"issue"`` for the ticket, ``"pr"`` for linked
    pulls) and may raise. ``ref`` is a ``(repo, number)`` pair;
    ``linked`` holds further ``(repo, number)`` pairs for the pull
    requests a resume acts on, deduplicated. The text names the ticket
    and the label, so a skip entry reads the same on every path.
    """
    try:
        ticket_repo, ticket_number = ref
    except (TypeError, ValueError):
        return (
            f"{ref!r}: {hold_label} unreadable "
            "(unparseable ticket ref): no resume "
            "until the labels read again"
        )
    ticket = f"{ticket_repo}#{ticket_number}"
    targets = [(ticket_repo, ticket_number, "issue", True)]
    seen = {(ticket_repo, ticket_number)}
    for pair in linked or ():
        try:
            repo, number = pair
        except (TypeError, ValueError):
            return (
                f"{ticket}: {hold_label} unreadable "
                f"(unparseable linked ref {pair!r}): no resume "
                "until the labels read again"
            )
        if (repo, number) in seen:
            continue
        seen.add((repo, number))
        targets.append((repo, number, "pr", False))
    for repo, number, kind, is_ticket in targets:
        try:
            labels = read_labels(kind, repo, number)
        except Exception as err:
            return (
                f"{ticket}: {hold_label} unreadable on {repo}#{number} "
                f"({err}): no resume until the labels read again"
            )
        if not isinstance(labels, list) or any(not isinstance(name, str) for name in labels):
            return (
                f"{ticket}: {hold_label} unreadable on {repo}#{number} "
                f"(labels returned {labels!r}): no resume "
                "until the labels read again"
            )
        if labels_held(labels, hold_label):
            where = "" if is_ticket else f" on {repo}#{number}"
            return f"{ticket} carries {hold_label}{where}: held for a human, no resume"
    return None
