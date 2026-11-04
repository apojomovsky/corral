"""Ticket 2: the GitHub board provisioner and its factory.

Canned payloads only: ``ProvisionFakeRun`` answers GraphQL bodies and
REST routes at the injected ``run`` seam, and ``FakePacer`` stands in
for the shared bucket. Importing ``tests.test_board_fields`` installs
the suite-wide tripwire that fails the run if any test reaches the
default pacer file or a real subprocess. No test reaches a real ``gh``,
the network, or a real board.
"""

import json
import unittest

from corral import tracker as iface
from corral.github import gh
from corral.github.board import GithubBoardProvisioner
from tests.test_board_fields import FakePacer
from tests.test_board_planner import spec_for, table


def text_node(fid, name):
    return {"__typename": "ProjectV2Field", "id": fid, "name": name, "dataType": "TEXT"}


def select_node(fid, name, options):
    return {
        "__typename": "ProjectV2SingleSelectField",
        "id": fid,
        "name": name,
        "dataType": "SINGLE_SELECT",
        "options": [
            {"id": oid, "name": oname, "color": color, "description": desc}
            for oid, oname, color, desc in options
        ],
    }


def project_node(
    pid="PVT_1",
    number=3,
    title="demo board",
    public=False,
    fields=(),
    repos=("example/demo",),
):
    return {
        "id": pid,
        "number": number,
        "title": title,
        "public": public,
        "repositories": {"nodes": [{"nameWithOwner": name} for name in repos]},
        "fields": {"nodes": list(fields)},
    }


PRIORITY_OPTIONS = (
    ("opt-1", "Urgent", "RED", ""),
    ("opt-2", "High", "ORANGE", ""),
    ("opt-3", "Normal", "YELLOW", ""),
    ("opt-4", "Low", "GREEN", ""),
    ("opt-5", "Backlog", "GREY", ""),
)

STATUS_OPTIONS = (
    ("st-1", "Todo", "GREEN", ""),
    ("st-2", "In Progress", "YELLOW", ""),
    ("st-3", "Done", "PURPLE", ""),
)


def standard_field_nodes():
    """The live fields of an exactly-standard board, as raw nodes."""
    return [
        text_node("F_agent", "Agent"),
        text_node("F_status", "Agent state"),
        select_node("F_board", "Status", STATUS_OPTIONS),
        select_node("F_prio", "Priority", PRIORITY_OPTIONS),
        text_node("F_blocked", "Blocked by"),
    ]


def owner_body(kind, oid="U_1", login="example"):
    """A full owner-resolution body: data plus the expected NOT_FOUND noise."""
    user = {"id": oid} if kind in ("user", "both") else None
    org = {"id": oid} if kind in ("organization", "both") else None
    errors = []
    if kind in ("organization", "unknown"):
        errors.append(
            {
                "type": "NOT_FOUND",
                "path": ["user"],
                "message": f"Could not resolve to a User with the login of '{login}'.",
            }
        )
    if kind in ("user", "unknown"):
        errors.append(
            {
                "type": "NOT_FOUND",
                "path": ["organization"],
                "message": f"Could not resolve to an Organization with the login of '{login}'.",
            }
        )
    return {"data": {"user": user, "organization": org}, "errors": errors}


def titles_payload(*boards, half="user"):
    """A title-list answer: ``boards`` are ``(id, number, title)``."""
    other = "organization" if half == "user" else "user"
    return {
        half: {
            "projectsV2": {
                "nodes": [{"id": pid, "number": num, "title": title} for pid, num, title in boards],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        },
        other: None,
    }


class ProvisionFakeRun:
    """A ``gh`` seam double: scripted GraphQL bodies plus REST routes."""

    def __init__(self):
        self.calls = []
        self.scripted = {}
        self.rest = {}

    def script(self, marker, payloads):
        self.scripted[marker] = list(payloads)
        return self

    def add_rest(self, path, payload):
        self.rest[path] = payload
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
        raise RuntimeError(f"no route for {path}")

    def _graphql(self, args):
        query = ""
        toks = args[2:]
        i = 0
        while i < len(toks):
            if toks[i] in ("-f", "-F") and i + 1 < len(toks):
                key, _, value = toks[i + 1].partition("=")
                if key == "query":
                    query = value
                i += 2
            else:
                i += 1
        for marker, payloads in self.scripted.items():
            if marker in query:
                if not payloads:
                    raise RuntimeError(f"no more {marker} payloads")
                answer = payloads.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                if isinstance(answer, dict) and ("data" in answer or "errors" in answer):
                    return json.dumps(answer)
                return json.dumps({"data": answer})
        raise RuntimeError(f"no scripted payload for query {query[:60]!r}")

    def queries(self):
        """Every GraphQL query string sent, in call order."""
        found = []
        for args, _ in self.calls:
            if args[1] != "graphql":
                continue
            toks = args[2:]
            i = 0
            while i < len(toks):
                if toks[i] in ("-f", "-F") and i + 1 < len(toks):
                    key, _, value = toks[i + 1].partition("=")
                    if key == "query":
                        found.append(value)
                    i += 2
                else:
                    i += 1
        return found

    def mutations(self):
        """The GraphQL queries that mutate, in call order."""
        return [query for query in self.queries() if "mutation" in query]

    def has_mutation(self, marker):
        return any(marker in query for query in self.mutations())

    def flag_values(self, marker, prefix):
        """Ordered ``key=value`` flags under one mutation marker."""
        values = []
        for query_args, _ in self.calls:
            toks = query_args[2:]
            i = 0
            query = ""
            while i < len(toks):
                if toks[i] in ("-f", "-F") and i + 1 < len(toks):
                    key, _, value = toks[i + 1].partition("=")
                    if key == "query":
                        query = value
                    i += 2
                else:
                    i += 1
            if marker not in query:
                continue
            i = 0
            while i < len(toks):
                if toks[i] in ("-f", "-F") and i + 1 < len(toks):
                    key, _, value = toks[i + 1].partition("=")
                    if key.startswith(prefix):
                        values.append(f"{key}={value}")
                    i += 2
                else:
                    i += 1
        return values


def provisioner_for(run, **over):
    """A provisioner on one fake run, defaulting to an addressed spec."""
    return GithubBoardProvisioner(run, pacer=FakePacer()), spec_for(table(), **over)


def bootstrap_provisioner_for(run, **flags):
    """A provisioner on one fake run with a bootstrap (table-less) spec."""
    spec = iface.effective_spec(variant="board", board=None, repo="example/demo", **flags)
    return GithubBoardProvisioner(run, pacer=FakePacer()), spec


def by_id(*nodes):
    """Board-by-id payloads: the query nests the project under ``node``."""
    return [{"node": node} for node in nodes]


def by_number(project):
    """A board-by-number payload: the project nests under ``projectV2``."""
    return {"user": {"projectV2": project}, "organization": None}


def script_full_create(run, owner="user", public=False, repos=("example/demo",)):
    """Script the happy path: user owner, no title match, fresh create."""
    run.script("OwnerResolve", [owner_body(owner)])
    run.script("BoardTitles", [titles_payload()])
    run.add_rest("repos/example/demo", {"node_id": "R_1"})
    run.script(
        "BoardCreate",
        [
            {
                "createProjectV2": {
                    "projectV2": {
                        "id": "PVT_9",
                        "number": 7,
                        "title": "demo board",
                        "public": public,
                    }
                }
            }
        ],
    )
    return run


class OwnerResolutionTests(unittest.TestCase):
    def test_unknown_owner_is_refused_before_any_mutation(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("unknown")])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(ValueError):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])
        self.assertEqual(len(run.scripted["OwnerResolve"]), 0)

    def test_owner_half_missing_its_id_is_unusable(self):
        body = {"data": {"user": {}, "organization": None}, "errors": []}
        run = ProvisionFakeRun().script("OwnerResolve", [body])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerError):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])

    def test_owner_resolving_to_both_halves_is_refused(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("both")])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(ValueError):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])

    def test_production_style_raise_with_a_body_still_provisions(self):
        body = owner_body("user")
        run = script_full_create(ProvisionFakeRun())
        run.scripted["OwnerResolve"] = [
            gh.GhError("other", "gh: Could not resolve to an Organization", output=json.dumps(body))
        ]
        run.script(
            "BoardById",
            by_id(
                project_node(
                    pid="PVT_9", number=7, fields=[select_node("F_board", "Status", STATUS_OPTIONS)]
                ),
                project_node(pid="PVT_9", number=7, fields=standard_field_nodes()),
            ),
        )
        run.script(
            "TextFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Agent", "Agent state", "Blocked by")
            ],
        )
        run.script(
            "SelectFieldCreate",
            [{"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": "Priority"}}}],
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertIn("project", report.created)
        self.assertTrue(run.has_mutation("BoardCreate"))


class FindBoardTests(unittest.TestCase):
    def test_renamed_board_resolves_by_address_without_title_search(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script(
            "BoardByNumber",
            [by_number(project_node(title="Old name", fields=standard_field_nodes()))],
        )
        run.script(
            "BoardById", by_id(project_node(title="Old name", fields=standard_field_nodes()))
        )
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())
        self.assertFalse(any("BoardTitles" in query for query in run.queries()))
        self.assertFalse(any("BoardCreate" in query for query in run.queries()))

    def test_addressed_board_by_node_id_never_queries_by_number(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script(
            "BoardById",
            by_id(
                project_node(pid="PVT_x", fields=standard_field_nodes()),
                project_node(pid="PVT_x", fields=standard_field_nodes()),
            ),
        )
        provisioner = GithubBoardProvisioner(run, pacer=FakePacer())
        spec = spec_for(table(project_number=None, project_id="PVT_x"))
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())
        self.assertEqual(report.project_id, "PVT_x")
        self.assertFalse(any("BoardByNumber" in query for query in run.queries()))

    def test_addressed_board_that_no_longer_resolves_is_refused(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script("BoardByNumber", [{"user": {"projectV2": None}, "organization": None}])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(ValueError) as caught:
            provisioner.ensure_board(spec)
        self.assertIn("no longer exists", str(caught.exception))
        self.assertIn("remove the table and re-run", str(caught.exception))
        self.assertEqual(run.mutations(), [])
        self.assertFalse(any("BoardTitles" in query for query in run.queries()))
        self.assertFalse(any("BoardCreate" in query for query in run.queries()))

    def test_missing_board_under_an_unknown_owner_is_refused(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("unknown")])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(ValueError) as caught:
            provisioner.ensure_board(spec)
        self.assertIn("example", str(caught.exception))
        self.assertEqual(run.mutations(), [])

    def test_two_same_title_boards_name_every_number(self):
        run = script_full_create(ProvisionFakeRun())
        run.scripted["BoardTitles"] = [
            titles_payload(("PVT_a", 2, "demo board"), ("PVT_b", 5, "demo board"))
        ]
        provisioner, spec = bootstrap_provisioner_for(run)
        with self.assertRaises(ValueError) as caught:
            provisioner.ensure_board(spec)
        self.assertIn("2", str(caught.exception))
        self.assertIn("5", str(caught.exception))
        self.assertEqual(run.mutations(), [])

    def test_title_search_ignores_other_titles(self):
        run = script_full_create(ProvisionFakeRun())
        run.scripted["BoardTitles"] = [titles_payload(("PVT_a", 2, "another board"))]
        run.script(
            "BoardById",
            by_id(
                project_node(
                    pid="PVT_9", number=7, fields=[select_node("F_board", "Status", STATUS_OPTIONS)]
                ),
                project_node(pid="PVT_9", number=7, fields=standard_field_nodes()),
            ),
        )
        run.script(
            "TextFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Agent", "Agent state", "Blocked by")
            ],
        )
        run.script(
            "SelectFieldCreate",
            [{"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": "Priority"}}}],
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertIn("project", report.created)
        self.assertEqual(report.number, 7)

    def test_title_page_without_page_info_fails_closed(self):
        run = script_full_create(ProvisionFakeRun())
        run.scripted["BoardTitles"] = [
            {"user": {"projectsV2": {"nodes": []}}, "organization": None}
        ]
        provisioner, spec = bootstrap_provisioner_for(run)
        with self.assertRaises(iface.TrackerError):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])

    def test_title_search_reads_the_set_half_from_one_query(self):
        from corral.github import board as board_mod

        self.assertFalse(hasattr(board_mod, "_TITLES_USER"))
        self.assertFalse(hasattr(board_mod, "_TITLES_ORG"))
        self.assertIn("user(login", board_mod._TITLES)
        self.assertIn("organization(login", board_mod._TITLES)
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("organization")])
        run.scripted["BoardTitles"] = [
            titles_payload(("PVT_a", 2, "demo board"), half="organization")
        ]
        run.script(
            "BoardById",
            by_id(
                project_node(pid="PVT_a", number=2, fields=standard_field_nodes()),
                project_node(pid="PVT_a", number=2, fields=standard_field_nodes()),
            ),
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())


class FullCreateTests(unittest.TestCase):
    def script_mid_and_final(self, run, mid_fields, final_fields=None):
        mid = project_node(pid="PVT_9", number=7, fields=mid_fields)
        final = project_node(
            pid="PVT_9", number=7, fields=final_fields if final_fields is not None else mid_fields
        )
        run.script("BoardById", by_id(mid, final))
        return run

    def test_fresh_board_reports_created_kept_and_map(self):
        run = script_full_create(ProvisionFakeRun())
        builtin = [select_node("F_board", "Status", STATUS_OPTIONS)]
        self.script_mid_and_final(run, builtin, standard_field_nodes())
        run.script(
            "TextFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Agent", "Agent state", "Blocked by")
            ],
        )
        run.script(
            "SelectFieldCreate",
            [{"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": "Priority"}}}],
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(
            report.created,
            (
                "project",
                "field:Agent",
                "field:Agent state",
                "field:Priority",
                "field:Blocked by",
            ),
        )
        self.assertEqual(report.kept, ("link:example/demo", "field:Status"))
        self.assertEqual(report.refused, ())
        self.assertEqual(report.owner, "example")
        self.assertEqual(report.number, 7)
        self.assertEqual(report.project_id, "PVT_9")
        self.assertEqual(
            [field.concept for field in report.fields],
            ["agent", "status", "board_status", "priority", "blocked_by"],
        )
        self.assertEqual(
            report.priority_map,
            {
                "opt-1": "urgent",
                "opt-2": "high",
                "opt-3": "normal",
                "opt-4": "low",
                "opt-5": "backlog",
            },
        )

    def test_create_carries_owner_title_and_repository(self):
        run = script_full_create(ProvisionFakeRun())
        self.script_mid_and_final(run, [], standard_field_nodes())
        run.script(
            "TextFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Agent", "Agent state", "Blocked by")
            ],
        )
        run.script(
            "SelectFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Status", "Priority")
            ],
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        provisioner.ensure_board(spec)
        flags = run.flag_values("BoardCreate", "")
        self.assertIn("ownerId=U_1", flags)
        self.assertIn("title=demo board", flags)
        self.assertIn("repositoryId=R_1", flags)

    def test_created_select_options_carry_color_and_description(self):
        run = script_full_create(ProvisionFakeRun())
        self.script_mid_and_final(run, [], standard_field_nodes())
        run.script(
            "TextFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Agent", "Agent state", "Blocked by")
            ],
        )
        run.script(
            "SelectFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Status", "Priority")
            ],
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        provisioner.ensure_board(spec)
        names = run.flag_values("SelectFieldCreate", "singleSelectOptions[][name]")
        self.assertIn("singleSelectOptions[][name]=Urgent", names)
        self.assertIn("singleSelectOptions[][name]=Todo", names)
        colors = run.flag_values("SelectFieldCreate", "singleSelectOptions[][color]")
        self.assertTrue(colors)
        self.assertTrue(all(value.split("=", 1)[1] for value in colors))
        descs = run.flag_values("SelectFieldCreate", "singleSelectOptions[][description]")
        self.assertEqual(len(descs), len(names))

    def test_create_without_a_repo_sends_no_link(self):
        run = script_full_create(ProvisionFakeRun())
        run.rest = {}
        self.script_mid_and_final(run, [], [])
        provisioner, spec = bootstrap_provisioner_for(run)
        spec = spec._replace(repo="")
        run.script(
            "TextFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Agent", "Agent state", "Blocked by")
            ],
        )
        run.script(
            "SelectFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Status", "Priority")
            ],
        )
        report = provisioner.ensure_board(spec)
        self.assertIn("project", report.created)
        self.assertFalse([entry for entry in report.created if entry.startswith("link:")])
        self.assertFalse([entry for entry in report.kept if entry.startswith("link:")])
        self.assertFalse(run.has_mutation("BoardLinkRepo"))
        self.assertEqual([args for args, _ in run.calls if args[1] != "graphql"], [])

    def test_rerun_over_an_exact_board_sends_no_mutation(self):
        run = addressed_run(*standard_field_nodes())
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())
        self.assertEqual(run.mutations(), [])
        self.assertIn("field:Priority", report.kept)
        self.assertIn("link:example/demo", report.kept)

    def test_existing_field_is_never_updated_or_deleted(self):
        run = addressed_run(*standard_field_nodes())
        provisioner, spec = provisioner_for(run)
        provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])
        self.assertFalse(run.has_mutation("OptionsAppend"))


class VisibilityTests(unittest.TestCase):
    def test_make_private_error_stops_before_link_and_fields(self):
        run = script_full_create(ProvisionFakeRun(), public=True)
        run.scripted["BoardCreate"] = [
            {
                "createProjectV2": {
                    "projectV2": {"id": "PVT_9", "number": 7, "title": "demo board", "public": True}
                }
            }
        ]
        run.script("BoardById", by_id(project_node(pid="PVT_9", number=7, public=True)))
        run.script("BoardMakePrivate", [gh.GhError("other", "boom")])
        provisioner, spec = bootstrap_provisioner_for(run)
        with self.assertRaises(iface.TrackerError):
            provisioner.ensure_board(spec)
        self.assertFalse(run.has_mutation("BoardLinkRepo"))
        self.assertFalse(run.has_mutation("FieldCreate"))

    def test_still_public_after_update_is_refused_before_link_and_fields(self):
        run = script_full_create(ProvisionFakeRun(), public=True)
        run.scripted["BoardCreate"] = [
            {
                "createProjectV2": {
                    "projectV2": {"id": "PVT_9", "number": 7, "title": "demo board", "public": True}
                }
            }
        ]
        run.script(
            "BoardById",
            by_id(
                project_node(pid="PVT_9", number=7, public=True),
                project_node(pid="PVT_9", number=7, public=True),
            ),
        )
        run.script(
            "BoardMakePrivate",
            [{"updateProjectV2": {"projectV2": {"id": "PVT_9", "public": True}}}],
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("private", report.refused[0])
        self.assertFalse(run.has_mutation("BoardLinkRepo"))
        self.assertFalse(run.has_mutation("FieldCreate"))
        self.assertEqual(report.created, ("project",))

    def test_still_public_after_update_keeps_the_atomic_link(self):
        run = script_full_create(ProvisionFakeRun(), public=True)
        run.scripted["BoardCreate"] = [
            {
                "createProjectV2": {
                    "projectV2": {"id": "PVT_9", "number": 7, "title": "demo board", "public": True}
                }
            }
        ]
        run.script(
            "BoardById",
            by_id(
                project_node(pid="PVT_9", number=7, public=True),
                project_node(pid="PVT_9", number=7, public=True),
            ),
        )
        run.script(
            "BoardMakePrivate",
            [{"updateProjectV2": {"projectV2": {"id": "PVT_9", "public": True}}}],
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ("project",))
        self.assertEqual(report.kept, ("link:example/demo",))

    def test_addressed_public_board_is_refused_but_continues(self):
        run = addressed_run(*standard_field_nodes(), public=True)
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("private", report.refused[0])
        self.assertIn("field:Priority", report.kept)

    def test_title_found_public_board_runs_link_then_stops_before_fields(self):
        run = script_full_create(ProvisionFakeRun())
        run.scripted["BoardTitles"] = [titles_payload(("PVT_a", 2, "demo board"))]
        run.script(
            "BoardById",
            by_id(project_node(pid="PVT_a", number=2, public=True, fields=standard_field_nodes())),
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("private", report.refused[0])
        self.assertIn("link:example/demo", report.kept)
        self.assertFalse(run.has_mutation("FieldCreate"))
        self.assertFalse(run.has_mutation("OptionsAppend"))


class LinkTests(unittest.TestCase):
    def test_link_refused_when_the_account_may_not_administer(self):
        run = addressed_run(*standard_field_nodes(), repos=())
        run.add_rest("repos/example/demo", {"node_id": "R_1"})
        run.script("BoardLinkRepo", [gh.GhError("other", "gh: Must have admin rights (HTTP 403)")])
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("example/demo", report.refused[0])
        self.assertIn("field:Priority", report.kept)
        self.assertNotIn("link:example/demo", report.created)

    def test_link_error_other_than_permission_fails_closed(self):
        run = addressed_run(*standard_field_nodes(), repos=())
        run.add_rest("repos/example/demo", {"node_id": "R_1"})
        run.script("BoardLinkRepo", [gh.GhError("other", "boom (HTTP 500)")])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerError):
            provisioner.ensure_board(spec)

    def test_repo_fetch_refused_when_the_repo_does_not_resolve(self):
        run = addressed_run(*standard_field_nodes(), repos=())
        run.add_rest("repos/example/demo", gh.GhError("other", "Not Found (HTTP 404)"))
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("example/demo", report.refused[0])
        self.assertFalse(run.has_mutation("BoardLinkRepo"))
        self.assertIn("field:Priority", report.kept)

    def test_link_forbidden_body_is_refused_without_the_rest_shape(self):
        body = {
            "data": {"linkProjectV2ToRepository": None},
            "errors": [{"type": "FORBIDDEN", "message": "nope"}],
        }
        run = addressed_run(*standard_field_nodes(), repos=())
        run.add_rest("repos/example/demo", {"node_id": "R_1"})
        run.script(
            "BoardLinkRepo",
            [gh.GhError("other", "generic failure", output=json.dumps(body))],
        )
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("example/demo", report.refused[0])
        self.assertNotIn("link:example/demo", report.created)
        self.assertIn("field:Priority", report.kept)


def addressed_run(*fields, repos=("example/demo",), public=False, final_fields=None):
    """A fake run resolving an addressed board holding ``fields``.

    Scripts the addressed fetch plus the final read-back (the
    provisioner always re-reads before reporting); ``final_fields``
    overrides what the read-back shows.
    """
    run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
    run.script(
        "BoardByNumber",
        [by_number(project_node(repos=repos, public=public, fields=list(fields)))],
    )
    final = list(final_fields) if final_fields is not None else list(fields)
    run.script("BoardById", by_id(project_node(repos=repos, public=public, fields=final)))
    return run


class FieldEnsureTests(unittest.TestCase):
    def test_missing_ranks_append_with_the_live_list_resent(self):
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            select_node(
                "F_prio",
                "Priority",
                [
                    ("opt-1", "Urgent", "RED", ""),
                    ("opt-2", "High", "ORANGE", ""),
                    ("opt-3", "Normal", "YELLOW", ""),
                ],
            ),
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live, final_fields=standard_field_nodes())
        run.script(
            "OptionsAppend",
            [{"updateProjectV2Field": {"projectV2Field": {"id": "F_prio", "name": "Priority"}}}],
        )
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ("option:Priority/Low", "option:Priority/Backlog"))
        self.assertIn("field:Priority", report.kept)
        self.assertEqual(report.refused, ())
        flags = run.flag_values("OptionsAppend", "singleSelectOptions[]")
        id_flags = [flag for flag in flags if "[id]=" in flag]
        self.assertEqual(len(id_flags), 3)
        self.assertIn("singleSelectOptions[][id]=opt-1", flags)
        self.assertIn("singleSelectOptions[][color]=RED", flags)
        self.assertIn("singleSelectOptions[][name]=Low", flags)
        self.assertNotIn("singleSelectOptions[][id]=opt-4", flags)
        self.assertEqual(
            report.priority_map,
            {
                "opt-1": "urgent",
                "opt-2": "high",
                "opt-3": "normal",
                "opt-4": "low",
                "opt-5": "backlog",
            },
        )

    def test_renamed_option_keeps_its_rank_by_id(self):
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            select_node(
                "F_prio",
                "Priority",
                [
                    ("opt-1", "Urgent", "RED", ""),
                    ("opt-2", "P1", "ORANGE", "renamed"),
                    ("opt-3", "Normal", "YELLOW", ""),
                    ("opt-4", "Low", "GREEN", ""),
                    ("opt-5", "Backlog", "GREY", ""),
                ],
            ),
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live)
        provisioner = GithubBoardProvisioner(run, pacer=FakePacer())
        spec = spec_for(table(priority_map={"opt-2": "high"}))
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())
        self.assertFalse(run.has_mutation("OptionsAppend"))
        self.assertEqual(report.priority_map.get("opt-2"), "high")

    def test_kept_status_is_never_topped_up(self):
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", [("s-9", " later", "GRAY", "")]),
            select_node("F_prio", "Priority", PRIORITY_OPTIONS),
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live)
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())
        self.assertFalse(run.has_mutation("OptionsAppend"))

    def test_wrong_kind_is_refused_without_a_lookalike(self):
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            text_node("F_prio", "Priority"),
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live)
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("Priority", report.refused[0])
        self.assertNotIn("field:Priority", report.created)
        self.assertFalse(run.has_mutation("FieldCreate"))

    def test_duplicate_field_names_are_refused_not_unreachable(self):
        live = standard_field_nodes() + [text_node("F_2", "Agent")]
        run = addressed_run(*live)
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertTrue(report.refused)
        self.assertNotIn("field:Agent", report.created)

    def test_duplicate_option_names_are_refused_not_unreachable(self):
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            select_node(
                "F_prio",
                "Priority",
                [("opt-1", "Urgent", "RED", ""), ("opt-9", "Urgent", "BLUE", "")],
            ),
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live)
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertTrue(report.refused)
        self.assertFalse(run.has_mutation("OptionsAppend"))

    def test_keep_only_area_present_is_kept_without_options(self):
        live = standard_field_nodes() + [
            select_node("F_area", "Area", [("a-1", "Mobile", "BLUE", "")])
        ]
        run = addressed_run(*live)
        provisioner = GithubBoardProvisioner(run, pacer=FakePacer())
        spec = spec_for(table(area_column="Area"))
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.refused, ())
        self.assertIn("field:Area", report.kept)
        self.assertIn("area", [field.concept for field in report.fields])

    def test_keep_only_area_absent_names_the_flag(self):
        run = addressed_run(*standard_field_nodes())
        provisioner = GithubBoardProvisioner(run, pacer=FakePacer())
        spec = spec_for(table(area_column="Area"))
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("--area", report.refused[0])
        self.assertNotIn("area", [field.concept for field in report.fields])

    def test_unresendable_live_options_refuse_the_append_by_hand(self):
        options = [
            {"id": "opt-1", "name": "Urgent", "color": None, "description": ""},
            {"id": "opt-2", "name": "High", "color": "ORANGE", "description": ""},
            {"id": "opt-3", "name": "Normal", "color": "YELLOW", "description": ""},
        ]
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": "F_prio",
                "name": "Priority",
                "dataType": "SINGLE_SELECT",
                "options": options,
            },
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live, final_fields=standard_field_nodes())
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(len(report.refused), 2)
        self.assertTrue(all("by hand" in line for line in report.refused))
        self.assertFalse(run.has_mutation("OptionsAppend"))
        self.assertIn("field:Priority", report.kept)

    def test_malformed_schema_is_unusable_before_any_field_mutation(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        bad = project_node()
        bad["fields"] = {"nodes": "nope"}
        run.script("BoardByNumber", [by_number(bad)])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerError):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])


class RateLimitTests(unittest.TestCase):
    def test_rate_limited_owner_query_is_rate_limited(self):
        run = ProvisionFakeRun().script(
            "OwnerResolve",
            [gh.GhError("unavailable", "API rate limit exceeded for user ID 1. (HTTP 403)")],
        )
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerRateLimited):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])

    def test_rate_limited_schema_fetch_is_rate_limited(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script(
            "BoardByNumber",
            [
                gh.GhError(
                    "unavailable",
                    "You have exceeded a secondary rate limit. Please wait a few minutes.",
                )
            ],
        )
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerRateLimited):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])

    def test_rate_limited_body_on_schema_fetch_is_rate_limited(self):
        body = {
            "data": {"node": None},
            "errors": [{"type": "RATE_LIMITED", "message": "slow down"}],
        }
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script(
            "BoardByNumber",
            [gh.GhError("other", "generic failure", output=json.dumps(body))],
        )
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerRateLimited):
            provisioner.ensure_board(spec)
        self.assertEqual(run.mutations(), [])

    def test_rate_limited_body_on_field_create_is_rate_limited(self):
        body = {
            "data": {"createProjectV2Field": None},
            "errors": [{"type": "RATE_LIMITED", "message": "slow down"}],
        }
        live = [
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            select_node("F_prio", "Priority", PRIORITY_OPTIONS),
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live)
        run.script(
            "TextFieldCreate",
            [gh.GhError("other", "generic failure", output=json.dumps(body))],
        )
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerRateLimited):
            provisioner.ensure_board(spec)


class DryRunTests(unittest.TestCase):
    def test_fresh_board_preview_lists_creations_without_mutating(self):
        run = script_full_create(ProvisionFakeRun())
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertEqual(
            report.created,
            (
                "project",
                "field:Agent",
                "field:Agent state",
                "field:Priority",
                "field:Blocked by",
            ),
        )
        self.assertEqual(report.kept, ("link:example/demo", "field:Status"))
        self.assertEqual(report.refused, ())
        self.assertEqual(run.mutations(), [])

    def test_preview_over_an_exact_board_is_all_kept(self):
        run = addressed_run(*standard_field_nodes())
        provisioner, spec = provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())
        self.assertIn("link:example/demo", report.kept)
        self.assertIn("field:Priority", report.kept)
        self.assertEqual(run.mutations(), [])

    def test_preview_never_searches_by_title_for_an_address(self):
        run = addressed_run(*standard_field_nodes())
        provisioner, spec = provisioner_for(run)
        provisioner.dry_run(spec)
        self.assertFalse(any("BoardTitles" in query for query in run.queries()))

    def test_preview_of_an_address_that_no_longer_resolves_is_refused(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script("BoardByNumber", [{"user": {"projectV2": None}, "organization": None}])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(ValueError) as caught:
            provisioner.dry_run(spec)
        self.assertIn("no longer exists", str(caught.exception))
        self.assertEqual(run.mutations(), [])

    def test_preview_names_both_numbers_for_an_ambiguous_title(self):
        run = script_full_create(ProvisionFakeRun())
        run.scripted["BoardTitles"] = [
            titles_payload(("PVT_a", 2, "demo board"), ("PVT_b", 5, "demo board"))
        ]
        provisioner, spec = bootstrap_provisioner_for(run)
        with self.assertRaises(ValueError) as caught:
            provisioner.dry_run(spec)
        self.assertIn("2", str(caught.exception))
        self.assertIn("5", str(caught.exception))
        self.assertEqual(run.mutations(), [])

    def test_preview_of_a_title_found_public_board_lists_the_link_only(self):
        run = script_full_create(ProvisionFakeRun())
        run.scripted["BoardTitles"] = [titles_payload(("PVT_a", 2, "demo board"))]
        run.script(
            "BoardById",
            by_id(project_node(pid="PVT_a", number=2, public=True, fields=standard_field_nodes())),
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("private", report.refused[0])
        self.assertIn("link:example/demo", report.kept)
        self.assertNotIn("field:Priority", report.kept)
        self.assertEqual(report.created, ())
        self.assertEqual(run.mutations(), [])

    def test_preview_of_an_addressed_public_board_keeps_planning_fields(self):
        run = addressed_run(*standard_field_nodes(), public=True)
        provisioner, spec = provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("private", report.refused[0])
        self.assertIn("field:Priority", report.kept)
        self.assertEqual(run.mutations(), [])

    def test_preview_lists_missing_ranks_as_option_creations(self):
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            select_node(
                "F_prio",
                "Priority",
                [
                    ("opt-1", "Urgent", "RED", ""),
                    ("opt-2", "High", "ORANGE", ""),
                    ("opt-3", "Normal", "YELLOW", ""),
                ],
            ),
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live)
        provisioner, spec = provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertIn("option:Priority/Low", report.created)
        self.assertIn("option:Priority/Backlog", report.created)
        self.assertIn("field:Priority", report.kept)
        self.assertEqual(report.refused, ())
        self.assertEqual(run.mutations(), [])

    def test_preview_refuses_an_unresendable_append_by_hand(self):
        options = [
            {"id": "opt-1", "name": "Urgent", "color": None, "description": ""},
            {"id": "opt-2", "name": "High", "color": "ORANGE", "description": ""},
            {"id": "opt-3", "name": "Normal", "color": "YELLOW", "description": ""},
        ]
        live = [
            text_node("F_agent", "Agent"),
            text_node("F_status", "Agent state"),
            select_node("F_board", "Status", STATUS_OPTIONS),
            {
                "__typename": "ProjectV2SingleSelectField",
                "id": "F_prio",
                "name": "Priority",
                "dataType": "SINGLE_SELECT",
                "options": options,
            },
            text_node("F_blocked", "Blocked by"),
        ]
        run = addressed_run(*live)
        provisioner, spec = provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertEqual(len(report.refused), 2)
        self.assertTrue(all("by hand" in line for line in report.refused))
        self.assertEqual(run.mutations(), [])

    def test_preview_lists_the_link_as_would_create_without_mutating(self):
        run = addressed_run(*standard_field_nodes(), repos=())
        run.add_rest("repos/example/demo", {"node_id": "R_1"})
        provisioner, spec = provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertIn("link:example/demo", report.created)
        self.assertEqual(report.refused, ())
        self.assertFalse(run.has_mutation("BoardLinkRepo"))
        self.assertEqual(run.mutations(), [])

    def test_preview_refuses_the_link_only_when_the_repo_does_not_resolve(self):
        run = addressed_run(*standard_field_nodes(), repos=())
        run.add_rest("repos/example/demo", gh.GhError("other", "Not Found (HTTP 404)"))
        provisioner, spec = provisioner_for(run)
        report = provisioner.dry_run(spec)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("example/demo", report.refused[0])
        self.assertNotIn("link:example/demo", report.created)
        self.assertEqual(run.mutations(), [])

    def test_preview_rate_limit_propagates(self):
        run = ProvisionFakeRun().script(
            "OwnerResolve",
            [gh.GhError("unavailable", "API rate limit exceeded for user ID 1. (HTTP 403)")],
        )
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerRateLimited):
            provisioner.dry_run(spec)
        self.assertEqual(run.mutations(), [])

    def test_preview_of_a_malformed_schema_fails_closed(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        bad = project_node()
        bad["fields"] = {"nodes": "nope"}
        run.script("BoardByNumber", [by_number(bad)])
        provisioner, spec = provisioner_for(run)
        with self.assertRaises(iface.TrackerError):
            provisioner.dry_run(spec)
        self.assertEqual(run.mutations(), [])

    def test_preview_rejects_an_unknown_concept_before_any_read(self):
        from corral import tracker as iface_mod

        run = ProvisionFakeRun()
        provisioner, _ = provisioner_for(run)
        spec = iface_mod.BoardSpec(
            owner="example",
            title="demo board",
            repo="example/demo",
            fields=(iface_mod.FieldSpec("mystery", "Mystery", "text", ()),),
        )
        with self.assertRaises(ValueError):
            provisioner.dry_run(spec)
        self.assertEqual(run.calls, [])


class FactoryTests(unittest.TestCase):
    def test_factory_builds_a_provisioner_on_the_injected_seams(self):
        from corral import trackers

        run = addressed_run(*standard_field_nodes())
        pacer = FakePacer()
        provisioner = trackers.production_board_provisioner(run=run, pacer=pacer)
        self.assertIsInstance(provisioner, GithubBoardProvisioner)
        report = provisioner.ensure_board(spec_for(table()))
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())

    def test_provisioner_takes_only_run_and_pacer(self):
        import inspect

        run = ProvisionFakeRun()
        provisioner = GithubBoardProvisioner(run)
        self.assertIs(provisioner._run, run)
        params = list(inspect.signature(GithubBoardProvisioner.__init__).parameters)
        self.assertEqual(params, ["self", "run", "pacer"])


class FieldKindTests(unittest.TestCase):
    def test_one_kind_mapping_serves_read_and_provision_paths(self):
        from corral.github.board import _field_kind, _want_kind

        text = {"__typename": "ProjectV2Field", "dataType": "TEXT"}
        select = {"__typename": "ProjectV2SingleSelectField", "dataType": "SINGLE_SELECT"}
        other = {"__typename": "ProjectV2IterationField", "dataType": "ITERATION"}
        self.assertEqual(_field_kind(text), "text")
        self.assertEqual(_field_kind(select), "single_select")
        self.assertEqual(_field_kind(other), "other")
        for concept in ("agent", "status", "blocked_by"):
            self.assertEqual(_want_kind(concept), "text")
        for concept in ("board_status", "priority", "area", "pool"):
            self.assertEqual(_want_kind(concept), "single_select")
