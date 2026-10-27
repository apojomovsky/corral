import unittest

from corral import prose


class NormalizeTest(unittest.TestCase):
    def test_strips_indent_and_marker(self):
        self.assertEqual(prose.normalize(["    // why not what"]), "why not what")

    def test_strips_c_block_markers(self):
        lines = ["/*", " * the reason", " */"]
        self.assertEqual(prose.normalize(lines), "the reason")

    def test_strips_rust_doc_markers(self):
        self.assertEqual(prose.normalize(["/// the contract"]), "the contract")
        self.assertEqual(prose.normalize(["//! the crate"]), "the crate")

    def test_strips_hash_marker(self):
        self.assertEqual(prose.normalize(["  # the reason"]), "the reason")

    def test_collapses_internal_whitespace(self):
        self.assertEqual(prose.normalize(["//  a    b"]), "a b")

    def test_drops_blank_lines(self):
        self.assertEqual(prose.normalize(["// a", "//", "// b"]), "a\nb")


class BlockIdTest(unittest.TestCase):
    def test_is_twelve_hex_chars(self):
        got = prose.block_id("explanatory", "a reason")
        self.assertEqual(len(got), 12)
        self.assertRegex(got, r"^[0-9a-f]{12}$")

    def test_reindenting_preserves_the_id(self):
        a = prose.block_id("explanatory", prose.normalize(["// the reason"]))
        b = prose.block_id("explanatory", prose.normalize(["        // the reason"]))
        self.assertEqual(a, b)

    def test_changing_a_word_changes_the_id(self):
        a = prose.block_id("explanatory", prose.normalize(["// the reason"]))
        b = prose.block_id("explanatory", prose.normalize(["// the cause"]))
        self.assertNotEqual(a, b)

    def test_kind_participates_in_the_id(self):
        self.assertNotEqual(
            prose.block_id("doc", "same text"),
            prose.block_id("explanatory", "same text"),
        )

    def test_path_does_not_participate_in_the_id(self):
        # The id is location-free so a message stays actionable even when
        # the block moves between edits.
        text = prose.normalize(["// the reason"])
        self.assertEqual(
            prose.block_id("explanatory", text),
            prose.block_id("explanatory", text),
        )
        self.assertNotIn("/", prose.block_id("explanatory", text))


class SyntaxForTest(unittest.TestCase):
    def test_known_extensions(self):
        self.assertEqual(prose.syntax_for("src/a.c"), "c")
        self.assertEqual(prose.syntax_for("src/a.h"), "c")
        self.assertEqual(prose.syntax_for("crates/isel/src/lib.rs"), "rust")
        self.assertEqual(prose.syntax_for("scripts/a.sh"), "hash")
        self.assertEqual(prose.syntax_for("scripts/a.py"), "hash")

    def test_known_basenames(self):
        self.assertEqual(prose.syntax_for("Makefile"), "hash")
        self.assertEqual(prose.syntax_for("sub/CMakeLists.txt"), "hash")
        self.assertEqual(prose.syntax_for("docker/Dockerfile.ci"), "hash")

    def test_unscanned_files(self):
        self.assertIsNone(prose.syntax_for("README.md"))
        self.assertIsNone(prose.syntax_for("data.bin"))


class ParseDiffTest(unittest.TestCase):
    def test_single_run_records_its_start_line(self):
        diff = "@@ -0,0 +10,2 @@\n+// the reason\n+int x = 1;\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [(10, ["// the reason"])])

    def test_consecutive_lines_are_one_run(self):
        diff = "@@ -0,0 +5,2 @@\n+// one\n+// two\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [(5, ["// one", "// two"])])

    def test_code_between_splits_runs(self):
        diff = "@@ -0,0 +1,3 @@\n+// one\n+int x;\n+// two\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [(1, ["// one"]), (3, ["// two"])])

    def test_block_comment_continuation_is_stateful(self):
        # A bare `*` counts only inside an open /* block, otherwise
        # `*ptr = x;` would read as prose.
        diff = "@@ -0,0 +1,3 @@\n+/*\n+ * why\n+ */\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [(1, ["/*", " * why", " */"])])

    def test_deref_is_not_a_comment(self):
        diff = "@@ -0,0 +1,1 @@\n+*ptr = x;\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [])

    def test_shebang_is_not_a_comment(self):
        diff = "@@ -0,0 +1,2 @@\n+#!/usr/bin/env bash\n+# the reason\n"
        self.assertEqual(prose.parse_diff(diff, "hash"), [(2, ["# the reason"])])

    def test_removed_lines_do_not_advance_the_counter(self):
        diff = "@@ -1,2 +1,1 @@\n-int old;\n-int older;\n+// the reason\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [(1, ["// the reason"])])

    def test_second_hunk_resets_the_counter(self):
        diff = "@@ -0,0 +3,1 @@\n+// one\n@@ -0,0 +40,1 @@\n+// two\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [(3, ["// one"]), (40, ["// two"])])

    def test_file_header_lines_are_ignored(self):
        diff = "+++ b/src/a.c\n@@ -0,0 +1,1 @@\n+// the reason\n"
        self.assertEqual(prose.parse_diff(diff, "c"), [(1, ["// the reason"])])

    def test_line_added_inside_an_open_block_is_a_comment(self):
        # The diff shows only the added ` * two`, which is indistinguishable
        # from a dereference without the on-disk context. Editing an existing
        # block comment is the common case, so this must not be missed.
        disk = ["/**", " * one", " * two", " */", "int x;"]
        diff = "@@ -2,0 +3,1 @@\n+ * two\n"
        self.assertEqual(prose.parse_diff(diff, "c", disk), [(3, [" * two"])])

    def test_line_added_outside_a_block_is_still_not_a_comment(self):
        disk = ["int x;", "*ptr = x;"]
        diff = "@@ -1,0 +2,1 @@\n+*ptr = x;\n"
        self.assertEqual(prose.parse_diff(diff, "c", disk), [])


class BlockOpenBeforeTest(unittest.TestCase):
    def test_open_inside_a_block(self):
        self.assertTrue(prose._block_open_before(["/**", " * one"], 3))

    def test_closed_after_the_terminator(self):
        self.assertFalse(prose._block_open_before(["/**", " */"], 3))

    def test_closed_at_the_top_of_a_file(self):
        self.assertFalse(prose._block_open_before(["int x;"], 1))

    def test_reopened_by_a_second_block(self):
        self.assertTrue(prose._block_open_before(["/* a */", "/* b"], 3))


class HeaderSpanTest(unittest.TestCase):
    def test_line_comment_header(self):
        lines = ["// one", "// two", "", "int x;"]
        self.assertEqual(prose.header_span(lines, "c"), (1, 2))

    def test_block_comment_header(self):
        lines = ["/**", " * one", " */", "int x;"]
        self.assertEqual(prose.header_span(lines, "c"), (1, 3))

    def test_skips_leading_blanks(self):
        lines = ["", "", "// one", "int x;"]
        self.assertEqual(prose.header_span(lines, "c"), (3, 3))

    def test_skips_shebang(self):
        lines = ["#!/usr/bin/env bash", "# one", "# two", "set -e"]
        self.assertEqual(prose.header_span(lines, "hash"), (2, 3))

    def test_no_header_when_code_comes_first(self):
        self.assertIsNone(prose.header_span(["int x;", "// later"], "c"))

    def test_empty_file(self):
        self.assertIsNone(prose.header_span([], "c"))


class ClassifyTest(unittest.TestCase):
    def test_block_inside_the_header_span_is_a_header(self):
        got = prose.classify("/**", 1, (1, 6), "c")
        self.assertEqual(got, prose.HEADER)

    def test_rust_bang_doc_at_file_top_is_doc_not_header(self):
        # `//!` is rendered by cargo doc, and many projects put one on
        # every crate root, so it never takes the header cap.
        got = prose.classify("//! the crate", 1, (1, 40), "rust")
        self.assertEqual(got, prose.DOC)

    def test_rust_slash_doc_at_file_top_is_doc(self):
        self.assertEqual(prose.classify("/// item", 1, (1, 4), "rust"), prose.DOC)

    def test_plain_slash_comment_at_rust_file_top_is_a_header(self):
        self.assertEqual(prose.classify("// note", 1, (1, 2), "rust"), prose.HEADER)

    def test_doxygen_block_below_the_header_is_doc(self):
        self.assertEqual(prose.classify("/**", 40, (1, 6), "c"), prose.DOC)

    def test_line_comment_below_the_header_is_explanatory(self):
        self.assertEqual(prose.classify("// why", 40, (1, 6), "c"), prose.EXPLANATORY)

    def test_no_header_span_means_no_header(self):
        self.assertEqual(prose.classify("// why", 1, None, "c"), prose.EXPLANATORY)

    def test_hash_block_below_the_header_is_explanatory(self):
        self.assertEqual(prose.classify("# why", 30, (2, 4), "hash"), prose.EXPLANATORY)


from unittest import mock


def name_status(paths):
    """`git diff -M -z --name-status` output for modified paths."""
    return "".join(f"M\0{p}\0" for p in paths)


class ExtractTest(unittest.TestCase):
    """`_git` and file reads are mocked; no real repository is built."""

    def _extract(self, files, diffs, contents):
        def fake_git(args, cwd=None):
            if "--name-status" in args:
                return mock.Mock(stdout=name_status(files), returncode=0)
            path = args[-1]
            return mock.Mock(stdout=diffs.get(path, ""), returncode=0)

        def fake_read(path, cwd=None):
            return contents.get(path)

        with (
            mock.patch.object(prose, "_git", side_effect=fake_git),
            mock.patch.object(prose, "_read_lines", side_effect=fake_read),
        ):
            return prose.extract("origin/master", "/tmp/repo")

    def test_explanatory_block_is_extracted(self):
        blocks = self._extract(
            ["src/a.c"],
            {"src/a.c": "@@ -0,0 +40,1 @@\n+// the reason\n"},
            {"src/a.c": ["int x;"] * 60},
        )
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.kind, prose.EXPLANATORY)
        self.assertEqual(block.path, "src/a.c")
        self.assertEqual((block.start, block.end), (40, 40))
        self.assertEqual(block.normalized, "the reason")

    def test_header_carries_the_whole_on_disk_length(self):
        # Only two lines are added, but the header on disk is ten, and the
        # cap is charged against the whole thing.
        disk = ["/**"] + [" * line"] * 8 + [" */"] + ["int x;"]
        blocks = self._extract(
            ["src/a.c"],
            {"src/a.c": "@@ -3,0 +4,2 @@\n+ * line\n+ * line\n"},
            {"src/a.c": disk},
        )
        self.assertEqual(blocks[0].kind, prose.HEADER)
        self.assertEqual(blocks[0].header_lines, 10)

    def test_unscanned_files_are_skipped(self):
        blocks = self._extract(["README.md"], {}, {})
        self.assertEqual(blocks, [])

    def test_missing_file_is_skipped(self):
        blocks = self._extract(["src/gone.c"], {"src/gone.c": "@@ -0,0 +1,1 @@\n+// x\n"}, {})
        self.assertEqual(blocks, [])

    def test_ids_are_assigned(self):
        blocks = self._extract(
            ["src/a.c"],
            {"src/a.c": "@@ -0,0 +40,1 @@\n+// the reason\n"},
            {"src/a.c": ["int x;"] * 60},
        )
        self.assertEqual(blocks[0].id, prose.block_id(prose.EXPLANATORY, "the reason"))


class ChangedFilesTest(unittest.TestCase):
    def test_excludes_vendor_build_and_plans(self):
        out = name_status(
            [
                "src/a.c",
                "vendor/x.c",
                "build/y.c",
                "build_output/y.c",
                "build-debug/y.c",
                "target/z.rs",
                ".worktrees/w/a.c",
                "third_party/t.c",
            ]
        )
        with mock.patch.object(prose, "_git", return_value=mock.Mock(stdout=out, returncode=0)):
            self.assertEqual(prose.changed_files("origin/master", "/tmp"), ["src/a.c"])

    def test_does_not_exclude_builder_directory(self):
        out = name_status(["src/a.c", "builder/main.py"])
        with mock.patch.object(prose, "_git", return_value=mock.Mock(stdout=out, returncode=0)):
            self.assertEqual(
                prose.changed_files("origin/master", "/tmp"),
                ["src/a.c", "builder/main.py"],
            )

    def test_uses_the_added_modified_filter(self):
        captured = {}

        def fake_git(args, cwd=None):
            captured["args"] = args
            return mock.Mock(stdout="", returncode=0)

        with mock.patch.object(prose, "_git", side_effect=fake_git):
            prose.changed_files("origin/master", "/tmp")
        self.assertIn("--diff-filter=ACMR", captured["args"])


def make_block(kind=prose.EXPLANATORY, path="src/a.c", n=1, header_lines=0):
    lines = ["// line"] * n
    normalized = prose.normalize(lines)
    return prose.Block(
        id=prose.block_id(kind, normalized),
        kind=kind,
        path=path,
        start=10,
        end=10 + n - 1,
        lines=lines,
        normalized=normalized,
        header_lines=header_lines,
    )


class IsTestPathTest(unittest.TestCase):
    def test_recognises_test_and_example_trees(self):
        self.assertTrue(prose.is_test_path("tests/combo/a.c"))
        self.assertTrue(prose.is_test_path("lib-lcd/tests/a.c"))
        self.assertTrue(prose.is_test_path("examples/a.c"))
        self.assertTrue(prose.is_test_path("lib/examples/a.c"))

    def test_source_paths(self):
        self.assertFalse(prose.is_test_path("src/a.c"))
        self.assertFalse(prose.is_test_path("lib-lcd/src/latest.c"))


class LineRulesTest(unittest.TestCase):
    def test_explanatory_over_the_cap_fails(self):
        found = prose.check_line_rules(make_block(n=9))
        self.assertIsNotNone(found)
        self.assertIn("hard cap", found.message)

    def test_explanatory_at_the_cap_passes(self):
        self.assertIsNone(prose.check_line_rules(make_block(n=8)))

    def test_source_header_over_eight_fails(self):
        block = make_block(kind=prose.HEADER, path="src/a.c", n=2, header_lines=25)
        found = prose.check_line_rules(block)
        self.assertIsNotNone(found)
        self.assertIn("rephrase", found.message)
        self.assertIn("25", found.message)

    def test_source_header_at_eight_passes(self):
        block = make_block(kind=prose.HEADER, path="src/a.c", n=2, header_lines=8)
        self.assertIsNone(prose.check_line_rules(block))

    def test_test_tree_header_has_no_cap(self):
        block = make_block(kind=prose.HEADER, path="tests/a.c", n=2, header_lines=84)
        self.assertIsNone(prose.check_line_rules(block))

    def test_doc_has_no_line_cap(self):
        self.assertIsNone(prose.check_line_rules(make_block(kind=prose.DOC, n=40)))


def content_block(lines, kind=prose.EXPLANATORY, path="src/a.c"):
    normalized = prose.normalize(lines)
    return prose.Block(
        id=prose.block_id(kind, normalized),
        kind=kind,
        path=path,
        start=1,
        end=len(lines),
        lines=lines,
        normalized=normalized,
        header_lines=0,
    )


class ContentChecksTest(unittest.TestCase):
    def messages(self, lines, **kwargs):
        return " | ".join(v.message for v in prose.check_content(content_block(lines, **kwargs)))

    def test_file_decoration_fails(self):
        got = self.messages(["/**", " * @file a.c", " */"], kind=prose.HEADER)
        self.assertIn("decoration", got)

    def test_narrative_fixed_it_by_fails(self):
        self.assertIn("narrative", self.messages(["// fixed it by reordering"]))

    def test_narrative_verified_by_fails(self):
        self.assertIn("narrative", self.messages(["// verified by simulation"]))

    def test_narrative_used_to_be_fails(self):
        self.assertIn("narrative", self.messages(["// this used to be banked"]))

    def test_fixed_point_is_not_narrative(self):
        # Numeric code is full of fixed-point arithmetic; a bare `fixed`
        # must never trip the check.
        self.assertEqual(self.messages(["// fixed point Q8.8 scaling"]), "")

    def test_em_dash_fails(self):
        self.assertIn("em-dash", self.messages(["// the reason \u2014 and more"]))

    def test_em_dash_in_ascii_art_passes(self):
        art = ["// ┌──┐", "// │ a \u2014 b │"]
        self.assertEqual(self.messages(art), "")

    def test_clean_block_has_no_violations(self):
        self.assertEqual(self.messages(["// datasheet 80329B 9.2 needs the read"]), "")

    def test_several_violations_are_all_reported(self):
        got = self.messages(["// verified by sim \u2014 and more"])
        self.assertIn("narrative", got)
        self.assertIn("em-dash", got)


class VerifyTest(unittest.TestCase):
    def _verify(self, blocks):
        with mock.patch.object(prose, "extract", return_value=blocks):
            return prose.verify("origin/master", "/tmp/repo")

    def test_clean_explanatory_block_passes(self):
        self.assertEqual(self._verify([make_block(n=1)]), [])

    def test_clean_long_explanatory_block_passes(self):
        self.assertEqual(self._verify([make_block(n=6)]), [])

    def test_header_needs_no_justification(self):
        block = make_block(kind=prose.HEADER, path="src/a.c", n=2, header_lines=6)
        self.assertEqual(self._verify([block]), [])

    def test_doc_needs_no_justification(self):
        self.assertEqual(self._verify([make_block(kind=prose.DOC, n=20)]), [])

    def test_over_cap_block_fails(self):
        found = self._verify([make_block(n=12)])
        self.assertTrue(any("hard cap" in v.message for v in found))

    def test_content_violation_fails(self):
        block = content_block(["// verified by simulation"])
        found = self._verify([block])
        self.assertTrue(any("narrative" in v.message for v in found))

    def test_doc_with_content_violation_fails(self):
        block = content_block(["/// verified by simulation"], kind=prose.DOC)
        found = self._verify([block])
        self.assertTrue(any("narrative" in v.message for v in found))

    def test_all_violations_are_reported(self):
        block = make_block(n=12)
        found = self._verify([block])
        self.assertEqual(len(found), 1)


import io
from contextlib import redirect_stdout, redirect_stderr

from corral import cli


class RenderTest(unittest.TestCase):
    def test_block_listing_shows_location_id_and_text(self):
        block = make_block(n=2)
        out = prose.render_blocks([block])
        self.assertIn(block.id, out)
        self.assertIn("src/a.c:10-11", out)
        self.assertIn("// line", out)

    def test_listing_shows_every_block_clean_or_not(self):
        out = prose.render_blocks([make_block(n=1), make_block(n=9)])
        self.assertEqual(out.count("src/a.c:"), 2)

    def test_empty_listing_says_so(self):
        self.assertIn("no comment blocks", prose.render_blocks([]))

    def test_violation_listing_names_the_file_and_id(self):
        found = [prose.Violation("src/a.c", 3, 4, "abc123abc123", "narrative")]
        out = prose.render_violations(found)
        self.assertIn("src/a.c:3-4", out)
        self.assertIn("abc123abc123", out)
        self.assertIn("1 blocking", out)


class ProseCommandTest(unittest.TestCase):
    def test_prose_help(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["prose", "--help"]), 0)
        self.assertIn("--verify", buf.getvalue())

    def test_listing_exits_zero(self):
        with mock.patch.object(prose, "extract", return_value=[]), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["prose"]), 0)

    def test_verify_exits_one_on_violations(self):
        found = [prose.Violation("src/a.c", 1, 1, "abc123abc123", "narrative")]
        with mock.patch.object(prose, "verify", return_value=found), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["prose", "--verify"]), 1)

    def test_verify_exits_zero_when_clean(self):
        with mock.patch.object(prose, "verify", return_value=[]), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["prose", "--verify"]), 0)


if __name__ == "__main__":
    unittest.main()
