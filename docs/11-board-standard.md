# 11: the standard project board, its provisioning and checks

Status: design, awaiting maintainer approval. No code ships with this doc.
Implementation waits for approval of the open decisions in section 9.

Every project that uses corral should have a hosted project board, and
corral itself should have one too. That is the standard this doc designs:
what the board holds, how corral provisions it idempotently, how the
provisioned identifiers reach `corral.toml` without a person copying ids
by hand, how existing issues are adopted into a new board, and what the
doctor checks. GitHub Projects v2 is the first and only target today.
Another forge later is a new adapter behind the same interfaces
(`00-design.md` section 2, `07-board-fields.md` section 0); section 6
states what the interface must not assume so that adapter fits.

`07-board-fields.md` designed and built the board-field tracker variant:
reading and writing a board a person made by hand, mapped by hand in
`corral.toml`. This doc removes the "by hand". It changes no concept, no
ownership rule and no exit code meaning from that doc.

## 0. Where this starts: what exists, what does not

Checked against the code, not against the older docs:

- The board variant is built: `tracker.variant` (`labels` default,
  `board`), the per-repo `[repo.board]` table validated in
  `settings._check_board`, the GraphQL reader and writer in
  `github/board.py`, the one factory in `trackers.build_tracker`, the
  board sync step in `sync.sync_board_repo`, and one doctor row per
  board-mode repo in `boarddoctor.check_sites`.
- The label bootstrap is built, and it is the precedent this design
  extends. The `Tracker` protocol in `tracker.py` carries
  `ensure_labels(repo)` returning a `LabelReport` (created, kept,
  refused), implemented by `GithubTracker.ensure_labels` and delegated
  by `BoardBackedTracker`. Provisioning on that protocol is therefore
  an established shape in corral, not a new idea.
- `corral github setup` is a built verb (`githubcli.cmd_setup` and
  `githubcli.setup_root`). It runs `ensure_labels` on every named
  `[[repo]]` entry, continuing past a `TrackerError`, and aggregates
  the exit code across entries: any failed repo is 3, else any refused
  label is 1, else 0 only when something was created, else 1. Today
  it creates labels only; no board half exists. `init` prints it as
  its next step and never runs it.
- `corral new` is built, including `--create-remote` (`new.py`): after
  creating the remote it bootstraps labels through the same
  `ensure_labels` (`new._bootstrap_labels`); without the flag it
  prints `next: corral github setup` and runs nothing outward. This
  doc designs only the board operation those built callers invoke; it
  does not re-design `new` or the label half of `setup`.
- Nothing creates a board. No `ensure_board` exists anywhere, and
  board mode assumes a person made the Project and mapped its fields
  by hand. That gap, and only that gap, is what this doc designs.

Two consequences. First, board provisioning extends the existing
`github setup` verb and joins the label bootstrap in that one
operation, so there is still exactly one code path that creates
outward tracker state (`06-corral-new.md` section 4: "one code path
owns this"). No new verb is added, and the label half is not rebuilt.
Second, board mode today
assumes a person made the Project, created the fields, read the option
ids out of the provider, and typed them into `corral.toml`. Every step
of that is a place to copy the wrong id. Provisioning writes the
mapping from what the provider returned, so the ids in the file are
the ids on the board by construction.

Non-goals: views and board layout (grouping, ordering, saved views are
people's presentation, never corral's contract; the fields are the
standard, the views are not), provider workflows and automation rules,
deleting or renaming anything on a board (no delete operation is
designed anywhere in this doc), backfilling people-owned field values
(`07-board-fields.md` section 6 keeps that a people action), and a
second provider (section 6 only keeps the door open).

## 1. The standard board

Decision: corral ships one standard board specification, as data in one
place, naming the seven concepts `07-board-fields.md` section 1 fixed
and nothing else. corral code keeps naming concept keys only; the
standard names the default board spelling of each concept. A project's
`corral.toml` remaps any name, and the remap wins everywhere (section 3
writes the effective names into the file, so after provisioning the
file never depends on the defaults again).

| Concept | BoardConfig key | Standard name | Kind | Owner |
| --- | --- | --- | --- | --- |
| `agent` | `agent_field` | `Agent` | text | corral (outbox) |
| `status` | `status_field` | `Agent state` | text | corral (outbox) |
| `board_status` | `status_column` | `Status` | single-select | people (read-only) |
| `priority` | `priority_column` | `Priority` | single-select | people (read-only) |
| `area` | `area_column` | `Area` | single-select | people (read-only) |
| `pool` | `pool_column` | `Pool` | single-select | people (read-only) |
| `blocked_by` | `blocked_by_column` | `Blocked by` | text | people (read-only) |

The kinds are not a choice: `github/board.py` already requires text
for `agent`, `status` and `blocked_by` and single-select for the four
columns (`_TEXT_CONCEPTS`, `_SELECT_CONCEPTS`, checked in
`_parse_field`). The names are the ones `07-board-fields.md` section 5
uses in its example table, so the standard and the built example agree.

`milestone`, `parent` and `children` from the concept list are not
board fields and stay out of the standard: the milestone reads from the
REST issue payload, the edges from the sub-issues endpoints
(`07-board-fields.md` section 1). Provisioning never touches them.

### Options

- `Status`: a field corral creates gets at least `Todo`,
  `In Progress` and `Done`. Those names are creation defaults only:
  corral assigns them no meaning and never tops up a kept field's
  options by name (section 2, step 6). corral carries the display
  name verbatim into `extra["board_status"]` and gives it no meaning; what a status
  allows is project eligibility policy, as today. A new GitHub Project
  is believed to ship a built-in `Status` single-select field with
  exactly these options (UNVERIFIED: section 9, item U1). When it does,
  provisioning keeps the built-in and creates nothing for this concept.
- `Priority`: five options, one per rank in the closed set every
  adapter already parses (`PRIORITIES` in `tracker.py`, the provider
  neutral interface module; `github/tracker.py` only imports it:
  `urgent`, `high`, `normal`, `low`, `backlog`, highest first). The
  rank mapping the provisioning writes is identity by rank:

  | Option name | Rank (`priority_map` value) |
  | --- | --- |
  | `Urgent` | `urgent` |
  | `High` | `high` |
  | `Normal` | `normal` |
  | `Low` | `low` |
  | `Backlog` | `backlog` |

  Matching at runtime stays by stable option id, never by name
  (`07-board-fields.md` section 2). Names are matched exactly once, at
  provisioning time, to build the id map that section 3 writes; a
  display-name rename afterwards is free, a deleted or replaced id is
  a doctor finding, both as today. `urgent` keeps its meaning from
  AGENTS.md: setting it is the maintainers' alone on corral's own
  board, and the equivalent people's call elsewhere; corral only
  provisions the option.
- `Area` and `Pool`: the standard names the fields and no options.
  Areas and pools are the project's own vocabulary, and no generic
  list is honest. A single-select field may not be creatable with zero
  options (UNVERIFIED: U2), so provisioning creates these two fields
  only when at least one option is declared for them. The only
  declarer in version one is the operator running setup, through
  repeatable `--area NAME` and `--pool NAME` flags on
  `corral github setup` (decision O6): there is deliberately no
  `corral.toml` key for the lists, because once the field exists its
  options are people's board state, and a config copy would be a
  second resolver for the same fact. Undeclared and unmapped, the
  concept stays out of the spec, which the reader already treats as
  absent; a later setup run with the flags creates the field then.
  Once a project's `corral.toml` maps the concept (`area_column` or
  `pool_column` is set, whether by an earlier flagged setup run or by
  hand), it enters every later spec as **keep-only**: the field must
  exist with the right kind and is then kept exactly as found, and no
  option is required, checked or appended. Options are required only
  at creation. A keep-only field that is absent from the board cannot
  be created without declared options, so it is a refused line naming
  the `--area` or `--pool` flag as the fix. This is what makes a
  plain re-run after a flagged run a no-op: the spec still names the
  concept, the field is kept, and section 3 renders the same table.

### What corral requires, what a team may add

Required for board mode to be healthy (doctor section 5 enforces):

- The Project resolves by owner plus number, or by node id.
- `Agent` and `Agent state` exist as text fields. Writes need them: an
  unmapped corral-owned concept raises `UnknownOption` at mirror time
  and the write is dropped, so a standard board maps both.
- Every concept the project maps in `corral.toml` exists on the board
  with the kind above, and every option id in `priority_map` exists on
  the live `Priority` field. This is the existing doctor contract.

The standard provisions all seven fields above (subject to the
`Area`/`Pool` rule). A team may then, without breaking the standard:

- add fields of any kind, for any purpose corral does not know about;
- add options to people-owned columns. An option on `Priority` that
  `priority_map` does not name reads as concept-absent and keeps the
  stored value, exactly as an unmapped option does today;
- rename option display names (ids are stable, names are comments);
- add views, groupings and provider workflows.

A team breaks the standard, and the doctor says so, by deleting or
replacing a mapped field or a mapped option id, changing a mapped
field's kind, or using the two corral-owned text fields for people
meaning (a person's edit there is handled by the settlement rules, but
the meaning is theirs to lose, per `07-board-fields.md` section 1).

Board title: the standard title for a board corral creates is
`<repo> board` (for `example/repo`, the title is `repo board`),
remappable through the same spec (decision O7). The title is how a
re-run finds a board its config does not address yet (section 2), and
for nothing else: runtime lookup stays owner plus number or node id,
because titles are not unique and people rename things.

## 2. Provisioning: one idempotent ensure operation

Decision: provisioning is one ensure operation on tracker-side types,
implemented once in the GitHub adapter, invoked by name from the
callers in the table below and from nowhere else. It creates what is
missing and never alters or deletes what exists, the same contract as
the label bootstrap in `06-corral-new.md` section 4.

### The interface shape

The operation does not go on the `Tracker` protocol, even though
`ensure_labels` sets a precedent for provisioning there. The
difference is the unit of work. Every `Tracker` method, `ensure_labels`
included, takes a repository and works on that repository; board
ensure needs a Project, not a repository: it resolves an owner, finds
or creates an owner-level board, and only then links a repository to
it. It must also run in the two bootstrap states where no board
tracker can exist yet (a labels entry, and board mode with no
`[repo.board]` table; see the factory below), which a method reached
through a built repo tracker cannot do. So the board half is a
separate provisioner object: `tracker.py` gains the provider-neutral
types and a separate narrow protocol, in the same style as the rest of
that module. `ensure_labels` stays where it is; nothing moves.

```python
class FieldSpec(NamedTuple):
    concept: str            # "agent" | "status" | "board_status" | ...
    name: str               # the effective board spelling of the concept
    kind: str               # "text" | "single_select"
    options: tuple[str, ...]  # standard option names, single-select only
    keep_only: bool = False  # mapped Area/Pool: must exist, never created or topped up

class BoardSpec(NamedTuple):
    owner: str              # board owner login (user or organization)
    title: str              # "<repo> board" by default
    repo: str               # "owner/name" to link, or "" for no link
    fields: tuple[FieldSpec, ...]
    private: bool = True
    priority_map: dict[str, str] = {}  # the config's existing id -> rank map, if any

class BoardProvision(NamedTuple):
    owner: str
    number: int | None      # the provider's per-owner number, when it has one
    project_id: str         # the provider's opaque node id
    fields: tuple[FieldSpec, ...]  # concept -> live name, after the final read-back
    created: tuple[str, ...]  # "project", "field:Agent", "option:Priority/High", "link:example/repo"
    kept: tuple[str, ...]
    refused: tuple[str, ...]  # one fix line per refusal
    priority_map: dict[str, str]  # live option id -> rank, for corral.toml

class BoardProvisioner(Protocol):
    def ensure_board(self, spec: BoardSpec) -> BoardProvision: ...
    def populate(self, project_id: str, repo: str) -> BoardPopulate: ...
```

`populate` is section 4's operation; `BoardPopulate` is the analogous
report (`added`, `kept`, `refused` as fix lines). Both methods raise
`TrackerError` (exit 3) for an unreachable or unusable provider and
`TrackerRateLimited` for a rate-limit answer, like every other
adapter call. A spec that cannot be executed as given (unresolvable
owner, ambiguous title in step 2 below) raises `ValueError` naming
the conflict, the established shape for a refused write in
`tracker.py`; setup counts it as a 4 for that entry in the cross-entry
aggregation below. A provision that ran but met refusals returns
normally with them in `refused`, counted as a 1 for that entry.

`STANDARD_BOARD` (the section 1 table as data) and a pure
`effective_spec` helper live beside these types: the one place that
turns "what the project mapped, else the standard name" into a spec.
Its inputs are the resolved `BoardConfig` (or `None` in the bootstrap
state), the entry's `tracker.repo`, and the operator's setup flags.
Derivation, in one place so no caller re-derives it: the owner is the
`--board-owner` flag when given, else the config's `project_owner`
when a table exists, else the tracker repo's owner; the title is
`<repo> board` from the tracker repo's name unless `--board-title`
overrides; each field name is the config's name for the concept when
mapped, else the standard name; the config's existing `priority_map`
rides into the spec so the planner can resolve renamed options by id
(step 6); `Area` and `Pool` enter the spec with at least one declared
option, or as keep-only when the config already maps them, and not at
all otherwise (section 1). No second resolver for field names may
appear; the reader keeps resolving names through `BoardConfig`, the
provisioner through this helper, and after a successful provision the
two agree because section 3 writes the provision's live names into
the config.

The GitHub implementation, `GithubBoardProvisioner`, lives in
`github/board.py` next to `BoardReader` and `BoardTracker`. Unlike
them it is constructed with only the `run(args)` seam and the pacer,
never a `BoardConfig`: `BoardReader` requires a resolved board table
(owner plus number or id), and `trackers.build_tracker` returns a
plain `GithubTracker` for a labels entry and refuses board mode
without a table, so neither can exist in the bootstrap states
provisioning exists for. A small factory beside
`trackers.production_trackers`, `production_board_provisioner(*, run=None, pacer=None)`,
builds the provisioner the same way `build_tracker` wires board mode
(the raw `gh` runner; GraphQL is paced through the shared bucket) and
is the one production wiring. `githubcli.cmd_setup` passes it into
`setup_root` as an injected `provisioner_factory` seam next to the
existing `tracker_factory`; tests inject a fake. Mutations go through
`gh.graphql`, so they are charged at mutation cost and rate-limit
answers raise `TrackerRateLimited` through the shared classifier,
exactly like board reads and writes. Nothing outside the adapter
knows a mutation name or a field-creation shape.

Whether an adapter supports provisioning is what the factory says:
it returns an object with a callable `ensure_board`, or it does not
build one at all. Asking setup to provision a board when the factory
yields nothing is a usage error counted as exit 4 in the aggregation
below ("this tracker does not provision boards"), never a silent
skip and never a missing-attribute crash on a repo tracker.

### The ensure algorithm

Each step checks its own precondition against the live board, so a
partial run converges on re-run and the board itself is the state; no
ledger is kept on disk (the `06-corral-new.md` section 5 rule):

1. **Resolve the owner.** The owner login comes from the spec. This
   needs a new owner query, not an existing one: `BoardReader._fetch_project`
   resolves a *project* by number across user and organization (it
   asks both for `projectV2(number)` and accepts exactly one); it
   resolves neither an owner's type nor its node id. Provisioning
   asks for the login as both a user and an organization, accepts
   exactly one, and reads that owner's node id for the create step
   (UNVERIFIED: U11, the query shape and its answer for an unknown
   login). An owner that resolves to neither raises `ValueError`
   (exit 4 in the aggregation) before any mutation. The standard is
   one board per tracker repo, but the operation takes the repo to
   link as data and does not forbid other repos linking the same
   board later.
2. **Find the Project.** By node id when the config has one, else by
   owner plus number, else, and only during provisioning, by exact
   title under the owner (paged list, exact string match, nodes
   validated like every other board answer). Zero title matches means
   create. Two or more title matches raises `ValueError` (exit 4 at
   the CLI) naming every matching number, before any mutation: corral
   never picks one (the same ambiguity in `_fetch_single` already
   fails closed the same way).
3. **Create the Project when missing.** One create mutation with the
   owner id and title (UNVERIFIED: U3, the mutation name and inputs).
   Visibility: private by default, the `06-corral-new.md` section 3
   rule for remotes applied to boards. A create may not accept a
   visibility input (UNVERIFIED: U4); the design therefore reads the
   new Project back and, when it reports public and the spec asks for
   private, sends one update to make it private before anything else
   is put on it. Making a just-created board private is part of
   creating it as specced; it is the only update an ensure ever sends.
   The correction gates everything after it: when the update errors,
   provisioning stops before the link and field steps and the error
   propagates as `TrackerError` (counted as exit 3 for the entry);
   when the update reports success but the board still reads public
   on the re-read, provisioning likewise stops before link and
   fields, with a refused line naming the manual fix (counted as
   exit 1). Corral never puts fields, links or items on a board it
   knows is not private as specced; a re-run after the fix converges.
   An existing Project's visibility is never flipped. When an existing
   board the config addresses directly (by number or id) is public
   while the spec asks for private, the report carries a refused line
   with the manual fix and provisioning continues: the config naming
   that board is the people's decision to use it. When corral found
   the board by title search and it is public while the spec wants
   private, provisioning stops before any field step, exit 1 with the
   fix line: adopting a public board a person made, on a title match
   alone, is not a default corral takes.
4. **Link the repository.** When the spec names a repo, link it to the
   Project (UNVERIFIED: U5, the link mutation and its already-linked
   answer). The board reader does not need the link (it queries by
   owner plus number); people do, since the link is what surfaces the
   board from the repository. Already linked reads as kept. A repo
   the account cannot administer refuses this step only (R5 in
   `01-onboarding.md`: someone else's repository), with a fix line,
   and the rest of the provision stands.
5. **Ensure fields.** Read the live schema once (the `_SCHEMA_BY_NUMBER`
   and `_SCHEMA_BY_ID` shapes, extended only if the spike shows a
   missing datum), as a raw field list for the planner, not through
   `BoardReader._project_index`: that index raises `TrackerError` on a
   duplicate field name, which is right for the passes but wrong here.
   Duplicate names on the live board are a planning refusal, never an
   unreachable error and never a silent pick: two fields with one
   name, or two options with one name on a field the spec names, each
   produce a refused line ("rename or delete the duplicate on the
   board, then re-run") and no step plans against the ambiguous name.
   For each unambiguously named spec field, in spec order:
   - name present with the right kind: kept, byte-for-byte as a person
     left it (description, option colors, ordering are never touched).
     A keep-only field stops here: present is the whole contract;
   - name present with the wrong kind: refused, with a fix line
     ("rename or delete the field on the board, then re-run"), and no
     lookalike field is created beside it;
   - name absent and keep-only: refused, with the fix line naming the
     `--area` or `--pool` options that would create it (section 1);
   - name absent: create it, with its standard options for a
     single-select (UNVERIFIED: U6, the create-field mutation, the
     `dataType` spellings, and whether options ride along at creation).
   Built-in fields the spec does not name (titles, assignees and the
   like) are ignored, like any other team field. A spec field whose
   name matches a built-in of the right kind (the expected `Status`
   case, U1) is kept like any existing field.
6. **Ensure options, rename-is-free.** Appending by missing *name*
   would undo a person's rename: rename `High` to `P1` (the id is
   stable, the map still valid) and a name-based top-up would append a
   second, duplicate-meaning `High` to a people's field. So on a kept
   `Priority` field the planner resolves ranks through the spec's
   existing `priority_map` ids first: a rank whose mapped id is live
   on the field is present, whatever that option is now called; then
   a rank whose standard name is live is present. Only a rank with
   neither a mapped live id nor the standard name is appended, under
   its standard name. The equivalent rule for `Status`: corral keeps
   no id map for it and gives its names no meaning, so there is no
   evidence by which a missing standard name is missing rather than
   renamed; corral therefore never appends options to a kept `Status`
   field at all. Its standard names are creation defaults (section 1),
   and a kept field's options are wholly people's. Keep-only fields
   are never topped up either. The provider's option update is
   believed to replace the whole option list, so the one append
   update corral may send (on `Priority` only) is the existing list,
   in its existing order, with the missing ranks' names appended;
   existing ids must survive it (UNVERIFIED: U7). If the spike shows
   ids do not survive an append, this step does not ship as designed:
   a kept field with an unrepresented rank becomes a refused line
   instead, because rewriting ids would silently invalidate every
   `priority_map` in the wild. Options are never removed and never
   renamed by corral.
7. **Read back and report.** One final schema fetch is the truth the
   report and section 3 are built from: project owner, number and node
   id, the live per-concept field names (`BoardProvision.fields`,
   live concepts only, so a refused field renders nothing),
   per-field and per-option created/kept, every refusal with its fix
   line, and `priority_map` built rename-is-free: first the spec's
   existing id-to-rank entries whose ids are still live, then, for
   ranks still unrepresented, the live option carrying the standard
   name. Duplicate option names were refused in step 5, so a name
   never silently picks one of two ids here. A rank with neither a
   live mapped id nor a standard-named option after step 6 is a
   refused line, and the map omits it, so the file never names an id
   the board does not hold.

A run that creates nothing reports all-kept and the caller exits 1
(nothing to do) when the config file is also already written, or 0
when it still writes the mapping. Per-board exit codes, public per
AGENTS.md:

| Situation | Exit |
| --- | --- |
| Board ensured and mapping written, or items populated | 0 |
| Nothing to do (board and file already converge) | 1 |
| A refusal a person must resolve (no permission, wrong kind, duplicate field or option name, public board where private was specced, still public after the correction, link refused) | 1, one fix line each |
| Tracker or host unreachable, rate limit outlasts the pacer cap, the make-private update errors | 3 |
| Bad spec or config (unresolvable owner, ambiguous title, a different board table already in the file, unknown concept in an extended spec, no provisioner for the adapter) | 4 |
| Admission slot | 75 is never used; setup starts no agents |

Across entries, setup adopts the aggregation the built verb already
uses in `githubcli.setup_root`, extended with where board 4s rank.
Every named entry is attempted, in entry order; a per-entry failure
of any kind is recorded and the loop continues with the next entry,
never aborting the rest (the built loop already continues past a
`TrackerError`; ticket 4 gives board `ValueError`s, different-table
refusals and provisioner-missing errors the same continue treatment).
Validation that the built verb does before the loop (no root, unknown
entry name, unparseable settings) still exits 4 before any entry
runs, unchanged. The final exit is the first of these that happened:

1. any usage or conflict error on any entry: 4;
2. else any entry unreachable (`TrackerError` from labels, board or
   populate): 3, even when another entry created something;
3. else any refused line, or a pass that created nothing at all
   (every label, field and file already kept): 1;
4. else 0: something was created (a label, a project, a field, an
   option, a link, a mapping write, a populated item) and nothing
   failed, was refused or conflicted.

An entry's work stands when a later entry fails: labels and board
state it created stay created, and the mapping file section 3 wrote
for it stays written. There is no cross-entry rollback; there cannot
be one for outward state. `new --create-remote` runs a single entry,
so the same outcomes map onto its existing codes through its
`_bootstrap_labels` precedent (section 8, ticket 7).

Two `setup` runs racing can still create two same-title boards when
both look before either creates (titles are not unique, U3): the
create is not guarded by a title check, and no post-create re-list
uncreates the second board. Duplicate titles are a documented risk
of the operator command, flagged for a person to resolve: the next
setup run refuses at step 2 naming both numbers, the doctor's fix
line points at that setup run, and a person deletes one board by
hand. Setup is an operator command, not a fleet pass; no lock is
designed for it, and the failure is closed and visible.

### The token scope

UNVERIFIED throughout (U8): Projects v2 over GraphQL is believed to
need the `project` OAuth scope for writes and `read:project` for reads
on a classic token (or the Projects read/write permission on a
fine-grained one), which the scopes that suffice for issues and PRs do
not include. Board reads and writes today ride on whatever token `gh`
already holds, so a host can run label mode for years and only meet
this at the first board call. The design states the fix line, never
runs it: `gh auth refresh -s project` (exact scope string per the
spike). Verification belongs to the spike in section 8, ticket 0,
which checks, on a throwaway owner and only with the maintainer's
go-ahead (creating boards is approval-gated, AGENTS.md): what
`gh auth status` reports before and after, which of read and write
fail without the scope and with what error text, and whether that text
is what `gh`'s existing `_classify` already calls `scope`, so the
doctor row in section 5 keys on a real answer rather than a guess.
Until the spike runs, every scope claim in this doc is marked
UNVERIFIED and no code may hard-code the string except behind the
spike's outcome.

### Who invokes it

| Caller | Behavior |
| --- | --- |
| `corral github setup` | Runs it (labels first, then the board, then the mapping write). The verb exists (`githubcli`); the board half extends it (ticket 4). The verb is the operator's approval, per `06-corral-new.md` section 3. |
| `corral new --create-remote` | Runs it after creating the repository and the labels, through the same operation and provisioner factory, never a second implementation. `new` exists; ticket 7 adds the board call after its built `_bootstrap_labels`. |
| `corral new` without the flag | Prints `corral github setup`, runs nothing outward. |
| `corral init` | Prints `corral github setup`, exactly as today; never runs it. |
| `corral doctor` | Prints it as the fix line, never runs it (outward fixes are printed, `01-onboarding.md` section 6). |
| `sync`, `mirror`, `drive`, `launch` | Never. A missing field at pass time stays an error line and a doctor finding (`07-board-fields.md` section 4); fleet passes create nothing. |

Which repos setup boards: for each `[[repo]]` entry setup is asked to
cover, the label bootstrap always runs; the board ensure runs when the
entry's `tracker.variant` is `board`, or when the operator passes
`--board` for a labels entry to pre-provision before a flip (section 3
writes the inert table; the flip stays the separate config edit of
`07-board-fields.md` section 6). A labels entry without `--board` is
labels-only, byte-identical to the `06` design.

`setup --plan` prints what it would do and does none of it, on either
half of the verb. The label half becomes report-only: `ensure_labels`
always mutates, so plan never calls it; instead the adapter's label
list read (the read `GithubTracker.ensure_labels` already starts
from) reports each bootstrap label as would-create or kept, and no
label is created. Ticket 4 exposes that read as the plan seam; the
`ensure_labels` contract itself does not change. The board half reads
the live board (a read is the only way to know created from kept),
lists the planned creations, and shows the mapping write as the
`init.plan_diff` of the target file. No mutation of any kind and no
file write happen under `--plan`. A plan against a board that does
not exist lists the full creation set from the spec.

## 3. Writing the mapping into `corral.toml`

Decision: the provision's identifiers are written by machine, through
`init`'s TOML machinery, into the one `corral.toml` that already
drives the repo entry. No person copies an id, and no second
`corral.toml` writer appears (`06-corral-new.md` section 1: there is
exactly one writer of that file; this design adds one helper to it,
not a rival).

### Which file

The file is the ops root's `corral.toml` for the entry: in the inline
layout the project's own file, in the split layout the ops root's
file and never the project clone, where corral writes nothing tracked
(R1 in `01-onboarding.md`). The root is the one root the built verb
already resolves: `settings.find_root` returns the nearest single
directory at or above the working directory holding a `corral.toml`
(it finds one root, never one of several candidates), and
`githubcli.cmd_setup` runs against that root today. Setup adds no new
way to find it. No root above the working directory, or an entry name
that root does not hold, is the built verb's pre-loop exit 4, with
`corral init` as the fix when there is no root: provisioning never
onboards as a side effect.

### The write

A new helper beside `_split_toml_change`, on its precedent:
`board_table_change(toml_target, entry_name, spec, provision)` returns
`(new_text, is_noop)`. The renderer needs both arguments: the spec
says which concepts were asked for, and `provision.fields` (with
`provision.priority_map`) says which of them are live under which
names after the read-back, so the seven names in the rendered table
come from the provision, never re-derived from defaults, and a
concept the provision refused renders nothing. `--force` changes none of the rules below. The
stdlib parses TOML but does not write it, so like every other corral
edit of this file the change is textual, block-shaped, and every byte
it does not own is preserved:

- **No `[repo.board]` table yet:** insert the rendered table
  immediately after the named entry's own block (after its last
  `[repo.*]` subtable, before the next `[[repo]]` header or end of
  file), where TOML attaches it to that entry and no other. Appending
  at end of file, the way `_split_toml_change` appends a whole entry,
  would attach the table to whichever entry happens to be last, so
  the insertion point is the entry's, found by scanning entry headers
  and matching the entry's `name` line. A file whose entry block
  cannot be located textually (hand-shaped TOML the scanner cannot
  prove) is exit 4 printing the rendered table for the person to
  paste: the ids still never travel by hand-copying, only by paste of
  machine output, and the common machine-written files always scan.
- **Table present and identical** to what the provision renders
  (owner, number, the seven names, the map): no-op.
- **Table present and different:** refused, exit 4, printing the diff
  and "edit it by hand", the `_split_toml_change` rule for a same-name
  entry with different values, `--force` included. Setup created or
  verified the board, but it does not rewrite a table a person may
  have remapped on purpose. Re-provisioning after an option was
  replaced upstream therefore means: fix the one id by hand, or
  delete the table and re-run setup. (Decision O3 offers a narrower
  map-only rewrite; the recommendation is to refuse, as here.)

What the rendered table holds, all explicit so the file never again
depends on defaults silently changing under it:

```toml
[repo.board]
project_owner = "example"    # board owner, written by corral github setup
project_number = 3
agent_field = "Agent"
status_field = "Agent state"
status_column = "Status"
priority_column = "Priority"
area_column = "Area"
pool_column = "Pool"
blocked_by_column = "Blocked by"

[repo.board.priority_map]    # live option id to rank; names are comments only
"opt-1" = "urgent"  # Urgent
"opt-2" = "high"    # High
"opt-3" = "normal"  # Normal
"opt-4" = "low"     # Low
"opt-5" = "backlog" # Backlog
```

Rules: the number is written, not the node id, because people read
and share numbers and `_check_board` accepts exactly one of the two;
the node id the provision also returned is dropped, since the reader
re-resolves it live from owner plus number on every schema fetch. A
concept with no live field in `provision.fields` (an undeclared
`Area`, or a refused field) is omitted from the table, never written
as an empty string, which `_check_board` rightly rejects. A keep-only
`Area` or `Pool` is live by definition when the run kept it, so it
renders its name like any other concept; that is why a plain re-run
after a flagged run renders a table identical to the one on disk and
is a no-op (all-kept, exit 1), rather than tripping the
different-table refusal above. Option display names ride as
comments only, the `07` rule. The rendered text must round-trip
through `settings._check_board`: a golden test renders a provision,
loads it through `settings.load`, and gets the same `BoardConfig`
back (section 8, ticket 3).

Order of operations in setup: provision the board first, then write
the file. A crash between the two leaves a board nobody's config
names yet, and the re-run finds it by title (section 2, step 2) and
writes the table. The reverse order could strand a config naming a
board that was never created, which turns every board-mode pass into
an error until a person intervenes.

`tracker.variant` is never flipped by this write. A board table on a
labels entry is inert for the passes (`trackers.build_tracker`
ignores it in label mode), so pre-provisioning is safe. The doctor
does list it, as the warn-only row of section 5, computed from config
alone with no board fetch; it changes no pass behavior.
When the table lands on a labels entry, setup prints the flip as its
next step, in the `07-board-fields.md` section 6 order: drain the
outbox, backfill on the board, set `tracker.variant = "board"`,
resync, `corral doctor`. When it lands on an entry already in board
mode (the bootstrap state: variant flipped first, doctor failing on
the missing table until setup runs), the next step printed is
`corral doctor`.

## 4. Populating a new board, and the first sync

Decision: adopting existing issues is an explicit, item-add-only step
of setup (`corral github setup --populate`), separate from ensuring
the schema, because it is the one provisioning action whose effect is
list-length-visible on people's board. `new --create-remote` needs no
flag: a repository it just created has no issues to adopt.

### Adoption

The operation, again implemented once in the GitHub adapter behind
the provisioner shape (`populate(project_id, repo)` from section 2,
returning added, kept and refused lines):

1. List the board's current items' content ids, paged with the
   existing poll discipline (a malformed page is `TrackerError`,
   never a partial membership set; adopting on partial knowledge
   would double-add or misreport).
2. List the tracker repo's **open issues** through the REST adapter
   seam. Open issues only: closed issues are history people can add
   back by hand, and pull requests are never board items corral
   creates (the reader tolerates PR items people added; it does not
   put them there). External issues (an author who is not an operator)
   are included: board membership writes to the board, not to the
   issue, and external items stay read-only for every item write, as
   today (decision O8).
3. Add each open issue not already present, one add-item mutation per
   issue by its node id (UNVERIFIED: U9, the mutation name and what it
   answers for an already-present item). An already-present answer,
   if the pre-check raced a person adding the same issue, reads as
   kept, never as a duplicate corral then tries to clean up: adoption
   has no removal path at all.

Adoption sets **no field value on any item**: not `Status`, not
`Priority`, never the corral-owned texts. It adds items only; it
never moves or edits them. Backfilling people-owned fields from
labels stays the people action of `07-board-fields.md` section 6,
reviewable on the board. Partial failure reports added, kept and
refused per issue and exits 1 when any refusal occurred, 3 when the
tracker fell over mid-list; a re-run converges because presence is
re-read each time.

### What the first sync does

Nothing new is built here; this is the existing machinery meeting a
fresh board, stated so nobody has to re-derive it:

- While the variant is still `labels`, sync and the mirror behave
  exactly as before; the board exists but nothing reads it.
- After the flip, the first board poll starts from the head (no
  stored board cursor), reads every item the page cap allows per
  pass, and merges concepts per key. Unset fields settle nothing: an
  unset single-select reads as concept-absent and keeps the stored
  value, and empty corral-owned texts echo nothing, so label-derived
  claims and ranks in the store survive first contact with an
  unbackfilled board (`07-board-fields.md` sections 1 and 3).
- The resync that follows the flip walks the REST listing as today
  (`BoardBackedTracker.items` delegates to the REST adapter) and is
  still the only path that adopts claims. The doctor parity report
  then names every item whose label rank and board rank differ, which
  is the operator's backfill checklist.
- An open issue never adopted onto the board is invisible to the
  poll, and a targeted read of it fails closed
  (`BoardBackedTracker.item` raises "unknown board item"), so a
  freshness-gated claim on it skips instead of trusting a stale row.
  That is the correct failure, and it is why `--populate` completeness
  matters more than its speed; section 5 names the doctor row that
  catches the gap once the seam for it exists.

## 5. Doctor

Decision: one board row per relevant repo, still rendered through
`boarddoctor.check_sites` with its two injected seams (`fetch`,
`open_store`), still read-only, outward fixes printed and never run.
The doctor gains findings, not powers. Its exit contract is
unchanged: a failing board row is exit 1 through
`doctor.exit_code`, whatever the underlying cause, because doctor's
exit 3 is reserved for the unreachable manager bus today and this doc
does not reopen it. The same causes at their own verbs keep their own
codes (`TrackerError` is exit 3 through sync, mirror and setup;
`SettingsError` is exit 4 at config load).

| Finding | Row | Fix line (printed, never run) |
| --- | --- | --- |
| Board mode, no `[repo.board]` table | fail (exists) | `corral github setup` (creates the board and writes the table) |
| Table names a board that does not resolve (deleted, wrong owner or number, no read access) | fail, naming owner and number | `corral github setup` if the board should exist under that owner, else correct the table |
| Token lacks the board scope | fail, naming the missing scope | `gh auth refresh -s project` (UNVERIFIED scope string, U8; printed for the person, who owns their login) |
| Board rate-limited during the check | fail (exists, marked transient) | wait for the cooldown, re-run `corral doctor` |
| Mapped field missing on the live board | fail, naming repo, concept and field name | `corral github setup` (creates the missing field, alters nothing) |
| Mapped field present with the wrong kind | fail, naming concept, field and both kinds | rename or delete the field on the board, then `corral github setup` |
| A `priority_map` option id is gone from the board | fail (exists, `check_options`), naming field, id and rank | re-add the option on the board, or delete the board table and re-run `corral github setup` |
| A live priority option the map does not name | warn, naming the option's display name | add its id to `priority_map`, or leave it: those items keep their stored rank |
| Variant flip with queued writes | fail (exists, `flip_refusal`) | drain the outbox, then flip |
| Label and board priority disagree; unparseable `blocked_by` | fail (exists, `parity_lines`) | backfill on the board, then resync |
| Board provisioned but variant still `labels` | warn, naming the table | the `07` section 6 flip steps, or remove the table |
| Open issues missing from the board | fail, naming the count and the first few numbers | `corral github setup --populate` (needs the section 8 ticket 6 seam; not in the first doctor ticket) |

Five implementation notes, so the rows above are buildable rather
than aspirational:

- **Naming the missing or wrong-kind field** needs findings, not an
  exception. Today `BoardReader._parse_schema` raises `TrackerError`
  on the first missing or wrong-kind mapped field and the doctor
  folds every schema failure into "the board does not resolve". The
  doctor ticket gives `boarddoctor` a pure comparison over the live
  schema's raw field list (name, kind, options per field) against the
  `BoardConfig`, so field-level rows name names; the reader keeps its
  fail-closed raise for the passes. One comparison, in
  `boarddoctor`, consuming data the `fetch` seam returns; the seam's
  production wiring learns to return the raw list beside the resolved
  schema. The reader's own schema parse is not reimplemented.
- **The scope row** keys on classification of the fetch error's text.
  `github/gh.py` already classifies scope answers (`_classify`, today
  private); the ticket exposes that one classifier for the doctor's
  seam rather than growing a second matcher in `boarddoctor`. Until
  the spike (U8) confirms the real error text and scope string, the
  row ships behind no hard-coded assumption stronger than the
  classifier itself.
- **The warn rows need warn plumbing.** `boarddoctor.BoardRow.status`
  is only `"ok"` or `"fail"` today; `doctor.Check` already carries
  `"warn"` and warn-only rows do not fail the doctor. Ticket 6 widens
  `BoardRow.status` to `"ok" | "warn" | "fail"` and passes it through
  to the `Check` unchanged. Row precedence inside `_check_site` is
  fail over warn over ok: any fail finding makes the row fail, else
  any warn finding (today: a live priority option the map does not
  name) makes it warn, else ok.
- **The provisioned-but-labels warn needs a site-inclusion rule.**
  `check_sites` keeps only `variant == "board"` sites and
  `sites_from_settings` skips labels repos entirely today, so that
  row currently has no site to hang on. Ticket 6 includes a labels
  entry whose resolved `[repo.board]` table is not `None` as a site
  too, and `check_sites` answers it with the single warn row from
  config alone: for that row it calls neither `fetch` nor the store,
  so the warn costs no board fetch and cannot fail on an unreachable
  board. A labels entry with no table still gets no row, exactly as
  today.
- **Missing-from-board** cannot be computed from the store alone: no
  column records board membership. The honest check lists board item
  numbers through a second injected fetch and diffs them against the
  store's open rows for the repo. That is ticket 6, after the others,
  and until it lands the fail-closed targeted read (section 4) is the
  backstop.

`doctor --fix` does not exist today (`doctor.add_parser` registers
`--json` only; `01-onboarding.md` section 6 describes `--fix` as
design). Nothing in this doc adds it, and when it lands elsewhere the
rule is already fixed: outward fixes, board provisioning included,
are printed and never applied.

## 6. Other forges later

GitHub-only today, stated plainly: everything in
`github/board.py` and the new provisioner beside it; addressing a
board by owner plus a per-owner number; opaque node ids for projects,
fields and items; stable single-select option ids as the thing
`priority_map` keys on; linking a repository to a board it does not
live in; finding a board by title under an owner; GraphQL itself; and
the token scope of section 2. The `[repo.board]` table as built
(`project_owner` plus exactly one of `project_number`,
`project_id`) is this provider's dialect wearing concept clothing: it
is honest to say so in the design rather than pretend the shape is
already portable.

What is portable, and must stay so, is the layer above the adapter:
the seven concepts and their kinds, the five ranks, the ensure
contract (create missing, never alter or delete, report
created/kept/refused), and the exit codes. A later adapter, GitLab's
or another, implements `ensure_board` against those, and setup,
`new` and the doctor do not change. Facts about GitLab in this
section are UNVERIFIED (U10); they are named here only as the
assumptions the interface must not bake in:

- **No owner-plus-number address.** A board there is believed to live
  inside a project or a group, addressed by its path, with no
  per-owner number space (UNVERIFIED). `BoardProvision.number` is
  therefore `None`-able, `project_id` is an opaque string, and
  section 3's writer writes whichever of the two the adapter
  returned. The settings table will need additive keys for such an
  adapter; the strict pop-and-validate precedent in
  `settings._split_board` makes that an additive change, and an
  unknown dialect's keys must fail loudly rather than be read as this
  one's.
- **No custom text fields on board items.** Boards there are believed
  to be lists driven by labels, milestones or assignees, with no free
  text field an item can carry (UNVERIFIED). The concept kinds text
  and single-select are the least common denominator: an adapter may
  back a concept with labels, exactly as corral's own label adapter
  already backs `agent` and `status` with `agent:*` and `status:*`
  labels today. The interface must therefore never promise a field,
  only a concept carried somehow.
- **No separate linking step, and no add-item step.** When a board
  lives in the project and membership is derived by query
  (UNVERIFIED), link and populate are no-ops that report everything
  kept, not errors. Both operations are therefore spec entries the
  adapter may satisfy trivially, and neither the setup CLI nor the
  doctor may require proof they "did" something.
- **No stable option id.** When a column is label-backed, its
  "options" are names, renameable by people (UNVERIFIED). The
  priority rank mapping must therefore stay an opaque-string-keyed
  map owned by config, as `priority_map` already is, and corral code
  outside the adapter must never parse, compare or mint an option key.
  This is today's rule; provisioning must not weaken it by treating
  the keys it harvests as anything more than strings to write down.
- **No title uniqueness or title lookup.** Already true here (titles
  are a provisioning-time search key only, ambiguity refused), and
  the interface must keep runtime addressing free of titles so an
  adapter with no title search can still implement every read.

And the mirror rule for corral itself: nothing outside
`github/board.py` and its provisioner may learn a mutation name, a
URL shape or a `dataType` spelling from this work. The day a second
adapter is needed it is a new module and its tests (`00-design.md`
section 2), not a fork of the setup CLI.

## 7. Corral's own board

Corral adopts its own standard board once the provisioning above is
built, and not by an agent ticket: creating boards is on the
AGENTS.md approval-gate list, so the run below happens in a
maintainer's own session, on the maintainer's explicit go-ahead in
that session. Nothing in this design creates corral's board, and the
rollout in section 8 deliberately ends before this step.

When approved, the steps are the ordinary adoption path with no
special cases:

1. `corral github setup --board --populate` against corral's own
   tracker repo, from the root that drives corral. This works on
   corral's labels entry precisely because the provisioner factory
   needs no `BoardConfig` (section 2). The mapping lands
   in whichever `corral.toml` that is: this repository carries no
   `corral.toml` of its own today, so the file written is the driving
   ops root's, per section 3, and it is committed there like any
   other config change (through a PR, if that root takes PRs).
2. Drain the outbox through the mirror until empty.
3. Backfill the people-owned fields from the labels on the board
   itself, a people pass over the open issues, using the doctor
   parity report as the checklist.
4. Flip `tracker.variant` to `board`, resync, `corral doctor`.
5. Leave the labels in place. Rollback is the flip undone plus a
   resync (`07-board-fields.md` section 6); the board itself is never
   deleted by corral, and a board abandoned by a roll back stays as a
   people's artifact until a person removes it by hand.

Until then corral stays in label mode, and the standard's first real
board is whichever project the maintainer provisions first.

## 8. Rollout: implementation tickets

Each ticket is sized for one agent, in order, with acceptance
criteria. Every behavior is tested through injected seams only: the
fake `run(args)` at the `gh` seam with canned GraphQL and REST
payloads, a fake pacer, fixture TOML in temporary directories, a
temporary store. No test runs a real `gh`, touches the network, or
reaches a real board; `tests/suiteguard.py` keeps enforcing that, and
the board guard precedent applies (a test that reaches the real pacer
state file or a real subprocess fails the suite). The only real-board
contact in the whole rollout is ticket 0, which is not an agent
ticket.

0. **Spike: verify the provider facts (maintainer-run, approval
   first).** On a throwaway owner the maintainer names, with the
   explicit go-ahead creating boards requires: create one Project,
   one text field, one single-select field, link one repository, add
   one issue, and record the answer to every GitHub UNVERIFIED item,
   U1 to U9 and U11 in section 9 (mutation names and inputs, built-in
   fields, visibility defaults, option-append id survival, the token
   scope and its error text, the already-present answers, title
   uniqueness, the owner lookup). U10 is not the spike's to settle:
   GitLab facts stay UNVERIFIED until a GitLab adapter is itself
   designed. Acceptance:
   each of U1 to U9 and U11 is marked verified or false with the command that
   showed it, in an amendment to this doc; where a fact is false, the
   affected section above is amended before ticket 2 starts (in
   particular, if U7 fails, section 2 step 6 ships as its refused-line
   variant). No corral code changes.
1. **Types, the standard spec, and the pure planner.** `tracker.py`
   gains `FieldSpec` (with `keep_only`), `BoardSpec` (with the
   existing `priority_map`), `BoardProvision` (with live `fields`),
   the `BoardProvisioner` protocol and `STANDARD_BOARD`;
   `effective_spec` resolves config-names-else-standard in one place,
   including config-mapped `Area` and `Pool` as keep-only; a pure
   planner maps (spec, live schema as plain data) to the ordered
   create/keep/refuse action list the adapter will execute.
   Acceptance: planner tests cover empty board, exact board, wrong
   kind, missing option, extra team fields and options (all kept,
   none planned for change), and the five-rank map built from live
   ids; a renamed priority option (`High` renamed, id unchanged, the
   spec's map naming that id) plans no append and keeps its rank,
   while a rank with neither a live mapped id nor its standard name
   plans exactly one append; a kept `Status` field plans no option
   append whatever its option names are; duplicate field names and
   duplicate option names on the live board each plan a refusal with
   a fix line, never a pick; a keep-only `Area` present on the board
   is kept with no option requirement, and absent is refused naming
   `--area`; label-mode fake trackers compile untouched (no new
   `Tracker` method).
2. **The GitHub provisioner and its factory.** `GithubBoardProvisioner`
   in `github/board.py`, constructed with only `run` and the pacer
   (no `BoardConfig`), executing the planner's actions: the new owner
   query, find-or-create, visibility read-back and correction, link,
   field and option ensures, final read-back report; plus
   `trackers.production_board_provisioner` beside
   `production_trackers` as the one production wiring. Acceptance
   against canned payloads: created/kept/refused reported separately;
   a re-run over an exactly-standard board sends no mutation; an
   existing field is never the target of an update or delete
   mutation; two same-title projects and an unresolvable owner raise
   `ValueError` naming the conflict before any mutation; duplicate
   field or option names in a canned schema return refusals, not
   `TrackerError`; a make-private update that errors stops before any
   link or field mutation and raises `TrackerError`, and a board that
   still reads public after the update stops at the same place with a
   refused line; rate-limit text raises `TrackerRateLimited`;
   malformed answers are `TrackerError`, never partial provisions.
3. **The mapping writer.** `init.board_table_change(toml_target,
   entry_name, spec, provision)` plus the table renderer, wired so
   the write lands after a successful provision. Acceptance: golden
   files for inline and split layouts (the clone untouched in split),
   insertion attaches to the named entry when a later `[[repo]]`
   entry exists, identical table is a no-op, a different table
   refuses with the diff even under `--force`, an unscannable entry
   block prints the rendered table and exits 4, and the rendered
   table round-trips through `settings.load` to the same
   `BoardConfig`; the names in the golden tables come from
   `provision.fields`, and a provision that kept a keep-only `Area`
   renders `area_column`, so re-rendering against the file a flagged
   run wrote is identical (the no-op behind the plain re-run's
   exit 1, exercised end to end in ticket 4).
4. **Extend `corral github setup`.** The verb and its label half are
   built (`githubcli.setup_root`, `ensure_labels` on the `Tracker`
   protocol); this ticket adds no verb and rebuilds no label code. It
   extends `setup_root` with an injected `provisioner_factory` seam
   (production: `trackers.production_board_provisioner`), the flags
   `--board`, `--populate`, `--plan`, `--area`, `--pool`,
   `--board-owner` and `--board-title`, the per-entry board ensure
   (board-mode entries, or any entry with `--board`) after the label
   ensure, the section 3 mapping write, the cross-entry aggregation
   of section 2 (4 over 3 over 1 over 0, every entry attempted), and
   the next-step lines (flip steps for labels entries, doctor
   otherwise). Acceptance through a fake tracker and fake
   provisioner: a labels entry with `--board` provisions and writes
   the inert table (the factory needed no `BoardConfig`); a
   board-variant entry with no `[repo.board]` table provisions and
   writes the table that makes it whole; a plain re-run after a run
   with `--area` and `--pool` is a no-op, exit 1, with no
   different-table refusal; mixed multi-entry runs aggregate as
   section 2 states (entry A creates and its mapping file stands,
   entry B raises `TrackerError`, entry C hits a different table: all
   three attempted, final exit 4; A and B without C exit 3; created
   plus refused exits 1; all-kept exits 1); every row of the section 2
   per-board exit table; `--plan` creates no label (the fake sees the
   report-only label read and no create), sends no board mutation and
   writes no file byte; the `--area`, `--pool`, `--board-owner` and
   `--board-title` flags reach `effective_spec` and nowhere else; a
   labels entry without `--board` behaves byte-identically to the
   built verb today; the README Commands row for `github setup` is
   updated for the board half and the new flags in this ticket.
5. **Population.** The adapter's populate operation and its setup
   wiring (may land with ticket 4 if small): open issues only,
   presence pre-read, add-missing only, no field value ever written.
   Acceptance: an already-present issue is kept without a mutation, a
   closed issue and a PR are never added, an external author's open
   issue is added (or not, per O8), partial failure reports per issue
   and a re-run adds only the remainder.
6. **Doctor rows.** The `boarddoctor` raw-schema comparison (missing
   field, wrong kind, unmapped option warn), the scope row over the
   exposed classifier, the provisioned-but-labels warn, fix lines
   pointing at `corral github setup`; the warn plumbing
   (`BoardRow.status` gains `"warn"`, passed through to `doctor.Check`,
   fail over warn over ok per row) and the site-inclusion rule (a
   labels entry with a `[repo.board]` table becomes a site answered
   warn-only from config, with no `fetch` and no store call for that
   row; a labels entry without a table still gets no row); then, in
   the same ticket or a follow-up if it grows, the missing-from-board
   row over its second injected fetch. Acceptance: each row of the
   section 5 table names repo and field against fake seams; the
   labels-with-table warn is produced with the fake `fetch` asserting
   it was never called; doctor exit codes unchanged (fail rows exit 1,
   warn-only exits 0); no row runs anything.
7. **`new --create-remote` wiring.** `corral new` is built; this is a
   small extension of it, not a blocked ticket. In the
   `--create-remote` path, after the built `_bootstrap_labels`, invoke
   the same board ensure (the `production_board_provisioner` factory
   and `effective_spec`, never a second implementation) for the new
   entry and write its mapping through ticket 3's writer. Outcomes
   map onto `new`'s existing codes by the `_bootstrap_labels`
   precedent: created counts as changed; refused prints
   `next: corral github setup` and the run ends 1; `TrackerError`
   prints the same next step and the run ends 3; a spec `ValueError`
   is a usage error, 4. Without `--create-remote`, print
   `corral github setup` exactly as today. Acceptance: fake-forge,
   fake-tracker and fake-provisioner tests show the one operation
   invoked once, no second provisioning path, the four outcome
   mappings above, and the printed-not-run default unchanged.
8. **Corral's own adoption.** Not an agent ticket. Held for the
   maintainer's go-ahead in their own session, run per section 7.

## 9. Unverified facts and open decisions

UNVERIFIED means: believed true of GitHub Projects v2 or GitLab, not
run by the author of this doc, and not to be treated as fact by any
implementation ticket until ticket 0 confirms it. Decisions O1 to O8
are the maintainer's; each block has one line under 60 characters per
option and one marked recommended.

- U1. A new Project ships a built-in `Status` single-select field
  (`Todo`, `In Progress`, `Done`), and built-ins cannot be deleted.
- U2. A single-select field cannot be created with zero options.
- U3. `createProjectV2` takes an owner node id and title; titles are
  not unique per owner.
- U4. Create does not take visibility; `updateProjectV2` can set a
  project private after creation.
- U5. A link mutation exists (`linkProjectV2ToRepository` or similar)
  and re-linking an already-linked repo is harmless.
- U6. `createProjectV2Field` takes `dataType` (`TEXT`,
  `SINGLE_SELECT`) and options at creation for single-select.
- U7. Appending options replaces the option list but preserves the
  ids of the options carried over unchanged.
- U8. The needed token scope is `project` (write) / `read:project`
  (read); its refusal text classifies as `scope`; the fix is
  `gh auth refresh -s project`.
- U9. `addProjectV2ItemById` adds an issue by node id and answers
  identifiably when the item is already present.
- U10. Every GitLab statement in section 6.
- U11. An owner login can be resolved to exactly one of user or
  organization, with its node id, by asking for both; and an unknown
  login answers identifiably as neither (not as a generic failure).

O1. Priority option names on the standard board:
Options:
1. The five rank names, title-cased (recommended)
2. P0 to P4, mapped to the five ranks in order
3. P0 to P3 only, no backlog option on the board

O2. How an entry reaches its first provision:
Options:
1. variant=board, no table; setup writes it (recommended)
2. Relax the table: owner-only means unprovisioned
3. Labels entry plus setup --board writes everything

O3. Setup meets an existing, different board table:
Options:
1. Refuse with the diff; edit by hand (recommended)
2. Rewrite priority_map only, keep names and address
3. Rewrite the whole table from the live board

O4. Adopting existing open issues:
Options:
1. Explicit setup --populate flag (recommended)
2. Automatic whenever setup creates the Project
3. Never; people add issues to the board by hand

O5. A project created by `new --create-remote`:
Options:
1. Labels default; board made, table inert (recommended)
2. Board variant from day one, table live at once
3. No board until the operator runs setup later

O6. `Area` and `Pool` when a project declares no options:
Options:
1. Skip the fields until options are declared (recommended)
2. Create each with one seed option named General
3. Require at least one option before setup runs

O7. Title for a board corral creates:
Options:
1. "<repo> board" from the tracker repo name (recommended)
2. The entry name from corral.toml, unchanged
3. No default; setup requires a --board-title flag

O8. External authors' open issues during population:
Options:
1. Add them; board membership writes no issue (recommended)
2. Operators' issues only; external stays off-board



