# 15: project-declared hygiene rules for gc and disk

Status: design, awaiting maintainer approval. No code ships with
this doc. It decides how a project declares its own host hygiene
rules for things korapy does not know about, so `korapy gc` and
`korapy disk` can clean them safely. Implementation waits for
approval and follows the tickets in section 9.

Conventions: module, function and file names quoted below were
read from the tree at design time (`gc.py`, `disk.py`,
`gccli.py`, `diskcli.py`, `paths.py`, `settings.py`,
`projects.py`, `worktrees.py`, `registry.py`, `units.py`,
`heavy.py`, `host_ledger.py`, `reviewrecord.py`). Behavior not
yet implemented is marked PROPOSED; everything else describes
the tree as it stands. Claims about kernel or Docker behavior
that were not verified on a live host are marked UNVERIFIED,
each with the safe read-only probe that settles it
(section 8).

## 0. Scope and non-goals

The design adds: one config shape for project-declared rules, one
resolver for the declared roots, pure planning per rule kind with
a stated proof, plan and apply wiring through the existing `disk`
flow, new injectable seams with fakes, and a worked example.

Non-goals: changing any `gc` verdict (`LANDED`, `SUPERSEDED`,
`MERGED_PR`, `LIVE_REMOTE`, `NO_UPSTREAM`, `UNKNOWN`) or the
`SAFE` tuple; changing the `disk` pressure thresholds
(`disk.min_free_pct`, `disk.min_free_gb`) or the `pressure`
function; pruning the Docker builder cache in version one
(section 4.4, decided against with reasons); per-ticket
priority; and any new CLI verb in version one (rules surface in
the existing `korapy disk` plan).

## 1. The problem

`korapy gc` and `korapy disk` clean what korapy itself creates:
branches with merge proof, registered and unregistered worktrees,
PR body drafts, and sessions under its own roots. A real project
also leaves other things on a host: container images built for
development and tagged with a project prefix, build caches under
a user cache directory, scratch files and logs written by its
checks, and old agent sessions. Today a project has no way to
declare these, so either they pile up until the disk is full or
an operator deletes them by hand. The migration audit of a
second consumer project listed exactly this gap (dev images,
build caches, scratch files, old sessions), and it must not be
solved by korapy guessing.

## 2. Principles

AGENTS.md "Proof before removal" governs the whole design:

1. Nothing is cleaned unless the project declares it. No rule,
   no candidate, no guessing from names or sizes.
2. Every rule names a root (a directory under a declared base,
   or an image name prefix), a selector (age, count, size, or
   "unreferenced by any live worktree or running unit"), and a
   proof (what makes the deletion safe, evaluated by pure code
   over plain data).
3. Every deletion goes through a destructive seam that takes its
   implementation as a required argument and fails closed with
   no default.
4. The plan form (`gc` and `disk` without `--apply`) lists
   exactly what would go and why, and `--apply` re-evaluates
   the proof at deletion time.
5. Policy lives in the project's config, not in core. Core owns
   the shape, the resolver, the proofs and the seams; the
   project owns the rules.

## 3. Config shape

PROPOSED. Rules live in the ops root's `korapy.toml`, the
project-owned file, as top-level `[hygiene.<name>]` tables, one
table per rule. Each table names the repo it belongs to and
exactly one kind:

```toml
[hygiene.acme-scratch]
repo = "acme"
kind = "dir"
base = "cache"
path = "acme/scratch"
selector = "age"
days = 7

[hygiene.acme-images]
repo = "acme"
kind = "image"
prefix = "acme-dev"
selector = "unreferenced"
older_than_days = 3

[hygiene.acme-sessions]
repo = "acme"
kind = "session"
base = "sessions"
path = "acme-archive"
retention_days = 30
```

Kinds and their keys:

- `dir`: `base` plus `path` (section 3.1), `selector` one of
  `age`, `count`, `size`. `age` takes `days`; `count` takes
  `keep` (newest entries to keep by mtime); `size` takes
  `max_bytes` (budget for the whole root).
- `image`: `prefix` (a repository name prefix, section 3.2),
  `selector` is always `unreferenced`, optional
  `older_than_days` as a floor so a fresh build is never a
  candidate.
- `session`: `base` plus `path`, `retention_days`. The proof
  mirrors the existing `disk.py` session proof, never a weaker
  one (section 4.3).

Strict tables, following the `projects.d` precedent
(`_PROJECT_KEYS` in `projects.py`): any unknown key is an
error naming the file and the key, never a silent default. A
misspelled selector must refuse, not clean the wrong set.

### 3.1 One resolver for the declared roots

PROPOSED. New module `hygiene.py` owns one resolver,
`hygiene.declared_roots(rules)`, returning `{name: realpath}`
for every rule with a directory root. It is the only function
that joins a rule's `base` and `path`. Validation
(section 3.2) and the big-directory skip (section 5) take
paths only from `declared_roots()` and never join `base`
and `path` themselves. `diskcli` builds its production roots
through it; no second resolver is allowed, and a test asserts
no second `base` plus `path` join exists anywhere (the
AGENTS.md one-fact-one-place rule).

Bases come from a fixed vocabulary, each resolved by its
existing helper: `cache` from `paths.cache_dir()`, `state`
from `paths.state_dir()`, `sessions` from
`paths.sessions_dir()`, and `worktree` from
`worktrees.root(main_clone, root)` for the rule's repo
clone. `path` is relative; an absolute
`path`, a `..` escape, or a symlink that resolves outside the
base is refused. The root must sit strictly below its base,
never equal to it.

### 3.2 Validation is fail-closed (exit 4)

PROPOSED. `hygiene.rules(root)` reads the tables and refuses
the whole load with exit 4 (configuration error, keeping the
public meaning) when any rule is bad, naming the rule and the
reason. Refused roots:

- a root not under a declared base (unknown `base`, absolute
  `path`, `..` escape, symlink escape);
- a root equal to its base;
- a root that is, or contains, the home directory;
- a root that is, or contains, a clone, a live worktree, the
  state directory, the sessions directory, or the cache
  directory itself;
- a root that is, or contains, the heavy-lane ledger dir
  (`heavy.ledger_path`), the host ledger file
  (`host_ledger.ledger_path`), or any `reviews/` record dir;
- an image `prefix` that is empty, that does not start
  with the rule's `repo` followed by `-` or `/` (repo
  scoping, so `acme-dev` is fine for repo `acme` but
  `other-dev` is refused), or that overlaps another
  rule's prefix (one prefix starts with the other).
  Repo scoping makes cross-project races impossible by
  construction; the overlap refusal covers two rules in
  the same repo racing `docker rmi` over the same image.

Validation failure refuses the command before anything is
planned: no partial plan from a half-valid file. Doubt at
load time resolves to refusing, the same way an unresolvable
base in `gc.audit_branches` never fires its verdicts.

## 4. Proofs for each kind

PROPOSED. Planning is pure: `hygiene.plan(rules, ...)` takes
plain data (rule rows, directory entries with mtimes and
sizes, image rows, container image digests, the live ticket
set, registry entries, `now`) and returns candidate rows that
join the `disk` plan as new kinds `hygiene-dir`,
`hygiene-image` and `hygiene-session`. The `proof` string of
every safe row names the rule, the selector values, and the
facts that fired. Unsafe rows are planned as keep rows with
the reason, exactly like `disk.py` does for `UNPROVEN` and
kept sessions.

### 4.1 Directory rules

- `age`: entries whose newest mtime under them is older than
  `days` are safe; anything with an unreadable mtime is kept
  ("modification time is unreadable", the `disk.py` precedent).
  Symlinks are never followed and never removed; an entry that
  resolves outside the declared roots is dropped with a note.
- `count`: entries are ordered by mtime, ties broken by ascending name;
  all but the newest `keep` are safe. The plan names the
  survivors, so a reviewer can see what stays. Apply re-sorts
  before deleting: a new entry between plan and apply shifts
  the cut, never deletes the wrong file.
- `size`: entries are ordered oldest first; the plan walks the
  order and marks safe until the root would measure under
  `max_bytes`. Apply re-measures and stops the moment the
  root is under budget; the rest is kept with the reason
  "under budget now".

### 4.2 Image rules

The lister runs `docker images --filter reference=<prefix>:*`
and parses repository, tag, image ID and creation time; the
container side runs `docker ps` and resolves each container to
its image digest. An image is safe when no running container
uses its ID and, when the rule sets `older_than_days`, its
creation time is older than the floor. The plan carries one
row per image ID, listing all its tags, so several tags
sharing one ID never plan twice. Kept images say why:
"used by container `<name>`" or "built `<n>`d ago, floor is
`<m>`d".

Apply re-lists both sides just before `docker rmi <id>` and
keeps anything that became referenced or cannot be resolved
to a digest anymore. Removal names the ID, never the tag: a
tag can move to a new image between plan and apply, an ID
cannot.

### 4.3 Session rules

The proof is the `disk.py` session proof unchanged: the
registry entry for the ticket is done, no live unit holds it
(the `units.live_tickets` set over every registered
`unit_prefix`, the `diskcli.production_live` precedent), and
the newest mtime under the leaf is older than
`retention_days`. Leaf names that do not parse as ticket dirs
(`paths.parse_ticket_dirname`) are `UNPROVEN` keep rows:
"has no registry entry: cannot prove nothing is lost".
Leaves whose parsed repo differs from the rule's `repo` are
skipped with a note ("belongs to repo `<other>`, rule is
for `<repo>"), never candidates under this rule. A
`reviews/` subtree is never a candidate; when a leaf holds
one, apply removes everything except it.

### 4.4 Build caches

Build caches under a user cache directory are `dir` rules
under the `cache` base (age, count or size to taste). The
Docker builder cache is out of scope for version one: `docker
builder prune` is host-wide, not prefix-scoped, and no
per-project proof exists for it. A later ticket may revisit
this with an `until=` filter once the section 8 probes
settle; until then the doc records the refusal, not a guess.

### 4.5 What a refusal looks like

Config errors refuse on stderr with exit 4 and plan nothing:

```text
korapy disk: hygiene.acme-scratch: root resolves outside its base
```

Proof failures are plan rows, never silent. In human output,
following the `diskcli.format_plan` shape:

```text
  [keep] hygiene-dir /home/someone/cache/acme/scratch/run-9 (5000 bytes): kept: touched 2d ago, rule keeps 7d
  note: dropped /home/someone/cache/acme/scratch/link: is a symlink
```

In `--json`, the same rows carry `"safe": false` with the
reason in `"proof"`, so automation branches on data, not on
text. Apply-time failures append kept rows with reasons
("referenced now", "touched since the plan", "under budget
now"), the `disk.ApplyResult.kept` precedent.

## 5. Interaction with gc, disk pressure and the heavy lane

- `gc` verdicts are untouched: `classify`, the `SAFE` tuple
  and every branch category keep their meaning. `korapy gc`
  stays clone-scoped and never lists hygiene rows; hygiene
  never deletes a branch or a worktree. Worktree reclaim
  stays with `gc --apply`, the `disk.py` precedent.
- `disk` pressure is untouched: `read`, `pressure` and the
  `min_free_pct` / `min_free_gb` thresholds keep their
  meaning, and hygiene inputs never change a reading. Hygiene
  rows only join the candidates, largest first, in the same
  ordering. Subdirs covered by a hygiene rule are skipped by
  the `BIG_DIR_BYTES` sweep, which takes its managed paths only
  from `declared_roots()`, so one root is never reported
  twice: hygiene owns them (one fact, one place).
- The heavy-lane ledger and the review records are never
  candidates. The resolver excludes the heavy ledger dir, the
  host ledger file and every `reviews/` dir even when they
  sit under a declared root (dropped with a note), and a
  rule whose root equals one of them is an exit 4 refusal
  (section 3.2). Learning rows and review evidence are
  append-only proof; no hygiene selector may name them.

## 6. Seams

PROPOSED. The core stays pure; every read and every deletion
goes through an injectable seam, following the `gc.git`,
`disk.plan` `usage` / `walk` parameters and
`diskcli.production_remove` precedents:

- `scan(root)` lists directory entries with mtime, size and
  symlink bits. Production reuses the `disk.measure` /
  `disk.newest_mtime` walk shape; tests inject canned trees.
- `list_images(prefix)` and `list_containers()` wrap the two
  Docker reads. Production parses the `--format` output;
  tests inject canned rows. Nothing in core imports Docker or
  runs a subprocess.
- `remove_path(path)` reuses the contained remover shape
  (inside declared roots, never a root itself, never a
  symlink). `remove_image(image_id)` runs `docker rmi`
  through the injected `run` seam. Both are required
  arguments with no default: a caller that passes nothing
  fails closed instead of falling back to a real deletion,
  the AGENTS.md destructive-seam rule.
- `live()` (the `(repo, number)` tickets) and registry
  entries reuse the `disk.plan` inputs unchanged.

Fakes live beside the tests and return canned listings,
including adversarial ones: a tag that moves between plan
and apply, a container that starts between plan and apply, a
symlink planted under a root, an unreadable mtime. A guard
test asserts no hygiene test reaches a real kill or a real
Docker call, the same way the existing suite guards the stop
paths. The one production-only test calls the real path
remover against a temp dir, the
`test_real_production_remove_contained_to_temp_dir`
precedent; no test touches the real state, sessions or cache
dirs.

## 7. Worked example

A project builds development images tagged `acme-dev:<hash>`
and keeps a build cache plus check scratch under its user
cache dir. Stand-ins throughout; nothing here names a real
host or project.

```toml
[hygiene.acme-images]
repo = "acme"
kind = "image"
prefix = "acme-dev"
selector = "unreferenced"
older_than_days = 3

[hygiene.acme-build-cache]
repo = "acme"
kind = "dir"
base = "cache"
path = "acme/build-cache"
selector = "size"
max_bytes = 10737418240

[hygiene.acme-scratch]
repo = "acme"
kind = "dir"
base = "cache"
path = "acme/scratch"
selector = "age"
days = 7
```

`korapy disk` without `--apply` then reports, alongside the
existing rows:

```text
pressure: low
  /home/someone/state/korapy: 6.00 GB free (12%)
  [safe] hygiene-image deadbee (tags acme-dev:aa00001, acme-dev:aa00002; 2.00 GB): rule acme-images; no container uses it; built 8d ago, floor 3d
  [keep] hygiene-image facefeed (tags acme-dev:aa00003; 2.00 GB): kept: built 21h ago, floor is 3d
  [safe] hygiene-dir /home/someone/cache/acme/scratch/run-4 (1.50 GB): rule acme-scratch; untouched for 9d, a regular entry inside the cache dir
  [keep] hygiene-dir /home/someone/cache/acme/scratch/run-9 (5.00 MB): kept: touched 2d ago, rule keeps 7d
```

With `--apply`, each safe row is re-verified just before
deletion (image still unreferenced, scratch still old, cache
still over budget) and anything that changed is kept with
its reason. The heavy ledger, the host ledger and every
`reviews/` dir never appear, even under a declared root.

## 8. Verified versus UNVERIFIED

Verified on one host while writing (Linux 7.0.0-38-generic
x86_64, Docker client and server 29.9.0), each read-only:

- `docker images --format` renders repository, tag, short ID
  and creation time columns for local images.
- `docker images --filter 'reference=<glob>'` filters
  server-side by name glob, so the lister never parses the
  full local image set.
- `docker ps --no-trunc --format '{{.Image}}'` yields the
  full `sha256:<id>` digest per running container, a stable
  identity for the unreferenced check.
- `docker system df -v` shows a per-image `CONTAINERS`
  count, a second unreferenced signal.

UNVERIFIED, each with the safe probe that settles
it (probes 1-4 are read-only: run none of those with a
mutating flag while learning; probe 5 mutates only scratch
images on a scratch host):

1. The exact `reference=` glob dialect across daemon
   versions (does `*` cross the `/` in `registry/repo`?).
   Probe: `docker version --format '{{.Server.Version}}'`
   beside `docker images --filter 'reference=<probe-glob>'`
   with a glob that matches nothing, and read the daemon
   docs for the running version.
2. Whether `.Image` in `docker ps` is always the digest,
   even for containers started by name before a rebuild.
   Probe: `docker ps --no-trunc --format '{{.Image}}'` next
   to `docker image inspect --format '{{.Id}}'` for the same
   name after rebuilding its tag.
3. Whether `docker system df` reclaimable accounting is a
   safe admission input for a later builder-cache ticket.
   Probe: `docker system df -v` (read-only) and
   `docker builder prune --help` (help text only); never run
   a prune to learn.
4. Whether creation time alone orders same-name rebuilds
   when a registry is involved. Probe:
   `docker images --format '{{.Repository}}:{{.Tag}}
   {{.ID}} {{.CreatedAt}}'` before and after retagging a
   scratch image.
5. What `docker rmi <id>` does when several tags share one
   image ID (whether the image goes away or only untags,
   and which tag names remain). Probe, on a scratch host
   only: build a scratch image, tag it twice, remove by ID,
   and list the remaining tags; never run this against a
   real project image. Settles before ticket 3.

## 9. Implementation ticket breakdown

Small, independent, in order. Each ships with tests using
fakes at the section 6 seams, and none touches Docker or the
real filesystem in tests.

1. Config and validation: `hygiene.rules`, the strict keys,
   the exit 4 refusals (including image prefixes that do
   not start with the rule's repo and prefixes that
   overlap across rules), plus the resolver test that pins
   one fact one place by asserting no second `base` plus
   `path` join exists.
2. Directory planning: age, count and size selectors as pure
   code over injected scans, with adversarial fakes
   (symlink, unreadable mtime, escape, an mtime tie under
   `count` proving the documented name order).
3. Image planning: prefix filtering and the unreferenced
   proof over injected image and container listings, one row
   per image ID listing all its tags, including a tag that
   moves between plan and apply; starts after the section 8
   multi-tag `rmi` probe settles.
4. Session planning: the `disk.py` proof reused, non-ticket
   names kept as unproven, `reviews/` never a candidate.
5. Disk wiring: plan section, `--apply` with re-verification,
   human and `--json` output, the ledger and review
   exclusions, the big-dir skip for managed roots, the
   README `--apply` sentence and the `diskcli.py` help
   text once hygiene rows become apply-eligible.
6. Probe follow-ups: settle section 8 on a scratch host and
   file the builder-cache decision (adopt or refuse) with
   evidence.

## 10. Options

Options:
1. `[hygiene.<name>]` tables, recommended
2. `[[repo.hygiene]]` subtables per repo
3. Rules in `projects.d` registration files
4. A separate `hygiene.toml` beside korapy.toml
5. Defer sessions, ship dirs and images first
6. Do nothing, keep hand deletion

The recommendation is option 1: the ops root file is already
the project-owned policy surface that `settings.load` reads,
top-level tables keep rule names stable across repo renames,
and strict keys give fail-closed validation in one place.
