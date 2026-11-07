"""Doctor rows for the remote-dependent project checks.

`docs/06-korapy-new.md`, section 6: a fresh `new` tree has no remote,
so the rows that need one (the default branch on the remote, the board
labels) warn instead of failing. Once a remote exists the rows are
strict. Pure core over the readings in `RepoSite`: `doctor` renders
each `RepoRow` as a `Check`, so this module never imports `doctor`.
"""

from typing import NamedTuple

from korapy import pushenv


class RepoSite(NamedTuple):
    """One project the remote rows cover, with its readings as data.

    `origin` is the remote URL, None when no remote is set. `branch`
    and `labels` are each "ok", "missing", "refused" (labels only) or
    "unknown" (the check could not run: the remote is unreachable).
    Both are meaningless without an origin and are ignored then.
    `cause` names the gather failure behind an "unknown" reading (the
    tracker unreachable, no access, an unreadable clone), and is None
    for a cleanly gathered site. `tracker_repo` is the configured
    `owner/name` and `canonical` the name the tracker answers for it
    (the renamed name after a GitHub rename), each None when unknown;
    the tracker row compares them.
    """

    name: str
    origin: str | None
    branch: str = "unknown"
    labels: str = "unknown"
    cause: str | None = None
    tracker_repo: str | None = None
    canonical: str | None = None
    #: How a launch would push to this origin: "ssh", "https" (the
    #: fallback rewrites SSH GitHub addresses to HTTPS through the host
    #: `gh` login) or "refuse" (neither path works). None means no
    #: reading, and then the report carries no push row.
    push: str | None = None


class RepoRow(NamedTuple):
    """One remote row; `doctor` renders it as a `Check`."""

    name: str  # "remote:<name>" | "labels:<name>"
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str


#: The fix for a gather failure: the row names the repo and the cause,
#: the operator resolves it and re-runs.
_CAUSE_FIX = "resolve the cause above, then run `korapy doctor` again"


def _remote_row(site: RepoSite) -> RepoRow:
    """The default-branch-on-the-remote row: warn without a remote."""
    name = f"remote:{site.name}"
    if site.origin is None and site.cause is None:
        return RepoRow(
            name,
            "warn",
            f"no remote is set for {site.name},"
            " so the default branch cannot be checked on the remote",
            "create the remote on the forge, then run `korapy github setup`"
            " (a fresh `korapy new` tree: re-run with `--create-remote`)",
        )
    if site.origin is None:
        return RepoRow(name, "warn", f"{site.name}: {site.cause}", _CAUSE_FIX)
    if site.branch == "missing":
        return RepoRow(
            name,
            "fail",
            f"{site.name}: the default branch does not exist on the remote",
            "push the default branch to the remote, then run `korapy doctor` again",
        )
    if site.branch == "ok":
        return RepoRow(
            name,
            "ok",
            f"the default branch for {site.name} exists on the remote",
            "",
        )
    if site.cause is not None:
        return RepoRow(
            name,
            "warn",
            f"{site.name}: the default branch was not checked: {site.cause}",
            _CAUSE_FIX,
        )
    # Defensive-only: every origin-set gather path already sets branch or cause.
    return RepoRow(
        name,
        "warn",
        f"{site.name}: the remote could not be reached, so the default branch was not checked",
        "check the network and that `gh` is logged in, then run `korapy doctor` again",
    )


def _labels_row(site: RepoSite) -> RepoRow:
    """The labels-exist row: warn without a remote, strict with one."""
    name = f"labels:{site.name}"
    if site.origin is None and site.cause is None:
        return RepoRow(
            name,
            "warn",
            f"no remote is set for {site.name}, so the board labels cannot be checked",
            "run `korapy github setup` once the remote exists",
        )
    if site.origin is None:
        return RepoRow(name, "warn", f"{site.name}: {site.cause}", _CAUSE_FIX)
    if site.labels == "missing":
        return RepoRow(
            name,
            "fail",
            f"{site.name}: the board labels are missing on the remote",
            "run `korapy github setup`, then run `korapy doctor` again",
        )
    if site.labels == "refused":
        return RepoRow(
            name,
            "fail",
            f"{site.name}: the board labels were refused on the remote",
            "ask an owner to create the missing labels, or move the board"
            " to the ops side, then run `korapy doctor` again",
        )
    if site.labels == "ok":
        return RepoRow(
            name,
            "ok",
            f"the board labels for {site.name} exist on the remote",
            "",
        )
    if site.cause is not None:
        return RepoRow(
            name,
            "warn",
            f"{site.name}: the board labels were not checked: {site.cause}",
            _CAUSE_FIX,
        )
    # Defensive-only: every origin-set gather path already sets labels or cause.
    return RepoRow(
        name,
        "warn",
        f"{site.name}: the remote could not be reached, so the board labels were not checked",
        "check the network and that `gh` is logged in, then run `korapy doctor` again",
    )


def _tracker_row(site: RepoSite) -> RepoRow | None:
    """The canonical-name row: warn when the tracker answers a rename.

    The configured `owner/name` and the tracker's answered name agree
    after a healthy setup; after a GitHub rename the tracker answers
    the new name while the config still holds the old one, and
    search-based calls quietly return nothing for the old name. Case
    alone never warns: tracker lookups are case-insensitive. No row
    when either name is unknown: an unreadable name is not evidence.
    """
    from korapy import reponame

    if not site.tracker_repo or not site.canonical:
        return None
    name = f"tracker:{site.name}"
    if reponame.canonical_matches(site.tracker_repo, site.canonical):
        return RepoRow(
            name,
            "ok",
            f"the tracker answers {site.tracker_repo} as configured",
            "",
        )
    return RepoRow(
        name,
        "warn",
        f"{site.name}: configured as {site.tracker_repo} but the tracker answers"
        f" {site.canonical}: the repository was renamed",
        f"run `korapy repo rename {site.tracker_repo} {site.canonical}`,"
        " then point the entry at the new name and run `korapy doctor` again",
    )


def _push_row(site: RepoSite) -> RepoRow | None:
    """The push-path row: how a launch would push to this origin.

    No row without a reading, or without an origin (the remote row
    already covers that). SSH reads ok; the HTTPS fallback reads ok
    with a note; an unpushable origin fails naming both fixes.
    """
    if site.push is None or site.origin is None:
        return None
    name = f"push:{site.name}"
    if site.push == "refuse":
        return RepoRow(
            name,
            "fail",
            f"{site.name}: neither SSH nor the host gh login can push to the origin remote",
            "restore the SSH agent (or add a key usable without one),"
            " or run `gh auth login`, then run `korapy doctor` again",
        )
    if site.push == "https":
        return RepoRow(
            name,
            "ok",
            f"{site.name}: no usable SSH agent, so pushes rewrite SSH GitHub"
            " addresses to HTTPS through the host gh login",
            "",
        )
    if site.push != "ssh":
        return None
    if pushenv.is_ssh_github_url(site.origin):
        return RepoRow(
            name,
            "ok",
            f"SSH to the remote works for {site.name}; pushes use it",
            "",
        )
    return RepoRow(
        name,
        "ok",
        f"the origin for {site.name} needs no SSH agent, so pushes work as configured",
        "",
    )


def check_sites(sites) -> list[RepoRow]:
    """Remote, labels and push rows per site, in order, then the tracker row when readable."""
    rows = []
    for site in sites:
        rows.append(_remote_row(site))
        rows.append(_labels_row(site))
        pushed = _push_row(site)
        if pushed is not None:
            rows.append(pushed)
        tracker = _tracker_row(site)
        if tracker is not None:
            rows.append(tracker)
    return rows
