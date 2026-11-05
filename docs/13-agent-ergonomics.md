# 13: agent ergonomics (wait, events, report, merge, gate)

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides which coordinator verbs belong in korapy,
specifies each one's arguments, output shape and exit codes, and
leaves the build to the tickets in section 11.

Conventions: module and function names quoted below were read from
the tree at design time (`coordevents.py`, `coord.py`, `coordcli.py`,
`takeoff.py`, `takeoffcli.py`, `reviewcli.py`, `reviewrecord.py`,
`mergeup.py`, `forge.py`, `drive.py`, `drivehub.py`, `push.py`,
`agentcli.py`, `snapshot.py`, `serve.py`, `settings.py`,
`paths.py`). Behavior not yet implemented is marked PROPOSED;
everything else describes the tree as it stands. JSON documents
below use `schema_version: 1`, following the `takeoff` precedent
(`takeoff.SCHEMA_VERSION`).

## 1. The problem

A coordinating agent today drives a fleet with ad hoc shell: polling
`systemctl` and logs to see which agents are live, sleeping in a
loop until a condition holds, reassembling a reviewer's final report
from streamed output, merging master into a worktree and running
checks before queueing a review, serializing squash-merges by
convention, retrying a dispatch until it is accepted. Each
coordinator re-invents this, and each invention drifts.

The candidates for core verbs are: `wait`, an event list with
follow, a review-report reader, a merge verb, and a configurable
gate. This doc covers each candidate, decides what belongs in
korapy and what stays project policy, and shows that every
coordinator script in use today rebuilds from the verbs in about
ten lines of shell.

## 2. What already exists

| Verb today | What it does | Gap this doc closes |
| --- | --- | --- |
| `korapy coord wait [--timeout] [--json]` | Blocks until an unresolved coordinator event exists; prints the batch or `[]`. Exits 0 on events, 1 on timeout, 4 on usage. | None. `wait` stays as is. |
| `korapy coord ack <id>` | Resolves one unresolved event. Exits 0, 1 on unknown id, 4 on usage. | None. Unchanged. |
| `korapy review show --pr N --json` | Prints the record a commit has, or inherits across a clean merge-up, plus `inherited_from`. Exits 0 when the record approves, 1 when none exists or the verdict blocks, 3 when the forge is unreachable, 4 on usage. | None. This already is the review-report reader; section 5.3 blesses it instead of adding a verb. |
| `korapy takeoff [--body FILE] [--test] [--json] [path]` | Runs the project-level gate and reports one row per check. Exits 0 when every check is ok or skipped, 1 on failure, 3 when the remote cannot be read, 4 on usage or configuration error, 75 when another takeoff holds the full-suite lock past the wait. | Named skips: today a check is skipped only by a config flag, silently. Section 6 adds `--skip NAME` with recorded skips. |
| `korapy drive [--plan]` | Runs one dispatch pass, or reports the verdict without changing anything under `--plan`. Exits 0, 1, 2 (a pass holds the lock), 3, 4, 75 (no free slot, try later). | None. Section 5.6 documents the retry loop over these codes. |
| `korapy push` | Pushes, verifies the remote ref, retries once on a dead connection. Exits 0, 1, 3, 4. | None. `push` stays the worktree-side half; `merge` (section 7) is the forge-side half. |
| `korapy agent list --json`, `korapy snapshot --json`, `korapy serve /v1/stream` | Read-only views of live agents and host state; the stream is Server-Sent Events over the snapshot. | None. Watchers compose these with `wait` (section 10). |

What does not exist yet: a read-only event list (`events`), a
follow mode for it, named gate skips, any merge verb, and a merge
method on the forge interface (`forge.py` has PR reads only:
`pull_request`, `pr_index`).

## 3. Principles applied

The ticket fixes six principles; each maps to an existing korapy
rule:

1. **Views over open state.** Events and agent state stay plain
   files (JSONL plus the existing store). `events --follow --json`
   and `wait` are thin readers. This is the store-cache rule from
   `00-design.md` section 2: the tracker stays the durable record,
   local files stay readable by hand.
2. **Verdict as data.** Every verb prints a machine-readable
   document (`--json`) and the caller branches. `drive --plan` is
   the model: `--plan` changes nothing and reports what would
   happen.
3. **Explicit skips.** A check is skipped only by naming it on the
   command line, and the skip is recorded in the output document.
   Silent config-off skips stay, but they report as config skips,
   never as passes.
4. **Policy in the project.** The gate runs a configured list of
   commands from the project's `korapy.toml`. Privacy checks, model
   policy and review wording never enter core.
5. **No daemon decisions.** No MCP server and no skill holds state
   or rules the CLI lacks. Section 9 states the MCP verdict.
6. **Composition over options.** Piping (`events --json | jq`)
   beats flags. Each verb does one thing; exit codes keep their
   public meaning (AGENTS.md: 0 ok, 1 nothing to do or gate said
   no, 2 race lost, 3 unreachable, 4 usage, 75 no slot).

## 4. The event log

### 4.1 Format

One JSON object per line, append-only, exactly the rows
`coordevents.record` writes today:

```json
{"id": "3f9a1c2e", "kind": "held-merge", "ref": "atlas#12",
 "evidence": "PR 34: checks failing on abc123",
 "state": "6:abc123checks failing",
 "first_seen": "2026-10-30T08:12:00+00:00",
 "resolved_at": null}
```

A resolved row carries `"resolved_at"` (ISO UTC) and
`"resolved_by"`: `"gone"` (the cause cleared), `"stale"` (the
cause moved on), or `"ack"` (a coordinator saw it). The dedupe key
is `(kind, ref, state)`: `state` is the cause's identity beyond
its ref (a held merge's head SHA plus reason via `hold_state`, a
question's comment body via `question_state`). Re-reporting an
open cause refreshes its evidence; a changed cause is a new event
and re-emits. Malformed lines are dropped by readers, never fatal.

### 4.2 Location: one fact, one place

The file is `<state>/projects/<project>/coord/events.jsonl`,
resolved ONLY by `coordevents.events_path()`. It builds on
`coord.project_root()`, which builds on `paths.state_dir()`, so
the chain stays single. No other module joins the path. `wait`,
the new `events` verb and the serve stream all read through it.
Anyone can still `cat` the file or query it with `jq`: nothing is
hidden behind an API.

### 4.3 Retention

Unresolved rows are kept forever: dropping an open event would
lose a fact nobody acted on. Resolved rows older than 14 days are
pruned on the next write (existing `RESOLVED_KEEP_DAYS`). Writes
rewrite the file atomically under the queue lock
(`events.lock`); readers take the shared lock. The prune bound
keeps the rewrite cheap at coordinator scale.

### 4.4 Writers

Daemon passes call `record()` / `sync_questions_holds()` with an
explicit `kinds` scope, so a pass that never observed a kind can
never clear it. `ack` only marks `resolved_by: "ack"`; an acked
cause that is still live never re-emits, and its return after
going away opens a new event. There is deliberately no
inject-event verb: humans signal through the tracker (a question
comment), and the next sync pass records it. A backdoor writer
would let two coordinators disagree about what is open.

## 5. The verbs

### 5.1 `korapy coord wait` (exists, unchanged)

Purpose: block until an unresolved coordinator event exists, with
no GitHub calls. The wakeup primitive; filtering and dispatch stay
in the caller's shell.

Arguments: `[--project NAME] [--timeout SECS] [--json]`.
`--timeout` waits at most that long; omitted waits forever; zero
times out at once when the queue is empty, and a negative value
is exit 4.

JSON shape (`--json`): the unresolved batch as an array, or `[]`
on timeout.

Exit codes: 0 events present, 1 timeout with an empty queue, 4 bad
timeout value or no project.

State read: the event log (section 4), shared lock, polled locally
(`WAIT_POLL_S`).

Read-only twin: the verb itself. `wait` writes nothing, so it
needs no `--plan`. Deliberately no `--kind` filter: the caller
loops `wait` with `jq` (section 10). One flag per new predicate
would turn `wait` into a query language.

### 5.2 `korapy coord events` (PROPOSED)

Purpose: the read-only twin the queue is missing. List what is
open (or everything, with `--all`), and follow new arrivals for
humans and log shippers. `wait` wakes a loop; `events` shows the
state the loop will act on.

Arguments: `[--project NAME] [--all] [--follow] [--json]`.
`--all` includes resolved rows; default is unresolved only.
`--follow` first dumps the current selection (honoring `--all`),
then prints arrivals by unseen id, and runs until killed; it
does not pair with `--timeout` (timeouts belong to `wait`).

JSON shape (`--json`): a document, not a bare array, so later
fields do not break callers:

```json
{"schema_version": 1, "project": "harbor",
 "events": [{"id": "3f9a1c2e", "kind": "held-merge", "ref": "atlas#12",
   "evidence": "PR 34: checks failing", "state": "6:abc123checks failing",
   "first_seen": "2026-10-30T08:12:00+00:00",
   "resolved_at": null}]}
```

(An open row carries no `resolved_by` key until it resolves, as
`record()` writes; a resolved row adds `resolved_at` and
`resolved_by`.)

Without `--json`, one line per event, the same terse form `wait`
prints: `id kind ref evidence` on one line. `--follow --json`
prints one document per new row (JSON lines) in the same
envelope with a single-element `events` array, flushing every row
so `events --follow --json | jq` works through a pipe.

Exit codes: 0 always on a successful read (an empty queue is an
empty list, not a failure), 4 on usage. `events` takes no
`--timeout` flag; passing one, with or without `--follow`, is
exit 4 (timeouts belong to `wait`). `--follow` runs until
terminated; it has no terminal success of its own, so scripts
that need an exit code use `wait` in a loop and reserve
`--follow` for watching and shipping.

State read: the event log (section 4), shared lock, local poll
only. Never the tracker, never the forge.

Read-only twin: the verb itself; it writes nothing.

### 5.3 The review-report reader: `korapy review show --json` (exists, blessed)

Purpose: return the machine fact a review established: the
verdict, the commit it covers, whose review it was, every finding,
and whether the record was inherited across a clean merge-up.
Coordinators reassembling a report from streamed reviewer output
today replace that scraping with this one call.

Arguments (as they stand): `REPO NUMBER --pr N [--commit C]
[--head H] [--forge-repo R] [--json]`. `--pr` is required: the
base comes from the forge's `base_sha`, fresh. `--head` pins the
expected head; a moved head fails closed.

JSON shape (`--json`): the record object plus inheritance:

```json
{"author": "agent-7", "commit": "abc123...",
 "findings": [{"severity": "major", "text": "retry loop has no bound"}],
 "inherited_from": null, "question": "is the gate sound?",
 "reviewer": "agent-9", "session": "reviewer-0",
 "verdict": "approve-with-findings"}
```

(`inherited_from` holds the covering commit when the record was
inherited across a clean merge-up, else null. `approves()` treats
`approve` and `approve-with-findings` as passing.)

Exit codes: 0 the record approves, 1 no record, a blocking
verdict, a head mismatch, a base absent from the clone, or a
missing `--pr` or unresolvable `--forge-repo` (the code returns
1 there today; making that case exit 4 is a later code change),
3 the forge is unreachable, 4 usage.

State read: the ticket's `reviews/` dir under the session root
(via `reviewrecord` and `paths.ticket_session_dir`), plus one
fresh forge PR read for base and head SHAs.

Read-only twin: the verb itself; it writes nothing, so no `--plan`
flag. The reviewer's full prose stays where it was written, in the
reviewer's session log, read with the existing session tail verbs.
No new verb, no report sidecar file: a second artifact beside the
record would split the one fact (the review's verdict) into two
places that can disagree.

### 5.4 `korapy merge` (PROPOSED)

Purpose: merge one pull request after proving it is mergeable,
serially, with every precondition reported as data. The forge-side
half of landing; `push` stays the worktree-side half.

Arguments: `REPO PR --head SHA [--method squash] [--skip CHECK]
[--json] [--plan]`. `REPO` is `owner/name`; `PR` the pull request
number. `--head` is required: the exact head SHA the caller
approved. `--method` takes one value today, `squash`; anything
else is exit 4, so the flag exists for later extension without
breaking callers. `--skip` names only `checks`, with the same
recording rule as the gate (section 6); the default is no skips.
Naming `lock`, `open`, `head`, `review` or `fork` is exit 4:
those checks establish what is being merged, and skipping
identity is not a policy choice (mirroring takeoff, section
5.5).

JSON shape (`--json`): the precondition verdict plus the outcome:

```json
{"schema_version": 1, "repo": "example/atlas", "pr": 34,
 "head": "abc123...", "method": "squash",
 "checks": [{"name": "open", "status": "ok", "detail": "PR is open"},
   {"name": "head", "status": "ok", "detail": "head is abc123"},
   {"name": "review", "status": "ok", "detail": "approved by agent-9"},
   {"name": "checks", "status": "ok", "detail": "5 green"}],
 "skipped": [], "merged": true, "merge_sha": "789abc..."}
```

Without `--json`, one line per check plus the outcome, in the
`takeoff` text style (`merge <name>: <status> - <detail>`).

Exit codes: 0 merged (under `--plan`: mergeable), 1 a
precondition failed or the merge was refused, 2 the merge lock is
held by another merger, 3 the forge is unreachable, 4 usage or
configuration error. Exit 2 reuses the public race meaning, so
retry loops treat it like the drive lock.

State read: one fresh forge PR read (state, head SHA, check
statuses, fork flag), the review record via `mergeup.find_record`
(the same reader takeoff uses, so both gates agree), and the
per-repo merge lock. No local worktree state: the merge runs
forge-side, so a coordinator needs no clone.

Read-only twin: `--plan`. It reports every precondition row and
the lock state (`free` or `held`) without taking the lock and
without merging. The lock row is informational: `--plan` answers
"is this PR mergeable", and exits 0 when every precondition holds
even if the lock is currently held. A planned merge that would
fail exits 1 with the failing rows.

Checks, refusal and never-dos are specified in section 7; the
forge seam it needs in section 8.

### 5.5 The gate: `korapy takeoff --skip NAME` (exists, extended)

Purpose: run the project-level gate before a PR and report each
check. `takeoff` already runs the configured command list and
reports one row per check; the extension is named skips with
recorded provenance. Full spec in section 6.

Arguments: existing flags plus PROPOSED repeatable
`--skip NAME`, where NAME is one of `commits`, `em-dash`, `body`,
`leak`, `review`, `checks`. Naming `base` or `worktree` is exit 4:
those checks establish what is being gated, and skipping
identity is not a policy choice.

JSON shape (`--json`): the existing document with one added
field per check:

```json
{"schema_version": 1, "base": "abc123...",
 "checks": [{"name": "review", "status": "skipped",
   "detail": "skipped by --skip review", "fix": "",
   "skipped_by": "flag"}]}
```

`skipped_by` is `null`, `"flag"`, or `"config"`. Existing
config-off skips (for example `review.required` false) keep their
behavior and report `"config"`. Text output names the skip the
same way: `takeoff review: skipped - skipped by --skip review`.

Exit codes: unchanged (0 every check ok or skipped, 1 a check
failed, 3 remote unreadable, 4 usage or configuration, 75 the
full-suite lock). A `--skip` of an unknown check name is exit 4.

State read: the worktree (status, commits, added lines), the
`--body` file, the leak rules, the review record, the forge base,
and `korapy.toml` (`checks.takeoff`, `checks.test`,
`review.required`, `git.commits.conventional`,
`checks.no_em_dash`). The configured commands run in the
worktree with the standard timeout and seam injection.

Read-only twin: PROPOSED `--plan`. Today `takeoff` already runs
the full suite only with `--test`, and the configured commands
always execute, so a pure read does not exist. `--plan` resolves
every check that needs no execution (base, worktree, commits,
em-dash, body, leak, review) and reports each configured command
as `status: "planned"` with the command string as detail, without
running it. Exit codes mirror the verdict: 0 when every resolved
check passes and the plan lists the commands, 1 when a resolved
check fails.

### 5.6 Retrying dispatch: `korapy drive` (exists, documented)

Purpose: run one dispatch pass. No new verb: the retry loop
branches on exit codes the pass already returns.

Arguments: `korapy drive [--plan]` (plus the existing hub and
timer flags). `--plan` is the read-only twin: it holds no lock
and writes nothing, and prints what the pass would do.

JSON shape: none today, deliberately. The pass verdict is one
line of text plus the exit code; structured per-project detail
stays in the drive journal, which the coordinator reads with
`jq`. Adding a `--json` document is a later ticket if a caller
needs it; today nothing in the tree parses drive's stdout.

Exit codes (the retry contract): 0 the pass ran, 1 nothing to do
or a gate said no, 2 another pass holds the lock (retry later), 3
unreachable, 4 usage or configuration, 75 no free slot (retry
later). Section 10 shows the loop: 2 and 75 sleep and retry, 0
and 1 stop, anything else aborts loudly.

## 6. The gate: config, skips, reporting

### 6.1 Config shape

The gate runs what the project's `korapy.toml` configures; core
only runs the list and reports each result. Two keys, both
registered today:

- `checks.takeoff`: list of command strings, run in order in the
  worktree, reported as one `checks` row per failing command
  (today) or per command (PROPOSED: one row per command, so a
  skip can name the group honestly; the row name for the group
  stays `checks`).
- `checks.test`: the suite command the agent and takeoff run.

Project policy keys gate the built-in checks: `review.required`,
`git.commits.conventional`, `checks.no_em_dash`. When false, the
corresponding check reports `skipped` with the config key as the
reason. The built-in check names are fixed and few: `base`,
`worktree`, `commits`, `em-dash`, `body`, `leak`, `review`,
`checks`. A new built-in check is a public interface change and
needs a maintainer decision, so the name list cannot drift by
accident.

### 6.2 How a skip is named and logged

`--skip NAME` is repeatable and names one built-in check or the
`checks` group. The skip is recorded in two places, both part of
the verb's output, so the caller keeps it by keeping the output:

1. The JSON document: the check row carries `"skipped_by":
   "flag"` and a detail naming the flag.
2. The text output: `takeoff <name>: skipped - skipped by
   --skip <name>`.

There is no quiet skip. A skip that reaches a PR body or a logbook
entry is traceable to the exact invocation. Config-off skips keep
reporting `"config"` with the key that turned them off. Nothing
is written to the repo or the state dir: the skip lives in the
reported verdict, and the caller decides where that verdict is
stored (PR body, logbook, journal).

### 6.3 How results are reported

One row per check, in gate order: `name`, `status` (`ok`,
`fail`, `skipped`, and PROPOSED `planned` under `--plan`),
`detail`, `fix`, plus `skipped_by`. The `fix` line tells the
agent exactly what to run or change, following the existing
takeoff style. Exit 0 requires every row ok or skipped; one `fail`
fails the gate. The document's `schema_version` marks the shape;
adding `skipped_by` and `planned` is the version 1 shape defined
here.

## 7. The merge verb in detail

### 7.1 Serialization

Merges to one repository serialize on a per-repo lock
(`<state>/merges/<owner>+<repo>.lock`, PROPOSED), resolved ONLY
by `merge.lock_path(repo)`, which builds on `paths.state_dir()`.
The exact filename is set at implementation; the rule is one
lock per forge repo, one resolver for its path. The verb acquires
the lock, re-reads the PR live, merges, verifies, and releases.
A second merger exits 2 while the lock is held: race lost, try
later. `--plan` never takes the lock. The lock is per repo, not
per host: two unrelated projects never block each other, and two
PRs to the same repo never merge concurrently.

### 7.2 The checks, in order

1. `lock`: free (informational under `--plan`).
2. `open`: the forge reports the PR `open`. Merged or closed
   refuses with exit 1: there is nothing to do.
3. `head`: the forge head SHA equals `--head`. A moved head
   refuses: the caller approved a commit that is no longer the
   tip.
4. `review`: `mergeup.find_record` over the ticket's `reviews/`
   dir finds an approving record for the head, directly or
   inherited across a clean merge-up. A block or a missing record
   refuses. This is the same reader takeoff uses, so the merge
   gate and the takeoff gate cannot disagree.
5. `checks`: the forge's check statuses on the head are green
   (read from the fresh `pull_request()`, extended per section
   8). `--skip checks` waives this row and records the skip; it
   is the only waivable merge check (section 5.4).
6. `fork`: the PR is not from a fork. Fork merges refuse with
   exit 1; a maintainer merges those by hand (AGENTS.md: external
   contributions are for maintainers).

After the merge call, the verb verifies: re-read the PR and
require state `merged` plus the merge commit SHA the forge
reports tied to the pinned head (section 8). A mismatch fails
closed with exit 1 (the merge happened but is unverified; the
row says so). A parent check on the merge commit is reserved for
a future non-squash method; squash produces a single-parent
commit, so no parent check applies here.

### 7.3 How it refuses, and what it never does

Refusals are exit 1 with the failing row named; the document
reports every row so the caller knows how far the PR got. The
verb never:

- merges without `--head` (an unpinned merge is a usage error);
- merges a closed, merged, or fork PR;
- force-pushes, deletes a branch, or rewrites history (no
  `--force` flag exists and none is added);
- bypasses hooks (the merge is forge-side; there is no local hook
  to bypass, and the verb adds no bypass flag);
- deletes the head branch on its own: branch cleanup stays with
  the host hygiene pass, which demands its proof before removal;
- retries a refused merge (1 is final; only exit 2 and 75 retry,
  and the merge verb never returns 75).

## 8. The forge seam for merge

`pull_request()` today returns no check statuses and no
merge-commit SHA, but sections 5.4 and 7.2 read both. PROPOSED:
extend `PullRequest` with two fields, read from the same fresh
PR fetch, so the merge gate needs no second seam:

- `checks_state: str`: `"passing"`, `"failing"` or `"pending"`,
  summarizing the forge's check statuses on the head SHA;
- `merge_sha: str | None`: the merge commit SHA once the PR
  state is `merged`, else None. The verify step ties this SHA to
  the pinned head.

Only the adapters know the provider shape (the GitHub adapter
parses both from the same `gh api` PR fetch it already
performs); everything else takes the `Forge` object. The
alternative (a separate `checks()` seam method) is rejected: two
reads can disagree about a moving PR, while one fresh read keeps
state, head, checks and merge SHA consistent.

`forge.py` gains one more method (PROPOSED):

```python
def merge(self, repo: str, number: int, *, head_sha: str, method: str) -> str:
    """Squash-merge PR `number` and return the merge commit SHA.

    Refuses unless the PR is open at `head_sha`. Raises
    `ForgeError` when the forge is unreachable or answers
    something unusable. Only the adapters call `gh` or know
    provider shapes.
    """
```

Only the GitHub adapter implements it (a `gh api` merge call
against the pinned head, verified as in section 7.2). Everything
else takes a `Forge`, per the rule that nothing outside the two
adapters calls `gh`. Coordinators calling `gh pr merge` directly
today migrate to `korapy merge`; the direct call keeps working,
since the state stays open, but it loses serialization and the
recorded verdict.

## 9. Core versus project

| Candidate | Verdict | Why |
| --- | --- | --- |
| `wait` | Core (exists) | Every coordinator waits; one reader, no policy. |
| `events` list and follow | Core (PROPOSED) | Views over open state; no policy in a listing. |
| Review-report reader | Core already (`review show --json`) | The verdict is a core fact; the prose stays in sessions. |
| `merge` | Core (PROPOSED) | Every project merges the same way; policy stays in preconditions and skips. |
| Gate runner | Core (`takeoff`, plus PROPOSED `--skip` and `--plan`) | Core runs the list and reports; the list contents are project config. |
| Privacy checks, model policy, review wording | Project | Policy, not mechanism; lives in `checks.takeoff` commands and prompts. |
| Retry timing and backoff | Project | Exit codes are the contract; sleep intervals are caller policy. |
| Which events wake which coordinator | Project | Kinds and refs are project vocabulary; core only stores and lists them. |

## 10. Surfaces: CLI, skill, MCP

The CLI is the API. Every verb above is a `korapy` subcommand
with `--json` output and public exit codes, so any shell, any
harness, and any future surface builds on the same behavior.

The skill is a thin file, PROPOSED at `skills/coordinator.md`:
one verb table (name, one-line purpose, read-only twin), the exit
code table, and the review-gate rules (reviewer is not the
author, separate session, record before merge). It holds no state
and no rules the CLI lacks; when a verb changes, the same ticket
updates the skill. A test asserts the skill's verb list matches
the CLI parsers, so the two cannot drift silently.

The MCP server is optional, last, and only a mirror: if one ever
exists, it exposes the same verbs with the same behavior and
holds no state or rules of its own. Verdict: korapy should NOT
ship an MCP server in this round. Reasons: the CLI is already
scriptable from any agent over stdio with no extra process to
supervise; a second surface doubles the conformance burden
(identical verbs, identical exit codes, identical output
shapes); and nothing in sections 5 to 8 needs in-context tool
calls that cannot shell out. Revisit when a project shows a call
pattern the CLI cannot serve.

## 11. The test of the design

Each coordinator script in use today rebuilds from the verbs in
about ten lines of shell. Names below are stand-ins.

Live-agent watcher (exit 0 when nothing is live on the ticket):

```sh
while korapy agent list --json \
  | jq -e 'any(.[]; .ticket.repo == "atlas" and .ticket.number == 12)' > /dev/null; do
  korapy coord wait --timeout 120 > /dev/null || true
done
```

Gate (run the project gate, report each check, refuse on failure):

```sh
korapy takeoff --json --body "$body" "$worktree" > gate.json || exit 1
jq -r '.checks[] | "\(.status) \(.name) \(.detail)"' gate.json
jq -e 'all(.checks[]; .status != "fail")' gate.json > /dev/null
```

Review-report reader (the machine verdict plus every finding):

```sh
korapy review show atlas 12 --pr 34 --head "$sha" --json > report.json
jq -e '.verdict == "approve" or .verdict == "approve-with-findings"' report.json
jq -r '.findings[] | "- \(.severity): \(.text)"' report.json
```

Serialized merge (one merger at a time per repo; exit 2 retries):

```sh
while true; do
  korapy merge example/atlas 34 --head "$sha" --json > merge.json; code=$?
  [ $code -eq 0 ] && break
  [ $code -eq 2 ] && sleep 30 && continue
  jq -r '.checks[] | "\(.status) \(.name): \(.detail)"' merge.json; exit 1
done
```

Retrying dispatch (plan first, then run; 75 and 2 retry later):

```sh
korapy drive --plan || exit 1
while true; do
  korapy drive; code=$?
  [ $code -eq 0 ] && break
  { [ $code -eq 75 ] || [ $code -eq 2 ]; } && sleep 60 && continue
  exit $code
done
```

Everything done by hand today stays possible, because the state
stays open: editing a ticket body through the tracker CLI,
redispatching under another slug with the launch branch flags,
merging manually with the forge CLI after reading `review show`
and `merge --plan`, and reading the raw files (`events.jsonl`,
session logs, the drive journal) with `cat` and `jq`. The verbs
are conveniences over files, not gates in front of them.

## 12. Implementation tickets, in order

Each ticket is small, independently reviewable, and ships its own
tests with fakes at the seams (no live `gh`, systemd, network, or
state dirs, per AGENTS.md).

1. `coord events` list verb: `--all`, `--json`, text form; pure
   reader over `coordevents`. Tests: row filtering, empty queue,
   malformed lines dropped.
2. `coord events --follow`: follow mode with per-row flush in
   the single-element envelope; injectable clock and sleep like
   `wait_for_events`. Tests: arrival batching, envelope shape
   per row, `--timeout` rejected with exit 4, kill handling, no
   tracker or forge calls.
3. `takeoff --skip NAME`: named skips for the six policy checks,
   `skipped_by` in JSON and text, exit 4 on unknown or identity
   names. Tests: each skip, config-off rows unchanged, refusal
   names.
4. `takeoff --plan`: resolve without executing; configured
   commands reported as `planned`. Tests: no command executed
   (fake runner asserts zero calls), exit codes mirror resolved
   rows.
5. `Forge.merge` seam plus the `PullRequest` extension
   (`checks_state`, `merge_sha`) and the GitHub adapter: parse
   both fields from the existing `gh api` PR fetch, pin
   `--head`, squash, post-merge verify (state `merged` plus the
   reported merge SHA tied to the pinned head). Tests: adapter
   against a fake runner (both fields parsed, missing PR raises
   `ForgeError`), fake-forge coverage of each `checks_state`
   value, seam refusal shapes.
6. `korapy merge` verb: per-repo lock via the single resolver
   `merge.lock_path(repo)`, ordered preconditions (section 7.2),
   `--plan`, `--json`, exit codes 0/1/2/3/4, `--skip` of
   anything but `checks` exits 4. Tests: lock race is exit 2,
   moved head refuses, fork refuses, verify-mismatch fails
   closed, non-`checks` skip is exit 4. README Commands table
   gains its row here (user-visible verb).
7. Coordinator skill file at `skills/coordinator.md` plus the
   parser-conformance test. No CLI change; README unchanged
   (no user-visible surface moves).

NOT a ticket: an MCP server (section 10 says no), a `--json`
document for `drive` (deferred until a caller needs it), and a
manual inject-event verb (rejected in section 4.4).

## 13. Options for the maintainer

1. Ship 5, 6, 7, 10 as specified; no MCP. (recommended)
2. Ship without `takeoff --plan`; keep skips only.
3. Ship without `events --follow`; keep list only.
4. Merge via forge-side call, not a local worktree merge.
5. Skill file under `docs/` instead of `skills/`.
6. Defer the skill conformance test to its own ticket.
