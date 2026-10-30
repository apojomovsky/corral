# 05: the dashboard API (`corral serve`)

Status: design, not built. This doc fixes a read-only HTTP API over the
fleet state corral already keeps, served by a new `corral serve` verb
from the standard library only. Nothing here changes the snapshot
schema, the registry, the board store, the coordinator files, quota,
admission, exit codes, or any existing CLI verb, so `README.md` needs
no change. The only index change is the row for this doc in
`docs/README.md`.

Conventions: "dashboard" means any UI owned by a project or a user,
never by corral. Field names below reuse the existing builders
(`snapshot.build`, `quotacli.snapshot`, `board.Store`,
`coord.read_entries`, `coordevents.read_events`, `launch.tail`) so the
API has no second resolver for any fact. Shapes quoted from those
modules were read from the tree; anything needing a live run is marked
UNVERIFIED with a safe way to learn it.

This doc is step 1 of milestone 2 in `00-design.md`, section 4 ("Put
the local API in front of everything on one host"). The peer pull and
the log follow later; this doc keeps every choice they need (host
identity, cursors, Bearer token) without designing them.

## 0. Where it fits

One new module, `src/corral/serve.py`, plus a `serve` section in
`settings.py` and a token path helper in `paths.py`. The server is
`http.server.ThreadingHTTPServer` from the standard library, GET only,
on loopback by default. Every endpoint reads the same local files the
CLI already reads and calls no other tool: no `gh`, no network, no
`systemctl`, no runtime binary. The GitHub rate budget is never
touched by a read (section 6).

## 1. What the API serves, and what it does not

The single source of truth is the `corral snapshot --json` document
(schema version 1 today): `schema_version`, `fleet_header` (host, now,
live/stale/finished counts), `agents` rows (id, project, repo, number,
runtime, model, state, started_at, last_seen, note), `quota` (per
provider state, windows, read_at, stale, mark, reader, error),
`capacity` (total_cap, repo_cap, live, by_project, pending), `disk`
(per path totals or per path error). `GET /v1/snapshot` returns that
document unchanged, built by the same `snapshot.build` with the same
injected views, so the CLI and the API can never disagree.

Filtered views of the same builders, for cheap polling and for detail
the snapshot only summarizes:

- `GET /v1/agents`: the `agents` list plus `fleet_header`.
- `GET /v1/quota`: the `quota` section plus `now`.
- `GET /v1/capacity`: the `capacity` section plus `now`.
- `GET /v1/disks`: the `disk` section plus `now`.
- `GET /v1/board?repo=&state=&label=&max_age=`: board store rows
  oldest first, each with its confidence (`confirmed`, `pending`,
  `dirty`), `fetched_at`, claim holder (agent, host, epoch,
  claimed_at) when held, and queued agent/status overlays. Listing
  goes through `Store.items` (which takes no `max_age` and reports no
  freshness); freshness is computed in exactly one place in the serve
  layer by calling `Store.item(repo, number, now=..., max_age=...)`
  per listed row, so `item()` stays the single freshness resolver. A
  decision-grade consumer asks for freshness with `max_age` and sees
  staleness in the reply instead of guessing it.
- `GET /v1/events?project=`: one project's unresolved coordinator
  events oldest first (`coordevents.unresolved`), each with id, kind,
  ref, evidence, state, first_seen. A `resolved_since` parameter
  includes recently resolved rows for consumers that reconcile.
- `GET /v1/journal?project=&kind=&limit=&cursor=`: the project's
  logbook entries oldest first (`coord.read_entries`), paged by an
  opaque cursor the server mints (the entry index today; consumers
  treat it as opaque). Archives (`journal-YYYY-MM-DD.jsonl`) are
  included when the cursor predates the live file, so a consumer that
  polls slowly still sees a continuous log.
- `GET /v1/tail?repo=&number=&last=`: one ticket's rendered agent
  activity via `launch.tail`, plain text, bounded by a server side
  maximum (default 200 lines, see section 6). This is activity lines
  only, the same output `corral tail` prints.
- `GET /v1/stream?topics=&cursor=`: Server-Sent Events (section 2)
  over the journal, the event queue, and a snapshot generation
  counter.
- `GET /v1/version`: `{api: 1, schema_version: 1}` for change
  detection (section 4).
- `GET /v1/health`: `{ok, host, now, uptime_s, requests_total,
  requests_304, degraded}` where `ok` is false exactly when
  `degraded` (the section names whose last build errored) is
  non-empty, and the counters reset on restart by design (section
  6). The per-section `{"error"}` body inside a `200` response is the
  authoritative degraded-state channel; `/v1/health` is only a
  roll-up of those same errors for polling dashboards, never a second
  verdict.

What is NOT served in v1:

- Secrets and tokens: the API token itself, runtime logins, the
  GitHub token, provider API keys. They are read at call time and
  never stored or logged (AGENTS.md), and no endpoint returns them.
- Quota payloads beyond usage numbers: `windows` carry `used_pct`
  and `resets_at` only, as `quotacli.snapshot` already shapes them.
  Reader credentials, raw allowance documents, and failure text beyond
  the recorded error string stay out.
- Session contents by default: no endpoint returns a transcript or a
  prompt file. `tail` returns rendered activity lines, bounded, and a
  full transcript stays a later, explicitly authenticated endpoint,
  not v1.
- Home configs and host internals: no `~/.config` or `~/.omp` paths,
  no process list, no unit control. Stopping, launching, and editing
  settings are writes, and v1 has no writes at all (section 3).
- Anything live: every endpoint reads local files. A tracker outage
  changes nothing except the staleness fields, exactly like the board
  store's offline rule in `00-design.md`.

## 2. Transport: loopback HTTP, polling plus SSE

Decision: `corral serve` runs a read-only HTTP server on loopback by
default, using only `http.server` and `json` from the standard
library. Two alternatives were considered and rejected:

- A static JSON file written by a timer: it cannot answer `tail`,
  `journal` cursors, or board filters without one file per query, and
  the writer would duplicate the server's read path while adding disk
  writes and a second staleness story. The file is still useful as a
  fallback (any consumer can `GET /v1/snapshot` on a schedule and save
  it), but it is a consumer choice, not the interface.
- A unix socket: browsers cannot speak HTTP over a unix socket, and
  dashboards are often browser pages. HTTP serves both a local page
  and a future peer pull over the same routes, which milestone 2
  already assumes (`00-design.md`, section 4: peers pull each other's
  logs over the API).

Polling is the base contract: every GET is cheap, cacheable, and
carries its own freshness (`now`, `read_at`/`stale`,
`fetched_at`/confidence), so a dashboard that polls `/v1/snapshot`
every few seconds is correct without any other machinery.
Conditional requests (`ETag` from a snapshot generation counter,
`Last-Modified` from `now`) let polls cost a `304` when nothing
changed.

Live updates use Server-Sent Events at `GET /v1/stream`, not
websockets: the flow is server to client only, `text/event-stream`
needs no new dependency, and `EventSource` reconnects on its own.
The stream carries three event types (`snapshot`, `events`,
`journal`), each with an id the client sends back as `Last-Event-ID`
to resume after a drop. The server emits from its own short poll of
the local stores (the `coordevents.WAIT_POLL_S` pattern: stdlib has no
inotify, so a poll is the mechanism); between polls it sends comment
heartbeats so idle connections stay visibly alive. A consumer that
never opens the stream loses nothing: polling covers every fact.

Discovery: the server binds an ephemeral port by default (`--port 0`)
and prints one line, `listening on 127.0.0.1:PORT`, as the machine
contract. `--port N` pins a port for dashboards with a fixed config.
No port number is reserved by this design: claiming one needs a host
registry that does not exist yet.

## 3. Security

- Bind address: `--bind` defaults to `127.0.0.1`. A non-loopback
  `--bind` is refused (exit 4) unless both `--allow-remote` is passed
  and a token file exists. Refusing by default follows the test
  guard's line (`tests/suiteguard.py` refuses every non-loopback
  address): loopback is the only address the suite can exercise, and
  production should need an explicit decision to go wider.
- Token: `<config_dir>/token`, resolved by one helper
  (`paths.api_token_path`, the same "one fact, one place" rule as
  every other path). The file holds one Bearer token. It is read at
  call time, compared in constant time, and never stored or logged;
  like every secret, it is never a setting (`settings.py`:
  "Secrets are never settings"). On loopback with no token file, the
  server serves openly: the loopback boundary is the auth, the same
  posture as the state dir itself. On a non-loopback bind the token is
  required and every request needs `Authorization: Bearer <token>`;
  without it the server answers `401` and logs only the fact of the
  rejection, never the presented value. This matches the milestone 2
  rule corral already records: host plus Bearer token, never the
  network layer, is the authentication.
- Token file permissions: created `0600` when `corral serve
  --new-token` mints one (the only write the verb ever does, to its
  own file); a token file readable beyond its owner warns on stderr
  and, on a non-loopback bind, refuses to start (exit 4).
- CORS: no `Access-Control-Allow-Origin: *` ever. Default serves
  same-origin only; `--allow-origin ORIGIN` (repeatable, exact match,
  no wildcards) adds named dashboard origins. Preflight answers only
  the origins configured.
- No writes: v1 registers GET only. Any other method answers `405`,
  unknown paths answer `404`, and neither path executes anything.
  There is no `POST /v1/ack`, no launch, no stop, no settings edit:
  those are later designs with their own auth story. A read-only
  server cannot be tricked into a state change because no code path
  from a request reaches a write.
- Error bodies are minimal (`{"error": text}`): no config paths, no
  token hints, no tracebacks.

## 4. Schema stability and exit codes

- Versioning: the path carries the API major (`/v1/`). The snapshot
  keeps its own `schema_version` field, and `/v1/version` reports
  both (`{api, schema_version}`). A breaking change is a new path
  major; the old major stays until a cutover PR removes it, following
  the one-version rule (AGENTS.md): no side by side dialects beyond
  one migration window.
- Additive-only within a major: new fields and new endpoints may
  appear; existing names keep their types and meaning; nothing is
  renamed or removed. `agents` rows keep null for missing fields,
  never absent keys, per the existing `_agent_row` rule.
- Change detection: a consumer compares `/v1/version` (or the
  `schema_version` inside any snapshot) against what it was built
  for. Unknown fields are ignored by contract; a missing expected
  field means the server is older than the consumer, which must say
  so instead of guessing.
- Exit codes of the verb (a public interface, AGENTS.md): `0` on a
  clean shutdown after `SIGINT`/`SIGTERM`; `4` on a usage or
  configuration error (bad `--bind`/`--port`, a non-loopback bind
  without `--allow-remote` and a token, an unreadable token file, a
  bind error other than a held port such as a bad address or
  permission); `2` when the port cannot be bound because it is already
  in use (`EADDRINUSE`: race lost or already running); `3` only when
  the state dir cannot be read at startup (the `snapshot.py`
  all-failed precedent). A request that finds one section unreadable
  does not move the exit code: the section reads `{"error": ...}`
  inside a `200` document, the snapshot rule that one broken section
  never breaks the rest.

## 5. Multi-host: mergeable without redesign, aggregator not designed

A future aggregator merges several hosts' documents without asking
this API for anything new. What it needs is already in the shapes:

- Identity: every response envelope carries `host` (the value
  `fleet_header.host` carries today) and every agent row keeps its
  host. A later slice may add a stable host id file next to the token
  when hostnames prove unreliable; v1 must not mint identity in two
  places, so it reuses the snapshot's host string and records the
  possible follow-up without building it.
- Time: every document carries `now` (epoch seconds) from the serving
  host, and every reading carries its own `read_at`/`fetched_at`.
  The aggregator never compares timestamps across hosts: staleness is
  computed by the serving host before sending (the `stale` flag, the
  confidence value), and the aggregator displays each host's `now`
  beside its rows so clock skew shows as data instead of corrupting a
  merge. Merge keys are `(host, repo, number)` for agents and claims,
  `(host, provider)` for quota, never bare ticket numbers.
- Cursors: `journal` and `stream` cursors are per host and opaque;
  the aggregator stores one cursor per host. Peer pull in milestone 2
  reuses the same cursor shape for log replication, which is why the
  cursor is opaque from day one rather than a documented index.

Nothing else about aggregation is designed here: no fan-out proxy,
no shared duty split, no claim acks. Those belong to the milestone 2
peer design.

## 6. Performance: bounded reads, cached snapshot, honest staleness

- The GitHub budget is never touched by a read. This is structural,
  not a convention: `serve.py` imports the local readers only
  (registry, board store, journal, events, quota ledger, disk usage).
  The `gh` seam is unreachable from any request handler, and a test
  asserts the import closure (section 7). A dashboard that polls
  forever cannot pace-limit the fleet.
- Cost bounds: a snapshot is local file reads plus one SQLite query
  set; `tail` is capped at a server side maximum (`serve.tail_max`,
  default 200 lines: a larger `last` is clamped, not refused);
  `board` and `journal` page with `limit` defaults (100 rows) and
  maxima; the stream emits at most one event per store change per
  poll interval. Disk usage reuses `shutil.disk_usage` (no tree
  walk): the API reports filesystem free space, never the reclaim
  plan's directory walk, which stays a CLI cost. Concurrent
  `/v1/stream` connections are bounded (`serve.max_streams`, default
  20: dashboards are few and each stream holds a
  `ThreadingHTTPServer` thread); past the bound the server sheds with
  `503` plus a `Retry-After` header and holds no thread for the
  refused connection. Polling covers every fact, so a shed stream
  loses nothing.
- Caching: the snapshot document is cached in memory with a short
  TTL (`serve.snapshot_ttl_s`, default 2 seconds); every request
  inside the window shares one build. A generation counter bumps on
  each rebuild and feeds `ETag`, so unchanged polls cost a `304`.
  Per endpoint counters (`requests_total`, `requests_304`) are kept
  in memory only and exposed at `GET /v1/health` with uptime; they
  reset on restart by design.
- Staleness is data, not a header the consumer must know: `quota`
  carries `read_at` and `stale` per provider, board rows carry
  `fetched_at` and confidence, the fleet header carries `now`. A
  dashboard shows "as of T" from those fields; the server never
  blocks to refresh anything.

## 7. Test plan: fakes at the existing seams, loopback only

No test runs a real server beyond a loopback ephemeral port, reads a
real state dir, or touches `gh`, systemd, or the network.
`tests/suiteguard.py` refuses every non-loopback address and records
any attempt, so the suite binds `127.0.0.1` port `0` and speaks to it
with `http.client` or raw sockets in-process. The design fits that
line on purpose: nothing in v1 needs another address.

New `tests/test_serve.py` (temp state, config, and session dirs; the
production seams take injected roots exactly like `snapshot.build`
takes injected views):

- Routes: each endpoint returns the document its builder returns,
  byte for byte where JSON is exact (`/v1/snapshot` equals
  `snapshot.build` output for the same inputs). Unknown paths are
  `404`, other methods are `405`, both with no side effects (a
  counter fake proves nothing was written).
- Snapshot failure rule: one broken section reads `{"error": ...}`
  inside a `200`; all broken is still `200` with every section
  errored (the CLI maps all-failed to exit 3, but an HTTP server has
  no exit per request; the `/v1/health` body reports the degraded
  state instead of hiding it).
- Tail bounds: `last` above the maximum is clamped; a missing ticket
  is `404`; output equals `launch.tail` rendering for the same
  session file.
- Board freshness: `max_age` honored, `fetched_at` and confidence
  visible; journal pagination returns a continuous log across the
  live file boundary using canned archives.
- Security: non-loopback bind without `--allow-remote` and a token
  refuses before listening (exit 4, asserted on the parser/validator,
  not by binding); with token required, a missing or wrong Bearer is
  `401` and the presented value never appears in logs (asserted with
  a capturing log handler); `--allow-origin` echoes only configured
  origins, never `*`.
- Stream: an SSE response parses as `text/event-stream`, carries ids,
  and resumes from `Last-Event-ID` with no gaps against a canned
  journal; heartbeats arrive within the idle timeout; past
  `serve.max_streams` concurrent streams the next connection gets
  `503` plus `Retry-After` and no thread is held for it (asserted by
  opening the bound plus one against a small test bound).
- Import closure: a test walks `serve.py` imports and fails on `gh`,
  tracker, forge, or any subprocess seam, so a later slice cannot
  smuggle a live call into a read.
- Exit codes: startup failures map through the same validator the
  verb calls: bad flags to 4, bad token perms to 4, unreadable state
  dir to 3, a held loopback port (`EADDRINUSE` via a held loopback
  socket) to 2, and other bind errors (bad address, permission) to 4.
  UNVERIFIED
  until run: behavior under a real browser `EventSource` client and
  under concurrent dashboard polls; both are safe to learn with
  loopback tools and need no model, network, or account.

## 8. Implementation tickets

Each is one agent of work with its own tests; none changes a public
interface beyond adding `corral serve` and its settings. Order
matters: each ticket builds on the previous one's tests.

- S1: routes and the snapshot. Build `serve.py` with the request
  router, `GET /v1/snapshot` from `snapshot.build`, `/v1/version`,
  `/v1/health`, the JSON error shape, and the `405`/`404` rules.
  Acceptance: the route, snapshot-equality, and import-closure tests
  pass; `corral snapshot --json` output unchanged; `make check`
  green.
- S2: settings, bind rules, and the verb. Register `serve.bind`
  (default `127.0.0.1`), `serve.port` (default `0`), `serve origins`
  (default empty), `serve.snapshot_ttl_s`, `serve.tail_max`,
  `serve.max_streams`; add
  `paths.api_token_path`; wire `corral serve [--bind] [--port]
  [--allow-remote] [--allow-origin] [--new-token]` through the CLI
  with the section 3 refusal order and the section 4 exit codes.
  Acceptance: the bind/token/CORS tests pass; the token is read at
  call time and appears in no log or setting; `make check` green.
- S3: filtered views. Add `/v1/agents`, `/v1/quota`, `/v1/capacity`,
  `/v1/disks`, `/v1/board`, `/v1/tail` with limits and clamps.
  Acceptance: each view equals its builder's output for the same
  inputs; tail clamps and `404`s; board shows confidence and
  `fetched_at`; `make check` green.
- S4: events, journal, and the stream. Add `/v1/events`,
  `/v1/journal` (live plus archives, opaque cursors), and `/v1/stream`
  with ids, resume, and heartbeats. Acceptance: the pagination,
  continuity, SSE, and stream-shedding (`503` plus `Retry-After` past
  `serve.max_streams`) tests pass; polling alone still covers every
  fact; `make check` green.
- S5: caching and health. Add the snapshot TTL cache, the generation
  counter with `ETag`/`304`, and the in-memory request counters in
  `/v1/health`. Acceptance: repeated polls within the TTL share one
  build (asserted with a counting builder fake); `304` on matching
  `ETag`; `make check` green.
- S6: takeoff. Full suite with `TEST=1`, `make check`, and the takeoff
  ritual on a branch, then review. Acceptance: green gates and a
  review record; a README Commands row for `corral serve` (a new
  user-visible verb, so README freshness applies), with the reason
  recorded in the PR body.
