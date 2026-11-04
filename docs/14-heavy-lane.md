# 14: a heavy lane so agents share bursty work

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides how several agents on one host share the
bursty, CPU- and memory-heavy parts of their work (a full test
suite, a build) so that one runs while another waits for its turn,
smartly rather than by a fixed count. It evaluates the six-point
shape the ticket proposes, keeps five, and defers the sixth with
reasons. The build follows the tickets in section 12.

Conventions: module, function and file names quoted below were
read from the tree at design time (`takeoff.py`,
`resources.py`, `pressure.py`, `host_ledger.py`, `units.py`,
`paths.py`, `settings.py`). Behavior not yet implemented is
marked PROPOSED; everything else describes the tree as it stands.
JSON documents below use `schema_version: 1`, following the
`takeoff` precedent (`takeoff.SCHEMA_VERSION`). Claims about
kernel or systemd behavior that were not verified on a live host
are marked UNVERIFIED, each with the safe read-only probe that
settles it (section 10).

## 0. Scope and non-goals

The lane adds: one wrapper verb that queues a heavy command,
runs it in a transient scope with CPU and memory limits, and
releases on exit; weights in millicores and megabytes instead of
a count; fair order with bounded backfill; learned per-command
costs; pressure-aware admission; a status view; an explicit
logged bypass; and the removal of the takeoff full-suite lock,
which the lane replaces.

Non-goals: disk (the `disk.py` gate already owns it), network
and GPU limits, placement across hosts (per-host lane only; the
multi-host hub design never admits on a host's behalf),
per-ticket priority in version one, pausing or killing a running
job for any reason, and a shared token pool for `make -j`
(section 2, point 6, evaluated and deferred).

## 1. The problem

Agents are idle most of the time (waiting on the model) and
spike when they run a heavy command. Capping the number of
agents blunts the average, not the spikes: several agents can
hit `make check` together and push the host far past its cores
and memory. Today the only guard is the takeoff advisory
`flock` (`takeoff.suite_lock`, one full suite per host at a
time, exit 75 past `checks.suite_lock_timeout_s`) plus the
pressure check when an agent is admitted
(`resources.admit` refuses a saturated host). Nothing weights
jobs, learns their real cost, reacts to live pressure at queue
time, or limits a running job. The suite lock is also binary:
a small lint and a full suite each hold the whole host.

## 2. The proposed shape, evaluated

The ticket proposes six points. Five are adopted, one with a
changed spelling; the sixth is deferred with reasons.

1. **Wrapper verb.** Adopted, spelled `corral heavy run --
   <cmd>` (a subverb, matching the `corral coord wait` and
   `corral disk` families) instead of a bare `corral heavy --`
   flag form, so `--plan`, `--json` and `--bypass` have a verb
   to hang on. It asks for a slot, waits the turn, runs the
   command in a transient scope with CPU and memory limits
   from the lane budget, and releases on exit. Waiting past
   `heavy.wait_timeout_s` exits 75, keeping the public meaning
   (no free slot, try later).
2. **Weighted slots with backfill.** Adopted, with one bound
   added: a waiter passed over by backfill carries a `skipped`
   count, and the lane stops backfilling past it after
   `heavy.max_skips` passes (section 5). Unbounded backfill
   can starve the head forever; the bound keeps the delay
   provable.
3. **Learned cost.** Adopted, with bounds, a sample window,
   cold start from config, and expiry (section 6).
   Hand-tuned weights stay only as the first-run default.
4. **Pressure-aware admission.** Adopted as specified:
   pressure delays admission and never touches a running job
   (section 7). The readers already exist
   (`resources.parse_pressure`, `resources.read_pressure`,
   `pressure.saturated`); the lane is their second caller
   after the launch gate.
5. **Replace the takeoff lock.** Adopted: the lane becomes the
   only queue for heavy commands (one fact, one place), and
   the suite-lock symbols go away (section 9). Takeoff keeps
   its exit codes, including 75.
6. **A shared token pool in the jobserver style.** Deferred,
   not built. A named FIFO shared across lane holders would
   need a token census: when a holder dies without returning
   its tokens the pool silently shrinks, and the only repair
   is a reaper that drains and refills the pipe under the
   lane lock, distinguishing lost tokens from tokens a live
   holder still owes. That reaper is a second queue beside
   the lane, with its own crash states, for a make-only
   benefit: only make (and a few close tools) speak the
   jobserver protocol, and a shared pipe leaks past any
   holder that passes `MAKEFLAGS` down to a nested make.
   The lane already bounds aggregate CPU through scope
   `CPUQuota`, which covers every tool, not just make. A
   single holder may still use make `-j` with its own
   internal jobserver, bounded by its own scope quota.
   Revisit when a project shows lane-bounded underuse that a
   shared pool would fix; the revisit needs its own design.

## 3. The verbs

One PROPOSED verb family, `corral heavy`, with thin parsers
over `heavy.py` (pure queue math) and injected seams for the
clock, sleep, pid liveness, scope control and file reads, on
the `takeoff.suite_lock` precedent (`_flock`, `_sleep`,
`_monotonic` assigned fakes in tests).

### 3.1 `corral heavy run` (PROPOSED)

Purpose: queue one heavy command, run it in a transient scope
with the lane's limits, report the outcome.

Arguments: `[--timeout SECS] [--bypass REASON] [--json]
[--plan] -- CMD [ARGS...]`. `--` is required: everything past
it is the command, and a missing command is exit 4.
`--timeout` caps this wait only and overrides
`heavy.wait_timeout_s` for the run; zero times out at once
when no slot is free; a negative value is exit 4 (the `coord
wait` precedent). `--bypass` skips the queue (section 3.4).
`--plan` reports without queueing or running (section 3.3).

Behavior: join the queue with the request's weight (section
6), wait for a slot under the queue protocol (section 5),
run the command in a transient scope
(`systemd-run --user --scope --unit=heavy-<id>` with
`MemoryMax=` and `CPUQuota=` from the request's limits, on
the `units.unit_argv` precedent), wait for it, capture the
measured peak (section 6), update the learned cost, release
the slot, and exit with the command's verdict: 0 when the
command exits 0, 1 when it exits nonzero. Lane failures keep
their own codes: 75 the wait passed the limit, 3 the scope
could not be created (the user manager unreachable, the
existing `Unreachable` path), 4 usage or configuration.

JSON shape (`--json`): one document, printed once at the end
(plus the command's own output, which passes through
untouched):

```json
{"schema_version": 1, "command": ["make", "check"],
 "weight": {"cpu_m": 3000, "memory_mb": 6144},
 "waited_s": 42.5, "admitted": true, "bypassed": false,
 "exit_code": 0,
 "measured": {"cpu_m": 2450, "memory_mb": 5100}}
```

`admitted` is true whenever the run held a slot, including
when no lane budget is configured (section 7): then every
request fits, the command runs at once in a scope with
default limits, and its cost is still recorded. Without
`--json`, one line names the outcome
(`heavy: ran in 42.5s after a 38.1s wait, exit 0`), and the
command's stdout and stderr pass through as today.

### 3.2 `corral heavy status` (PROPOSED)

Purpose: the read-only twin the queue needs. Show holders,
waiters and learned costs, for humans and for scripts.

Arguments: `[--json]`. No filters in version one: the file
is small (tens of rows) and callers pipe through `jq` (the
doc 13 composition rule, section 9).

JSON shape (`--json`):

```json
{"schema_version": 1, "budget": {"cpu_m": 4000, "memory_mb": 8192},
 "holders": [{"id": "9c2e..", "command": ["make", "check"],
   "weight": {"cpu_m": 3000, "memory_mb": 6144}, "since": "2026-11-04T10:12:00+00:00"}],
 "waiters": [{"id": "7a1b..", "command": ["make", "build"],
   "weight": {"cpu_m": 1000, "memory_mb": 1024}, "waited_s": 12.0, "skipped": 0}],
 "costs": {"<repo>|<argv-hash>": {"cpu_m": 2450, "memory_mb": 5100, "samples": 6}},
 "recent": [{"at": "2026-11-04T10:11:00+00:00", "command": ["make", "check"],
   "outcome": "released", "measured": {"cpu_m": 2450, "memory_mb": 5100}}]}
```

Cost keys carry the repo plus the argv hash in one string so
`jq` can index them; the argv hash is hex of the command
list, and section 6 defines the key. Without `--json`, one
line per holder and waiter plus a cost table, in the
`takeoff` text style. Exit codes: 0 on a successful read (an
empty lane is an empty list, not a failure), 4 on usage.

### 3.3 Read-only twins

Every verb above has one. `heavy run --plan` resolves the
weight, reads pressure and the holder sum, and reports the
verdict without joining the queue and without running
anything: `{"schema_version": 1, "admitted": true,
"position": 0, "weight": {...}, "reason": "..."}`. A planned
admission that would wait reports `admitted` false with the
head reason and the queue position, and exits 1 (nothing
would run now); a planned admission with a free slot exits 0.
`heavy status` itself writes nothing, so it needs no
`--plan`.

### 3.4 The bypass (PROPOSED)

`--bypass REASON` runs the command now, skipping the queue,
inside the same scope limits the request would have carried.
The reason is required (a bare `--bypass` is exit 4) and is
recorded in `recent[]` with `"outcome": "bypassed"` plus the
reason, so the bypass is visible in `heavy status --json`
and to anyone reading the file. There is no config-off
bypass: a standing bypass would be a second lane with no
accounting. The bypass keeps scope limits (kernel safety is
not policy) but is not charged against the lane budget and
does not update the head-of-queue order; it still records
its measured cost, since a bypassed run measures the same
command. Abuse (every run bypassed) is visible, not
prevented: the status view names bypass rows, and the
project decides what to do about them.

## 4. State: files and single resolvers

One directory, two files, each resolved by exactly one
helper built on `paths.state_dir()`, so hand readers and
every verb agree:

| File | Resolver (PROPOSED) | Contents |
| --- | --- | --- |
| `<state>/heavy/lane.json` | `heavy.lane_path()` | schema version, budget copy, holders, waiters, costs, recent ring |
| `<state>/heavy/lane.lock` | `heavy.lock_path()` | the flock sidecar, empty |

`lane.json` is versioned JSON, written atomically
(`paths.atomic_write_text`) under the lock sidecar, on the
`host_ledger._locked` precedent: non-blocking flock with a
retry loop against a monotonic deadline
(`LOCK_TIMEOUT_S`-scale, section 6 of doc 09); a lock still
busy past the timeout refuses the join with exit 75. Readers
take the shared lock; writers hold it exclusive. A
present-but-corrupt file refuses (exit 4, naming the file)
and is never reinitialized: the lane cannot prove an empty
queue, so it must not invent one. A missing file reads as an
empty lane and is created on the first join, under the lock.

The budget copy inside `lane.json` is a read cache only:
admission resolves the live budget from settings every pass
(section 7); the cached copy lets `status` and hand readers
see what the last writer saw. When the two disagree the live
settings win, and the next write refreshes the cache.

Costs are bounded: keys unused for 30 days are pruned on the
next write, and `recent[]` keeps the last 50 outcomes. No
other retention: holders and waiters are live rows, reaped
by the protocol in section 5.

## 5. The queue protocol

Pure queue math in `heavy.py` over plain data (holders,
waiters, budget, pressure, clock readings); every subprocess
and file touch through injected seams faked in tests.

**Join.** Under the exclusive lock, append one waiter row:
`{id, pid, command, argv_hash, weight, arrived_at,
skipped}`. `id` is a uuid; `pid` is a liveness hint only,
never identity (section 5, stale detection).

**Admit loop.** The waiter polls every `HEAVY_POLL_S`
(1.0 second, the `SUITE_LOCK_POLL_S` precedent) against a
monotonic deadline of `--timeout` or
`heavy.wait_timeout_s`:

1. Read pressure (section 7). Saturated means nobody is
   admitted this round: sleep and recheck. Pressure delays
   admission; it never kills or pauses a holder.
2. Else, admit the head waiter when the holder sum plus its
   weight fits the lane budget in both dimensions.
3. Else, backfill: scan later waiters in arrival order for
   the first one that fits, skipping rows whose `skipped`
   count reached `heavy.max_skips`. Each passed-over waiter
   gains one `skipped`. When the head itself reached the
   bound, or no later waiter fits, nobody is admitted this
   round: sleep and recheck.
4. Past the deadline, remove the waiter row under the lock
   and exit 75.

**Release.** The normal path removes the holder row under
the lock, appends a `released` row to `recent[]`, and folds
the measured peak into the learned cost (section 6).

**Crash recovery for a dead holder.** A holder row carries
the scope unit name and the runner pid. A holder is dead
when its pid fails liveness (the `kill(pid, 0)` seam reports
ESRCH, faked in tests) and its scope is gone. The next
admit pass reaps dead holders under the lock: the row goes,
the weight frees, and `recent[]` gains a `reaped` row. No
cost is recorded for a reaped holder: there is no
measurement, only an absence. The kernel already released
the scope's enforcement when the holder died; the lane only
repairs its own accounting, never signals a process.

**Stale waiters.** A waiter is stale when its pid is dead or
its `arrived_at` is older than the wait timeout plus a grace
bound: a live waiter would have exited 75 and removed
itself by then, so an older row belongs to a dead process.
Admit passes reap stale waiters under the lock the same way
as dead holders. Pid reuse cannot resurrect real state: at
worst a reused pid keeps a dead waiter listed until its age
bound fires, and the row only ever blocks backfill, never
admission of the fitting.

**No daemon.** Waiters poll; nothing wakes them. Reads take
the shared lock and each poll is one small JSON read, so a
few dozen agents polling once a second stay trivial. When a
waiter is killed, its row waits for the next admit pass to
reap it; nothing leaks beyond one stale row.

## 6. Weights and the learning rule

A weight is two numbers in the `resources.Usage` units:
millicores of CPU and megabytes of memory. The lane caps
CPU and memory only; pids stay with the launch gate (doc
09), since a heavy command runs inside the agent's existing
accounting and the lane must not price what another gate
owns.

**Cold start.** The first run of a key uses the
project-configured weight: the `[heavy.commands]` entry
matching the command, else `heavy.default_weight`
(section 8). Hand-tuned weights are only this default; every
measured run moves the key toward its real cost.

**Measurement.** After the scope's child exits, read the
scope cgroup's `memory.peak` (bytes, the high-water mark)
and the `cpu.stat` `usage_usec` delta from scope start over
wall seconds, converted to millicores. Both reads go
through injected file-reader seams faked in tests, on the
`resources.read_slice_use` precedent. A scope whose peak
file is missing records no sample: unknown never blocks,
and the weight stays where it was with a note in
`recent[]`.

**Averaging.** Each key holds `{cpu_m, memory_mb,
samples}`. A new sample folds in with a fixed factor:

```text
new = round(0.75 * old + 0.25 * sample)
samples = min(samples + 1, 20)
```

The window keeps a long-lived key responsive without
letting one crazy sample own it. Both dimensions learn
independently from their own samples.

**Bounds.** A sample clamps to `[1, lane cap]` per
dimension before folding, and the folded value clamps to
the same range: a weight can never exceed what the lane
could admit, and never rounds to zero and runs free. The
lane cap itself validates at or below the host budget
(section 7); a config that breaks this is exit 4.

**A changed command.** The key is `(repo, argv-hash)`:
any change to the command list is a new key and cold
starts. This over-learns at first (a flag tweak relearns
from the default) and never misprices (a new command never
inherits an old one's weight). Keys unused for 30 days are
pruned on the next write. The repo component keeps one
project's suite from pricing another's.

## 7. Admission with PSI and the host budget

Admission is a pure function, shaped like
`resources.admit`: plain data in (request weight, holder
sum, lane budget, pressure reading, clock), a verdict with
a reason string out. Exit mapping at the CLI seam: admit
means run now, delay means keep waiting, refuse means leave
with 75 (past the wait) or 4 (bad config).

**Budget fit.** The holder weights plus the request must
fit the lane budget in both CPU and memory. The lane
budget defaults to the `[resources.host]` budget when the
project sets no `[heavy]` cap; a configured cap must fit
inside the host budget or settings refuse with exit 4.
When no host budget is configured at all, the lane still
queues (order without numbers is still order) but every
request fits: the run executes at once in a scope with
default limits, and its cost is still recorded. Learning
never needs enforcement to measure.

**Pressure.** Before the fit check, read pressure through
the existing seams: load per CPU and `MemAvailable`
percent (`resources.parse_pressure`), plus the stall files
`/proc/pressure/cpu` and `/proc/pressure/memory` (`avg10`,
already parsed by `resources` today). Thresholds reuse the
`[resources.pressure]` caps, so one setting names
saturation for both the launch gate and the lane. A
saturated reading delays admission for every waiter,
including the head; an unknown reading (missing files,
unparsable content) never blocks (the `disk.py`
precedent). Pressure covers what corral does not manage:
a desktop eating the host shows up in PSI and load even
though no holder row names it. Pressure never kills or
pauses a holder: backoff applies to admission only, the
doc 09 rule.

**Composition order** with the launch-time gates stays as
doc 09 sets it: disk, count, budget, quota at launch; the
lane runs later, at command time, inside an already
admitted agent. A lane delay never un-admits an agent.

## 8. How agents use it

Three candidates, one recommendation.

| Surface | How it works | Cost |
| --- | --- | --- |
| Prompts and skills | Text tells agents to prefix heavy commands with `corral heavy run --` | Drifts: every prompt copy can differ, and nothing enforces it |
| A wrapper on PATH | A shim dir prepends `make` and friends that queue first | Hides behavior: `make` stops meaning `make`, and hand runs change meaning with PATH |
| Project check commands | `corral.toml` lists heavy commands; takeoff and agents route them through the lane | One list per project, visible, versioned beside the commands |

**Recommendation: project check commands, plus manual
`heavy run` for the rest.** The project's `[heavy]`
table names the commands that queue (the suite, the
build); takeoff routes exactly those through the lane
(section 9), and an agent with an ad hoc heavy command
runs `corral heavy run -- <cmd>` by hand, or by one line
in the project skill. Prompts stay as documentation of
the list, never as the mechanism; no wrapper dir is
added to any PATH. Tradeoff: a new heavy command queues
only after someone lists it, so the first unlisted spike
still lands unqueued. That is the price of explicitness,
and the status view shows the unlisted cost to motivate
listing it.

**Opt-in per project (recommended).** A project with no
`[heavy]` table keeps today's behavior byte-identical:
takeoff runs its commands under no lane, and `heavy run`
still works by hand with default weights. Enforcement
without measured costs would throttle blindly; projects
adopt command by command as costs accumulate. A later
ticket may flip the default once costs exist across the
fleet; that flip is a policy decision with its own
approval, not part of this design.

## 9. What it reuses, what changes

**From doc 09 (budget, slices).** The lane reuses: the
`Usage` triple and its arithmetic; `parse_pressure` and
`read_pressure` plus the `[resources.pressure]` caps; the
`host_ledger._locked` timed-lock pattern and its 75-on-busy
shape; the `units` scope property builders (`MemoryMax=`,
`CPUQuota=`, slice placement) and the controller-skip rule;
`paths.state_dir()` as the root every lane path builds on;
`settings.register` strict keys with exit 4 naming file and
key. It adds no new enforcement primitive: scopes and
quotas stay exactly what 09 built.

**From doc 13 (events, gate, merge).** The lane reuses: the
`--plan` and `--json` conventions with `schema_version`;
the exit-code contract (0 ok, 1 nothing to do, 2 race lost,
3 unreachable, 4 usage, 75 no slot); the skill-table
pattern (the lane adds its rows to the coordinator skill in
the last implementation ticket, with the same
parser-conformance test). It changes one verb: `takeoff`.

**What changes in `takeoff`.** The full-suite lock goes
away, and the lane is the only queue (one fact, one
place):

- Deleted: `SUITE_LOCK_NAME`, `suite_lock_path()`,
  `suite_lock()`, `SuiteBusy`, the
  `checks.suite_lock_timeout_s` setting, and the lock hold
  around `_check_commands`. A config still naming the old
  setting is exit 4 (strict keys already refuse unknown
  keys, so no extra code is needed beyond the deletion).
- Added: before each configured command, takeoff asks the
  lane whether the project lists the command as heavy; a
  listed command runs with `heavy run` semantics through a
  shared module call (not a subprocess), and an unlisted
  command runs as today. The wait honors
  `heavy.wait_timeout_s`; passing it exits 75 with the lane
  reason, preserving takeoff's current contract.
- Unchanged: `checks.timeout_s` still kills a hung
  command; the check rows, the `--skip` and `--plan`
  surface from doc 13, and every other exit code.

With the lock gone, two takeoffs running different heavy
commands share the host by weight instead of serializing
behind one lock, and two takeoffs running unlisted
commands behave exactly as today.

## 10. Verified versus UNVERIFIED

Read-only probes run on the author's host while writing;
nothing was started, stopped or written. Host-specific
values are omitted; only the shape of each answer is
claimed.

| Claim | State | Probe (read-only) |
| --- | --- | --- |
| PSI files exist with `some` and `full` `avg10` lines | Verified | `cat /proc/pressure/cpu` (and `.../memory`) `|| echo absent` |
| cgroup v2 with `cpu`, `memory`, `pids` controllers | Verified | `cat /sys/fs/cgroup/cgroup.controllers` |
| `memory.peak` exists on a live scope and reports bytes | Verified | `p=$(sed -n 's/^0:://p' /proc/self/cgroup); cat /sys/fs/cgroup$p/memory.peak` |
| `systemd-run` offers `--scope`, `-p`, `--collect` | Verified | `systemd-run --help` |
| `memory.peak` still readable after its scope exits | UNVERIFIED | Run a scratch scope that sleeps, then read its `memory.peak` path after exit, with and without `--collect` |
| A nested slice `MemoryMax=` caps the aggregate, not each leaf | UNVERIFIED | Doc 09 section 1 probe (scratch slice, `systemd-run` writer, read leaf `memory.peak`) |
| Which `systemctl --user show` property reports delegated controllers | UNVERIFIED | Doc 09 section 1 probe (`systemctl --user show -p Controllers` plus the slice `cgroup.controllers` file) |

The implementation tickets must settle the first
UNVERIFIED row before the learning ticket: if the peak
vanishes with the scope, the capture step must read it
before the unit unloads (no `--collect`; explicit cleanup
through a required-arg seam) rather than after release.

## 11. The test of the design

A coordinator rebuilds "run the suite when it fits" in
about ten lines of shell. Names below are stand-ins.

Run the suite when it fits (75 waits and retries, a
failing suite exits with its code):

```sh
while true; do
  corral heavy run -- make check; code=$?
  [ $code -eq 75 ] && sleep 60 && continue
  exit $code
done
```

Watch the lane (holders, waiters, learned costs):

```sh
corral heavy status --json | jq '{holders, waiters, costs}'
```

Gate the same way takeoff will (plan first, then run;
config errors abort loudly):

```sh
corral heavy run --plan -- make check || exit 1
corral heavy run -- make check
```

Everything done by hand today stays possible, because the
state stays open: the same suite runs without the lane
(it just does not queue), the same scope runs by hand
(`systemd-run --user --scope` with the same properties),
the queue reads with `cat` and `jq` straight from
`lane.json`, and one urgent command jumps the queue with
`corral heavy run --bypass "release fix" -- make check`,
leaving its reason in the status view. The verbs are
conveniences over files, not gates in front of them.

## 12. Implementation tickets, in order

Each ticket is small, independently reviewable, and ships
its own tests with fakes at the seams (no live systemd,
no `/proc` or `/sys`, no state dir outside temp dirs, per
AGENTS.md). Later tickets build on earlier ones.

1. Weighted slots with limits, plus the takeoff lock
   replacement. Lane budget check, scope argv builder on
   the `units` precedent, per-command routing in
   `_check_commands`, deletion of the suite-lock symbols
   and setting. Criteria: fake-`run` tests assert the
   scope argv; each failure row produces its exit code;
   no test references the deleted symbols.
2. Queue file, waiters, status and plan. `lane.json`
   plus lock sidecar, join/wait/release, `heavy run`
   `--plan` and `heavy status --json`, wait timeout to
   exit 75. Criteria: golden-file tests for both JSON
   shapes; lock-busy refuses 75; corrupt file refuses 4
   and is never reset.
3. Fair order, backfill and reaping. Head-first admit,
   bounded skips, dead-holder and stale-waiter reaping
   under the lock with fake pid-liveness and clock
   seams. Criteria: backfill admits a small job past a
   big head; the skip bound stops starvation; a dead
   holder frees its weight with no cost recorded.
4. Learned costs. Peak and counter capture through
   injected readers, the averaging rule with bounds,
   cold start, key change on command change, 30-day
   prune. Criteria: a two-sample fold hits the exact
   integer; bounds clamp both sides; missing peak
   records no sample. Blocked on the section 10 probe.
5. PSI admission and doctor rows. Saturated delays,
   unknown never blocks, `[resources.pressure]` caps
   shared with the launch gate, `resources` doctor rows
   for lane health, stuck-waiter warnings, bypass
   counts. Criteria: saturated refuses while shares are
   free; absent files read unknown; every row renders.
6. Bypass, journal surface and docs. `--bypass` with
   required reason, `recent[]` ring, coordinator skill
   rows with the parser-conformance test, and README
   Commands-table rows. Criteria: `test_readme.py` passes; a
   bare `--bypass` is exit 4; bypass rows name the
   reason.

NOT a ticket: the shared token pool (section 2 defers
it), per-ticket priority (no ordering over project
boards exists to mirror), and any pauser or killer for
running jobs (pressure never touches holders).

## 13. Options for the maintainer

Ship the lane opt-in per project, five points now, the
pool deferred.

Options:
1. Ship 1 to 6 as specified, opt-in (recommended)
2. Ship without learned cost, static weights
3. Keep the takeoff flock beside the lane
4. Lane mandatory for listed suites at once
5. Drop the bypass flag, config-off only
