# 00: design

Status: draft. Only the host check exists so far.

## 1. The problem

Run one agent fleet on a host and the tooling is simple. Run two projects' fleets on the same host, sharing the same provider accounts, the same GitHub account, the same docker daemon and the same disk, and each fleet's tooling is blind to the other:

- each project's quota gate sees only its own spend, so together they exhaust a provider that neither thinks is busy;
- each project caps its own agents, and nothing caps the total;
- one project edits the shared runtime config to route its models, and the other works around it;
- two cleanup passes with different rules prune the same docker daemon and the same disk;
- one project paces its GitHub calls and the other does not, against one rate limit.

corral is the part every such project needs unchanged. Projects keep what makes them different.

## 2. Where the line falls

The test: **if a second project would need it unchanged, it belongs in corral.**

### corral owns

1. **Runtimes.** One adapter per headless agent runtime (omp, opencode, muse). The adapter builds the argv, writes the per-launch config overlay, finds the session file, resumes, tails, and classifies provider errors. A reviewer launch asks the runtime for its read-only mode (muse adds `--disable-write`); adapters with no read-only flag accept the request and rely on the reviewer prompt's read-only rule.
2. **Units.** Transient `systemd --user` units: spawn lock, liveness, stop, naming. The unit prefix is set per project.
3. **Sessions.** One session root per host, one directory per `<project>/<ticket>`, with one leaf per role generation (`<role>-<n>`) below it, so a reviewer launch never overwrites the worker's record. Resume and tail read the newest leaf of a role; a legacy single-directory session reads as generation 0.
4. **Accounts and models.** One accounts file per host lists providers, accounts and their quota readers. Each project declares its own models (roles, fallback chains, sub-agent overrides), naming only providers from that pool. The user's home runtime config is never edited.
5. **Quota.** Readers, reactive rate-limit marks, cooldowns, the gate, pacing, outage probes. The gate is evaluated per project's model chains against **one shared ledger**: a project whose chains avoid an exhausted provider keeps running.
6. **Admission.** A total cap for the host, a floor and ceiling per project, and a disk ceiling, all under one lock. Each project decides what it wants to start; corral decides whether it may start now.
7. **Host hygiene.** One cleanup pass and one disk keeper per host, over every project's worktrees, caches, docker and sessions. Each project supplies a keep-check.
8. **GitHub pacing.** One token bucket per account, with a `gh` shim on every agent's PATH.
9. **Review gate building blocks** (optional for a project). Review records keyed by commit, the "reviewer is not the author" check, a diff-derived review budget, and a takeoff skeleton that projects extend with their own gates.
10. **Agent registry and snapshot.** Every agent on the host from every project (runtime, role, model, ticket, state) plus quota and capacity, as one stable JSON schema for dashboards.
11. **Coordinator building blocks** (optional for a project). A lease with generation fencing, a journal, follow-ups and an event queue, keyed by project. The coordinator itself stays in the project.
12. **Bootstrap and doctor.** The host programs (`system-deps.toml`), linger, units, the accounts file, runtime logins.

### Two provider interfaces

corral talks to code hosting through two interfaces, so a provider can be swapped later without touching the rest:

- **Tracker:** tickets, labels, claims, comments, and the board, if any.
- **Forge:** branches on the remote, pull requests, their checks and review state, merges.

They are separate because they need not be the same service, or even the same repository (onboarding, R4): tickets can sit in a private ops repository while PRs go to a project hosted elsewhere. Each has one implementation today, GitHub through `gh`, behind the single `gh` seam and its rate budget. Nothing outside the two adapters calls `gh` or knows GitHub's URL shapes. A second provider is not planned. When one is needed, it is a new adapter and its tests.

### The board store

Everything that reads tickets reads a local store instead of the tracker: agents, the coordinator, planning, claims and dashboards. The store is SQLite (stdlib `sqlite3`, WAL mode), out of git.

- **It is a cache that knows how confident it is.** Each row is `confirmed`, `pending` (we wrote it, and the tracker has not shown it back yet) or `dirty` (a write failed).
- **It stays fresh in three ways:**
  - the tracker's change feed;
  - a rolling audit of the rows verified least recently;
  - a full resync, the last resort.
- **Coordination writes go to an outbox**, which a mirror pass pushes in batches through the rate budget. Status, priority and links are coordination writes; so is the claim, after the transaction that decides it.
- **Claims are store transactions.** A claim needs zero tracker calls.
- **Decisions on stale rows re-read live.** Paths that decide (a claim, merge preconditions) trust a row only within a staleness bound; past it, they read live.
- **PRs and merges always act live.** Creating a PR and merging it go to the forge directly.
- **Field ownership resolves conflicts.**
  - People win the fields they own on the tracker: title, body, labels, priority, closing, comments, their own assignments.
  - corral wins the agent fields: the claim, in-flight status, the agent.
  - A queued write that a person has meanwhile overridden is dropped and logged.
- **External items are read-only.** An item is external when its author is not an operator, or when it comes from a fork. External items are labelled for a human, and every write to them is refused, mirror included.
- **The tracker can be down.** Claims, status and dashboards keep working, and only PR creation and merges wait.

The tracker stays the durable record: losing the store costs one resync. The tracker adapter holds what is provider-specific (change probes, batched mutations, board fields). A project's own board fields are stored as `extra` fields.

### Projects own

The board and its schema, which ticket is eligible, triage, prompts, the decision and merge policies, releases, notifications, CI watching, and per-repository takeoff gates.

### The contract

A project registers with a small file, `~/.config/corral/projects.d/<name>.toml`: its name, unit prefix, clones, model declaration, cap settings, and an entry module implementing a narrow protocol:

```python
class Project(Protocol):
    name: str
    def keep(self, clone: str, branch: str) -> KeepVerdict: ...   # cleanup keep-check
    def ticket_of(self, unit: str) -> TicketRef | None: ...       # registry and snapshot
    def wants(self) -> list[StartRequest]: ...                    # optional: shared start pass
```

Projects call corral as a library (`corral.launch(...)`, `corral.admit(...)`, `corral.quota.gate(...)`). corral calls back into projects only through this protocol.

### Packaging

corral is a uv project. On a host, `uv tool install` puts the CLI in its own environment, and most projects need nothing more: a `corral.toml` is the whole contract ([`01-onboarding.md`](01-onboarding.md)). Only a project with code hooks depends on corral as a library. It pins the source in `[tool.uv.sources]` (a git tag, or an editable path while developing both), so the name never resolves from PyPI by accident. The doctor warns when a project expects a newer API than the one installed.

## 3. Milestone 1: one host

1. Ground rules: skeleton, hooks, `make bootstrap`, `corral deps`, the API versioning rule.
2. Runtimes, units, sessions. Exit: a fake project launches, resumes, tails and stops an omp, an opencode and a muse agent in tests, through fake seams.
3. Accounts, models, quota. Exit: `corral quota` shows one view, and a launch whose chain is exhausted is refused or re-routed.
4. Admission. Exit: with the total cap reached by one project, another gets only its floor.
5. Host hygiene, run first in plan-only mode next to whatever it replaces.
6. GitHub pacing.
7. Snapshot. Exit: `corral snapshot --json` lists every project's agents.

## 4. Milestone 2: several hosts, a hub

Superseded: the peer-to-peer plan was replaced by the hub design in
[`08-hub-multihost.md`](08-hub-multihost.md), decided by the
maintainer (see
[`adr/0001-hub-not-peer-to-peer.md`](adr/0001-hub-not-peer-to-peer.md)).
The peer log-pull, ack roundtrip, feed duty, and cap-share machinery
of the old plan were abandoned, not implemented. The hub summary
follows; the new doc is authoritative.

Decided: a hub, hosted on one host the maintainer owns, reachable
over a private network. A small service in front of SQLite, standard
library only, authenticated per host with bearer tokens.

**Assumption:** every host can reach the hub directly at a configured address (a LAN, or a tailnet such as Tailscale or Netbird), so hosts are always "local-ish". corral sees `host:port` and a per-host bearer token kept in the host config dir, and never relies on the network layer for authentication.

**Principle: certainty over availability.** A host acts alone only where it is certain nothing can collide. A hub outage blocks new claims everywhere; a single host outage blocks nothing.

### Claims are hub transactions

- **The hub claim comes first, then the local store adopts the
  hub-minted fencing epoch, then the tracker mirrors it.** Racing
  proposes resolve in one database transaction with `epoch+1`;
  losers get a conflict naming the holder.
- **Leases bound crash recovery.** A lapsed lease re-grants to a
  new owner with a bumped epoch; a partitioned holder keeps
  running its agents but claims nothing new, and reconciles
  stale-first on reconnect (exit 3, host unreachable, while the
  hub is down).
- **If the hub is unreachable, no new claim is made.** Work on
  claims already held continues (work, commit, push, open PRs,
  resume). Removing a host is an operator action
  (`corral hub remove-host <host>`): it bumps the membership epoch
  and releases the removed host's claims. A removed host that comes
  back sees the newer epoch, stops its agents, drops their claims,
  and does nothing until it is added again. Worktrees stay under
  the cleanup's proof rule.
- **The tracker stays the durable record.** The hub rebuilds from
  re-registering hosts and the tracker's in-flight labels.

### No shared duties, no cap split

The peer plan split the feed duty, the rate budget, and the cap
across peers. The hub needs none of that: each host pushes only the
writes it made (idempotent, as today), reads the tracker's change
feed as today, and admits only within its own host total
(`08-hub-multihost.md`, section 11).

### Placement is hooks only

- A start request carries constraints: docker, attached hardware,
  free disk, a clone.
- Each host advertises what it has at registration and on every
  heartbeat.
- **A resume always runs on the host that holds the session.**
  Sessions do not move.
- Remote launch and stop are a later phase, not designed here.

### One dashboard across hosts

Hosts push snapshots on the heartbeat; the hub merges per project
or all at once, with per-host staleness and clocks shown as data.
The host-local API is unchanged.

### Steps

Hub with claims and a registry first, operator verbs next, the
aggregated dashboard last (`08-hub-multihost.md`, section 10).

## 5. Later

- **Settings from the dashboard** (low priority). The snapshot is read-only at first. Later, the API can expose the host and project settings (caps, model chains, pacing), so a dashboard can show and edit them. Every write goes through the same resolver and validation as the config files: one fact, one place.
