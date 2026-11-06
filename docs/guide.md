# Operator's guide: running korapy on one host

This guide takes you from an empty Linux host to a fleet of headless
agents driven by `korapy drive`, and shows how to coordinate that fleet
day to day. Every command below was checked against the real parsers
(`tests/test_guide.py` parses each shell block), and every sample
output was produced by the real tool in a temporary state directory.
Paths and hostnames in outputs stand for your own: where you see
`/tmp/EXAMPLE`, read the path from your own `korapy paths`.

## What korapy is

korapy is an opinionated core for running a fleet of headless coding
agents on one Linux host. It launches agents as systemd user units in
one worktree per ticket, keeps their sessions, routes their models
around exhausted quota, admits them under one host cap and budget,
paces their GitHub calls, and cleans up branches, worktrees, caches
and sessions only once it can show nothing is lost. It is not a
project manager: your project brings its own board, eligibility rules,
prompts and policies. It is Linux only, GitHub only (behind the
tracker and forge interfaces), headless runtimes only, and one major
version of each runtime at a time.

The host programs korapy needs are checked by one verb:

```sh
korapy deps
```

What you should see is one `ok` line per program, or the install
command for what is missing:

```text
ok  uv         0.13.5   runs korapy, its Python and its tools
ok  git        2.43.0   clones, worktrees and branches, one worktree per ticket
ok  make       4.3      the repository's entry points
ok  curl       8.5.0    the installers below
ok  gh         2.102.0  GitHub: issues, pull requests, boards
ok  docker     29.9.0   agent toolchains, and image pruning by the host keeper
ok  systemd    255      agents run as transient systemd --user units
ok  linger     yes      user units keep running after you log out
ok  omp        18.14.0  agent runtime (major 18 until the cutover to 19)
ok  muse       1.4.14   agent runtime (major 1 until the cutover to 2)
```

Ten programs are required. An eleventh, `tailscale`, matters only for
multi-host setups: any network that gives each host an address works.
`make bootstrap` (needs only `uv`) installs the Python, the locked
environment and the git hooks, then runs this check. It never uses
sudo: for anything missing it prints the commands to run yourself and
exits 1.

Install from a checkout:

```sh
make install
```

That runs `uv tool install --reinstall .`, which puts `korapy` on your
PATH. Re-run it to upgrade.

## First run on a host

Start with the read-only health report:

```sh
korapy doctor
```

What you should see is one row per check, `ok`, `warn` (`!`) or `fail`
(`✗`), each failure naming its fix:

```text
✓ deps: 10 required programs present
✓ accounts: the accounts file is usable
✓ settings: the host config and korapy.toml resolve
✓ state-dir: the state and config dirs are writable
✓ linger: linger is on for LOGIN
✓ user-manager: the user manager answers (0 units)
✓ drive-timer: the user timer korapy-drive.timer is enabled
✓ drive-projects: every registration loads and every policy entry resolves
✓ quota-readers: every configured reader loads
✓ registry: 0 entries (live=0, stale=0, finished=0)
✓ prompts: the role prompts are built-in or valid overrides
✓ disk: disk pressure ok
! resources: no host budget configured: the budget gate passes everything and launches set no slice or limit properties
  fix: set [resources.host] in CONFIG/config.toml to enforce budgets, then run `korapy doctor` again
✓ resources-ledger: the host ledger reads (0 projects, 0 foreign claims)
✓ resources-marker: no ledger marker yet: the next launch records STATE
✓ resources-pressure: host pressure readings are available
✓ resources-foreign: no foreign claims
✓ profiles: no profiles declared
✓ profile-runtimes: no profile references a runtime
```

`doctor` exits 0 when every row is ok or warn, 1 on a failing check,
and 3 when the user manager bus is gone. Each registered board repo
adds one `board:REPO` row. `korapy doctor --json` prints the same
report as one document. Read `✓` as healthy, `!` as usable but open
(the unset host budget above is the normal first-run warning), and
`✗` as stop and run the `fix:` line. On a host where the timer is
not installed yet, the `drive-timer` row reads warn (`not installed`)
instead of ok; it turns ok after `korapy drive install-timer` below.

Know where korapy keeps things:

```sh
korapy paths --mkdir
korapy paths state
korapy config show
```

What you should see from the first command:

```text
state = /tmp/EXAMPLE/state
config = /tmp/EXAMPLE/config
cache = /tmp/EXAMPLE/cache
sessions = /tmp/EXAMPLE/state/sessions
```

The state dir holds ledgers, the board store and logs; the config dir
holds the host config, the accounts file and the project
registrations; the cache dir holds scratch files such as PR body
drafts; sessions holds one directory per agent session.
`KORAPY_STATE`, `KORAPY_CONFIG`, `KORAPY_CACHE` and `KORAPY_SESSIONS`
move them, and must name absolute paths (a relative value is refused).
Unset, they follow the XDG base directories under `~/.local/state`,
`~/.config` and `~/.cache`. One deliberate exception: the host-wide
resource ledger ignores `KORAPY_STATE` and lives under `XDG_STATE_HOME`
alone, so every project on the host shares one pool. A marker beside
it records which state dir owns this host, and a launch from another
state dir refuses with exit 4, so two fleets never share a host
unknowingly. Move state only while no fleet runs on the host:

```sh
KORAPY_STATE=/srv/korapy/state korapy paths
```

Next, the accounts file. Every project on the host shares one pool,
in the config dir as `accounts.toml`. It names providers and the
reader that knows each provider's quota format. The reader is the only
provider-specific code, and it lives in your ops directories, never in
korapy (korapy ships none):

```toml
# ~/.config/korapy/accounts.toml
reader_path = ["~/ops-root/readers"]
[[provider]]
name = "provider-a"
reader = "my_tools.quota:ProviderA"
```

Each `reader_path` entry must be absolute after `~` and environment
expansion; korapy puts the entries on `sys.path` only while a reader
loads. Reader modules are trusted code that loads in process, so only
install readers you trust. With no file, the pool is empty:

```sh
korapy quota
korapy quota refresh
korapy quota refresh PROVIDER
korapy quota mark PROVIDER --until UNTIL --reason TEXT
korapy quota unmark PROVIDER --reason TEXT
```

What you should see from `korapy quota` before any provider exists is
one line: `no providers`. `refresh` runs the readers and records
readings or failures. `mark` blocks a provider until the given epoch
seconds (default: now plus the cooldown); `unmark` releases it early.
A dead reader shows in `korapy quota` and the doctor but does not stop
the fleet.

Then the host budget. With no `[resources.host]` table nothing is
enforced: launches set no slice properties and the doctor warns, as in
the first-run output above. Once the table is set, every launch is
charged by its role and the kernel enforces the limits through nested
slices:

```toml
# ~/.config/korapy/config.toml
[resources.host]
cpu_m = 8000
memory_mb = 16000
pids = 4096

[resources.role.worker]
cpu_m = 1000
memory_mb = 2048
pids = 256

[resources.role.reviewer]
cpu_m = 500
memory_mb = 1024
pids = 128

[resources.role.triage]
cpu_m = 250
memory_mb = 512
pids = 64

[resources.pressure]
max_load_per_cpu = 2.0
min_mem_available_pct = 10.0
```

The host table needs all three dimensions. A role with no charge table
is refused at launch with exit 4. An exhausted budget or share refuses
the launch with exit 75 and the numbers that refused it. Past the
`[resources.pressure]` saturation line, new launches refuse with exit
75 even with shares free; running agents are never paused or killed by
pressure. Each project's registration may add a `[resources]` share
beside its count floor and ceiling: a `weight` splitting what remains
after every floor, per-dimension floors guaranteed even past a full
pool, and optional ceilings. An outside fleet declares its own load
with one JSON file per claim in the `foreign-claims.d/` directory
beside the host ledger; claims expire after `resources.gone_s`
seconds (default 600).

## Registering a project

A project is an operations directory holding `korapy.toml`, plus one
host registration per host. In the inline layout the ops directory is
the project clone itself. In the split layout the agent side lives in
a separate ops root (`korapy init --ops PATH`) and the clone is
touched only through `core.hooksPath` and `.git/info/exclude`.
Register from the clone:

```sh
korapy init --plan
korapy init
```

`--plan` prints the diff and writes nothing. `init` detects the
project, writes `korapy.toml`, `agents.overlay.md` and the hooks, and
registers the host. `--force` replaces an existing `korapy.toml` or
registration with different content, `--no-register` skips the
registration, and `--set key=value`, `--runtime`, `--cap` and
`--test-cmd` override detection. `korapy config show --origin` names
the layer every setting came from. What you should see from `--plan`
is the entry `init` would append: one `[[repo]]` block naming the
tracker repo, the clone path and the checks.

```toml
# korapy.toml, in the ops root
[[repo]]
name = "app"

[repo.clone]
path = "."
default_branch = "main"

[repo.checks]
test = "make test"

[repo.tracker]
repo = "example-owner/app"
variant = "labels"
```

`name` is the entry `launch`, `tail` and `stop` take as REPO.
`clone.path` is the main clone relative to the ops root;
`clone.default_branch` defaults to `main`. `checks.test` is the test
command the agent and takeoff run. `tracker.repo` is the
`owner/name` the adapter talks to (detected from the clone's origin
remote when unset); `tracker.variant` is `labels` or `board`. A board
entry adds a `[repo.board]` table mapping the project board, written
by `korapy github setup`, never by hand.

Profiles select the runtime and model chains per ticket and role:

```toml
[profile]
default = "default"

[profiles.default]
runtime = "omp"

[profiles.default.models.default]
chain = ["provider-a/model-x"]
```

Each entry names a provider from the accounts pool as
`provider/model`; an unknown provider is exit 4. The per-launch
overlay drops exhausted providers from each chain, so a launch routes
around quota that is gone; when a role has no live provider left, the
launch refuses with exit 75. Inspect resolution with:

```sh
korapy profile list
korapy profile show --repo REPO --ticket N --role worker
```

Eligibility is one line in the host registration
(`~/.config/korapy/projects.d/NAME.toml`), not code. The ready-made
policy needs no writing:

```toml
eligibility = "korapy.policies:basic"
```

It selects open, unheld, unblocked tickets with no open linked pull,
weighted by board priority. The same file carries the project's slot
`floor` and `ceiling`: with the host total full, a project below its
floor still starts, and a project at its ceiling does not.

Labels and the board come from one verb. It creates the missing board
labels on the tracker (existing labels are left alone), provisions the
board for board entries, and writes the `[repo.board]` mapping into
`korapy.toml`:

```sh
korapy github setup --plan
korapy github setup
korapy github setup --board --populate
```

`--plan` prints what setup would do and changes nothing. `--board`
provisions the board too (for a labels entry, pre-provisioning before
a flip). `--populate` adds the tracker repo's open issues missing from
the board. Exit 1 means everything was already kept or something was
refused; exit 3 means a repository failed.

One label holds a ticket for a human: `board.hold_label` (default
`hold`). The basic policy never selects a held row, and `resume`
refuses with exit 1 while the ticket carries it. A human removes the
label when the ticket is workable again.

## The loop

One dispatch pass syncs the board store from the tracker, claims
proceeding tickets through the store, launches workers through the
launch path, then mirrors the outbox. Preview it without taking the
lock or changing anything:

```sh
korapy drive --plan
korapy drive
```

What you should see from `--plan` on an idle host is the candidate
table and its summary:

```text
plan: 0 candidate(s), would start 0
```

A real pass ends with the same shape under the `pass:` label, plus
`resumed`, `fixed` and `reviewed` counts when the pass did that work.
Each repo syncs and mirrors through its own tracker. The pass holds
the single-instance lock and exits 2 when another pass holds it. In
hub mode the hub claim comes first, and an unreachable hub exits 3.

To freeze the loop without touching the timer, write the pause file.
Its first line is the reason humans see:

```sh
echo "deploy freeze, back soon" > ~/.local/state/korapy/drive.pause
korapy drive --plan
rm ~/.local/state/korapy/drive.pause
```

What you should see while the file exists is
`korapy drive: paused: deploy freeze, back soon` and exit 1, with
nothing started. The file lives directly in the state dir
(`drive.pause` beside `drive.lock`); remove it to resume.

Repeat the pass on a cadence with the user timer:

```sh
korapy drive install-timer
```

That writes the `korapy-drive` user units, reloads the manager and
enables the timer. The cadence is baked from `drive.period` at install
time (default `*:0/5`, every five minutes), so a new cadence needs a
reinstall, which overwrites hand edits to the installed units. Passes
never install the timer themselves; installing is an explicit operator
step. There is no dry run: `install-timer` refuses `--plan`.

Claims are the loop's memory. The board store grants each ticket to
one holder agent id with an epoch. Only the holder with the matching
epoch can refresh or release the claim. When a pass finds work whose
agent died, it releases the dead claim and says so, keeping the epoch
so the next grant fences a stale holder still acting on the old one.
In hub mode each grant carries the hub's fencing epoch too, and a
local epoch the hub superseded skips instead of restarting. The
operator override for hub mode force-releases one ticket's claim,
keeping the epoch, and logs the reason:

```sh
korapy hub release REPO N --reason TEXT
```

Workers and reviewers ride the same loop in different roles
(`worker`, `reviewer`, `triage`). Resume and fix rounds for
already-claimed tickets go before new starts, so in-flight work is not
starved by new tickets, and review waits its turn with the starts.
Each role keeps its own session, so resuming the worker never picks up
a reviewer's session. Watch and stop them by ticket:

```sh
korapy launch REPO N --role worker --title TEXT
korapy tail REPO N
korapy tail REPO N --last 20
korapy agent list
korapy stop REPO N
korapy resume REPO N --message TEXT
```

`tail` shows the newest session of a role (default worker);
`--last` keeps the last lines. `stop` ends the ticket's live agents
that its own log proves it started: exit 1 when none is live, exit 2
when a live unit the log does not prove ours is left running (named,
never stopped). `resume` starts the ticket's newest session of a role
in a new unit; it needs `--message`, and while the ticket carries the
hold label it refuses with exit 1.

The fix-round rules decide when a review sends work back.
`drive.fix_round_threshold` (default `normal`) is the lowest review
finding severity that still opens a fix round. A `block` verdict
always wants one, even with no findings. An approving verdict
(`approve` or `approve-with-findings`) wants one only when a finding
meets the threshold: lows alone are merge-ready and stay in the record
for the coordinator to file as issues. A fresh start never launches on
a ticket with an open linked pull request.

Restarts and the budget close the loop. The pass restarts work whose
agent died, subject to `drive.max_starts_per_day` (default 50): real
starts per UTC calendar day. Resumes and plans never count toward it.
Once the cap stops a start the pass exits 1; the counter lives in the
state dir (`drive.starts.json`) beside the journal
(`drive/journal.jsonl`).

## Driving as a coordinator

One coordinator per project drives the loop above through a lease, a
logbook and an event queue. `KORAPY_PROJECT` names the project when
`--project` is not given. The verbs:

```sh
korapy sync --push
korapy sync --resync
korapy snapshot
korapy snapshot --json
korapy coord events --project NAME
korapy coord wait --project NAME --timeout SECS
korapy coord ack --project NAME EVENT_ID
korapy review show REPO N --pr 7 --head SHA
korapy review record REPO N --commit SHA --verdict approve --author WORKER
korapy takeoff --body FILE --test PATH
korapy takeoff --plan PATH
korapy push
korapy merge REPO N --pr 7 --head SHA
korapy merge REPO N --pr 7 --head SHA --method squash --plan
korapy heavy run --plan -- make check
korapy heavy status
korapy mirror --limit N
```

`sync` feeds the board store from the tracker's change feed
(`--resync` rebuilds from the full listing, `--audit N` re-reads the N
rows verified least recently, `--push` mirrors the outbox after). A
board repo runs the feed step and then the board step. Exit 1 when
nothing changed or the only failures are refused reads, 3 when the
tracker is unreachable, 4 when stored rows are corrupt and nothing
synced. `mirror` pushes queued writes oldest first; exit 0 when a
write landed, 1 when nothing was queued, 3 when every attempt failed
or a rate limit stopped the pass with nothing sent.

`snapshot` is one read-only view of the host: agents, quota, capacity,
resources, disks and the drive section. `--json` prints the stable
document the dashboard serves.

`coord events` lists what is open (`--all` includes resolved rows);
`coord wait` blocks until an unresolved event exists (or the timeout,
default forever) and prints the batch. `wait` and `events` only read.
`coord ack` resolves one event by its id from `wait`. Timeouts belong
to `wait`: `events` rejects `--timeout`. The lease and the logbook
live under the same command; `korapy coord --help` lists `acquire`,
`status`, `heartbeat`, `release`, `takeover`, `log`, `followup`,
`handoff` and `brief`.

`review show` prints the record a commit has, or inherits across a
clean merge-up of the PR base, with every finding. The commit must be
the PR head. Exit 0 only for `approve` or `approve-with-findings`.
`review record` states one verdict on one commit of a ticket, once; a
self-review (reviewer naming the change's author as its own reviewer)
is refused. Findings attach as repeatable `--finding severity:text`
with severities `high`, `normal` and `low`.

`takeoff` is the project-level gate before a PR: the remote base (read
fresh, never from a local tracking ref), the ticket worktree, the
commit rules, the body link and review record, the leak guard, the
approving review record on the head commit, and the `checks.takeoff`
commands. Without `--body` the body check is skipped. Repeatable
`--skip NAME` waives one policy check (`commits`, `em-dash`, `body`,
`leak`, `review`, `checks`) and records the skip; the identity checks
(`base`, `worktree`) cannot be skipped, and an unknown name is exit 4.
`--plan` resolves each check without running the configured commands.
`--test` also runs `checks.test` when it is not already listed.

`push` pushes the branch, verifies the remote ref and retries once.
`merge` merges one PR after proving it mergeable, one merger per repo:
open at the pinned head, with an approving review record, green checks
and no fork. The method is `--method` when given, else the
`merge.method` project setting (a squash by default, or a merge commit
that keeps the branch commits). Only `checks` can be skipped, and
`--plan` reports every row and the lock state without taking the lock
or merging. Retry a refused merge never; retry a held lock after a
sleep:

```sh
while true; do
  korapy merge REPO N --pr 7 --head SHA --json > merge.json; code=$?
  [ $code -eq 0 ] && break
  [ $code -eq 2 ] && sleep 30 && continue
  exit 1
done
```

`heavy run` queues one heavy command and runs it in a transient scope
with CPU and memory limits, releasing the slot on exit; `--plan`
reports the verdict without queueing or running. Everything after
`--` is the queued command. `heavy status` is the rollup of the lane:
holders, waiters and learned costs. An empty lane is an empty list,
not a failure.

A renamed tracker repository keeps its issues, so plain reads keep
working through the forge redirect; but the board store keys every
row by the old `owner/name`. `korapy repo rename` moves those keys
after you rename on the forge:

```sh
korapy repo rename OLD NEW --plan
korapy repo rename OLD NEW
```

`--plan` prints what would move and changes nothing. The move itself
migrates items, claims (agent and epoch kept), the outbox and the
sync cursors in a single transaction. Session directories and review
records are keyed by the short `[[repo]]` name and are not touched
(see #479). The verb refuses with exit 2 while a live or stale agent
entry or a live-held claim names either name, or while a drive pass
holds its lock. A stale entry is an agent that died without closing:
confirm nothing runs under its id, clear it with `korapy agent done`
ID, and retry. Every successful migration leaves a timestamped backup
beside the store; the newest five are kept and older ones are pruned.
To restore one, stop the loop and copy it back over the store file.

Run the move while the repo is quiet: pause the loop with the pause
file (the loop section above), stop the repo's agents (`korapy stop
REPO N` per ticket) until `korapy agent list` shows none live, run
the verb, point the clone's origin remote at the new URL and set
`tracker.repo` to the new name, then remove the pause file and
re-run `korapy doctor`.

Exit codes are a public interface; scripts branch on them:

| Code | Meaning |
| --- | --- |
| 0 | Success (under `--plan`: the plan holds) |
| 1 | Nothing to do, or a gate said no |
| 2 | Race lost or already running: retry later |
| 3 | GitHub, systemd or a host unreachable |
| 4 | Configuration or usage error |
| 75 | No free slot (try later) |

An empty event queue is not a failure: `coord events` prints an empty
list and exits 0. Scripts that need an exit code use `coord wait` in a
loop and reserve `--follow` for watching.

The review gate is proof that a separate reviewer looked. The merge
gate reads the same record the takeoff gate reads, so the two cannot
disagree:

- The reviewer is never the author. A record naming the author as its
  own reviewer is refused when recorded.
- The review happens in its own session. Each role keeps its own
  session, and the record names the reviewer's session.
- The record comes before the merge. `merge` needs an approving record
  on the pinned head; `takeoff` needs one on the gated commit. A
  missing record, a blocking verdict or a moved head refuses with
  exit 1.
- The record is the machine fact. The reviewer's full prose stays in
  the reviewer's session log; nothing is copied into a second artifact
  that could disagree with the record.

Dispatch a separate reviewer before every PR: a different agent, not
the author, with no stake in the change. The dispatch asks one
question and states an expected duration and a hard limit (at least
three times the expectation). `korapy budget` derives that block from
the diff so it is reproducible rather than hand-picked:

```sh
korapy budget --repo REPO
```

The agent-facing version of this section is `skills/coordinator.md`:
the same verbs, exit codes and gate rules, written for the reviewer
prompt instead of the operator.

## Troubleshooting

Each symptom below names the command to run first and what its answer
means. The diagnostic commands only read; `mirror` and `sync` write.

| Symptom | Run | What it means |
| --- | --- | --- |
| Nothing starts | `korapy drive --plan` | The candidate table names one reason per ticket. `0 candidate(s)` with exit 0 means nothing is eligible, not that the loop is broken. A `paused:` line means the pause file is in place. |
| Still nothing, no reason above | `korapy doctor` | A `fail` row stops the loop before tickets: fix the named row first, usually registration, quota readers or the tracker. |
| A worker restarts or looks wedged | `korapy tail REPO N` | Shows the newest worker session. Pair it with `korapy agent list`: an agent the registry calls stale died without closing, and the next pass restarts its work. |
| A claim looks stuck | `korapy drive --plan` | The next live pass releases dead claims itself and prints whom it released, keeping the epoch. In hub mode, `korapy hub release REPO N` force-releases one ticket now. |
| The host looks overloaded | `korapy snapshot` | The `pressure` line and the `resources` charges say whether load, memory or the budget is the ceiling. `korapy disk` adds the reclaim plan, largest first. Launches refused by either read exit 75. |
| GitHub rate limits | `korapy gh-calls --since 1h` | Per-caller and per-minute totals with throttled calls and query shapes. `korapy mirror` exits 3 when a rate limit stops the pass with nothing sent; `korapy sync` exits 3 when the tracker is unreachable. |

```sh
korapy drive --plan
korapy doctor
korapy tail REPO N
korapy agent list
korapy snapshot
korapy disk
korapy gh-calls --since 1h
korapy mirror --limit N
korapy sync --push
```

## Upgrading, backups, and where state lives

Upgrade from a fresh checkout, the reverse of install:

```sh
make install
```

That reinstalls the tool in place. korapy supports one major version
of each agent runtime at a time, so a runtime cutover is a single
change: upgrade the runtime, re-run `korapy deps`, and read what the
doctor says before the next pass.

Back up the store while the loop is quiet. Pause the timer's effect
with the pause file (the loop section above), wait for any live pass
to release the lock, then copy the state dir. In hub mode, export
first, because the hub database lives under its own dir:

```sh
korapy hub export --file PATH
korapy hub backup --to PATH
korapy hub import --file PATH
```

`export` writes hosts and claims with epochs as JSON, without token
material; `import` loads an export into a fresh hub, keeping every
epoch (it refuses a hub that already holds state, and hosts are
re-added afterwards); `backup` copies the hub database to a path and
refuses to clobber. Restore a backup by replacing the file while the
hub is stopped.

Where state lives, so the copy covers it:

- The config dir (`korapy paths config`, default
  `~/.config/korapy/`): `config.toml`, `accounts.toml`, the
  `projects.d/` registrations, and the dashboard API token file.
- The state dir (`korapy paths state`): the board store
  (`board.sqlite`), the drive lock, pause file and day counter, the
  drive journal (`drive/journal.jsonl`), the quota ledger
  (`quota.json`), and the sessions tree. Never hand-edit the store:
  the pass, `sync` and `mirror` own its writes.
- The host ledger beside the XDG state base (not under
  `KORAPY_STATE`): `host-resources.json` with the
  `foreign-claims.d/` directory and the state-dir marker.
- The cache dir (`korapy paths cache`): scratch files such as PR body
  drafts. Safe to drop; `korapy disk --apply` removes only safe
  `pr-body` and `session` rows and never worktrees.
