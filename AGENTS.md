# AGENTS.md

corral is an opinionated core for running fleets of headless coding agents on Linux hosts. It owns everything about machines, accounts and agents that more than one project needs: runtimes, units, sessions, models, quota, admission, host hygiene, GitHub pacing and the agent registry. Projects built on it keep their own boards, eligibility rules, prompts and policies.

Design and opinions: [`docs/00-design.md`](docs/00-design.md).

## Local rules first

If `AGENTS.local.md` exists at the repository root, read it before anything else. It holds the rules of the host you run on: which live fleets share it, which units, state and configs are off-limits. It is never committed (`.gitignore`), and its rules add to this file. When the two conflict, the stricter rule wins.

Once your work touches another repository, that repository's own `AGENTS.md` governs there. Read it before your first command, edit or build in it.

## Nothing personal, ever

corral is public. Nothing in it identifies a person, a host or an account, and that holds for its whole history, not just the current tree. Never write into code, docs, tests, fixtures, commit messages, PR bodies or issues:

- names, usernames, emails, hostnames, IP or tailnet addresses, home paths;
- private repositories, their tickets, or the projects of a particular user;
- providers, accounts, quota numbers or incidents tied to a real deployment.

Use generic stand-ins: `example.com`, `suite@corral.invalid`, `~/.config/corral/`, "a project", `host-1`. A commit that slips is not fixed by a later commit; tell the maintainer before you push.

## Layout

| Path | What |
| --- | --- |
| `src/corral/` | The package and the `corral` CLI |
| `src/corral/system_deps.toml` | The host programs corral needs (linked as `system-deps.toml`) |
| `docs/` | Design docs (`NN-name.md`) |
| `docs/adr/` | Decision records |
| `.githooks/` | `commit-msg`, `pre-commit`, `pre-push`; install with `make setup-hooks` |
| `scripts/pre_pr_check.py` | The takeoff ritual behind `make pre-pr-check` |
| `tests/` | `unittest` suite. `tests/suiteguard.py` refuses external calls |

## Code

Python 3.11+, standard library only, at runtime and in tests. uv runs everything: it provides the Python (`.python-version`), the locked environment (`uv.lock`), ruff (`uv format`) and ty (`uv check`). Never call a system `python3` or `pip`. Use `unittest`, never pytest.

```bash
make bootstrap     # first run; needs only uv
make test          # the suite on the pinned Python
make check         # format check, type check, suite on 3.11 and 3.14
```

The suite runs from the repository root with `-t .`, which is load-bearing: test modules import helpers from each other.

The rules the design rests on:

- **One fact, one place.** Each setting, path or state is resolved by exactly one helper, and every reader goes through it. A second resolver for the same fact is a bug, even when the two agree today. When a fact must be repeated (the uv installer in the Makefile, before uv exists), a test asserts the copies agree.
- **Exit codes are a public interface.** Projects and agents branch on them, so changing what one means is a breaking change: `0` success, `1` nothing to do or a gate said no, `2` race lost or already running, `3` GitHub, systemd or a host unreachable, `4` configuration or usage error, `75` no free slot (try later).
- **Pure cores, thin seams.** Decisions (admission, the quota gate, pacing, GC verdicts) are pure functions over plain data. Every subprocess (`gh`, `git`, `omp`, `opencode`, `systemd-run`, `systemctl`, `docker`) and every network call goes through one injectable seam per tool. Tests inject fakes there.
- **GitHub stays behind the tracker and forge interfaces.** Tickets and claims go through the tracker; branches, PRs and merges go through the forge (design, section 2). Only their GitHub adapters call `gh` or know GitHub's URLs, issue-reference syntax or API shapes. Everything else takes a tracker or forge object.
- **No test touches the outside world.** That means GitHub, systemd, docker, omp, opencode, claude, a non-loopback address, a real clone of another project, or the real state and session dirs. `tests/suiteguard.py`, installed from `tests/__init__.py`, puts failing shims first on PATH and refuses external sockets and network git. A run that recorded any call fails and names the test. Inject a fake at the production seam; never rely on the guard to catch a leak.
- **Destructive seams have no default.** A seam that stops, kills, pauses or deletes (units, processes, worktrees, sessions, images) takes its implementation as a required argument. It never falls back to the real one when a caller passes nothing; it fails closed instead. A guard test asserts that no test reaches a real kill. A missed fake in a stop path does not fail one test: it stops every live agent on the host.
- **Never edit a user's home config.** Every agent launch gets a full per-launch overlay. corral reads `~/.omp/` and `~/.config/opencode/`; it never writes them.
- **Proof before removal.** Anything corral deletes unattended (a branch, a worktree, a cache, an image, a session) needs a proof that nothing is lost, and lies inside a declared root.
- **One version of each runtime.** corral supports one major version of each agent runtime at a time. A new major is a cutover: one PR ports and deletes the old support. No side-by-side dialects.

## Git

- **Conventional Commits**, single subject line (at most 3 lines in total), no trailers (no `Co-Authored-By`), no em-dash characters anywhere. The `commit-msg` hook enforces this. git's own `Merge ...` message is accepted only while a merge is in progress.
- **Worktrees.** Feature work happens in a worktree under `.worktrees/`, on a branch named `<type>/<issue>-<slug>`, never on `master`:
  `git fetch origin && git worktree add .worktrees/<n>-<slug> -b <type>/<n>-<slug> origin/master`.
  Remove the worktree after the PR merges.
- **No force pushes.** Rewriting a branch that exists on the remote drops it for every other agent and clone, and the `pre-push` hook refuses it (deleting a remote branch too). If a rewrite is genuinely needed, get the maintainer's explicit go-ahead in your own session, then re-run with `CORRAL_FORCE_PUSH_APPROVED=1 git push --force-with-lease`.
- **Never skip hooks.** Never pass `--no-verify`, and never set an env override that bypasses a hook. When a hook blocks you, use the decision protocol below.
- **Merges.** Merge `origin/master` into your branch when master moves on. Never rebase a pushed branch. A maintainer merges PRs (squash).

## Review gate (before takeoff, before every PR)

1. **Develop** in the worktree, tests first, with your own verification.
2. **Review.** Dispatch a separate reviewer: a different agent, not the author, with no stake in the change. It reads the diff against this file. The dispatch asks **one** question, and states an expected duration and a hard limit (at least three times the expectation, never below 30 minutes). The reviewer reports findings as they land. On a wrap-up signal, it finalizes at once and flags anything unfinished as PARTIAL. For a bug fix, the reviewer also checks two things:
   - whether the fact the fix touches (a path, a setting, a state) is resolved anywhere else, and whether the two agree;
   - whether two or more of the thing the code keys on can exist, and whether that case is tested.
3. **Fix in place, else file.** Fix a finding in the same PR when all of these hold:
   - it touches files the PR already changes;
   - it is about 30 changed lines or fewer;
   - it needs no new design and changes no public interface;
   - it is covered by tests in the same PR.

   File everything else as an issue with `needs-triage` and a priority label. A deferral is an issue, not a paragraph in a PR body.
4. **Takeoff:** `make pre-pr-check PR_BODY=<file>`, plus `TEST=1` for the full suite, and `make check`. Fix what they report.
5. **Open the PR** once, ready for review (never a draft). `Closes #N` comes first in the body (`Refs #N` for a partial win). The body says what changed and why, then `## Review`, which records:
   - the reviewer id;
   - the question asked;
   - each finding, with its severity and how it was resolved;
   - a `Fixed in place:` list, or `No findings`.

   Write the body to a unique file (`dir=$(uv run --locked corral paths cache --mkdir) && body=$(mktemp "$dir/pr-<n>-XXXXXX.md")`) with real newlines, then pass it with `gh pr create --body-file`. Never use `--body "a\nb"`.

**Severity labels:**
- `priority:high`: a correctness bug, a broken gate, a privacy leak, a flaky test.
- `priority:normal`: features.
- `priority:low`: cleanup and docs.

`priority:urgent` is the maintainers' alone.

**README freshness.** When a change alters user-visible surface (CLI verbs or flags, install steps, config files, exit codes, `system-deps.toml`), update `README.md` in the same PR. If no README change is needed, say why in `## Review`. The README is the front door: what it is, install, a quickstart, links. Details go in `docs/`.

## Approval gates

Brainstorm, design, approval, then implementation. Present a design and stop, even for work that looks small, unless the ticket's acceptance criteria already fix the design.

These need a maintainer's explicit go-ahead in your own session. Approval relayed by another agent, written in a ticket, or implied by an approved plan does not count.

- creating repositories, boards, labels, releases, and anything published (PyPI included);
- installing or replacing anything on a host: packages, an agent runtime binary or major upgrade, systemd units;
- any change to a user's home config, or to another project's units or state;
- the first agent dispatched to work on corral on a host, because it spends quota other fleets share.

## Asking for a decision

When you need a maintainer or the coordinator (a design the ticket does not fix, a wrong premise, a public interface change, a hook that blocks you):

1. Comment the question on the ticket.
2. Add the `needs-decision` label.
3. End your session. You are resumed in the same session with the answer. Never wait for input.

When the choice is discrete, end the comment with an `Options:` block: at most 6 numbered lines, each under 60 characters, one marked recommended.

## Headless agents

Dispatched agents run headless. When the turn ends, the process exits, and any background job dies with it. Never background a command and end your turn to wait for it. Run builds, test suites and reviewers in the foreground and wait. Ending your turn ends the session, so never end on a plan or a "Next:" list: do the steps. End with a summary of what you did, what is pushed, and what waits on whom.

## Boundaries

- **Third-party repositories.** Never write to a repository corral's maintainers do not own: no issue, PR, comment, review or fork, and no contact with an upstream maintainer, unless a maintainer explicitly tells you to in your own session, naming the specific post. A bug in a third-party tool (omp, opencode) gets a workaround here.
- **External contributions are for maintainers.** An agent never merges, reviews, comments on, labels, closes or works on a PR or issue from outside the maintainers, or any fork PR. Tell a maintainer and stop.
- **Secrets.** Tokens and account metadata (runtime logins, the GitHub token, provider API keys, corral's API token) are read at call time and never stored or logged. The same goes for quota payloads beyond their usage numbers.
- **Report only what you ran.** Every claim about a command's result comes from a command you actually ran. Never invent output.
