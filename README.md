# corral

An opinionated core for running fleets of headless coding agents on Linux hosts.

You bring the projects: a board, the rules for which ticket is ready, the prompts. corral keeps the agents in one pen. It launches them as systemd user units, keeps their sessions, routes their models, watches the provider quota every project shares, caps how many run at once, and cleans up after them without losing work.

Status: early. The host check (`corral deps`) works; the rest is being built.

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

## Commands

| Command | What it does |
| --- | --- |
| `corral deps` | check the host programs corral needs; print how to install what is missing |
| `corral paths [state\|config\|cache\|sessions] [--mkdir]` | the directories corral keeps on this host, after `CORRAL_*` and XDG overrides |
| `corral config show [--origin] [--repo NAME] [--root PATH]` | every setting with its resolved value; `--origin` names the layer it came from and marks values clamped by a host limit |
| `corral push [branch]` | push, verify the remote ref, retry once; the retry skips the suite the hook already passed |
| `corral prose [--verify]` | list the comment blocks a branch adds; `--verify` fails on ones that break the prose rules |
| `corral budget` | the review dispatch block: one question, scope, and a time budget derived from the diff |
| `corral coord ... --project <name>` | one coordinator per project: a lease (`acquire`, `heartbeat`, `release`, `takeover`, `status`), a logbook (`log`, `followup`, `handoff`, `brief`) and an event queue (`wait`, `ack`). `CORRAL_PROJECT` names the project when `--project` is not given. |

Settings layer as built-in defaults, then the host config (`~/.config/corral/config.toml`), then the ops root's `corral.toml` (its `[[repo]]` entry wins for that repository), then `CORRAL_*` environment variables, then flags. The host config's `[limits]` table caps numeric settings for every project.

Exit codes: `0` success, `1` nothing to do or a gate said no, `2` race lost (the lease is held, or a generation is stale), `3` a remote or service unreachable, `4` usage error.

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
