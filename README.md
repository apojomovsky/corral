# corral

An opinionated core for running fleets of headless coding agents on Linux hosts.

You bring the projects: a board, the rules for which ticket is ready, the prompts. corral keeps the agents in one pen. It launches them as systemd user units, keeps their sessions, routes their models, watches the provider quota every project shares, caps how many run at once, and cleans up after them without losing work.

Status: alpha. Working today: launching, resuming, tailing and stopping agents (`corral launch`, `resume`, `tail`, `stop`); the agent registry, admission, quota, models and prompts; `corral init` and `uninit`; the board store with `sync` and `mirror`; `gc` and `disk` hygiene; `doctor` and `snapshot`; the review records and the takeoff ritual (`make pre-pr-check`). Not yet: hosts beyond one, a dashboard API, `corral new`.

## Opinions

corral makes these choices for you, and does not try to support the alternatives:

- **Linux only.** Every agent is a transient `systemd --user` unit. macOS, Windows, containers-as-agents and other init systems are not supported as agent hosts.
- **GitHub, behind two interfaces.** Tickets and claims (the tracker) and pull requests (the forge) each go through one interface. Today each has exactly one implementation: GitHub, through `gh`. Other providers are not supported, but adding one means writing an adapter, not redesigning corral.
- **One ticket, one worktree, one branch.** Agents never share a checkout.
- **Headless runtimes.** omp and opencode, one major version of each at a time.
- **Your home config is yours.** Every launch gets its own config overlay. corral reads `~/.omp/` and `~/.config/opencode/` and never writes them.
- **One pool per host.** All projects on a host share one cap, one quota ledger and one GitHub rate budget, so one project cannot starve another without the others noticing.
- **Proof before removal.** corral deletes a branch, worktree, cache or session only once it can show nothing is lost.
- **Network-agnostic.** Across hosts, corral sees `host:port` and a token. LAN, Tailscale or anything else that gives each host an address will do.
- **uv and the standard library.** No runtime dependencies.

Why, and how the pieces fit: [`docs/00-design.md`](docs/00-design.md). Bringing a project in, new or existing: [`docs/01-onboarding.md`](docs/01-onboarding.md).

## Install

You need [uv](https://docs.astral.sh/uv/). Everything else is listed, with how to get it, in [`system-deps.toml`](system-deps.toml).

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv
make bootstrap    # Python, the locked venv, git hooks; then checks the host
```

`make bootstrap` never uses sudo. For anything missing, it prints the commands to run yourself, then exits 1. Run `make deps` (or `corral deps`) at any time to check again.

To put `corral` on your PATH from a checkout:

```bash
make install      # uv tool install
```

## Quickstart

From a checkout to a first agent in the inline layout:

```bash
make install                            # corral on your PATH
corral deps                             # host check; prints how to install what is missing
corral init                             # run in a project clone; writes corral.toml and hooks
corral doctor                           # read-only health report
corral quota                            # the provider pool; needs one reader (see below)
corral launch myproj 12 --role worker  # first agent, in its own worktree
corral tail myproj 12                  # what the agent is doing
corral tail myproj 12 --last 20        # only the last lines
corral stop myproj 12                  # stop it again
corral gc                               # hygiene audit; branches print as git branch -d
```

The host pool needs one provider with a reader, the only provider-specific code (entry points are described in [`docs/02-accounts-quota.md`](docs/02-accounts-quota.md), "Readers"):

```toml
# ~/.config/corral/accounts.toml
[[provider]]
name = "provider-a"
reader = "my_tools.quota:ProviderA"
```

## Commands

| Command | What it does |
| --- | --- |
| **Setup** | |
| `corral deps` | check the host programs and the accounts file; print how to install what is missing |
| `corral doctor [--json]` | read-only health report for the core's own prerequisites; exit 1 on a failing check, 3 when the user manager or `loginctl` cannot be reached |
| `corral init [PATH]` | detect the project and write `corral.toml`, `agents.overlay.md`, the hooks (`.githooks/`, `core.hooksPath`) and the host registration; `--plan` prints the diff, `--no-register` skips the registration, `--force` replaces `corral.toml` or the registration, `--set`/`--runtime`/`--cap`/`--test-cmd` override detection; `--ops PATH`/`--ops-name NAME` writes the agent side to a separate ops root instead and touches the clone only through `core.hooksPath` and `.git/info/exclude` |
| `corral uninit [PATH]` | remove the hooks and the registration `corral init` wrote, only what still matches its templates; `--plan` prints the plan and changes nothing; `--ops PATH`/`--ops-name NAME` reverses a split-layout init and keeps `corral.toml`, the overlay and the ops registration |
| `corral paths [state\|config\|cache\|sessions] [--mkdir]` | the directories corral keeps on this host, after `CORRAL_*` and XDG overrides |
| `corral config show [--origin] [--repo NAME] [--root PATH]` | every setting with its resolved value; `--origin` names the layer it came from and marks values clamped by a host limit |
| **Running agents** | |
| `corral launch REPO N (--prompt-file F \| --role ROLE) [--title T] [--model M] [--runtime R] [--runtime-config JSON] [--root PATH]` | start an agent on ticket N of a repo in its own worktree, as a transient unit; `--role` renders the built-in worker, reviewer or triage prompt (title from the board store or `--title`); exit 75 when every slot is taken, 2 when the ticket is already live |
| `corral resume REPO N --message TEXT [--model M] [--root PATH]` | resume the ticket's newest runtime session in a new unit; refused with exit 1 while the ticket carries the hold label (a human removes the label); exit 4 when there is no session to resume |
| `corral stop REPO N [--root PATH]` | stop the ticket's live agents that its own log proves it started; exit 1 when none is live, exit 2 when a live unit the log does not prove ours is left running (named on stderr, never stopped) |
| `corral tail REPO N [--last K] [--root PATH]` | show the ticket's agent activity, from its session file |
| `corral agent register\|heartbeat\|done\|list` | the host's agent registry: any agent, from any runtime, reports itself; a ticket is `--repo` and `--number` (optional `--project`); exit 4 on a heartbeat means the agent was dropped and registers again |
| `corral projects [--json]` | the registered projects with their prefix, root, floor, ceiling and live agents, counted with the host registry settings (a per-project `registry.*` override applies to launch admission only) |
| `corral prompts show ROLE [--ops DIR]` | print the effective role prompt template (override or built-in), unrendered |
| `corral prompts eject ROLE [--ops DIR] [--force]` | copy the built-in template to `.corral/prompts/` for editing; prints the path and refuses to overwrite without `--force` |
| `corral prompts check [--ops DIR]` | validate every override's placeholders and report stale ejections; exit 1 when something is stale or invalid |
| `corral coord ... --project <name>` | one coordinator per project: a lease (`acquire`, `heartbeat`, `release`, `takeover`, `status`), a logbook (`log`, `followup`, `handoff`, `brief`) and an event queue (`wait`, `ack`). `CORRAL_PROJECT` names the project when `--project` is not given. |
| **State and quota** | |
| `corral quota [--json]` | the provider pool and its quota ledger: state, windows, marks and staleness |
| `corral quota refresh [provider]` | run the readers, record readings or failures; exit 3 when every reader failed |
| `corral quota mark PROVIDER [--until T] [--reason R]` | block a provider until T (default: now plus the cooldown); exit 1 when a further mark stands |
| `corral quota unmark PROVIDER [--reason R]` | release a provider's mark early; exit 1 when none is stored |
| `corral snapshot [--json]` | one read-only view of the host: agents, quota, capacity and disks; `--json` prints the stable document |
| `corral sync [REPO ...] [--resync] [--audit N] [--push] [--json]` | feed the board store from the tracker's change feed (`--resync` rebuilds from the full listing, `--audit N` re-reads the N rows verified least recently, `--push` runs a mirror pass over the outbox after the sync); exit 1 when nothing changed, 3 when the tracker is unreachable |
| `corral mirror [--limit N] [--json]` | push the board store's queued writes into the tracker, oldest first (`--limit N` caps the batch); exit 0 when a write landed, 1 when nothing was queued, 3 when every attempted write failed or a rate limit stopped the pass with nothing sent |
| `corral gh-calls [--since D] [--by-shape] [--json]` | summary of the paced `gh` calls agents make: per caller and minute, throttled totals, GraphQL query shapes; `--since` takes `30m`, `1h`, `7d`; exit 4 on a bad `--since` |
| **Hygiene** | |
| `corral gc [--repo PATH] [--apply] [--json]` | plan-only hygiene audit: landed, superseded and merged branches print as `git branch -d`; worktree cleanup and prune need `--apply`, branches are never deleted. `merged-pr` comes from the forge's PR index for the clone's origin repo, empty with a note when the forge is unreachable |
| `corral disk [--apply] [--json]` | disk pressure and the reclaim plan, largest first: stale `pr-*.md` drafts, finished tickets' session dirs, the `corral gc` worktree rows, and other large dirs (plan only; `--apply` removes only safe `pr-body` and `session` rows). Pressure is `low` under 10% or 5 GiB free and `critical` under half of either (`disk.min_free_pct`, `disk.min_free_gb`); a critical disk refuses launches with exit 75. `corral doctor` carries the same reading |
| `corral leakcheck [--base REF] [--body FILE] [--json] [--ops DIR]` | scan the commit messages, branch name, PR body and added diff lines in `<base>..HEAD` for private terms; exit 1 names each finding. Finding lines name the matched term, so keep this output on the ops side |
| **Review** | |
| `corral review record REPO N --commit SHA --verdict V --author ID` | a reviewer states its verdict on one commit of a ticket, once; a self-review is refused. The record is the review gate's proof |
| `corral review show REPO N --pr P [--commit SHA] [--head SHA] [--forge-repo owner/name] [--json]` | the record a commit has, or inherits across a clean merge-up of the PR base, taken from the forge (`--forge-repo` defaults to the clone's origin); the commit must be the PR head. Exit 0 only for `approve` or `approve-with-findings`, 1 for none, a `block`, or no authoritative base, 3 when the forge is unreachable |
| `corral prose [--verify]` | list the comment blocks a branch adds; `--verify` fails on ones that break the prose rules |
| `corral push [branch]` | push, verify the remote ref, retry once; the retry skips the suite the hook already passed |
| `corral budget` | the review dispatch block: one question, scope, and a time budget derived from the diff |
Settings layer as built-in defaults, then the host config (`~/.config/corral/config.toml`), then the ops root's `corral.toml` (its `[[repo]]` entry wins for that repository), then `CORRAL_*` environment variables, then flags. The host config's `[limits]` table caps numeric settings for every project. `corral config show` lists every setting. A `[[repo]]` entry sets `clone.path` (the repository's main clone, relative to the ops root) and `clone.default_branch` (default `main`), which `corral launch` needs. A project registers with a file per host in `~/.config/corral/projects.d/<name>.toml`, naming its `unit_prefix`, its ops `root`, its guaranteed `floor` of slots (default 0) and its `ceiling` of slots (default none): with the host total full, a project below its floor still starts, and a project at its ceiling does not. With no registration `agents.cap` is the project's ceiling; with one the registry ceiling wins, and a differing `agents.cap` is reported in the refusal, not an error. The provider pool lives beside the host config in `accounts.toml`: one `[[provider]]` entry per provider, naming its quota reader; a missing file means an empty pool. A project declares its model chains in the same `corral.toml`:

```toml
[models.default]
chain = ["provider-a/model-x", "provider-b/model-y"]
```

Every entry names a provider from the pool as `provider/model`; an unknown provider is a configuration error (exit 4), and an id starting with `-` is refused. The per-launch overlay carries each chain with exhausted providers removed, so a launch routes around quota that is gone; when a required role has no live provider left, the launch is refused with exit 75, naming the role and its earliest known reset.

The ops root's `corral.toml` may also list private terms that must never reach the shared side:

```toml
[private]
terms = ["a client name", "host-1.example.com"]
patterns = ["acme-[0-9]+"]
```

`terms` are plain strings, `patterns` are regular expressions; both match case-insensitively. corral always adds the ops repository's own name and path, the host name and the home directory path. `[private]` lives in the ops root's file only: the host config and `[[repo]]` entries cannot set it. A bad pattern is a configuration error (exit 4), and `corral leakcheck` scans a branch's messages, branch name, body and added lines against the list.

Exit codes: `0` success, `1` nothing to do or a gate said no, `2` race lost or already running, `3` GitHub, systemd or a host unreachable, `4` configuration or usage error, `75` no free slot (try later).

## Develop

```bash
make test         # the unittest suite
make check        # format check, type check, suite on Python 3.11 and 3.14
make format       # ruff, through uv
make pre-pr-check PR_BODY=<file> [TEST=1]   # takeoff before a PR
make help
```

Rules for contributors and agents: [`AGENTS.md`](AGENTS.md).

## License

MIT, see [`LICENSE`](LICENSE).
