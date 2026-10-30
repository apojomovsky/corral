"""The board tracker adapter: hosted project boards over GraphQL.

Reads one project's schema (field and option ids) and its items (paged
with the ``after`` cursor), then reads each item's issue through the
REST adapter and overlays the board concepts. Issue parsing stays in
``github.tracker``: this module maps board fields to concepts only.
Nothing outside the two adapters calls ``gh`` or knows a query shape.

A missing or malformed page is a ``TrackerError``, never a partial
result: a cut poll would read absent items as still present. Option ids
resolve live against the fetched schema on every poll; display names
are never matched.
"""

import re
from typing import NamedTuple

from corral import tracker as iface
from corral.github import gh
from corral.github.tracker import GithubTracker

#: One page of board items; the maximum the endpoint serves.
PAGE_SIZE = 100

_SCHEMA_BY_NUMBER = """query BoardSchemaByNumber($owner: String!, $number: Int!) {
  user(login: $owner) { projectV2(number: $number) { ...BoardProject } }
  organization(login: $owner) { projectV2(number: $number) { ...BoardProject } }
}
fragment BoardProject on ProjectV2 {
  id
  fields(first: 100) {
    nodes {
      __typename
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType options { id name } }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
}"""

_SCHEMA_BY_ID = """query BoardSchemaById($id: ID!) {
  node(id: $id) { ... on ProjectV2 { ...BoardProject } }
}
fragment BoardProject on ProjectV2 {
  id
  fields(first: 100) {
    nodes {
      __typename
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType options { id name } }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
}"""

_ITEMS = """query BoardItems($project: ID!, $after: String) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          updatedAt
          content {
            __typename
            ... on Issue { number repository { nameWithOwner } }
            ... on PullRequest { number repository { nameWithOwner } }
          }
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


class FieldDef(NamedTuple):
    """One mapped board field: its node id, its kind, and its options."""

    id: str
    kind: str  # "text" | "single_select"
    options: dict  # option id -> display name, single-select only


class BoardSchema(NamedTuple):
    """A project's resolved board: its node id plus concept fields."""

    project_id: str
    fields: dict  # concept -> FieldDef


#: Board config attribute per concept, in poll order.
_CONCEPT_ATTRS = (
    ("agent", "agent_field"),
    ("status", "status_field"),
    ("board_status", "status_column"),
    ("priority", "priority_column"),
    ("area", "area_column"),
    ("pool", "pool_column"),
    ("blocked_by", "blocked_by_column"),
)

#: Concepts carried as option display names.
_SELECT_CONCEPTS = frozenset({"board_status", "priority", "area", "pool"})

#: Concepts carried as free text.
_TEXT_CONCEPTS = frozenset({"agent", "status", "blocked_by"})

#: One ``owner/repo#N`` reference, matched whole: owner is a login,
#: the repo allows dots and underscores, the number never starts at 0.
_REF_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+#[1-9][0-9]*")


def _fail(text) -> iface.TrackerError:
    return iface.TrackerError(f"unusable board answer: {text}")


def parse_blocked_by(text):
    """A ``blocked_by`` text value as refs, ``[]``, or the raw text.

    Comma-separated ``owner/repo#N`` refs parse strictly to a list; a
    blank value reads as ``[]`` (the cleared encoding). Any unparseable
    entry carries the whole value verbatim as a raw string, so the
    ticket reads as blocked rather than free.
    """
    if not isinstance(text, str):
        raise _fail(f"bad blocked_by {text!r}")
    if not text.strip():
        return []
    refs = []
    for entry in text.split(","):
        ref = entry.strip()
        if _REF_RE.fullmatch(ref) is None:
            return text
        refs.append(ref)
    return refs


class BoardReader:
    """Board items as ``TrackerItem`` readings, through the injected seam."""

    def __init__(self, board, run, pacer=None):
        """``board`` is the resolved ``[board]`` table; ``run(args)``
        returns stdout and raises when ``gh`` fails; ``pacer`` meters the
        shared bucket and defaults to the real one."""
        self._board = board
        self._run = run
        self._pacer = pacer
        self._issues = GithubTracker(run)

    def _graphql(self, query, **variables):
        """One paced GraphQL call as its ``data`` payload, else ``TrackerError``.

        Rate-limit text maps to ``TrackerRateLimited`` through the shared
        classifier, so the pass stops; anything else fails closed.
        """
        try:
            return gh.graphql(query, run=self._run, pacer=self._pacer, **variables)
        except gh.GhError as err:
            text = str(err) or repr(err)
            if gh.is_rate_limited(text):
                raise iface.TrackerRateLimited(text, gh.retry_after_s(text)) from err
            raise iface.TrackerError(f"board unreachable: {text}") from err
        except Exception as err:
            raise iface.TrackerError(f"board unreachable: {err}") from err

    def schema(self) -> BoardSchema:
        """The project's node id, field ids and option ids, live."""
        if self._board.project_id is not None:
            payload = self._graphql(_SCHEMA_BY_ID, id=self._board.project_id)
            if not isinstance(payload, dict):
                raise _fail("schema is not an object")
            return self._parse_schema(payload.get("node"))
        payload = self._graphql(
            _SCHEMA_BY_NUMBER,
            owner=self._board.project_owner,
            number=self._board.project_number,
        )
        if not isinstance(payload, dict):
            raise _fail("schema is not an object")
        user = payload.get("user")
        org = payload.get("organization")
        user_project = user.get("projectV2") if isinstance(user, dict) else None
        org_project = org.get("projectV2") if isinstance(org, dict) else None
        if (user_project is None) == (org_project is None):
            raise _fail("board project is missing or ambiguous")
        return self._parse_schema(user_project if org_project is None else org_project)

    def _parse_schema(self, project) -> BoardSchema:
        """One project object as field ids per concept, else ``TrackerError``."""
        if not isinstance(project, dict):
            raise _fail(f"board project is not an object: {project!r}")
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise _fail(f"bad board project id {project_id!r}")
        fields = project.get("fields")
        nodes = fields.get("nodes") if isinstance(fields, dict) else None
        if not isinstance(nodes, list):
            raise _fail("board fields are not a list")
        by_name = {}
        for node in nodes:
            if not isinstance(node, dict):
                raise _fail(f"bad board field {node!r}")
            fid = node.get("id")
            name = node.get("name")
            if not isinstance(fid, str) or not fid:
                raise _fail(f"bad board field {node!r}")
            if not isinstance(name, str) or not name:
                raise _fail(f"bad board field {node!r}")
            if name in by_name:
                raise _fail(f"duplicate board field {name!r}")
            by_name[name] = node
        found = {}
        for concept, attr in _CONCEPT_ATTRS:
            name = getattr(self._board, attr)
            if not name:
                continue
            node = by_name.get(name)
            if node is None:
                raise _fail(f"board field {name!r} for {concept} is missing")
            found[concept] = self._parse_field(concept, name, node)
        return BoardSchema(project_id=project_id, fields=found)

    def _parse_field(self, concept, name, node) -> FieldDef:
        """One mapped schema node as a ``FieldDef``, else ``TrackerError``."""
        typename = node.get("__typename")
        datatype = node.get("dataType")
        if typename == "ProjectV2SingleSelectField" and datatype == "SINGLE_SELECT":
            kind = "single_select"
        elif typename == "ProjectV2Field" and datatype == "TEXT":
            kind = "text"
        else:
            kind = "other"
        want = "single_select" if concept in _SELECT_CONCEPTS else "text"
        if kind != want:
            raise _fail(f"board field {name!r} for {concept} has the wrong kind")
        options = {}
        if kind == "single_select":
            entries = node.get("options")
            if not isinstance(entries, list):
                raise _fail(f"board field {name!r} has no options")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _fail(f"bad board option {entry!r} in {name!r}")
                oid = entry.get("id")
                oname = entry.get("name")
                if not isinstance(oid, str) or not oid:
                    raise _fail(f"bad board option {entry!r} in {name!r}")
                if not isinstance(oname, str):
                    raise _fail(f"bad board option {entry!r} in {name!r}")
                options[oid] = oname
        return FieldDef(id=node["id"], kind=kind, options=options)

    def poll(self, schema: BoardSchema, *, after=None, max_pages) -> tuple[list, str | None]:
        """Board items oldest first, plus the paging resume cursor.

        Pages with ``after`` until a short page or an empty ``pageInfo``
        ends it; at most ``max_pages`` pages per call. A malformed page
        raises ``TrackerError`` and returns nothing, never a partial
        result. A call stopped by ``max_pages`` returns what it fetched
        with the cursor to resume from; a call that reaches the end
        returns ``None`` so the next pass starts over from the head.
        """
        if max_pages < 1:
            raise ValueError(f"max_pages must be at least 1, got {max_pages!r}")
        by_field = {field.id: concept for concept, field in schema.fields.items()}
        items = []
        fetch_after = after
        for _ in range(max_pages):
            nodes, info = self._page(schema.project_id, fetch_after)
            for node in nodes:
                found = self._read_node(by_field, node)
                if found is not None:
                    items.append(found)
            end, more = self._resume(info, len(nodes))
            if not more:
                return items, None
            fetch_after = end
        return items, fetch_after

    def _page(self, project_id, after):
        """One items page as ``(nodes, pageInfo)``, else ``TrackerError``."""
        if after is None:
            payload = self._graphql(_ITEMS, project=project_id)
        else:
            payload = self._graphql(_ITEMS, project=project_id, after=after)
        if not isinstance(payload, dict):
            raise _fail("board items are not an object")
        node = payload.get("node")
        if not isinstance(node, dict):
            raise _fail("board project is missing")
        items = node.get("items")
        if not isinstance(items, dict):
            raise _fail("board items are missing")
        nodes = items.get("nodes")
        if not isinstance(nodes, list):
            raise _fail("board items are not a list")
        info = items.get("pageInfo")
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

    def _resume(self, info, count):
        """``(endCursor, continue)`` for one fetched page."""
        if info is None or not info["hasNextPage"]:
            end = info.get("endCursor") if info else None
            return (end if end else None), False
        if count == 0:
            raise _fail("board page claims more items but holds none")
        if count < PAGE_SIZE:
            end = info.get("endCursor")
            return (end if end else None), False
        end = info.get("endCursor")
        if not end:
            raise _fail("board page claims more items but names no cursor")
        return end, True

    def _read_node(self, by_field, node):
        """One board node as a ``TrackerItem``, ``None`` for a draft.

        Anything short of the full shape is a ``TrackerError``; a draft
        item (no issue behind it) is skipped, since it can never be a row.
        """
        if not isinstance(node, dict):
            raise _fail(f"board item is not an object: {node!r}")
        try:
            board_time = iface.parse_cursor(node.get("updatedAt"))
        except iface.TrackerError:
            raise _fail(f"bad board updatedAt {node.get('updatedAt')!r}") from None
        content = node.get("content", {})
        if content is None:
            raise _fail("board item content is missing")
        if not isinstance(content, dict):
            raise _fail(f"bad board content {content!r}")
        if content.get("__typename") == "DraftIssue" and "number" not in content:
            return None
        number = content.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _fail(f"bad board content number {number!r}")
        repo = content.get("repository")
        repo = repo.get("nameWithOwner") if isinstance(repo, dict) else None
        if not isinstance(repo, str) or "/" not in repo:
            raise _fail(f"bad board content repo {repo!r}")
        values = node.get("fieldValues")
        entries = values.get("nodes") if isinstance(values, dict) else None
        if not isinstance(entries, list):
            raise _fail("board field values are not a list")
        texts = {}
        selects = {}
        for entry in entries:
            concept, kind, value = self._read_value(by_field, entry)
            if concept is None:
                continue
            if kind == "text":
                texts[concept] = value
            else:
                selects[concept] = value
        item, milestone = self._issues.read_issue(repo, number)
        extra = self._concepts(selects, texts, milestone)
        return item._replace(
            updated_at=max(board_time, item.updated_at),
            agent=texts.get("agent", ""),
            status=texts.get("status", ""),
            extra=extra,
        )

    def _read_value(self, by_field, entry):
        """One field-value node as ``(concept, kind, value)``.

        ``(None, None, None)`` for a field no concept maps; an unset
        single-select reads as absent. A value of the wrong kind for a
        mapped concept is a ``TrackerError``.
        """
        if not isinstance(entry, dict):
            raise _fail(f"bad board value {entry!r}")
        field = entry.get("field")
        fid = field.get("id") if isinstance(field, dict) else None
        if not isinstance(fid, str) or not fid:
            raise _fail(f"bad board value field {field!r}")
        concept = by_field.get(fid)
        if concept is None:
            return None, None, None
        if concept in _TEXT_CONCEPTS:
            if "text" not in entry:
                raise _fail(f"board value for {concept} is not text")
            text = entry["text"]
            if text is None:
                text = ""
            if not isinstance(text, str):
                raise _fail(f"bad board value for {concept}: {text!r}")
            return concept, "text", text.strip()
        oid = entry.get("optionId")
        if oid is None:
            if "text" in entry:
                raise _fail(f"board value for {concept} is not a select")
            return None, None, None
        if not isinstance(oid, str) or not oid:
            raise _fail(f"bad board value for {concept}: {oid!r}")
        name = entry.get("name")
        if not isinstance(name, str):
            raise _fail(f"bad board value for {concept}: {name!r}")
        return concept, "select", (oid, name)

    def _concepts(self, selects, texts, milestone):
        """Board values plus the milestone as the reading's ``extra``.

        Option ids resolve through the config's ``priority_map`` for
        priority and through display names for the other columns; an
        option id the map no longer names leaves the concept absent, so
        the merge keeps the stored value.
        """
        extra = {}
        if "board_status" in selects:
            extra["board_status"] = selects["board_status"][1]
        if "priority" in selects:
            rank = self._board.priority_map.get(selects["priority"][0])
            if rank:
                extra["priority"] = rank
        if "area" in selects:
            extra["area"] = selects["area"][1]
        if "pool" in selects:
            extra["pool"] = selects["pool"][1]
        if "blocked_by" in texts:
            extra["blocked_by"] = parse_blocked_by(texts["blocked_by"])
        extra["milestone"] = milestone
        return extra
