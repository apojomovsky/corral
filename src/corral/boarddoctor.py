"""Doctor board checks: schema, option ids, flip refusal, parity.

One read-only check per repo in board mode, per docs/07-board-fields.md
section 5. The board must resolve, every mapped field and every mapped
option id must exist, and no queued writes may predate a variant flip.
The parity report compares label-derived and board-derived concepts per
stored item and renders stable text (sorted by repo and number), so a
one-time run after a board edit or a variant flip reports exactly what
differs.

Pure core plus two injected seams: ``fetch`` resolves one board config
to its live schema (raising ``TrackerError`` when it does not), and
``open_store`` opens the board store the parity rows and the outbox
counts are read from. Tests inject fakes at both; production wires the
GraphQL board reader and the real store in ``doctor.production_env``.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from corral import board, paths, settings
from corral import tracker as iface


class BoardSite(NamedTuple):
    """One repo the doctor may check: tracker repo, variant, board table."""

    repo: str  # "owner/name" the adapter talks to (or the entry name)
    variant: str  # "labels" | "board"
    board: settings.BoardConfig | None  # the resolved [board] table


class BoardRow(NamedTuple):
    """One board check row; ``doctor`` renders it as a ``Check``."""

    name: str
    status: str  # "ok" | "fail"
    detail: str
    fix: str


def check_options(repo: str, board_config: settings.BoardConfig, schema) -> list[str]:
    """Findings for option ids in ``priority_map`` missing from the board.

    Option ids are matched by stable id, never by display name: a rename
    is free, while a deleted or replaced id is a configuration error that
    names the repo, the field and the missing id. Pure.
    """
    fields = getattr(schema, "fields", None)
    if not isinstance(fields, dict):
        return [f"{repo}: the board schema has no fields table"]
    priority = fields.get("priority")
    if priority is None:
        if board_config.priority_map:
            return [
                f"{repo}: board field {board_config.priority_column!r} for the"
                " priority concept is not in the board schema, so the mapped"
                " option ids were not checked"
            ]
        return []
    options = getattr(priority, "options", None)
    if not isinstance(options, dict):
        return [f"{repo}: board field {board_config.priority_column!r} has no options"]
    findings = []
    for option_id in sorted(board_config.priority_map):
        if option_id not in options:
            rank = board_config.priority_map[option_id]
            findings.append(
                f"{repo}: board field {board_config.priority_column!r} has no"
                f" option {option_id!r} (mapped to {rank!r}); the option was"
                " deleted or replaced upstream"
            )
    return findings


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


def _check_site(site: BoardSite, *, store: board.Store, fetch) -> BoardRow:
    """One repo in board mode as a row. A labels repo never reaches here."""
    name = f"board:{site.repo}"
    if site.board is None:
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: tracker.variant is 'board' but no [board] table names the project board",
            "add the [repo.board] table (project_owner plus project_number"
            " or project_id) to corral.toml, then run `corral doctor` again",
        )
    try:
        schema = fetch(site.board)
    except iface.TrackerRateLimited as err:
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: the board read hit the shared budget and is rate"
            f" limited (transient): {err}",
            "wait for the cooldown and re-run `corral doctor`",
        )
    except Exception as err:
        return BoardRow(
            name,
            "fail",
            f"{site.repo}: the board does not resolve: {err}",
            "confirm project_owner and project_number (or project_id) in"
            " corral.toml name a board this account can read,"
            " then run `corral doctor` again",
        )
    findings = check_options(site.repo, site.board, schema)
    fixes = []
    if findings:
        fixes.append(
            "correct the [repo.board] table in corral.toml (field names and"
            " option ids must match the live board), then run `corral doctor` again"
        )
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
        findings.append(refusal)
        fixes.append(
            "drain the outbox (the mirror pass pushes queued writes) until"
            " empty, then flip tracker.variant and resync"
        )
    mismatches = parity_lines(rows)
    if mismatches:
        findings.extend(mismatches)
        fixes.append(
            "backfill the board fields from the labels on the board itself"
            " (a people action, reviewable there), then resync"
        )
    if findings:
        return BoardRow(name, "fail", "; ".join(findings), "; ".join(fixes))
    fields = getattr(schema, "fields", None)
    count = len(fields) if isinstance(fields, dict) else 0
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
) -> list[BoardRow]:
    """One row per repo in board mode, in the given order.

    Labels repos get no row. Sites with no seams fail closed instead of
    reaching the real pacer or a real subprocess. The store opens once
    for all sites and closes before returning.
    """
    wanted = [site for site in sites if site.variant == "board"]
    if not wanted:
        return []
    if fetch is None or open_store is None:
        return [
            BoardRow(
                f"board:{site.repo}",
                "fail",
                f"{site.repo}: the board check has no schema seam wired",
                "reinstall corral, then run `corral doctor` again",
            )
            for site in wanted
        ]
    try:
        store = open_store()
    except Exception as err:
        repos = ", ".join(site.repo for site in wanted)
        return [
            BoardRow(
                "board",
                "fail",
                f"cannot read the board store for {repos}: {err}",
                "make the state dir usable, then run `corral doctor` again",
            )
        ]
    try:
        return [_check_site(site, store=store, fetch=fetch) for site in wanted]
    finally:
        store.close()


def sites_from_settings(
    root: str | None, *, env: Mapping[str, str] | None = None
) -> tuple[list[BoardSite], str | None]:
    """Board-mode sites from the ops root, or ([], error) that names it.

    Only repos with ``tracker.variant`` set to ``board`` become sites;
    labels repos get no doctor row. Reads plain files, never the network.
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
        if str(loaded["tracker.variant"]) != "board":
            continue
        repo = str(loaded["tracker.repo"] or name)
        sites.append(BoardSite(repo=repo, variant="board", board=loaded.board))
    return sites, None
