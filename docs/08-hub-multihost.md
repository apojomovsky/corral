# 08: the hub, multi-host claims and the aggregated dashboard

Status: design, awaiting maintainer approval. No code ships with this doc.

This doc replaces the peer-to-peer plan in `00-design.md`, section 4.
The maintainer decided in session: a hub, hosted on a host the
maintainer owns, reachable over a private network. Hosts keep running
work they already hold when the hub is unreachable. See
[`adr/0001-hub-not-peer-to-peer.md`](adr/0001-hub-not-peer-to-peer.md).
That decision is not reopened here.

Decisions already made (do not reopen):

1. A hub, hosted on one host the maintainer owns, reachable over a
   private network. Not a cloud service, not a shared database that
   hosts connect to directly.
2. The hub is a small service in front of SQLite, standard library
   only, authenticated with the bearer token model already in
   `05-dashboard-api.md`.
3. Hub unreachable: hosts keep running work they already hold (commit,
   push, open PRs, resume). No new claims until it is back.
4. One korapy per machine: one state directory and one host-wide
   ledger; projects register to it. Resource admission (CPU, memory)
   is decided by the host, never by the hub; the hub only learns each
   host's capacity and load.
5. The tracker stays the durable record; the hub can be rebuilt from
   it.

Conventions: "dashboard" means any UI owned by a project or a user,
never by korapy, per `05-dashboard-api.md`. Shapes quoted from
existing modules were read from the tree; anything needing a live run
is marked UNVERIFIED with a safe way to learn it.

## 0. Scope and non-goals

One design for the multi-host layer: entities and schema (section 1),
the claim protocol (section 2), hub-down behavior (section 3),
single-host mode (section 4), the aggregated dashboard (section 5),
security (section 6), hub operations (section 7), placement hooks
(section 8), the test plan (section 9), and the rollout tickets
(section 10). Section 11 draws the line against the resource
governance design (issue 195).

Non-goals: remote launch and stop (a later phase, section 8 says so),
per-host admission decisions (issue 195, section 11), merging and
triage policy (projects own them, per `00-design.md`), and any change
to the tracker or forge interfaces.

## 1. Entities and schema

The hub holds one SQLite database, WAL mode, in its own state dir
(section 7). Every hub write that decides goes through a single
transaction. Four tables plus one meta row:

- **hosts**: one row per host serving at least one project.
  `host_id` (stable, operator-assigned, for example `host-1`; never a
  raw hostname, per the `04-dispatch-loop.md` section 4 rule that the
  claim agent id never carries a raw hostname), `address`
  (`host:port` on the private network), `capacity` (CPUs, memory,
  disk ceilings the host reports), `constraints` (docker, attached
  hardware, clones it serves), `last_seen` (hub clock, epoch
  seconds), `reachable` (derived, section 5). The id is minted once
  by `korapy hub add-host` and stored on the host; a host that loses
  its id re-registers as a new host, never by guessing.
- **projects**: one row per registered project: `name`, the repos it
  serves, and which hosts serve it. Registration stays per host (a
  project registers to each host's state dir as today); the hub row
  is the union the hosts report, used only for the dashboard filter
  and for operator listings, never for decisions.
- **claims**: one row per claimed ticket, keyed `(repo, number)`:
  `host_id`, `agent`, `epoch`, `lease_expires` (hub clock),
  `updated_at`. The hub is the sole minter of `epoch`: strictly
  higher for each new owner after a release, never reused. The local
  board store keeps its claims table unchanged in shape, but in
  hub mode the fencing token on each row is the hub-minted epoch,
  adopted from the hub receipt (section 2). Merge keys everywhere
  are `(host, repo, number)` for agents and claims and
  `(host, provider)` for quota, per `05-dashboard-api.md` section 5.
- **snapshots**: one row per host: the last pushed host document in
  the `korapy snapshot --json` shape (unchanged schema, built by the
  same `snapshot.build`), plus `host_now` (the serving host's clock
  at build time) and `received_at` (hub clock). Snapshots are data,
  never decisions: the hub never admits, gates, or claims from them.
- **meta**: a single row holding `membership_epoch` (integer, starts
  at 0, bumped by every `remove-host`). It is not a table of rows;
  it is the one membership counter the hub owns. Both the register
  reply and every heartbeat reply carry the current
  `membership_epoch`, so a host learns the newest epoch on its next
  contact after a removal (section 2, operator verbs; section 3,
  step 1).

Why the hub stores snapshots instead of proxying: the dashboard must
answer during a host outage, showing that host's last push honestly
marked stale (section 5). A proxy would turn one host outage into a
dashboard hole.

## 2. Claim protocol

Order is fixed: **the hub claim comes first, the tracker mirrors
it.** A drive pass that wants to start work on `(repo, number)`:

1. Proposes to the hub: `POST /v1/claims` with `repo`, `number`,
   `agent`, `host_id`. The `agent` id keeps the existing
   `drive-` shape and the hashed-host rule from
   `04-dispatch-loop.md` section 4, since the hub receipt is
   mirrored to the tracker as today.
2. On grant, records the hub receipt (holder plus `epoch`) into the
   local board store claim row, adopting the hub epoch as the
   fencing token, then enqueues the `agent` and `status` writes to
   the outbox for the mirror pass, exactly as today. The local
   `Store.claim` already mints `epoch+1` after a release; in hub
   mode the store adopts the hub's epoch instead of minting, so
   there is still one minter. The exact call shape (an
   adopt-receipt argument beside `claim`) is an implementation
   detail, with one constraint: no second minting path may exist
   beside the hub receipt path.
3. On conflict (`409` naming the holder), the pass records "race
   lost" and continues with the next candidate, the same rule as
   the local `Claimed` path in `04-dispatch-loop.md` section 4.
4. On hub unreachable, section 3 applies: no new claims, exit 3.

The grant is atomic: one SQLite transaction that checks the existing
row (unclaimed, or lease expired past `lease_expires`) and inserts
or replaces the holder with `epoch+1` and a fresh lease, or refuses.
Linearizability comes from that one transaction, which is the
property the peer-ack plan could not give without every peer up.

Lease renewal: a holder renews with `POST /v1/claims/renew`
(`repo`, `number`, `agent`, `epoch`) before the lease lapses. Renewal
moves `lease_expires` only; the epoch never changes on renew, so a
renew can never fence. Renewal rides on the heartbeat that already
carries the snapshot push (section 5): one call per interval, not
two. The heartbeat body carries a `renewals` list beside the
snapshot, one entry per held claim:
`{"renewals": [{"repo": ..., "number": ..., "agent": ..., "epoch": ...}]}`.
The heartbeat reply answers each entry with granted or `409`. A
`409` on any renewal, whether in a heartbeat batch or on the
single-renew route, means another owner holds the ticket: the host
stops the ticket's units through the injected stop seam, drops its
local claim state without touching the hub row, and logs, exactly
like the reconnect rule in the stale-holder paragraph below. No
resume, no same-pass re-propose; the next pass may propose only
tickets the hub shows as unclaimed. Suggested defaults, tunable as
`hub.lease_ttl_s` and `hub.heartbeat_s`: heartbeat every 30 seconds,
lease 3 times that.
UNVERIFIED until run: whether 30 seconds keeps hub write load trivial
at ten hosts; safe to learn with a loopback hub and scripted
heartbeats counting transactions per second, no hosts needed.

Release: the holder releases with `POST /v1/claims/release`
(`repo`, `number`, `agent`, `epoch`). Only the matching agent plus
epoch releases; anyone else gets `409`. The row is cleared but the
epoch is kept, so the next grant mints `epoch+1` and fences a stale
holder still acting on the old epoch. This mirrors the local
`Store.release` contract (`agent`/`host` cleared, epoch left for the
next claim to mint).

Membership routes: `POST /v1/register` with `host_id`, `address`,
`capacity`, and `constraints`; `POST /v1/heartbeat` with `host_id`,
the `renewals` list from above, and the `snapshot` document
(section 5). Both replies carry the current `membership_epoch`
(section 1); the heartbeat reply additionally carries one
granted-or-`409` answer per renewal entry. Registration and
heartbeat are the only calls that report capacity and constraints;
claims never carry them.

Fencing epochs: every actor that stops or restarts work checks the
epoch first. A unit whose claim epoch is older than the hub's row for
that ticket is stale and must not act. This check before the stuck
restart's stop-plus-launch is NEW hub-mode behavior: `04-dispatch-loop.md`
section 5 has no epoch check (the epoch guard today lives only in the
section 4 orphan rule). In hub mode the stuck path compares the stored
epoch against the hub row first, and a stale epoch skips instead of
restarting.

Crash recovery, two cases:

- **Lease expired, holder truly dead.** Any host may propose the
  ticket; the grant transaction sees the lapsed lease and grants
  with `epoch+1`. The dead holder's ordinary orphan rule
  (`04-dispatch-loop.md` section 4) still applies locally and agrees,
  because the local row's holder is gone with it.
- **Holder alive but partitioned.** It keeps running its agents
  (decided), but its renewals fail and the lease lapses. If nobody
  else claimed the ticket meanwhile, the holder re-grants on
  reconnect and adopts the new epoch (a re-grant always bumps the
  epoch, even for the same agent, so any in-flight duplicate from
  before the partition is fenced). If another host was granted the
  ticket while this one was away, the reconnecting host is a stale
  holder: it stops the ticket's units through the injected stop
  seam, drops its local claim state without touching the hub row,
  and logs. Stopping is local only; worktrees and branches stay
  under the cleanup proof rule and are never deleted by this path.

Stale-holder behavior on reconnect is therefore: stop, drop, log;
never resume, never re-propose in the same pass. The next pass may
propose only tickets the hub shows as unclaimed.

Operator verbs (all client commands against the hub, all logged):

- `korapy hub add-host --id host-1`: mints the host id and its
  bearer token (section 6), prints the token once.
- `korapy hub remove-host --id host-1`: bumps `membership_epoch`
  in the meta row and releases that host's claims (epochs kept for
  the next mint). The bump is visible to a rejoining host through
  the register and heartbeat replies, which both carry the current
  `membership_epoch` (section 1): a host whose stored epoch is older
  than the reply's stops its agents, drops their claims, and does
  nothing until added again. Same rule as the peer plan's removal,
  minus the ack roundtrip.
- `korapy hub release <repo> <number>`: force-releases one claim
  (operator override, logged with reason).
- `korapy hub rotate-token --id host-1`: revokes and re-mints one
  host's token (section 6).

## 3. Hub-down behavior

Exactly as decided, no more and no less. When the hub is
unreachable:

- Hosts keep running work they already hold: workers and reviewers
  continue, commits land, pushes go out, PRs open through the forge,
  finished workers reconcile and release locally per
  `04-dispatch-loop.md` section 5 (the local release stands; the hub
  row lapses by lease and is reconciled on reconnect).
- No new claims. Any pass that attempted a hub operation and found
  it unreachable records "hub unreachable" in the logbook, skips
  every claim-needing candidate with that reason, and exits 3
  (host unreachable), the existing meaning of that code. Local-only
  duties (registry and unit liveness, hygiene, journal, snapshot,
  outbox drain to the tracker) continue through the outage, per the
  `04-dispatch-loop.md` section 4 outage rule.
- Resume of already-running agents is unaffected: resume needs no
  claim, only the local registry and session, which never leave the
  host.

A reconnecting host reconciles in this order, and only in this
order:

1. Register and heartbeat: prove identity (token), refresh
   `last_seen`, report current capacity and constraints, and read
   the reply's `membership_epoch`. A newer epoch than the host's
   stored one means the host was removed while away: it stops all
   its agents, drops all local claim state, and does nothing until
   added again (section 2, `remove-host`).
2. Fetch the hub claims table and compare, ticket by ticket,
   against locally held claims.
3. Fence the stale: for every local claim the hub shows under a
   newer epoch or a different holder, stop the ticket's units
   through the injected stop seam and drop the local claim state.
4. Re-adopt the live: for every local claim the hub still shows
   under this host with the same epoch (lease lapsed but no new
   owner), re-grant and adopt the bumped epoch; hub rows naming
   this host for which the host has no local record are released
   unconditionally (the host holds nothing for them, so releasing
   is safe).
5. Resume renewals and snapshot pushes, then allow new claims.

Steps 3 before 4 is load-bearing: a host must never renew or
propose before it has stopped what is no longer its. Steps are
idempotent, so a pass cut mid-reconcile is safe to re-run.

UNVERIFIED until run: full partition timing (lease lapse versus
reconnect under a 30 second heartbeat) needs a loopback fault
injection test with a scriptable clock, no real network (section 9).

## 4. Single-host mode

With no hub configured, korapy behaves as today. The config key is
`hub.url`; absent means single-host: the drive skips the hub step
entirely (no exit 3 from a hub that was never configured), claims go
through the local store transaction only, and the dashboard is the
host-local API from `05-dashboard-api.md`. A fleet of one needs no
hub, and configuring one must not change any single-host path.
Implementation rule: every hub call site branches once on the
resolved `hub.url`, so single-host behavior is the absence of the
step, not a hub client pointed at localhost.

## 5. Dashboard

The hub serves the aggregated read-only view. The host-local API
from `05-dashboard-api.md` stays exactly as designed: same routes,
same shapes, same token rule on loopback. The hub merges; it never
replaces per-host reads.

- New hub routes, same conventions (GET only, JSON, versions at
  `/v1/version`): `GET /v1/hosts` (membership, capacity, load,
  `last_seen`, `reachable` per host), `GET /v1/snapshot?project=`
  (merged agents, quota, capacity, disk across hosts) with
  `project=all` as the default, and per-project slices by name.
  Filter values come from the hub's projects table; an unknown
  project is `404`, never an empty merge that hides a typo.
- Push, not pull, for snapshots. Each host pushes its host-local
  snapshot document with `POST /v1/heartbeat` on the heartbeat
  interval (one call carries heartbeat, lease renewals, and the
  snapshot; section 2 names the fields). Push needs
  only outbound connectivity from hosts, matches the hub-down
  detection the hosts already do, and gives the hub a document to
  serve while a host is down. Pull (hub scraping hosts) was
  considered and rejected: it needs inbound reachability to every
  host, turns one host outage into a dashboard gap, and duplicates
  the staleness math the serving host already does.
- Staleness is shown honestly per host. Every merged section keeps
  its host's `now`, `read_at`/`fetched_at`, and confidence fields
  untouched; the hub adds only `received_at` (hub clock) and a
  `stale` flag per host when `received_at` is older than the one
  named hub setting `hub.stale_after_s` (default three heartbeat
  intervals). A host that stops pushing keeps its last rows
  visible with a growing "as of" age; rows are never dropped
  silently and never presented as current.
- Clock skew is shown as data. The hub never compares timestamps
  across hosts and never orders merged rows by time. Each host's
  `now` is displayed beside its rows, exactly the rule from
  `05-dashboard-api.md` section 5, extended to the merge. Skew
  between hosts is therefore visible on the dashboard instead of
  corrupting it, and skew between a host and the hub is visible as
  `host_now` versus `received_at` on the snapshot row.

## 6. Security

- Token model: the bearer model from `05-dashboard-api.md`
  section 3, extended to one token per host. `korapy hub add-host`
  mints a random token (stdlib `secrets`), prints it once, and
  stores only a salted hash hub-side (stdlib `hashlib`, salt beside
  the hash). The host keeps its token in `<config_dir>/hub-token`
  (`0600`, same rule as the API token), resolved by exactly one
  helper (`paths.hub_token_path`, next to `paths.api_token_path`
  from `05-dashboard-api.md`); the hub token file is never the API
  token file. Every hub request needs
  `Authorization: Bearer <token>`; failures answer `401` and log
  only the fact, never the presented value.
  Constant-time comparison, read at call time, never stored in
  logs or settings.
- Remote bind rules: the hub binds loopback by default. A bind
  that is not loopback or private warns on stderr and keeps
  serving (decision D2): only loopback, private addresses, and
  `localhost` stay quiet; anything else warns and serving
  continues. There is no `--allow-remote` flag and no bind
  refusal: the operator knows best.
- Leaked token: the operator runs
  `korapy hub rotate-token --id <host>`; the old hash is replaced
  immediately, so the leaked token dies on the next request. The
  affected host's requests get `401` until its operator installs
  the new token; a `401` is a configuration error (exit 4 naming
  the host), never silent, never retried as an outage. Rotation is
  per host: no other host's token changes, which is why tokens are
  per host rather than shared (see Options).
- TLS and tailnet, stated explicitly: the hub speaks plain HTTP
  and relies on the private network for encryption and for keeping
  the port off the public internet. That is the decided transport
  (hub reachable over a private network, for example a tailnet).
  Running the hub on an address reachable beyond that network is
  unsupported; the doctor warns when the hub bind address looks
  non-private. UNVERIFIED until run: the exact private-range check
  against real tailnet addresses; safe to learn with loopback
  fixtures of known private and public addresses, no network
  needed.

## 7. Hub operations

- Location: the database lives at `<hub-state>/hub.sqlite` (WAL
  mode), resolved by exactly one helper in `paths.py`, following
  the one-fact-one-place rule. The hub runs on the maintainer's
  host under its own state dir; it never shares a state dir with a
  worker host's korapy.
- Backup: a file copy of the SQLite database (WAL checkpointed
  first) is a complete backup; the stdlib `sqlite3` backup API
  gives an online variant. Either is operator-run, on a schedule
  the operator owns. Restore is a file replacement while the hub
  is stopped.
- Export and import, so the hub can move to another host:
  `korapy hub export` writes JSON with hosts, projects, and claims
  including epochs; `korapy hub import` loads it into a fresh hub.
  Epochs are preserved across the move, so fencing continuity
  survives it. Snapshots are excluded by design: hosts re-push on
  their next heartbeat. Token hashes are excluded: after an import
  the operator re-runs `add-host` or `rotate-token` for each host
  and installs the minted tokens, so no secret crosses the export.
- Loss and rebuild: the tracker stays the durable record. A hub
  that loses its database rebuilds membership from re-registering
  hosts and claims from the tracker's in-flight labels (the
  resync `adopt_claim` path), with recovery epochs as today. Any
  claim the tracker no longer shows is not rebuilt.
- Version compatibility: hub and hosts exchange `(api,
  schema_version)` on every call, per `05-dashboard-api.md`
  section 4. The hub refuses an older major with a typed error;
  the host maps it to exit 4 naming both versions. Additive-only
  within a major; a breaking change is a new path major with a
  cutover, following the one-version rule. Hub and hosts upgrade
  in lockstep per the rollout tickets (section 10); mixed fleets
  beyond one migration window are unsupported.

## 8. Placement

Only the hooks, as decided. Each host advertises constraints at
registration and on every heartbeat (docker present, attached
hardware, free disk, clones it serves). A start request carries
constraints as today; a host proposes a hub claim for tickets
whose constraints it satisfies. **A resume always runs on the host
that holds the session**: the host whose registry and session dir
hold the ticket's session resumes it there, and sessions never move.
Resume takes no hub claim: a claim may lapse while its session lives
on in the same host, and that residual race is allowed, bounded by
the lease and the host heartbeat. If another host was granted the
ticket meanwhile, fencing catches the stale holder at its next
renew, and the reconnect rules (section 2) stop it. Remote launch
and stop (one host starting or stopping work on another) are a later
phase and are not designed here; nothing in this doc assumes them,
and no route in section 5 stops, launches, or edits settings.

## 9. Testing

Every seam injected, loopback only, no real network. The suite guard
already refuses non-loopback addresses; hub tests bind `127.0.0.1`
on ephemeral ports and speak to them in-process.

| Behavior | Seam (fake) |
| --- | --- |
| Grant atomicity: two racing proposes, one wins with `epoch+1` | temp hub DB, two fake hub clients over loopback |
| Conflict names holder; loser skips to next candidate | temp hub DB plus temp board store |
| Renewal moves lease, keeps epoch; wrong epoch gets `409`; heartbeat carries the `renewals` list from section 2 | fake clock, temp hub DB, heartbeat body shape assertion |
| Renewal `409` in a heartbeat batch or on the single-renew route stops the units, drops the local claim, and logs; no resume, no same-pass re-propose | fake clock, temp hub DB, superseded epoch; stop seam must be called, next-pass propose counter stays zero |
| Release clears holder, keeps epoch; next grant fences | temp hub DB, epoch assertions |
| Crash: lease lapse re-grants to another host with bumped epoch | fake clock advanced past `lease_ttl_s` |
| Partition: holder keeps running locally, cannot claim, reconciles stale-first on reconnect | fake hub client scripted unreachable, then live hub with superseded epoch; stop seam must be called |
| Reconnect order: fence before renew before propose | ordered fake recording call sequence |
| Hub-down: held work continues, new claims skipped, exit 3 | fake hub client raising unreachable; local registry and forge fakes |
| Single-host: no hub configured, zero hub calls, today's behavior | read-only hub-client counter asserting no calls |
| Snapshot push: hub serves merged view per project and all; unknown project is `404` | canned host documents, temp hub DB |
| Staleness: silent host keeps last rows with growing age and `stale` | fake clock, `received_at` assertions |
| Clock skew shown, never compared: merge keeps per-host `now` | two canned documents with skewed clocks, byte comparison |
| Token: missing or wrong bearer is `401` with nothing leaked; rotation kills old token only | capturing log handler, temp token store |
| Bind warning: a non-loopback, non-private bind warns on stderr and keeps serving | bind helper over loopback fixtures of known private, shared, and public addresses, no DNS |
| Export and import preserve epochs; snapshots excluded | temp hub DB roundtrip |
| Version mismatch is exit 4 naming both versions | canned version replies |
| Import closure: hub client unreachable from host read paths | walk of read-path imports, as in `05-dashboard-api.md` section 7 |

## 10. Rollout

Each ticket is one agent of work with its own tests; each builds on
the previous ticket's tests. Order matters: hub with claims and a
registry first, the aggregated dashboard last.

- H1: hub skeleton, host registry, claims table. New hub service
  (stdlib HTTP plus SQLite), `hub.url` setting, `korapy hub
  add-host`, atomic grant with epochs, `409` conflicts.
  Acceptance: the grant-atomicity and conflict tests pass; `make
  check` green. H1 updates `README.md` for the new `korapy hub`
  verbs and `hub.url` (the only README change this design needs).
- H2: leases, renewal, release, crash recovery. Renew and release
  routes, `lease_ttl_s` and `heartbeat_s` settings, expiry
  re-grant with bumped epoch, stale-holder stop-and-drop on
  reconnect, and the NEW stuck-restart epoch check from section 2
  (stale epoch skips instead of stop-plus-launch). Acceptance: the
  lease, fencing, stuck-restart, and recovery tests pass; `make
  check` green.
- H3: drive composition and single-host mode. Hub claim first,
  hub-epoch adoption in the local store, hub-unreachable skips
  with exit 3, reconnect reconcile in section 3 order, zero hub
  calls without `hub.url`, and the stuck path honoring the hub
  epoch from section 2. Acceptance: the composition,
  hub-down, order, stuck-epoch, and single-host tests pass; `make
  check` green.
- H4: operator verbs. `remove-host` (membership epoch, claim
  release), per-ticket `release`, `rotate-token`. Acceptance: one
  test per verb, including the removed-host-rejoins rule;
  `make check` green.
- H5: snapshot push and the aggregated dashboard. Heartbeat
  carrying renewals plus the snapshot document, hub merge routes
  (`/v1/hosts`, `/v1/snapshot?project=` with `all`), per-host
  staleness under `hub.stale_after_s` and clocks-as-data. The
  host-local API from `05-dashboard-api.md` is unchanged. Acceptance: the merge,
  filter, `404`, staleness, and skew tests pass; `make check`
  green.
- H6: hub operations. State-dir path helper, backup and
  export/import roundtrip with epochs preserved, version exchange
  with exit 4 on older major, doctor checks for hub reachability
  and token health. Acceptance: the roundtrip and version tests
  pass; `make check` green.
- H7: takeoff. Full suite with `TEST=1`, `make check`, and the
  takeoff ritual on a branch, then review. Acceptance: green
  gates and a review record. No further README change is expected
  (H1 already covered the `korapy hub` verbs and `hub.url`);
  record that reason in the H7 PR body.

## 11. What the hub owns versus resource governance (issue 195)

The hub and the resource governance work share one sentence each,
and neither decides for the other:

- **The hub owns cross-host mutual exclusion and the merged view.**
  Which host holds which ticket (claims, epochs, leases,
  membership), and what every host reported (snapshots, staleness,
  clocks). It learns each host's capacity and load and displays
  them. It never refuses a claim for capacity reasons and never
  starts, stops, or places work.
- **Resource governance (issue 195) owns per-host admission.**
  Whether this host may start work now given its CPUs, memory,
  floors, ceilings, and disk, decided by the host alone through
  the admission path. It never grants a cross-host claim and never
  reads another host's state.

The seam between them is one fact in one place: the host's
capacity and load are reported by the host (governance side) and
stored by the hub (display side). Any second decider, a hub that
second-guesses admission or a host that second-guesses a claim,
is a bug.

## 12. Open decisions

Decided (no options; do not reopen): one token per host rather
than one shared hub token; the hub serves the merged dashboard
itself with no separate process; export format is JSON with epochs
preserved. Two choices remain genuinely open:

**D1: heartbeat and lease defaults.** Tunable either way; the
question is only the shipped default.

Options:
1. 30s heartbeat, lease 3 heartbeats (recommended).
2. 10s heartbeat, lease 3 heartbeats.
3. 60s heartbeat, lease 3 heartbeats.

**D2: hub startup behavior on a non-private bind address.** Warn
keeps the hub up where the operator knows best; refuse fails
closed.

Options:
1. Warn on stderr, keep serving (recommended).
2. Refuse to start with exit 4.
