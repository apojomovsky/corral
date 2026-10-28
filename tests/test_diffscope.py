import os
import subprocess
import tempfile
import unittest
from unittest import mock

from corral import diffscope, prose

LONG_BLOCK = "\n".join(f"// reason line {n}" for n in range(12))
C_SOURCE = LONG_BLOCK + "\nint value(void)\n{\n    return 1;\n}\n"


def fake_git(stdout):
    return lambda args, cwd=None: mock.Mock(stdout=stdout, returncode=0)


def brief_only(path):
    """A rename context like a C-docs gate's: tests and examples are a lighter tier."""
    return "/tests/" in path or "/examples/" in path or path.startswith(("tests/", "examples/"))


def tier_and_vendored(path):
    """A rename context that also tracks the vendored rule."""
    return (brief_only(path), diffscope.is_excluded(path))


class ParseTest(unittest.TestCase):
    def test_added_and_modified_are_content_changes(self):
        out = "A\0src/new.c\0M\0src/old.c\0"
        self.assertEqual(
            diffscope.changes("b", fake_git(out)),
            [
                diffscope.Change("src/new.c", None, False),
                diffscope.Change("src/old.c", None, False),
            ],
        )

    def test_a_pure_rename_is_dropped(self):
        out = "R100\0old/a.c\0new/a.c\0"
        self.assertEqual(diffscope.content_changes("b", fake_git(out)), [])

    def test_a_rename_with_edits_keeps_both_paths(self):
        out = "R087\0old/a.c\0new/a.c\0"
        self.assertEqual(
            diffscope.content_changes("b", fake_git(out)),
            [diffscope.Change("new/a.c", "old/a.c", False)],
        )

    def test_a_pure_copy_is_dropped(self):
        out = "C100\0old/a.c\0new/a.c\0"
        self.assertEqual(diffscope.content_changes("b", fake_git(out)), [])

    def test_a_copy_with_edits_keeps_both_paths(self):
        out = "C086\0old/a.c\0new/a.c\0"
        self.assertEqual(
            diffscope.content_changes("b", fake_git(out)),
            [diffscope.Change("new/a.c", "old/a.c", False)],
        )

    def test_mixed_entries_stay_in_step(self):
        out = "R100\0a\0b\0M\0c\0R050\0d\0e\0A\0f\0"
        self.assertEqual(
            [c.path for c in diffscope.content_changes("b", fake_git(out))],
            ["c", "e", "f"],
        )

    def test_pathspec_is_passed_after_the_separator(self):
        seen = {}

        def git(args, cwd=None):
            seen["args"] = args
            return mock.Mock(stdout="", returncode=0)

        diffscope.changes("b", git, pathspec=("*.c",))
        self.assertEqual(seen["args"][-2:], ["--", "*.c"])
        self.assertIn("-M", seen["args"])


class RealRepoTest(unittest.TestCase):
    """A real temporary repository: git's own rename detection decides."""

    def setUp(self):
        # A git hook exports GIT_DIR and GIT_INDEX_FILE, which would make
        # every git call below act on the real repository, not the fixture.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in [n for n in os.environ if n.startswith("GIT_")]:
            del os.environ[name]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "master")
        for d in ("old", "docs", "vendor", "tests"):
            os.makedirs(os.path.join(self.repo, d))
        self.write("old/a.c", C_SOURCE)
        self.write("docs/n.txt", "// old reason\nnote\n")
        self.write("vendor/v.c", "// vendored reason\nint v;\n")
        self.write("tests/t.c", "int t;\n")
        self.git("add", "-A")
        self.commit("base")
        self.git("checkout", "-q", "-b", "work")

    def git(self, *args):
        # Belt and braces with the setUp scrub below: per-call, so this
        # helper stays safe even when hook-like GIT_* vars are set.
        from corral.gitenv import clean_env

        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
            env=clean_env(),
        )

    def commit(self, msg):
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def move(self, edit=None):
        os.makedirs(os.path.join(self.repo, "new"))
        self.git("mv", "old/a.c", "new/a.c")
        if edit:
            self.write("new/a.c", edit)
        self.git("add", "-A")
        self.commit("move")

    def test_a_pure_move_charges_no_prose(self):
        self.move()
        self.assertEqual(prose.changed_files("master", cwd=self.repo), [])
        self.assertEqual(prose.extract("master", cwd=self.repo), [])

    def test_a_pure_move_leaves_no_c_files_for_a_c_file_gate(self):
        self.move()
        self.assertEqual(
            diffscope.content_changes("master", prose._git, cwd=self.repo, pathspec=("*.c", "*.h")),
            [],
        )

    def test_an_edited_move_charges_only_the_edited_comment(self):
        self.move(edit=C_SOURCE + "// a new reason\nint other;\n")
        blocks = prose.extract("master", cwd=self.repo)
        self.assertEqual([b.normalized for b in blocks], ["a new reason"])
        self.assertEqual(blocks[0].path, "new/a.c")

    def move_leaving_symlink(self, edit=None):
        os.makedirs(os.path.join(self.repo, "new"))
        os.rename(os.path.join(self.repo, "old/a.c"), os.path.join(self.repo, "new/a.c"))
        if edit:
            self.write("new/a.c", edit)
        os.symlink("../new/a.c", os.path.join(self.repo, "old/a.c"))
        self.git("add", "-A")
        self.commit("move")

    def test_a_move_leaving_a_symlink_charges_no_prose(self):
        self.move_leaving_symlink()
        self.assertEqual(prose.changed_files("master", cwd=self.repo), [])
        self.assertEqual(prose.extract("master", cwd=self.repo), [])

    def test_a_symlinked_move_with_an_edit_charges_only_the_edit(self):
        edited = C_SOURCE.replace("// reason line 3", "// reason line 3 edited")
        self.move_leaving_symlink(edit=edited)
        blocks = prose.extract("master", cwd=self.repo)
        self.assertEqual([b.normalized for b in blocks], ["reason line 3 edited"])
        self.assertEqual(blocks[0].path, "new/a.c")

    def test_an_edited_move_is_still_a_c_file_for_a_c_file_gate(self):
        self.move(edit=C_SOURCE + "int other;\n")
        got = diffscope.content_changes(
            "master", prose._git, cwd=self.repo, pathspec=("*.c", "*.h")
        )
        self.assertEqual([c.path for c in got], ["new/a.c"])

    def mv(self, old, new):
        os.makedirs(os.path.dirname(os.path.join(self.repo, new)), exist_ok=True)
        self.git("mv", old, new)
        self.git("add", "-A")
        self.commit("move")

    def test_a_move_from_an_unscanned_extension_is_charged_whole(self):
        self.mv("docs/n.txt", "src/n.c")
        blocks = prose.extract("master", cwd=self.repo)
        self.assertEqual([b.normalized for b in blocks], ["old reason"])

    def test_a_move_out_of_an_excluded_directory_is_charged_whole(self):
        self.mv("vendor/v.c", "src/v.c")
        blocks = prose.extract("master", cwd=self.repo)
        self.assertEqual([b.path for b in blocks], ["src/v.c"])

    def test_a_move_out_of_tests_is_held_to_the_full_rules(self):
        self.mv("tests/t.c", "src/t.c")
        got = diffscope.content_changes(
            "master", prose._git, cwd=self.repo, pathspec=("*.c", "*.h"), context=brief_only
        )
        self.assertEqual([c.path for c in got], ["src/t.c"])

    def test_a_move_within_tests_stays_dropped(self):
        self.mv("tests/t.c", "tests/sub/t.c")
        got = diffscope.content_changes(
            "master", prose._git, cwd=self.repo, pathspec=("*.c", "*.h"), context=brief_only
        )
        self.assertEqual(got, [])

    def test_a_move_out_of_vendor_is_charged_whole_by_a_c_file_gate(self):
        self.mv("vendor/v.c", "src/v.c")
        got = diffscope.content_changes(
            "master", prose._git, cwd=self.repo, pathspec=("*.c", "*.h"), context=tier_and_vendored
        )
        self.assertEqual([c.path for c in got], ["src/v.c"])

    def test_an_unknown_base_fails_closed(self):
        with self.assertRaises(diffscope.DiffError):
            diffscope.changes("no-such-ref", prose._git, cwd=self.repo)
        with self.assertRaises(diffscope.DiffError):
            prose.extract("no-such-ref", cwd=self.repo)

    def test_prose_cli_exits_4_on_an_unknown_base(self):
        import argparse

        from corral import cli

        before = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, before)
        with mock.patch("sys.stderr"):
            rc = cli.cmd_prose(argparse.Namespace(base="no-such-ref", verify=True))
        self.assertEqual(rc, 4)

    def test_a_plain_added_file_is_charged_whole(self):
        self.write("added.c", "// a reason\nint x;\n")
        self.git("add", "-A")
        self.commit("add")
        blocks = prose.extract("master", cwd=self.repo)
        self.assertEqual([b.path for b in blocks], ["added.c"])

    def test_the_vendored_pattern_is_shared(self):
        self.assertIs(prose._EXCLUDED, diffscope.EXCLUDED)
        for path in (
            "vendor/v.c",
            "third_party/a.h",
            "lib/third_party/b.c",
            "target/x.c",
            "build/x.c",
            ".worktrees/w/y.c",
        ):
            self.assertTrue(diffscope.is_excluded(path), path)
        self.assertFalse(diffscope.is_excluded("src/main.c"))


class UnderAHookTest(RealRepoTest):
    """The same fixture with hook variables aimed at a sacrificial repo.

    Regression guard: a fixture `git init` inheriting
    hook GIT_* vars re-initializes the outer repo instead of its own cwd
    (observed: flips the shared core.bare). Every git call below, the
    fixture setup here and the reads via prose inside the tests,
    must resolve against the fixture: the inherited assertions catch
    read-misdirection (they fail on the outer repo's data), and the
    cleanup below catches write-corruption, per test.
    """

    def setUp(self):
        super().setUp()
        from corral.gitenv import clean_env

        # Beside the fixture, never inside it: the fixture diffs its own
        # tree, so anything under it would show up as content.
        self.outer_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.outer_tmp.cleanup)
        self.outer = os.path.join(self.outer_tmp.name, "outer")
        os.makedirs(self.outer)
        for args in (
            ["init", "-q", "-b", "master"],
            [
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "base",
            ],
        ):
            subprocess.run(
                ["git", *args],
                cwd=self.outer,
                check=True,
                capture_output=True,
                text=True,
                env=clean_env(),
            )
        with open(os.path.join(self.outer, ".git", "config"), "rb") as f:
            self.outer_before = f.read()
        hook = {
            "GIT_DIR": os.path.join(self.outer, ".git"),
            "GIT_INDEX_FILE": os.path.join(self.outer, ".git", "index"),
            "GIT_WORK_TREE": self.outer,
            "GIT_PREFIX": "",
        }
        patcher = mock.patch.dict(os.environ, hook)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._assert_outer_untouched)

    def _assert_outer_untouched(self):
        with open(os.path.join(self.outer, ".git", "config"), "rb") as f:
            now = f.read()
        self.assertEqual(
            now, self.outer_before, "hook env redirected a git write at the outer repo"
        )


class DefaultBaseTest(unittest.TestCase):
    def test_flag_then_env_then_the_default(self):
        self.assertEqual(diffscope.default_base("main", {"BASE_REF": "x"}), "main")
        self.assertEqual(diffscope.default_base(None, {"BASE_REF": "x"}), "x")
        self.assertEqual(diffscope.default_base(None, {"BASE_REF": ""}), "origin/master")
        self.assertEqual(diffscope.default_base(None, {}), "origin/master")

    def test_no_other_module_reads_base_ref(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        readers = []
        for here, _, names in os.walk(src):
            for name in names:
                if name.endswith(".py"):
                    path = os.path.join(here, name)
                    with open(path, encoding="utf-8") as handle:
                        if "BASE_REF" in handle.read():
                            readers.append(os.path.relpath(path, src))
        self.assertEqual(readers, [os.path.join("corral", "diffscope.py")])


class CallersUseTheResolverTest(unittest.TestCase):
    """Each --base caller takes $BASE_REF when no flag is given."""

    def setUp(self):
        patch = mock.patch.dict(os.environ, {"BASE_REF": "origin/elsewhere"})
        patch.start()
        self.addCleanup(patch.stop)

    def quiet(self, fn, *args):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return fn(*args)

    def test_prose(self):
        from corral import cli

        with mock.patch.object(prose, "extract", return_value=[]) as extract:
            self.quiet(cli.main, ["prose"])
        self.assertEqual(extract.call_args.args[0], "origin/elsewhere")

    def test_budget_verb_and_module(self):
        from corral import budget, cli

        with mock.patch.object(budget, "dispatch_block", return_value="") as block:
            self.quiet(cli.main, ["budget"])
            self.quiet(budget.main_cli, [])
            self.quiet(budget.main_cli, ["--base", "x"])
        self.assertEqual(
            [c.args[0] for c in block.call_args_list], ["origin/elsewhere", "origin/elsewhere", "x"]
        )

    def test_review_show(self):
        from corral import cli, mergeup

        with mock.patch.object(mergeup, "find_record", return_value=(None, "")) as find:
            code = self.quiet(cli.main, ["review", "show", "lib", "1"])
        # No --pr means no authoritative base: fail closed, never consult a local ref.
        self.assertEqual(code, 1)
        find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
