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

import json
import re
from typing import NamedTuple

from korapy import tracker as iface
from korapy.github import gh
from korapy.github.tracker import GithubTracker

#: One page of board items; the maximum the endpoint serves.
PAGE_SIZE = 100

_SCHEMA_BY_NUMBER = """query BoardSchemaByNumber($owner: String!, $number: Int!) {
  user(login: $owner) { projectV2(number: $number) { ...BoardProject } }
  organization(login: $owner) { projectV2(number: $number) { ...BoardProject } }
}
fragment BoardProject on ProjectV2 {
  id
  title
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
  title
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


#: Board config attribute per concept, in poll order. The one mapping
#: lives in ``korapy.tracker`` beside the planner that shares it; this
#: module only names the reader's kind contract below.
_CONCEPT_ATTRS = iface.BOARD_CONCEPT_ATTRS

#: Concepts carried as option display names. Every other board concept
#: is carried as free text: this one set owns the text-versus-select
#: fact, and both the read and provision paths resolve it through
#: ``_want_kind`` below.
_SELECT_CONCEPTS = frozenset({"board_status", "priority", "area", "pool"})


def _want_kind(concept) -> str:
    """``"single_select"`` or ``"text"`` for one board concept."""
    return "single_select" if concept in _SELECT_CONCEPTS else "text"


def _field_kind(node) -> str:
    """``"text"``, ``"single_select"`` or ``"other"`` for one schema node.

    The one mapping from the provider's ``(__typename, dataType)`` shape
    to a kind: the read path (``_parse_field``) and the provision path
    (``_live_fields``) both call this, so the contract cannot fork.
    """
    if (
        node.get("__typename") == "ProjectV2SingleSelectField"
        and node.get("dataType") == "SINGLE_SELECT"
    ):
        return "single_select"
    if node.get("__typename") == "ProjectV2Field" and node.get("dataType") == "TEXT":
        return "text"
    return "other"


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

    def _graphql_body(self, query, **variables):
        """One paced two-alias read as its tolerant ``data`` payload.

        The owner queries ask one login as both halves; the losing half's
        ``NOT_FOUND`` noise rides beside the winning half's data, which
        ``gh.graphql`` refuses to return, so this goes through the one
        two-alias helper instead. Rate limits map like any other read.
        """
        return _two_alias_data(query, variables, self._run, self._pacer)

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
        return self._parse_schema(self._fetch_project())

    def snapshot(self) -> iface.BoardSnapshot:
        """The board's read-only shape for the doctor, as plain data.

        The live title, every live field raw (duplicates kept, so the
        comparison refuses the ambiguity instead of this picking one),
        and every board under the same owner. Malformed answers fail
        closed with ``TrackerError``; rate limits surface as
        ``TrackerRateLimited`` through the shared classifier, like any
        other read.
        """
        project = self._fetch_project()
        _project_id(project)
        title = project.get("title")
        if not isinstance(title, str) or not title:
            raise _fail("board title is missing")
        fields = project.get("fields")
        nodes = fields.get("nodes") if isinstance(fields, dict) else None
        if not isinstance(nodes, list):
            raise _fail("board fields are not a list")
        found = [_live_field(node) for node in nodes]
        owner_boards = self._owner_boards(self._board.project_owner)
        return iface.BoardSnapshot(title=title, fields=tuple(found), owner_boards=owner_boards)

    def issue_refs(self) -> set[tuple[str, int]]:
        """``(repo, number)`` on the board, paged, else ``TrackerError``.

        Membership only: no field value is read and no issue is fetched
        over REST, so the doctor diffs board presence against the
        store's open rows without a full poll. A malformed page fails
        closed, never a partial set.
        """
        project_id = _project_id(self._fetch_project())
        refs = set()
        after = None
        while True:
            nodes, info = self._page(project_id, after)
            for node in nodes:
                ref = _content_ref(node)
                if ref is not None:
                    refs.add(ref)
            end, more = self._resume(info, len(nodes))
            if not more:
                return refs
            after = end

    def _owner_boards(self, login) -> tuple:
        """Every board under ``login`` as ``OwnerBoard``, paged.

        The same title list provisioning searches, read through the
        one page validation, so the two cannot fork.
        """
        boards = []
        after = None
        while True:
            if after is None:
                payload = self._graphql_body(_TITLES, login=login)
            else:
                payload = self._graphql_body(_TITLES, login=login, after=after)
            entries, info = _titles_entries(payload)
            boards.extend(
                iface.OwnerBoard(number=number, title=title) for _, number, title in entries
            )
            after, more = _titles_next(info)
            if not more:
                return tuple(boards)

    def _fetch_project(self):
        """The live board project object, else ``TrackerError``."""
        if self._board.project_id is not None:
            payload = self._graphql(_SCHEMA_BY_ID, id=self._board.project_id)
            if not isinstance(payload, dict):
                raise _fail("schema is not an object")
            return payload.get("node")
        payload = self._graphql_body(
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
        return user_project if org_project is None else org_project

    def _project_index(self, project):
        """``(project id, fields by name)`` for one project object."""
        project_id = _project_id(project)
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
        return project_id, by_name

    def _parse_schema(self, project) -> BoardSchema:
        """One project object as field ids per concept, else ``TrackerError``."""
        project_id, by_name = self._project_index(project)
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
        kind = _field_kind(node)
        want = _want_kind(concept)
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

    def _board_time(self, node):
        """One board node's ``updatedAt`` as epoch seconds, else ``TrackerError``."""
        try:
            return iface.parse_cursor(node.get("updatedAt"))
        except iface.TrackerError:
            raise _fail(f"bad board updatedAt {node.get('updatedAt')!r}") from None

    def _node_values(self, by_field, node):
        """``(texts, selects)`` for one board node's field values, strict.

        Shared by the poll path and the targeted single-item read: every
        entry is validated through ``_read_value``, so anything short of
        the value shape fails closed on both paths.
        """
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
        return texts, selects

    def _read_node(self, by_field, node):
        """One board node as a ``TrackerItem``, ``None`` for a draft.

        Anything short of the full shape is a ``TrackerError``; a draft
        item (no issue behind it) is skipped, since it can never be a row.
        """
        if not isinstance(node, dict):
            raise _fail(f"board item is not an object: {node!r}")
        board_time = self._board_time(node)
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
        texts, selects = self._node_values(by_field, node)
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
        if _want_kind(concept) == "text":
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


#: Korapy-owned concepts writable on the board, with their config attribute.
_WRITE_ATTRS = (("agent", "agent_field"), ("status", "status_field"))

_WRITE_TEXT = """mutation BoardWriteText($project: ID!, $item: ID!, $field: ID!, $text: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field, value: {text: $text}}) {
    projectV2Item { id }
  }
}"""

_CLEAR_TEXT = """mutation BoardClearField($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field}) {
    projectV2Item { id }
  }
}"""


class BoardTracker(BoardReader):
    """Korapy-owned writes as board text-field mutations, over the same seam.

    Only ``agent`` and ``status`` are writable, each through the one
    configured text field for the concept; any other field is refused with
    ``ValueError``. A write whose concept the config never mapped raises
    ``UnknownOption`` (the mirror drops it); a write whose field is missing
    from the live board raises ``TrackerError`` instead, so the rows stay
    queued for the next pass. A write that changes nothing sends no
    mutation. Reads stay with ``BoardReader`` and the board sync step.

    One adapter instance covers one mirror pass: the schema is fetched once
    and each item's node id is resolved once and reused across that item's
    fields, so two fields on one item cost one schema fetch plus one scan.
    Item scans stop at ``max_pages`` pages; the caches live only as long as
    the instance, so a later pass re-reads the board.
    """

    def __init__(self, board, run, pacer=None, *, max_pages):
        """``max_pages`` bounds every item scan, like the poll page cap.

        The schema and the located items are cached on the instance: one
        adapter per mirror pass means one fetch and one scan per item.
        """
        super().__init__(board, run, pacer)
        if max_pages < 1:
            raise ValueError(f"max_pages must be at least 1, got {max_pages!r}")
        self._max_pages = max_pages
        self._schema = None
        self._located = {}

    def _cached_schema(self):
        """``(project id, fields by name)``, fetched once per instance."""
        if self._schema is None:
            self._schema = self._project_index(self._fetch_project())
        return self._schema

    def write(self, repo: str, number: int, field: str, value: str) -> None:
        """Set one korapy-owned concept through its board text field.

        Resolves the concept to its live field id, locates the board item
        behind ``repo#number``, and sends one text mutation (the clear
        mutation for ``""``). An already-matching board sends nothing;
        anything outside the korapy-owned fields is refused with
        ``ValueError``. Rate-limit answers raise ``TrackerRateLimited``
        through the shared classifier. Values are compared, set and cleared
        in stripped canonical form, like both read paths; a whitespace-only
        value clears.
        """
        if not isinstance(value, str):
            raise iface.TrackerError(f"bad board value for {field!r}: {value!r}")
        value = value.strip()
        try:
            attr = dict(_WRITE_ATTRS)[field]
        except (KeyError, TypeError):
            raise ValueError(f"refusing to write non-korapy field {field!r}") from None
        name = getattr(self._board, attr)
        if not name:
            raise iface.UnknownOption(f"board maps no field for {field!r}")
        project_id, by_name = self._cached_schema()
        node = by_name.get(name)
        if node is None:
            raise iface.TrackerError(f"board field {name!r} for {field} is missing")
        field_id = self._parse_field(field, name, node).id
        item_id, texts, selects = self._locate(project_id, repo, number)
        if field_id in selects:
            raise _fail("board value for a text field is not text")
        if texts.get(field_id, "") == value:
            return
        if value:
            self._graphql(_WRITE_TEXT, project=project_id, item=item_id, field=field_id, text=value)
        else:
            self._graphql(_CLEAR_TEXT, project=project_id, item=item_id, field=field_id)
        texts[field_id] = value

    def _locate(self, project_id, repo, number):
        """``(item node id, texts, selects)`` for one issue.

        Resolved once per item and cached on the instance, so a second
        field on the same item reuses the one scan. ``texts`` maps a field
        id to its stripped text; ``selects`` holds the field ids with a
        select value, so the write refuses a select behind a text field
        id. An unset field is in neither: it reads as ``""``.
        """
        key = (repo, number)
        hit = self._located.get(key)
        if hit is None:
            hit = self._scan(project_id, repo, number)
            self._located[key] = hit
        return hit

    def _scan(self, project_id, repo, number):
        """One uncached item scan: at most ``max_pages`` pages, else ``TrackerError``.

        A missing or malformed page is a ``TrackerError``, never a partial
        search; an item the board does not hold, or one past the page cap,
        is a ``TrackerError`` too, so the write stays queued.
        """
        after = None
        for _ in range(self._max_pages):
            nodes, info = self._page(project_id, after)
            for node in nodes:
                found = self._locate_node(node, repo, number)
                if found is not None:
                    return found
            end, more = self._resume(info, len(nodes))
            if not more:
                raise iface.TrackerError(f"unknown board item {repo}#{number}")
            after = end
        raise iface.TrackerError(
            f"board item {repo}#{number} not found within {self._max_pages} pages"
        )

    def _locate_node(self, node, repo, number):
        """``(item id, texts, selects)`` for the wanted issue, else None.

        Every node is validated like a read: anything short of the full
        shape is a ``TrackerError``. A draft item (no issue behind it)
        never matches.
        """
        if not isinstance(node, dict):
            raise _fail(f"board item is not an object: {node!r}")
        item_id = node.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise _fail(f"bad board item id {item_id!r}")
        content = node.get("content", {})
        if content is None:
            raise _fail("board item content is missing")
        if not isinstance(content, dict):
            raise _fail(f"bad board content {content!r}")
        if content.get("__typename") == "DraftIssue" and "number" not in content:
            return None
        got_number = content.get("number")
        if isinstance(got_number, bool) or not isinstance(got_number, int) or got_number < 1:
            raise _fail(f"bad board content number {got_number!r}")
        got_repo = content.get("repository")
        got_repo = got_repo.get("nameWithOwner") if isinstance(got_repo, dict) else None
        if not isinstance(got_repo, str) or "/" not in got_repo:
            raise _fail(f"bad board content repo {got_repo!r}")
        values = node.get("fieldValues")
        entries = values.get("nodes") if isinstance(values, dict) else None
        if not isinstance(entries, list):
            raise _fail("board field values are not a list")
        texts: dict = {}
        selects = set()
        for entry in entries:
            fid, text = self._locate_value(entry)
            if text is None:
                selects.add(fid)
            else:
                texts[fid] = text
        if got_repo != repo or got_number != number:
            return None
        return item_id, texts, selects

    def _locate_value(self, entry):
        """``(field id, stripped text or None)`` for one field-value node.

        A select value reads as ``None``; an unset value reads as ``""``.
        Anything short of the value shape is a ``TrackerError``.
        """
        if not isinstance(entry, dict):
            raise _fail(f"bad board value {entry!r}")
        field = entry.get("field")
        fid = field.get("id") if isinstance(field, dict) else None
        if not isinstance(fid, str) or not fid:
            raise _fail(f"bad board value field {field!r}")
        if "text" in entry:
            text = entry["text"]
            if text is None:
                return fid, ""
            if not isinstance(text, str):
                raise _fail(f"bad board value text {text!r}")
            return fid, text.strip()
        if entry.get("optionId") is not None:
            return fid, None
        return fid, ""

    def adopt_missing(self, project_id, repo, present, *, limit, now):
        """Add the repo's missing open and recently closed issues, up to ``limit``.

        ``present`` holds the ``(repo, number)`` already on the board,
        from the step's own poll; candidates come from the tracker
        listing with pull requests and long-closed issues filtered out.
        The adds run through the one shared adder setup uses. A listing
        that falls over raises ``TrackerError`` instead of adopting on
        partial knowledge; a rate limit halts the run with the rest left
        for the next pass. A non-positive limit adopts nothing and
        lists nothing.
        """
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("board adopt needs a project id")
        if limit is not None and limit <= 0:
            return iface.BoardAdopt((), (), (), None)
        candidates = [
            ((repo, number), node_id, f"{repo}#{number}")
            for node_id, number in self._adoptable_issue_refs(repo, now)
        ]
        return add_missing_issues(
            self._run,
            self._pacer,
            project_id=project_id,
            present=set(present),
            candidates=candidates,
            limit=limit,
        )

    def _adoptable_issue_refs(self, repo, now):
        """``(node id, number)`` for the repo's open and recently closed issues.

        Pull requests are never board items this tracker creates, and
        closed issues older than ``ADOPT_RECENT_DAYS`` stay history for
        people to add by hand. The page-walk is the shared listing
        helper below; an unexpected state fails the whole adopt, since
        the pass must prove membership before it adds.
        """
        for entry in _list_issue_entries(self._run, self._pacer, repo, "all"):
            if "pull_request" in entry:
                continue
            state = entry.get("state")
            if state not in ("open", "closed"):
                raise _fail(f"bad issue state {state!r} on {repo}")
            node_id, number = _issue_ref(entry, repo)
            if state == "closed" and not _recently_closed(entry.get("closed_at"), now):
                continue
            yield node_id, number


#: How long a closed issue still counts as adoptable: older closed
#: issues stay history for people to add by hand.
ADOPT_RECENT_DAYS = 3


def _list_issue_entries(run, pacer, repo, state):
    """Each raw entry of the tracker's paged issue listing, in order.

    The one page-walk every issue-listing reader shares: ``state`` is
    the listing's own ``state`` parameter. A listing that falls over
    raises ``TrackerError`` instead of yielding partial knowledge, and
    a rate-limit answer raises ``TrackerRateLimited``; a malformed
    page fails closed, never as a partial entry set. Callers classify
    each entry's own state themselves: the pass adopt fails a page
    carrying an unexpected state (it must prove membership before it
    adds), while the open-only listing skips anything but open (it
    asked for open only).
    """
    base = f"repos/{repo}/issues?state={state}&per_page=100"
    page = 1
    while True:
        path = base if page == 1 else f"{base}&page={page}"
        try:
            payload = json.loads(gh.paced_run(["api", path], run=run, pacer=pacer))
        except gh.GhError as err:
            text = str(err) or repr(err)
            if gh.is_rate_limited(text):
                raise iface.TrackerRateLimited(text, gh.retry_after_s(text)) from err
            raise iface.TrackerError(f"board unreachable: {text}") from err
        except Exception as err:
            raise iface.TrackerError(f"board unreachable: {err}") from err
        if not isinstance(payload, list):
            raise _fail(f"issues of {repo} are not a list")
        for entry in payload:
            if not isinstance(entry, dict):
                raise _fail(f"bad issue {entry!r} on {repo}")
            yield entry
        if len(payload) < 100:
            return
        page += 1


def _issue_ref(entry, repo):
    """``(node id, number)`` for one listing entry, else ``TrackerError``."""
    number = entry.get("number")
    node_id = entry.get("node_id")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise _fail(f"bad issue {entry!r} on {repo}")
    if not isinstance(node_id, str) or not node_id:
        raise _fail(f"bad issue {entry!r} on {repo}")
    return node_id, number


def _recently_closed(closed_at, now) -> bool:
    """Whether a closed issue is still adoptable: closed within the window.

    A missing or malformed closed time fails closed, never as adoptable.
    """
    if not isinstance(closed_at, str) or not closed_at:
        raise _fail(f"bad closed time {closed_at!r}")
    try:
        closed = iface.parse_cursor(closed_at)
    except iface.TrackerError:
        raise _fail(f"bad closed time {closed_at!r}") from None
    return now - closed <= ADOPT_RECENT_DAYS * 86400


#: The owner lookup: one query asking for the login as both halves, so
#: the adapter learns the owner's node id and user-or-organization kind
#: without a second round trip. Exactly one half resolves; the other
#: answers a NOT_FOUND error beside the data, which the runner reports
#: with a non-zero exit while still printing the body on stdout.
_OWNER_QUERY = """query ProvisionOwnerResolve($login: String!) {
  user(login: $login) { id }
  organization(login: $login) { id }
}"""

#: Everything one ensure step needs from a project: identity, title and
#: visibility, the linked repositories, and the schema with every
#: option's id, color and description (the append resends the whole
#: list, so a partial option shape would silently reset the rest).
_PROJECT_FRAGMENT = """fragment ProvisionBoardProject on ProjectV2 {
  id number title public
  repositories(first: 100) { nodes { nameWithOwner } }
  fields(first: 100) {
    nodes {
      __typename
      ... on ProjectV2Field { id name dataType }
      ... on ProjectV2SingleSelectField { id name dataType options { id name color description } }
      ... on ProjectV2IterationField { id name dataType }
    }
  }
}"""

_BY_NUMBER = (
    """query ProvisionBoardByNumber($owner: String!, $number: Int!) {
  user(login: $owner) { projectV2(number: $number) { ...ProvisionBoardProject } }
  organization(login: $owner) { projectV2(number: $number) { ...ProvisionBoardProject } }
}
"""
    + _PROJECT_FRAGMENT
)

_BY_ID = (
    """query ProvisionBoardById($id: ID!) {
  node(id: $id) { ... on ProjectV2 { ...ProvisionBoardProject } }
}
"""
    + _PROJECT_FRAGMENT
)

#: The title search: one query asking for the login as both halves, the
#: way ``_BY_NUMBER`` does. Exactly one half resolves; the other answers
#: null (with the expected NOT_FOUND noise, which the one two-alias
#: helper tolerates). The caller picks the set half from the body.
_TITLES = """query ProvisionBoardTitles($login: String!, $after: String) {
  user(login: $login) {
    projectsV2(first: 100, after: $after) {
      nodes { id number title }
      pageInfo { hasNextPage endCursor }
    }
  }
  organization(login: $login) {
    projectsV2(first: 100, after: $after) {
      nodes { id number title }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def _titles_entries(data):
    """``(entries, pageInfo)`` for one board-title page, else ``TrackerError``.

    Shared by the provisioner's title search and the doctor's snapshot:
    one validation of the owner-halves shape, so the two cannot fork.
    Entries are ``(node id, number, title)`` in listed order.
    """
    if not isinstance(data, dict):
        raise _fail("board title list is missing")
    halves = [
        half for half in (data.get("user"), data.get("organization")) if isinstance(half, dict)
    ]
    if not halves:
        raise _fail("board title list is missing")
    if len(halves) > 1:
        raise _fail("board title list is ambiguous")
    connection = halves[0].get("projectsV2")
    if not isinstance(connection, dict):
        raise _fail("board title list is missing")
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise _fail("board titles are not a list")
    entries = []
    for entry in nodes:
        if not isinstance(entry, dict):
            raise _fail(f"bad board title {entry!r}")
        eid = entry.get("id")
        number = entry.get("number")
        title = entry.get("title")
        if not isinstance(eid, str) or not eid:
            raise _fail(f"bad board title {entry!r}")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _fail(f"bad board title {entry!r}")
        if not isinstance(title, str) or not title:
            raise _fail(f"bad board title {entry!r}")
        entries.append((eid, number, title))
    info = connection.get("pageInfo")
    if info is None:
        raise _fail("board title list is missing its pageInfo")
    if not isinstance(info, dict):
        raise _fail(f"bad board pageInfo {info!r}")
    return entries, info


def _titles_next(info):
    """``(cursor, True)`` to continue a title list, ``(None, False)`` at its end."""
    more = info.get("hasNextPage")
    if not isinstance(more, bool):
        raise _fail(f"bad board pageInfo {info!r}")
    if not more:
        return None, False
    end = info.get("endCursor")
    if not isinstance(end, str) or not end:
        raise _fail("board title list claims more boards but names no cursor")
    return end, True


_CREATE = """mutation ProvisionBoardCreate($owner: ID!, $title: String!, $repo: ID) {
  createProjectV2(input: {ownerId: $owner, title: $title, repositoryId: $repo}) {
    projectV2 { id number title public }
  }
}"""

_MAKE_PRIVATE = """mutation ProvisionBoardMakePrivate($project: ID!) {
  updateProjectV2(input: {projectId: $project, public: false}) {
    projectV2 { id public }
  }
}"""

_LINK = """mutation ProvisionBoardLinkRepo($project: ID!, $repo: ID!) {
  linkProjectV2ToRepository(input: {projectId: $project, repositoryId: $repo}) {
    repository { id nameWithOwner }
  }
}"""

_TEXT_FIELD_CREATE = """mutation ProvisionTextFieldCreate($project: ID!, $name: String!) {
  createProjectV2Field(input: {projectId: $project, dataType: TEXT, name: $name}) {
    projectV2Field { id name }
  }
}"""

_SELECT_FIELD_CREATE = """mutation ProvisionSelectFieldCreate($project: ID!, $name: String!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  createProjectV2Field(input: {projectId: $project, dataType: SINGLE_SELECT, name: $name, singleSelectOptions: $options}) {
    projectV2Field { id name }
  }
}"""

_OPTIONS_APPEND = """mutation ProvisionOptionsAppend($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
    projectV2Field { id name }
  }
}"""

#: Everything one populate step needs from a project: the item ids with
#: the content behind each item. Draft items carry no content and read
#: as skipped, never as failures.
_PROVISION_ITEMS = """query ProvisionBoardItems($project: ID!, $after: String) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        nodes {
          id
          content { __typename ... on Issue { id number } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

_ADD_ITEM = """mutation ProvisionBoardAddItem($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}"""

#: How many times one issue add is attempted before the run reports it:
#: the first answer after a link or create can be a temporary move
#: conflict, and retrying converges without ever refusing a transient.
_ADD_ATTEMPTS = 5

#: The fix line for a spec addressing a board that no longer resolves.
_REMOVE_TABLE_LINE = "the board in the config no longer exists: remove the table and re-run"


def _project_id(project) -> str:
    """One project object's node id, else ``TrackerError``.

    The light half of ``BoardReader._project_index`` for reads that
    need no field mapping: the snapshot and the membership listing
    resolve fields themselves, so a duplicate field name must not fail
    them here.
    """
    if not isinstance(project, dict):
        raise _fail(f"board project is not an object: {project!r}")
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise _fail(f"bad board project id {project_id!r}")
    return project_id


def _live_field(node) -> iface.LiveField:
    """One schema node as raw doctor data, duplicates kept, else ``TrackerError``.

    Malformed nodes and options fail closed like any other unusable
    answer; two fields under one name are both listed, and the
    comparison refuses the ambiguity instead of this picking one.
    """
    if not isinstance(node, dict):
        raise _fail(f"bad board field {node!r}")
    fid = node.get("id")
    name = node.get("name")
    if not isinstance(fid, str) or not fid:
        raise _fail(f"bad board field {node!r}")
    if not isinstance(name, str) or not name:
        raise _fail(f"bad board field {node!r}")
    kind = _field_kind(node)
    options: list = []
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
            options.append(iface.LiveOption(id=oid, name=oname))
    return iface.LiveField(name=name, kind=kind, options=tuple(options))


def _content_ref(node):
    """``(repo, number)`` for one board item node, ``None`` for a gone item.

    A null content (an item whose issue is gone) reads as absent, like
    the populate membership does; a draft without a number is skipped
    the way the poll skips it. Anything short of that shape is a
    ``TrackerError``, never a partial membership set.
    """
    if not isinstance(node, dict):
        raise _fail(f"board item is not an object: {node!r}")
    content = node.get("content", {})
    if content is None:
        return None
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
    return repo, number


def _link_fix(repo) -> str:
    """The fix line when the account may not link ``repo`` to the board."""
    return (
        f"could not link {repo} to the board: the account may not see or administer "
        "that repository; link it on the board by hand, then re-run"
    )


def _option_fix(name, field_name) -> str:
    """The fix line when a live option cannot be resent in full."""
    return f"add the {name!r} option to field {field_name!r} on the board by hand, then re-run"


def _public_fix_new(title) -> str:
    """The fix line when a just-created board stays public after correction."""
    return (
        f"new board {title!r} is public but the spec asks for private: "
        "make it private by hand, then re-run"
    )


def _public_fix_keep(title) -> str:
    """The fix line when an existing board is public but private was specced."""
    return f"board {title!r} is public but the spec asks for private: make it private by hand"


def _transient_add_failure(text) -> bool:
    """Whether a failed issue add reads as transient and merits a retry.

    The move conflict right after a link or create names itself, as
    does the generic execution failure the spike observed. Anything
    else fails into the per-issue refusal, never into a retry loop.
    """
    lowered = text.lower()
    return "temporary conflict" in lowered or "request id" in lowered


class _ItemRefused(Exception):
    """One issue add the run reports instead of failing: carries the fix line.

    Caught per issue by ``populate``; anything else propagates like any
    other adapter failure.
    """


#: The color of an option the provisioner creates. New options carry no
#: meaning, so they share one neutral color; people recolor by hand.
_NEW_OPTION_COLOR = "GRAY"


def _response_body(err):
    """A GraphQL response body surviving on a failed ``run``, else None.

    The runner prints the body on stdout even when GraphQL ``errors``
    entries make it exit non-zero, and the seam carries that output on
    the error; a seam double may also carry the body as the message.
    Anything else is not a body.
    """
    output = getattr(err, "output", "") or ""
    if output:
        try:
            parsed = json.loads(output)
        except ValueError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed
    try:
        parsed = json.loads(str(err) or "")
    except ValueError:
        return None
    if isinstance(parsed, dict) and "data" in parsed:
        return parsed
    return None


def _missing_only(text) -> bool:
    """Whether a failure is only NOT_FOUND entries, never a resolve.

    Seam doubles that raise instead of returning a body still name an
    unresolvable owner or board this way; anything broader fails closed.
    """
    try:
        parsed = json.loads(text or "")
    except ValueError:
        return False
    if not isinstance(parsed, list) or not parsed:
        return False
    return all(isinstance(entry, dict) and entry.get("type") == "NOT_FOUND" for entry in parsed)


def _errors_rate_limited(errors) -> bool:
    """Whether a GraphQL body's errors answer a rate limit.

    Structured first, like ``gh.graphql``: a RATE_LIMITED entry is rate
    limited even when its message names something else.
    """
    if not isinstance(errors, list):
        return False
    if any(isinstance(entry, dict) and entry.get("type") == "RATE_LIMITED" for entry in errors):
        return True
    return gh.is_rate_limited(json.dumps(errors))


class _BoardForbidden(iface.TrackerError):
    """A GraphQL answer refused the call: no permission, not unreachable.

    A subclass of ``TrackerError`` so paths that do not distinguish it
    still fail closed with exit code 3. The link step catches it and
    reports the refusal with its fix line instead.
    """


def _two_alias_data(query, variables, run, pacer):
    """The ``data`` of a two-alias owner query, tolerating the losing half.

    One paced read through the raw ``run`` seam (``gh.graphql_args``):
    ``gh api graphql`` exits non-zero on any GraphQL ``errors`` entry even
    beside partial ``data``, which ``gh.graphql`` refuses to return, so the
    queries that ask one login as both halves read the body here instead.
    A ``NOT_FOUND`` entry whose path is exactly the half that resolved to
    null is the expected noise and is tolerated; a rate-limit entry (or
    rate-limit wording) starts the shared cooldown and raises
    ``TrackerRateLimited``; any other entry raises ``TrackerError``. A body
    whose data resolves nothing is returned as-is: the caller decides a
    missing board from the data, with its own message.
    """
    active = pacer if pacer is not None else gh._pacer()
    try:
        active.acquire(cost=gh.GRAPHQL_CALL_COST)
        out = run(gh.graphql_args(query, variables), paced=True)
    except gh.GhError as err:
        text = str(err) or repr(err)
        if err.kind == "unavailable" or gh.is_rate_limited(text):
            active.cooldown(gh.retry_after_s(text))
            raise iface.TrackerRateLimited(text, gh.retry_after_s(text)) from err
        body = _response_body(err)
        if body is None:
            raise iface.TrackerError(f"board unreachable: {text}") from err
    except Exception as err:
        raise iface.TrackerError(f"board unreachable: {err}") from err
    else:
        try:
            body = json.loads(out)
        except ValueError:
            raise iface.TrackerError(f"unusable board answer: {out[:80]!r}") from None
    if not isinstance(body, dict):
        raise iface.TrackerError(f"unusable board answer: {body!r}")
    _check_two_alias_errors(body.get("errors"), body.get("data"), active)
    data = body.get("data")
    if data is None or isinstance(data, dict):
        return data
    raise iface.TrackerError(f"unusable board answer: {body!r}")


def _check_two_alias_errors(errors, data, pacer) -> None:
    """Raise for error entries besides the losing half's ``NOT_FOUND``.

    The one classifier for two-alias answers, so the schema read, the
    title list and the provision lookup decide the same way: a rate-limit
    entry cools the shared bucket down and raises ``TrackerRateLimited``;
    a ``NOT_FOUND`` on exactly the half that resolved to null passes; and
    anything else fails closed with ``TrackerError``. No errors passes,
    and the caller decides a missing board from the data.
    """
    if not errors:
        return
    if _errors_rate_limited(errors):
        text = json.dumps(errors)
        pacer.cooldown(gh.retry_after_s(text))
        raise iface.TrackerRateLimited(text, gh.retry_after_s(text))
    if not isinstance(errors, list):
        raise iface.TrackerError(f"board unreachable: {json.dumps(errors)}")
    for entry in errors:
        if _tolerated_half_error(entry, data):
            continue
        raise iface.TrackerError(f"board unreachable: {json.dumps(errors)}")


def _tolerated_half_error(entry, data) -> bool:
    """Whether one error entry is the losing half's expected ``NOT_FOUND``.

    Only a ``NOT_FOUND`` whose path is exactly one owner half, and that
    half resolved to null: a ``NOT_FOUND`` anywhere else (or beside a
    resolved half) fails closed like any other error entry.
    """
    if not isinstance(entry, dict) or entry.get("type") != "NOT_FOUND":
        return False
    path = entry.get("path")
    if path not in (["user"], ["organization"]):
        return False
    half = path[0]
    return not isinstance(data, dict) or data.get(half) is None


def _check_body_errors(body, pacer) -> None:
    """Raise for the error entries a GraphQL body carries beside its data.

    The one classifier every single-alias provision read and mutation runs
    its failed response through, so the error contract holds on all
    paths: a RATE_LIMITED entry raises ``TrackerRateLimited``, a
    FORBIDDEN entry raises ``_BoardForbidden``, and any other entry
    besides NOT_FOUND entries (a read for a node that is not there)
    raises ``TrackerError`` instead of resolving to a missing board or a
    refusal. Two-alias owner queries never reach this classifier; they go
    through ``_check_two_alias_errors`` instead. A transient conflict
    entry therefore fails closed here; only the populate operation
    retries those. A body with no errors, or only NOT_FOUND entries,
    passes: the caller decides those from the data.
    """
    errors = body.get("errors") if isinstance(body, dict) else None
    if not errors:
        return
    if _errors_rate_limited(errors):
        text = json.dumps(errors)
        if pacer is not None:
            pacer.cooldown(gh.retry_after_s(text))
        raise iface.TrackerRateLimited(text, gh.retry_after_s(text))
    if any(isinstance(entry, dict) and entry.get("type") == "FORBIDDEN" for entry in errors):
        raise _BoardForbidden(json.dumps(errors))
    if all(isinstance(entry, dict) and entry.get("type") == "NOT_FOUND" for entry in errors):
        return
    raise iface.TrackerError(f"board unreachable: {json.dumps(errors)}")


def _mutate(run, pacer, query, **variables):
    """One paced GraphQL mutation as its ``data`` payload.

    The one mutation path every board write goes through, so setup and
    the pass pace through the same shared bucket: rate-limit text maps
    to ``TrackerRateLimited`` through the shared classifier, and a
    response body surviving on the error is decided by the one body
    classifier; anything else fails closed, like the board reads and
    writes.
    """
    active = pacer if pacer is not None else gh._pacer()
    try:
        return gh.graphql(query, run=run, pacer=pacer, **variables)
    except gh.GhError as err:
        text = str(err) or repr(err)
        if err.kind == "unavailable" or gh.is_rate_limited(text):
            raise iface.TrackerRateLimited(text, gh.retry_after_s(text)) from err
        body = _response_body(err)
        if body is not None:
            _check_body_errors(body, active)
        raise iface.TrackerError(f"board unreachable: {text}") from err
    except Exception as err:
        raise iface.TrackerError(f"board unreachable: {err}") from err


def _add_item(run, pacer, project_id, content_id):
    """One issue added to the board: its item id, or ``_ItemRefused``.

    The one issue add every adopter shares: a temporary move conflict
    (the first adds after a link or create) retries bounded and never
    refuses a transient; a permission answer and an exhausted conflict
    refuse with a fix line instead.
    """
    attempts = 0
    while True:
        try:
            payload = _mutate(run, pacer, _ADD_ITEM, projectId=project_id, contentId=content_id)
        except iface.TrackerRateLimited:
            raise
        except _BoardForbidden as err:
            raise _ItemRefused(
                "the account may not add items to it; add it on the board by hand, then re-run"
            ) from err
        except iface.TrackerError as err:
            text = str(err)
            if "(HTTP 403)" in text:
                raise _ItemRefused(
                    "the account may not add items to it; add it on the board by hand, then re-run"
                ) from err
            if _transient_add_failure(text) and attempts + 1 < _ADD_ATTEMPTS:
                attempts += 1
                continue
            raise _ItemRefused(f"{text}; re-run to retry") from err
        node = payload.get("addProjectV2ItemById") if isinstance(payload, dict) else None
        item = node.get("item") if isinstance(node, dict) else None
        item_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(item_id, str) or not item_id:
            raise _fail(f"unusable board add answer: {payload!r}")
        return item_id


def add_missing_issues(run, pacer, *, project_id, present, candidates, limit=None):
    """Add each missing candidate to the board: the one shared adder.

    ``present`` holds the keys already on the board (opaque to this
    function: node ids for setup, ``(repo, number)`` for the pass) and
    ``candidates`` are ``(key, content id, name)`` in listing order.
    A repeated key is attempted once, so a page-boundary duplicate
    cannot add twice or burn the cap twice. ``limit`` caps the
    attempted adds (a refused add still consumes the budget, so a
    failing run cannot retry the whole backlog in one pass); the rest
    carries to the next pass. Every add goes through
    the one issue-add path above, paced through the shared bucket; a
    rate limit halts the run with ``stopped`` set, and a failed add is
    one ``refused`` line naming the issue, retried on the next pass.
    A malformed add answer fails closed.
    """
    added: list = []
    kept: list = []
    refused: list = []
    remaining = limit
    seen: set = set()
    for key, content_id, name in candidates:
        if key in seen:
            continue
        seen.add(key)
        if key in present:
            kept.append(name)
            continue
        if remaining is not None and remaining <= 0:
            break
        try:
            _add_item(run, pacer, project_id, content_id)
        except iface.TrackerRateLimited as err:
            return iface.BoardAdopt(
                tuple(added), tuple(kept), tuple(refused), (str(err), err.retry_after)
            )
        except _ItemRefused as err:
            refused.append(f"could not add {name} to the board: {err}")
        else:
            added.append(name)
        if remaining is not None:
            remaining -= 1
    return iface.BoardAdopt(tuple(added), tuple(kept), tuple(refused), None)


class GithubBoardProvisioner:
    """One board's idempotent ensure, over the injected ``run`` seam.

    Constructed with only ``run`` and the pacer, never a ``BoardConfig``:
    provisioning runs in the bootstrap states where no board tracker can
    exist yet. Each step checks its precondition against the live board,
    so a partial run converges on re-run; the board itself is the state.
    A spec that cannot run as given raises ``ValueError`` before any
    mutation, an unreachable provider raises ``TrackerError``, and a
    rate-limit answer raises ``TrackerRateLimited``.
    """

    def __init__(self, run, pacer=None):
        """``run(args)`` returns stdout and raises when ``gh`` fails."""
        self._run = run
        self._pacer = pacer

    def _active_pacer(self):
        """The injected pacer, else the machine-wide shared bucket."""
        return self._pacer if self._pacer is not None else gh._pacer()

    def _mutate(self, query, **variables):
        """One paced GraphQL mutation as its ``data`` payload.

        The shared module path above, bound to this provisioner's seam.
        """
        return _mutate(self._run, self._pacer, query, **variables)

    def _read(self, query, make_missing, **variables):
        """One paced GraphQL read as its ``data`` payload, tolerating bodies.

        A body that carries GraphQL ``errors`` beside partial ``data``
        still decides: ``make_missing`` builds the ``ValueError`` for
        data that resolves nothing (``None`` when the caller has no such
        case, and then an unresolvable read fails closed instead).
        """
        try:
            return gh.graphql(query, run=self._run, pacer=self._pacer, **variables)
        except gh.GhError as err:
            text = str(err) or repr(err)
            if err.kind == "unavailable" or gh.is_rate_limited(text):
                raise iface.TrackerRateLimited(text, gh.retry_after_s(text)) from err
            body = _response_body(err)
            if body is not None:
                _check_body_errors(body, self._active_pacer())
                data = body.get("data")
                if data is None or isinstance(data, dict):
                    return data
            if make_missing is not None and _missing_only(text):
                raise make_missing() from err
            raise iface.TrackerError(f"board unreachable: {text}") from err
        except Exception as err:
            raise iface.TrackerError(f"board unreachable: {err}") from err

    def _read_owner(self, query, **variables):
        """One paced two-alias read as its tolerant ``data`` payload.

        The provision lookup asks one login as both halves, the way the
        reader does; both go through the one two-alias helper, so the
        losing half's ``NOT_FOUND`` noise never fails the lookup. A body
        whose data resolves nothing is returned as-is: the caller decides
        a missing board from the data, with its own message.
        """
        return _two_alias_data(query, variables, self._run, self._pacer)

    def _resolve_owner(self, login):
        """``(kind, node id)`` for one board owner login.

        ``kind`` is ``"user"`` or ``"organization"``. An unknown login
        and a login resolving to both halves raise ``ValueError`` before
        any mutation; a half that resolves without a node id is an
        unusable answer.
        """

        def missing():
            return ValueError(
                f"board owner {login!r} resolves to neither a user nor an organization"
            )

        data = self._read_owner(_OWNER_QUERY, login=login)
        if not isinstance(data, dict):
            raise missing()
        user = data.get("user")
        org = data.get("organization")
        if user is None and org is None:
            raise missing()
        for half in (user, org):
            if half is None:
                continue
            if not isinstance(half, dict):
                raise _fail(f"bad board owner {half!r}")
            node_id = half.get("id")
            if not isinstance(node_id, str) or not node_id:
                raise _fail(f"bad board owner {half!r}")
        if user is not None and org is not None:
            raise ValueError(f"board owner {login!r} resolves to both a user and an organization")
        if user is not None:
            return "user", user["id"]
        return "organization", org["id"]

    def _live_option(self, field_name, entry):
        """One raw option node as a ``LiveOption``, else ``TrackerError``.

        A missing color or description rides as ``None``: the append
        step refuses to resend an option it cannot reproduce in full.
        """
        if not isinstance(entry, dict):
            raise _fail(f"bad board option {entry!r} in {field_name!r}")
        oid = entry.get("id")
        name = entry.get("name")
        if not isinstance(oid, str) or not oid:
            raise _fail(f"bad board option {entry!r} in {field_name!r}")
        if not isinstance(name, str):
            raise _fail(f"bad board option {entry!r} in {field_name!r}")
        color = entry.get("color")
        description = entry.get("description")
        if color is not None and not isinstance(color, str):
            raise _fail(f"bad board option {entry!r} in {field_name!r}")
        if description is not None and not isinstance(description, str):
            raise _fail(f"bad board option {entry!r} in {field_name!r}")
        return iface.LiveOption(id=oid, name=name, color=color, description=description)

    def _live_fields(self, nodes):
        """Raw field nodes as planner input, else ``TrackerError``.

        Duplicate names are not deduped here: the planner refuses them
        with a fix line, never silently picks one.
        """
        if not isinstance(nodes, list):
            raise _fail("board fields are not a list")
        found = []
        for node in nodes:
            if not isinstance(node, dict):
                raise _fail(f"bad board field {node!r}")
            fid = node.get("id")
            name = node.get("name")
            if not isinstance(fid, str) or not fid:
                raise _fail(f"bad board field {node!r}")
            if not isinstance(name, str) or not name:
                raise _fail(f"bad board field {node!r}")
            kind = _field_kind(node)
            if kind != "single_select":
                found.append(iface.LiveField(name=name, kind=kind))
                continue
            options = node.get("options")
            if not isinstance(options, list):
                raise _fail(f"board field {name!r} has no options")
            found.append(
                iface.LiveField(
                    name=name,
                    kind=kind,
                    options=tuple(self._live_option(name, entry) for entry in options),
                )
            )
        return tuple(found)

    def _parse_project(self, payload):
        """One extended project object as plain data, else ``TrackerError``.

        Returns ``(id, number, title, public, repos, live fields, field
        ids by name)``; the ids serve the option-append update, which the
        planner input does not carry.
        """
        if not isinstance(payload, dict):
            raise _fail(f"board project is not an object: {payload!r}")
        project_id = payload.get("id")
        number = payload.get("number")
        title = payload.get("title")
        public = payload.get("public")
        if not isinstance(project_id, str) or not project_id:
            raise _fail(f"bad board project id {project_id!r}")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _fail(f"bad board project number {number!r}")
        if not isinstance(title, str) or not title:
            raise _fail(f"bad board project title {title!r}")
        if not isinstance(public, bool):
            raise _fail(f"bad board project visibility {public!r}")
        repositories = payload.get("repositories")
        nodes = repositories.get("nodes") if isinstance(repositories, dict) else None
        if not isinstance(nodes, list):
            raise _fail("board repositories are not a list")
        repos = []
        for entry in nodes:
            if not isinstance(entry, dict):
                raise _fail(f"bad board repository {entry!r}")
            name = entry.get("nameWithOwner")
            if not isinstance(name, str) or not name:
                raise _fail(f"bad board repository {entry!r}")
            repos.append(name)
        fields = payload.get("fields")
        field_nodes = fields.get("nodes") if isinstance(fields, dict) else None
        if not isinstance(field_nodes, list):
            raise _fail("board fields are not a list")
        live = self._live_fields(field_nodes)
        ids = {}
        for node, field in zip(field_nodes, live):
            ids.setdefault(field.name, node["id"])
        return (project_id, number, title, public, tuple(repos), live, ids)

    def _read_project_by_id(self, project_id):
        """One board by node id as parsed data, else ``ValueError`` or ``TrackerError``."""

        def missing():
            return ValueError(_REMOVE_TABLE_LINE)

        data = self._read(_BY_ID, missing, id=project_id)
        if not isinstance(data, dict):
            raise missing()
        node = data.get("node")
        if node is None:
            raise missing()
        return self._parse_project(node)

    def _find_by_address(self, spec):
        """The spec's addressed board as parsed data, never a neighbor.

        A board found by address is the board whatever its current title
        is; an address that resolves nothing raises ``ValueError`` with
        the remove-the-table fix before any mutation, and never falls
        through to title search or creation.
        """

        def missing():
            return ValueError(_REMOVE_TABLE_LINE)

        if spec.project_id is not None:
            return self._read_project_by_id(spec.project_id)
        data = self._read_owner(_BY_NUMBER, owner=spec.owner, number=spec.number)
        if not isinstance(data, dict):
            raise missing()
        user = data.get("user")
        org = data.get("organization")
        for half in (user, org):
            if half is not None and not isinstance(half, dict):
                raise _fail(f"bad board owner {half!r}")
        user_project = user.get("projectV2") if isinstance(user, dict) else None
        org_project = org.get("projectV2") if isinstance(org, dict) else None
        if user_project is None and org_project is None:
            raise missing()
        if user_project is not None and org_project is not None:
            raise _fail("board project is missing or ambiguous")
        project = user_project if org_project is None else org_project
        return self._parse_project(project)

    def _find_by_title(self, spec):
        """The spec's title match as a project node id, ``None`` or ``ValueError``.

        Zero matches means create; two or more name every matching
        number and raise before any mutation. The one query carries
        both owner halves and the set half is picked from the body, the
        way ``_find_by_address`` does. Titles are a
        provisioning-time search key only: an addressed run never calls
        this.
        """
        matches = []
        after = None
        while True:
            if after is None:
                data = self._read_owner(_TITLES, login=spec.owner)
            else:
                data = self._read_owner(_TITLES, login=spec.owner, after=after)
            entries, info = _titles_entries(data)
            for eid, number, title in entries:
                if title == spec.title:
                    matches.append((eid, number))
            after, more = _titles_next(info)
            if not more:
                break
        if not matches:
            return None
        if len(matches) > 1:
            numbers = ", ".join(
                str(number) for _, number in sorted(matches, key=lambda item: item[1])
            )
            raise ValueError(
                f"board title {spec.title!r} under {spec.owner} matches more than one board "
                f"(numbers {numbers}): delete the board the config does not address by hand, then re-run"
            )
        return matches[0][0]

    def _create_board(self, spec, owner_id, repo_id):
        """The spec's board, created and read back, as parsed data."""
        variables = {"ownerId": owner_id, "title": spec.title}
        if repo_id is not None:
            variables["repositoryId"] = repo_id
        payload = self._mutate(_CREATE, **variables)
        node = payload.get("createProjectV2") if isinstance(payload, dict) else None
        project = node.get("projectV2") if isinstance(node, dict) else None
        if not isinstance(project, dict):
            raise _fail(f"unusable board create answer: {payload!r}")
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise _fail(f"unusable board create answer: {payload!r}")
        return self._read_project_by_id(project_id)

    def _make_private(self, project_id):
        """A just-created board made private: one update mutation.

        Making the board private is part of creating it as specced, and
        gates everything after it: when the update errors the error
        propagates and nothing else runs.
        """
        payload = self._mutate(_MAKE_PRIVATE, projectId=project_id)
        node = payload.get("updateProjectV2") if isinstance(payload, dict) else None
        if not isinstance(node, dict) or not isinstance(node.get("projectV2"), dict):
            raise _fail(f"unusable board update answer: {payload!r}")

    def _fetch_repo_id(self, repo):
        """One tracker repo's node id, ``None`` when unlinkable, else ``TrackerError``.

        A repo the account cannot see or administer refuses only the
        link step; a rate-limit answer stops the run, and anything else
        fails closed.
        """
        try:
            payload = json.loads(
                gh.paced_run(["api", f"repos/{repo}"], run=self._run, pacer=self._pacer)
            )
        except gh.GhError as err:
            text = str(err) or repr(err)
            if gh.is_rate_limited(text):
                raise iface.TrackerRateLimited(text, gh.retry_after_s(text)) from err
            if "(HTTP 404)" in text or "(HTTP 403)" in text:
                return None
            raise iface.TrackerError(f"board unreachable: {text}") from err
        except Exception as err:
            raise iface.TrackerError(f"board unreachable: {err}") from err
        if not isinstance(payload, dict):
            raise _fail(f"unusable repo answer: {payload!r}")
        node_id = payload.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise _fail(f"unusable repo answer: {payload!r}")
        return node_id

    def _ensure_link(self, spec, project_id, repos, repo_ids, created, kept, refused):
        """The spec's repo linked, or kept, or refused with a fix line."""
        if not spec.repo:
            return
        if spec.repo in repos:
            kept.append(f"link:{spec.repo}")
            return
        if spec.repo in repo_ids:
            repo_id = repo_ids[spec.repo]
        else:
            repo_id = self._fetch_repo_id(spec.repo)
            repo_ids[spec.repo] = repo_id
        if repo_id is None:
            refused.append(_link_fix(spec.repo))
            return
        try:
            payload = self._mutate(_LINK, projectId=project_id, repositoryId=repo_id)
        except iface.TrackerRateLimited:
            raise
        except _BoardForbidden:
            refused.append(_link_fix(spec.repo))
            return
        except iface.TrackerError as err:
            if "(HTTP 403)" in str(err):
                refused.append(_link_fix(spec.repo))
                return
            raise
        node = payload.get("linkProjectV2ToRepository") if isinstance(payload, dict) else None
        if not isinstance(node, dict):
            raise _fail(f"unusable board link answer: {payload!r}")
        created.append(f"link:{spec.repo}")

    def _create_field(self, project_id, field):
        """One spec field created, with its standard options when single-select."""
        if field.kind == "text":
            payload = self._mutate(_TEXT_FIELD_CREATE, projectId=project_id, name=field.name)
        else:
            options = [
                {"name": name, "color": _NEW_OPTION_COLOR, "description": ""}
                for name in field.options
            ]
            payload = self._mutate(
                _SELECT_FIELD_CREATE,
                projectId=project_id,
                name=field.name,
                singleSelectOptions=options,
            )
        node = payload.get("createProjectV2Field") if isinstance(payload, dict) else None
        created = node.get("projectV2Field") if isinstance(node, dict) else None
        if (
            not isinstance(created, dict)
            or not isinstance(created.get("id"), str)
            or not created["id"]
        ):
            raise _fail(f"unusable board field answer: {payload!r}")

    def _append_options(self, project_id, field_ids, plan, created, refused):
        """A kept Priority field's missing ranks appended, or refused by hand.

        The update replaces the whole option list, so every existing
        option rides along with its id, color and description unchanged;
        an option that cannot be resent in full refuses the append
        instead, since resetting people's colors is forbidden and new
        ids would silently invalidate every map in the wild.
        """
        missing = [option for option in plan.options]
        resend = list(plan.resend)
        if any(
            option.id is None or option.color is None or option.description is None
            for option in resend
        ):
            for name in missing:
                refused.append(_option_fix(name, plan.name))
            return
        options = [
            {
                "id": option.id,
                "name": option.name,
                "color": option.color,
                "description": option.description,
            }
            for option in resend
        ]
        options += [
            {"name": name, "color": _NEW_OPTION_COLOR, "description": ""} for name in missing
        ]
        payload = self._mutate(
            _OPTIONS_APPEND, fieldId=field_ids[plan.name], singleSelectOptions=options
        )
        node = payload.get("updateProjectV2Field") if isinstance(payload, dict) else None
        if not isinstance(node, dict) or not isinstance(node.get("projectV2Field"), dict):
            raise _fail(f"unusable board options answer: {payload!r}")
        created.extend(f"option:{plan.name}/{name}" for name in missing)

    def _ensure_fields(self, spec, project_id, live, field_ids, created, kept, refused):
        """The planner's field actions executed: creates, keeps, appends."""
        plan = iface.plan_board(spec, live)
        refused.extend(plan.refused)
        for field, field_plan in zip(spec.fields, plan.fields):
            if field_plan.decision == "create":
                self._create_field(project_id, field)
                created.append(f"field:{field.name}")
            elif field_plan.decision == "keep":
                kept.append(f"field:{field.name}")
                if field_plan.options:
                    self._append_options(project_id, field_ids, field_plan, created, refused)

    def _report(self, spec, project_id, number, created, kept, refused, live):
        """The ensure outcome, built from the final read-back.

        The names in ``fields`` come from the provision, never
        re-derived from defaults, and a refused concept renders nothing;
        the map is built rename-is-free, so the file never names an id
        the board does not hold. A refusal standing on the final
        read-back is the same refusal, never a second line.
        """
        plan = iface.plan_board(spec, live)
        refused = tuple(dict.fromkeys(tuple(refused) + plan.refused))
        live_concepts = {
            field_plan.concept for field_plan in plan.fields if field_plan.decision != "refuse"
        }
        fields = tuple(field for field in spec.fields if field.concept in live_concepts)
        return iface.BoardProvision(
            owner=spec.owner,
            number=number,
            project_id=project_id,
            fields=fields,
            created=tuple(created),
            kept=tuple(kept),
            refused=refused,
            priority_map=plan.priority_map,
        )

    def ensure_board(self, spec):
        """Create what the spec names and is missing, keep the rest.

        Never alters or deletes what exists: an existing field is kept
        byte-for-byte, and options are only ever appended to a kept
        Priority field. See the class docstring for the error contract.
        """
        _, owner_id = self._resolve_owner(spec.owner)
        created: list = []
        kept: list = []
        refused: list = []
        repo_ids: dict = {}
        if spec.project_id is not None or spec.number is not None:
            project = self._find_by_address(spec)
            kept.append("project")
            via_address = True
        else:
            found_id = self._find_by_title(spec)
            if found_id is None:
                repo_id = None
                if spec.repo:
                    repo_id = self._fetch_repo_id(spec.repo)
                    repo_ids[spec.repo] = repo_id
                project = self._create_board(spec, owner_id, repo_id)
                created.append("project")
                via_address = False
            else:
                project = self._read_project_by_id(found_id)
                kept.append("project")
                via_address = False
        project_id, number, title, public, repos, live, field_ids = project
        if spec.private and public:
            if created:
                self._make_private(project_id)
                project = self._read_project_by_id(project_id)
                project_id, number, title, public, repos, live, field_ids = project
                if public:
                    fix = _public_fix_new(title)
                    link_kept = [f"link:{spec.repo}"] if spec.repo and spec.repo in repos else []
                    return self._report(
                        spec, project_id, number, ["project"], link_kept, [fix], live
                    )
            elif via_address:
                refused.append(_public_fix_keep(title))
            else:
                refused.append(_public_fix_keep(title))
                self._ensure_link(spec, project_id, repos, repo_ids, created, kept, refused)
                return self._report(spec, project_id, number, created, kept, refused, live)
        self._ensure_link(spec, project_id, repos, repo_ids, created, kept, refused)
        self._ensure_fields(spec, project_id, live, field_ids, created, kept, refused)
        project = self._read_project_by_id(project_id)
        project_id, number, _, _, _, live, _ = project
        return self._report(spec, project_id, number, created, kept, refused, live)

    def dry_run(self, spec):
        """What ``ensure_board`` would do, changing nothing. Read-only.

        Every step is a read: the owner, the board by address or title,
        the live schema, the repo for the link preview. The report has
        the same shape as the ensure one, with would-be creations in
        ``created``. A spec that cannot run raises ``ValueError`` before
        any read; an unreachable provider raises ``TrackerError``.
        """
        iface.plan_board(spec, ())
        self._resolve_owner(spec.owner)
        if spec.project_id is not None or spec.number is not None:
            project = self._find_by_address(spec)
            via_address = True
        else:
            found_id = self._find_by_title(spec)
            if found_id is None:
                return self._dry_run_create(spec)
            project = self._read_project_by_id(found_id)
            via_address = False
        project_id, number, title, public, repos, live, _ = project
        created: list = []
        kept: list = []
        refused: list = []
        stop_fields = False
        if spec.private and public:
            refused.append(_public_fix_keep(title))
            stop_fields = not via_address
        if spec.repo:
            if spec.repo in repos:
                kept.append(f"link:{spec.repo}")
            else:
                repo_id = self._fetch_repo_id(spec.repo)
                if repo_id is None:
                    refused.append(_link_fix(spec.repo))
                else:
                    created.append(f"link:{spec.repo}")
        if not stop_fields:
            plan = iface.plan_board(spec, live)
            for field, field_plan in zip(spec.fields, plan.fields):
                if field_plan.decision == "create":
                    created.append(f"field:{field.name}")
                elif field_plan.decision == "keep":
                    kept.append(f"field:{field.name}")
                    if field_plan.options:
                        self._preview_append(field_plan, created, refused)
        return self._report(spec, project_id, number, created, kept, refused, live)

    def _dry_run_create(self, spec):
        """The preview for a board the title search did not find.

        Every spec field would be created (the planner over an empty
        live schema says what the fresh board keeps), except a missing
        keep-only field, which refuses with its flag fix line. The
        standard-named ``Status`` field is the exception: every new
        board ships that builtin, so the ensure keeps it and the
        preview says kept, exactly as the create's own read-back would.
        A remapped name misses the builtin and is created like any
        other field. The atomic link the create would carry reads as
        kept, like the ensure's own re-read reports it.
        """
        standard_status = next(
            field.name for field in iface.STANDARD_BOARD if field.concept == "board_status"
        )
        created = ["project"]
        kept: list = []
        refused: list = []
        if spec.repo:
            repo_id = self._fetch_repo_id(spec.repo)
            if repo_id is None:
                refused.append(_link_fix(spec.repo))
            else:
                kept.append(f"link:{spec.repo}")
        plan = iface.plan_board(spec, ())
        for field, field_plan in zip(spec.fields, plan.fields):
            if field_plan.decision != "create":
                continue
            if field.concept == "board_status" and field.name == standard_status:
                kept.append(f"field:{field.name}")
            else:
                created.append(f"field:{field.name}")
        refused.extend(plan.refused)
        return self._report(spec, "", None, created, kept, refused, ())

    def _preview_append(self, field_plan, created, refused):
        """The option append one kept field would send, or its refusal."""
        missing = list(field_plan.options)
        if any(
            option.id is None or option.color is None or option.description is None
            for option in field_plan.resend
        ):
            for name in missing:
                refused.append(_option_fix(name, field_plan.name))
            return
        created.extend(f"option:{field_plan.name}/{name}" for name in missing)

    def _board_content_ids(self, project_id):
        """The issue node ids on the board, paged, else ``TrackerError``.

        Draft items carry no content and read as skipped. A malformed
        page fails closed, never as a partial membership set: adopting
        on partial knowledge would double-add or misreport.
        """
        seen = set()
        after = None
        while True:
            if after is None:
                payload = self._read(_PROVISION_ITEMS, None, project=project_id)
            else:
                payload = self._read(_PROVISION_ITEMS, None, project=project_id, after=after)
            node = payload.get("node") if isinstance(payload, dict) else None
            items = node.get("items") if isinstance(node, dict) else None
            if not isinstance(items, dict):
                raise _fail("board items are missing")
            nodes = items.get("nodes")
            if not isinstance(nodes, list):
                raise _fail("board items are not a list")
            for item in nodes:
                if not isinstance(item, dict):
                    raise _fail(f"bad board item {item!r}")
                content = item.get("content")
                if content is None:
                    continue
                if not isinstance(content, dict):
                    raise _fail(f"bad board item content {content!r}")
                if content.get("__typename") != "Issue":
                    continue
                content_id = content.get("id")
                if not isinstance(content_id, str) or not content_id:
                    raise _fail(f"bad board item content {content!r}")
                seen.add(content_id)
            info = items.get("pageInfo")
            if not isinstance(info, dict):
                raise _fail("board items are missing their pageInfo")
            more = info.get("hasNextPage")
            if not isinstance(more, bool):
                raise _fail(f"bad board pageInfo {info!r}")
            if not more:
                return seen
            end = info.get("endCursor")
            if not isinstance(end, str) or not end:
                raise _fail("board items claim more items but name no cursor")
            after = end

    def _open_issue_refs(self, repo):
        """``(node id, number)`` for the tracker repo's open issues.

        Closed issues stay people's history to add by hand, and pull
        requests are never board items korapy creates. The page-walk is
        the shared listing helper; anything but open is skipped, since
        this listing asked for open only.
        """
        for entry in _list_issue_entries(self._run, self._pacer, repo, "open"):
            if "pull_request" in entry:
                continue
            if entry.get("state") != "open":
                continue
            yield _issue_ref(entry, repo)

    def _add_item(self, project_id, content_id):
        """One issue added to the board: its item id, or ``_ItemRefused``.

        The shared module add above, bound to this provisioner's seam.
        """
        return _add_item(self._run, self._pacer, project_id, content_id)

    def populate(self, project_id, repo):
        """Add the tracker repo's open issues missing from the board.

        Item-add only: no field value is ever written. Open issues
        only, whatever their author; closed issues and pull requests
        are never added. The adds run through the one shared adder the
        board step uses, uncapped; a rate limit still propagates like
        any other adapter failure. Each issue the run could not add
        reports its own refused line, and a re-run converges because
        presence is re-read each time. A listing that falls over raises
        ``TrackerError`` instead of adopting on partial knowledge.
        """
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("board populate needs a project id")
        present = self._board_content_ids(project_id)
        candidates = [
            (node_id, node_id, f"{repo}#{number}")
            for node_id, number in self._open_issue_refs(repo)
        ]
        adopted = add_missing_issues(
            self._run,
            self._pacer,
            project_id=project_id,
            present=present,
            candidates=candidates,
        )
        if adopted.stopped is not None:
            raise iface.TrackerRateLimited(*adopted.stopped)
        return iface.BoardPopulate(adopted.added, adopted.kept, adopted.refused)

    def preview_populate(self, project_id, repo):
        """What ``populate`` would add, changing nothing. Read-only.

        The same membership read the mutating path starts from, with
        would-be additions in ``added`` and the issues already present
        in ``kept``. ``setup --plan`` reports this and adds nothing.
        """
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("board populate needs a project id")
        present = self._board_content_ids(project_id)
        added: list = []
        kept: list = []
        for node_id, number in self._open_issue_refs(repo):
            name = f"{repo}#{number}"
            if node_id in present:
                kept.append(name)
            else:
                added.append(name)
        return iface.BoardPopulate(tuple(added), tuple(kept), ())
