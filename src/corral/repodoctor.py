"""Doctor rows for the remote-dependent project checks.

`docs/06-corral-new.md`, section 6: a fresh `new` tree has no remote,
so the rows that need one (the default branch on the remote, the board
labels) warn instead of failing. Once a remote exists the rows are
strict. Pure core over the readings in `RepoSite`: `doctor` renders
each `RepoRow` as a `Check`, so this module never imports `doctor`.
"""

from typing import NamedTuple


class RepoSite(NamedTuple):
    """One project the remote rows cover, with its readings as data.

    `origin` is the remote URL, None when no remote is set. `branch`
    and `labels` are each "ok", "missing", "refused" (labels only) or
    "unknown" (the check could not run: the remote is unreachable).
    Both are meaningless without an origin and are ignored then.
    """

    name: str
    origin: str | None
    branch: str = "unknown"
    labels: str = "unknown"


class RepoRow(NamedTuple):
    """One remote row; `doctor` renders it as a `Check`."""

    name: str  # "remote:<name>" | "labels:<name>"
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str


def _remote_row(site: RepoSite) -> RepoRow:
    """The default-branch-on-the-remote row: warn without a remote."""
    name = f"remote:{site.name}"
    if site.origin is None:
        return RepoRow(
            name,
            "warn",
            f"no remote is set for {site.name},"
            " so the default branch cannot be checked on the remote",
            "create the remote on the forge, then run `corral github setup`"
            " (a fresh `corral new` tree: re-run with `--create-remote`)",
        )
    if site.branch == "missing":
        return RepoRow(
            name,
            "fail",
            f"{site.name}: the default branch does not exist on the remote",
            "push the default branch to the remote, then run `corral doctor` again",
        )
    if site.branch == "ok":
        return RepoRow(
            name,
            "ok",
            f"the default branch for {site.name} exists on the remote",
            "",
        )
    return RepoRow(
        name,
        "warn",
        f"{site.name}: the remote could not be reached, so the default branch was not checked",
        "check the network and that `gh` is logged in, then run `corral doctor` again",
    )


def _labels_row(site: RepoSite) -> RepoRow:
    """The labels-exist row: warn without a remote, strict with one."""
    name = f"labels:{site.name}"
    if site.origin is None:
        return RepoRow(
            name,
            "warn",
            f"no remote is set for {site.name}, so the board labels cannot be checked",
            "run `corral github setup` once the remote exists",
        )
    if site.labels == "missing":
        return RepoRow(
            name,
            "fail",
            f"{site.name}: the board labels are missing on the remote",
            "run `corral github setup`, then run `corral doctor` again",
        )
    if site.labels == "refused":
        return RepoRow(
            name,
            "fail",
            f"{site.name}: the board labels were refused on the remote",
            "ask an owner to create the missing labels, or move the board"
            " to the ops side, then run `corral doctor` again",
        )
    if site.labels == "ok":
        return RepoRow(
            name,
            "ok",
            f"the board labels for {site.name} exist on the remote",
            "",
        )
    return RepoRow(
        name,
        "warn",
        f"{site.name}: the remote could not be reached, so the board labels were not checked",
        "check the network and that `gh` is logged in, then run `corral doctor` again",
    )


def check_sites(sites) -> list[RepoRow]:
    """One remote row plus one labels row per site, in order. Pure."""
    rows = []
    for site in sites:
        rows.append(_remote_row(site))
        rows.append(_labels_row(site))
    return rows
