# 09: host resource governance across projects on one host

Status: approved 2026-11-01, re-scoped after review round 1 to one
korapy per machine. Implemented by the tickets in section 9.

Admission today counts agents (`admission.admit`, `registry.counted_tickets`).
It knows nothing about CPU or memory. Test suites and builds run by agents
can saturate a small host and starve everything else on it, including other
fleets. This doc designs the budget layer that sits next to the count layer:
a host budget divided per project and per agent, enforced by the kernel
through nested systemd slices.

One korapy per machine: one state directory, one host-wide ledger that every
project on the host registers to. Coordination between separate state
directories on the same host is not a goal; a guard refuses a second
instance pointed at a different state directory (section 2). The host is the
only authority on its CPU and memory. A hub (separate design) never admits
on a host's behalf; it only learns the host's capacity and load through a
read-only capacity report (section 2), and it never writes the ledger.

It decides the nine points the ticket lists, with reasoning. Where it changes
a public surface (a setting, a launch flag, a CLI verb) it says so;
implementation waits for approval. Claims about systemd or kernel behavior
that were not verified on a live host are marked UNVERIFIED, each with a safe
read-only command that settles it.

Verified on one host while writing: the machine exposes
`cpuset cpu io memory hugetlb pids rdma misc dmem` at
`/sys/fs/cgroup/cgroup.controllers`, while the login user's slice
(`user-<uid>.slice`) delegates only `cpu memory pids`, and
`systemctl --user show` answers. Generalizing beyond that host is UNVERIFIED.

## 0. Scope and non-goals

The slice adds: a three-level budget (host, project, agent), a host-level
ledger keyed by project, a single-instance guard, a pure budget-admission
function composed with the existing count cap and quota gate, per-role cost
charges, pressure backoff, a read-only capacity report for the hub, doctor
rows, and a rollout of small implementation tickets.

Non-goals: disk (the `disk.py` pressure gate already owns it), network and
GPU limits, placement across hosts (per-host budgets only; the `00-design.md`
milestone 2 peer model is untouched), several login users on one host
(explicitly out of scope, section 2), and containers as enforcement
(section 8).

## 1. Resource hierarchy

Three levels, each resolved in exactly one place (AGENTS.md, one fact one
place):

1. **Host budget.** The operator-set machine total: millicores of CPU,
   megabytes of memory, max pids.
2. **Project share.** Each registered project's portion of the host budget:
   a guaranteed floor plus a weight for the remainder plus an optional
   ceiling, mirroring the count floors and ceilings in `admission.py` so the
   two compose without a second mental model.
3. **Per-agent charge.** Each launch is charged by role (worker, reviewer,
   triage): a fixed CPU and memory cost, like a price tag, decided before
   anything is measured.

Enforcement maps to nested slices under the user manager:

```text
user.slice
  korapy.slice                          host budget (MemoryMax, CPUQuota)
    korapy-<project>.slice              project share
      korapy-agent-<repo>-<n>           per-agent unit (MemoryMax, CPUQuota, TasksMax)
```

The launch path extends the existing `units.unit_argv` builder (which already
emits `--property=` lines) with `Slice=`, `MemoryMax=`, `CPUQuota=` and
`TasksMax=` for the agent unit, and starts the host slice and the project
slice with their own limits first. Slice creation goes through the same
injected `run` seam as every other subprocess; tests assert on argv strings,
never on a live manager.

Verified on one host while fixing slice creation (systemd 255, user
manager): slices are created with `systemctl --user start <name>.slice`
(exit 0 fresh and already active) and limited with `systemctl --user
set-property --runtime <name>.slice MemoryMax=512M CPUQuota=50%` (exit 0;
`show` then reports `MemoryMax=536870912`,
`CPUQuotaPerSecUSec=500ms`). A dashed name nests by itself: starting
`X-child.slice` activates `X.slice` and `show` reports `Slice=X.slice`,
so project slices need no parent flag. A refused assignment answers exit 1
(`Unknown assignment: BogusProp=1`, mapped to exit 4). The earlier
`systemd-run --user --unit=<x>.slice --remain-after-exit /bin/true` form
fails with `Cannot set property RemainAfterExit, or unknown property`,
because a slice unit cannot carry that service-only property. Probe units
were created and stopped in the same step; the real slice names stayed
untouched.

Controllers: **required** are `cpu`, `memory` and `pids`; **optional** are
`cpuset` and `io`, which are often not delegated to the user manager (on the
one host checked while writing, the user slice delegates only `cpu memory
pids`). When an optional controller is absent the design skips its properties
and the doctor reports them as unavailable, never as a failure. When a
required controller is absent the launch refuses (section 6).

UNVERIFIED: that a nested slice's `MemoryMax=` caps the aggregate of its
children rather than each child separately; learn with
`systemd-run --user --slice=test-cap.slice --property=MemoryMax=1G ...`
on a scratch host and read `memory.peak` under each leaf. UNVERIFIED: the
exact `systemctl --user show` property that reports delegated controllers for
a slice; learn with `systemctl --user show -p Controllers user.slice` and
`cat` on the slice's `cgroup.controllers` file. UNVERIFIED: whether
`CPUQuota=` or `CPUWeight=` is the better share mechanism on this kernel;
learn with `man systemd.resource-control`.

Doctor checks delegation by reading the user slice's `cgroup.controllers`
file through an injected reader (a path seam, faked in tests): every required
controller present reads ok, a missing optional one reads as unavailable, a
missing required one fails the `resources` row and every launch refuses until
it returns.

## 2. One ledger, one instance, keyed by project

Every project on the host registers to a single host-wide ledger. There is
no per-instance layer: no instance shares, no instance weights, no
cross-instance heartbeats, no peer expiry.

**Location.** The ledger lives at
`<xdg-state-base>/korapy/host-resources.json`, resolved by a new
`paths.host_ledger_path()` helper built on
`_xdg_base("XDG_STATE_HOME", "~/.local/state", env)` on the `config_home()`
precedent: the XDG state base directly, never `KORAPY_STATE`, and never read
straight from the environment by any caller. This is the one deliberate
exception to the override rule, documented next to the resolver: an
override-sensitive path could never be shared, and a shared path must not
move under a running fleet. A test asserts the copies agree: the helper is
the only resolver, and its result matches the documented location even with
`KORAPY_STATE` set.

**Guard.** A canonical state-directory marker sits beside the ledger, in the
same directory: a small file holding the state dir that created this host's
ledger. It is written once, under the ledger lock, when the ledger is first
created. Every launch resolves `paths.state_dir()` and compares it to the
marker; a launch against a different state directory on the same host refuses
with exit 4, stderr naming both dirs and pointing at the marker. The marker
is never updated by a launch: moving a host to a new state dir is an operator
action (stop the fleet, move the marker by hand, start again), not something
two fleets negotiate.

**Format.** Versioned JSON, mirroring the `quota_ledger.py` shape, keyed by
project:

```json
{"version": 1, "host_budget": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
 "projects": {"<name>": {"weight": 1, "cpu_floor_m": 500, "memory_floor_mb": 1024}},
 "foreign": [{"name": "desktop", "cpu_m": 1000, "memory_mb": 2000, "claimed_at": 0.0}]}
```

**Locking.** One flock sidecar plus atomic write (`paths.atomic_write_text`),
the exact `quota_ledger.py` precedent: every read-modify-write holds the
lock, writers never leave a torn file, readers tolerate a missing file only
by creating it under the lock. A present-but-corrupt file is never
reinitialized: the launch refuses (section 6). The quota ledger's lock blocks
without a timeout, so the ledger ticket (rollout 3) adds a timed variant: a
non-blocking flock with a retry loop against a monotonic deadline; a lock
still busy past the timeout refuses with exit 75.

**Staleness and crash recovery.** There is nothing to expire: charged usage
is recomputed from the live registry on every admit (section 3), so a crash
holds no share and recovery is automatic. The only time-bounded entries are
foreign claims, which expire under `resources.gone_s` (default 10 minutes,
same scale as `registry.gone_s`); an expired foreign claim is ignored with a
doctor warn row, never held against the pool.

**Same user only.** Several login users on one host are out of scope. The
user manager, the delegation and the `$HOME`-relative ledger path are all
per-user; spanning users would need system slices and root, which korapy
never touches (`make bootstrap` never uses sudo). Two users running korapy on
one host today simply do not see each other; the foreign-claim mechanism
(section 4) is the only bridge offered, declared by hand.

**Capacity report (read-only, for the hub).** The hub learns capacity and
load through one stable shape, never by reading or writing the ledger
itself:

```json
{"schema_version": 1, "host_budget": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
 "charged": {"cpu_m": 2500, "memory_mb": 5120, "pids": 120},
 "by_project": {"<name>": {"cpu_m": 2000, "memory_mb": 4096, "pids": 100}},
 "pressure": {"load_per_cpu": 0.4, "mem_available_pct": 55.0, "saturated": false}}
```

`charged` and `by_project` sum the static role tags of live agents;
`pressure` carries the measured readings from section 4. The hub never
writes the ledger and never admits on the host's behalf; the report shape is
kept compatible with the multi-host hub design by versioning it
(`schema_version`) and adding keys only.

## 3. Admission by budget, not by count

A pure function `resources.admit`, shaped like `admission.admit`: plain data
in, a verdict with a reason string out. Inputs are the host budget, the
project shares with floors, weights and ceilings, the role costs, the foreign
claims, and the charged usage (the sum of role costs of live agents, from the
same `registry.visible` plus `units` listing the count gate already reads).
Charged usage is static by design: measured use is noisy and gameable, so
measurement informs the doctor and the snapshot while admission prices from
the tag.

Fair share between projects: each project is guaranteed its floor; the
remainder of the host budget splits by weight; a ceiling caps even an idle
project's neighbor from taking more. A project below its floor is admitted
past a full pool, exactly like the count floor in `admission.admit`.

Priorities: project weights plus role costs only. No per-ticket priority in
version one: it would need a second ordering over tickets that projects own
(the board's), and the count layer has no such concept to mirror.

Queueing versus refusal: refusal with exit 75, no queue. A queue would need a
waiter, a wakeup and an ordering across headless processes that never meet;
exit 75 already means "no free slot, try later" and the drive pass retries.
The verdict reason names the level that refused (host, project or role cost)
with the numbers, the same way `admission.Verdict.reason` does.

Composition with the count cap and the quota gate: `launch.admit` evaluates
in this order and the first refusal wins: disk pressure, agent count,
resource budget, provider quota. Count and budget both refuse with 75 and
each reason names its gate, so callers branching on the code see one
contract while humans reading stderr see which limit hit. Config errors in
any layer stay exit 4 and are evaluated before any gate.

## 4. Foreign load

Load korapy does not control (another fleet, the desktop) is measured, never
managed. Two readings, each through an injected reader seam faked in tests:

- **Host pressure:** `/proc/loadavg` and `/proc/meminfo` (`MemAvailable`),
  plus the pressure-stall files `/proc/pressure/cpu` and
  `/proc/pressure/memory` when the kernel exposes them (UNVERIFIED on any
  particular host: needs cgroup v2 and a recent kernel; learn with
  `cat /proc/pressure/cpu || echo absent`). A missing file reads as unknown,
  and unknown never blocks (the `disk.py` precedent).
- **Per-slice use:** the leaf `cpu.stat`, `memory.current` and `pids.current`
  under the user slice, for the doctor and the snapshot only.

Backoff: a `saturated` pressure reading turns the budget verdict into a
refusal (exit 75, reason naming the pressure source) even when shares are
free. Saturation is a threshold over load per CPU and `MemAvailable` percent,
both settings with conservative defaults. This is backoff, not preemption:
running agents are never paused or killed by pressure; only new launches
wait.

An external fleet can declare a claim on the ledger: a `foreign` entry with
a name and an explicit CPU and memory reservation. Foreign claims subtract
from the pool exactly like a project share and expire under the same
`gone_s` bound; an expired foreign claim is ignored with a doctor warn row,
never held against the pool. The claim is advisory and self-reported:
korapy cannot verify it, so it trusts the declarer and shows the numbers in
the doctor row for a human to audit. The write channel is an open decision
(section 10, O5).

## 5. Settings

One `[resources]` surface, resolved by `settings.resolve` on the existing
`[private]` precedent (strict keys, exit 4 naming the file
and the key):

```toml
[resources.host]
cpu_m = 8000            # millicores for every project together
memory_mb = 16000
pids = 4096

[resources]     # in projects.d/<name>.toml, beside floor/ceiling
cpu_floor_m = 500
weight = 1
memory_floor_mb = 1024

[resources.role.worker] # per-agent charge
cpu_m = 1000
memory_mb = 2048
[resources.role.reviewer]
cpu_m = 500
memory_mb = 1024
```

There is no `[resources.instance]` table and no instance weight: with one
korapy per machine the host budget divides directly between projects.

Defaults: no host budget configured means no enforcement. The budget gate
passes everything and the doctor warns that host limits are unset. This is
the one place the design does not fail closed, deliberately: every existing
single-instance host runs today with no budget configured, and inventing a
cap from machine totals would either throttle working hosts or pretend to
protect them. Enforcement starts the moment the operator sets
`[resources.host]`; from then on every failure in section 6 refuses.

Validation (exit 4, each message naming the offending numbers): negative
charges or budgets; a floor above its ceiling; floors summing above the host
budget (the `admission.validate_limits` precedent); unknown role names; a
weight of zero with a positive floor is allowed (floor only, no share of the
remainder).

README impact: none in this design (no verb, flag or config file ships with
a doc). Each implementation ticket that adds surface updates `README.md` in
the same PR per the freshness rule; the final rollout ticket owns the
`resources` rows in the Commands table and the settings documentation.

## 6. Failure modes

Fail closed: when enforcement cannot be proven, the launch refuses rather
than running unlimited.

| Failure | Behavior | Exit |
| --- | --- | --- |
| Manager refuses a property, or a required controller is not delegated | Refuse, stderr naming the property or controller and the doctor row to run | 4 |
| Ledger present but corrupt, or version newer than this korapy | Refuse, stderr naming the file | 4 |
| State dir differs from the marker beside the ledger | Refuse, stderr naming both dirs and the marker | 4 |
| User manager bus unreachable | Refuse, existing `Unreachable` path | 3 |
| Host or project budget exhausted, or host saturated | Refuse, reason with the numbers | 75 |
| Ledger lock busy past a short timeout | Refuse, try later | 75 |
| Pressure or slice-use file unreadable | Reads unknown; admission continues on charged tags, doctor warns | 0, with notes |
| Foreign claim expired | Ignored, doctor warns | 0, with notes |

Destructive seams keep required implementations: the AGENTS.md rule holds for
any future reaper (a pressure-triggered pauser or killer takes its stop
function as a required argument and fails closed without one). This design
adds no pauser or killer; backoff applies to launches only.

Changed limits reconcile on the next launch. Slice creation starts the
slice (a no-op when already active) and re-applies its limits as runtime
properties, so after the operator changes a number the admission gate
and the kernel enforcement agree again once an agent of that project
launches; between the change and that launch the doctor drift row warns.
Clearing a budget (unsetting `[resources.host]`) applies nothing: stale
limits stay until the operator stops the slice by hand.

## 7. Testing

No test touches the real host (AGENTS.md): no systemd, no cgroup
filesystem, no ledger on disk outside a temp dir. One injected seam per
external touch, mirroring the existing seams:

| Behavior | Seam (fake) |
| --- | --- |
| `unit_argv` carries `Slice=`, `MemoryMax=`, `CPUQuota=`, `TasksMax=`; unknown controller skipped | pure argv builder, string assertions |
| Slice creation argv per shape (host start, project start, runtime properties, parents before children) and the exists/unreachable/refused buckets | fake `run` recording argv |
| Pure `resources.admit`: weights, floors, ceilings, role costs, foreign subtraction, staleness bounds | plain data, no seam |
| Ledger read-modify-write under the timed lock; corrupt file refused, never reset; guard refuses a foreign state dir | temp-dir store path plus fake `now` and a fake monotonic clock for the lock deadline |
| `paths.host_ledger_path()` is the only ledger resolver; result matches the documented location with overrides set | env-matrix unit test (the copies-agree rule) |
| Controller delegation parse; pressure parse with absent files reading unknown | fake file readers returning fixture text |
| Capacity report shape: budget, charged totals, pressure, schema version; hub path never writes | golden-file test plus a write-attempt refusal test |
| Doctor `resources` rows for each failure in section 6 | fake readers plus temp ledger |
| Guard: no test reaches a real `systemd-run`, `systemctl`, `/proc` or `/sys` | the existing `suiteguard.py` plus a test asserting the new module never imports a live seam |

## 8. Containers

Resource limits do not need containers. The kernel already enforces CPU,
memory and pids through cgroups. UNVERIFIED: that systemd slices compose
with the transient units korapy already starts; learn on a scratch host
with `systemd-run --user --slice=test-gov.slice
--property=MemoryMax=1G ...` and read the leaf `cgroup.controllers`.
A container would add image builds,
layer storage, a daemon dependency and a second identity scheme for no new
enforcement primitive. Docker stays what it is in korapy: a seam for
placement (attached hardware, a daemon a project needs), not for limits.

Containers belong when the need is isolation rather than accounting: code
that must not see the host filesystem, a userland the host does not have, or
a host with no systemd user manager to enforce slices. None of those is this
ticket; if one arrives it is a new design with its own threat model, not an
extension of this one.

## 9. Rollout

Ordered, each small enough for one agent, each with acceptance criteria.
Later tickets build on earlier ones; none lands without the tests in
section 7.

1. Pure budget core. `resources.admit` plus validation, shaped like
   `admission.py`. Criteria: the section 7 pure rows pass; floors, ceilings,
   weights, role costs and foreign subtraction each have a boundary test.
2. Settings surface. The `[resources]` settings keys through
   `settings.resolve` with strict keys; settings keys only, no CLI
   surface here (any CLI surface is decided with O5 and lands in
   ticket 7).
   Criteria: the documented tables load; every validation in section 5 is a
   `SettingsError` naming file and key; defaults leave current behavior
   byte-identical.
3. Host ledger and guard. Marker file, versioned format, timed lock, atomic
   write: a `_locked` helper using non-blocking flock with a retry loop
   against a monotonic deadline (the quota ledger's blocking lock has no
   timeout and is not reused). Criteria: the section 7 ledger rows pass; a
   corrupt file refuses and is never reset; a foreign state dir refuses with
   exit 4; `paths.host_ledger_path()` is the only resolver and agrees with
   the documented location under an env matrix.
4. Launch wiring. Slice creation order, `unit_argv` properties, admit
   composition order, every section 6 exit code. Criteria: fake-`run` tests
   assert the argv; each failure row produces its exit code.
5. Doctor rows. Delegation, ledger health, guard marker, pressure, foreign
   claims. Criteria: each failure in section 6 renders its named row.
6. Snapshot, registry carriage and capacity report. Charged versus measured
   columns per agent and per project, plus the read-only report from
   section 2. Criteria: snapshot JSON gains stable keys with golden-file
   tests; the capacity report carries its schema version; a test proves the
   hub path cannot write the ledger.
7. Foreign claims and pressure backoff. Claim channel from O5 plus the
   saturation refusal. Criteria: an expired claim frees the pool with a
   warn row; saturation refuses with 75 while shares are free.
8. Docs and README. Commands-table rows for any new verb or flag,
   settings documentation, removal of the section 9 stopgap. Criteria:
   `test_readme.py` passes; the stopgap flag is gone without a leftover.

## 10. Open decisions

Options blocks, one per decision, each line under 60 characters:

O1. Ledger location:
Options:
1. paths.host_ledger_path(), fixed file (recommended)
2. Next to the state dir with a symlink farm
3. XDG_RUNTIME_DIR with rebuild on boot
4. One project owns it, peers ask over its API

O2. Share model between projects:
Options:
1. Floor plus weight plus ceiling (recommended)
2. Explicit static caps per project
3. Pure weights with no floors
4. First-come queue with waiting list

O3. Host budget default when unset:
Options:
1. No enforcement plus doctor warn (recommended)
2. Refuse all launches until set
3. Auto-detect machine totals minus reserve
4. Copy the count cap ratio into millicores

O4. Queueing versus refusal:
Options:
1. Refuse with exit 75, drive retries (recommended)
2. Bounded wait queue with timeout
3. Priority preemption of running agents
4. Overcommit with pressure killing

O5. Foreign claim write channel:
Options:
1. Drop-in JSON dir merged at read (recommended)
2. New `korapy resources` verb family
3. Environment variables on launch
4. Hand-edited ledger section

O6. Per-ticket priority in version one:
Options:
1. None, weights plus role costs only (recommended)
2. Board rank maps to budget priority
3. Launch flag names a priority class
4. Project policy hook decides order
