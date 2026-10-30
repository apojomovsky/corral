# 07: the board-field tracker variant

Status: design, awaiting maintainer approval. No code ships with this doc.

Some projects coordinate through hosted project board fields (a Projects v2
single-select, number and text fields, and item relationships such as
blocked-by, parent and sub-issue edges) that cannot be flattened into labels
without losing state. This doc designs a tracker variant that reads and
writes those fields alongside the label-based tracker, behind the existing
tracker interface. Projects that need it can onboard; projects that do not
never see it.

It decides the eight points the ticket lists, with reasoning. Where it
changes a public surface (a setting, a protocol type, a `TrackerItem`
field) it says so; implementation waits for approval.

## 0. Scope and non-goals

The slice adds: a second GitHub tracker adapter (Projects v2 over GraphQL,
issues and sub-issues still over REST), carriage of board concepts through
the store's `extra` fields into `TicketRow`, board-aware sync and mirror
paths, per-repo board configuration, and a doctor parity check. No new CLI
verb ships, and no exit code changes meaning.

Non-goals: a second provider (GitHub only, per `00-design.md` section 2; a
later provider is a new adapter), project eligibility policy (projects keep
it; corral only supplies richer rows), merging and closing (a maintainer
merges; the loop never closes), and anything the loop deletes.

## 1. The model: concepts, `TicketRow`, and who owns what

Decision: corral code knows a fixed set of canonical concept keys and
nothing project-specific. All project naming lives in the project's own
`corral.toml` (section 5). Concepts travel in the store's existing `extra`
map (`board.Item.extra`, already persisted as JSON) and reach eligibility
through one new `TicketRow.extra` mapping, defaulting to empty so
label-only rows are unchanged.

| Concept | Carries | Owner |
| --- | --- | --- |
| `agent` | claim holder id | corral (outbox) |
| `status` | in-flight state | corral (outbox) |
| `board_status` | workflow state column | people (read-only) |
| `priority` | triage rank | people (read-only) |
| `area` | area or component | people (read-only) |
| `milestone` | milestone title | people (read-only) |
| `pool` | which pool may take the ticket | people (read-only) |
| `blocked_by` | list of `owner/repo#N` | people (read-only) |
| `parent`, `children` | sub-issue edges as `owner/repo#N` lists | people (read-only) |

`milestone` reads from the REST issue payload, not from a board field, so
no board mapping is needed for it. `parent` and `children` read from the
sub-issues REST endpoints through the same single `gh` seam; `blocked_by`
reads from one optional people-owned text field holding comma-separated
`owner/repo#N` refs, parsed strictly: an unparseable entry is ignored and
reported as a sync error line, never guessed.

Ownership follows `00-design.md` field ownership with no new rule:

- People win people-owned fields. A tracker reading always overwrites the
  stored value. These concepts are never enqueued, and the adapter offers
  no write path for them (no method, not a refused one).
- corral wins `agent` and `status`. A reading only settles queued writes,
  with the exact label semantics already in `board.py`: a fresher reading
  echoing a queued value marks it sent, a fresher different value drops it
  as `overridden`, an empty value settles nothing, and a stale reading
  (older than the queued write) settles nothing. A person typing into the
  corral-owned board text fields is therefore handled like a person
  editing a label: their edit either confirms the queue or loses to a
  newer queued write, and the drop is logged.

`TicketRow` gains `extra: Mapping[str, object]` carrying these concepts.
Precedence between the two sources of rank is resolved in exactly one
place, a pure helper owned by the board slice: board `priority` wins when
present, else the label-derived priority stands. Eligibility keeps reading
one rank; it never branches on variant.

Relationship cycles (A blocked by B blocked by A) are project policy, not
corral logic: the store carries the lists verbatim and eligibility, a pure
function over rows, decides what a cycle means for that project.

Why not flatten board state into labels on read: the adapter would then
write project meaning into the shared label namespace (creating labels
like `area:gpu` on repos the project does not own, against R5 in
`01-onboarding.md`), and option renames would churn labels. Carrying
concepts in `extra` keeps the tracker untouched and the mapping in one
project-owned file.

## 2. The adapter: GraphQL Projects v2 behind the single `gh` seam

Decision: a second adapter module next to `github/tracker.py` with the
same constructor shape (`run(args)` injected, fakes at that seam only). It
calls `gh api graphql` through `corral.github.gh.graphql` for board reads
and mutations, and the existing REST paths for issues, sub-issues and
milestones. Nothing outside the two adapters calls `gh` or knows a URL or
query shape, per the AGENTS.md rule. This changes no seam: it reuses the
one `gh` seam and the machine-wide `GraphqlPacer`.

Cost per read, budgeted up front: one page of up to 100 items with all
mapped field values costs one GraphQL read call; field and option
definitions cost one call per board and are cached (below); one mutation
per field write at mutation cost. A full board poll of N items is about
`ceil(N/100)` read calls plus schema refreshes. A per-pass page cap
(`board.poll_pages`, default small) bounds one pass; the poll resumes
where it stopped on the next pass.

Batching and pagination: the item query pages with `first: 100` and the
`after` cursor until a short page or an empty `pageInfo` ends it. A missing
or malformed page is a `TrackerError`, never a partial result, for the
same reason the REST feed refuses truncation: a cut feed would read absent
items as still present.

The budget is shared with other fleets on the host and can be exhausted.
Reads therefore degrade instead of blocking dispatch: when the pacer
reports a live cooldown or a wait past the cap, the board sync step
records a board-outage line, keeps its cursor, and the pass continues on
store data. Only PR creation and merges wait. Freshness-gated paths keep
the `04-dispatch-loop.md` section 4 rule unchanged: a claim or
merge-precondition check past the staleness bound still attempts a live
read, and a live check that fails for any reason skips the claim instead
of using the stale row.

The pacing shim needs no change: `graphql()` and `paced_run()` charge the
shared bucket upstream and mark the child with `PACED_MARKER`, so the
`gh` on agents' PATH logs the call without charging twice.

Caching and staleness: board schema (project id, field ids, option ids and
names) is cached in a small store table with a `fetched_at` stamp, under a
`board.schema_max_age_s` setting; past the bound the next board step
refreshes it live or degrades per the budget rule above. Item freshness
reuses the existing `fetched_at` and audit ordering. Option matching is by
option id, never by name, so renames are free and option-name maps are
validated against ids at refresh time.

## 3. Sync: feed, board poll, cursors, and silent field edits

Decision: two cursors per repo in the existing cursor table, the `tracker`
cursor already kept by `sync.py` plus a `board` cursor naming the newest
project-item timestamp applied. Three readers keep rows fresh, cheapest
first:

1. The REST change feed (`changes`, cursor `tracker`), exactly as today,
   for issue-level edits.
2. A board poll: project items ordered by their board timestamp, paginated
   per section 2, advancing the `board` cursor to the newest item applied.
   This is what catches board-field edits, which do not always bump the
   issue `updated_at` and are therefore invisible to the feed.
3. The rolling audit (`audit_repo`, oldest `fetched_at` first), extended to
   compare board fields and not just issue payloads, so a row nobody
   touches still gets its fields verified.
4. A full resync, the last resort, unchanged in shape: it walks every item
   and every board page, marks absent rows dirty, and stays the only path
   that adopts claims.

Cursor semantics match the existing contract: a cursor names an instant in
the existing ISO wire shape, never moves backwards, and a failed step
keeps the old cursor with an error line. Detection is field-level: each
fetched item is compared concept by concept against the stored `extra`,
and any difference upserts through the same ownership rules as section 1.

`TrackerItem` gains `extra: dict` with a default of empty, carrying only
canonical concept values, never raw board shapes; `sync._reading` copies
it into `board.Item.extra`, which the store already persists. This is a
public interface change to the tracker protocol, named here: fake
trackers in existing tests keep working through the default.

Why not poll the board only and drop the feed: the feed is cheaper per
pass for issue churn and already handles cursors, adoption and absence.
Why not audit only: the audit is bounded per pass by design and would let
a hot board field lag for passes. The poll is the timely path; the audit
is the backstop.

## 4. Writes: the same outbox, the same race rules

Decision: board writes go through the existing mirror and outbox with no
new queue. `Store.enqueue` keeps accepting only `agent` and `status`; the
per-adapter translation (concept to label set in label mode, concept to
field id in board mode) happens once, at mirror time, inside the adapter's
`write`. One translation point per adapter, no second resolver.

Idempotence and lost races are identical to label writes because they are
the same code path: the mirror groups queued rows per item and keeps only
the last value per field, so writing the same value twice leaves one
write; a superseded row is marked `dropped` with reason `superseded`.
External and closed items drop the same way (`external`, `closed`), and a
rate-limit answer halts the drain with `TrackerRateLimited`, leaving rows
queued for the next pass.

Two board-specific cases:

- An option disappears or is renamed on the board. Reads match by option
  id: a stored id with no live option keeps its stored value, logs a sync
  error line, and is flagged by the doctor check (section 5) instead of
  being mapped to a lookalike. A queued corral-owned write whose field or
  option id is gone is dropped with reason `unknown-option` plus an error
  line: corral never invents a mapping. Renames need no handling at all,
  since names are display only.
- A missing board field at mirror time (deleted or renamed upstream) is a
  configuration error surfaced once per pass, and the rows stay queued;
  the doctor names the repo and the field.

## 5. Configuration: the project declares its board

Decision: one per-repo setting picks the variant, and one project-owned
table maps names to concepts. Scalars go in the settings registry;
name maps are TOML tables parsed once by the board slice, with unknown
concept keys refused as `SettingsError` (exit 4).

```toml
[[repo]]
tracker.repo = "example/repo"
tracker.variant = "board"   # "labels" (default) or "board"

[repo.board]
project_owner = "example"   # board owner login
project_number = 3          # board number; node id accepted instead
agent_field = "Agent"       # corral-owned text field for the agent id
status_field = "Agent state" # corral-owned text field for in-flight status
status_column = "Status"    # people-owned workflow column (optional)
priority_column = "Priority"
area_column = "Area"
pool_column = "Pool"
blocked_by_column = "Blocked by"

[repo.board.priority_map]   # option name to corral rank; ids resolved live
"P0" = "urgent"
"P1" = "high"
"P2" = "normal"
"P3" = "low"
```

Rules: corral code names concept keys only, never a project's field or
option names; those live in the project's file. Boards are looked up by
owner plus number (names alone are ambiguous across owners; a node id is
accepted for renames). Scalar board settings in the registry:
`tracker.variant` (default `"labels"`), `board.poll_pages`,
`board.schema_max_age_s`, `board.item_max_age_s` for freshness-gated
reads. The doctor gains one board check per repo in board mode: the board
resolves, every mapped field and every mapped option id exists, and no
queued writes predate a variant flip. A flip with a non-empty outbox is
refused as a configuration error so no write crosses variants.

## 6. Coexistence, migration, and exit codes

Decision: one repo uses exactly one variant at a time, so two writers
never race on one item. Two repos under one ops root may use different
variants, since the setting is per `[[repo]]`. The loop's label-based
decision writes (`needs-decision` and the loop's own comment) stay as
they are in both modes: they address people on the tracker, where people
read them.

Migration of a label-only project, in order: drain the outbox through the
mirror until empty; the operator backfills the board fields from the
labels on the board itself (a people action, reviewable there); flip
`tracker.variant`; resync; the doctor parity check compares label-derived
and board-derived concepts per item and reports mismatches. Rollback is
the same steps in reverse: flip back and resync, after which the mirror
re-pushes corral-owned values from the store. No new verb ships for any
of this; the variant flip is a config edit plus existing sync and doctor
verbs.

Exit codes are unchanged (AGENTS.md fixes their meaning):

| Situation | Exit |
| --- | --- |
| Board poll degraded to store data, pass otherwise complete | 0, with notes |
| Pass synced nothing at all (tracker and board both unreachable) | 3 |
| Lost claim race on a board-backed row | 2, same `Claimed` path |
| Bad board config, unknown field or option, flip with queued writes | 4 |
| Host full or quota refused under board mode | 75, same gating |

## 7. Test plan, fakes at the single `gh` seam only

No test touches the outside world per AGENTS.md. Every behavior below is
tested through the injected `run(args)` seam (canned GraphQL and REST
payloads) plus a fake pacer clock, with a temporary store:

| Behavior | Seam (fake) |
| --- | --- |
| Issue parse plus board concepts land in `extra`; unknown option keeps stored value with an error line | fake `run` payloads |
| Board poll pages with `after`, stops on short page; malformed page is `TrackerError`, never partial | fake `run` pages |
| Board-only edit (issue `updated_at` unchanged) detected by poll and audit | fake `run`, temp store with aged row |
| Cursor pair advances independently; failed board step keeps old cursor | fake `run` raising once |
| `enqueue` refuses people-owned concepts; adapter has no write path for them | temp store |
| Corral-owned write round-trips through one text field per concept; repeat write leaves one label-free mutation | fake `run` recording mutations |
| Fresher person edit drops queued write as `overridden`; newer queue overwrites person edit | temp store plus fake readings |
| Missing field or option id drops with `unknown-option` and an error line; rename by id is free | fake schema payloads |
| Rate-limit answer halts the drain, rows stay queued, cooldown honored | fake `run` rate-limit text, fake pacer |
| Budget exhausted: pass continues on store data with a board-outage line; stale-gated claim skips | fake pacer in cooldown, fake `Tracker` |
| Schema cache refresh past `schema_max_age_s`, else reused without a call | fake `run` call counts |
| Variant flip with queued writes refused; doctor parity flags mismatches | temp store plus outbox rows |
| Resync adopts board-shown claims, reports store-wins conflicts identically | fake `run` full walk |

## 8. Implementation tickets, in order

Each is sized for one agent, with acceptance criteria.

1. Store carriage for board concepts. `TrackerItem.extra` with a default,
   `sync._reading` copies it, precedence helper for priority in one place.
   Criteria: existing fake trackers compile untouched; board-concept rows
   persist and read back through `extra`.
2. Board settings and validation. Registry scalars plus the `[repo.board]`
   table parser with strict keys. Criteria: unknown concept key is exit 4
   naming repo and key; defaults leave label mode byte-identical.
3. GraphQL board reader. Adapter read path: schema fetch plus paged items
   with `after`, strict page shapes, option ids resolved live. Criteria:
   the section 7 read rows pass against canned payloads; malformed page
   fails closed.
4. Board sync step. `board` cursor, poll plus extended audit, field-level
   compare, board-only-edit detection. Criteria: the section 7 sync rows
   pass; cursors move independently and never backwards.
5. Board mirror writes. Concept-to-field translation in the adapter's
   `write`, text-field round-trip, `unknown-option` drop, halt on rate
   limit. Criteria: the section 7 write rows pass; label-mode writes
   unchanged.
6. Ownership settlement for board readings. Echo, overridden, empty and
   stale cases through `upsert` for board-sourced values. Criteria: the
   section 7 race rows pass with identical verdicts to label mode.
7. Doctor board check and migration path. Schema resolution, option-id
   validation, flip-with-queue refusal, parity report. Criteria: each
   failure names repo and field; parity output is stable text.
8. Docs follow-up. `TicketRow.extra` wired into eligibility rows and the
   drive's freshness-gated re-reads using the board reader. Criteria: a
   stale-gated claim re-reads live through the board adapter and skips
   when unreachable, per section 2.
