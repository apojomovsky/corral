# 01: onboarding a project

Status: slices 1 to 3 built (inline `corral init` with detection, `corral.toml`, hooks, the overlay and the host registration, plus `corral uninit`; and the split layout with `corral init --ops`/`uninit --ops`); prompts are still design, not built. Decided 2026-10-26: `corral.toml` at the root, `init` asks with detected defaults (`--yes` and `--set` for headless), `new` scaffolds a minimal language-agnostic repository.

corral should take a project from zero to its first agent in a few commands, whether the project is a repository that already exists or one that does not exist yet. corral is opinionated about how agents run. It should not be opinionated about the project: its language, its branch names, its labels, its test command or its prompts.

## Requirements from the ways corral is used

These shaped the layout decisions in the next section, and sections 1 to 7 follow them.

A common way to work is a pair of repositories:
- the **project**, which may be public or shared with a client;
- a private **ops** repository next to it, which holds the agent side (configuration, prompts, tickets, notes) and is the one that uses corral.

Using corral from the project itself must stay just as supported, whether its owner does not need the split or a user simply prefers it.

- **R1. Configuration can live outside the project.** Settings, prompts, hooks and agent instructions may sit in another repository that points at the project's clone. In that layout, corral writes nothing tracked into the project. Local, untracked git state (`core.hooksPath`, `.git/info/exclude`) is all it may touch there.
- **R2. Both layouts are first-class.** Neither is a degraded mode of the other. One code path should serve both, for example by treating the inline layout as the case where the ops repository and the project are the same repository.
- **R3. Nothing crosses from the private side to the shared side.** Commits, branch names, PR bodies, code and comments in the project never mention the ops repository or anything in it. This calls for a guard, not just a convention: for example, a list of private terms that takeoff checks against messages, bodies and diffs.
- **R4. Tickets and code may live in different repositories.** The repository holding the issues need not be the one receiving the PRs. Links, closing keywords and eligibility cannot assume they are the same.
- **R5. The project may belong to someone else.** In someone else's organisation, corral may not be able to create labels, install hooks in tracked files or change settings. Whatever needs those rights must be optional, or move to the ops side.
- **R6. The project may be private.** Nothing may assume a public repository.
- **R7. One ops repository may serve several projects** (for example a client's frontend and backend). Whether to support this from the start is open. Supporting it later would change the file format.

## Layouts: what was decided

Decided on 2026-10-26, meeting R1 to R7:

- **One code path.** `corral.toml` always lives in an *ops root* and lists the repositories it drives as `[[repo]]` entries. The inline layout is the case where the ops root is the project, and the list holds that one repository. Commands, settings and the doctor behave the same in both layouts. The split layout adds only two things:
  - the rule that nothing tracked is written into the project (R1);
  - the leak guard (below).
- **Several repositories per ops root.** Each `[[repo]]` entry has its own clone, default branch, test command and caps. Shared settings (models, prompts, private terms) are declared once. When several repositories share one tracker, a `repo:<name>` label set at triage says which repository a ticket targets.
- **Where tickets live follows visibility.** For each repository, `init` proposes:
  - a **public** project: its tickets live in the project repository, so contributors can read how it evolved;
  - a **private** project: its tickets live in the ops repository, so the project's owner sees the PRs, not the work behind them.

  The tracker is a setting (`tracker.repo`), so either can be overridden. Material meant only for the operator always goes to the ops side, whatever the visibility: decision questions, coordinator notes, the journal. A decision question about a project ticket is posted in the ops repository and links to the ticket. If a project's visibility changes later, its tickets stay where they are; `doctor` reports the mismatch.
- **Name of the ops repository:** `<project>_ops` by default, overridable with `--ops-name`.
- **The leak guard.** The ops `corral.toml` lists private terms: names, hostnames, client names, or regular expressions. corral always adds the ops repository's own name and path, the host name and home paths. The list itself never leaves the ops side. It is checked in two places:
  - **takeoff:** commit messages, the branch name, the PR body and added lines in the diff;
  - **the tracker adapter:** a comment or ticket that matches is refused before it is posted.

  A match is exit 1 and names the term and where it was found. Prompts tell agents what is private, but the guard is the enforcement. In the inline layout with no terms listed, the guard checks only the built-in ones.

## 1. The path

```bash
uv tool install corral         # once per host (from a git tag until there is a release)
corral deps                    # what the host still needs, with the commands to get it

cd my-repo && corral init      # an existing repository, corral inside it
cd my-repo && corral init --ops ../my-repo_ops   # or driven from a private ops repo
corral new my-repo             # or a new project: scaffold, then init

corral doctor                  # is this project ready to run agents?
corral launch 42               # the first agent, on issue #42
```

Every step is safe to re-run. `init` and `new` show what they would write and ask before writing. `--yes` accepts the detected defaults and `--set key=value` overrides one, so an agent can onboard a project headless.

## 2. What is fixed and what is a setting

The opinions are not settings. corral does not make these configurable:
- agents run as `systemd --user` units;
- the tracker and the forge are the source of truth (GitHub only, for now; see the design, section 2);
- agent hosts run Linux;
- one ticket gets one worktree and one branch;
- launches use config overlays and never edit home configs;
- nothing is deleted without a proof;
- the exit codes are fixed.

Everything a project might reasonably do differently is a named setting with a default. The defaults are corral's own conventions:

| Setting | Default | Notes |
| --- | --- | --- |
| `project.name` | the repository name | also the default unit prefix |
| `project.units.prefix` | `<name>-agent` | |
| `git.default_branch` | detected from the remote | |
| `git.branch` | `{type}/{issue}-{slug}` | a template that must render `<type>/<n>-<slug>` (lowercase type, no leading zero in `<n>`); `feature/{issue}` alone does not |
| `git.worktrees` | `.worktrees/` | relative to the clone |
| `git.commits.conventional` | `true` | the commit-msg hook can be turned off |
| `board.ready` | label `ready` | which issues an agent may take |
| `board.labels` | `needs-triage`, `needs-decision`, `priority:*` | names only; the meanings are corral's |
| `checks.test` | detected (see 3) | the command the agent and takeoff run |
| `checks.takeoff` | `[checks.test]` | any list of commands; each must exit 0 |
| `checks.timeout_s` | `1800` | seconds before a takeoff command is killed |
| `agents.cap` | 1 | this project's ceiling on the host |
| `agents.runtime` | `omp` | or `opencode` |
| `models.*` | roles and fallback chains | must name providers from the host's accounts |
| `prompts.<role>` | built-in templates | override with a file (see 4) |

Settings resolve in one place, in this order (later wins):
1. built-in defaults;
2. the host config, `~/.config/corral/config.toml`;
3. the ops root's `corral.toml`: first its shared settings, then the `[[repo]]` entry, which wins for that repository;
4. `CORRAL_*` environment variables;
5. CLI flags.

**Host limits are ceilings, not layers.** The host config also sets limits that no project can exceed, whatever the order above resolves to:
- the total agent cap;
- the disk ceiling;
- provider pacing;
- the runtimes allowed on the host.

A project asking for more is clamped to the limit. `doctor` reports it, for example "project asks cap 5, host allows 3".

`corral config show --origin` prints every value with the layer it came from, and marks values clamped by a host limit.

Secrets are never settings. Tokens and API keys are read at call time from their own stores (runtime logins, `gh`, the token file) and never appear in `corral.toml`, the host config or `config show`.

## 3. `corral init`, in an existing repository

`init` detects what it can and asks about the rest, offering the detected value as the default:

- the GitHub `owner/repo`, from the remote, and the default branch;
- the test command, from what the repository already has:
  - a `make test` target;
  - `pyproject.toml`: `uv run pytest`, or `uv run python -m unittest`;
  - `package.json`: `npm test`;
  - `Cargo.toml`: `cargo test`;
  - `go.mod`: `go test ./...`;
  - none of these: ask.
- whether `AGENTS.md`, `CLAUDE.md` or git hooks already exist.

The first question is the layout: corral inside this repository, or a separate ops repository (`--ops <path>`, created as `<project>_ops` if missing). Running `init --ops` again from a second project's clone adds a `[[repo]]` entry to the same ops repository. For each repository, `init` reads its visibility through the forge and proposes where its tickets live (Layouts, above).

In the inline layout it writes only the files below, all in the repository itself. In the split layout, the same files go to the ops repository. In the project clone it touches only local git state: `core.hooksPath`, and `.git/info/exclude`.

It writes only these:

- **`corral.toml`** at the root: the settings that differ from the defaults, followed by every other setting commented out, with its default and a one-line explanation. This file is the whole contract for a project that needs no code.
- **`agents.overlay.md`** next to `corral.toml`: the agent overlay (section 3a), from a template. It is yours to edit after that, and a re-run never overwrites it.
- **Hooks, only if the repository has none.** It installs them through `core.hooksPath`. If hooks already exist, it prints how to chain corral's hooks from them.
- **The host registration**, `~/.config/corral/projects.d/<name>.toml`, which points at the clone. This is the only file `init` writes outside the repository.

Anything on GitHub (labels, a project board) is an outward action. `init` prints `corral github setup` for the user to run, and never runs it itself.

`corral init --plan` prints the diff and writes nothing. `corral uninit` removes the hooks it installed and the registration, and leaves `corral.toml` and `agents.overlay.md` for the user to delete.

## 3a. The project's `AGENTS.md` and the agent overlay

Two files instruct agents, and they have different audiences:

| | The project's `AGENTS.md` | `agents.overlay.md` |
| --- | --- | --- |
| Audience | everyone: contributors and their own agents | only the agents corral launches |
| Lives in | the project, tracked | the ops root, next to `corral.toml` |
| Says | how to build, test and contribute to this code | how these agents are driven: worktrees, review gate, takeoff, the decision protocol, house preferences, what is private |

corral's tooling never edits the project's `AGENTS.md` (`init`, `new` on an existing file, upgrades). Agents may change it like any other file, through a ticket and a PR. At launch, corral puts the overlay into the agent's prompt and tells the agent to read the project's `AGENTS.md`. In the inline layout, the overlay is a tracked file in the project, but still separate from `AGENTS.md`, so contributors never read corral-only rules.

**Where a new rule goes.** The overlay template states this test, and the coordinator building blocks point to it. The first yes decides:

1. It mentions anything private (the ops side, a client, people, quota, accounts, internal priorities): **overlay**. The leak guard enforces this.
2. It only makes sense because corral runs the agent: **overlay**.
3. A contributor's PR would be rejected for not following it: **project `AGENTS.md`**.
4. Otherwise, it is a preference for these agents only: **overlay**.

When in doubt, put it in the overlay first. A rule that turns out to hold for everyone is promoted with a PR to the project. If the overlay would contradict the project's file, one of them is wrong: drop the overlay rule, or change the project's `AGENTS.md` in a PR. For a repository someone else owns, that PR is a proposal.

## 4. Overrides without forking

- **Prompts.** Each role (worker, reviewer, triage) has a built-in template with named placeholders: `{issue}`, `{title}`, `{branch}`, `{worktree}`, `{checks.test}`, `{overlay}`, `{project_agents_md}`. `corral prompts eject worker` copies the template to `.corral/prompts/worker.md` for editing. corral warns when the built-in template changes after the ejected copy was taken.
- **Code hooks**, for what a setting cannot express: custom eligibility, a keep-check, a takeoff gate. `corral.toml` names a Python entry point (`hooks = "my_tools.corral:Project"`) that implements the project protocol from [`00-design.md`](00-design.md), section 2. Without it, the defaults apply: GitHub issues with the ready label are eligible, and a branch is kept while it has an open PR or unpushed commits.
- **Where hook code lives:** in the ops root. In the split layout, the ops repository becomes a small uv project: a `pyproject.toml` that depends on corral (pinned through `[tool.uv.sources]`), and a module with the hooks that `corral.toml` names. The code is versioned with the rest of the ops side, and the project never sees it. In the inline layout, the same module lives in the project.
- **Language.** corral is a tool installed on the host, not a dependency of the project. A project with no Python in it never needs any. Only a project with code hooks depends on corral as a library, pinned through `[tool.uv.sources]`.

## 5. `corral new`, for a project that does not exist yet

`new` scaffolds a small repository: README, `AGENTS.md`, `.gitignore` and a Makefile with `test`, `check` and `setup-hooks`. It then runs `init` on it, so the rest of the path is the same as section 3.

`--template` picks a starting point: `blank` is the default, `python-uv` uses corral's own layout. Creating the GitHub repository is an outward action, so `new` prints the `gh repo create` command instead of running it.

## 6. Doctor

`corral doctor` checks the project end to end. It never changes anything. Each failing line comes with the command that fixes it.

`corral doctor --fix` applies only fixes that are local, safe and reversible, then lists what it changed:
- installing hooks;
- writing the host registration;
- re-rendering a stale unit file;
- creating state and session dirs.

Two kinds of fix are never applied by `doctor`, with or without `--fix`:
- **Fixes that need sudo or install software:** apt packages, the docker group, linger, a runtime binary. These are printed as commands, as `corral deps` does.
- **Outward fixes:** labels, repository settings, anything on the tracker or forge. For these, `doctor` prints `corral github setup` for the user to run.

Because `--fix` cannot reach beyond the user and the clones, agents may run it.

- **The host:** `corral deps`.
- **The project file:**
  - it parses;
  - every model names a provider that is in the host's accounts;
  - every prompt placeholder is known.
- **The repository:**
  - the default branch exists on the remote;
  - the test command runs;
  - the hooks are installed;
  - the overlay exists, and its placeholders are known.
- **Drift:**
  - the installed corral, or an installed unit file, is behind its source;
  - a repository's visibility no longer matches where its tickets live.
- **The tracker and forge** (GitHub today):
  - `gh` is logged in and can see the repository;
  - the labels exist.

## 7. Testing onboarding

`init` and `new` run against fixture repositories in temporary directories:
- empty;
- Python with uv;
- Node;
- Rust;
- a repository that already has `AGENTS.md` and hooks.

Each run produces golden files. Re-running `init` must produce no diff. `uninit` must restore the original tree except `corral.toml` and `agents.overlay.md`. GitHub and the host registration go through fake seams, as everywhere else.
