# Contributing

corral is opinionated: one fact in one place, pure cores with thin seams, and no test that touches the outside world. `AGENTS.md` has the full rules, and they apply to human contributors too. If a change could go several ways, ask in an issue first: a scope question is cheaper there than in a PR.

## Setup

```bash
make bootstrap    # Python, the locked environment, git hooks, then the host check
```

## Loop

1. Write the tests first, with fakes at the seams (`unittest`, never `pytest`).
2. Work in a worktree under `.worktrees/`, on a branch named `<type>/<issue>-<slug>`, never on `master`.
3. Commit with Conventional Commits: a single subject line, at most 3 lines, no trailers, no em-dashes.
4. Run `make check`: format check, type check, and the suite on Python 3.11 and 3.14.
5. Merge `origin/master` into your branch when master moves on; never rebase a pushed branch.

## Review gate

1. Develop in the worktree, tests first, with your own verification.
2. Dispatch a separate reviewer: a different agent, not the author, with no stake in the change.
3. Fix small findings in place, or file the rest as issues with `needs-triage` and a priority label.
4. Takeoff: `make pre-pr-check PR_BODY=<file>`, plus `TEST=1` for the full suite, and `make check`.
5. Open the PR once, ready for review: `Closes #N` first, then what and why, then a `## Review` section.

Where this file and `AGENTS.md` disagree, `AGENTS.md` wins.
