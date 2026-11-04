# 14: a heavy lane so agents share bursty work

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides how several agents on one host share the
bursty, CPU- and memory-heavy parts of their work (a full test
suite, a build) so that one runs while another waits for its turn,
smartly rather than by a fixed count. It evaluates the six-point
shape the ticket proposes, keeps five, and defers the sixth with
reasons. The build follows the tickets in section 13.

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
   the computed deadline (section 6.4) exits 75, keeping
   the public meaning (no free slot, try later).
2. **Weighted slots with backfill.** Adopted, with one bound
   added: a waiter passed over by backfill carries a `skipped`
   count, and the lane stops backfilling past it after
   `heavy.max_skips` passes (section 5). Unbounded backfill
   can starve the head forever; the bound keeps the delay
   provable.
3. **Learned cost.** Adopted, as the section 6 ledger
   estimator: raw rows in an append-only ledger, robust
   statistics (a median plus a high percentile) per repo,
   command and role, a conservative cold-start default,
   recency weighting with a small-sample rule, a changed
   command as a new key, and starved samples excluded from
   duration learning. Hand-tuned weights stay only as the
   first-run default.
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

Arguments: `[--timeout SECS] [--role NAME] [--bypass REASON]
[--json] [--plan] -- CMD [ARGS...]`. `--` is required:
everything past it is the command, and a missing command is
exit 4. `--timeout` caps this wait only and overrides the
computed wait deadline for the run (section 6); zero times
out at once when no slot is free; a negative value is exit
4 (the `coord wait` precedent). `--role` names the caller
for the ledger (`takeoff` when takeoff routes through the
shared module call, `hand` otherwise); an empty role is
exit 4. `--bypass` skips the queue (section 3.4). `--plan`
reports without queueing or running (section 3.3).

Behavior: join the queue with the request's weight (section
6), wait for a slot under the queue protocol (section 5),
run the command in a transient scope
(`systemd-run --user --scope --unit=heavy-<id>` with
`MemoryMax=` and `CPUQuota=` from the request's limits, on
the `units.unit_argv` precedent), wait for it, capture the
per-cgroup measurement (section 6), append the ledger row,
release the slot, and exit with the command's verdict: the
process exit equals the command's exit when that exit is
one of the public codes (0, 1, 2, 3, 4, 75); any other
command exit maps to 1. Lane failures keep their own
codes: 75 the wait passed the limit, 3 the scope could not
be created (the user manager unreachable, the existing
`Unreachable` path), 4 usage or configuration.

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
default limits, and its cost is still recorded. The JSON
document always carries the raw command exit in
`exit_code`, so a reader can tell a 75 from the lane
(never admitted) from a 75 from the command (admitted):
a bare retry loop on 75 retries both, and the precise loop
in section 11 checks `.admitted` first. `measured` is null
when the scope vanished before the read (section 6); the
weight then stays where it was. Without `--json`, one
line names the outcome
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
   "weight": {"cpu_m": 3000, "memory_mb": 6144}, "since": "2026-11-04T10:12:00+00:00",
   "eta_s": 120.0}],
 "waiters": [{"id": "7a1b..", "command": ["make", "build"],
   "weight": {"cpu_m": 1000, "memory_mb": 1024}, "waited_s": 12.0, "skipped": 0,
   "eta_s": 300.0}],
 "costs": {"<repo>|<argv-hash>|<role>": {"cpu_m": 2450, "memory_mb": 5100,
   "samples": 6, "p50_s": 180.0, "p90_s": 240.0, "flaky": false}},
 "recent": [{"at": "2026-11-04T10:11:00+00:00", "command": ["make", "check"],
   "outcome": "released", "measured": {"cpu_m": 2450, "memory_mb": 5100}}]}
```

Cost keys carry the repo, the argv hash and the role in one
string so `jq` can index them; the argv hash is hex of the
command list, and section 6 defines the key and every
estimated field. Holder and waiter `eta_s` are the
section 6 predictions (remaining run time, queued wait);
both are approximate and may be null when the key has no
estimate yet. Without `--json`, one line per holder and
waiter plus a cost table, in the `takeoff` text style.
Exit codes: 0 on a successful read (an empty lane is an
empty list, not a failure), 4 on usage.

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
does not update the head-of-queue order; it still appends
its ledger row, since a bypassed run measures the same
command. Abuse (every run bypassed) is visible, not
prevented: the status view names bypass rows, and the
project decides what to do about them.

### 3.5 `corral heavy rollup` (PROPOSED)

Purpose: regenerate the portable per-key summary
(section 6.5) from the raw ledger files.

Arguments: `[--out PATH]`. Standard output always carries
the rollup JSON document; `--out` writes it atomically to
the given path instead. Exit 0 with an empty key set when
the ledger is empty (nothing learned yet, not a failure),
4 on usage. The verb never commits; the operator commits
the copy it points at.

## 4. State: files and single resolvers

One directory, four paths, each resolved by exactly one
helper built on `paths.state_dir()`, so hand readers and
every verb agree:

| File | Resolver (PROPOSED) | Contents |
| --- | --- | --- |
| `<state>/heavy/lane.json` | `heavy.lane_path()` | schema version, budget copy, holders, waiters, cost estimates, recent ring |
| `<state>/heavy/lane.lock` | `heavy.lock_path()` | the flock sidecar, empty |
| `<state>/heavy/runs/<host>-<period>.jsonl` | `heavy.ledger_path()` | append-only raw run rows, one file per host per period |
| `<state>/heavy/host-id` | `heavy.host_label()` | the opaque stable host label, created once |

`lane.json` is versioned JSON, written atomically
(`paths.atomic_write_text`) under the lock sidecar, on the
`host_ledger._locked` precedent: non-blocking flock with a
retry loop against a monotonic deadline
(`LOCK_TIMEOUT_S`-scale, section 6 of doc 09); a lock still
busy past the timeout refuses the join with exit 75. Readers
take the shared lock; writers hold it exclusive. A
present-but-corrupt file refuses (exit 4, naming the file)
and is never reinitialized by any verb: the lane cannot
prove an empty queue, so it must not invent one. A missing
file reads as an empty lane and is created on the first
join, under the lock. Repair is explicit and keeps the
evidence: the operator moves the corrupt file aside to
`lane.json.corrupt-<utc-timestamp>` in the same directory
(archived, never deleted, so nothing is lost) and the next
join recreates an empty lane under the lock. The doctor
names the corruption without parsing the queue (ticket 5):
`heavy: lane.json unparsable, N bytes, last write <ts>;
repair by moving it aside`. A corrupt ledger row never
blocks the lane: the writer appends only well-formed rows,
and the estimator skips rows that fail validation,
counting them in the status view.

The budget copy inside `lane.json` is a read cache only:
admission resolves the live budget from settings every pass
(section 7); the cached copy lets `status` and hand readers
see what the last writer saw. When the two disagree the live
settings win, and the next write refreshes the cache.

Cost rows are bounded: keys unused for 30 days are pruned on
the next write, and `recent[]` keeps the last 50 outcomes.
Raw ledger files are never pruned by corral; the operator
may archive old period files by hand once the rollup
(section 6) has absorbed them. No other retention: holders
and waiters are live rows, reaped by the protocol in
section 5.

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
monotonic deadline of `--timeout` or the computed wait
deadline (section 6):

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
the lock, appends a `released` row to `recent[]`, and
appends the run's ledger row (section 6).

**Crash recovery for a dead holder.** A holder row carries
the scope unit name and the runner pid; the pid is a hint
for log lines only, never identity. A holder is dead when
its scope unit is gone: kernel state is authoritative,
checked through an injected `scope_exists(unit)` seam
faked in tests. The next admit pass reaps dead holders
under the lock: the row goes, the weight frees, and
`recent[]` gains a `reaped` row naming the unit. No ledger
row is appended for a reaped holder: there is no
measurement, only an absence. Pid reuse cannot leak weight:
a reused pid never keeps a row alive, because the pid is
not consulted; the worst case is a live scope whose runner
died while children still hold resources, and that row
correctly keeps its weight until the scope itself exits.
The kernel already released the scope's enforcement when
the holder died; the lane only repairs its own accounting,
never signals a process.

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

## 6. Weights, measurement, and learning from the ledger

A weight is two numbers in the `resources.Usage` units:
millicores of CPU and megabytes of memory. The lane caps
CPU and memory only; pids stay with the launch gate (doc
09), since a heavy command runs inside the agent's existing
accounting and the lane must not price what another gate
owns. Weights are not stored guesses folded into
`lane.json` by a fixed ratio. They are estimates recomputed
from a raw run ledger that stays readable by hand, so any
estimate can be checked against the rows that produced it.

### 6.1 Measurement in the job's own cgroup

Every ledgered command runs as its own transient unit, and
every counter comes from that unit's cgroup only, so other
processes on the host never pollute the measurement. After
the scope's child exits and before the scope is released,
the lane resolves the unit's cgroup path through the
manager (`systemctl --user show <unit> -p ControlGroup`,
injected seam, faked in tests) and reads, through injected
file-reader seams on the `resources.read_slice_use`
precedent:

| Counter | What it is |
| --- | --- |
| `cpu.stat` `usage_usec` delta | work: CPU-seconds consumed |
| `memory.peak` | high-water mark of resident memory |
| `pids.peak` | high-water mark of task count |
| `cpu.pressure` totals | time its tasks waited for CPU: how much it was starved by others |
| `memory.pressure` totals | time its tasks waited on memory, kept for diagnosis |

Section 10 records which of these the target host
(kernel 7.0, cgroup v2, user session delegating `cpu`
`memory` `pids`) was verified to expose. Two limits hold
everywhere. First, the `io` controller is not delegated to
the user session, so there is no per-job IO accounting,
only host-wide `io.pressure`; IO stays a later UNVERIFIED
stage and never silently stands in for CPU or memory.
Second, true peak parallelism needs sampling the lane has
no daemon for, so the record carries average parallelism
(`cpu_s / wall_s`, derived, exact) plus the `cpu.stat`
throttle counters (`nr_throttled`, `throttled_usec`: the
job wanted more than its quota), and peak-cores sampling
stays a later stage.

The read-at-exit procedure keeps the unit loaded until the
five reads complete (no `--collect` cleanup before the
read; explicit teardown through a required-arg seam after,
so a missed fake fails closed instead of leaking a
scope). When the cgroup is already gone, the run records a
row with `measured` null and a note in `recent[]`, and the
weight stays where it was: unknown never blocks.

Contention is read from the job's own counters, not from a
host-wide guess. A sample is starved when its cgroup CPU
`some`-pressure total, divided by wall microseconds,
passes `heavy.starved_ratio` (default 0.2): the job spent
a fifth of its life runnable but not running. Starved rows
are flagged in the ledger and excluded from duration
learning, so an estimate cannot shrink merely because a
job was starved. Peak memory still folds starved rows: the
high-water mark survives throttling.

### 6.2 The run ledger, the raw truth

One record per run, appended as one JSON line. One file
per host per period
(`<state>/heavy/runs/<host>-<period>.jsonl`, monthly while
`heavy.ledger_period` stays `"month"`), so two hosts never
write the same file and a merge never conflicts. The host
is the opaque stable label from `heavy.host_label()`
(random hex in `<state>/heavy/host-id`, created once),
never a hostname or address. The ledger is host-local
state, never committed; real ledgers never enter the
public corral repository, which holds only the schema, the
code, and a synthetic example.

A row (keys stable under `schema_version`, section 6.5):

```json
{"schema_version": 1, "at": "2026-11-04T10:12:00+00:00",
 "host": {"label": "9c2e..", "cores": 8, "mem_mb": 16384, "python": "3.11"},
 "repo": "a-project", "argv_hash": "abc123..", "role": "takeoff",
 "argv": ["make", "check"], "wall_s": 182.4, "cpu_s": 412.7,
 "peak_mb": 5210, "pids_peak": 96, "avg_cores": 2.3,
 "throttled_us": 12000, "cpu_pressure_us": 8400000,
 "mem_pressure_us": 0, "starved": false, "exit": 0,
 "waited_s": 38.1, "weight": {"cpu_m": 3000, "memory_mb": 6144},
 "host_pressure": {"load_per_cpu": 0.4, "mem_avail_pct": 62.0}}
```

`repo` comes from the one resolver both callers share,
`heavy.repo_key(cwd)`: the enclosing checkout's
`corral.toml` `project.name` when present, else the
checkout top directory name. Takeoff and a hand `heavy
run` resolve the same key through it; a test asserts both
callers agree (one fact, one place). `role` is `takeoff`
or `hand` in version one. `host_pressure` is the lane's
admission-time reading (load per CPU, available memory
percent), so a later reader sees what the queue saw.

### 6.3 The estimator

Estimates are robust statistics over the raw rows per
`(repo, argv-hash, role)`, recomputed from the ledger, and
a hand reader with `jq` can reproduce them. Rules:

- Window: the trailing 20 rows of the key within the
  trailing 90 days. Duration sets (`wall_s`) exclude rows
  flagged starved; peak sets (`peak_mb`) use all rows.
- Small samples: with fewer than 3 rows, the prediction is
  the maximum observed, not an average. A first heavy
  suite priced at the 1000 MB default that measures
  6000 MB prices at 6000 MB after one sample, where a
  fixed fold would sit near 2250 MB and under-admit the
  next run.
- Steady state: predicted peak is the high percentile
  (p90) of the peak set; typical duration is the median
  (p50) of the duration set; sizing uses the duration p90.
- Cold start: a key with no rows uses the
  project-configured weight, the `[heavy.commands]` entry
  matching the command else `heavy.default_weight`
  (section 8). The default is documented as a conservative
  upper bound: unknown jobs queue safely, and the first
  measurement corrects them.
- A changed command is a new key: any change to the
  command list changes the argv hash and cold starts. This
  over-learns at first and never misprices. A role with
  fewer than 3 rows falls back to the role-agnostic key
  `(repo, argv-hash)`; roles never borrow peaks across
  different commands.
- Bounds and rounding: predictions round to integers and
  clamp to `[1, lane cap]` per dimension, so a weight can
  never exceed what the lane could admit and never rounds
  to zero and runs free. The lane cap validates at or
  below the host budget (section 7); a config that breaks
  this is exit 4.
- Keys unused for 30 days are pruned from `lane.json` on
  the next write; the raw rows stay, so nothing learned is
  ever truly lost.

### 6.4 What the estimates feed

- Admission: the request weight is the predicted peak
  (p90, or max when young, else the cold-start default).
- Backfill: predicted typical duration (p50) orders the
  scan and prices the delay bound.
- ETAs in the status view: a holder's remaining time is
  `max(0, p50 - elapsed)`; a waiter's ETA sums the
  predicted durations ahead of it that block it. Both are
  approximate and null when the key has no estimate.
- Timeouts: the default wait deadline is
  `max(heavy.wait_timeout_s, ceil(heavy.wait_factor *
  p90_s))` with `heavy.wait_factor` defaulting to 3, so a
  long suite waits longer than a quick lint by construction.
  `--timeout` still overrides absolutely. A holder running
  past `wait_factor * p90` and a waiter waiting past the
  deadline scale raise stuck rows in the doctor view
  (ticket 5).
- Flaky detection: a key whose trailing 10 rows hold at
  least 2 zero and 2 nonzero exits reports `flaky: true`
  in costs plus a doctor row. Exit history is signal, not
  enforcement: nothing refuses a flaky key.

### 6.5 Portable schema, rollup, and cross-host prediction

Raw rows carry `schema_version`. The rules for changing
the schema: new fields are optional with documented
defaults; fields are never renamed or removed; readers
ignore unknown fields; the version bumps only on a
breaking change. Records written today stay readable: a
reader pins its supported version and refuses (exit 4,
naming the file) only what it cannot parse, never what it
merely does not know.

The derived per-key summary is a separate small file meant
to be committed to a project's private ops repository, so
a fresh clone or a new host starts with warm estimates:
`corral heavy rollup [--out PATH]` (PROPOSED) regenerates
it from the raw files. The default path resolves through
`heavy.rollup_path()` (`<state>/heavy/rollup.json`);
`--out` writes a copy wherever the operator points it, and
the operator commits it. corral never commits anything
itself. The rollup carries its `schema_version`, its
source range, and per key the peak p50 and p90, the
duration p50 and p90, the sample count, the flaky flag,
and the portable work figures below. It holds hashes,
never argv text or paths; opaque host labels may appear
only as correction keys. A worked rollup:

```json
{"schema_version": 1, "sources": ["<host>-2026-11.jsonl"],
 "keys": {"a-project|abc123..|takeoff":
  {"peak_mb_p50": 5100, "peak_mb_p90": 5400,
   "dur_s_p50": 180.0, "dur_s_p90": 240.0, "samples": 6,
   "flaky": false, "work_ref_s": 206.0, "avg_cores": 2.3,
   "corr": {"<host>": 1.02}}}}
```

Hardware-neutral measures let one host's rows warm
another. Each row records work, not just elapsed time:
CPU-seconds consumed, peak resident memory, average and
throttled parallelism, wall seconds, plus a host
descriptor (core count, memory, the interpreter version
that matters; projects may add toolchain versions later).
Speed is defined as the time a small fixed stdlib-only
reference workload takes on the host, expressed relative
to a reference constant published in the schema, so work
converts to reference-core-seconds. Wall time on a host
predicts as reference work over the host's speed and
usable cores, bounded by the parallelism the job has been
seen to use. This is approximate where the record is
silent (IO, memory bandwidth, thermal limits, a different
interpreter version), and a learned per-host correction
(observed over predicted, running average) converges each
host's predictions from the shared figures.

Staged: the first implementation records the raw fields
and the host descriptor from day one, with speed absent
and estimates local-only. Calibration and cross-host
prediction come in a later ticket (section 13), since
retrofitting the descriptor is hard and the prediction is
easy once the rows exist.

### 6.6 Later stage: learning at the ticket level

Clearly separated from run learning, and not part of the
first build: duration and quota use per ticket type, and
review rework rates, feeding the drive loop's start
choices. It reads the same ledger (joined on repo and
role; the ticket type field arrives with that stage's own
record-only ticket), and its first ticket records rows
and changes nothing else. It needs its own approval: it
touches project policy and board surface, not just host
accounting.

## 7. Admission with PSI and the host budget

Admission is a pure function, shaped like
`resources.admit`: plain data in (request weight, holder
sum, lane budget, pressure reading, clock), a verdict with
a reason string out. Exit mapping at the CLI seam: admit
means run now, delay means keep waiting, refuse means leave
with 75 (past the wait) or 4 (bad config).

**Budget fit.** The holder weights plus the request must
fit the lane budget in both CPU and memory, where the
request weight is the section 6 predicted peak for its
key. The lane budget defaults to the `[resources.host]`
budget when the project sets no `[heavy]` cap; a
configured cap must fit inside the host budget or settings
refuse with exit 4. When no host budget is configured at
all, the lane still queues (order without numbers is still
order) but every request fits: the run executes at once in
a scope with default limits, and its row is still recorded.
Learning never needs enforcement to measure.

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
doc 09 rule. The admission-time reading is stored in the
run's ledger row (section 6.2), and it is also the
host-wide pressure kept distinct from each job's own
cgroup pressure.

**Wait deadline.** The computed wait deadline scales with
the typical run (section 6.4): the longer the suite
usually takes, the longer its waiter may wait. A project
that wants a fixed ceiling sets `--timeout` per run.

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
parser-conformance test). The lane adds the ledger and
`heavy rollup` beside the verbs (section 6.5): raw rows
stay host-local, and the rollup is the only file meant to
leave the host. It changes one verb: `takeoff`.

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
  shared module call (not a subprocess) with role
  `takeoff`, resolving the ledger key through the same
  `heavy.repo_key()` a hand run uses, and an unlisted
  command runs as today. The wait honors the computed wait
  deadline (section 6.4); passing it exits 75 with the lane
  reason, preserving takeoff's current contract.
- Unchanged: `checks.timeout_s` still kills a hung
  command; the check rows, the `--skip` and `--plan`
  surface from doc 13, and every other exit code.

With the lock gone, two takeoffs running different heavy
commands share the host by weight instead of serializing
behind one lock, and two takeoffs running unlisted
commands behave exactly as today.

## 10. Verified versus UNVERIFIED

Read-only probes; nothing was started, stopped or
written. Host-specific values are omitted; only the shape
of each answer is claimed. `Verified on the target host`
means checked read-only on kernel 7.0 with cgroup v2 and
a user session delegating `cpu` `memory` `pids`.

| Claim | State | Probe (read-only) |
| --- | --- | --- |
| Target host runs kernel 7.0 with cgroup v2 | Verified on the target host | `uname -r`; `stat -fc %T /sys/fs/cgroup` reads `cgroup2fs` |
| User session delegates `cpu`, `memory`, `pids` | Verified on the target host | `cat` the user service slice `cgroup.controllers` |
| PSI files exist with `some` and `full` `avg10` lines | Verified on the target host | `cat /proc/pressure/cpu` (and `.../memory`) `|| echo absent` |
| An agent transient unit exposes `cpu.stat` with `usage_usec` | Verified on the target host | resolve the unit cgroup, `grep -o 'usage_usec [0-9]*'` its `cpu.stat` |
| An agent transient unit exposes `memory.peak` reporting bytes | Verified on the target host | `p=$(sed -n 's/^0:://p' /proc/self/cgroup); cat /sys/fs/cgroup$p/memory.peak` |
| An agent transient unit exposes `pids.peak` | Verified on the target host | `cat` the unit cgroup `pids.peak` |
| An agent transient unit exposes its own `cpu.pressure` and `memory.pressure` | Verified on the target host | `cat` the unit cgroup `cpu.pressure` and `memory.pressure` |
| `systemd-run` offers `--scope`, `-p`, `--collect` | Verified on the target host | `systemd-run --help` |
| `io` is NOT delegated to the user session: no per-job IO accounting, only host-wide `io.pressure` | Verified on the target host | `cat` the unit cgroup `cgroup.controllers` (no `io`), `cat /proc/pressure/io` (present) |
| Peak counters stay readable after the scope exits | UNVERIFIED | Run a scratch scope that sleeps, then read its `memory.peak` path after exit, with and without `--collect` |
| A nested slice `MemoryMax=` caps the aggregate, not each leaf | UNVERIFIED | Doc 09 section 1 probe (scratch slice, `systemd-run` writer, read leaf `memory.peak`) |
| Which `systemctl --user show` property reports the unit cgroup path | UNVERIFIED | `systemctl --user show <unit> -p ControlGroup` on a scratch scope |

The learning ticket must settle the scope-exit row first:
the read-at-exit procedure (section 6.1) already keeps the
unit loaded until the reads complete, but if the probe
shows peaks vanish with the scope on some manager
version, the capture step reads before the child is
reaped rather than after release, with no `--collect`
until the teardown seam runs. The IO row needs no probe:
it records an absence, and per-job IO stays a later stage
until delegation changes.

## 11. The test of the design

A coordinator rebuilds "run the suite when it fits" in
about ten lines of shell. Names below are stand-ins.

Run the suite when it fits (a 75 the lane refused waits
and retries; a 75 from the command itself does not):

```sh
while true; do
  row=$(corral heavy run --json -- make check); code=$?
  [ $code -eq 75 ] && [ "$(echo "$row" | jq -r .admitted)" = "false" ] \
    && sleep 60 && continue
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

## 12. Build versus adopt

The lane is a thin policy layer, not a scheduler product.
Each candidate below was judged against the same bar: the
seven lane requirements (weighted slots in memory and
CPU; learned per-command cost; admission from pressure
corral does not manage; plain-file state with a CLI that
can show every decision; no daemon making decisions the
CLI cannot show; stdlib only with no new runtime
dependency; one injectable seam), plus what adoption
would cost (a new host program in `system_deps.toml`, a
new seam, a new failure mode). Nothing was installed to
test any of them; installing on a host needs the
maintainer's go-ahead. Behavioral claims below are
UNVERIFIED except where the cited probe already runs in
this design. pueue's group parallelism and task-spooler's
slot count were confirmed against their published docs in
this session; the rest is judged from stable documented
behavior and marked UNVERIFIED where a version could
prove it wrong.

| Candidate | Meets | Fails | Adopt cost |
| --- | --- | --- | --- |
| pueue (queue daemon, per-group parallel limits) | queue, order, surviving restarts | count only, no memory weight; no learned cost; no PSI admission; daemon decides (status shows, policy hidden); new Rust binary in `system_deps.toml` plus a daemon seam and daemon-down failure mode | high: host program, daemon lifecycle, second queue beside the lane |
| task-spooler (one-slot spooler, `-S` slots) | tiny queue, FIFO order | count only; no weights, no learning, no pressure, no memory limit; server process owns state (UNVERIFIED on current release) | medium: host program, server-owned queue corral cannot read as plain files |
| GNU make jobserver (named FIFO token pool) | shares a CPU budget across `make -j` | make-only (leaks via `MAKEFLAGS` to nested makes); token census needs a reaper: a second queue with its own crash states (section 2, point 6) | medium: FIFO lifecycle seam, lost-token failure mode, no memory story |
| Ninja pools | depth-limited pools for heavy steps | build-system only; no queue, no learning, no pressure, no CLI status of holders | medium: only helps Ninja builds, nothing else |
| `make --load-average` | one-line CPU backoff | no queue, no memory, no learning, no status; load only, blind to cgroup and PSI (UNVERIFIED against PSI-masked hosts) | low but buys almost nothing |
| systemd scopes with cgroup limits | per-job CPU and memory caps, kernel-enforced | no queue, no weights, no learning, no pressure admission | none: already the lane's enforcement primitive (doc 09) |
| PSI with a watcher such as systemd-oomd | host-wide pressure signal | oomd kills on pressure; it never queues, never learns, never admits | none as a signal; wrong tool as a policy (killer, not lane) |
| Slurm, Kueue as models | weighted fair-share, backfill, learned sizing | cluster daemons with their own state, accounts and network; a host fleet is not a cluster | out of scope: adopt nothing, borrow backfill and fair-share shape only |

Verdict: build the thin lane on the primitives (flock
queue, systemd scopes, PSI signal, JSONL ledger) and
adopt nothing wholesale. Every candidate either counts
instead of weighing, decides inside a daemon the CLI
cannot replay, or prices one tool instead of every heavy
command. The one partial reuse stays: a single holder may
still run `make -j` with its own internal jobserver,
bounded by its own scope quota. Revisit only with
evidence: a project showing lane-bounded underuse that a
shared pool fixes, with its own design. The adopt
alternative stands as an option below.

## 13. Implementation tickets, in order

Each ticket is small, independently reviewable, and ships
its own tests with fakes at the seams (no live systemd,
no `/proc` or `/sys`, no state dir outside temp dirs, per
AGENTS.md). Later tickets build on earlier ones.

1. Weighted slots with limits, the takeoff lock
   replacement, and raw recording. Lane budget check,
   scope argv builder on the `units` precedent,
   per-command routing in `_check_commands` with role
   `takeoff`, deletion of the suite-lock symbols and
   setting, plus one ledger row per run with the host
   descriptor from day one (estimates stay local-only;
   weights still come from config). Criteria: fake-`run`
   tests assert the scope argv; each failure row produces
   its exit code; a ledger-row golden file pins the
   schema; a vanished cgroup records `measured` null;
   no test references the deleted symbols.
2. Queue file, waiters, status and plan. `lane.json`
   plus lock sidecar, join/wait/release, `heavy run`
   `--plan` and `heavy status --json`, the deadline floor
   to exit 75. Criteria: golden-file tests for both JSON
   shapes; lock-busy refuses 75; corrupt file refuses 4
   and is never reset (repair stays the documented
   move-aside).
3. Fair order, backfill and reaping. Head-first admit,
   bounded skips, dead-holder reaping on scope-gone and
   stale-waiter reaping under the lock with fake scope,
   pid-liveness and clock seams. Criteria: backfill
   admits a small job past a big head; the skip bound
   stops starvation; a reaped holder frees its weight
   with no ledger row; pid reuse leaks nothing.
4. The estimator and the rollup verb. Per-cgroup capture
   through injected readers, the section 6.3 rules
   (median plus p90, small-sample maximum, starved
   exclusion, cold start, role fallback, clamp),
   section 6.4 feeds (ETAs, scaled deadlines, flaky
   flag), and `heavy rollup` regenerating the portable
   summary. Criteria: estimator unit tests on synthetic
   rows pin p50, p90, the young-key maximum, the starved
   exclusion and the flaky rule; a rollup golden file
   pins the schema. Blocked on the section 10 scope-exit
   probe.
5. PSI admission and doctor rows. Saturated delays,
   unknown never blocks, `[resources.pressure]` caps
   shared with the launch gate, `resources` doctor rows
   for lane health, stuck-waiter and stuck-holder rows,
   bypass counts, the corrupt-`lane.json` row.
   Criteria: saturated refuses while shares are free;
   absent files read unknown; every row renders.
6. Bypass, roles, journal surface and docs. `--bypass`
   with required reason, `--role` with the takeoff
   default through the module call, `recent[]` ring,
   coordinator skill rows with the parser-conformance
   test, and README Commands-table rows. Criteria:
   `test_readme.py` passes; a bare `--bypass` is exit 4;
   bypass rows name the reason.

Later, in order: speed calibration and cross-host
prediction (the reference workload, per-host correction,
warm-start import of a committed rollup); then
ticket-level learning, whose first ticket records the
ticket-type field and changes nothing else, and whose use
in the drive loop needs its own approval.

NOT a ticket: the shared token pool (section 2 defers
it), per-ticket priority (no ordering over project
boards exists to mirror), and any pauser or killer for
running jobs (pressure never touches holders).

## 14. Options for the maintainer

Ship the lane opt-in per project, five points now, the
pool deferred, nothing adopted wholesale.

Options:
1. Ship 1 to 6 as specified, opt-in (recommended)
2. Adopt pueue for queue and slots, lane learns on top
3. Ship without learned cost, static weights
4. Keep the takeoff flock beside the lane
5. Lane mandatory for listed suites at once
6. Drop the bypass flag, config-off only
