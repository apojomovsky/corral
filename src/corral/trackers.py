"""One tracker factory for every repo: labels or board-backed, from settings.

``syncli``, ``mirrorcli``, ``drive`` and ``doctor`` all build repo
trackers through this module, so no other module decides the variant.
The factory reads the validated ``[repo.board]`` data off the resolved
settings and never re-reads the file. Production runs are wired here
too: paced REST for label mode, the raw ``gh`` runner for board mode
(the board reader paces its own GraphQL through the shared bucket).
"""

from typing import NamedTuple

from corral import tracker as iface
from corral.github import gh
from corral.github.board import BoardReader, BoardSchema, BoardTracker
from corral.github.gh import paced_run
from corral.github.tracker import GithubTracker

#: The tracker variants one ``[[repo]]`` entry may name. ``settings``
#: imports this set instead of keeping its own copy.
VARIANTS = frozenset({"labels", "board"})

#: One board item's project items: the issue's board memberships, newest
#: first, filtered to the configured project by the reader. ``first: 20``
#: bounds one lookup; an issue is rarely on more boards than that, and a
#: longer list pages with ``after`` until ``hasNextPage`` ends it.
_ITEM = """query BoardItemByNumber($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      projectItems(first: 20, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          updatedAt
          project { id }
          fieldValues(first: 50) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldTextValue {
                text field { ... on ProjectV2Field { id name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                optionId name field { ... on ProjectV2SingleSelectField { id name } }
              }
            }
          }
        }
      }
    }
  }
}"""


def _fail(text) -> iface.TrackerError:
    return iface.TrackerError(f"unusable board answer: {text}")


def _split_repo(repo) -> tuple[str, str]:
    """``(owner, name)`` for a tracker repo, else ``TrackerError``."""
    parts = repo.split("/") if isinstance(repo, str) else []
    if len(parts) != 2 or not all(parts):
        raise iface.TrackerError(f"bad board repo {repo!r}")
    return parts[0], parts[1]


class BoardBackedTracker(BoardTracker):
    """A board repo's full tracker: board reads, board writes, REST feed.

    Reads and writes share the one injected ``run`` seam. One instance
    covers one pass: the board schema is fetched once and reused by every
    read (a targeted single-item read, the board step's poll), while the
    write scan keeps the parent's per-item cache. A later pass builds a
    new instance, so nothing goes stale across passes.
    """

    def __init__(self, board_config, run, pacer=None, *, max_pages):
        super().__init__(board_config, run, pacer, max_pages=max_pages)
        self._project = None
        self._board_schema: BoardSchema | None = None

    @property
    def board_poll_pages(self) -> int:
        """The board page cap this instance polls and scans with."""
        return self._max_pages

    def _fetch_project_cached(self):
        """The live board project object, fetched once per instance.

        Reads and writes share this one fetch, so a pass that polls and
        then mirrors costs one schema call, not one per path.
        """
        if self._project is None:
            self._project = self._fetch_project()
        return self._project

    def _cached_schema(self):
        """``(project id, fields by name)``, over the shared fetch."""
        if self._schema is None:
            self._schema = self._project_index(self._fetch_project_cached())
        return self._schema

    def board_schema(self) -> BoardSchema:
        """The resolved board schema, over the shared fetch."""
        if self._board_schema is None:
            self._board_schema = self._parse_schema(self._fetch_project_cached())
        return self._board_schema

    def item(self, repo: str, number: int) -> iface.TrackerItem:
        """One item, board concepts overlaid on the live REST issue.

        A targeted GraphQL read finds the issue's board item no matter
        how large the board is (no page cap, no poll). Anything short of
        the full shape is a ``TrackerError``; an issue the board does not
        hold is one too, never a silent concept-less REST fallback.
        """
        schema = self.board_schema()
        node = self._fetch_single(schema.project_id, repo, number)
        rest_item, milestone = self._issues.read_issue(repo, number)
        by_field = {field.id: concept for concept, field in schema.fields.items()}
        board_time, texts, selects = self._node_item(by_field, node)
        extra = self._concepts(selects, texts, milestone)
        return rest_item._replace(
            updated_at=max(board_time, rest_item.updated_at),
            agent=texts.get("agent", ""),
            status=texts.get("status", ""),
            extra=extra,
        )

    def changes(self, repo: str, cursor: str | None) -> tuple[list, str]:
        """The REST change feed, exactly as in label mode."""
        return self._issues.changes(repo, cursor)

    def items(self, repo: str):
        """Every REST item, oldest first, for a full resync."""
        return self._issues.items(repo)

    def comment(self, repo: str, number: int, body: str) -> None:
        """One decision-protocol comment, through the REST adapter."""
        self._issues.comment(repo, number, body)

    def add_label(self, repo: str, number: int, label: str) -> None:
        """The one decision label, through the REST adapter."""
        self._issues.add_label(repo, number, label)

    def _fetch_single(self, project_id, repo, number):
        """The board item node behind ``repo#number``, else ``TrackerError``."""
        owner, name = _split_repo(repo)
        after = None
        while True:
            if after is None:
                payload = self._graphql(_ITEM, owner=owner, name=name, number=number)
            else:
                payload = self._graphql(_ITEM, owner=owner, name=name, number=number, after=after)
            nodes, info = self._single_page(payload, repo, number)
            for node in nodes:
                if self._node_project(node) == project_id:
                    if any(
                        self._node_project(other) == project_id
                        for other in nodes
                        if other is not node
                    ):
                        raise _fail(f"board item {repo}#{number} is ambiguous")
                    return node
            end, more = self._resume(info, len(nodes))
            if not more:
                raise iface.TrackerError(f"unknown board item {repo}#{number}")
            after = end

    def _single_page(self, payload, repo, number):
        """One project-items page as ``(nodes, pageInfo)``, else ``TrackerError``."""
        if not isinstance(payload, dict):
            raise _fail("board item is not an object")
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            raise _fail("board repository is missing")
        issue = repository.get("issue")
        if issue is None:
            raise iface.TrackerError(f"unknown board item {repo}#{number}")
        if not isinstance(issue, dict):
            raise _fail("board issue is not an object")
        scoped = issue.get("projectItems")
        if not isinstance(scoped, dict):
            raise _fail("board project items are missing")
        nodes = scoped.get("nodes")
        if not isinstance(nodes, list):
            raise _fail("board project items are not a list")
        info = scoped.get("pageInfo")
        if info is None:
            return nodes, None
        if not isinstance(info, dict):
            raise _fail(f"bad board pageInfo {info!r}")
        more = info.get("hasNextPage")
        if not isinstance(more, bool):
            raise _fail(f"bad board pageInfo {info!r}")
        end = info.get("endCursor")
        if end is not None and not isinstance(end, str):
            raise _fail(f"bad board pageInfo {info!r}")
        return nodes, {"hasNextPage": more, "endCursor": end}

    def _node_project(self, node):
        """The board project id behind one item node, else ``TrackerError``."""
        if not isinstance(node, dict):
            raise _fail(f"board item is not an object: {node!r}")
        project = node.get("project")
        project_id = project.get("id") if isinstance(project, dict) else None
        if not isinstance(project_id, str) or not project_id:
            raise _fail(f"bad board item project {project!r}")
        return project_id

    def _node_item(self, by_field, node):
        """``(board_time, texts, selects)`` for one matched item node."""
        self._node_project(node)
        board_time = self._board_time(node)
        return (board_time, *self._node_values(by_field, node))


def is_board_tracker(tracker) -> bool:
    """Whether ``tracker`` runs the board step.

    A board tracker carries a cached schema, a board poll, and an
    integer page cap. The sync and dispatch passes branch on this, never
    on the variant: the factory decides the variant, the tracker's own
    shape decides the step.
    """
    pages = getattr(tracker, "board_poll_pages", None)
    return (
        callable(getattr(tracker, "board_schema", None))
        and callable(getattr(tracker, "poll", None))
        and isinstance(pages, int)
        and not isinstance(pages, bool)
    )


class RepoTracker(NamedTuple):
    """One ``[[repo]]`` entry's production wiring: what the passes need."""

    name: str  # the entry name, which the reports print
    tracker_repo: str  # "owner/name" the adapter talks to
    operators: list  # the entry's trusted logins
    variant: str  # "labels" | "board", as resolved
    tracker: iface.Tracker  # the built tracker


def build_tracker(*, variant, board, max_pages, run=None, pacer=None) -> iface.Tracker:
    """One repo's tracker from validated settings data: the one factory.

    ``variant`` is ``resolved["tracker.variant"]``, ``board`` is the
    resolved ``[repo.board]`` table (or None), ``max_pages`` is the
    resolved ``board.poll_pages``. A missing ``run`` wires the
    production runners (paced REST for label mode, the raw runner for
    board mode); tests pass a fake. Unknown variants and board mode
    without a board table are refused, never run as label mode.
    """
    if variant == "labels":
        return GithubTracker(paced_run if run is None else run)
    if variant == "board":
        if board is None:
            raise ValueError(
                "tracker.variant is 'board' but no [board] table names the project board"
            )
        return BoardBackedTracker(
            board, gh.run if run is None else run, pacer=pacer, max_pages=max_pages
        )
    raise ValueError(f"unknown tracker variant {variant!r} (want one of 'board', 'labels')")


def board_schema_for(board_config, *, run, pacer=None) -> BoardSchema:
    """One board's resolved schema through the injected seam.

    The doctor's fetch: the same construction the factory uses, but only
    the schema is read. Tests pass a fake; production passes the raw runner.
    """
    return BoardReader(board_config, run, pacer=pacer).schema()


def production_trackers(root, names, *, git) -> tuple[dict[str, RepoTracker], str | None]:
    """``({entry name: RepoTracker}, None)`` for one ops root, or ``({}, problem)``.

    Resolves each entry once through the shared ``repo_operators`` and
    the settings resolver, then builds each tracker through the one
    factory. ``names`` are entry names, already defaulted by the caller.
    """
    from corral import settings, syncli

    entries, problem = syncli.repo_operators(root, list(names), git)
    if problem is not None:
        return {}, problem
    mapping: dict[str, RepoTracker] = {}
    for name in names:
        if name not in entries:
            return {}, f"unknown repo {name!r} (not a [[repo]] entry in {root})"
        try:
            resolved = settings.load(root=root, repo=name)
        except settings.SettingsError as err:
            return {}, str(err)
        tracker_repo, operators = entries[name]
        variant = resolved["tracker.variant"]
        try:
            tracker = build_tracker(
                variant=variant,
                board=resolved.board,
                max_pages=resolved["board.poll_pages"],
            )
        except (ValueError, settings.SettingsError) as err:
            return {}, f"[[repo]] {name}: {err}"
        mapping[name] = RepoTracker(
            name=name,
            tracker_repo=tracker_repo,
            operators=list(operators),
            variant=str(variant),
            tracker=tracker,
        )
    return mapping, None
