# Configuration

Every setting korapy reads lives in one registry
(`src/korapy/settings.py`), and the table at the bottom of this file is
generated from it. If a value below disagrees with `korapy config show`,
the command wins: regenerate the table with
`uv run --locked python scripts/gen_settings_table.py` and the drift
shows up as a diff.

## Layers

Later layers win. `korapy config show --origin` names the layer each
value came from and marks values clamped by a host limit.

1. Built-in defaults, from the registry table below.
2. The host config, `<config>/config.toml` (`~/.config/korapy/` by
   default; `korapy paths config` prints the live path).
3. The ops root's `korapy.toml`: its shared tables first, then the
   `[[repo]]` entry named for the repository being resolved.
4. `KORAPY_*` environment variables: `KORAPY_` plus the key in upper
   case with dots as underscores (`agents.cap` is `KORAPY_AGENTS_CAP`).
5. CLI flags.

The host config's `[limits]` table sets ceilings on numeric settings. A
value above its ceiling is clamped whatever layer set it, and
`config show --origin` and `doctor` report the clamp. The old
`agents.runtime` key was replaced by profiles and is refused everywhere
it appears; declare a `[profiles.*]` home instead (see below).

## The project file

The ops root's `korapy.toml` holds the shared tables plus one
`[[repo]]` entry per repository. In the inline layout the ops root is
the project clone itself; in the split layout the agent side lives in a
separate ops root (`korapy init --ops PATH`) and the clone is touched
only through `core.hooksPath` and `.git/info/exclude`. A `[[repo]]`
entry sets `clone.path` (relative to the ops root) and
`clone.default_branch`, which `korapy launch` needs. A project
registers with one file per host in
`~/.config/korapy/projects.d/<name>.toml`, naming its `unit_prefix`,
its ops `root`, its guaranteed `floor` of slots and its `ceiling` of
slots; the `[resources]` share beside them is described under Host
budget below. The full onboarding flow is in [`guide.md`](guide.md)
and [`01-onboarding.md`](01-onboarding.md).

## The accounts file

Every project on the host shares one pool, beside the host config in
`accounts.toml`. It names providers and the quota reader that knows
each provider's quota format. The reader is the only
provider-specific code, and it lives in the project's own directories,
never in korapy (korapy ships none). Each `reader_path` entry must be
absolute after `~` and environment expansion; with no file, the pool
is empty. Readers are trusted code loaded in process, so install only
readers you trust. Details: [`02-accounts-quota.md`](02-accounts-quota.md)
and the First run section of [`guide.md`](guide.md).

## Profiles

A project's default profile declares its runtime and model chains in
the same `korapy.toml`. Each chain entry names a provider from the
pool as `provider/model`; an unknown provider is a configuration
error (exit 4), and an id starting with `-` is refused. The per-launch
overlay drops exhausted providers from each chain, so a launch routes
around quota that is gone; when a required role has no live provider
left, the launch is refused with exit 75. Resolution, roles, overrides
and pinning are in [`10-agent-profiles.md`](10-agent-profiles.md).

## Board mapping

A `[[repo]]` entry whose project coordinates through hosted board
fields sets `tracker.variant = "board"` (default `"labels"`) and maps
its board in a `[repo.board]` table: the board owner plus the board
number (or node id), the korapy-owned text fields, the people-owned
columns, and a `priority_map` of board option ids to ranks. A variant
outside `"labels"` and `"board"` is a configuration error naming the
repo and the key, as are unknown concept keys; the option ids are
opaque and display names are comments only. Details:
[`07-board-fields.md`](07-board-fields.md) and
[`11-board-standard.md`](11-board-standard.md).

## Host budget

With no `[resources.host]` table nothing is enforced: launches set no
slice or limit properties, and `doctor` warns that host limits are
unset. Once the table is set, every launch is charged by its role and
the kernel enforces the limits through nested slices (`korapy.slice`,
then `korapy-<project>.slice`) with the agent unit under them. The
host table needs all three dimensions; a missing dimension in a role
table reads as zero, and a role with no charge table is refused at
launch. A weight of zero keeps only the floors; floors summing above
the host budget, a missing required cgroup controller, and a corrupt
host ledger are all configuration errors. Changed limits reconcile on
the next launch of that project. Clearing the budget applies nothing:
stale limits stay until the operator stops the slice by hand.
`[resources.pressure]` sets the saturation line for load korapy does
not control: past it, new launches refuse with exit 75 even with
shares free, while running agents are never paused or killed. An
outside fleet declares its own load with one JSON file per claim
(`<name>.json`; other names are ignored) in the `foreign-claims.d/`
directory beside the host ledger; claims subtract from the pool and
expire after the `resources.gone_s` interval below. Details:
[`09-host-resource-governance.md`](09-host-resource-governance.md).

## Private terms

The ops root's `korapy.toml` may list private terms that must never
reach the shared side: plain strings (`terms`) and regular expressions
(`patterns`), both matched case-insensitively. korapy always adds the
ops repository's own name and path, the host name and the home
directory path. `[private]` lives in the ops root's file only: the
host config and `[[repo]]` entries cannot set it. A bad pattern is a
configuration error, and `korapy leakcheck` scans a branch's messages,
branch name, body and added lines against the list.

## Settings

One row per registered setting: the key, its built-in default, and
what it means. Do not edit this table by hand.

<!-- settings-table:start -->
| Setting | Default | Meaning |
| --- | --- | --- |
| `admission.repo_cap` | `0` | live plus pending agents per repo (0 means none) |
| `admission.total_cap` | `4` | live plus pending agents the host admits in total |
| `agents.cap` | `1` | this project's ceiling of live agents on the host |
| `agents.unit_prefix` | `'korapy-agent-'` | prefix of transient agent unit names |
| `board.hold_label` | `'hold'` | label that holds a ticket for a human |
| `board.item_max_age_s` | `300` | seconds before a freshness-gated read re-reads a board row live |
| `board.poll_pages` | `5` | board poll page cap per pass (default: small) |
| `board.ready` | `'ready'` | label that makes an issue eligible |
| `board.schema_max_age_s` | `3600` | seconds before the board step refreshes the cached board schema |
| `checks.no_em_dash` | `True` | takeoff refuses added lines and bodies with an em-dash |
| `checks.takeoff` | `[]` | takeoff gate commands, run in order in the worktree |
| `checks.test` | `''` | the test command the agent and takeoff run |
| `checks.timeout_s` | `1800` | seconds before a takeoff command is killed |
| `clone.default_branch` | `'main'` | the default branch of the clone |
| `clone.path` | `''` | the repository's main clone (relative to the ops root) |
| `disk.min_free_gb` | `5` | free gigabytes under which the disk reads low |
| `disk.min_free_pct` | `10` | free-space percent under which the disk reads low |
| `disk.session_retention_days` | `14` | days before a finished ticket's session dir is a reclaim candidate |
| `drive.agent_host` | `''` | display name for the host segment of minted agent ids (empty hashes the hostname) |
| `drive.fix_round_threshold` | `'normal'` | lowest review finding severity that still opens a fix round (high, normal or low): an approving review below it is merge-ready |
| `drive.max_starts_per_day` | `50` | real starts one dispatch pass may launch per calendar day (plans never count) |
| `drive.period` | `'*:0/5'` | systemd OnCalendar cadence at which the installed timer wakes a pass |
| `git.branch` | `'{type}/{issue}-{slug}'` | branch name template |
| `git.commits.conventional` | `True` | enforce Conventional Commits |
| `git.worktrees` | `'.worktrees/'` | worktree root, relative to the clone |
| `hub.heartbeat_s` | `30` | seconds between hub heartbeats |
| `hub.host_id` | `''` | this host's operator-assigned hub id (empty means unregistered, hub mode refused) |
| `hub.lease_ttl_s` | `90` | seconds a hub claim grant stays valid |
| `hub.stale_after_s` | `90` | seconds after the last heartbeat a host reads stale on the merged dashboard |
| `hub.url` | `''` | address of the hub, for example http://host-1:8000 (empty means single-host) |
| `merge.method` | `'squash'` | how korapy merge lands a PR: a squash, or a merge commit |
| `prompts.reviewer` | `''` | override file for the reviewer prompt (relative to the ops root) |
| `prompts.triage` | `''` | override file for the triage prompt (relative to the ops root) |
| `prompts.worker` | `''` | override file for the worker prompt (relative to the ops root) |
| `quota.low_pct` | `90` | usage percent at which a provider reads low |
| `quota.unknown_blocks` | `False` | an unknown provider blocks its roles instead of passing them |
| `registry.count_toward_cap` | `True` | live registered ticket workers take cap slots |
| `registry.gone_s` | `3600` | seconds without a heartbeat before an agent is dropped |
| `registry.stale_s` | `600` | seconds without a heartbeat before an agent reads stale |
| `resources.gone_s` | `600` | seconds a foreign claim counts against the pool |
| `review.budget.base_minutes` | `15` | reviewer minutes before the diff size matters (loading, build, fast gates) |
| `review.budget.ceiling_minutes` | `120` | agent runtime ceiling in minutes for one dispatched reviewer |
| `review.budget.floor_minutes` | `30` | hard limit floor in minutes, however small the diff |
| `review.budget.limit_multiple` | `3` | hard limit as a multiple of the expected duration |
| `review.budget.seconds_per_line` | `6` | reviewer seconds per changed line in the diff |
| `review.required` | `True` | takeoff needs an approving review record for the head |
| `serve.bind` | `'127.0.0.1'` | address to bind (default 127.0.0.1, loopback) |
| `serve.max_streams` | `20` | concurrent stream connections before the server sheds with 503 |
| `serve.origins` | `[]` | dashboard origins allowed cross-origin (exact match, never *) |
| `serve.port` | `8420` | port to bind (default 8420, a fixed unprivileged port with no common clash; 0 picks a free port for a second or throwaway instance) |
| `serve.snapshot_ttl_s` | `2` | seconds one built snapshot is shared between requests |
| `serve.tail_max` | `200` | maximum activity lines one tail read returns |
| `tracker.operators` | `[]` | tracker logins treated as operators (default: the repository owner only) |
| `tracker.repo` | `''` | tracker repository as owner/name (default: the clone's origin) |
| `tracker.variant` | `'labels'` | tracker source: labels or board (default: labels) |
<!-- settings-table:end -->
