# Coordinator skill

Coordinate a fleet through the CLI. The CLI is the API: every verb below
is a `korapy` subcommand with `--json` output and a public exit code, so
any shell loop, any harness, and any future surface builds on the same
behavior. This file holds no state and no rules the CLI lacks: when a
verb changes, the same change updates this file, and a test parses the
commands named here and checks each one against the real parsers.

Five rules bind every verb:

- The CLI is the API. Coordinate by calling it, never by reimplementing
  it. Anything done by hand stays possible, because the state stays open:
  the event log, the session logs and the drive journal read plainly with
  `cat` and `jq`.
- State stays open. The event log is one JSON object per line; readers
  take the shared lock and never write.
- Read-only twins. Every verb that changes something has a twin that
  reports without changing: `wait` and `events` only read, and `drive`,
  `takeoff` and `merge` report under `--plan`.
- Explicit skips. A check is skipped only by naming `--skip NAME` on the
  command line, and the skip is recorded in the output document, never
  silent.
- Policy stays in the project. The gate runs the command list the
  project's `korapy.toml` configures; privacy checks and model policy
  live there, never in core.

## Verbs

| Command | Purpose | Read-only twin |
| --- | --- | --- |
| `korapy coord wait [--project NAME] [--timeout SECS] [--json]` | Block until an unresolved coordinator event exists; print the batch, or `[]` on timeout. | Itself: it writes nothing. |
| `korapy coord events [--project NAME] [--all] [--follow] [--json]` | List what is open (`--all` includes resolved rows); `--follow` prints the selection, then new arrivals until killed. Takes no `--timeout`: timeouts belong to `coord wait`. | Itself: it writes nothing. |
| `korapy coord ack [--project NAME] ID` | Resolve one unresolved event by its id from `coord wait`. | None: resolving the event is the write. |
| `korapy review show REPO NUMBER --pr N [--commit SHA] [--head SHA] [--forge-repo OWNER/NAME] [--json]` | Print the review record a commit has, or inherits across a clean merge-up, with every finding. | Itself: it writes nothing. |
| `korapy takeoff [--body FILE] [--test] [--skip NAME] [--plan] [--json] [--ops DIR] [PATH]` | Run the project-level gate over a worktree and report one row per check. `--skip NAME` waives one policy check and records the skip. | `--plan`: resolves each check without running the configured commands. |
| `korapy merge REPO NUMBER --pr N --head SHA [--forge-repo OWNER/NAME] [--method METHOD] [--skip CHECK] [--plan] [--json]` | Merge one PR after proving it mergeable, one merger per repo. `--head` pins the approved head; only `checks` can be skipped. | `--plan`: reports every row and the lock state without taking the lock or merging. |
| `korapy drive [--plan]` | Run one dispatch pass over the board store. | `--plan`: prints the candidate table and changes nothing. |
| `korapy push [BRANCH] [--remote REMOTE]` | Push the branch, verify the remote ref, retry once on a dead connection. | None: pushing is the worktree-side half of landing. |
| `korapy agent list [--json]` | Agents on this host that are not gone. | Itself: it writes nothing. |
| `korapy snapshot [--json]` | One read-only view of the host: agents, quota, capacity, resources, disks, the drive section. | Itself: it writes nothing. |
| `korapy serve [--bind ADDR] [--port N]` | Run the read-only dashboard API on loopback; the stream is `/v1/stream` over the snapshot. | Itself: the API only reads. |

Leases and the logbook live under the same command: `korapy coord --help`
lists `acquire`, `status`, `heartbeat`, `release`, `takeover`, `log`,
`followup`, `handoff` and `brief`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success: the verb did what was asked (under `--plan`: the plan holds). |
| 1 | Nothing to do, or a gate said no: a `wait` timeout, an unknown event, a missing or blocking review record, a refused merge, a failed gate. |
| 2 | Race lost or already running: the lease is held, another merger holds the per-repo lock, another pass holds the drive lock. Retry later. |
| 3 | Unreachable: the forge, the tracker, or the state dir cannot be read. |
| 4 | Usage or configuration error: a bad flag value, a missing project, `--skip` naming an unknown or identity check. |
| 75 | No free slot (try later): the daily start cap or the suite lock stopped the run. |

An empty event queue is not a failure: `coord events` prints an empty
list and exits 0. Scripts that need an exit code use `coord wait` in a
loop and reserve `--follow` for watching.

## Review-gate rules

A review is proof that a separate reviewer looked, and the merge gate
reads the same record the takeoff gate reads, so the two cannot disagree:

- The reviewer is never the author. A record naming the author as its
  own reviewer is refused when recorded.
- The review happens in its own session. Each role keeps its own
  session, and the record names the reviewer's session, so resuming the
  worker never picks up a reviewer's session.
- The record comes before the merge. `merge` needs an approving record
  on the pinned head; `takeoff` needs one on the gated commit. A missing
  record, a blocking verdict, or a moved head refuses with exit 1.
- The record is the machine fact. The reviewer's full prose stays in the
  reviewer's session log; nothing is copied into a second artifact that
  could disagree with the record.

## Loops

Watch for live work, then wait for the next event:

```sh
while korapy agent list --json | jq -e 'length > 0' > /dev/null; do
  korapy coord wait --timeout 120 > /dev/null || true
done
```

Gate a worktree and report each check:

```sh
korapy takeoff --json --body FILE PATH > gate.json || exit 1
jq -r '.checks[] | "\(.status) \(.name) \(.detail)"' gate.json
```

Retry a refused merge never; retry a held lock after a sleep:

```sh
while true; do
  korapy merge REPO NUMBER --pr N --head SHA --json > merge.json; code=$?
  if [ $code -eq 0 ] || [ $code -eq 2 ]; then break; fi
  jq -r '.checks[] | "\(.status) \(.name): \(.detail)"' merge.json; exit 1
done
```
