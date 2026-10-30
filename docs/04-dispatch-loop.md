# 04: the dispatch loop (`corral drive`)

Status: design, awaiting maintainer approval. No code ships with this doc.

The loop is the part of corral that keeps a fleet busy with no human
typing `corral launch`. Each tick it syncs the board from the tracker,
asks every registered project which tickets are eligible, consults
admission and the quota gate, claims, and starts workers and reviewers
through the existing launch path. Projects keep their own eligibility
rules; corral owns the loop, ordering across projects, caps, backoff
and idempotence.

This doc decides the ten points the ticket lists, with reasoning. Where
it changes a public surface (a verb, a setting, a protocol method) it
says so; implementation waits for approval.

## 0. Scope and non-goals

`corral drive` runs one pass over all registered projects on this host:
sync, mirror backlog, eligibility, gating, claims, starts, stuck-agent
handling, bookkeeping. One pass, then exit. A timer repeats it.

Non-goals: merging (a maintainer merges), triage policy (projects own
it), provider-specific pacing (a later step per `02-accounts-quota.md`),
multi-host arbitration (section 8 leaves hooks only), and anything the
loop deletes (section 7: the loop deletes nothing).

## 1. Process model: a timer running a one-shot pass

Decision: no daemon. `corral drive` runs one pass and exits. A
`systemd --user` timer wakes it every `drive.period` (default 5
minutes). The timer unit ships with corral and is enabled by the
operator; installing or enabling host units needs a maintainer's
go-ahead per AGENTS.md, so the implementation ticket files that as a
separate step.

Why not a long-lived daemon:

- A pass holds no memory state worth keeping. Every fact it needs
  lives in durable stores already: the board store, the outbox, the
  quota ledger, the registry, the day counter. A killed pass loses
  nothing; the next tick re-reads and continues.
- Crash safety falls out of idempotence instead of recovery code. Each
  step is a transaction or a guarded write (sections 3 and 4), so a
  pass cut at any point is safe to re-run.
- There is nothing to supervise. A daemon needs restarts, log rotation
  and liveness checks for no gain at a five minute cadence.

Single instance: one non-blocking `flock` on `<state>/drive.lock`,
taken at startup, released at exit. A second pass finding the lock
held exits 2 ("already running"), the existing meaning of that code.
The lock guards only the pass, not the stores: every store keeps its
own locking, and read-only verbs (`snapshot`, `drive --plan`) never
take the drive lock.

What survives a crash or reboot: everything durable listed above, plus
the timer itself (systemd persistent timers catch up one missed tick).
What does not survive: the in-memory candidate list, which is rebuilt
from the store each pass by construction.

## 2. The eligibility hook: a small Python entry point over plain data

Decision: each project names an entry point in its registration file:

```toml
name = "blue"
unit_prefix = "blue-agent"
root = "/srv/ops/blue_ops"
eligibility = "blue_policy:eligible"
```

On each pass corral imports the entry point once per project and calls:

```python
def eligible(rows: Sequence[TicketRow], *, now: float) -> list[StartRequest]: ...
```

`TicketRow` is frozen plain data built from the board store:
`repo`, `number`, `title`, `labels`, `priority`, `state`, `agent`,
`status`, `external`, and linked pull requests with their check and
review state as data. `StartRequest` names `repo`, `number` and `kind`
(`start`, `resume`, `review`, `fix`) and may carry a project-local
weight used only to order within that project.

The function is pure by contract: no tracker, forge, `gh`, network,
clock or settings access inside. Corral supplies everything as
arguments, including `now`. That keeps project policy testable with
plain rows and keeps every side effect behind corral's seams.

Why not the alternatives:

- A subprocess command adds serialization, version skew between corral
  and the policy, and slow failure modes, for no isolation benefit:
  policy code already runs in-process as a corral library per the
  design doc's contract section.
- Declarative rules in `corral.toml` cannot express real triage
  (labels plus priority plus linked PR state plus history), and every
  new predicate would become a project-specific setting inside corral,
  exactly what the line in section 2 of `00-design.md` forbids.

Open question settled here: the design doc's `Project.wants()` sketch
takes no arguments. It becomes `eligible(rows, *, now)` as above; a
project with no `eligibility` entry registers no candidates and the
drive skips it with a logbook line. Loading failures are configuration
errors (exit 4) naming the project and entry point, reported once per
pass, and they block only that project.

## 3. Ordering and fairness; caps, quota and hold; what exit 75 does

Each pass builds one candidate list: every project's requests,
annotated with board facts (priority, `tracker_updated_at` for wait
time, current agent and unit liveness). Global order is:

1. Required-kind first: `resume` and `fix` for already-claimed tickets
   before new `start` claims, so in-flight work is not starved by new
   tickets.
2. Then priority rank from the board `priority` field, higher first.
3. Then longest waiting (`tracker_updated_at` oldest first).
4. Then `(project, repo, number)` for determinism.

Fairness across projects comes from admission, not the sort: with the
total full, a project below its floor is still admitted (`admission`
section: the floor rule), which is the whole fairness mechanism. No
second balancer is added. One pass starts at most one agent per ticket
and re-reads live state before every start, never a cached count.

Per-candidate gate order, cheapest first:

1. Non-open, externally owned (the row's stored `external` field,
   computed at sync time by `board.is_external`; the loop never
   re-derives it), or withdrawn by this pass's eligibility: skip with
   reason.
2. Claim routing by request kind against the row's live holder.
   `resume`, `fix`, `review` and stuck restarts are classified here,
   before any claimed check, so the check below never sees them as
   plain starts:
   - Unclaimed row: `start` and `review` proceed. A `resume` or
     `fix` naming an unclaimed ticket means its holder went away
     between eligibility and gating; the request is reclassified as
     a fresh `start` on the same ticket and branch (the branch is
     reused, nothing is deleted).
   - Claimed row plus `start`: skip ("already claimed by `<agent>`").
   - Claimed row plus `resume`, `fix` or restart: proceed only when
     the holder is a managed agent, meaning an id this loop minted
     (recognized by its `drive-` prefix; every other reader still
     treats the id as opaque, and fencing still uses only
     `Claim.epoch`). Any other holder is a foreign claim (operator
     work adopted by resync, another host's agent, a hand claim):
     skip with reason "foreign claim" and never touch it. No resume,
     no restart, no release.
   - Claimed row plus `review`: skip while the worker still holds
     the claim. The review is scheduled on a later pass, after the
     section 5 completion release frees the row.
3. Hold: `hold.reason` over the ticket and linked PR labels.
   Unreadable labels fail closed (skip, record "unreadable").
   `needs-decision` label also skips: a ticket waiting on a human is
   not started, resumed or reviewed by the loop.
4. Admission (`admission.admit` against live state from
   `launch.admission_state`): project ceiling and repo cap first, then
   the host total with the floor rule.
5. Quota gate over the project's chains against the shared ledger. A
   refused role is a skip for that candidate only; another project's
   chains may route around the exhausted provider.

Exit 75 handling differs by source:

- `TOTAL_FULL` with the project at or above its floor: stop scanning
  new starts entirely, but keep scanning `resume`/`fix` and
  below-floor candidates, which admission may still allow. When none
  remain, end the pass with exit 0 and record "host full".
- `PROJECT_CEILING` or `REPO_CAP`: skip the candidate, keep scanning
  other projects and repos.
- Quota refusal (exit 75 from the launch path): skip the candidate and
  keep scanning; quota is per-project chains, so others may pass.
- `dispatch_now` is never set by the loop: forced starts exist for
  operators, not for routine dispatch.

## 4. Claims, races, outages and orphans

The loop claims through the board store transaction
(`Store.claim(repo, number, agent, host, now)`), then enqueues the
`agent` and `status` writes to the outbox for the mirror pass. This is
the existing rule that claims need zero tracker calls. The agent id is opaque to every reader except debugging, for
example `drive-<host>-<start-seconds>-<owner>--<name>-<number>`
(the repo segment encodes `owner/name` with a double dash so the id
itself contains no `/`; `start-seconds` is a wall-clock timestamp,
not a fencing token). Fencing uses only `Claim.epoch`; the host
field and the epoch are the tokens a future multi-host loop needs.

- Lost race (exit 2, `Claimed`): re-read the row live. If another agent
  now holds it, record "race lost to `<agent>`" and continue with the
  next candidate. Never steal, never retry in the same pass.
- Tracker outage (exit 3 from sync or mirror): abort the sync or mirror
  step only, keep prior rows and the old cursor, and continue the pass
  on store data. Rows within the staleness bound keep their normal
  paths; only PR creation and merges wait. But the staleness rule below
  takes precedence over this outage rule: a claim or merge-precondition
  path that needs a live re-read past the bound must still attempt it,
  and when that live check itself fails, the claim path fails closed
  (skip with reason "tracker unreachable, row stale") instead of
  claiming from the stale row. Local-only bookkeeping (registry and
  unit liveness, hygiene, journal, snapshot) continues through the
  outage. The outage is recorded in the logbook and the pass exit is 0
  with errors noted, unless the pass synced nothing at all, which is
  exit 3. An unreadable store is different: `board.StoreError`
  carries exit 4, and `cmd_sync` already maps it to 4, since a store
  the loop cannot read is a configuration error, not an unreachable
  host. (`snapshot` instead stays exit 0 with a section error
  because it reports across sections and the store is only one of
  them; the loop cannot decide anything without the store, so it
  fails closed with 4. Tracker-unreachable paths keep 3.)
- Stale rows: paths that decide (claim, merge preconditions) re-read
  live past the staleness bound, per the board design. The loop never
  claims from a row older than the bound without a live check, and a
  live check that fails for any reason (including a tracker outage)
  skips the claim rather than using the stale row.

Orphaned claims are recovered, not adopted, by the loop. Adoption from
tracker labels happens only in the full resync path (`sync.resync_repo`
via `Store.adopt_claim`), never in a drive pass. Each pass instead
releases claims that are provably dead: the store names an agent, and
the registry shows no live or stale entry for that agent id, and no
live unit exists for the ticket, and the claim is older than twice
`registry.gone_s`. The loop passes the agent and epoch straight
from the store row into `Store.release(repo, number, agent, epoch,
*, now)`, which releases only on a matching agent plus epoch and
raises `Claimed` otherwise. A `Claimed` there means the row changed
under the pass (re-claimed or adopted elsewhere), so it is no longer
an orphan: skip it with a logbook line, never retry in the same
pass. A successful release clears the holder (`agent`/`host`) and
leaves `epoch` untouched, per the `Store.release` contract; the next
`claim` mints `epoch+1`, which fences a stale holder still acting on
the old epoch. The release writes a logbook line. The worktree and
branch stay untouched: release abandons the schedule, never the work,
so no proof
rule is triggered. Anything less certain (a stale-but-present agent, a
live unit with no heartbeat) is left for the stuck handling in
section 5.

## 5. The ticket lifecycle the loop drives

States and who moves them:

- Open, unclaimed, eligible: the loop claims and starts a worker
  through the existing launch path (worktree, branch, unit, session).
- Branch pushed, PR opened: workers open PRs themselves through the
  forge; the loop does not open PRs. On the next passes the linked-PR
  data on the row shows the PR ready for review.
- PR ready: the loop claims a review slot and starts a reviewer with
  the existing review path. The "reviewer is not the author" check
  stays where it is, at record time: `reviewrecord.write_record`
  refuses author==reviewer, which is the only place both ids are
  known. The loop does no author comparison when scheduling, by
  design: eligibility returns `StartRequest`s, never a reviewer
  pool, and each reviewer id is minted fresh at start time, so there
  is nothing to compare a row against. No `author` field is added to
  `TicketRow` because scheduling never needs it.
- Changes requested: the loop schedules a fix round as `resume` when
  the worker's unit or registry entry is still usable, else a fresh
  `fix` start on the same ticket and branch. Fix rounds count against
  the restart budget below.
- Merge-ready: the loop does nothing. A maintainer merges. The loop
  never merges, never closes, never assigns.

The decision protocol stays human: when a ticket needs a maintainer
(a design question, a takeoff failure needing judgment, repeated fix
failures), the loop posts the question as a comment, adds the
`needs-decision` label, releases its claim, and walks away. Tickets
carrying that label are never started (section 3).

This is an explicit protocol change: neither write can go through
the outbox. `Store.enqueue` refuses anything outside `AGENT_FIELDS`
(`board.py`), and `Tracker.write` covers only the corral-owned
`agent` and `status` fields, while `00-design.md` reserves labels and
comments as people-owned tracker fields. So the loop gains a narrow
decision-write seam on `Tracker`: `comment(repo, number, body)` and
`add_label(repo, number, label)`, implemented once by the GitHub
adapter behind the single `gh` seam. The people-owned rule stands:
the loop writes only the one `needs-decision` label and its own
comment (marked as the loop's), and never edits title, body, other
labels or assignments; the mirror treats those two writes as
corral-originated, never as conflicts to drop. The writes go direct,
not through the outbox, and on `TrackerError` the loop keeps the
claim, logs, and retries on a later pass: it never releases a ticket
it failed to flag for a human.

Finished workers are reconciled before stuck ones. When the row's
holder has a `finished` registry entry (a `done` file, kept for
`RETAIN_S`), the worker is complete: the loop releases the claim
with a logbook line, leaving branch and PR untouched, so a later
pass can schedule review on the unclaimed PR-ready row. Before
releasing, the loop captures the worker's agent id (falling back to
the `finished` registry entry, which carries it for `RETAIN_S`) and
passes it as the reviewer's `--author`: `review record --author` is
required and `reviewrecord` refuses author==reviewer, so the
reviewer start needs the worker id the release just cleared from
the row. Worker completion releases before any reviewer start and
long before merge; merge-ready rows stay unclaimed and the loop
ignores them. A `finished` entry is neither `live` nor `stale` under
`registry.status`, so it already counts as "no live or stale entry"
for the section 4 orphan rule; reconciliation runs first, so a
finished worker always takes this completion path, never the orphan
one.

Stuck agents: each pass compares claimed tickets against registry
liveness and unit liveness. Only managed holders ever reach here;
foreign claims were skipped at gate step 2.

- Heartbeat older than `registry.stale_s` with a live unit: watch once
  more; older than `registry.gone_s` or unit dead: restart (stop the
  unit through the injected stop seam, launch again on the same claim
  and epoch, reusing the identical agent id: `Store.claim` is
  idempotent only for the same agent, and a fresh id on a still-held
  row gets `Claimed`).
- Restart budget: at most 3 automatic restarts per ticket in any
  sliding 24 hour window, counted as timestamps in a small state
  file: a restart is allowed only when fewer than 3 stored timestamps
  are newer than now minus 24 hours, and each pass prunes older ones.
  (The section 7 day cap is date-keyed instead: it bounds starts per
  calendar day, while this window bounds restart rate around an
  incident.) Only stop-plus-launch restarts consume the budget. Fix
  rounds are not restarts: a `resume` that reuses the live unit and
  session never consumes it. Past the budget, give up: post the
  tail of what was observed with `Tracker.comment`, add
  `needs-decision` with `Tracker.add_label` (the decision-write seam
  above; the outbox carries neither), then release the claim. A
  failed decision write keeps the claim for a later pass.

## 6. Observability: logbook, snapshot and `--plan`

Every pass appends one JSON object per decision to a host-level
logbook, `<state>/drive/journal.jsonl` (append-only, same rotation
discipline as the coordinator journal): pass start and end, each start
with ticket and verdict counts, each skip with its reason, each
release, each give-up. Per-ticket comments go to the tracker only on
give-up and on the decision protocol; routine skips stay local.

`corral snapshot` gains a `drive` section, all reads, no new state:
`last_pass_at`, `last_exit`, `lock_held`, `paused` (with reason),
`starts_today`, `starts_remaining`, and the last pass verdict counts
(started, skipped by reason, released, gave up). Snapshot stays exit 0
with section errors on failure, per its contract.

`corral drive --plan` is the dry run. It reads the store and runs
the whole gate order but never syncs and writes nothing: no tracker
calls, no claims, no outbox rows, no starts, no mirror, no journal. It prints one row per candidate with the
verdict and the reason in the same words the logbook would use, plus a
summary (would start N, blocked per gate). Exit 0 on a completed plan,
4 when the store could not be read or on configuration errors
(`board.StoreError` already means 4; there is no exit 3 path because
`--plan` never contacts the tracker). `--plan`
never takes the drive lock.

## 7. Safety: kill switch, start caps, never-start rules, no deletion

- Kill switch: a pause file, `<state>/drive.pause`. When present the
  loop does nothing except report paused (exit 1, "nothing to do"),
  with the file's first line as the reason. `snapshot` shows it. No
  setting roundtrip: an operator creates or deletes one file. A
  `drive.enabled` setting is deliberately not added: two switches would
  disagree, and one fact lives in one place.
- Per-day start cap: `drive.max_starts_per_day` (default 50) with a
  date-keyed counter in the state dir, incremented only by real starts
  (`--plan` excluded, restarts included). At the cap the pass ends
  with exit 1 and a logbook line; resumes of already-running agents
  are not starts and are unaffected.
- Never started: external items, non-open tickets, held tickets
  (including unreadable labels, fail closed), tickets or linked PRs
  carrying the hold label, tickets carrying `needs-decision`, and
  tickets whose eligibility the project withdrew this pass. The loop
  checks these on every pass, not only at first claim: a label added
  mid-flight stops the next resume.
- No deletion: the loop stops units (via the injected stop seam, which
  has no default) and releases claims. It never removes worktrees,
  branches, sessions, images or caches. Cleanup stays with the hygiene
  pass under its proof rules.

## 8. Multi-host: what stays local, what the future needs

Host-local now: the drive lock, the registry, units, the quota ledger,
the day counter, the pause file, and the journal. A pass never assumes
another host sees its state except through the tracker labels the
mirror writes.

The future multi-host loop (per `00-design.md` section 4) needs,
without this doc designing it: claims carrying host plus epoch (kept),
peer-acked claims before launch, the cap split across peers, and one
peer owning the tracker feed duty. The loop leaves those hooks by
recording host and epoch on every claim, leaving the epoch for the
next claim to mint after a release (which fences the stale holder),
and never deciding from a row it has not freshness-checked. When
multi-host lands, the per-pass order in section 3 stays; only the
claim step gains the ack roundtrip.

## 9. Test plan, fakes only at the single seams

No test touches the outside world per AGENTS.md. Each behavior below
is tested through the seam its production code already injects:

| Behavior | Seam (fake) |
| --- | --- |
| Sync, resync, audit fill rows; outage keeps cursor | fake `Tracker` (in-memory items, scripted `TrackerError`) |
| Outbox drain, supersede, external and closed drops, rate-limit halt | fake `Tracker.write`, temp `Store` |
| Claim race: second claimer gets exit 2 path and moves on | temp `Store`, two agent ids |
| Claim routing: `start` on a claimed row skips, `resume`/`fix` on a managed holder proceeds, foreign holder skips, unclaimed `resume` reclassifies | temp `Store`, fake registry entries |
| Finished worker releases before review; `finished` takes the completion path, never the orphan one | temp `Store`, fake registry with a `done` entry, linked-PR fixture |
| Orphan release rule and epoch fencing | temp `Store`, fake registry entries and unit list |
| Orphan release racing a re-claim: `Claimed` skips with a logbook line | temp `Store`, release with a stale epoch after a second claim |
| Stale row with failed freshness re-read skips the claim, local bookkeeping continues | fake `Tracker` raising `TrackerError` on the re-read, temp `Store` with an aged row |
| Admission ordering, floor through a full total, ceiling and repo cap skips | pure `admission.admit` over constructed state |
| Quota refusal skips one project, not the pass | fake ledger readings plus project chains |
| Hold and unreadable labels skip; `needs-decision` skips | fake `read_labels` |
| Ordering: resume before start, priority, wait time, determinism | pure ordering function over constructed rows |
| Restart budget counts stop-plus-launch restarts in a sliding 24h window, foreign holders never restart, give-up comments and releases | fake units stop seam, fake registry, temp state dir with budget timestamps |
| Decision comment and `needs-decision` label through the new seam; a failed write keeps the claim | fake `Tracker` recording `comment`/`add_label` calls, scripted `TrackerError` |
| Reviewer start carries the pre-release worker id as `--author` | temp `Store`, fake registry with a `done` entry, author==reviewer refusal case |
| Day cap stops starts, not resumes; pause file stops all | temp state dir with counter and pause files |
| `--plan` writes nothing | read-only assertion: store and outbox hashes unchanged |
| Snapshot `drive` section on missing or corrupt journal | temp state dir, section-error shape |

New logic stays pure where decisions live (ordering comparator, orphan
predicate, budget counter); subprocesses (`gh`, `systemd-run`,
`systemctl`) and network stay behind the one seam per tool, and the
stop seam keeps its required-argument rule with the guard test
extended to the loop's stop path.

## 10. Implementation tickets, in order

Each is sized for one agent, with acceptance criteria. File owners are
the modules named.

1. Drive skeleton, lock and `--plan`. One pass that takes the lock
   (exit 2 when held), reads the store, prints the candidate table,
   writes nothing. Criteria: `--plan` on a temp store prints verdict
   rows and leaves store and outbox byte-identical; second concurrent
   pass exits 2.
2. Project registration and the eligibility hook. `eligibility` entry
   in `projects.d`, loader, `TicketRow` and `StartRequest` types, pure
   call with rows and `now`. Criteria: stub policies over fixture rows
   return ordered requests; bad entry is exit 4 naming project and entry;
   project without the entry is skipped with a logbook line.
3. Gate order: external, claim routing, hold, admission, quota.
   Criteria: one test per skip reason in section 3; claimed plus
   `start` skips while claimed plus `resume` on a managed holder
   proceeds; foreign holder skips with "foreign claim"; unclaimed
   `resume` reclassifies as a start; floor project admitted at full
   total; ceiling and repo cap skip and continue; quota refusal skips
   one candidate only.
4. Claims and the launch handoff. Criteria: claim then launch through
   fake units; lost race (exit 2) records and continues; tracker
   outage keeps cursor and continues on store data; mirror backlog
   drains on the next healthy pass.
5. Orphan recovery. Criteria: the section 4 dead-claim rule releases
   (holder cleared, epoch left for the next claim to mint) with a
   logbook line; a release racing a re-claim gets `Claimed` and skips
   with a logbook line; stale-but-present agents and live
   units are left alone; adoption still happens only in resync.
6. Reviewer scheduling and fix rounds. Criteria: reviewer starts only
   for PR-ready rows, with the pre-release worker id passed as
   `--author` (author==reviewer refusal intact at record time); fix
   round resumes when possible else restarts on the same branch;
   merge-ready rows are ignored by the loop.
7. Tracker decision-write seam. `comment` and `add_label` on the
   `Tracker` protocol plus the GitHub adapter implementation behind
   the single `gh` seam. Criteria: adapter test posts one comment
   and one label through a fake `gh`; fake-`Tracker` test records
   both calls; the outbox still refuses both fields; only the
   `needs-decision` label and the loop's own comment go through it.
8. Stuck detection, restart budget, give-up. Criteria: gone managed
   agent with dead unit restarts within budget reusing the identical
   agent id; foreign holders are never restarted; the budget counts
   only stop-plus-launch restarts in a sliding 24h window, fix-round
   resumes do not consume it; past budget posts through the ticket 7
   decision-write seam, then releases, and a failed write keeps the
   claim; `finished` holders take the section 5 completion release
   before any stuck handling.
9. Observability: journal and snapshot section. Criteria: each pass
   appends well-formed lines; snapshot shows the section 6 fields;
   corrupt journal yields a section error, not a crash.
10. Safety: pause file and day cap. Criteria: pause file exits 1 with
    the file's reason and starts nothing; cap counts starts not
    resumes or plans and resets on date change.
11. Timer unit, doctor checks and docs. Criteria: user timer installed
    and enabled by an explicit operator step; doctor reports missing
    timer, bad registration entries and unreadable policy modules;
    README commands row added if a verb ships.
