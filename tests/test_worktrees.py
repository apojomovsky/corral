"""One worktree per ticket: naming, the worktree list, and ensure_worktree.

Real git in temp dirs. The bare repository in a temp dir is `origin`, so
nothing leaves the machine.
"""

import os
import shutil
import tempfile
import unittest

from corral import worktrees
from corral.worktrees import WorktreeError

from tests.test_reviewrecord import git_in


class SlugifyTest(unittest.TestCase):
    def test_drops_a_conventional_prefix_and_lowercases(self):
        self.assertEqual(
            worktrees.slugify("feat(api)!: Handle the empty input"),
            "handle-the-empty-input",
        )

    def test_punctuation_and_runs_of_separators_become_one_hyphen(self):
        self.assertEqual(
            worktrees.slugify("Add a parser,  with   punctuation!"), "add-a-parser-with-punctuation"
        )

    def test_cuts_at_the_last_whole_word_within_forty_characters(self):
        title = "aaaa " * 12
        slug = worktrees.slugify(title)
        self.assertEqual(slug, "-".join(["aaaa"] * 8)[:39])
        self.assertLessEqual(len(slug), worktrees.MAX_SLUG)

    def test_one_word_longer_than_the_limit_is_cut_as_is(self):
        self.assertEqual(worktrees.slugify("x" * 50), "x" * 40)

    def test_falls_back_to_ticket_when_nothing_is_left(self):
        for title in (None, "", "feat:", "feat: !!!"):
            with self.subTest(title=title):
                self.assertEqual(worktrees.slugify(title), "ticket")


class BranchForTest(unittest.TestCase):
    def test_the_type_comes_from_a_known_conventional_prefix(self):
        self.assertEqual(worktrees.branch_for(7, "fix(api)!: handle it"), "fix/7-handle-it")
        self.assertEqual(worktrees.branch_for(7, "Docs: Write it"), "docs/7-write-it")
        self.assertEqual(worktrees.branch_for(7, "revert: undo"), "revert/7-undo")

    def test_an_unknown_or_missing_prefix_means_feat(self):
        self.assertEqual(worktrees.branch_for(7, "wip: half done"), "feat/7-half-done")
        self.assertEqual(worktrees.branch_for(7, "No prefix here"), "feat/7-no-prefix-here")
        self.assertEqual(worktrees.branch_for(7, None), "feat/7-ticket")

    def test_the_default_template_is_the_git_branch_setting(self):
        default = worktrees.settings.REGISTRY["git.branch"].default
        self.assertEqual(default, "{type}/{issue}-{slug}")
        self.assertEqual(worktrees.branch_for(3, "fix: x", template=default), "fix/3-x")

    def test_an_explicit_template_is_rendered(self):
        self.assertEqual(
            worktrees.branch_for(3, "fix: x", template="wip/{issue}/{type}-{slug}"),
            "wip/3/fix-x",
        )

    def test_a_template_with_an_unknown_field_is_refused(self):
        with self.assertRaises(WorktreeError):
            worktrees.branch_for(3, "fix: x", template="{nope}/{issue}")


class ParseWorktreeListTest(unittest.TestCase):
    def test_maps_each_path_to_its_branch_and_detached_to_empty(self):
        text = (
            "worktree /srv/clone\n"
            "HEAD 1111\n"
            "branch refs/heads/master\n"
            "\n"
            "worktree /srv/clone/.worktrees/7-x\n"
            "HEAD 2222\n"
            "branch refs/heads/feat/7-x\n"
            "\n"
            "worktree /srv/clone/.worktrees/9-y\n"
            "HEAD 3333\n"
            "detached\n"
        )
        self.assertEqual(
            worktrees.parse_worktree_list(text),
            {
                "/srv/clone": "master",
                "/srv/clone/.worktrees/7-x": "feat/7-x",
                "/srv/clone/.worktrees/9-y": "",
            },
        )

    def test_empty_input_is_no_worktrees(self):
        self.assertEqual(worktrees.parse_worktree_list(""), {})
        self.assertEqual(worktrees.parse_worktree_list(None), {})


class TicketBranchesTest(unittest.TestCase):
    def test_returns_only_the_ticket_sorted(self):
        names = [
            "feat/7-x",
            "fix/70-y",
            "feat/7-a",
            "master",
            "docs/7",
            "ci/22-z",
            "feat/22-b",
            " feat/7-q ",
            "fix/2-x",
        ]
        self.assertEqual(worktrees.ticket_branches(7, names), ["feat/7-a", "feat/7-q", "feat/7-x"])

    def test_a_number_is_matched_whole(self):
        self.assertEqual(worktrees.ticket_branches(2, ["fix/22-x", "fix/2-x"]), ["fix/2-x"])


class EnsureWorktreeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.origin = os.path.join(self.tmp, "origin.git")
        seed = os.path.join(self.tmp, "seed")
        git_in(self.tmp, "init", "-q", "--bare", "-b", "master", self.origin)
        git_in(self.tmp, "init", "-q", "-b", "master", seed)
        git_in(seed, "commit", "-q", "--allow-empty", "-m", "one")
        git_in(seed, "remote", "add", "origin", self.origin)
        git_in(seed, "push", "-q", "origin", "master")
        self.main = os.path.join(self.tmp, "main")
        git_in(self.tmp, "clone", "-q", self.origin, self.main)
        self.seed = seed

    def ensure(self, number, **kwargs):
        kwargs.setdefault("default_branch", "master")
        return worktrees.ensure_worktree(self.main, number, **kwargs)

    def worktree_count(self):
        return len(
            worktrees.parse_worktree_list(git_in(self.main, "worktree", "list", "--porcelain"))
        )

    def test_a_new_ticket_gets_a_worktree_on_a_branch_from_origin_default(self):
        workdir, branch = self.ensure(7, title="feat(api): add the widget")
        self.assertEqual(branch, "feat/7-add-the-widget")
        self.assertEqual(
            workdir, os.path.join(os.path.realpath(self.main), ".worktrees", "7-add-the-widget")
        )
        self.assertTrue(os.path.isdir(workdir))
        self.assertEqual(git_in(workdir, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertEqual(
            git_in(workdir, "rev-parse", "HEAD"), git_in(self.main, "rev-parse", "origin/master")
        )

    def test_several_tickets_get_separate_worktrees(self):
        first, _ = self.ensure(7, title="fix: first")
        second, _ = self.ensure(22, title="feat: second")
        self.assertNotEqual(first, second)
        self.assertEqual(os.path.basename(first), "7-first")
        self.assertEqual(os.path.basename(second), "22-second")
        self.assertEqual(self.worktree_count(), 3)

    def test_an_existing_worktree_on_the_ticket_branch_is_reused(self):
        first = self.ensure(7, title="feat: x")
        again = self.ensure(7, title="feat: x")
        self.assertEqual(first, again)
        self.assertEqual(self.worktree_count(), 2)

    def test_a_local_ticket_branch_without_a_worktree_is_reused(self):
        git_in(self.main, "branch", "feat/7-old-name", "origin/master")
        workdir, branch = self.ensure(7, title="feat: a different title")
        self.assertEqual(branch, "feat/7-old-name")
        self.assertEqual(os.path.basename(workdir), "7-old-name")

    def test_a_branch_on_origin_only_is_reused_at_its_head(self):
        git_in(self.seed, "checkout", "-q", "-b", "fix/7-pr-head")
        git_in(self.seed, "commit", "-q", "--allow-empty", "-m", "pr work")
        git_in(self.seed, "push", "-q", "origin", "fix/7-pr-head")
        head = git_in(self.seed, "rev-parse", "HEAD")
        workdir, branch = self.ensure(7)
        self.assertEqual(branch, "fix/7-pr-head")
        self.assertEqual(git_in(workdir, "rev-parse", "HEAD"), head)

    def test_an_explicit_branch_is_used_as_given(self):
        workdir, branch = self.ensure(7, branch="feat/7-custom")
        self.assertEqual(branch, "feat/7-custom")
        self.assertEqual(os.path.basename(workdir), "7-custom")

    def test_a_custom_root_is_honoured_relative_and_absolute(self):
        rel, _ = self.ensure(7, title="feat: rel", root="custom/trees")
        self.assertEqual(rel, os.path.join(os.path.realpath(self.main), "custom", "trees", "7-rel"))
        absolute = os.path.join(self.tmp, "elsewhere")
        abs_dir, _ = self.ensure(22, title="feat: abs", root=absolute)
        self.assertEqual(abs_dir, os.path.join(absolute, "22-abs"))

    def test_the_git_seam_is_the_only_way_git_runs(self):
        calls = []

        def recording(args, cwd):
            calls.append((args[0], cwd))
            return worktrees.git(args, cwd)

        self.ensure(7, title="feat: seam", git=recording)
        self.assertTrue(calls)
        self.assertEqual(calls[0][0], "fetch")
        self.assertTrue(all(cwd == os.path.realpath(self.main) for _, cwd in calls))

    def test_refused_when_the_main_clone_sits_on_the_ticket_branch(self):
        git_in(self.main, "checkout", "-q", "-b", "feat/7-here")
        with self.assertRaisesRegex(WorktreeError, "main clone"):
            self.ensure(7, title="feat: here")

    def test_refused_when_a_registered_worktree_is_missing_on_disk(self):
        workdir, _ = self.ensure(7, title="feat: gone")
        shutil.rmtree(workdir)
        with self.assertRaisesRegex(WorktreeError, "worktree prune"):
            self.ensure(7, title="feat: gone")

    def test_refused_when_a_leftover_directory_is_not_a_worktree_on_the_branch(self):
        os.makedirs(os.path.join(self.main, ".worktrees", "7-leftover"))
        with self.assertRaisesRegex(WorktreeError, "not a worktree"):
            self.ensure(7, title="feat: leftover")

    def test_refused_without_a_branch_or_a_title(self):
        with self.assertRaisesRegex(WorktreeError, "no title"):
            self.ensure(7)

    def test_refused_without_a_main_clone(self):
        with self.assertRaisesRegex(WorktreeError, "no main clone"):
            worktrees.ensure_worktree(
                os.path.join(self.tmp, "absent"), 7, default_branch="master", title="feat: x"
            )

    def test_refused_when_origin_cannot_be_fetched(self):
        git_in(self.main, "remote", "set-url", "origin", os.path.join(self.tmp, "absent.git"))
        with self.assertRaisesRegex(WorktreeError, "cannot fetch origin"):
            self.ensure(7, title="feat: x")

    def test_refused_for_a_ticket_number_that_is_not_positive(self):
        for number in (0, -3, True, "7"):
            with self.subTest(number=number), self.assertRaises(WorktreeError):
                self.ensure(number, title="feat: x")


if __name__ == "__main__":
    unittest.main()
