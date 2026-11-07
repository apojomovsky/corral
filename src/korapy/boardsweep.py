"""Board drift: the cards out of step with their issues, found in one place.

The board step (``sync.sync_board_repo``) records drift verdicts every
poll through ``reconcile``; ``korapy board sweep`` lists and fixes them
on demand; the doctor and the dashboard read the recorded flags. This
module holds the pure core every writer shares: normalizing poll and
store cards, deciding which ones drifted, and rendering the moves.
Store flag reads and writes ride on the passed store; every tracker
call stays with the caller, so tests inject fakes at the reader seam.
"""

from typing import NamedTuple

from korapy import boardstatus, quotacli


class Card(NamedTuple):
    """One card's drift inputs: where it is, what it shows, when it changed."""

    repo: str
    number: int
    state: str  # "open" | "closed"
    status: str  # the live Status display name, "" when unset
    updated_at: float  # the card's last change, the drift's earliest start


class Move(NamedTuple):
    """One card out of step: the issue state and the move that fixes it."""

    repo: str
    number: int
    state: str
    current: str  # the live Status display name, "" when unset or missing
    derived: str  # the derived key for a set-move, "" for an add-move
    kind: str  # "add" (onto the board) | "set" (Status to ``want``)
    want: str  # the Status display name to set, "" for an add-move
    age_s: float  # seconds out of step, from the card's last change


def poll_cards(found) -> list:
    """Poll readings as cards: issues only, oldest first.

    Pull-request rows are never tickets, like everywhere else. A
    missing or foreign board status reads as unset, never as a guess.
    """
    cards = []
    for item in found:
        if getattr(item, "kind", "issue") != "issue":
            continue
        extra = getattr(item, "extra", None)
        status = extra.get("board_status") if isinstance(extra, dict) else ""
        cards.append(
            Card(
                item.repo,
                item.number,
                item.state,
                status if isinstance(status, str) else "",
                item.updated_at,
            )
        )
    return sorted(cards, key=lambda card: (card.repo, card.number))


def store_cards(rows) -> list:
    """Stored open rows as cards, for the missing-from-board check."""
    cards = []
    for row in rows:
        extra = row.extra if isinstance(row.extra, dict) else {}
        status = extra.get("board_status", "")
        cards.append(
            Card(
                row.repo,
                row.number,
                row.state,
                status if isinstance(status, str) else "",
                row.tracker_updated_at,
            )
        )
    return sorted(cards, key=lambda card: (card.repo, card.number))


def _want_name(names, option_id, derived):
    """The display name behind one written option id, deterministically.

    Two names behind one id resolve to the sorted first; an id the
    live board no longer offers falls back to the derived key, so the
    plan still names the move when the mapping itself is broken.
    """
    reverse: dict = {}
    for name in sorted(names):
        reverse.setdefault(names[name], name)
    return reverse.get(option_id, derived)


def status_moves(cards, *, names, ids, claims, records, pulls, pulls_error="", now) -> tuple:
    """``(moves, notes)`` for the cards on the board. Pure.

    A card whose ``decide`` says write is drifted; a card a person
    moved (live differs from the record with the derived state
    unchanged) rests, as does a card already at its derived value.
    Open cards rest with one note each when the pulls listing is
    unknown, the way the pass rests them; closed cards derive Done
    with no pulls. ``names`` maps live display names to option ids,
    ``ids`` derived keys to option ids, ``claims`` holds claimed
    ``(repo, number)`` keys, ``records`` the last-set records.
    """
    moves: list = []
    notes: list = []
    for card in sorted(cards, key=lambda item: (item.repo, item.number)):
        key = (card.repo, card.number)
        if card.state == "closed":
            has_open_pull = False
        elif pulls is None:
            notes.append(f"{card.repo}#{card.number}: {pulls_error}")
            continue
        else:
            has_open_pull = card.number in pulls
        claimed = key in claims
        derived = boardstatus.derive(card.state, has_open_pull, claimed)
        current = names.get(card.status) if card.status else ""
        action, payload = boardstatus.decide(current, records.get(key), derived, ids)
        if action != "write":
            continue
        moves.append(
            Move(
                card.repo,
                card.number,
                card.state,
                card.status,
                derived,
                "set",
                _want_name(names, payload, derived),
                max(0.0, now - card.updated_at),
            )
        )
    return tuple(moves), tuple(notes)


def missing_moves(cards, *, present, now) -> tuple:
    """The open stored cards missing from the board. Pure.

    ``cards`` are normalized open store rows; ``present`` holds the
    ``(repo, number)`` pairs the poll saw. A missing card's Status is
    unknown until it is adopted, so the move names no derived state.
    """
    moves = []
    for card in sorted(cards, key=lambda item: (item.repo, item.number)):
        if (card.repo, card.number) in present:
            continue
        moves.append(
            Move(
                card.repo,
                card.number,
                card.state,
                card.status,
                "",
                "add",
                "",
                max(0.0, now - card.updated_at),
            )
        )
    return tuple(moves)


def reconcile(
    store,
    repo,
    *,
    cards,
    open_cards,
    present,
    names,
    ids,
    pulls,
    pulls_error="",
    now,
    complete,
    status_active,
    adopt_active,
):
    """Record this poll's drift verdicts, returning ``(moves, notes)``.

    The same function the pass and the sweep share: status drift over
    the polled cards (flagged while still out of step, cleared once a
    card rests at its derived value), plus missing open rows on a
    complete poll only, when a partial view proves nothing missing. A
    complete poll also clears verdicts for rows that are neither on
    the board nor open in the store (history or gone), and for present
    cards when the status step is inactive (an unmapped project cannot
    drift). A failed poll never reaches here, so nothing is recorded
    on partial knowledge. The first sighting wins: a card that stays
    drifted keeps its original ``since``, seeded from the card's last
    change, so the recorded age agrees with the plan's drift age.
    """
    claims = set()
    records = {}
    for card in cards:
        if store.claim_of(card.repo, card.number) is not None:
            claims.add((card.repo, card.number))
        records[(card.repo, card.number)] = store.status_set_of(card.repo, card.number)
    moves: list = []
    notes: list = []
    if status_active:
        found, notes = status_moves(
            cards,
            names=names,
            ids=ids,
            claims=claims,
            records=records,
            pulls=pulls,
            pulls_error=pulls_error,
            now=now,
        )
        moves.extend(found)
        flagged = {(move.repo, move.number) for move in found}
        for card in cards:
            if (card.repo, card.number) in flagged:
                store.note_drift(card.repo, card.number, now=min(now, card.updated_at))
            else:
                store.clear_drift(card.repo, card.number)
    elif complete:
        for card in cards:
            store.clear_drift(card.repo, card.number)
    if adopt_active and complete:
        for move in missing_moves(open_cards, present=present, now=now):
            moves.append(move)
            store.note_drift(move.repo, move.number, now=now - move.age_s)
    if complete:
        seen = {(card.repo, card.number) for card in cards}
        still_open = {(card.repo, card.number) for card in open_cards}
        for flagged_repo, flagged_number, _since in store.drift_flags():
            if flagged_repo != repo:
                continue
            key = (flagged_repo, flagged_number)
            if key in seen or key in still_open:
                continue
            store.clear_drift(flagged_repo, flagged_number)
    moves.sort(key=lambda move: (move.repo, move.number))
    return tuple(moves), tuple(notes)


def format_move(move) -> str:
    """One plan line: the card, the issue state and the move. Pure."""
    age = quotacli.format_age(move.age_s)
    if move.kind == "add":
        return f"{move.repo}#{move.number}: state {move.state}, add to board (drift {age})"
    return (
        f"{move.repo}#{move.number}: state {move.state}, set Status to {move.want!r} (drift {age})"
    )


def board_line(added: int, written: int, remaining: int) -> str | None:
    """One pass line when the board step changed or found drift, else None.

    The dispatch pass prints the line, so the journal shows the board
    being kept; a quiet pass prints nothing. Pure.
    """
    parts = []
    if added:
        parts.append(f"{added} added")
    if written:
        parts.append(f"{written} status set")
    if remaining:
        parts.append(f"drift remaining: {remaining}")
    if not parts:
        return None
    return "board kept: " + ", ".join(parts)
