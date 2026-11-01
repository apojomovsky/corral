# 08: host resource governance across projects and instances

Status: proposed 2026-11-01. Design only; no code ships with this doc.

Admission today counts agents (`admission.admit`, `registry.counted_tickets`).
It knows nothing about CPU or memory, and nothing about another corral
instance on the same machine with its own state and config dirs. Test suites
and builds run by agents can saturate a small host and starve everything else
on it, including other fleets. This doc designs the budget layer that sits
next to the count layer: a host budget every instance shares, divided per
instance, per project and per agent, enforced by the kernel through nested
systemd slices.

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

The slice adds: a four-level budget (host, instance, project, agent), a
host-level ledger shared by instances, a pure budget-admission function
composed with the existing count cap and quota gate, per-role cost charges,
pressure backoff, doctor rows, and a rollout of small implementation tickets.

Non-goals: disk (the `disk.py` pressure gate already owns it), network and
GPU limits, placement across hosts (per-host budgets only; the `00-design.md`
milestone 2 peer model is untouched), several login users on one host
(explicitly out of scope, section 2), and containers as enforcement
(section 8).

## 1. Resource hierarchy

Four levels, each resolved in exactly one place (AGENTS.md, one fact one
place):

1. **Host budget.** The operator-set machine total, shared by every instance
   on the host: millicores of CPU, megabytes of memory, max pids.
2. **Instance share.** Each corral instance's portion of the host budget,
   recorded in the host ledger (section 2).
3. **Project share.** Each registered project's portion of its instance:
   a guaranteed floor plus a weight for the remainder plus an optional
   ceiling, mirroring the count floors and ceilings in `admission.py` so the
   two compose without a second mental model.
4. **Per-agent charge.** Each launch is charged by role (worker, reviewer,
   triage): a fixed CPU and memory cost, like a price tag, decided before
   anything is measured.

Enforcement maps to nested slices under the user manager:

```text
user.slice
  corral.slice                          host budget (MemoryMax, CPUQuota)
    corral-<instance>.slice             instance share
      corral-<project>.slice            project share
        corral-agent-<repo>-<n>         per-agent unit (MemoryMax, CPUQuota, TasksMax)
```

The launch path extends the existing `units.unit_argv` builder (which already
emits `--property=` lines) with `Slice=`, `MemoryMax=`, `CPUQuota=` and
`TasksMax=` for the agent unit, and creates parent slices with their own
limits first. Slice creation goes through the same injected `run` seam as
every other subprocess; tests assert on argv strings, never on a live
manager.

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

## 2. Coordination between instances

Two instances on one host have separate state and config dirs by definition,
so neither dir can hold the shared record. The ledger lives at one fixed
host path both instances derive without consulting any override:
`<xdg-state-base>/corral/host-resources.json`, where the base is the XDG
state base (`~/.local/share` by default) read straight from the environment,
ignoring `CORRAL_STATE`. This is the one deliberate exception to the override
rule, documented next to the resolver: an override-sensitive path could never
be shared, and a shared path must not move under a running fleet.

**Identity.** Each instance mints a stable random id once and keeps it in
its own config dir (`instance.id`). The resolver that reads it is the single
owner of instance identity. Collisions are not handled beyond 128 bits of
randomness; a duplicate id fails closed because two writers would share one
heartbeat slot and each would read the other's heartbeat as its own, so the
ledger refuses entries whose id already holds a live heartbeat from a
different boot nonce (each boot mints a nonce, stored next to the id only in
memory).

**Format.** Versioned JSON, mirroring the `quota_ledger.py` shape:

```json
{"version": 1, "host_budget": {"cpu_m": 8000, "memory_mb": 16000, "pids": 4096},
 "instances": {"<id>": {"cpu_m": 4000, "memory_mb": 8000, "heartbeat_at": 0.0,
   "pid": 123, "nonce": "abc", "projects": {"<name>": {"weight": 1}}}},
 "foreign": [{"name": "desktop", "cpu_m": 1000, "memory_mb": 2000, "heartbeat_at": 0.0}]}
```

**Locking.** One flock sidecar plus atomic write (`paths.atomic_write_text`),
the exact `quota_ledger.py` precedent: every read-modify-write holds the
lock, writers never leave a torn file, readers tolerate a missing file only
by creating it under the lock. A present-but-corrupt file is never
reinitialized: the launch refuses (section 6).

**Staleness and crash recovery.** Every instance refreshes `heartbeat_at` on
each admit path and on each drive pass. An entry whose heartbeat is older
than `resources.gone_s` (default 10 minutes, same scale as
`registry.gone_s`) is dead and its share returns to the pool. A dead pid
(`kill(pid, 0)` failing, the `registry.pid_alive` precedent) plus any stale
heartbeat reclaims immediately; a live pid with a stale heartbeat still waits
out the bound, because a paused event loop is not a dead instance. No manual
removal exists: unlike the `00-design.md` cross-host peers, same-host
instances share a kernel, so liveness is checkable and expiry is automatic.

**Peer disappears without releasing.** It cannot hold anything: expiry is by
time and pid liveness, not by a release message. The worst case is a share
reserved until `gone_s` passes, bounded and visible in the doctor row.

**Same user only.** Several login users on one host are out of scope. The
user manager, the delegation and the `$HOME`-relative ledger path are all
per-user; spanning users would need system slices and root, which corral
never touches (`make bootstrap` never uses sudo). Two users running corral on
one host today simply do not see each other; the foreign-claim mechanism
(section 4) is the only bridge offered, declared by hand.

## 3. Admission by budget, not by count

A pure function `resources.admit`, shaped like `admission.admit`: plain data
in, a verdict with a reason string out. Inputs are the host budget, the live
ledger shares, the project weights with floors and ceilings, the role costs,
and the charged usage (the sum of role costs of live agents, from the same
`registry.visible` plus `units` listing the count gate already reads).
Charged usage is static by design: measured use is noisy and gameable, so
measurement informs the doctor and the snapshot while admission prices from
the tag.

Fair share between projects: each project is guaranteed its floor; the
remainder of the instance share splits by weight; a ceiling caps even an idle
project's neighbor from taking more. A project below its floor is admitted
past a full pool, exactly like the count floor in `admission.admit`.

Priorities: project weights plus role costs only. No per-ticket priority in
version one: it would need a second ordering over tickets that projects own
(the board's), and the count layer has no such concept to mirror.

Queueing versus refusal: refusal with exit 75, no queue. A queue would need a
waiter, a wakeup and an ordering across headless processes that never meet;
exit 75 already means "no free slot, try later" and the drive pass retries.
The verdict reason names the level that refused (host, instance, project or
role cost) with the numbers, the same way `admission.Verdict.reason` does.

Composition with the count cap and the quota gate: `launch.admit` evaluates
in this order and the first refusal wins: disk pressure, agent count,
resource budget, provider quota. Count and budget both refuse with 75 and
each reason names its gate, so callers branching on the code see one
contract while humans reading stderr see which limit hit. Config errors in
any layer stay exit 4 and are evaluated before any gate.

## 4. Foreign load

Load corral does not control (another fleet, the desktop) is measured, never
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
from the pool exactly like an instance share and expire under the same
`gone_s` bound; an expired foreign claim is ignored with a doctor warn row,
never held against the pool. The claim is advisory and self-reported:
corral cannot verify it, so it trusts the declarer and shows the numbers in
the doctor row for a human to audit. The write channel is an open decision
(section 10, O5).

## 5. Settings

One `[resources]` surface, resolved by `settings.resolve` on the existing
`[models]` and `[private]` precedent (strict keys, exit 4 naming the file
and the key):

```toml
[resources.host]
cpu_m = 8000            # millicores for every instance together
memory_mb = 16000
pids = 4096

[resources.instance]
weight = 1              # this instance's share weight against its peers

[resources.project]     # in projects.d/<name>.toml, beside floor/ceiling
cpu_floor_m = 500
cpu_weight = 1
memory_floor_mb = 1024

[resources.role.worker] # per-agent charge
cpu_m = 1000
memory_mb = 2048
[resources.role.reviewer]
cpu_m = 500
memory_mb = 1024
```

Plus launch flags for one-off overrides: `--cpu-m` and `--memory-mb`,
validated positive integers, exit 4 otherwise. The stopgap `--slice` flag
(section 9) is the only other flag; it is removed when the managed hierarchy
lands.

Defaults: no host budget configured means no enforcement. The budget gate
passes everything and the doctor warns that host limits are unset. This is
the one place the design does not fail closed, deliberately: every existing
single-instance host runs today with no budget configured, and inventing a
cap from machine totals would either throttle working hosts or pretend to
protect them. Enforcement starts the moment the operator sets
`[resources.host]`; from then on every failure in section 6 refuses.

Validation (exit 4, each message naming the offending numbers): negative
charges or budgets; a floor above its ceiling; floors summing above the
instance share or shares above the host budget (the
`admission.validate_limits` precedent); unknown role names; a weight of zero
with a positive floor is allowed (floor only, no share of the remainder).

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
| Ledger present but corrupt, or version newer than this corral | Refuse, stderr naming the file | 4 |
| User manager bus unreachable | Refuse, existing `Unreachable` path | 3 |
| Host, instance or project budget exhausted, or host saturated | Refuse, reason with the numbers | 75 |
| Ledger lock busy past a short timeout | Refuse, try later | 75 |
| Pressure or slice-use file unreadable | Reads unknown; admission continues on charged tags, doctor warns | 0, with notes |
| Foreign claim expired | Ignored, doctor warns | 0, with notes |

Destructive seams keep required implementations: the AGENTS.md rule holds for
any future reaper (a pressure-triggered pauser or killer takes its stop
function as a required argument and fails closed without one). This design
adds no pauser or killer; backoff applies to launches only.

## 7. Testing

No test touches the real host (AGENTS.md): no systemd, no cgroup
filesystem, no ledger on disk outside a temp dir. One injected seam per
external touch, mirroring the existing seams:

| Behavior | Seam (fake) |
| --- | --- |
| `unit_argv` carries `Slice=`, `MemoryMax=`, `CPUQuota=`, `TasksMax=`; unknown controller skipped | pure argv builder, string assertions |
| Slice creation order (parents before children) and property-refusal mapping | fake `run` recording argv |
| Pure `resources.admit`: weights, floors, ceilings, role costs, foreign subtraction, staleness bounds | plain data, no seam |
| Ledger read-modify-write under lock; corrupt file refused, never reset; dead-peer expiry by time and pid | temp-dir store path plus fake `now` and fake `alive` (the `registry` precedent) |
| Controller delegation parse; pressure parse with absent files reading unknown | fake file readers returning fixture text |
| Doctor `resources` rows for each failure in section 6 | fake readers plus temp ledger |
| Guard: no test reaches a real `systemd-run`, `systemctl`, `/proc` or `/sys` | the existing `suiteguard.py` plus a test asserting the new module never imports a live seam |

## 8. Containers

Resource limits do not need containers. The kernel already enforces CPU,
memory and pids through cgroups, and systemd slices compose with the
transient units corral already starts; a container would add image builds,
layer storage, a daemon dependency and a second identity scheme for no new
enforcement primitive. Docker stays what it is in corral: a seam for
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
2. Settings surface. The `[resources]` tables through `settings.resolve`
   with strict keys, plus `corral.resources` in the provider list.
   Criteria: the documented tables load; every validation in section 5 is a
   `SettingsError` naming file and key; defaults leave current behavior
   byte-identical.
3. Host ledger. Identity, format, flock plus atomic write, heartbeat,
   expiry and crash recovery. Criteria: the section 7 ledger rows pass;
   a corrupt file refuses and is never reset; a dead peer's share returns.
4. Launch wiring. Slice creation order, `unit_argv` properties, admit
   composition order, every section 6 exit code. Criteria: fake-`run` tests
   assert the argv; each failure row produces its exit code.
5. Doctor rows. Delegation, ledger health, pressure, foreign claims.
   Criteria: each failure in section 6 renders its named row.
6. Snapshot and registry carriage. Charged versus measured columns per
   agent, project and instance. Criteria: snapshot JSON gains stable keys
   with golden-file tests.
7. Foreign claims and pressure backoff. Claim channel from O5 plus the
   saturation refusal. Criteria: an expired claim frees the pool with a
   warn row; saturation refuses with 75 while shares are free.
8. Docs and README. Commands-table rows for any new verb or flag,
   settings documentation, removal of the section 9 stopgap. Criteria:
   `test_readme.py` passes; the stopgap flag is gone without a leftover.

Manual stopgap, replaced by the rollout: one slice unit file plus one
launch flag. The operator installs
`~/.config/systemd/user/corral-cap.slice` with `MemoryMax=` and `CPUQuota=`
under `[Slice]`, enables linger so it persists, and launches with
`corral launch ... --slice corral-cap.slice`, a passthrough flag validated
to end in `.slice` (exit 4 otherwise) that appends one `--slice=` line to
the `systemd-run` argv. It caps the aggregate coarsely with no per-project
fairness and no cross-instance view; tickets 1 to 4 replace it.

## 10. Open decisions

Options blocks, one per decision, each line under 60 characters:

O1. Ledger location:
Options:
1. Fixed `$HOME` path ignoring overrides (recommended)
2. Next to the state dir with a symlink farm
3. XDG_RUNTIME_DIR with rebuild on boot
4. One instance owns it, peers ask over its API

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
2. New `corral resources` verb family
3. Environment variables on launch
4. Hand-edited ledger section

O6. Per-ticket priority in version one:
Options:
1. None, weights plus role costs only (recommended)
2. Board rank maps to budget priority
3. Launch flag names a priority class
4. Project policy hook decides order

