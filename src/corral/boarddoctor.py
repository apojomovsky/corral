"""Doctor board checks: schema, option ids, flip refusal, parity.

One read-only check per repo in board mode, per docs/07-board-fields.md
section 5 and docs/11-board-standard.md section 5. The board must
resolve, every mapped field and every mapped option id must exist, and
no queued writes may predate a variant flip. The parity report compares
label-derived and board-derived concepts per stored item and renders
stable text (sorted by repo and number), so a one-time run after a
board edit or a variant flip reports exactly what differs.

Pure core plus three injected seams: ``fetch`` resolves one board
config to its live snapshot (raw fields, title, owner boards, raising
``TrackerError`` when it does not), ``board_items`` lists the
``(repo, number)`` pairs on the board for the missing-from-board row,
and ``open_store`` opens the board store the parity rows and the outbox
counts are read from. Tests inject fakes at all three; production wires
the GraphQL board reader and the real store in ``doctor.production_env``.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from corral import board, paths, settings
from corral import tracker as iface
from corral.github import gh


class BoardSite(NamedTuple):
    """One repo the doctor may check: tracker repo, variant, board table."""

    repo: str  # "owner/name" the adapter talks to (or the entry name)
    variant: str  # "labels" | "board"
    board: settings.BoardConfig | None  # the resolved [board] table


class BoardRow(NamedTuple):
    """One board check row; ``doctor`` renders it as a ``Check``."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str


class FieldFinding(NamedTuple):
    """One schema comparison outcome: its severity, text and fix line."""

    severity: str  # "fail" | "warn"
    detail: str
    fix: str


#: The wanted kind per concept, from the standard board: the one mapping
#: from a concept key to ``"text"`` or ``"single_select"``, beside the
#: concept-attribute mapping in ``tracker``. The reader and the
#: provisioner resolve the same fact through their own copies; after a
#: successful provision the file and the board agree because setup
#: writes the provision's live names into the config.
_WANT_KIND = {field.concept: field.kind for field in iface.STANDARD_BOARD}

#: How many missing issue numbers one row names before counting the rest.
_SHOWN_MISSING = 5


def _priority_options(
    repo: str, board_config: settings.BoardConfig, field_name: str, live: iface.LiveField
) -> list[FieldFinding]:
    """Failures and warns for the live Priority options against the map.

    Option ids are matched by stable id, never by display name: a rename
    is free, while a deleted or replaced id is a configuration error that
    names the repo, the field and the missing id. A live option no map
    entry names warns: those items keep their stored rank.
    """
    findings = []
    names = [option.name for option in live.options]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    for name in duplicates:
        findings.append(
            FieldFinding(
                "fail",
                f"{repo}: board field {field_name!r} has two options named {name!r}",
                "rename or delete the duplicate on the board, then re-run `corral github setup`",
            )
        )
    live_ids = {option.id for option in live.options}
    for option_id in sorted(board_config.priority_map):
        if option_id not in live_ids:
            rank = board_config.priority_map[option_id]
            findings.append(
                FieldFinding(
                    "fail",
                    f"{repo}: board field {field_name!r} has no"
                    f" option {option_id!r} (mapped to {rank!r}); the option was"
                    " deleted or replaced upstream",
                    "re-add the option on the board, or delete the board table"
                    " and re-run `corral github setup`",
                )
            )
    for option in live.options:
        if option.id not in board_config.priority_map:
            findings.append(
                FieldFinding(
                    "warn",
                    f"{repo}: board field {field_name!r} has an unmapped option {option.name!r}",
                    "add its id to priority_map in corral.toml, or leave it:"
                    " items with it keep their stored rank",
                )
            )
    return findings


def compare_fields(
    repo: str, board_config: settings.BoardConfig, fields: Sequence[iface.LiveField]
) -> tuple[FieldFinding, ...]:
    """The live raw field list against the config, as findings. Pure.

    Every mapped concept must exist with its standard kind; the Priority
    option ids must exist with a warn for live options the map does not
    name. Duplicate live names (fields or options) refuse with a fix
    line instead of this picking one. Unmapped concepts are silent.
    """
    findings: list[FieldFinding] = []
    by_name: dict[str, list] = {}
    for field in fields:
        by_name.setdefault(field.name, []).append(field)
    for concept, attr in iface.BOARD_CONCEPT_ATTRS:
        name = getattr(board_config, attr)
        if not name:
            continue
        candidates = by_name.get(name, [])
        if len(candidates) > 1:
            findings.append(
                FieldFinding(
                    "fail",
                    f"{repo}: board field {name!r} for the {concept} concept"
                    f" is ambiguous ({len(candidates)} fields share the name)",
                    "rename or delete the duplicate on the board,"
                    " then re-run `corral github setup`",
                )
            )
            continue
        if not candidates:
            findings.append(
                FieldFinding(
                    "fail",
                    f"{repo}: board field {name!r} for the {concept} concept"
                    " is missing from the board",
                    "run `corral github setup` (it creates the missing field and alters nothing)",
                )
            )
            continue
        (live,) = candidates
        want = _WANT_KIND[concept]
        if live.kind != want:
            findings.append(
                FieldFinding(
                    "fail",
                    f"{repo}: board field {name!r} for the {concept} concept"
                    f" is {live.kind!r}, want {want!r}",
                    "rename or delete the field on the board, then run `corral github setup`",
                )
            )
            continue
        if concept == "priority":
            findings.extend(_priority_options(repo, board_config, name, live))
    if not board_config.priority_column and board_config.priority_map:
        findings.append(
            FieldFinding(
                "fail",
                f"{repo}: the priority concept maps to no board field,"
                " so the mapped option ids were not checked",
                "map the priority concept in the [repo.board] table,"
                " then run `corral github setup`",
            )
        )
    return tuple(findings)


def duplicate_title_warn(repo: str, snapshot: iface.BoardSnapshot) -> FieldFinding | None:
    """Warn when another board under the owner shares the board's title.

    The addressed board itself is healthy, and which of the two a person
    deletes is a people's call, so this warns naming the title and every
    matching number instead of failing. Pure.
    """
    matches = sorted(
        entry.number for entry in snapshot.owner_boards if entry.title == snapshot.title
    )
    if len(matches) < 2:
        return None
    numbers = ", ".join(str(number) for number in matches)
    return FieldFinding(
        "warn",
        f"{repo}: another board under the same owner shares the title"
        f" {snapshot.title!r} (numbers {numbers})",
        "delete the board the config does not address, by hand",
    )


def flip_refusal(repo: str, queued: int) -> str | None:
    """Why a variant flip is refused, or None when the outbox is drained.

    A flip with a non-empty outbox is refused as a configuration error
    so no queued write crosses variants. Pure.
    """
    if queued <= 0:
        return None
    writes = "write" if queued == 1 else "writes"
    return (
        f"{repo}: variant flip refused with {queued} queued {writes};"
        " drain the outbox until empty, then flip tracker.variant"
    )


def parity_lines(items: Sequence[board.Item]) -> list[str]:
    """Stable mismatch text comparing label-derived and board concepts.

    Only concepts with both a label side and a board side are compared:
    the label rank against the board rank, plus any blocked-by entry the
    board carried raw because it does not parse (which reads as blocked).
    Single-sided values are silent: an absent side cannot conflict.
    Sorted by repo and number, so the output is stable text. Pure.
    """
    lines = []
    for item in sorted(items, key=lambda found: (found.repo, found.number)):
        extra = item.extra if isinstance(item.extra, dict) else {}
        label = item.priority or ""
        board_rank = extra.get("priority")
        if label and isinstance(board_rank, str) and board_rank and label != board_rank:
            lines.append(
                f"{item.repo}#{item.number}: priority {label!r} from labels"
                f" but {board_rank!r} from the board"
            )
        blocked = extra.get("blocked_by")
        if isinstance(blocked, str):
            lines.append(
                f"{item.repo}#{item.number}: blocked-by entry {blocked!r}"
                " does not parse; the ticket reads as blocked"
            )
    return lines


def missing_finding(repo: str, missing: Sequence[int]) -> FieldFinding:
    """One fail finding for open issues missing from the board. Pure.

    Names the count and the first few numbers; the fix adopts them.
    """
    shown = ", ".join(str(number) for number in missing[:_SHOWN_MISSING])
    rest = len(missing) - _SHOWN_MISSING
    tail = f" and {rest} more" if rest > 0 else ""
    if len(missing) == 1:
        detail = f"{repo}: 1 open issue is missing from the board (number {shown})"
    else:
        detail = (
            f"{repo}: {len(missing)} open issues are missing from the board (numbers {shown}{tail})"
        )
    return FieldFinding(detail=detail, severity="fail", fix="run `corral github setup --populate`")


def _board_address(board_config: settings.BoardConfig) -> str:
    """The configured board as ``owner number N`` or ``owner id ID``."""
    if board_config.project_number is not None:
        return f"{board_config.project_owner} number {board_config.project_number}"
    return f"{board_config.project_owner} id {board_config.project_id!r}"


def _unresolvable_row(site: BoardSite, err: Exception) -> BoardRow:
    """The board-naming failure row for a fetch error, scope-aware.

    A scope-classified error is the token missing its board scope, with
    the refresh as the fix; anything else names the owner and number
    (or node id) that does not resolve, with the remove-the-table fix.
    """
    name = f"board:{site.repo}"
    if gh.classify_error(str(err)) == "scope":
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: the board read needs the `project` token scope: {err}",
            "run `gh auth refresh -s project`, then run `corral doctor` again",
        )
    assert site.board is not None
    return BoardRow(
        name,
        "fail",
        f"{site.repo}: the board {_board_address(site.board)} does not resolve: {err}",
        "the board in the config no longer exists: remove the table and"
        " re-run `corral github setup`, or correct the address by hand",
    )


def _membership_finding(site: BoardSite, store: board.Store, board_items) -> FieldFinding | None:
    """The missing-from-board finding over the second injected fetch.

    Lists the board's ``(repo, number)`` pairs and diffs them against
    the store's open rows for the repo. A listing that falls over fails
    closed instead of reporting on partial knowledge. Pure apart from
    the two seams.
    """
    assert site.board is not None
    if board_items is None:
        return FieldFinding(
            "fail",
            f"{site.repo}: the board check has no membership seam wired",
            "reinstall corral, then run `corral doctor` again",
        )
    try:
        refs = board_items(site.board)
    except iface.TrackerRateLimited as err:
        return FieldFinding(
            "fail",
            f"{site.repo}: the board read hit the shared budget and is rate"
            f" limited (transient): {err}",
            "wait for the cooldown, re-run `corral doctor`",
        )
    except Exception as err:
        if gh.classify_error(str(err)) == "scope":
            return FieldFinding(
                "fail",
                f"{site.repo}: the board read needs the `project` token scope: {err}",
                "run `gh auth refresh -s project`, then run `corral doctor` again",
            )
        return FieldFinding(
            "fail",
            f"{site.repo}: cannot list the board's items: {err}",
            "re-run `corral doctor`",
        )
    try:
        pairs = {(repo, number) for repo, number in refs}
    except (TypeError, ValueError) as err:
        return FieldFinding(
            "fail",
            f"{site.repo}: cannot list the board's items: {err}",
            "re-run `corral doctor`",
        )
    board_numbers = {number for repo, number in pairs if repo == site.repo}
    missing = sorted(item.number for item in store.items(repo=site.repo, state="open"))
    missing = [number for number in missing if number not in board_numbers]
    if not missing:
        return None
    return missing_finding(site.repo, missing)


def _labels_table_row(site: BoardSite) -> BoardRow:
    """The warn-only row for a labels entry with a provisioned table.

    Answered from config alone: no board fetch and no store call, so
    the row costs nothing and cannot fail on an unreachable board.
    """
    return BoardRow(
        f"board:{site.repo}",
        "warn",
        f"{site.repo}: the [repo.board] table is provisioned but tracker.variant is still 'labels'",
        "drain the outbox, backfill on the board, set tracker.variant ="
        ' "board", resync, then run `corral doctor`; or remove the'
        " [repo.board] table",
    )


def _check_site(site: BoardSite, *, store: board.Store, fetch, board_items) -> BoardRow:
    """One repo in board mode as a row. A labels repo never reaches here."""
    name = f"board:{site.repo}"
    if site.board is None:
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: tracker.variant is 'board' but no [repo.board] table names the project board",
            "run `corral github setup` (it creates the board and writes the [repo.board] table)",
        )
    try:
        snapshot = fetch(site.board)
    except iface.TrackerRateLimited as err:
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: the board read hit the shared budget and is rate"
            f" limited (transient): {err}",
            "wait for the cooldown, re-run `corral doctor`",
        )
    except Exception as err:
        return _unresolvable_row(site, err)
    findings = list(compare_fields(site.repo, site.board, snapshot.fields))
    try:
        title_warn = duplicate_title_warn(site.repo, snapshot)
    except Exception as err:
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: the board title check failed: {err}",
            "re-run `corral doctor`",
        )
    if title_warn is not None:
        findings.append(title_warn)
    try:
        queued = [row for row in store.queued_writes() if row.repo == site.repo]
        rows = store.items(repo=site.repo)
    except Exception as err:
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: cannot read the board store: {err}",
            "make the state dir usable, then run `corral doctor` again",
        )
    refusal = flip_refusal(site.repo, len(queued))
    if refusal is not None:
        findings.append(
            FieldFinding(
                "fail",
                refusal,
                "drain the outbox (the mirror pass pushes queued writes) until"
                " empty, then flip tracker.variant and resync",
            )
        )
    mismatches = parity_lines(rows)
    if mismatches:
        findings.append(
            FieldFinding(
                "fail",
                "; ".join(mismatches),
                "backfill the board fields from the labels on the board itself"
                " (a people action, reviewable there), then resync",
            )
        )
    membership = _membership_finding(site, store, board_items)
    if membership is not None:
        findings.append(membership)
    if findings:
        fixes = list(dict.fromkeys(finding.fix for finding in findings))
        status = "fail" if any(finding.severity == "fail" for finding in findings) else "warn"
        return BoardRow(
            name, status, "; ".join(finding.detail for finding in findings), "; ".join(fixes)
        )
    count = len(snapshot.fields)
    return BoardRow(
        name,
        "ok",
        f"the board resolves ({count} fields,"
        f" {len(site.board.priority_map)} mapped options); the outbox is"
        f" empty; parity is clean ({len(rows)} items)",
        "",
    )


def check_sites(
    sites: Sequence[BoardSite],
    *,
    fetch: Callable[[settings.BoardConfig], object] | None = None,
    open_store: Callable[[], board.Store] | None = None,
    board_items: Callable[[settings.BoardConfig], object] | None = None,
) -> list[BoardRow]:
    """One row per board-mode repo, plus one warn per provisioned labels repo.

    Site order is kept. Labels repos with a `[repo.board]` table get
    their warn-only row from config alone, with no `fetch` and no store
    call; a labels repo without a table still gets no row. Board-mode
    sites with no seams fail closed instead of reaching the real pacer
    or a real subprocess. The store opens once for all board sites and
    closes before returning.
    """
    kinds = []
    for site in sites:
        if site.variant == "board":
            kinds.append(("board", site))
        elif site.board is not None:
            kinds.append(("labels", site))
    if not kinds:
        return []
    board_sites = [site for kind, site in kinds if kind == "board"]
    board_rows: list[BoardRow] = []
    if board_sites:
        if fetch is None or open_store is None:
            board_rows = [
                BoardRow(
                    f"board:{site.repo}",
                    "fail",
                    f"{site.repo}: the board check has no schema seam wired",
                    "reinstall corral, then run `corral doctor` again",
                )
                for site in board_sites
            ]
        else:
            try:
                store = open_store()
            except Exception as err:
                repos = ", ".join(site.repo for site in board_sites)
                board_rows = [
                    BoardRow(
                        "board",
                        "fail",
                        f"cannot read the board store for {repos}: {err}",
                        "make the state dir usable, then run `corral doctor` again",
                    )
                ]
            else:
                try:
                    board_rows = [
                        _check_site(site, store=store, fetch=fetch, board_items=board_items)
                        for site in board_sites
                    ]
                finally:
                    store.close()
    positions = [index for index, (kind, _) in enumerate(kinds) if kind == "board"]
    placed = dict(zip(positions, board_rows))
    rows = []
    for index, (kind, site) in enumerate(kinds):
        if kind == "labels":
            rows.append(_labels_table_row(site))
        elif index in placed:
            rows.append(placed[index])
    return rows


def sites_from_settings(
    root: str | None, *, env: Mapping[str, str] | None = None
) -> tuple[list[BoardSite], str | None]:
    """Board sites from the ops root, or ([], error) that names it.

    Every repo with ``tracker.variant`` set to ``board`` becomes a
    site, plus every labels repo whose resolved `[repo.board]` table is
    not `None` (answered warn-only from config, with no board fetch). A
    labels repo with no table still gets no doctor row. Reads plain
    files, never the network.
    """
    try:
        names = settings.repo_names(root)
    except (settings.SettingsError, OSError) as err:
        return [], f"cannot list [[repo]] entries: {err}"
    sites = []
    for name in names:
        try:
            loaded = settings.load(root, repo=name, env=env)
        except (settings.SettingsError, paths.PathError, OSError) as err:
            return [], f"cannot resolve [[repo]] {name}: {err}"
        if str(loaded["tracker.variant"]) != "board" and loaded.board is None:
            continue
        repo = str(loaded["tracker.repo"] or name)
        variant = str(loaded["tracker.variant"])
        sites.append(BoardSite(repo=repo, variant=variant, board=loaded.board))
    return sites, None
