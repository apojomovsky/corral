"""The leak guard: private terms never cross from the ops side to the shared side."""

import os
import tempfile
import unittest

from corral import leakguard, settings

TERM = leakguard.Term("term", "Acme Client", "corral.toml")
PATTERN = leakguard.Term("pattern", "acme-[0-9]+", "corral.toml")


def terms(*items):
    return list(items)


class ScanTest(unittest.TestCase):
    def test_a_plain_term_matches_case_insensitively(self):
        (match,) = leakguard.scan("shipping ACME CLIENT work", terms(TERM))
        self.assertEqual((match.line, match.column), (1, 10))
        self.assertEqual(match.term, TERM)
        self.assertEqual(match.excerpt, "shipping ACME CLIENT work")

    def test_a_pattern_matches_case_insensitively(self):
        (match,) = leakguard.scan("see ACME-42 here", terms(PATTERN))
        self.assertEqual((match.line, match.column), (1, 5))

    def test_clean_text_matches_nothing(self):
        self.assertEqual(leakguard.scan("ordinary work", terms(TERM, PATTERN)), [])

    def test_each_line_reports_its_own_number(self):
        found = leakguard.scan("clean\nAcme Client lands\nclean", terms(TERM))
        self.assertEqual([(m.line, m.column) for m in found], [(2, 1)])

    def test_a_long_line_is_trimmed_to_80_characters(self):
        line = "x" * 90 + " Acme Client"
        (match,) = leakguard.scan(line, terms(TERM))
        self.assertEqual(len(match.excerpt), 80)

    def test_columns_are_one_based(self):
        (match,) = leakguard.scan("ab Acme Client", terms(TERM))
        self.assertEqual(match.column, 4)


class TermsTest(unittest.TestCase):
    def test_configured_terms_come_first_with_their_source(self):
        with tempfile.TemporaryDirectory(prefix="corral-leak-") as ops:
            with open(os.path.join(ops, "corral.toml"), "w", encoding="utf-8") as handle:
                handle.write('[private]\nterms = ["Acme Client"]\npatterns = ["acme-[0-9]+"]\n')
            found = leakguard.terms(
                ops, host="host-1.example.com", home="/home/someone", ops_name="ops-root"
            )
        kinds = [(term.kind, term.text, term.source) for term in found]
        self.assertIn(("term", "Acme Client", "corral.toml"), kinds)
        self.assertIn(("pattern", "acme-[0-9]+", "corral.toml"), kinds)

    def test_builtins_cover_the_ops_name_root_host_and_home(self):
        with tempfile.TemporaryDirectory(prefix="corral-leak-") as ops:
            with open(os.path.join(ops, "corral.toml"), "w", encoding="utf-8") as handle:
                handle.write("")
            found = leakguard.terms(
                ops,
                host="host-1.example.com",
                home="/home/someone",
                ops_name=os.path.basename(ops),
            )
        by_source = {term.source for term in found}
        self.assertIn("ops directory name", by_source)
        self.assertIn("ops root path", by_source)
        self.assertIn("host name", by_source)
        self.assertIn("home directory", by_source)

    def test_a_builtin_shorter_than_4_characters_is_ignored(self):
        found = leakguard.terms(None, host="ab", home="/x", ops_name="")
        self.assertEqual(found, [])

    def test_without_an_ops_root_only_host_and_home_apply(self):
        found = leakguard.terms(None, host="host-1.example.com", home="/home/someone", ops_name="")
        self.assertEqual(
            [(term.text, term.source) for term in found],
            [
                ("host-1.example.com", "host name"),
                ("/home/someone", "home directory"),
            ],
        )

    def test_a_bad_pattern_is_a_configuration_error_naming_the_pattern(self):
        with tempfile.TemporaryDirectory(prefix="corral-leak-") as ops:
            with open(os.path.join(ops, "corral.toml"), "w", encoding="utf-8") as handle:
                handle.write('[private]\npatterns = ["acme-([0-9]+"]\n')
            with self.assertRaisesRegex(settings.SettingsError, r"acme-\(\[0-9\]\+"):
                leakguard.terms(ops, host="host-1", home="/home/someone", ops_name="ops")


class TargetsTest(unittest.TestCase):
    def test_messages_report_the_short_sha(self):
        commits = [("abcdef1234567890", "land Acme Client work"), ("12345678", "clean")]
        (finding,) = leakguard.check_messages(commits, terms(TERM))
        self.assertEqual(finding.where, "commit abcdef12")
        self.assertEqual(finding.term, TERM)

    def test_branch_body_and_diff_each_report_their_where(self):
        dashed = leakguard.Term("term", "acme-client", "corral.toml")
        (branch,) = leakguard.check_branch("feat/acme-client-x", terms(dashed))
        self.assertEqual(branch.where, "branch")
        (body,) = leakguard.check_body("ships Acme Client", terms(TERM))
        self.assertEqual(body.where, "body")
        diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            "-removed\n"
            "+added Acme Client here\n"
        )
        (hit,) = leakguard.check_diff(diff, terms(TERM))
        self.assertEqual(hit.where, "diff src/a.py:2")

    def test_the_diff_ignores_context_removed_and_header_lines(self):
        diff = (
            "diff --git a/acme-client.py b/acme-client.py\n"
            "--- a/acme-client.py\n"
            "+++ b/acme-client.py\n"
            "@@ -1,2 +1,2 @@\n"
            " acme-client context\n"
            "-acme-client removed\n"
            "+clean replacement\n"
        )
        self.assertEqual(leakguard.check_diff(diff, terms(TERM)), [])

    def test_the_diff_counts_new_file_lines_across_hunks(self):
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "@@ -20,1 +10,1 @@\n"
            "-old\n"
            "+Acme Client lands\n"
        )
        (hit,) = leakguard.check_diff(diff, terms(TERM))
        self.assertEqual(hit.where, "diff a.txt:10")
        self.assertEqual(hit.line, 10)

    def test_added_content_starting_with_plus_plus_still_scans(self):
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1,1 +1,2 @@\n"
            " old\n"
            "+++Acme Client leaked\n"
        )
        (hit,) = leakguard.check_diff(diff, terms(TERM))
        self.assertEqual(hit.where, "diff a.txt:2")
        self.assertEqual(hit.column, 3)

    def test_a_second_files_header_is_not_scanned_as_content(self):
        dashed = leakguard.Term("term", "acme-client", "corral.toml")
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/acme-client.txt b/acme-client.txt\n"
            "--- a/acme-client.txt\n"
            "+++ b/acme-client.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        self.assertEqual(leakguard.check_diff(diff, terms(dashed)), [])


class GuardTest(unittest.TestCase):
    def test_a_match_raises_with_exit_code_1(self):
        with self.assertRaises(leakguard.LeakError) as ctx:
            leakguard.guard_text("Acme Client lands", terms(TERM))
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(len(ctx.exception.findings), 1)

    def test_clean_text_passes(self):
        self.assertEqual(leakguard.guard_text("ordinary work", terms(TERM)), [])


class PrivateSettingsTest(unittest.TestCase):
    def resolve(self, **kw):
        kw.setdefault("env", {})
        return settings.resolve(**kw)

    def test_private_terms_default_to_empty(self):
        self.assertEqual(self.resolve().private, settings.Private(terms=[], patterns=[]))

    def test_the_ops_file_may_list_terms_and_patterns(self):
        resolved = self.resolve(
            project={"private": {"terms": ["Acme Client"], "patterns": ["acme-[0-9]+"]}}
        )
        self.assertEqual(
            resolved.private, settings.Private(terms=["Acme Client"], patterns=["acme-[0-9]+"])
        )

    def test_a_bad_entry_is_an_error(self):
        for raw in (
            {"terms": "Acme"},
            {"terms": [3]},
            {"terms": [""]},
            {"patterns": [["x"]]},
            {"patterns": [""]},
        ):
            with self.subTest(raw=raw), self.assertRaises(settings.SettingsError):
                self.resolve(project={"private": raw})

    def test_a_bad_pattern_names_the_pattern(self):
        with self.assertRaisesRegex(settings.SettingsError, r"acme-\(\[0-9\]\+"):
            self.resolve(project={"private": {"patterns": ["acme-([0-9]+"]}})

    def test_the_host_config_must_not_take_private_terms(self):
        with self.assertRaisesRegex(settings.SettingsError, "'private.terms'"):
            self.resolve(host={"private": {"terms": ["Acme Client"]}})

    def test_a_repo_entry_must_not_take_private_terms(self):
        with self.assertRaisesRegex(settings.SettingsError, "'private.patterns'"):
            self.resolve(
                project={"repo": [{"name": "app", "private": {"patterns": ["acme-[0-9]+"]}}]}
            )


if __name__ == "__main__":
    unittest.main()
