# 12: the web dashboard (browser UI over `corral serve` and the hub)

Status: design, awaiting maintainer approval. No code ships with this
doc. It designs a read-only browser UI served by the two processes
that already serve JSON: `corral serve` for one host
(`05-dashboard-api.md`, built as `src/corral/serve.py`) and
`corral hub serve` for the merged multi-host view
(`08-hub-multihost.md`, built as `src/corral/hub.py`). One screen to
see every project and every agent, filterable by project with an
"all" view, on arbitrary devices that can reach the network,
tablets included.

Nothing here changes the snapshot schema, any existing route shape,
exit codes, or any CLI verb's behavior. The Python additions this
design calls for are additive only: static asset routes, one config
route, security headers, and the small optional API additions named
in section 10 and put to the maintainer in section 13. `README.md`
changes land with the implementation tickets that make the UI
reachable (section 10 says which), not with this doc; the only
index change here is the row for this doc in `docs/README.md`.

Conventions: shapes, constants and behaviors quoted from
`serve.py`, `hub.py`, `snapshot.py`, `board.py`, `coord.py` and
`quotacli.py` were read from the tree at design time. Browser and
library facts that could not be verified from the tree are marked
UNVERIFIED with a safe way to learn them. "Host mode" means the UI
is served by `corral serve` on one host; "hub mode" means it is
served by `corral hub serve` and reads the hub's merged routes.
Line and byte counts for the UI itself are estimates and are
labelled as such; they are budgets the test plan enforces
(section 8), not measurements.

Decided by the maintainer (not reopened here): the UI runs in a
browser on arbitrary network devices including a tablet,
touch-first and responsive; simple and lightweight wins, with the
stack chosen pragmatically (section 1); it shows what the APIs
already serve plus the hub's merged view with honest per-host
staleness. Where this design challenges a coordinator
recommendation or a maintainer requirement, section 11 says so
plainly with the cost saved, and section 13 puts each real choice
in an Options block with one marked recommended.

Three findings from the code shape everything below:

1. **A browser cannot attach a Bearer [REDACTED] to its first
   navigation or to a native `EventSource`** (platform behavior,
   UNVERIFIED here; the smoke checklist in section 8 confirms it
   on the maintainer's browsers). On a non-loopback
   bind, `corral serve` requires the token on every request
   (`_authorized` runs before routing in `do_GET`), so a
   token-entry page can never load unless the static shell is
   exempt from auth (section 4). And `EventSource` accepts no
   custom headers, so the existing `/v1/stream` cannot be used by
   an authed browser at all without a workaround (section 6).
2. **The hub has no read-only credential.** A hub read route
   accepts any registered host token (`_reader` is `_authed_host`
   in `hub.py`), and that same token can propose, renew and
   release claims and push heartbeats as that host (`do_POST`
   binds the caller to the body's `host_id` and nothing more).
   Putting a host token in a tablet browser would hand a
   read-only screen a write-capable fleet credential. Hub mode
   therefore needs a read-only "reader" token class first
   (sections 4 and 10).
3. **The two backends do not serve the same surface.** Host mode
   has agents, quota, capacity, resources, disks, board, tail,
   events, journal and the stream. Hub mode has three data read
   routes, `GET /v1/hosts`, `GET /v1/snapshot?project=` and
   `GET /v1/claims` (plus the open `GET /v1/version`). The merged
   document nests one host snapshot per host (`{project, now,
   hosts: [{host_id, received_at, stale, snapshot}]}` from
   `read_snapshot`, with `membership_epoch` added by the
   handler), and `GET /v1/claims` lists every claim row for the
   hosts' reconnect fetch. There is no hub board, tail, journal,
   stream, ETag or CORS today. "Same UI, different backend"
   therefore means
   one UI with a small data adapter and some views that exist in
   host mode only, saying so on screen instead of failing
   (section 2).

## 1. The stack: plain HTML, CSS and ES modules, no build

The test the coordinator set: the simplest, lightest thing that is
easy to work on and easy to update. Four honest options were
evaluated against the coordinator's criteria (eight rows below:
shipped bytes and the standard-library cost each get their own
row). Library byte sizes are
the widely published minified-and-gzipped figures for current
majors; they are marked UNVERIFIED here because nothing was
downloaded while writing this doc. Safe way to learn them before
any adoption: fetch the single pinned release file on a
workstation, run `wc -c` and `gzip -9 | wc -c`, and record both
numbers in the vendoring ticket. The ranking below does not
depend on the exact figures; the gaps are an order of magnitude.

| Criterion | A. Vanilla ES modules | B. Preact + htm, vendored | C. Lit, vendored | D. React or Vue with a build |
| --- | --- | --- | --- | --- |
| Framework bytes to a tablet (gzip, UNVERIFIED) | 0 | about 4 to 5 KB | about 5 to 7 KB | about 45 KB (React) or 16 to 20 KB (Vue runtime) |
| App bytes (estimate, all views) | 60 to 100 KB uncompressed JS+CSS+HTML | similar, slightly less rendering code | similar | similar app code, shipped as a bundle |
| Moving parts a contributor must learn | HTML, CSS, DOM APIs they likely know | components, hooks subset, htm tagged templates | web components, reactive properties, shadow DOM styling | the framework, JSX or SFCs, the bundler, npm |
| Build step or package manager | none: files are served as written | none at runtime; a human copies two pinned files and reviews them | none at runtime; several pinned module files | node, npm, a lockfile with hundreds of transitive deps, a bundler config |
| Cost to the standard-library-only rule | none: Python stays stdlib, repo gains no toolchain | none for Python; the repo gains vendored third-party JS that must be reviewed, licensed and updated by hand | same as B, more files | breaks the spirit: a second ecosystem in the repo, `system_deps.toml` gains node, `make check` would need it, and `uv_build` cannot run npm, so the built bundle must be committed as generated code or the package build breaks |
| How an update reaches devices | edit file, upgrade corral, restart the server, reload (section 7) | same, plus re-vendoring on library updates (security fixes are manual forever) | same as B | rebuild, commit the bundle (unreviewable diffs), upgrade, reload |
| Tested under our rules | Python tests cover serving, headers, contracts and source discipline (section 8); JS logic kept tiny and pure | same Python side; library behavior is trusted, not tested | same | same Python side, plus a JS test runner becomes tempting, which needs node (absent from `system_deps.toml` today) |
| How long the choice stays easy to change | forever: DOM APIs are the platform, no version to chase | years; Preact majors are rare, but the vendored copy ages until someone re-vendors | years | shortest: framework and bundler churn is continuous |

A fifth option, server-rendered HTML from Python, was considered
and rejected: it is the most testable in `unittest`, but every
interaction this UI needs (project filtering without a reload,
tail auto-scroll, staleness timers, disconnected banners) still
needs JavaScript, so the result is two renderers (one per backend
shape) plus JavaScript, and string-built HTML in Python moves the
XSS surface into the server. It remains the right answer for a
zero-JS status page, which this is not.

**Decision: option A, vanilla ES modules.** Reasons, in order:

- The data is lists, cards and one text pane, refreshed at poll
  cadence (section 6). At fleet scale (dozens of agents, board
  pages of 50 rows by the UI's own choice), re-rendering
  one section's subtree per poll is a few hundred DOM nodes.
  Keyed diffing, the main thing B to D sell, buys nothing
  measurable here.
- Read-only version one has almost no client state: the current
  view, the project filter, the last good payload per view, and
  the token. No forms beyond token entry, no optimistic updates,
  no undo. Component state machinery has nothing to manage.
- Zero dependencies means zero forced updates, zero supply-chain
  review, and a diff a Python contributor can read in full. The
  whole UI is an estimated 1,500 to 2,500 lines across all views;
  the rendering helpers it needs (`el(tag, text, attrs)`, section
  re-render, list builders) are an estimated 150 lines, written
  once.
- It is the only option that adds neither a toolchain nor any
  third-party code to review, license and keep updated: `make
  bootstrap` still needs only uv, and the repository gains
  nothing that ages.

**Runner-up: option B (Preact + htm, two vendored files).** Switch
when one of these becomes true: the actions phase arrives with
real forms (confirm dialogs, CSRF tokens, optimistic state), or
the view code passes roughly 3,000 lines, or two contributors in
a row trip over the hand-rolled rendering helpers. The migration
stays cheap if, from day one, the data layer (`api.js`: fetch,
auth, mode adapter; `fmt.js`: pure formatters) touches no DOM:
views can then be ported one at a time onto components while the
data layer stands still. That separation is a design rule in
section 3 either way.

Charts are inline SVG computed in JS (bar and gauge widths as SVG
attributes, never CSS), no chart library. Sparklines are cut:
there is no history endpoint, quota and capacity are current
values only, and inventing client-side history retention to draw
a line is scope the data does not support (section 11).

## 2. Users, screens and navigation

One user class: the operator glancing at the fleet, usually to
answer "is anything stuck, starving or broken, and what is that
agent doing right now". Everything is read-only; every control
either navigates or filters. Four views earn their place in
version one; a fifth (hosts) exists in hub mode only.

**Overview (default, both modes).** Header counts from
`fleet_header` (live, stale, finished), then cards: quota
(`quota.providers` is an object keyed by provider name; one bar
per entry of its `windows` object, keyed by window id, from
each window's `used_pct`; `windows` may be null and a single
window or its `used_pct` may be null, and those render as "no
reading" text with the provider's `error`, never as a zero bar;
the provider `state`, `read_at` age and the `stale` flag are
shown as text, never color alone), capacity (`live` of
`total_cap`, `repo_cap`,
`by_project`), resources (the host budget with charged versus
measured totals and per-project charged shares from the
`resources` section's `host_budget`, `charged`, `by_project`
and `measured_by_project`, plus `pressure` as text; the
per-agent `agents` rows stay out of version one, section 11),
disks (free of total per path in `disk.paths`),
and the drive strip (`paused` with `pause_reason`, `last_pass_at`
age, `last_exit`, `starts_today` of `max_starts_per_day`). Below,
agents grouped by project: ticket (`repo#number`), state, runtime,
model, role, age from `started_at`, `last_seen` age, and `note`
when present. A section whose value is the `{"error": ...}` body
(`snapshot.is_error_section`) renders as an error card with that
text, a failed `resources` section included: degraded state is
shown, never hidden behind a spinner,
mirroring the `/v1/health` rule in `05-dashboard-api.md`. In hub
mode the same cards repeat per host from that host's nested
`snapshot`, each headed by `host_id`, its `stale` flag and
`received_at` age; quota bars stay per (host, provider) and are
never merged, per the merge-key rule in `05-dashboard-api.md`
section 5.

**Agent detail.** Reached by tapping an agent row. The detail is
keyed by the row, not the ticket: the route carries the row's
`id` when it has one, else `repo`, `number` and `role`, and on a
cold load the view resolves that key against a fresh snapshot
("this agent is no longer listed" when nothing matches, never a
guessed neighbour). Several rows can name one ticket; each gets
its own detail and they share one tail. All row fields plus, in
host mode, the tail pane: `GET /v1/tail` for the row's `repo`
and `number`, rendered as one text pane (section 5). Agents
without a ticket (null `repo` or `number`, which `_agent_row`
allows) show fields only, with "no ticket, no tail" stated. In
hub mode there is no tail route; the detail says the
tail lives on that host and shows the host's registered
`address` from `/v1/hosts` as plain text for the operator to open
that host's own dashboard. The hub never proxies a tail.

**Board (host mode only).** A filterable, paged table over
`GET /v1/board`: state and label filters map to the route's own
query parameters, page size 50 via `limit`, previous and next via
`offset` and the reply's `total`, and every request carries a
board `max_age` (300s, a named constant in the view) because
without it the store reports every existing row fresh and the
`fresh` flag would be decoration. Columns: `repo#number`, title,
state, priority, labels, claim holder (`claim.agent`,
`claim.host` and epoch when held), `fetched_at` age with `fresh`
and `confidence` as text. The row's `url` links out to the
tracker
only when its scheme is `http` or `https`, with
`rel="noopener noreferrer"`; anything else renders as text. The
ticket `body` is never rendered in version one (sections 6
and 11). In hub mode the Board tab is absent; deep links to it
show "the board is per-host, open a host dashboard".

**Log (host mode only).** One project at a time, two stacks:
"needs attention" from `GET /v1/events` (unresolved rows: kind,
ref, evidence, `first_seen` age; the route returns all
unresolved rows unpaged, so the view renders at most 100 and
states the true count when capped), and the logbook from
`GET /v1/journal` (entries: `at`, kind, holder, refs, text; kind
filter chips from the `coord.KINDS` tuple the route validates
against). The journal API pages oldest first; the UI shows the
most recent page by learning `total` from a first `limit=1`
reply and then requesting the last window, with a "load older"
button paging backwards and a DOM cap (section 6). That window
arithmetic depends on today's cursor being the entry index,
which the API contract calls opaque: the deviation is accepted
and pinned, not hidden, by a contract test asserting the
decimal-index minting, so a future cursor change fails in the
suite instead of in a browser (section 8). In hub mode the Log
tab is absent on the same terms as the board.

**Hosts (hub mode only).** One row per registered host from
`GET /v1/hosts`: `host_id`, reachable dot plus the word reachable
or unreachable, `last_seen` age, address as text, and a capacity
and constraints summary. This view joins with the merged
snapshot by `host_id`: a registered host that has never pushed
appears here but has no entry in the snapshot's `hosts` list
(`read_snapshot` only returns hosts with a stored document), and
the overview must show such a host as "registered, nothing
pushed yet" instead of dropping it silently. `GET /v1/claims`
already lists every claim row, but there is no claims view in
version one; claims are visible per host as agent rows and, in
host mode, as board claim holders. Whether the UI ever shows
the raw claims list is decision D4 (section 13).

**Project filter.** A horizontally scrollable chip row ("All"
plus one chip per project) sits above the overview and the log
view and persists between them. It does not govern the board:
board rows carry no project field, so the board filters by
`repo`, state and label in its own controls, and the chip row is
replaced there by those. Nor does "All" merge logs: the log
routes take exactly one `?project=`, so "All" on the log view
shows the project picker, not a combined log. The selected
project rides in the hash (below), so links are shareable and
the back button works. Filter values are derived client-side
the same way in both modes: the union, over every snapshot in
play (the host's one, or each host's nested one), of agent-row
projects and `capacity.by_project` keys, mirroring the hub's
own `_project_names`, with null and empty-string projects (both
possible in the data) excluded from chips and shown under no
heading but "All". Filtering itself is also client-side in both
modes: the snapshot payload is already whole, so the hub's
server-side `?project=` filter (and its 404 for unknown
projects) goes unused by the UI and stays for other consumers.
The honest gap: a project with no agents and no counted tickets
is invisible to the Log view, because the host API has no
project list route. Section 10 includes a small additive
`GET /v1/projects` ticket before the Log view ticket, and
decision D5 covers it; even then the route is
registration-derived (`projects.load_all` reads the registry
dir and returns an empty list when there is none, while
`coord.project_root` accepts any plain project name), so the
UI unions the route's answer with the derived set and never
intersects, and says when its project list is derived from
activity only. A project known to neither source cannot be
offered, and the log view does not free-type project names
into the API.

**Navigation and routing.** Hash routing only:
`#/` overview, `#/agent/<row-key>` with the keying of the agent
detail above, `#/board`, `#/log`, `#/hosts`, the overview and
log optionally carrying `?project=<name>`. The server
therefore needs exactly one document route (`GET /`) and never a
fallback handler, a refresh can never 404, and no route table
grows in Python. The cost is uglier URLs, accepted. Unknown
hashes land on the overview with a notice. Layout: a top bar
(title, mode and host, connection state, last-updated age, token
button), the project chip row, then content. On narrow screens
the view tabs (Overview, Board, Log, Hosts where applicable)
become a bottom tab bar with at least 44px targets; on wide
screens they sit under the top bar. Tables become stacked cards
under roughly 700px. One breakpoint, no framework grid.

Overview, narrow (wide screens put the cards in two columns):

```
+---------------------------------------------------+
| corral  host-1        live 12s ago      [token]  |
| [All] [project-a] [project-b]  >                  |
+---------------------------------------------------+
| Agents: 6 live, 1 stale, 23 finished              |
| Quota  provider-x  ok, read 40s ago               |
|   window-a [========----------] 42% resets 14:00  |
|   window-b [===---------------] 17%               |
| Capacity 6/10 (per repo 3)  a:4 b:2               |
| Resources: 7/10 charged, 5 measured, pressure ok  |
| Drive: running, last pass 3m ago, starts 5/20     |
| Disk: 82G free of 200G (state), 80G free (cache)  |
+---------------------------------------------------+
| project-a (4)                                     |
|  repo-a#42  live   runtime-x  model-y  12m        |
|  repo-a#43  stale  runtime-x  model-y  1h   >     |
| project-b (2) ...                                 |
+---------------------------------------------------+
|    [Overview]     [Board]     [Log]               |
+---------------------------------------------------+
```

Agent detail and disconnected state share one skeleton; the
banner replaces the top bar's freshness text whenever polls
fail, and stale data stays visible under it, greyed, with its
age still counting. A page that shows old numbers as current is
the one failure this UI must never have.

```
+---------------------------------------------------+
| < repo-a#42                     live 4s ago      |
| runtime-x model-y worker, started 12m ago         |
| note: (text)                                      |
+---------------------------------------------------+
| [tail: pause] [follow: on]                        |
| 12:01 reading issue ...                           |
| 12:02 running tests ...                           |
| (one text pane, raw lines, no markup)             |
+---------------------------------------------------+

+---------------------------------------------------+
| ! Cannot reach the API. Showing data from 3m ago. |
|   [retry now]        (polls back off to 60s)      |
+---------------------------------------------------+
```

Board, log, hosts and the token screen, same skeleton, narrow:

```
+---------------------------------------------------+
| Board   repo [______] state [open v] label [____] |
| repo-a#42  Title text                 [tracker >] |
|   open, priority:high, agent-1 on host-1 (e3)     |
|   fetched 2m ago, confirmed, fresh                |
| repo-a#43  ...                                    |
| [ < prev ]            1-50 of 137        [next > ]|
+---------------------------------------------------+

+---------------------------------------------------+
| Log  [All projects... choose one]                 |
| Needs attention (2)                               |
|  held-merge repo-a#42: waiting on checks, 3h      |
| Journal  [note] [action] [done] ...               |
|  12:04 note agent-1: text ...                     |
|  [ load older ]                                   |
+---------------------------------------------------+

+---------------------------------------------------+
| Hosts (hub)          hub time 12:05, epoch 4      |
| host-1  reachable, seen 20s ago   6 agents        |
| host-2  unreachable, seen 2h ago  (last pushed)   |
| host-3  registered, nothing pushed yet            |
+---------------------------------------------------+

+---------------------------------------------------+
| corral dashboard, host mode (api 1)               |
| Paste the API token for this server.              |
| It lives in the host's token file; mint one with  |
|   corral serve --new-token                        |
| [________________________]  [ save ]              |
| Wrong token: this server answered 401.            |
+---------------------------------------------------+
```

## 3. Serving: package data, one asset table, two processes

Static files live at `src/corral/web/` inside the Python package
and ship the way `prompts/`, `templates/`, `hooks/` and
`system_deps.toml` already ship: read through
`importlib.resources.files("corral")`, the pattern in `deps.py`,
`prompts.py` and `init.py`. No packaging change is expected
(`uv_build` includes package data it finds in the package tree);
the first implementation ticket verifies a built wheel actually
contains the files (section 8), since that fact is load-bearing
and cheap to check. The file set is deliberately small and flat:

| File | What |
| --- | --- |
| `index.html` | the shell: landmarks, one stylesheet link, one module script, no inline anything |
| `app.css` | all styling, CSS variables for light and dark |
| `app.js` | entry module: routing, view mounting, poll loop |
| `api.js` | fetch, auth header, mode adapter (host shape and hub shape into one view model); no DOM |
| `fmt.js` | pure formatters: ages, bytes, percents, text cleaning; no DOM |
| `views.js` | the view renderers; DOM building only through `textContent` |

Asset resolution is one fact in one place: a new small module,
`src/corral/webui.py`, owns the asset table (URL path to bytes,
content type and a strong ETag computed once as a truncated
SHA-256 of the bytes), the document route's content, and the
security header values of section 5. It is a pure core: functions
over plain data, no handler imports, so both `serve.py`'s
`_Handler` and `hub.py`'s `HubHandler` call it and neither can
drift. Loading bytes through `importlib.resources` happens once
at server construction behind an injectable seam (tests pass a
dict), following `serve.py`'s injected-reader pattern; `hub.py`
today injects only its database path, clock and TTLs, and gains
this one seam the same way.

Routes, added identically to both processes, GET only:

- `GET /` returns `index.html`
  (`text/html; charset=utf-8`, `Cache-Control: no-cache`, ETag).
- `GET /ui/<file>` returns the named asset for the five other
  files (exact content types, `Cache-Control: no-cache`, ETag).
  `no-cache` means revalidate-on-load: an upgrade reaches a
  device on its next reload with only 304s when nothing changed,
  and no hashed filenames or manifest are needed. Anything under
  `/ui/` that is not in the table is a 404, including traversal
  shapes (`..`, `%2e%2e`, doubled slashes, backslashes): lookup
  is by exact table key after the server has split the path,
  never by joining user input onto a filesystem path.
- `GET /ui/config.json` is generated per process, never cached
  (`Cache-Control: no-store`): exactly `{"mode": "host",
  "api": ...}` or `{"mode": "hub", "api": ...}`, the api number
  read from the same constant the process serves at
  `/v1/version` (`serve.API_VERSION` or `hub.HUB_API`). It
  deliberately carries no schema number: the hub's
  `SCHEMA_VERSION` is its SQLite `PRAGMA user_version`, not the
  snapshot document's `schema_version`, and the two only
  coincide at 2 today. Document schemas are checked where the
  documents are (section 7). This is how identical bytes learn
  which backend they face without probing: no request is spent
  discovering whether `/v1/hosts` exists, and no 404 noise
  lands in server logs. The config carries no data and no
  secrets.

Method and auth precedence on the UI paths is fixed, not left
to fall out of each handler: the asset paths are exempt from
auth entirely (section 4), so on them a GET serves the asset
and any other method answers 405 in host mode (never a 401 for
an asset, even though non-GET data routes check auth first
today); in hub mode they follow the hub's own conventions (405
for a wrong method on a known path, 404 beyond them). Browsers
need no HEAD, and conditional GET covers revalidation.
Everything else keeps today's behavior: unknown paths 404,
error bodies stay minimal JSON. The `listening on HOST:PORT` stdout line is a machine
contract (`05-dashboard-api.md` section 2) and does not change;
the same address in a browser is the UI, and the tickets update
`README.md` to say so. No new verb, no new port, no off switch in
version one: the assets are inert public files from a public
repository, and a `serve.ui` setting would be a knob nobody has
asked for (section 11).

Same-origin is a hard rule. The UI only ever talks to the origin
that served it, using origin-absolute paths (`/ui/...`,
`/v1/...`), so `serve.origins` (CORS) is untouched and the hub
needs no CORS at all (it has none today: `do_OPTIONS` answers
405). A consequence, stated plainly:
single-host versus hub mode is chosen by which address the
operator opens, never by a switch inside the page, and one open
tab can never blend a hub overview with a host tail. When a hub
is configured the operator bookmarks the hub; otherwise the
host. What happens when the API is unreachable or the hub is
down: the page still loads if the serving process is up, polls
fail, and the disconnected state of section 2 applies. If the
serving process itself is down there is no page and nothing to
design. A host that stops pushing to the hub keeps its last
document in hub mode with a growing age, exactly the hub's
existing honest-staleness rule.

## 4. Authentication on arbitrary devices

The two backends authenticate differently today, and the UI must
fit both without inventing a third scheme.

**Host mode.** `corral serve` requires the Bearer [REDACTED] only on
a non-loopback bind: `cmd_serve` sets `token_required=remote`,
`check_bind` refuses a remote bind without `--allow-remote` and a
readable token file (exit 4), the token lives at
`paths.api_token_path()` and is re-read at call time, compared
with `hmac.compare_digest`, and never logged. On loopback no
token is checked even when a file exists: a desktop browser on
the host just works. A tablet can never be loopback to the host,
so **tablet use always means a remote bind plus a token**, with
the bind rules of `05-dashboard-api.md` section 3 unchanged.

**Hub mode.** Every data route requires a registered host's
token (salted-hash storage, constant-time verify in
`verify_token`); routing precedes auth, so unknown paths 404
and wrong methods 405 without one, and `GET /v1/version` is
open. There is no loopback exemption and no read-only class.
This design adds one, because the alternative is a fleet
credential in a browser: a "reader" token minted by
`corral hub add-reader --id <name>` (printed once, same
`secrets.token_hex(32)` mint and salted-hash storage as
`add_host`), accepted by `_reader` for the GET data routes, and
rejected by every POST route, which keeps requiring a host
token bound to the body's `host_id`. The verb spellings are
decided here, parallel to the host verbs: `add-reader`,
`remove-reader` and `rotate-reader-token`, each `--id`, each
exit 4 on an unknown or duplicate id, following the existing
hub table. Reader ids and host ids share one uniqueness space
so a presented token resolves to at most one identity. Storage
is ticket W7's real work and is designed, not left to it: a
`readers` table (`reader_id`, `token_salt`, `token_hash`) lands
as hub database schema version 3 with the same
migrate-forward-or-fail-closed rule `open_db` applies today
(version 2 migrates, anything unknown refuses); export carries
reader ids and never hash material, exactly as it carries
hosts without theirs, and after an import the operator
re-mints each reader with `rotate-reader-token`, the same
recovery posture as hosts after import. This is the one
server-side security change hub mode needs, and it is ticketed
before any hub UI ticket (section 10, decision D1).

**The shell exemption.** In both processes the asset routes of
section 3 (`/`, `/ui/<file>`, `/ui/config.json`) answer without
a token. Without this a browser on a remote bind gets a 401 JSON
body instead of a token-entry form, and there is no way forward:
browsers cannot attach an `Authorization` header to a
navigation (UNVERIFIED as a universal, like point 1 in the
introduction; no counter-example is relied on either way).
What the exemption leaks is the UI's own source from
a public repository and the fact that a dashboard exists on that
address. Every data route keeps its current rule, and the test
plan pins both halves (assets 200 and data 401 without a token,
on a token-required server). HTTP Basic for the shell was
rejected: it is a second credential scheme with a hostile
tablet prompt. A token in the URL query was rejected harder:
`serve.py` runs with request logging on by default
(`make_server(quiet=False)`, and `cmd_serve` never sets it), so
the request line including any query reaches stderr, and the URL
also lands in browser history. The token never appears in a URL,
a hash, or a log.

**Entry.** First load with no stored token and a 401 from the
first data call shows the token screen: one password-type field
(no autocomplete), a save button, and one line of help naming
where the token comes from (the host's token file, minted by
`corral serve --new-token`, which deliberately never prints it,
so the operator reads the file on the host; or the hub reader
token printed once at mint). Paste from a password manager is
the expected path for a 64-hex-char token, UNVERIFIED per
device; safe to learn with the maintainer's own tablet during
the smoke checklist. No QR onboarding: the standard library has
no QR encoder, and a vendored JS decoder plus camera permission
(which needs a secure context, below) is a project of its own.
Validation happens by using the token: host mode calls
`/v1/version` (authed under `token_required`, open on loopback),
hub mode calls `/v1/hosts` (`/v1/version` is open on the hub and
would prove nothing). A wrong token says so and keeps the field.

**Storage.** `localStorage`, one key per origin
(`corral.token`), written only after a successful validation,
with a visible "forget this token" action that removes it and
reloads. The alternatives lose on a tablet: memory-only means
retyping 64 hex characters on every reload, and `sessionStorage`
dies whenever the tablet discards the tab, which mobile browsers
do aggressively (UNVERIFIED per browser; the smoke checklist
notes the observed behavior). A cookie would need a new POST
session endpoint, breaking `serve`'s GET-only rule to buy
`HttpOnly` storage; deferred with the actions phase, where CSRF
design will revisit it anyway. The accepted risk is stated, not
hidden: any script running on the dashboard origin can read
`localStorage`, so the defenses are that the origin runs only
this UI (strict CSP, zero dependencies, `textContent` discipline,
section 5), that the device itself is locked, and that both
tokens are narrow and revocable below. Data payloads are never
stored (memory only, section 6), so storage holds exactly one
secret and nothing else.

**Expiry and rotation.** Neither token expires today and this
design adds no expiry: revocation is rotation. Host mode:
replacing the token file applies immediately (read at call
time) with no restart; the ticket improves nothing here, since
`--new-token` refuses to replace an existing file by design, so
rotation is remove-then-mint on the host, then paste once per
device. Hub mode: `rotate-token` kills the old value on its next
request, per host, and `rotate-reader-token` does the same per
reader. When a stored token is rejected, the UI shows the token
screen over the last good data (marked with its age) and does
**not** auto-clear the stored value: `serve` also answers 401
when the token file becomes unreadable (the reader fails closed
into a rejection), and silently forgetting the token on a
transient host error would cost a re-paste for nothing. Replace
and forget are explicit operator actions.

**What a leaked tablet token costs.** Host token: read access
to the whole host API and nothing else, because `serve`
registers GET only and no request path reaches a write
(`05-dashboard-api.md` section 3). That read access is worth
stating at full strength: fleet state, board rows including full
ticket bodies, tail output and journal text, which may contain
anything an agent printed (section 5). It cannot launch, stop,
claim or edit. Reader token (hub): read access to the merged
view of every host, no writes of any kind, and compromise is
contained by rotating that one reader. A hub *host* token, by
contrast, can claim, renew, release, register and push forged
snapshots as that host; that is precisely why it never belongs
in a browser, and why D1 recommends gating hub mode on reader
tokens instead of accepting a host token in the UI even
temporarily.

**TLS and secure contexts.** Both servers speak plain HTTP and
rely on a private network, the decided transport for the hub
(`08-hub-multihost.md` section 6) and the only sane one here
without a certificate lifecycle corral should not own. The UI
is designed to need no secure-context-only feature: no service
worker, no `crypto.subtle`, no camera, no clipboard API
(selectable text instead), no notifications. `localStorage`,
`fetch` and hash routing work on plain-HTTP LAN origins
(UNVERIFIED in private-browsing modes, which may discard
storage; the token screen returning is the graceful failure).
Loopback is treated as a secure context by current browsers
(UNVERIFIED per browser; the UI needs nothing from it either
way). Recommended deployment, in order: bind `serve` and
`hub serve` to the private overlay address (a private IP
literal; `serve` treats it as remote and applies the token
rules, the hub's `bind_warning` stays quiet for private
addresses), or keep loopback and tunnel. Binding to a plain LAN
address is supported by the same flags but the bearer token
then crosses that LAN in the clear, where anyone on it can take
it and read the fleet until rotation; the README text in the
implementation tickets must say exactly that. TLS termination
by an operator's reverse proxy is possible but unsupported:
the UI assumes it owns its origin root (origin-absolute `/ui/`
and `/v1/` paths), so subpath proxying will not work in version
one, and no forwarded-header handling is added.

## 5. Security

**Content-Security-Policy**, sent as a header on the document
and every asset (a `<meta>` policy arrives only with the HTML
that carries it and cannot express `frame-ancestors`;
UNVERIFIED per browser, and the header is used either way):

```
default-src 'self'; script-src 'self'; style-src 'self';
img-src 'self'; connect-src 'self'; font-src 'self';
object-src 'none'; base-uri 'none'; form-action 'self';
frame-ancestors 'none'
```

No inline script or style anywhere: `index.html` carries only a
stylesheet link and a module script tag, the JS sets no inline
event handlers and no `style` attributes (chart geometry is SVG
attributes, section 1), and there are no webfonts, images from
elsewhere, analytics or CDN references for the policy to
excuse. `connect-src 'self'` makes exfiltration to a third
origin a policy violation even if a future bug tried.
`frame-ancestors 'none'`, plus a legacy `X-Frame-Options: DENY`
for older engines, closes clickjacking; a read-only page has
little to clickjack, but the token field does.

**Other headers**, all new, all additive, owned by `webui.py`
for UI responses and added to JSON responses where noted:
`X-Content-Type-Options: nosniff` on everything both servers
send (today neither server sets it; it matters most for
`/v1/tail`, whose `text/plain` body a browser must never sniff
into HTML when navigated to directly, and for JSON bodies),
`Referrer-Policy: no-referrer` on UI responses (tracker links
out carry nothing), and the cache headers of section 3. Header
additions to existing API responses are additive under the
API's own stability rule, and the tests pin the exact values so
a refactor cannot quietly drop one.

**XSS and untrusted text.** Almost everything the UI displays
is agent- or tracker-originated: ticket titles, notes, evidence,
journal text, tail lines, host and project names. The rule is
absolute: such strings enter the DOM only as text
(`textContent`, `createTextNode`), never as markup, and
`innerHTML` appears nowhere in the shipped JS, a fact the
Python tests assert by inspection (section 8). Links are built
only for board `url` fields whose scheme parses as `http` or
`https`; anything else is text. Hash-route parameters (repo,
number, project) are used as query values through
`encodeURIComponent` and as text, never interpolated into
selectors or markup. One `cleanText` helper in `fmt.js` strips
control characters other than newline and tab, plus the Unicode
bidirectional override range, before display: tail output may
carry terminal escape bytes that are inert in a text node but
visually misleading, and bidi overrides can make one ticket's
label impersonate another. That is presentation hygiene, not a
sanitizer; the text-node rule is the actual boundary.

**Secrets in agent output: the policy.** Agents print command
output, and command output sometimes contains things that look
like secrets. The API already serves tail text, journal text
and ticket bodies to token holders; the UI displays what it is
served and performs **no client-side redaction**, because
redaction in the browser is theater: the bytes are already on
the device, visible in any inspector. Dashboard access is
therefore equivalent, for secrecy purposes, to shell access to
the host's session tails, and tokens go only to operators
already trusted that far. If scrubbing is ever wanted it
belongs server-side in the API, as its own design, where it
would protect every consumer equally; it is listed as deferred
in section 12, not built here. The UI's own secret hygiene:
the token is never put in a URL, hash, title or console
message, `Authorization` is set only on API fetches, and error
banners show server error text (already minimal by API design)
without echoing request details.

**The read-only guarantee** is structural in host mode (GET is
the only method `serve` registers; the UI could not write if it
tried) and credential-shaped in hub mode (the reader token
fails every POST at the hub). The UI itself issues GET only;
the action phase, when designed, gets its own CSRF and
confirmation story per the coordinator's note, and will need
more than this UI's current token story, which section 4
already flags.

## 6. Data flow and performance on small devices

**Polling, not streaming, in version one.** This challenges the
coordinator's recommendation head-on, with reasons. Native
`EventSource` cannot send an `Authorization` header, so on any
authed deployment (which is every tablet deployment) the stream
needs one of: a hand-rolled SSE parser over `fetch` and
`ReadableStream` reimplementing reconnect, `Last-Event-ID`
resume and heartbeat handling (an estimated 120 to 180 lines
plus its own failure states); a token in the stream URL query
(rejected in section 4: logs and history); or cookie sessions
(a new write endpoint, deferred). The hub has no stream at all,
so hub mode would poll anyway and the UI would carry two live-
update mechanisms. Meanwhile the stream's snapshot frames carry
only a `{digest}` (`serve._run_stream`), so the client refetches
the snapshot on every change regardless; the stream saves idle
polls, not data polls. Fleet state changes at human cadence.
Decision D2 records the alternatives; the recommended one is
polling only, with the stream kept for the non-browser
consumers it was built for. The switch trigger mirrors
section 1: if tail-watching latency is a demonstrated
complaint, add the `fetch`-stream client for host mode's
`journal` and `events` topics (whose payloads do ride frames)
without touching the poll paths.

**Cadence and backoff.** Overview data every 5s, agent-detail
tail every 3s, board and log every 15s, all only while the page
is visible (`visibilitychange` pauses timers; returning polls
immediately), and only the mounted view polls. On any failure
the interval backs off 5, 15, 30, 60s capped, with a manual
retry button; success resets it. Two staleness clocks are kept
apart and labelled apart: connection freshness ("live 4s ago",
client receipt time versus client clock) and data freshness
(the API's own `now`, `read_at`, `fetched_at`, `received_at`
fields, shown as the serving host stamped them). The UI never
subtracts a hub timestamp from a host timestamp to fake an age;
per-host ages in hub mode come from `received_at` against the
reply's own hub `now`, and host clock skew stays visible as
data, per `05-dashboard-api.md` section 5.

Timestamp formats are mixed in the APIs and the UI keeps one
parser per format, chosen per field, never sniffed: epoch
seconds as JSON numbers (agent `started_at` and `last_seen`,
quota `read_at` and `resets_at`, board `fetched_at` and
`claim.claimed_at`, drive `last_pass_at`, hub `last_seen` and
`received_at`, every `now`), and ISO 8601 strings (event
`first_seen` and `resolved_at`, journal `at`). Both parsers
treat null as "not reported" and anything unparseable as the
raw value shown verbatim, so a format surprise degrades to
legible text instead of NaN ages.

**Fetching discipline.** Every API fetch uses
`cache: "no-store"`. The snapshot-derived routes send `ETag`
and `Last-Modified` but no `Cache-Control`, so a browser's
heuristic cache could otherwise serve a fresh-looking stale
body (UNVERIFIED per browser; `no-store` is correct either
way); and the generation ETag only yields 304s within the
server's 2s snapshot TTL anyway (each rebuild past the TTL
bumps the generation in `serve._cached_doc`), so a JS-side
conditional-request dance would buy almost nothing. Change
detection compares canonical JSON with the volatile stamps
removed: every rebuild restamps `now` and per-reading
`read_at`, so raw response bytes nearly always differ and
byte comparison would re-render on every idle poll. The UI
strips exactly the server's own volatile set
(`serve.VOLATILE_KEYS`, today `now` and `read_at`) at every
depth, mirroring `serve._stable`, and a source-discipline test
asserts the JS copy names the same set so the two cannot drift
silently. Equal canonical forms skip render entirely, which
keeps scroll position, selection and touch state untouched on
idle polls. Request timeout via `AbortController` (10s), so a
wedged connection cannot stack polls.

**Payload bounds.** A host snapshot is small by construction:
one agent row is a few hundred bytes of JSON, so even a
50-agent fleet polls in the tens of kilobytes; quota, capacity,
disk and drive sections are smaller still. Hub mode multiplies
that by host count, still LAN-trivial at any plausible fleet
size. Neither server compresses; at these sizes on a LAN that
is fine, and gzip is named as deferred rather than built
(section 12). The one real payload hazard is the board: each
row is `asdict(board.Item)` including the full ticket `body`
and an `extra` dict, so a page of 100 (the route default; the
UI asks 50) can carry hundreds of kilobytes of text the list
view never shows. Decision D6 proposes an additive `body=0`
query parameter on `/v1/board` that omits `body` and `extra`
from rows; without it the board view still works and simply
pays the bytes, which the ticket ordering reflects (board
lands after overview, tail and log).

**Memory and DOM bounds.** The tail is one `<pre>` text node
replaced per poll (at most `serve.tail_max`, default 200 lines,
server-clamped), never appended and never split into per-line
nodes, so its memory is flat by construction; "follow" mode
scrolls to the bottom only while the operator is already at
the bottom, and any upward scroll disengages it. The journal
caps rendered entries at 300 nodes ("load older" replaces the
window rather than growing it past the cap). The events stack
renders at most 100 rows with the true unresolved count stated
when capped; its payload is the one unbounded one (the route
returns every unresolved row), accepted because an ever-growing
unresolved queue is itself the incident the view exists to
show, and each row is a few hundred bytes. Board pages are 50
rows. Last-good payloads live in memory only, one per view;
nothing but the token is cached client-side, so a reload
always starts from truth or from an honest empty state, never
from quietly stale storage.

## 7. Updating, versioning and operations

The UI ships inside the package and is versioned by the package
release; there is no separate UI versioning. Updating every
device is: upgrade corral on the host (or hub host), restart
`corral serve` (or `corral hub serve`) as today, reload the
tab. Content-hash ETags with `no-cache` (section 3) make the
reload fetch only what changed and make a half-updated shell
impossible to keep serving: `index.html` revalidates on every
load, and its asset references are unversioned paths whose own
ETags move with their bytes. One caveat is stated rather than
solved: within a single page load mid-upgrade, a browser could
in principle mix a just-fetched `app.js` with an older cached
module import (UNVERIFIED per browser cache behavior for module
graphs; the failure mode is a broken page fixed by one more
reload, and the smoke checklist includes an upgrade-while-open
step to observe it).

A stale open tab against a newer API is covered by the API's
additive-only rule within a major (`05-dashboard-api.md`
section 4): new fields are ignored, old fields keep meaning.
Boot order is fixed: the unauthed `/ui/config.json` first (mode
and api for the shell and the token screen, which can therefore
say which backend is being unlocked before any credential
exists), then, once a token is held or loopback needs none,
`/v1/version` plus data. The version gate has two independent
halves, because the numbers are two different facts. The `api`
major comes from `/v1/version`, compared at each successful
unlock and then once a minute and on becoming visible again:
a different major stops rendering data and shows "this
dashboard is out of date for this server, reload after
upgrading" (never a guess, per the same section). The snapshot
document schema is checked on each document itself: the
top-level `schema_version` in host mode, and per nested host
`snapshot.schema_version` in hub mode, against the compiled-in
expectation (2). A newer document schema proceeds (additive);
an older or missing one renders that host's card as
"unsupported document schema" naming the value, while other
hosts still render. The hub's own `/v1/version`
`schema_version` is its database version (`PRAGMA
user_version`) and the UI never compares it to anything; it
exists for hub-to-host compatibility, which is the hosts'
existing `check_version` concern, not the browser's.

Operations are unchanged: the same verbs under the same
supervision hold the UI up, request counters in `/v1/health`
already count UI requests (every GET counts, assets included,
which makes `requests_total` noisier; accepted, and the health
roll-up itself is unchanged), and a broken asset table must
never take the API down: `webui` load failure at startup
degrades UI routes to the generic 500 body with the cause on
stderr, not a startup refusal, since dashboards are consumers
of the API, not the other way round. That last point is a
deliberate
choice the first ticket pins in a test.

## 8. Testing under the repo's rules

Standard library only, `unittest`, no outside world: every test
binds loopback ephemeral ports or none at all, exactly like
`tests/test_serve.py` and `tests/test_hub_dashboard.py` today,
with `tests/suiteguard.py` behind them. The honest split:

**Tested in Python** (new `tests/test_webui.py`, plus cases in
the existing serve and hub suites):

- Asset table: every expected file loads through
  `importlib.resources`, is non-empty UTF-8, has its exact
  content type, and its ETag equals the content hash; an
  injected empty table drives the degraded-UI choice of
  section 7 (generic 500 on UI routes) without touching the
  API routes.
- Serving, per process: `GET /` and each `/ui/<file>` return
  the table's bytes exactly with the section 5 headers
  (CSP string compared in full, `nosniff`, frame headers,
  cache headers); `If-None-Match` yields 304 with an empty
  body; unknown and traversal paths under `/ui/` are 404;
  non-GET methods on UI paths follow section 3's fixed
  precedence (405 in host mode with or without a token, never
  401; the hub's own 405 and 404 conventions in hub mode);
  `/ui/config.json` returns the right mode and an api number
  equal to what the same process serves at `/v1/version` (an
  agreement test, in the house style of the `TAIL_MAX_DEFAULT`
  pin in `serve.py`), and carries no schema field.
- The shell exemption, per process: with a token required,
  assets and config answer 200 without one while every data
  route answers 401 without one and 200 with one. On the hub:
  a reader token reads `/v1/hosts`, `/v1/snapshot` and
  `GET /v1/claims` and is refused (401) on every POST route (the
  claims list is read-only data of the same sensitivity as the
  board holders the UI already shows, so readers may have it;
  the claim write routes stay host-only), a host token still
  does both, rotating a reader kills only that reader, and a
  retired host token gains nothing from the reader table.
- Contract with the API: for each endpoint the UI consumes,
  one test builds the payload at the production seam with
  fakes (the existing `ServeCase` and hub dashboard fixtures)
  and asserts the fields the UI reads are present with the
  right types and nullability, timestamps included (epoch
  number versus ISO string per the field table in section 6):
  agent rows (the `_agent_row` key set, numeric-or-null
  timestamps), `fleet_header` counts, quota providers keyed by
  name with null-or-object windows, `capacity.by_project`,
  `disk.paths`, drive keys, board rows minus body under
  `body=0`, event rows with ISO `first_seen`, journal entries
  with ISO `at`, and the hub's `{hosts[].snapshot,
  received_at, stale}` envelope with each nested snapshot's
  own `schema_version`. One test additionally pins the journal
  cursor's current decimal-index minting, the accepted
  deviation of section 2, so a reminting breaks here first.
  These are the tests that fail when an API change would
  silently blank or garble a view.
- Source discipline over the shipped files (the Python suite
  reads the package's own web assets): no `innerHTML`, no
  `eval(`, no `document.write` in any JS; no `http://` or
  `https://` literal anywhere (no external reference can
  creep in); no inline `<script>` or `<style>` content and no
  inline event-handler attributes in `index.html`; the only
  storage key written is the token key; the volatile-key set
  in `fmt.js` equals `serve.VOLATILE_KEYS` (imported and
  compared, not restated); and a size budget
  (total shipped HTML+CSS+JS at most 120 KB uncompressed, and
  no single file over 60 KB) that fails the build when the UI
  quietly stops being lightweight. Adding a dependency later
  means arguing with these tests in a PR, which is the point.
- Packaging: the wheel built by the first ticket contains
  `corral/web/*` (checked by listing the built artifact in
  that ticket, not necessarily as a permanent suite test, to
  keep the suite fast; the ticket records the listing).

**Not tested by any runner: the JavaScript logic itself.** That
is the explicit trade-off of rejecting a JS test runner, and it
is rejected on facts, not taste: node appears nowhere in
`system_deps.toml`, `package.json` does not exist, and `make
bootstrap` needs only uv. Adding `node --test` for a few hundred
lines of formatting code would add a system dependency, a
second test entry point beside `make check`, and a version to
pin, to cover the smallest and most stable part of the UI. The
mitigations are structural instead: all decisions that can be
pure are pure and live in DOM-free modules (`api.js` shape
adaptation, `fmt.js`), kept small enough to review line by
line; rendering is a thin, uniform layer over those (build
nodes, set text), so the untested surface is mostly layout;
and the Python contract tests above cover the failure mode
that actually breaks dashboards, which is the API changing
under the UI. If the actions phase or the section 1 switch
trigger ever lands, revisiting a JS runner is part of that
design, with these same files as its first subjects since they
were kept runner-ready (ES modules, no DOM at module scope in
the logic files).

**Manual smoke checklist** (owned by the last ticket, kept as a
short doc under `docs/` or the PR template, run on a real
tablet browser and one desktop browser against a loopback or
private-network server with throwaway state): first load and
token paste; wrong token; forget token; loopback no-token
load; each view in light and dark OS themes; kill the server
mid-view and watch the disconnected banner age honestly while
polls back off 5, 15, 30, 60s; restart the server and watch the
page recover without a reload; serve a snapshot that throws
during rendering (a null resources share) and watch the loop
survive with a could-not-draw notice over aging data; load the
page with the server
down and watch the never-connected banner retry instead of
stranding on "connecting"; a host gone silent in hub mode showing
stale, never dropping; upgrade the server under an open tab
(the section 7 module-mix caveat); 200% text zoom and keyboard-
only navigation; tail follow disengaging on scroll-up; CSP
console clean (zero violations, which also settles the SVG-
attribute chart approach of section 1 empirically). Browser
facts this design could not verify (storage behavior in
private modes, `sessionStorage` tab-discard behavior, module
graph caching, paste flows per tablet browser) are all
UNVERIFIED above and each is settled by one line of this
checklist on the maintainer's own devices, which is the only
place they can be settled truthfully.

## 9. Accessibility and look

Modest, platform-led: the system font stack (no downloads),
`rem` sizing so device text scaling works, a viewport meta for
tablets, and one responsive breakpoint. Light and dark follow
`prefers-color-scheme` through CSS variables only; there is no
in-page theme toggle in version one (section 11). Text contrast
targets WCAG AA (4.5:1 body, 3:1 large) in both schemes, status
is never carried by color alone (every dot has its word beside
it, every bar has its percent), and `tabular-nums` keeps counts
aligned. Touch targets are at least 44px, focus is always
visible (`:focus-visible` outlines never removed), landmarks
are semantic (`header`, `nav`, `main`, lists as lists), the
connection banner is an `aria-live="polite"` region that
announces state changes without announcing every poll, and
`prefers-reduced-motion` disables the few transitions that
exist. The tail pane is plain preformatted text a screen reader
can traverse, and every view works at 200% zoom without
horizontal scrolling except the board table, whose card layout
under the breakpoint covers that case too.

## 10. Rollout: implementation tickets

Ordered, one agent each, each with its own tests and `make
check` green, following the review gate in `AGENTS.md`. No
ticket changes a public interface beyond the additive routes,
flags and verbs it names.

- **W1: static serving and the shell (host mode).** `webui.py`,
  the `src/corral/web/` files with a shell that shows mode,
  version and connection state, `/` and `/ui/*` and
  `/ui/config.json` in `serve.py`, the shell auth exemption,
  section 5 headers (including `nosniff` on all `serve`
  responses), a minimal token entry screen with `localStorage`
  and forget (remote use is impossible without it, so the
  minimum cannot wait for the polish ticket), and the
  `test_webui.py` asset, serving, exemption and source-
  discipline tests. Acceptance: wheel listing shows
  `corral/web/*`; a browser on loopback loads the shell with a
  clean CSP console; with a token required, assets load and
  data calls 401 until the token is pasted; `README.md` gains
  one sentence on the `serve` row saying the same address in a
  browser is the dashboard; `make check` green.
- **W2: overview and polling core (host mode).** `api.js` and
  `fmt.js`; the overview with quota, capacity, resources, disk
  and drive cards, grouped agents, error-section cards,
  project chips, client-side filtering, the poll loop with
  backoff, textual staleness, the disconnected banner, and the
  version check. Acceptance: contract tests for the snapshot
  fields pass, including the `resources` section shape
  (`host_budget`, charged versus measured, `by_project`,
  `pressure`) and a failed-resources error card;
  change detection strips the volatile keys of section 6
  (the VOLATILE_KEYS agreement test passes, and the single
  compare-and-skip path is confirmed by code inspection,
  since no JS runner exists, section 8); degraded sections
  show as error cards against a broken-section fixture;
  `make check` green.
- **W3: agent detail and tail (host mode).** Detail view, tail
  pane with follow and pause, ticket-less agents stated
  plainly. Acceptance: tail renders as a single text node
  (source-discipline test extended if a cheap assertion
  exists); a 404 tail shows "nothing to show" without breaking
  the poll loop; `make check` green.
- **W4: `GET /v1/projects` (server only, decision D5).**
  Additive route in `serve.py` returning exactly
  `{"projects": [<name>, ...]}`, names only, sorted (the order
  `projects.load_all` already returns), behind the same auth
  as every data route. A `ProjectsError` answers the generic
  500 body with the detail on stderr, the board route's
  pattern. Acceptance: route tests for a missing registry dir
  (empty list, not an error), a multi-project registry, and a
  bad project file (500, file named on stderr, never in the
  body); no path, root or policy entry point leaks into the
  reply; `make check` green.
- **W5: log view (host mode).** Events stack and journal
  stack with kind chips, last-window paging and the DOM cap,
  project list upgraded to W4 when available (derived set as
  the stated fallback). Acceptance: contract tests for events
  and journal fields; empty project and unknown project states
  render as text, not exceptions; `make check` green.
- **W6: board view, with `body=0` (decision D6).** The
  additive `/v1/board` parameter omitting `body` and `extra`,
  then the paged, filterable table with scheme-checked tracker
  links. Acceptance: server tests prove `body=0` rows drop
  exactly those keys and the default reply is unchanged byte
  for byte; paging honors `total`; `make check` green.
- **W7: hub reader tokens (server only, decision D1).** The
  `readers` table as hub database schema version 3 (version 2
  migrates forward, anything unknown fails closed, the
  existing `open_db` rule), reader mint, verify, rotate and
  remove beside the host token machinery in `hub.py` and
  `hubcli.py` under the verb spellings fixed in section 4, one
  id namespace across hosts and readers, readers accepted by
  `_reader` on the read routes only (`/v1/hosts`,
  `/v1/snapshot` and the existing `GET /v1/claims`, whose
  handler today checks `_authed_host` and must move to
  `_reader`), and export and import extended to carry
  reader ids without hash material. Acceptance: a version 2
  database migrates with hosts, claims and snapshots intact
  and an unknown version still refuses; the reader test matrix
  of section 8 (all three reads pass, every POST refused,
  rotation is per-reader, a duplicate id across either table
  is exit 4);
  export then import preserves reader ids, carries no hash
  material, and old exports without readers still import;
  `README.md` hub rows updated for the new verbs; exit codes
  follow the existing hub table; `make check` green.
- **W8: hub mode.** The section 3 routes in `hub.py` (assets,
  config with `mode: hub`, `nosniff` on hub responses), the
  `api.js` hub adapter, per-host overview with registered-but-
  silent hosts joined from `/v1/hosts`, the hosts view, and
  host-only tabs absent with honest deep-link messages.
  Acceptance: the hub serving and exemption tests mirror W1's;
  a three-host fixture (one fresh, one stale, one registered
  but never pushed) renders all three states distinctly,
  including a per-host unsupported-document-schema card against
  a doctored nested snapshot; `README.md` hub serve row gains
  the browser sentence; `make check` green.
- **W9: polish, accessibility pass and the smoke checklist.**
  Contrast and focus audit, reduced-motion, 200% zoom fixes,
  the section 8 checklist written up and run on real devices,
  UNVERIFIED items replaced by observed results in that doc.
  Acceptance: checklist fully run with results recorded; any
  defect over the in-place fix line filed as an issue per the
  review gate instead of fixed silently; `make check` green.

The smallest useful thing is W1 plus W2: a token-gated page
that shows the fleet, quota, capacity and resources for one
host. If the
maintainer wants eyes on it before anything else is built,
that is the stopping point to judge.

## 11. What I would cut or change, and why

Each entry names whose text it challenges and what it saves.
Real choices among them are repeated as decisions in
section 13; the rest are decided here and the maintainer can
overrule any of them with a word.

- **Live stream over polling (coordinator recommendation):
  cut for version one.** Native `EventSource` cannot carry the
  bearer token, the hub has no stream, and snapshot frames
  carry only a digest that forces a refetch anyway. Saves an
  estimated 120 to 180 lines of hand-rolled SSE client code,
  its reconnect state machine, and a second update mechanism
  in hub mode. Polling at 5s on a LAN is indistinguishable for
  fleet state. (Section 6, decision D2.)
- **Hub mode in the first release (maintainer requirement 3
  ordering): sequenced last, gated on reader tokens.** The
  design covers the hub fully, but shipping hub mode before a
  read-only hub credential exists means a write-capable host
  token in a browser. Saves nothing in total work; it buys
  safety order: W1 to W6 prove the UI on one host, W7 makes
  the hub credential safe, W8 reuses everything. The hostile
  alternative, accepting a host token "temporarily", is named
  in D1 only so it can be rejected on the record.
- **Sparkline charts (coordinator recommendation): cut.**
  No endpoint serves history; quota and capacity are current
  values. A sparkline would need invented client-side
  retention with its own staleness lies. Bars and gauges from
  current values only. Saves a data-retention design nobody
  asked for.
- **A separate project view screen (ticket scope wording):
  merged into the filter.** The overview, board and log with a
  project chip selected *are* the project view; a fourth
  screen would duplicate all three assemblies. Saves one
  view, its route and its tests.
- **Per-agent resources (ticket scope wording): not shown.**
  The snapshot does carry a `resources` section (host budget,
  charged versus measured totals, per-project shares and a
  per-agent table, and `ERROR_SECTIONS` lists it sixth), and
  the overview shows it as one card (section 2), because that
  card is the number that explains why a launch was refused.
  The per-agent rows stay out of version one: on a tablet they
  duplicate the agent list with numbers only an operator
  debugging a refusal needs, and the CLI already reports them.
  Saves a table, its tests and its bytes on every poll.
- **A hub claims view (ticket scope): deferred to version two.**
  `GET /v1/claims` already lists every claim row, and what
  operators need day to day (who holds what) is already
  visible as agent rows and board claim holders. No new route
  is needed; the only work is the UI list itself.
  (Decision D4.)
- **Board ticket bodies on screen: never in version one, and
  slimmed on the wire.** Bodies are long, secret-bearing and
  unreadable on a tablet list; the tracker link is one tap
  away. Rendering them would also pressure the
  text-only rule. `body=0` (D6) stops paying their bytes on
  every poll. Deep board work, like full journal search,
  stays better in the CLI and the tracker, and this UI does
  not pretend otherwise.
- **Cookie sessions, token expiry, QR onboarding: cut.**
  Each needs server machinery (a write endpoint, a clock
  policy, a QR codec) to improve on paste-once, which works.
  Revisit only with the actions phase (cookies) or field
  complaints (the rest).
- **PWA install, offline mode, service worker: cut.** A
  service worker needs a secure context (UNVERIFIED per
  browser) the plain-HTTP deployment does not have on a LAN
  address, and offline fleet data is stale
  data with better marketing. The disconnected banner over
  last-good in-memory data is the honest version of the same
  feature.
- **Theme toggle and a settings screen: cut.** The OS already
  carries the preference; `prefers-color-scheme` is one CSS
  block. No other setting exists to put on a settings screen:
  cadences and caps are constants with the numbers argued in
  section 6.
- **An off switch for the UI (`serve.ui`): not built.** The
  assets are public source served same-origin; the data
  routes are unchanged. A setting, its validation and its
  tests would guard a door that opens onto the repository
  itself. If an operator ever needs it, it is one additive
  boolean later.
- **gzip on either server: deferred.** Real benefit, wrong
  first move at LAN payload sizes (section 6). It is a
  cross-cutting server change that deserves its own numbers,
  not a rider on a UI ticket.

## 12. Not building in version one

Write actions of any kind (release, pause, stop, launch, ack);
their design owns CSRF, confirmation and probably cookie
sessions. Server-side secret scrubbing of tail, journal and
bodies. The SSE client (D2's switch trigger names when).
Hub-proxied tails, boards or journals. Search across journals
or board bodies. History, trends and sparklines. Push
notifications. Multi-user anything: no accounts, no per-user
views, no audit log of reads. Internationalization. A
component framework (section 1's trigger names when). TLS in
corral. Subpath or reverse-proxy deployments.

## 13. Open decisions

Decided above (no options; overrule by word): vanilla ES
modules with Preact + htm as the named successor; hash
routing; same-origin serving from both existing processes;
the unauthenticated shell over authed data; no QR, no PWA, no
cookies, no expiry, no theme toggle, no UI off switch; ticket
bodies never rendered. Six choices remain genuinely the
maintainer's:

**D1: the hub browser credential.** Hub mode needs a token a
tablet may hold. A host token can write claims as that host
(section 4); a reader token class is one server ticket (W7).

Options:
1. Reader tokens first, hub UI after (recommended).
2. Host token in the browser, reader tokens later.
3. No hub mode; per-host dashboards only.

**D2: live updates.** Polling covers every fact; the stream
cannot take a bearer token from a native `EventSource`
(section 6).

Options:
1. Polling only in version one (recommended).
2. Polling plus a fetch-stream SSE client, host mode.
3. Cookie session endpoint, then native EventSource.

**D3: token storage in the browser.** Persistence against
exposure on a shared or lost tablet (section 4).

Options:
1. localStorage until forgotten (recommended).
2. sessionStorage; re-paste per browser session.
3. Memory only; re-paste per page load.

**D4: a hub claims view in the UI.** `GET /v1/claims`
already exists and W7 admits reader tokens to it (section 8),
so no route work remains; the question is only whether the
Hosts view gains a read-only claims list in version two.
Agent rows and board holders cover the daily question
(section 2).

Options:
1. Defer; no claims view in version one.
2. Read-only claims list on the Hosts view in version two (recommended).

**D5: a host projects route.** Without it, projects with no
current activity are invisible to the log view (section 2).

Options:
1. Add GET /v1/projects, names only (recommended).
2. Derive projects from activity only, forever.

**D6: slimming board rows.** Board payloads include full
ticket bodies the list never renders (section 6).

Options:
1. Add body=0 to /v1/board (recommended).
2. Accept the payload; no API change.

