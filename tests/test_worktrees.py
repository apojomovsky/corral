"""One worktree per ticket: naming, the worktree list, and ensure_worktree.

Real git in temp dirs. The bare repository in a temp dir is `origin`, so
nothing leaves the machine.
"""

import inspect
import os
import re
import shutil
import tempfile
import unittest

from corral import settings, worktrees
from corral.worktrees import WorktreeError

from tests.test_reviewrecord import git_in

TEMPLATE = "{type}/{issue}-{slug}"
ROOT = ".worktrees"
PRE_PR_CHECK = os.path.join(os.path.dirname(__file__), "..", "scripts", "pre_pr_check.py")


class SlugifyTest(unittest.TestCase):
    def test_drops_a_conventional_prefix_and_lowercases(self):
        self.assertEqual(
            worktrees.slugify("feat(api)!: Handle the empty input"),
            "handle-the-empty-input",
        )

    def test_punctuation_and_runs_of_separators_become_one_hyphen(self):
        self.assertEqual(
            worktrees.slugify("Add a parser,  with   punctuation!"),
            "add-a-parser-with-punctuation",
        )

    def test_cuts_at_the_last_whole_word_within_forty_characters(self):
        slug = worktrees.slugify("aaaa " * 12)
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
        render = lambda title: worktrees.branch_for(7, title, template=TEMPLATE)
        self.assertEqual(render("fix(api)!: handle it"), "fix/7-handle-it")
        self.assertEqual(render("Docs: Write it"), "docs/7-write-it")
        self.assertEqual(render("revert: undo"), "revert/7-undo")

    def test_an_unknown_or_missing_prefix_means_feat(self):
        render = lambda title: worktrees.branch_for(7, title, template=TEMPLATE)
        self.assertEqual(render("wip: half done"), "feat/7-half-done")
        self.assertEqual(render("No prefix here"), "feat/7-no-prefix-here")
        self.assertEqual(render(None), "feat/7-ticket")

    def test_the_template_is_the_caller_s_and_is_required(self):
        self.assertEqual(
            worktrees.branch_for(3, "fix: x", template="task/{issue}-{slug}"), "task/3-x"
        )
        for name in ("branch_for", "ensure_worktree"):
            params = inspect.signature(getattr(worktrees, name)).parameters
            for key in ("template", "branch_template", "root", "default_branch"):
                if key in params:
                    self.assertIs(params[key].default, inspect.Parameter.empty, f"{name}.{key}")
                    self.assertIs(params[key].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_a_template_that_does_not_render_the_grammar_is_refused(self):
        for template in ("feature/{issue}", "{type}/{issue}", "{nope}/{issue}-{slug}"):
            with self.subTest(template=template), self.assertRaises(WorktreeError):
                worktrees.branch_for(3, "fix: x", template=template)

    def test_the_registry_default_is_the_documented_template(self):
        self.assertEqual(settings.REGISTRY["git.branch"].default, TEMPLATE)


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


class BranchGrammarTest(unittest.TestCase):
    """TICKET_BRANCH_RE and the branch check in scripts/pre_pr_check.py agree on the type."""

    @staticmethod
    def _pre_pr_check_regex():
        with open(PRE_PR_CHECK, encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(r'^BRANCH_RE = re\.compile\(r"(.*)"\)$', source, re.MULTILINE)
        if match is None:
            raise AssertionError("BRANCH_RE not found in scripts/pre_pr_check.py")
        return re.compile(match.group(1))

    def test_both_accept_and_reject_the_same_sample_names(self):
        pre = self._pre_pr_check_regex()
        samples = [
            "feat/7-x",
            "fix/12-a.b",
            "docs/3-x_y",
            "chore/9-Mixed",
            "x/1-y",
            "Feat/7-x",
            "FEAT/7-x",
            "a1/7-x",
            "feat7-x",
            "feat/-x",
            "feat/7",
            "feat/7-",
            "feat/7-x y",
            "feat/7-x/y",
            "feat-x/7-y",
        ]
        for name in samples:
            with self.subTest(name=name):
                self.assertEqual(
                    bool(pre.match(name)),
                    bool(worktrees.TICKET_BRANCH_RE.match(name)),
                )

    def test_a_zero_padded_number_is_refused(self):
        for name in ("feat/07-x", "feat/0-x", "feat/00-x"):
            with self.subTest(name=name):
                self.assertIsNone(worktrees.TICKET_BRANCH_RE.match(name))
        self.assertIsNotNone(worktrees.TICKET_BRANCH_RE.match("feat/10-x"))


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
        kwargs.setdefault("branch_template", TEMPLATE)
        kwargs.setdefault("root", ROOT)
        return worktrees.ensure_worktree(self.main, number, **kwargs)

    def worktree_count(self):
        return len(
            worktrees.parse_worktree_list(git_in(self.main, "worktree", "list", "--porcelain"))
        )

    def real_main(self):
        return os.path.realpath(self.main)

    def push_seed_branch(self, name, commit=True):
        git_in(self.seed, "checkout", "-q", "-B", name, "master")
        if commit:
            git_in(self.seed, "commit", "-q", "--allow-empty", "-m", name)
        head = git_in(self.seed, "rev-parse", "HEAD")
        git_in(self.seed, "push", "-q", "origin", name)
        git_in(self.seed, "checkout", "-q", "master")
        return head

    def test_a_new_ticket_gets_a_worktree_on_a_branch_from_origin_default(self):
        workdir, branch = self.ensure(7, title="feat(api): add the widget")
        self.assertEqual(branch, "feat/7-add-the-widget")
        self.assertEqual(workdir, os.path.join(self.real_main(), ROOT, "7-add-the-widget"))
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

    def test_ticket_five_and_ticket_fifty_get_separate_worktrees(self):
        five, five_branch = self.ensure(5, title="feat: five")
        fifty, fifty_branch = self.ensure(50, title="feat: fifty")
        self.assertEqual(five_branch, "feat/5-five")
        self.assertEqual(fifty_branch, "feat/50-fifty")
        self.assertEqual(os.path.basename(five), "5-five")
        self.assertEqual(os.path.basename(fifty), "50-fifty")
        self.assertNotEqual(five, fifty)
        self.assertEqual(self.ensure(5)[0], five)
        self.assertEqual(self.ensure(50)[0], fifty)

    def test_an_existing_worktree_on_the_ticket_branch_is_reused(self):
        first = self.ensure(7, title="feat: x")
        self.assertEqual(first, self.ensure(7, title="feat: x"))
        self.assertEqual(self.worktree_count(), 2)

    def test_a_local_ticket_branch_without_a_worktree_is_reused(self):
        git_in(self.main, "branch", "feat/7-old-name", "origin/master")
        workdir, branch = self.ensure(7, title="feat: a different title")
        self.assertEqual(branch, "feat/7-old-name")
        self.assertEqual(os.path.basename(workdir), "7-old-name")

    def test_a_branch_on_origin_only_is_reused_at_its_head(self):
        head = self.push_seed_branch("fix/7-pr-head")
        workdir, branch = self.ensure(7)
        self.assertEqual(branch, "fix/7-pr-head")
        self.assertEqual(git_in(workdir, "rev-parse", "HEAD"), head)

    def test_an_explicit_branch_is_used_as_given(self):
        workdir, branch = self.ensure(7, branch="feat/7-custom")
        self.assertEqual(branch, "feat/7-custom")
        self.assertEqual(os.path.basename(workdir), "7-custom")

    def test_an_explicit_branch_for_another_ticket_is_refused(self):
        with self.assertRaisesRegex(WorktreeError, "not ticket #7"):
            self.ensure(7, branch="feat/8-x")

    def test_an_explicit_branch_outside_the_grammar_is_refused(self):
        for name in ("Feat/7-x", "feat/07-x", "feat/7", "feat-x/7-y"):
            with self.subTest(name=name), self.assertRaises(WorktreeError):
                self.ensure(7, branch=name)

    def test_several_ticket_branches_without_a_worktree_are_refused_and_listed(self):
        git_in(self.main, "branch", "feat/7-a", "origin/master")
        self.push_seed_branch("fix/7-b")
        with self.assertRaisesRegex(WorktreeError, "feat/7-a, fix/7-b"):
            self.ensure(7, title="feat: ignored")
        self.assertEqual(self.worktree_count(), 1)

    def test_the_one_ticket_branch_with_a_worktree_wins_over_the_others(self):
        git_in(self.main, "branch", "feat/7-a", "origin/master")
        self.ensure(7, branch="feat/7-a")
        self.push_seed_branch("fix/7-b")
        workdir, branch = self.ensure(7)
        self.assertEqual(branch, "feat/7-a")
        self.assertEqual(os.path.basename(workdir), "7-a")

    def test_several_ticket_branches_with_worktrees_are_refused_and_listed(self):
        git_in(self.main, "branch", "feat/7-a", "origin/master")
        self.ensure(7, branch="feat/7-a")
        self.push_seed_branch("fix/7-b")
        self.ensure(7, branch="fix/7-b")
        with self.assertRaisesRegex(WorktreeError, "feat/7-a, fix/7-b"):
            self.ensure(7)

    def test_a_stale_local_branch_is_fast_forwarded_to_origin(self):
        git_in(self.main, "branch", "feat/7-x", "origin/master")
        head = self.push_seed_branch("feat/7-x")
        workdir, branch = self.ensure(7)
        self.assertEqual(branch, "feat/7-x")
        self.assertEqual(git_in(self.main, "rev-parse", "refs/heads/feat/7-x"), head)
        self.assertEqual(git_in(workdir, "rev-parse", "HEAD"), head)

    def test_a_local_branch_ahead_of_origin_is_kept(self):
        self.push_seed_branch("feat/7-x", commit=False)
        git_in(self.main, "fetch", "-q", "origin")
        git_in(self.main, "checkout", "-q", "-b", "feat/7-x", "origin/feat/7-x")
        git_in(self.main, "commit", "-q", "--allow-empty", "-m", "local work")
        local = git_in(self.main, "rev-parse", "HEAD")
        git_in(self.main, "checkout", "-q", "master")
        workdir, _ = self.ensure(7)
        self.assertEqual(git_in(self.main, "rev-parse", "refs/heads/feat/7-x"), local)
        self.assertEqual(git_in(workdir, "rev-parse", "HEAD"), local)

    def test_diverged_local_and_origin_branches_are_refused_and_left_alone(self):
        git_in(self.main, "checkout", "-q", "-b", "feat/7-x", "origin/master")
        git_in(self.main, "commit", "-q", "--allow-empty", "-m", "local")
        local = git_in(self.main, "rev-parse", "HEAD")
        git_in(self.main, "checkout", "-q", "master")
        self.push_seed_branch("feat/7-x")
        with self.assertRaisesRegex(WorktreeError, "diverged"):
            self.ensure(7)
        self.assertEqual(git_in(self.main, "rev-parse", "refs/heads/feat/7-x"), local)

    def test_a_stale_branch_checked_out_in_a_worktree_is_not_moved(self):
        git_in(self.main, "branch", "feat/7-x", "origin/master")
        workdir, _ = self.ensure(7, branch="feat/7-x")
        old = git_in(self.main, "rev-parse", "refs/heads/feat/7-x")
        self.push_seed_branch("feat/7-x")
        self.assertEqual(self.ensure(7)[0], workdir)
        self.assertEqual(git_in(self.main, "rev-parse", "refs/heads/feat/7-x"), old)

    def test_a_custom_relative_root_is_honoured(self):
        rel, _ = self.ensure(7, title="feat: rel", root="custom/trees")
        self.assertEqual(rel, os.path.join(self.real_main(), "custom", "trees", "7-rel"))

    def test_an_absolute_root_inside_the_clone_is_honoured(self):
        inside = os.path.join(self.main, "trees")
        workdir, _ = self.ensure(22, title="feat: abs", root=inside)
        self.assertEqual(workdir, os.path.join(os.path.realpath(inside), "22-abs"))

    def test_a_root_outside_the_clone_is_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        for root in (outside, "../escape", "..", "."):
            with self.subTest(root=root), self.assertRaisesRegex(WorktreeError, "outside"):
                self.ensure(7, title="feat: x", root=root)
        self.assertFalse(os.path.exists(outside))

    def test_a_registered_worktree_outside_the_root_is_refused(self):
        outside = os.path.join(self.tmp, "outside")
        git_in(self.main, "worktree", "add", "-q", "-b", "feat/7-out", outside, "origin/master")
        with self.assertRaisesRegex(WorktreeError, "outside the worktree root"):
            self.ensure(7)

    def test_a_registered_worktree_without_a_git_file_is_refused(self):
        workdir, _ = self.ensure(7, title="feat: broken")
        os.remove(os.path.join(workdir, ".git"))
        with self.assertRaisesRegex(WorktreeError, "no .git file"):
            self.ensure(7, title="feat: broken")

    def test_the_git_seam_is_the_only_way_git_runs(self):
        calls = []

        def recording(args, cwd):
            calls.append((args[0], cwd))
            return worktrees.git(args, cwd)

        self.ensure(7, title="feat: seam", git=recording)
        self.assertTrue(calls)
        self.assertEqual(calls[0][0], "fetch")
        self.assertTrue(all(cwd == self.real_main() for _, cwd in calls))

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
        os.makedirs(os.path.join(self.main, ROOT, "7-leftover"))
        with self.assertRaisesRegex(WorktreeError, "not a worktree"):
            self.ensure(7, title="feat: leftover")

    def test_refused_without_a_branch_or_a_title(self):
        with self.assertRaisesRegex(WorktreeError, "no title"):
            self.ensure(7)

    def test_refused_without_a_main_clone(self):
        with self.assertRaisesRegex(WorktreeError, "no main clone"):
            worktrees.ensure_worktree(
                os.path.join(self.tmp, "absent"),
                7,
                default_branch="master",
                branch_template=TEMPLATE,
                root=ROOT,
                title="feat: x",
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
