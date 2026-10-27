import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from corral.github import gh


def _popen(returncode=0, stdout="", stderr=""):
    proc = mock.Mock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


class RunTest(unittest.TestCase):
    def test_paced_marker_only_on_opt_in(self):
        with mock.patch("subprocess.Popen", return_value=_popen(stdout="x")) as spawn:
            gh.run(["issue", "list"])
        env = spawn.call_args[1].get("env")
        self.assertTrue(env is None or env.get(gh.PACED_MARKER) != "1")
        with mock.patch("subprocess.Popen", return_value=_popen(stdout="x")) as spawn:
            gh.run(["issue", "list"], paced=True)
        self.assertEqual(spawn.call_args[1]["env"][gh.PACED_MARKER], "1")

    def test_returns_stdout_on_success(self):
        with mock.patch("subprocess.Popen", return_value=_popen(stdout="hello")):
            self.assertEqual(gh.run(["api", "x"]), "hello")

    def test_missing_scope_is_classified(self):
        err = "error: your authentication token is missing required scopes [read:project]"
        with mock.patch("subprocess.Popen", return_value=_popen(1, stderr=err)):
            with self.assertRaises(gh.GhError) as caught:
                gh.run(["api", "x"])
        self.assertEqual(caught.exception.kind, "scope")

    def test_network_failure_is_classified(self):
        err = "dial tcp: lookup api.github.com: connection refused"
        with mock.patch("subprocess.Popen", return_value=_popen(1, stderr=err)):
            with self.assertRaises(gh.GhError) as caught:
                gh.run(["api", "x"])
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_other_failure_is_classified(self):
        with mock.patch("subprocess.Popen", return_value=_popen(1, stderr="boom")):
            with self.assertRaises(gh.GhError) as caught:
                gh.run(["api", "x"])
        self.assertEqual(caught.exception.kind, "other")

    def test_rate_limit_is_classified_unavailable(self):
        err = "gh: API rate limit exceeded for user ID 1. (HTTP 403)"
        with mock.patch("subprocess.Popen", return_value=_popen(1, stderr=err)):
            with self.assertRaises(gh.GhError) as caught:
                gh.run(["api", "x"])
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_secondary_rate_limit_is_classified_unavailable(self):
        err = "You have exceeded a secondary rate limit (abuse detection)."
        with mock.patch("subprocess.Popen", return_value=_popen(1, stderr=err)):
            with self.assertRaises(gh.GhError) as caught:
                gh.run(["api", "x"])
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_stalled_gh_raises_unavailable(self):
        stalled = mock.Mock()
        stalled.pid = 123456789
        stalled.communicate.side_effect = subprocess.TimeoutExpired("gh", 30)
        with mock.patch("subprocess.Popen", return_value=stalled):
            with self.assertRaises(gh.GhError) as caught:
                gh.run(["api", "graphql"], timeout=0.05)
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_default_timeout_bounds_stalls(self):
        stalled = mock.Mock()
        stalled.pid = 123456789
        stalled.communicate.side_effect = subprocess.TimeoutExpired("gh", 30)
        ticks = iter([100.0, 100.0 + gh.GH_TIMEOUT_S + 1.0])
        with (
            mock.patch("subprocess.Popen", return_value=stalled),
            mock.patch.object(gh.time, "monotonic", side_effect=lambda: next(ticks, 200.0)),
        ):
            with self.assertRaises(gh.GhError) as caught:
                gh.run(["api", "x"])
        self.assertEqual(caught.exception.kind, "unavailable")


class RateLimitKindTest(unittest.TestCase):
    def test_rest_primary_is_rest(self):
        self.assertEqual(
            gh.rate_limit_kind("gh: API rate limit exceeded for user ID 1. (HTTP 403)"), "rest"
        )

    def test_graphql_keyword_is_graphql(self):
        self.assertEqual(gh.rate_limit_kind("GraphQL: API rate limit exceeded"), "graphql")

    def test_graphql_error_type_is_graphql(self):
        payload = json.dumps(
            {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
        )
        self.assertEqual(gh.rate_limit_kind(payload), "graphql")

    def test_bare_rate_limited_type_is_graphql(self):
        self.assertEqual(gh.rate_limit_kind('{"type":"RATE_LIMITED"}'), "graphql")

    def test_secondary_is_secondary(self):
        self.assertEqual(
            gh.rate_limit_kind("You have exceeded a secondary rate limit."), "secondary"
        )
        self.assertEqual(gh.rate_limit_kind("abuse detection mechanism, retry later"), "secondary")

    def test_secondary_wins_over_graphql_wording(self):
        self.assertEqual(gh.rate_limit_kind("abuse detection: GraphQL rate limit"), "secondary")

    def test_markers_inside_repo_names_do_not_set_bucket(self):
        # Single-token bucket markers match standing alone only:
        # a repo name carrying one must not move the hold off REST.
        err = "gh: API rate limit exceeded for repository acme/graphql-tool (HTTP 403)"
        self.assertEqual(gh.rate_limit_kind(err), "rest")

    def test_other_text_is_none(self):
        self.assertIsNone(gh.rate_limit_kind("HTTP 502: Bad Gateway"))
        self.assertIsNone(gh.rate_limit_kind(""))
        self.assertIsNone(gh.rate_limit_kind(None))


class ClassifyTest(unittest.TestCase):
    """_classify reads the diagnosis, never the repo name."""

    def test_scope_repo_404_is_other(self):
        err = "gh: Could not resolve to a repository with the name 'acme/scope-tool' (HTTP 404)"
        self.assertEqual(gh._classify(err), "other")

    def test_timeout_repo_404_is_other(self):
        err = "gh: repo owner/timeout-helper not found (HTTP 404)"
        self.assertEqual(gh._classify(err), "other")

    def test_issue_number_matching_a_status_code_is_other(self):
        self.assertEqual(gh._classify("gh: issue #502 not found"), "other")

    def test_status_code_in_repo_url_is_other(self):
        err = "gh: Not Found (https://api.github.com/repos/o/504-gateway) (HTTP 404)"
        self.assertEqual(gh._classify(err), "other")

    def test_scope_list_still_needs_scope(self):
        err = "The 'project' field requires one of the following scopes: ['read:project']"
        self.assertEqual(gh._classify(err), "scope")

    def test_insufficient_still_needs_scope(self):
        self.assertEqual(gh._classify("gh: insufficient scopes to execute this query"), "scope")

    def test_go_timeout_diagnosis_is_unavailable(self):
        self.assertEqual(
            gh._classify(
                "Post https://api.github.com/graphql: "
                "Client.Timeout exceeded while awaiting headers"
            ),
            "unavailable",
        )

    def test_http_503_is_unavailable(self):
        self.assertEqual(
            gh._classify("gh: HTTP 503 Service Unavailable (https://api.github.com/graphql)"),
            "unavailable",
        )

    def test_marker_as_repo_owner_is_other(self):
        err = "gh: Could not resolve to a repository with the name 'scope/timeout-tool' (HTTP 404)"
        self.assertEqual(gh._classify(err), "other")


class GraphqlTest(unittest.TestCase):
    def setUp(self):
        # Each test gets its own pacer file; the real one is machine-shared
        # and a stray cooldown written here would stall the next caller.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pacer = gh.GraphqlPacer(
            os.path.join(tmp.name, "graphql-pacing.json"), sleep=lambda seconds: None
        )
        patcher = mock.patch.object(gh, "_pacer", return_value=pacer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rate_limited_type_without_rate_words_is_unavailable(self):
        payload = json.dumps(
            {
                "data": None,
                "errors": [
                    {
                        "type": "RATE_LIMITED",
                        "message": "Something went wrong with acme-timeout-helper",
                    }
                ],
            }
        )
        with mock.patch("subprocess.Popen", return_value=_popen(stdout=payload)):
            with self.assertRaises(gh.GhError) as caught:
                gh.graphql("{x}")
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_marker_repo_in_graphql_message_is_other(self):
        payload = json.dumps(
            {
                "data": None,
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "message": "Could not resolve to a repository 'o/scope-tool'",
                    }
                ],
            }
        )
        with mock.patch("subprocess.Popen", return_value=_popen(stdout=payload)):
            with self.assertRaises(gh.GhError) as caught:
                gh.graphql("{x}")
        self.assertEqual(caught.exception.kind, "other")

    def test_unwraps_the_data_key(self):
        payload = json.dumps({"data": {"user": {"login": "acme"}}})
        with mock.patch("subprocess.Popen", return_value=_popen(stdout=payload)):
            self.assertEqual(gh.graphql("{x}"), {"user": {"login": "acme"}})

    def test_graphql_errors_raise(self):
        payload = json.dumps({"data": None, "errors": [{"message": "bad field"}]})
        with mock.patch("subprocess.Popen", return_value=_popen(stdout=payload)):
            with self.assertRaises(gh.GhError):
                gh.graphql("{x}")

    def test_graphql_rate_limited_payload_is_unavailable(self):
        payload = json.dumps(
            {
                "data": None,
                "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
            }
        )
        with mock.patch("subprocess.Popen", return_value=_popen(stdout=payload)):
            with self.assertRaises(gh.GhError) as caught:
                gh.graphql("{x}")
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_int_variables_use_capital_f(self):
        payload = json.dumps({"data": {}})
        with mock.patch("subprocess.Popen", return_value=_popen(stdout=payload)) as run:
            gh.graphql("query($n:Int!){x}", n=7, owner="acme")
        args = run.call_args[0][0]
        self.assertIn("-F", args)
        self.assertIn("n=7", args)
        self.assertIn("owner=acme", args)

    def test_bool_variables_use_json_lowercase(self):
        payload = json.dumps({"data": {}})
        with mock.patch("subprocess.Popen", return_value=_popen(stdout=payload)) as run:
            gh.graphql("query($enabled:Boolean!){x}", enabled=True)
        args = run.call_args[0][0]
        self.assertIn("-F", args)
        self.assertIn("enabled=true", args)


class NormalizePrBodyTest(unittest.TestCase):
    def test_literal_n_becomes_newline(self):
        self.assertEqual(gh.normalize_pr_body("a\\n\\n- b"), "a\n\n- b")

    def test_real_newlines_unchanged(self):
        body = "a\n\n- b\n- c"
        self.assertEqual(gh.normalize_pr_body(body), body)

    def test_crlf_escaped(self):
        self.assertEqual(gh.normalize_pr_body("a\\r\\nB"), "a\nB")

    def test_empty_and_no_escape(self):
        self.assertEqual(gh.normalize_pr_body(""), "")
        self.assertEqual(gh.normalize_pr_body("no escapes"), "no escapes")

    def test_contains_literal_escapes(self):
        self.assertTrue(gh.contains_literal_escapes("a\\n b"))
        self.assertTrue(gh.contains_literal_escapes("a\\r b"))
        self.assertFalse(gh.contains_literal_escapes("a\n b"))
        self.assertFalse(gh.contains_literal_escapes(""))

    def test_a_real_world_body(self):
        literal = "Split per #109 option B.\\n\\n- common_ram is now PIC14\\n- access_bank is PIC18"
        expected = "Split per #109 option B.\n\n- common_ram is now PIC14\n- access_bank is PIC18"
        self.assertEqual(gh.normalize_pr_body(literal), expected)


class ReviewRecordTest(unittest.TestCase):
    def test_full_record_counts(self):
        body = (
            "Fix the ladder.\n\n## Review\n"
            "- Reviewer: reviewer@host\n"
            "- Question: does the parser agree with takeoff?\n"
            "- Findings:\n"
            "  - [high] fence handling missed - fixed in gh.py\n"
            "  - [low] typo - fixed\n"
        )
        self.assertTrue(gh.has_review_record(body))

    def test_no_findings_counts(self):
        self.assertTrue(gh.has_review_record("Fix.\n\n## Review\n\nNo findings\n"))

    def test_missing_section_is_none(self):
        body = "Fix.\n\n- bullet one\n"
        self.assertIsNone(gh.extract_review_section(body))
        self.assertFalse(gh.has_review_record(body))

    def test_empty_and_blank_sections_do_not_count(self):
        self.assertFalse(gh.has_review_record("Fix.\n\n## Review\n"))
        self.assertFalse(gh.has_review_record("Fix.\n\n## Review\n   \n"))
        self.assertEqual(gh.extract_review_section("Fix.\n\n## Review\n"), "")

    def test_section_ends_at_next_heading(self):
        body = "Fix.\n\n## Review\n\nNo findings\n\n## Notes\nlate addition\n"
        self.assertEqual((gh.extract_review_section(body) or "").strip(), "No findings")
        self.assertTrue(gh.has_review_record(body))

    def test_heading_levels_and_case(self):
        self.assertTrue(gh.has_review_record("Fix.\n\n### review\n\nNo findings\n"))
        self.assertTrue(gh.has_review_record("Fix.\n\n# REVIEW\n\nNo findings\n"))

    def test_heading_without_space_does_not_count(self):
        body = "Fix.\n\n##Review\n\nNo findings\n"
        self.assertIsNone(gh.extract_review_section(body))
        self.assertFalse(gh.has_review_record(body))

    def test_fenced_heading_is_ignored(self):
        body = "Fix.\n\n```\n## Review\n\nNo findings\n```\n"
        self.assertIsNone(gh.extract_review_section(body))
        self.assertFalse(gh.has_review_record(body))

    def test_tilde_fenced_heading_is_ignored(self):
        body = "Fix.\n\n~~~\n## Review\n\nNo findings\n~~~\n"
        self.assertIsNone(gh.extract_review_section(body))
        self.assertFalse(gh.has_review_record(body))

    def test_empty_body_has_no_record(self):
        self.assertIsNone(gh.extract_review_section(""))
        self.assertFalse(gh.has_review_record(""))


class ClosingRefsTest(unittest.TestCase):
    def test_numbers_from_closing_references(self):
        pr = {"closingIssuesReferences": [{"number": 119}, {"number": 120}]}
        self.assertEqual(gh.closing_issue_numbers(pr), [119, 120])

    def test_empty_missing_and_null_yield_no_numbers(self):
        self.assertEqual(gh.closing_issue_numbers({"closingIssuesReferences": []}), [])
        self.assertEqual(gh.closing_issue_numbers({}), [])
        self.assertEqual(gh.closing_issue_numbers({"closingIssuesReferences": None}), [])

    def test_skips_entries_without_an_integer_number(self):
        pr = {
            "closingIssuesReferences": [
                {"number": 119},
                {"nope": 1},
                "x",
                {"number": "abc"},
                {"number": True},
            ]
        }
        self.assertEqual(gh.closing_issue_numbers(pr), [119])

    def test_title_only_mention_is_not_a_closing_reference(self):
        # A (#N) title suffix or bare #N mention fills no field: only the
        # references list counts, so a body mention alone yields nothing.
        self.assertEqual(gh.closing_issue_numbers({"body": "feat thing"}), [])


class RefsTest(unittest.TestCase):
    def test_refs_keyword_numbers(self):
        self.assertEqual(gh.refs_issue_numbers("Refs #119"), [119])

    def test_list_after_one_keyword(self):
        self.assertEqual(gh.refs_issue_numbers("Refs #12, #13 and #14"), [12, 13, 14])

    def test_keyword_is_case_insensitive(self):
        self.assertEqual(gh.refs_issue_numbers("refs #7"), [7])

    def test_closes_keyword_is_not_a_refs_link(self):
        self.assertEqual(gh.refs_issue_numbers("Closes #1"), [])

    def test_fenced_refs_do_not_link(self):
        self.assertEqual(gh.refs_issue_numbers("```\nRefs #5\n```\n"), [])

    def test_tilde_fenced_refs_do_not_link(self):
        self.assertEqual(gh.refs_issue_numbers("~~~\nRefs #5\n~~~\n"), [])

    def test_backtick_line_does_not_close_tilde_fence(self):
        body = "~~~\n```\nRefs #5\n~~~\nRefs #6\n"
        self.assertEqual(gh.refs_issue_numbers(body), [6])

    def test_shorter_run_does_not_close_longer_tilde_fence(self):
        body = "~~~~\n~~~\nRefs #5\n~~~~\nRefs #6\n"
        self.assertEqual(gh.refs_issue_numbers(body), [6])

    def test_backtick_fence_not_closed_by_tildes(self):
        body = "```\nRefs #5\n~~~\nRefs #6\n```\nRefs #7\n"
        self.assertEqual(gh.refs_issue_numbers(body), [7])

    def test_longer_fence_closes_only_on_bare_run(self):
        body = "````\n```\nRefs #5\n```python\nRefs #7\n````\nRefs #6\n"
        self.assertEqual(gh.refs_issue_numbers(body), [6])

    def test_inline_code_refs_do_not_link(self):
        self.assertEqual(gh.refs_issue_numbers("Write `Refs #5` for a partial win."), [])

    def test_double_backtick_refs_do_not_link(self):
        self.assertEqual(gh.refs_issue_numbers("Write ``Refs #5`` for a partial win."), [])
        self.assertEqual(gh.refs_issue_numbers("``x`` then Refs #5"), [5])

    def test_linked_refs_are_closes_then_refs_deduplicated(self):
        pr = {"closes": [("acme/app", 1)], "refs": [("acme/app", 1), ("acme/app", 2)]}
        self.assertEqual(gh.linked_issue_refs(pr), [("acme/app", 1), ("acme/app", 2)])


class BodyClosesTest(unittest.TestCase):
    def test_close_keywords_count(self):
        for word in (
            "Close",
            "Closes",
            "Closed",
            "Fix",
            "Fixes",
            "Fixed",
            "Resolve",
            "Resolves",
            "Resolved",
        ):
            with self.subTest(word=word):
                self.assertEqual(gh.closes_issue_numbers_from_body(f"{word} #379"), [379])

    def test_keyword_is_case_insensitive(self):
        self.assertEqual(gh.closes_issue_numbers_from_body("closes #377"), [377])

    def test_list_after_one_keyword(self):
        self.assertEqual(gh.closes_issue_numbers_from_body("Closes #377, #379"), [377, 379])
        self.assertEqual(gh.closes_issue_numbers_from_body("Fixes #12, #13 and #14"), [12, 13, 14])

    def test_refs_keyword_is_not_a_close(self):
        self.assertEqual(gh.closes_issue_numbers_from_body("Refs #1"), [])

    def test_bare_mention_is_not_a_close(self):
        self.assertEqual(gh.closes_issue_numbers_from_body("see #379"), [])
        self.assertEqual(gh.closes_issue_numbers_from_body(""), [])

    def test_fenced_and_inline_closes_do_not_link(self):
        self.assertEqual(gh.closes_issue_numbers_from_body("```\nCloses #5\n```\n"), [])
        self.assertEqual(gh.closes_issue_numbers_from_body("Write `Closes #5` instead."), [])

    def test_branch_ticket_matrix(self):
        # docs/377-x with Closes #379 fails;
        # Closes #377 and Closes #377, #379 pass; empty set stays advisory.
        def ci_fails(branch, body):
            expected = gh.branch_issue_number(branch)
            linked = gh.body_linked_issue_numbers(body)
            return bool(linked) and expected is not None and expected not in linked

        self.assertTrue(ci_fails("docs/377-x", "Closes #379"))
        self.assertFalse(ci_fails("docs/377-x", "Closes #377"))
        self.assertFalse(ci_fails("docs/377-x", "Closes #377, #379"))
        self.assertFalse(ci_fails("docs/377-x", "no ticket here"))

    def test_body_linked_merges_closes_and_refs(self):
        self.assertEqual(gh.body_linked_issue_numbers("Closes #379\n\nRefs #377"), [379, 377])
        self.assertEqual(gh.body_linked_issue_numbers("Refs #377"), [377])


class CrossRepoRefsTest(unittest.TestCase):
    def test_refs_bare_resolves_to_default(self):
        self.assertEqual(gh.refs_issue_refs("Refs #119", "acme/app"), [("acme/app", 119)])

    def test_refs_bare_without_default_stays_unqualified(self):
        self.assertEqual(gh.refs_issue_refs("Refs #119"), [(None, 119)])

    def test_refs_qualified_and_mixed_list(self):
        self.assertEqual(
            gh.refs_issue_refs("Refs #12, acme/lib#13 and #14", "acme/app"),
            [("acme/app", 12), ("acme/lib", 13), ("acme/app", 14)],
        )

    def test_refs_keyword_case_insensitive(self):
        self.assertEqual(gh.refs_issue_refs("refs acme/lib#7", "acme/app"), [("acme/lib", 7)])

    def test_refs_same_repo_qualified_dedups_bare(self):
        self.assertEqual(gh.refs_issue_refs("Refs #5, acme/app#5", "acme/app"), [("acme/app", 5)])

    def test_refs_fenced_and_inline_qualified_do_not_link(self):
        self.assertEqual(gh.refs_issue_refs("```\nRefs acme/lib#5\n```\n"), [])
        self.assertEqual(gh.refs_issue_refs("Write `Refs acme/lib#5` down."), [])

    def test_bare_wrappers_ignore_qualified(self):
        self.assertEqual(gh.refs_issue_numbers("Refs acme/lib#13"), [])
        self.assertEqual(gh.refs_issue_numbers("Refs #12, acme/lib#13"), [12])
        self.assertEqual(gh.closes_issue_numbers_from_body("Closes acme/lib#5"), [])

    def test_closes_qualified_and_mixed_list(self):
        self.assertEqual(
            gh.closes_issue_refs_from_body("Fixes acme/lib#5, #6", "acme/app"),
            [("acme/lib", 5), ("acme/app", 6)],
        )

    def test_body_linked_refs_are_closes_then_refs(self):
        self.assertEqual(
            gh.body_linked_issue_refs("Closes acme/lib#5\n\nRefs #5", "acme/app"),
            [("acme/lib", 5), ("acme/app", 5)],
        )

    def test_closing_refs_keep_repository_shapes(self):
        pr = {
            "closingIssuesReferences": [
                {"number": 1, "repository": {"nameWithOwner": "acme/lib"}},
                {"number": "2", "repository": {"name": "app", "owner": {"login": "acme"}}},
                {"number": 3},
                {"number": "abc"},
                "junk",
            ]
        }
        self.assertEqual(
            gh.closing_issue_refs(pr, "acme/app"),
            [("acme/lib", 1), ("acme/app", 2), ("acme/app", 3)],
        )


class BranchIssueNumberTest(unittest.TestCase):
    def test_typed_branch_with_slug(self):
        self.assertEqual(gh.branch_issue_number("feat/119-closes-warning"), 119)
        self.assertEqual(gh.branch_issue_number("fix/42-prose-excluded-builder"), 42)
        self.assertEqual(gh.branch_issue_number("chore/630-review-budget"), 630)

    def test_bare_typed_branch(self):
        self.assertEqual(gh.branch_issue_number("feat/119"), 119)

    def test_non_conforming_branches_yield_none(self):
        self.assertIsNone(gh.branch_issue_number("master"))
        self.assertIsNone(gh.branch_issue_number("fix/tests-diskwatch-stop-flake-113"))
        self.assertIsNone(gh.branch_issue_number("feat/foo"))
        self.assertIsNone(gh.branch_issue_number(""))
        self.assertIsNone(gh.branch_issue_number(None))


class BranchTicketLinkedTest(unittest.TestCase):
    """The branch ticket is linked from its own repo, a sibling with the same
    number, or unqualified; never another repo or another number."""

    SIBLINGS = frozenset({"acme/app-ops", "acme/hal"})

    def test_unqualified_and_same_repo_satisfy(self):
        cases: list[list[tuple[str | None, int]]] = [[(None, 450)], [("acme/lib", 450)]]
        for pairs in cases:
            with self.subTest(pairs=pairs):
                self.assertTrue(gh.branch_ticket_linked(450, pairs, "acme/lib"))

    def test_a_sibling_with_the_same_number_satisfies(self):
        self.assertTrue(
            gh.branch_ticket_linked(450, [("acme/app-ops", 450)], "acme/lib", self.SIBLINGS)
        )

    def test_names_match_case_insensitively(self):
        self.assertTrue(
            gh.branch_ticket_linked(450, [("Acme/HAL", 450)], "acme/lib", self.SIBLINGS)
        )
        self.assertTrue(gh.branch_ticket_linked(450, [("acme/lib", 450)], "ACME/Lib"))

    def test_with_no_siblings_only_the_own_repo_counts(self):
        self.assertFalse(gh.branch_ticket_linked(450, [("acme/hal", 450)], "acme/lib"))

    def test_different_number_never_satisfies(self):
        self.assertFalse(
            gh.branch_ticket_linked(450, [("acme/hal", 451)], "acme/lib", self.SIBLINGS)
        )

    def test_non_siblings_never_satisfy(self):
        for repo in ("someone/else", "acme/other-repo", "someone/hal"):
            with self.subTest(repo=repo):
                self.assertFalse(
                    gh.branch_ticket_linked(450, [(repo, 450)], "acme/lib", self.SIBLINGS)
                )

    def test_no_default_repo_rejects_qualified_pairs(self):
        self.assertFalse(gh.branch_ticket_linked(450, [("acme/hal", 450)], None, self.SIBLINGS))
        self.assertTrue(gh.branch_ticket_linked(450, [(None, 450)], None))

    def test_no_branch_ticket_is_false(self):
        self.assertFalse(gh.branch_ticket_linked(None, [(None, 450)], "acme/lib"))


class ReadmeNaNoteTest(unittest.TestCase):
    def test_canonical_note_counts(self):
        self.assertTrue(gh.has_readme_na_note("Fix.\n\nREADME: n/a\n"))

    def test_case_and_spacing_variants_count(self):
        self.assertTrue(gh.has_readme_na_note("readme: N/A"))
        self.assertTrue(gh.has_readme_na_note("Readme: na"))
        self.assertTrue(gh.has_readme_na_note("surface only, README:  n/a (internal)"))

    def test_missing_empty_and_bare_na_do_not_count(self):
        self.assertFalse(gh.has_readme_na_note("Fix.\n\nNo findings\n"))
        self.assertFalse(gh.has_readme_na_note(""))
        self.assertFalse(gh.has_readme_na_note("n/a"))
        self.assertFalse(gh.has_readme_na_note("README: updated for the new flag"))

    def test_fenced_note_is_ignored(self):
        body = "Fix.\n\n```\nREADME: n/a\n```\n"
        self.assertFalse(gh.has_readme_na_note(body))

    def test_tilde_fenced_note_is_ignored(self):
        body = "Fix.\n\n~~~\nREADME: n/a\n~~~\n"
        self.assertFalse(gh.has_readme_na_note(body))
