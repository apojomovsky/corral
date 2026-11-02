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

    def missing_labels(self, repo: str) -> tuple[str, ...]:
        """Names of the bootstrap labels absent on ``repo``, in bootstrap order.

        Read-only: one label listing, never a create, so `doctor` can
        check the board labels without changing them. A failed call
        raises ``TrackerError``; a rate-limit answer raises
        ``TrackerRateLimited`` with the seconds to wait.
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


#: The board concept per ``BoardConfig`` attribute, in poll order. The
#: one mapping from a concept key to the config attribute holding its
#: board field name; the GitHub reader resolves live names through
#: this, and ``effective_spec`` resolves configured names through
#: this. A concept added here is never silently dropped anywhere.
BOARD_CONCEPT_ATTRS = (
    ("agent", "agent_field"),
    ("status", "status_field"),
    ("board_status", "status_column"),
    ("priority", "priority_column"),
    ("area", "area_column"),
    ("pool", "pool_column"),
    ("blocked_by", "blocked_by_column"),
)

#: The concepts corral provisions, in standard order.
BOARD_CONCEPTS = tuple(concept for concept, _ in BOARD_CONCEPT_ATTRS)

#: Concepts carried as free text; the rest are single-select columns.
BOARD_TEXT_CONCEPTS = frozenset({"agent", "status", "blocked_by"})


class FieldSpec(NamedTuple):
    """One board field the provisioner ensures, as plain data.

    ``concept`` is the corral concept key, ``name`` its board spelling,
    ``kind`` is ``"text"`` or ``"single_select"``, and ``options`` are
    the option names a created single-select field starts with (empty
    for text). ``keep_only`` marks a mapped Area or Pool: it must
    exist with the right kind and is then kept exactly as found, never
    created or topped up.
    """

    concept: str
    name: str
    kind: str
    options: tuple[str, ...]
    keep_only: bool = False


class BoardSpec(NamedTuple):
    """What the provisioner ensures, as plain data.

    The address is exactly one of ``number`` and ``project_id``, or
    neither in the bootstrap states where no table exists yet; ``title``
    is then the only way to find the board. ``repo`` is the
    ``"owner/name"`` to link, or ``""`` for no link. ``priority_map``
    is the config's existing option-id to rank map, copied, or ``None``
    when the config holds no map.
    """

    owner: str
    title: str
    repo: str
    fields: tuple[FieldSpec, ...]
    private: bool = True
    number: int | None = None
    project_id: str | None = None
    priority_map: dict[str, str] | None = None


class BoardProvision(NamedTuple):
    """The ensure outcome for one board: what ran and what refused.

    ``fields`` carries the live per-concept names after the final
    read-back, so the config writer never re-derives names from
    defaults. ``created`` names what the run made (``"project"``,
    ``"field:Agent"``, ``"option:Priority/High"``,
    ``"link:owner/name"``), ``kept`` what it left alone, ``refused``
    one fix line per refusal. ``priority_map`` is the live option id
    to rank map for the config file.
    """

    owner: str
    number: int | None
    project_id: str
    fields: tuple[FieldSpec, ...]
    created: tuple[str, ...]
    kept: tuple[str, ...]
    refused: tuple[str, ...]
    priority_map: dict[str, str]


class BoardPopulate(NamedTuple):
    """The adopt-issues outcome for one board: added, kept, refused.

    Adoption adds open issues only and writes no field value; each
    entry of ``refused`` is a fix line for one issue the run could not
    add.
    """

    added: tuple[str, ...]
    kept: tuple[str, ...]
    refused: tuple[str, ...]


class BoardProvisioner(Protocol):
    """One board's idempotent ensure and populate, provider-neutral."""

    def ensure_board(self, spec: BoardSpec) -> BoardProvision:
        """Create what the spec names and is missing, keep the rest.

        Never alters or deletes what exists. A spec that cannot run as
        given raises ``ValueError``; an unreachable provider raises
        ``TrackerError`` and a rate-limit answer raises
        ``TrackerRateLimited``.
        """
        ...

    def populate(self, project_id: str, repo: str) -> BoardPopulate:
        """Add the tracker repo's open issues missing from the board.

        Item-add only: no field value is ever written. Same errors as
        ``ensure_board``.
        """
        ...


#: The standard board as data: the seven concepts with their default
#: board spellings, kinds and creation options. The Priority options
#: are the five ranks title-cased, highest first; Status carries its
#: creation defaults; Area and Pool carry no options, so
#: ``effective_spec`` leaves them out until options are declared.
STANDARD_BOARD = (
    FieldSpec(concept="agent", name="Agent", kind="text", options=()),
    FieldSpec(concept="status", name="Agent state", kind="text", options=()),
    FieldSpec(
        concept="board_status",
        name="Status",
        kind="single_select",
        options=("Todo", "In Progress", "Done"),
    ),
    FieldSpec(
        concept="priority",
        name="Priority",
        kind="single_select",
        options=("Urgent", "High", "Normal", "Low", "Backlog"),
    ),
    FieldSpec(concept="area", name="Area", kind="single_select", options=()),
    FieldSpec(concept="pool", name="Pool", kind="single_select", options=()),
    FieldSpec(concept="blocked_by", name="Blocked by", kind="text", options=()),
)

#: The standard option name per rank, highest first. Derived from the
#: standard Priority field above, so the rank vocabulary stays in one
#: place.
_RANK_OPTION = dict(
    zip(PRIORITIES, next(f.options for f in STANDARD_BOARD if f.concept == "priority"))
)


def _spec_error(text: str) -> ValueError:
    """A spec refused before any lookup, naming the conflict."""
    return ValueError(text)


def _split_spec_repo(repo: str) -> tuple[str, str]:
    """``(owner, name)`` for a tracker repo, else ``ValueError``."""
    parts = repo.split("/") if isinstance(repo, str) else []
    if len(parts) != 2 or not all(parts):
        raise _spec_error(f"bad board repo {repo!r}: want 'owner/name'")
    return parts[0], parts[1]


def _checked_options(flag: str, values) -> tuple[str, ...]:
    """Declared ``--area``/``--pool`` names deduplicated, else ``ValueError``."""
    seen: tuple[str, ...] = ()
    for value in values or ():
        if not isinstance(value, str) or not value:
            raise _spec_error(f"{flag} names must be non-empty strings, got {value!r}")
        if value not in seen:
            seen += (value,)
    return seen


def effective_spec(
    *,
    variant,
    board,
    repo: str,
    title: str | None = None,
    owner: str | None = None,
    areas=(),
    pools=(),
) -> BoardSpec:
    """The provisioner spec for one repo entry: config names else standard.

    ``board`` is the entry's resolved ``BoardConfig`` (or ``None`` when
    no table exists yet), ``repo`` its tracker repo, and the remaining
    flags are the operator's ``--board-title``, ``--board-owner``,
    ``--area`` and ``--pool`` values. ``variant`` travels with the
    entry for the setup wiring; derivation never branches on it, the
    caller decides which entries are provisioned. A spec that cannot
    run (both addresses, a non-positive number, an empty flag value,
    a bad repo) raises ``ValueError`` before any lookup.
    """
    if not isinstance(variant, str) or not variant:
        raise _spec_error(f"bad tracker variant {variant!r}")
    repo_owner, repo_name = _split_spec_repo(repo)
    number = board.project_number if board is not None else None
    project_id = board.project_id if board is not None else None
    if number is not None and project_id is not None:
        raise _spec_error("board spec carries both a project number and a project id")
    if number is not None and (isinstance(number, bool) or number < 1):
        raise _spec_error(f"board spec project number must be positive, got {number!r}")
    if owner is None:
        eff_owner = board.project_owner if board is not None else repo_owner
    elif isinstance(owner, str) and owner:
        eff_owner = owner
    else:
        raise _spec_error(f"board owner flag must be a non-empty string, got {owner!r}")
    if title is None:
        eff_title = f"{repo_name} board"
    elif isinstance(title, str) and title:
        eff_title = title
    else:
        raise _spec_error(f"board title flag must be a non-empty string, got {title!r}")
    declared = {
        "area": _checked_options("--area", areas),
        "pool": _checked_options("--pool", pools),
    }
    standard = {field.concept: field for field in STANDARD_BOARD}
    fields = []
    for concept, attr in BOARD_CONCEPT_ATTRS:
        mapped = getattr(board, attr) if board is not None else ""
        if concept in declared:
            options = declared[concept]
            if options:
                fields.append(
                    FieldSpec(concept, mapped or standard[concept].name, "single_select", options)
                )
            elif mapped:
                fields.append(FieldSpec(concept, mapped, "single_select", (), keep_only=True))
        else:
            fields.append(
                FieldSpec(
                    concept,
                    mapped or standard[concept].name,
                    standard[concept].kind,
                    standard[concept].options,
                )
            )
    return BoardSpec(
        owner=eff_owner,
        title=eff_title,
        repo=repo,
        fields=tuple(fields),
        number=number,
        project_id=project_id,
        priority_map=dict(board.priority_map) if board is not None else {},
    )


class LiveOption(NamedTuple):
    """One live single-select option: stable id, display name and extras.

    The planner matches ranks by id first and name second, and resends
    ``color`` and ``description`` unchanged when it appends an option,
    so an append never resets people's option styling.
    """

    id: str
    name: str
    color: str = ""
    description: str = ""


class LiveField(NamedTuple):
    """One live board field as plain data: name, kind and options."""

    name: str
    kind: str  # "text" | "single_select" | whatever the provider reports
    options: tuple[LiveOption, ...] = ()


class FieldPlan(NamedTuple):
    """What the adapter must do about one spec field, in spec order.

    ``decision`` is ``"create"``, ``"keep"`` or ``"refuse"``. For a
    create, ``options`` are the option names to create with; for a kept
    Priority field missing ranks, ``options`` are the standard names to
    append while ``resend`` carries every existing option unchanged for
    the whole-list update (empty when nothing is appended). For a
    refusal, ``fix`` is the one fix line.
    """

    concept: str
    name: str
    kind: str
    decision: str  # "create" | "keep" | "refuse"
    options: tuple[str, ...] = ()
    resend: tuple[LiveOption, ...] = ()
    fix: str = ""


class BoardPlan(NamedTuple):
    """The ordered action list for one spec: per-field plans plus fallout.

    ``refused`` collects every ``FieldPlan.fix`` in spec order, and
    ``priority_map`` is the live option id to rank map for ranks the
    board already resolves; a rank with neither a live mapped id nor
    its standard name is omitted (its append is the fix).
    """

    fields: tuple[FieldPlan, ...]
    refused: tuple[str, ...]
    priority_map: dict[str, str]


def _refuse(concept: str, name: str, kind: str, fix: str) -> FieldPlan:
    """One refused field plan carrying its fix line."""
    return FieldPlan(concept=concept, name=name, kind=kind, decision="refuse", fix=fix)


def _plan_priority(
    spec_map: dict[str, str], live: LiveField
) -> tuple[tuple[str, ...], tuple[LiveOption, ...]]:
    """The Priority append and resend list for one kept field.

    Ranks resolve by mapped id first, then by standard name; only a
    rank with neither is appended under its standard name. Existing
    options ride the append unchanged, in live order.
    """
    live_by_name: dict[str, LiveOption] = {}
    for entry in live.options:
        live_by_name.setdefault(entry.name, entry)
    live_ids = {entry.id for entry in live.options}
    present = {
        rank for option_id, rank in spec_map.items() if option_id in live_ids and rank in PRIORITIES
    }
    missing = []
    for rank in PRIORITIES:
        if rank in present:
            continue
        if _RANK_OPTION[rank] in live_by_name:
            present.add(rank)
        else:
            missing.append(_RANK_OPTION[rank])
    if not missing:
        return (), ()
    return tuple(missing), live.options


def plan_board(spec: BoardSpec, live: tuple[LiveField, ...]) -> BoardPlan:
    """The adapter's action list for one spec over one live schema. Pure.

    Every spec field plans in spec order: an unambiguous live field
    with the right kind is kept (a kept Priority field appends ranks
    with neither a live mapped id nor their standard name), a missing
    field is created unless keep-only, and anything else refuses with
    a fix line. Unknown concepts, unknown kinds, and a creatable
    single-select with no options raise ``ValueError``: the spec
    cannot run as given.
    """
    for field in spec.fields:
        if field.concept not in BOARD_CONCEPTS:
            raise _spec_error(f"unknown board concept {field.concept!r}")
        if field.kind not in ("text", "single_select"):
            raise _spec_error(
                f"board field {field.name!r} for {field.concept} has an unknown kind {field.kind!r}"
            )
        if not field.keep_only and field.kind == "single_select" and not field.options:
            raise _spec_error(
                f"board field {field.name!r} for {field.concept} "
                "is single-select with no options to create"
            )
    spec_map = dict(spec.priority_map or {})
    by_name: dict[str, list[LiveField]] = {}
    for entry in live:
        by_name.setdefault(entry.name, []).append(entry)
    plans = []
    for field in spec.fields:
        matches = by_name.get(field.name, [])
        if len(matches) > 1:
            fix = (
                f"duplicate board field {field.name!r}: rename or delete "
                "the duplicate on the board, then re-run"
            )
            plans.append(_refuse(field.concept, field.name, field.kind, fix))
            continue
        if not matches:
            if field.keep_only:
                if field.concept in ("area", "pool"):
                    flag = f"--{field.concept}"
                    fix = (
                        f"board field {field.name!r} for {field.concept} "
                        f"is missing: re-run with {flag} NAME to create it"
                    )
                else:
                    fix = (
                        f"board field {field.name!r} for {field.concept} "
                        "is missing: add the field on the board by hand, then re-run"
                    )
                plans.append(_refuse(field.concept, field.name, field.kind, fix))
            else:
                plans.append(
                    FieldPlan(
                        concept=field.concept,
                        name=field.name,
                        kind=field.kind,
                        decision="create",
                        options=field.options,
                    )
                )
            continue
        found = matches[0]
        if found.kind != field.kind:
            fix = (
                f"board field {field.name!r} for {field.concept} has the wrong kind "
                f"(live {found.kind!r}, want {field.kind!r}): rename or delete "
                "the field on the board, then re-run"
            )
            plans.append(_refuse(field.concept, field.name, field.kind, fix))
            continue
        if field.keep_only:
            plans.append(
                FieldPlan(concept=field.concept, name=field.name, kind=field.kind, decision="keep")
            )
            continue
        if field.kind == "text" or field.concept != "priority":
            plans.append(
                FieldPlan(concept=field.concept, name=field.name, kind=field.kind, decision="keep")
            )
            continue
        seen: set[str] = set()
        duplicate = None
        for entry in found.options:
            if entry.name in seen:
                duplicate = entry.name
                break
            seen.add(entry.name)
        if duplicate is not None:
            fix = (
                f"duplicate board option {duplicate!r} on field {field.name!r}: "
                "rename or delete the duplicate on the board, then re-run"
            )
            plans.append(_refuse(field.concept, field.name, field.kind, fix))
            continue
        append, resend = _plan_priority(spec_map, found)
        plans.append(
            FieldPlan(
                concept=field.concept,
                name=field.name,
                kind=field.kind,
                decision="keep",
                options=append,
                resend=resend,
            )
        )
    ordered = tuple(plans)
    return BoardPlan(
        fields=ordered,
        refused=tuple(plan.fix for plan in ordered if plan.decision == "refuse"),
        priority_map=_final_priority_map(spec, ordered, live),
    )


def _final_priority_map(
    spec: BoardSpec, plans: tuple[FieldPlan, ...], live: tuple[LiveField, ...]
) -> dict[str, str]:
    """The live option id to rank map for the config file.

    The spec's existing id-to-rank entries whose ids are still live
    come first, then the live option carrying the standard name for
    each rank still unrepresented. A rank the board cannot resolve is
    omitted: its planned append is the fix.
    """
    plan = next((p for p in plans if p.concept == "priority"), None)
    if plan is None or plan.decision == "refuse":
        return {}
    field = next((f for f in live if f.name == plan.name), None)
    if field is None:
        live_ids: set[str] = set()
        live_by_name: dict[str, str] = {}
    else:
        live_ids = {entry.id for entry in field.options}
        live_by_name = {}
        for entry in field.options:
            live_by_name.setdefault(entry.name, entry.id)
    out: dict[str, str] = {}
    for option_id, rank in (spec.priority_map or {}).items():
        if option_id in live_ids and option_id not in out:
            out[option_id] = rank
    for rank in PRIORITIES:
        if rank in out.values():
            continue
        option_id = live_by_name.get(_RANK_OPTION[rank])
        if option_id is not None and option_id not in out:
            out[option_id] = rank
    return out
