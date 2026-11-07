"""The derived board Status and the manual-move rule. Pure: no I/O, no
``gh``, no network. The pass keeps the per-card inputs and the write
itself at its own seams; this module only decides."""

from typing import NamedTuple

#: The derived status keys, in derivation order (closed first).
AVAILABLE = "available"
CLAIMED = "claimed"
REVIEW = "review"
DONE = "done"

#: The derived states in sync order, naming the ``BoardConfig`` option
#: id attribute behind each: the one map from a state to its setting.
#: Both the status step and the doctor resolve ids through this, so a
#: state is never silently dropped by one path and not the other.
STATUS_ID_ATTRS = (
    (AVAILABLE, "status_available"),
    (CLAIMED, "status_claimed"),
    (REVIEW, "status_review"),
    (DONE, "status_done"),
)


def status_ids(board_config) -> dict:
    """The mapped Status option ids by derived state, unmapped states left out."""
    return {
        state: option_id
        for state, attr in STATUS_ID_ATTRS
        if (option_id := getattr(board_config, attr))
    }


def option_names(id_to_name) -> dict:
    """The display name to option id map for one Status field. Pure.

    A name two options share resolves to neither: the step reads the
    card as unknown rather than picking a side, the same way the
    doctor refuses the ambiguity instead of resolving to one pick.
    The one place that decides, shared by the status step, the sweep
    and the doctor.
    """
    by_name = {}
    ambiguous = set()
    for option_id, name in dict(id_to_name).items():
        if name in by_name:
            ambiguous.add(name)
        by_name[name] = option_id
    for name in ambiguous:
        del by_name[name]
    return by_name


def derive(state, has_open_pull, claimed):
    """The derived status key for one card: closed wins over everything,
    then an open linked pull request, then a live claim, else available.
    """
    if state == "closed":
        return DONE
    if has_open_pull:
        return REVIEW
    if claimed:
        return CLAIMED
    return AVAILABLE


class StatusRecord(NamedTuple):
    """The Status korapy last set on one card: its option id and the
    derived state behind the write. A card whose current Status differs
    from the recorded option id was moved by a person."""

    option_id: str
    derived: str


def decide(current, record, derived, options):
    """``("write", option_id)``, ``("record", StatusRecord)`` or
    ``("leave", None)`` for one card.

    ``current`` is the card's live Status option id (``None`` when the
    board names no option the schema can resolve), ``record`` the stored
    ``StatusRecord`` or ``None``, ``derived`` the derived key, and
    ``options`` the configured key-to-option-id map. A state the map
    does not name syncs nothing; a card a person moved (current
    differs from the record) rests until the derived state changes,
    then writes again; a card already at the derived value with no
    record adopts one so a later move is seen.
    """
    want = options.get(derived, "")
    if not want:
        return ("leave", None)
    if record is not None and current != record.option_id:
        if record.derived == derived:
            return ("leave", None)
        return ("write", want)
    if current == want:
        if record is not None and record.option_id == want and record.derived == derived:
            return ("leave", None)
        return ("record", StatusRecord(option_id=want, derived=derived))
    return ("write", want)
