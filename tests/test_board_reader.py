"""The board reader: schema fetch plus paged items over GraphQL, then issues over REST.

Canned payloads only: ``BoardFakeRun`` answers GraphQL pages and REST
issue payloads at the injected ``run`` seam, and ``FakePacer`` stands in
for the shared bucket. Importing ``tests.test_board_fields`` installs the
suite-wide tripwire that fails the run if any test reaches the default
pacer file or a real subprocess.
"""

import calendar
import datetime
import json
import os
import shutil
import tempfile
import unittest
from typing import Any

from corral import board as store
from corral import settings
from corral import tracker as iface
from corral.github import board as boardreader
from corral.github import gh
from tests.test_board_fields import FakePacer
from tests.test_tracker import item_payload, pull_payload

T0 = "2026-10-28T12:00:00Z"
T1 = "2026-10-28T13:00:00Z"


def config(**over: Any):
    """A board config naming every concept, with overrides."""
    base: dict[str, Any] = dict(
        project_owner="example",
        project_number=3,
        project_id=None,
        agent_field="Agent",
        status_field="Agent state",
        status_column="Status",
        priority_column="Priority",
        area_column="Area",
        pool_column="Pool",
        blocked_by_column="Blocked by",
        priority_map={"opt-p0-id": "urgent", "opt-p1-id": "high"},
    )
    base.update(over)
    return settings.BoardConfig(**base)


def text_field(fid, name, datatype="TEXT", typename="ProjectV2Field"):
    return {"__typename": typename, "id": fid, "name": name, "dataType": datatype}


def select_field(fid, name, options):
    return {
        "__typename": "ProjectV2SingleSelectField",
        "id": fid,
        "name": name,
        "dataType": "SINGLE_SELECT",
        "options": [{"id": oid, "name": oname} for oid, oname in options],
    }


def schema_payload(*fields, project_id="PVT_1"):
    return {
        "user": {
            "projectV2": {
                "id": project_id,
                "title": "Board",
                "fields": {"nodes": list(fields)},
            }
        },
        "organization": None,
    }


def full_schema_fields():
    return [
        text_field("F_agent", "Agent"),
        text_field("F_status", "Agent state"),
        select_field("F_wf", "Status", [("opt-s1", "In progress")]),
        select_field("F_prio", "Priority", [("opt-p0-id", "P0"), ("opt-p1-id", "P1")]),
        select_field("F_area", "Area", [("opt-a1", "Backend")]),
        select_field("F_pool", "Pool", [("opt-q1", "Dispatch")]),
        text_field("F_blocked", "Blocked by"),
    ]


def text_value(fid, name, text):
    return {
        "__typename": "ProjectV2ItemFieldTextValue",
        "text": text,
        "field": {"__typename": "ProjectV2Field", "id": fid, "name": name},
    }


def select_value(fid, name, oid, oname):
    return {
        "__typename": "ProjectV2ItemFieldSingleSelectValue",
        "optionId": oid,
        "name": oname,
        "field": {"__typename": "ProjectV2SingleSelectField", "id": fid, "name": name},
    }


def item_node(number, repo="example/demo", updated=T1, values=(), typename="Issue"):
    if typename == "DraftIssue":
        content = {"__typename": "DraftIssue", "title": "An idea"}
    else:
        content = {
            "__typename": typename,
            "number": number,
            "repository": {"nameWithOwner": repo},
        }
    return {
        "id": f"PVTI_{typename}-{number}",
        "updatedAt": updated,
        "content": content,
        "fieldValues": {"nodes": list(values)},
    }


def items_payload(nodes, end=None, more=False):
    return {
        "node": {
            "items": {
                "pageInfo": {"hasNextPage": more, "endCursor": end},
                "nodes": list(nodes),
            }
        }
    }


class BoardFakeRun:
    """A ``gh`` seam double: scripted GraphQL pages plus REST routes."""

    def __init__(self):
        self.rest = {}
        self.rest_fallback = None
        self.pages = {}
        self.calls = []

    def add_rest(self, path, payload):
        self.rest[path] = payload
        return self

    def fail_rest(self, path, error):
        self.rest[path] = error
        return self

    def script(self, marker, payloads):
        self.pages[marker] = list(payloads)
        return self

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        if args[1] == "graphql":
            return self._graphql(args)
        path = args[1]
        if path in self.rest:
            answer = self.rest[path]
            if isinstance(answer, Exception):
                raise answer
            return json.dumps(answer)
        if self.rest_fallback is not None:
            return self.rest_fallback(path)
        raise RuntimeError(f"no route for {path}")

    def _graphql(self, args):
        query = ""
        variables = {}
        toks = args[2:]
        i = 0
        while i < len(toks):
            if toks[i] in ("-f", "-F") and i + 1 < len(toks):
                key, _, value = toks[i + 1].partition("=")
                if key == "query":
                    query = value
                else:
                    variables[key] = value
                i += 2
            else:
                i += 1
        for marker, payloads in self.pages.items():
            if marker in query:
                if not payloads:
                    raise RuntimeError(f"no more {marker} pages")
                answer = payloads.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                if isinstance(answer, dict) and ("data" in answer or "errors" in answer):
                    return json.dumps(answer)
                return json.dumps({"data": answer})
        raise RuntimeError(f"no scripted page for query {query[:60]!r}")

    def graphql_variables(self, marker):
        """The variables of every GraphQL call whose query holds ``marker``."""
        found = []
        for args, _ in self.calls:
            if args[1] != "graphql":
                continue
            query = ""
            variables = {}
            toks = args[2:]
            i = 0
            while i < len(toks):
                if toks[i] in ("-f", "-F") and i + 1 < len(toks):
                    key, _, value = toks[i + 1].partition("=")
                    if key == "query":
                        query = value
                    else:
                        variables[key] = value
                    i += 2
                else:
                    i += 1
            if marker in query:
                found.append(variables)
        return found


def reader_for(run, **over):
    return boardreader.BoardReader(config(**over), run, pacer=FakePacer())


def schema_for(run, **over):
    return reader_for(run, **over).schema()


def rest_issue(run, number, repo="example/demo", **over):
    run.add_rest(f"repos/{repo}/issues/{number}", item_payload(number, **over))
    return run


class SchemaFetchTest(unittest.TestCase):
    def test_schema_by_number_resolves_ids_and_options(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*full_schema_fields())])
        found = schema_for(run)
        self.assertEqual(found.project_id, "PVT_1")
        self.assertEqual(found.fields["priority"].id, "F_prio")
        self.assertEqual(
            found.fields["priority"].options,
            {"opt-p0-id": "P0", "opt-p1-id": "P1"},
        )
        self.assertEqual(found.fields["agent"].kind, "text")
        self.assertEqual(found.fields["priority"].kind, "single_select")

    def test_schema_by_node_id_when_no_number(self):
        run = BoardFakeRun().script(
            "BoardSchemaById",
            [
                {
                    "node": {
                        "id": "PVT_9",
                        "title": "Board",
                        "fields": {"nodes": full_schema_fields()},
                    }
                }
            ],
        )
        found = schema_for(run, project_number=None, project_id="PVT_9")
        self.assertEqual(found.project_id, "PVT_9")
        self.assertIn("blocked_by", found.fields)

    def test_unmapped_concepts_are_absent_from_the_schema(self):
        run = BoardFakeRun().script(
            "BoardSchemaByNumber",
            [
                schema_payload(
                    text_field("F_agent", "Agent"), select_field("F_prio", "Priority", [])
                )
            ],
        )
        found = schema_for(
            run,
            status_column="",
            area_column="",
            pool_column="",
            blocked_by_column="",
            status_field="",
        )
        self.assertNotIn("area", found.fields)
        self.assertNotIn("pool", found.fields)
        self.assertNotIn("blocked_by", found.fields)
        self.assertNotIn("board_status", found.fields)
        self.assertNotIn("status", found.fields)

    def test_missing_project_is_a_tracker_error(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [{"user": None, "organization": None}])
        with self.assertRaises(iface.TrackerError):
            schema_for(run)

    def test_project_on_both_owners_is_a_tracker_error(self):
        payload = schema_payload(*full_schema_fields())
        payload["organization"] = payload["user"]
        run = BoardFakeRun().script("BoardSchemaByNumber", [payload])
        with self.assertRaises(iface.TrackerError):
            schema_for(run)

    def test_missing_mapped_field_is_a_tracker_error(self):
        fields = [f for f in full_schema_fields() if f["name"] != "Priority"]
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*fields)])
        with self.assertRaisesRegex(iface.TrackerError, "Priority"):
            schema_for(run)

    def test_duplicate_field_names_are_a_tracker_error(self):
        fields = full_schema_fields() + [text_field("F_dup", "Agent")]
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*fields)])
        with self.assertRaises(iface.TrackerError):
            schema_for(run)

    def test_wrong_field_kind_is_a_tracker_error(self):
        fields = [
            select_field("F_agent", "Agent", [("o1", "Alice")]) if f["name"] == "Agent" else f
            for f in full_schema_fields()
        ]
        run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*fields)])
        with self.assertRaises(iface.TrackerError):
            schema_for(run)

    def test_non_text_pairs_rejected_for_text_concepts(self):
        bad_agent = [
            {
                "__typename": "ProjectV2IterationField",
                "id": "F_agent",
                "name": "Agent",
                "dataType": "TEXT",
            },
            text_field("F_agent", "Agent", datatype="NUMBER"),
        ]
        for agent in bad_agent:
            with self.subTest(agent=agent):
                fields = [agent if f["name"] == "Agent" else f for f in full_schema_fields()]
                run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*fields)])
                with self.assertRaises(iface.TrackerError):
                    schema_for(run)

    def test_non_select_pairs_rejected_for_select_concepts(self):
        bad_priority = [
            text_field("F_prio", "Priority"),
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": "F_prio",
                "name": "Priority",
                "dataType": "TEXT",
                "options": [{"id": "o1", "name": "P0"}],
            },
            {
                "__typename": "ProjectV2IterationField",
                "id": "F_prio",
                "name": "Priority",
                "dataType": "SINGLE_SELECT",
            },
        ]
        for priority in bad_priority:
            with self.subTest(priority=priority):
                fields = [priority if f["name"] == "Priority" else f for f in full_schema_fields()]
                run = BoardFakeRun().script("BoardSchemaByNumber", [schema_payload(*fields)])
                with self.assertRaises(iface.TrackerError):
                    schema_for(run)

    def test_malformed_schema_shapes_fail_closed(self):
        good = full_schema_fields()
        bad_shapes = [
            {"user": {"projectV2": None}, "organization": None},
            {"user": {"projectV2": {"id": "", "fields": {"nodes": good}}}, "organization": None},
            {
                "user": {"projectV2": {"id": "PVT_1", "fields": {"nodes": "nope"}}},
                "organization": None,
            },
            {
                "user": {"projectV2": {"id": "PVT_1", "fields": {"nodes": [{"id": "F_agent"}]}}},
                "organization": None,
            },
            {
                "user": {
                    "projectV2": {
                        "id": "PVT_1",
                        "fields": {
                            "nodes": [
                                text_field("F_agent", "Agent"),
                                {
                                    "__typename": "ProjectV2SingleSelectField",
                                    "id": "F_prio",
                                    "name": "Priority",
                                    "dataType": "SINGLE_SELECT",
                                    "options": [{"id": "o1"}],
                                },
                            ]
                        },
                    }
                },
                "organization": None,
            },
        ]
        for payload in bad_shapes:
            with self.subTest(payload=payload):
                run = BoardFakeRun().script("BoardSchemaByNumber", [payload])
                with self.assertRaises(iface.TrackerError):
                    schema_for(run)

    def test_graphql_errors_fail_the_schema(self):
        run = BoardFakeRun().script(
            "BoardSchemaByNumber",
            [{"errors": [{"message": "boom"}], "data": None}],
        )
        with self.assertRaises(iface.TrackerError):
            schema_for(run)

    def test_rate_limit_maps_to_tracker_rate_limited(self):
        run = BoardFakeRun().script(
            "BoardSchemaByNumber",
            [gh.GhError("unavailable", "API rate limit exceeded for user")],
        )
        with self.assertRaises(iface.TrackerRateLimited):
            schema_for(run)

    def test_run_failure_is_a_tracker_error(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [gh.GhError("other", "nope")])
        with self.assertRaises(iface.TrackerError):
            schema_for(run)

    def test_non_json_answer_is_a_tracker_error(self):
        run = lambda args, **kwargs: "not json{{{"
        with self.assertRaises(iface.TrackerError):
            reader_for(run).schema()

    def test_list_shaped_answer_is_a_tracker_error(self):
        run = lambda args, **kwargs: "[1, 2]"
        with self.assertRaises(iface.TrackerError):
            reader_for(run).schema()


class PollPagingTest(unittest.TestCase):
    def setUp(self):
        self.fields = full_schema_fields()

    def poller(self, run, **over):
        reader = reader_for(run, **over)
        return reader, reader.schema()

    def test_single_short_page_needs_no_after(self):
        run = BoardFakeRun()
        rest_issue(run, 7)
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload([item_node(7)])])
        reader, schema = self.poller(run)
        found, cursor = reader.poll(schema, max_pages=10)
        self.assertEqual([item.number for item in found], [7])
        self.assertEqual(run.graphql_variables("BoardItems"), [{"project": "PVT_1"}])

    def test_full_first_page_follows_after(self):
        run = BoardFakeRun()
        first = [item_node(n, updated=T0) for n in range(1, 101)]
        run.rest_fallback = lambda path: json.dumps(item_payload(int(path.rsplit("/", 1)[-1])))
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script(
            "BoardItems",
            [
                items_payload(first, end="C1", more=True),
                items_payload([item_node(101)]),
            ],
        )
        reader, schema = self.poller(run)
        found, cursor = reader.poll(schema, max_pages=10)
        self.assertEqual(len(found), 101)
        self.assertIsNone(cursor)
        variables = run.graphql_variables("BoardItems")
        self.assertEqual(variables, [{"project": "PVT_1"}, {"project": "PVT_1", "after": "C1"}])

    def test_short_page_ends_even_when_more_is_claimed(self):
        run = BoardFakeRun()
        rest_issue(run, 7)
        rest_issue(run, 8)
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload([item_node(7), item_node(8)], end="C1", more=True)])
        reader, schema = self.poller(run)
        found, _ = reader.poll(schema, max_pages=10)
        self.assertEqual([item.number for item in found], [7, 8])
        self.assertEqual(len(run.graphql_variables("BoardItems")), 1)

    def test_missing_page_info_ends_the_poll(self):
        run = BoardFakeRun()
        rest_issue(run, 7)
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [{"node": {"items": {"nodes": [item_node(7)]}}}])
        reader, schema = self.poller(run)
        found, _ = reader.poll(schema, max_pages=10)
        self.assertEqual([item.number for item in found], [7])

    def test_max_pages_returns_what_it_has_with_the_resume_cursor(self):
        run = BoardFakeRun()
        run.rest_fallback = lambda path: json.dumps(item_payload(int(path.rsplit("/", 1)[-1])))
        first = [item_node(n, updated=T0) for n in range(1, 101)]
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload(first, end="C1", more=True)])
        reader, schema = self.poller(run)
        found, cursor = reader.poll(schema, max_pages=1)
        self.assertEqual(len(found), 100)
        self.assertEqual(cursor, "C1")

    def test_malformed_page_fails_closed_never_partial(self):
        bad_pages = [
            {
                "node": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": "nope",
                    }
                }
            },
            {
                "node": {
                    "items": {"pageInfo": {"hasNextPage": "yes", "endCursor": None}, "nodes": []}
                }
            },
            {
                "node": {
                    "items": {"pageInfo": {"hasNextPage": True, "endCursor": None}, "nodes": []}
                }
            },
            {"node": None},
            {
                "node": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"id": "PVTI_x"}],
                    }
                }
            },
        ]
        for page in bad_pages:
            with self.subTest(page=page):
                run = BoardFakeRun()
                run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
                run.script("BoardItems", [page])
                reader, schema = self.poller(run)
                with self.assertRaises(iface.TrackerError):
                    reader.poll(schema, max_pages=10)


class PollItemsTest(unittest.TestCase):
    def setUp(self):
        self.fields = full_schema_fields()

    def poll_one(self, run, values, number=7, node_updated=T1, **issue_over):
        rest_issue(run, number, **issue_over)
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script(
            "BoardItems", [items_payload([item_node(number, updated=node_updated, values=values)])]
        )
        reader = reader_for(run)
        found, _ = reader.poll(reader.schema(), max_pages=10)
        self.assertEqual(len(found), 1)
        return found[0]

    def test_board_concepts_land_in_extra(self):
        run = BoardFakeRun()
        found = self.poll_one(
            run,
            [
                select_value("F_prio", "Priority", "opt-p0-id", "P0"),
                select_value("F_area", "Area", "opt-a1", "Backend"),
                select_value("F_pool", "Pool", "opt-q1", "Dispatch"),
                select_value("F_wf", "Status", "opt-s1", "In progress"),
            ],
        )
        self.assertEqual(found.extra["priority"], "urgent")
        self.assertEqual(found.extra["area"], "Backend")
        self.assertEqual(found.extra["pool"], "Dispatch")
        self.assertEqual(found.extra["board_status"], "In progress")
        self.assertEqual(found.priority, "high")

    def test_option_ids_resolve_live_not_by_name(self):
        run = BoardFakeRun()
        found = self.poll_one(
            run,
            [
                select_value("F_prio", "Priority", "opt-p1-id", "Renamed display"),
            ],
        )
        self.assertEqual(found.extra["priority"], "high")

    def test_unknown_option_id_keeps_the_concept_absent(self):
        run = BoardFakeRun()
        found = self.poll_one(
            run,
            [
                select_value("F_prio", "Priority", "opt-deleted", "P9"),
            ],
        )
        self.assertNotIn("priority", found.extra)

    def test_corral_text_fields_become_agent_and_status(self):
        run = BoardFakeRun()
        found = self.poll_one(
            run,
            [
                text_value("F_agent", "Agent", "agent-1"),
                text_value("F_status", "Agent state", "working"),
            ],
            labels=[{"name": "ready"}, {"name": "agent:stale"}],
        )
        self.assertEqual(found.agent, "agent-1")
        self.assertEqual(found.status, "working")

    def test_empty_text_fields_read_as_empty(self):
        run = BoardFakeRun()
        found = self.poll_one(run, [text_value("F_agent", "Agent", "  ")])
        self.assertEqual(found.agent, "")

    def test_blocked_by_parses_strictly(self):
        run = BoardFakeRun()
        found = self.poll_one(
            run,
            [
                text_value("F_blocked", "Blocked by", "example/demo#1, other/repo#22"),
            ],
        )
        self.assertEqual(found.extra["blocked_by"], ["example/demo#1", "other/repo#22"])

    def test_unparseable_blocked_by_is_carried_raw(self):
        run = BoardFakeRun()
        found = self.poll_one(
            run,
            [
                text_value("F_blocked", "Blocked by", "example/demo#1, someday maybe"),
            ],
        )
        self.assertEqual(found.extra["blocked_by"], "example/demo#1, someday maybe")

    def test_empty_blocked_by_clears_the_list(self):
        run = BoardFakeRun()
        found = self.poll_one(run, [text_value("F_blocked", "Blocked by", "  ")])
        self.assertEqual(found.extra["blocked_by"], [])

    def test_milestone_comes_from_the_issue_payload(self):
        run = BoardFakeRun()
        found = self.poll_one(run, [], milestone={"title": "M1"})
        self.assertEqual(found.extra["milestone"], "M1")

    def test_missing_milestone_clears_the_concept(self):
        run = BoardFakeRun()
        found = self.poll_one(run, [], milestone=None)
        self.assertIn("milestone", found.extra)
        self.assertIsNone(found.extra["milestone"])

    def test_board_updated_at_is_the_max_of_both_times(self):
        run = BoardFakeRun()
        later = self.poll_one(run, [], node_updated="2026-10-28T15:00:00Z", updated_at=T0)
        want = float(calendar.timegm((2026, 10, 28, 15, 0, 0, 0, 0, 0)))
        self.assertEqual(later.updated_at, want)
        run = BoardFakeRun()
        earlier = self.poll_one(run, [], node_updated=T0, updated_at=T1)
        want = datetime.datetime(2026, 10, 28, 13, tzinfo=datetime.timezone.utc)
        self.assertEqual(earlier.updated_at, want.timestamp())

    def test_draft_items_are_skipped(self):
        run = BoardFakeRun()
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload([item_node(0, typename="DraftIssue")])])
        reader = reader_for(run)
        found, _ = reader.poll(reader.schema(), max_pages=10)
        self.assertEqual(found, [])

    def test_deleted_content_fails_closed(self):
        run = BoardFakeRun()
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        node = item_node(7)
        node["content"] = None
        run.script("BoardItems", [items_payload([node])])
        reader = reader_for(run)
        with self.assertRaises(iface.TrackerError):
            reader.poll(reader.schema(), max_pages=10)

    def test_pull_requests_read_the_fork_flag(self):
        run = BoardFakeRun()
        payload = item_payload(9, pull_request={})
        run.add_rest("repos/example/demo/issues/9", payload)
        run.add_rest("repos/example/demo/pulls/9", pull_payload(9, fork=True))
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload([item_node(9, typename="PullRequest")])])
        reader = reader_for(run)
        found, _ = reader.poll(reader.schema(), max_pages=10)
        self.assertEqual(found[0].kind, "pr")
        self.assertTrue(found[0].from_fork)

    def test_rest_failure_fails_the_poll(self):
        run = BoardFakeRun()
        run.fail_rest("repos/example/demo/issues/7", RuntimeError("gone"))
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload([item_node(7)])])
        reader = reader_for(run)
        with self.assertRaises(iface.TrackerError):
            reader.poll(reader.schema(), max_pages=10)

    def test_wrong_value_kind_for_a_concept_fails_closed(self):
        run = BoardFakeRun()
        found_values = [text_value("F_prio", "Priority", "urgent")]
        rest_issue(run, 7)
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload([item_node(7, values=found_values)])])
        reader = reader_for(run)
        with self.assertRaises(iface.TrackerError):
            reader.poll(reader.schema(), max_pages=10)

    def test_select_value_missing_option_id_reads_as_absent(self):
        run = BoardFakeRun()
        entry = {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "field": {
                "__typename": "ProjectV2SingleSelectField",
                "id": "F_prio",
                "name": "Priority",
            },
        }
        found = self.poll_one(run, [entry])
        self.assertNotIn("priority", found.extra)

    def test_select_value_null_option_id_reads_as_absent(self):
        run = BoardFakeRun()
        found = self.poll_one(run, [select_value("F_prio", "Priority", None, "P0")])
        self.assertNotIn("priority", found.extra)

    def test_unknown_field_ids_are_ignored(self):
        run = BoardFakeRun()
        found = self.poll_one(run, [text_value("F_other", "Other", "x")])
        self.assertEqual({k: v for k, v in found.extra.items() if k != "milestone"}, {})

    def test_extra_dicts_are_not_shared(self):
        run = BoardFakeRun()
        rest_issue(run, 7)
        rest_issue(run, 8)
        run.script("BoardSchemaByNumber", [schema_payload(*self.fields)])
        run.script("BoardItems", [items_payload([item_node(7), item_node(8)])])
        reader = reader_for(run)
        found, _ = reader.poll(reader.schema(), max_pages=10)
        self.assertIsNot(found[0].extra, found[1].extra)

    def test_store_round_trip_keeps_board_concepts(self):
        from corral import sync

        run = BoardFakeRun()
        found = self.poll_one(
            run,
            [
                select_value("F_prio", "Priority", "opt-p0-id", "P0"),
                text_value("F_blocked", "Blocked by", "example/demo#1"),
            ],
        )
        tmpdir = tempfile.mkdtemp(prefix="corral-board-reader-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        with store.open_store(os.path.join(tmpdir, "board.sqlite")) as db:
            db.upsert_item(sync._reading(found), operators=["alice"], now=found.updated_at)
            row, _ = db.item("example/demo", 7, now=found.updated_at)
        self.assertEqual(row.extra["priority"], "urgent")
        self.assertEqual(row.extra["blocked_by"], ["example/demo#1"])


class MilestoneOfTest(unittest.TestCase):
    def test_title(self):
        from corral.github.tracker import milestone_of

        self.assertEqual(milestone_of(item_payload(7, milestone={"title": "M1"})), "M1")

    def test_missing_or_null_is_none(self):
        from corral.github.tracker import milestone_of

        self.assertIsNone(milestone_of(item_payload(7)))
        self.assertIsNone(milestone_of(item_payload(7, milestone=None)))

    def test_malformed_milestones_fail(self):
        from corral.github.tracker import milestone_of

        for milestone in ("M1", {"title": ""}, {"title": 3}, {"name": "M1"}):
            with self.subTest(milestone=milestone):
                with self.assertRaises(iface.TrackerError):
                    milestone_of(item_payload(7, milestone=milestone))


class BlockedByParseTest(unittest.TestCase):
    def test_refs(self):
        self.assertEqual(
            boardreader.parse_blocked_by("example/demo#1, other/repo#22"),
            ["example/demo#1", "other/repo#22"],
        )

    def test_blank_clears(self):
        self.assertEqual(boardreader.parse_blocked_by("  "), [])

    def test_garbage_is_raw(self):
        self.assertEqual(boardreader.parse_blocked_by("someday"), "someday")
        self.assertEqual(boardreader.parse_blocked_by("a/b#1, nope"), "a/b#1, nope")
        self.assertEqual(boardreader.parse_blocked_by("a/b#0"), "a/b#0")
        self.assertEqual(boardreader.parse_blocked_by("a/b#"), "a/b#")
        self.assertEqual(boardreader.parse_blocked_by("a#1"), "a#1")


if __name__ == "__main__":
    unittest.main()
