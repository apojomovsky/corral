# 11: the standard project board, its provisioning and checks

Status: design, awaiting maintainer approval. No code ships with this doc.
Implementation waits for approval of the open decisions in section 9.

Every project that uses korapy should have a hosted project board, and
korapy itself should have one too. That is the standard this doc designs:
what the board holds, how korapy provisions it idempotently, how the
provisioned identifiers reach `korapy.toml` without a person copying ids
by hand, how existing issues are adopted into a new board, and what the
doctor checks. GitHub Projects v2 is the first and only target today.
Another forge later is a new adapter behind the same interfaces
(`00-design.md` section 2, `07-board-fields.md` section 0); section 6
states what the interface must not assume so that adapter fits.

`07-board-fields.md` designed and built the board-field tracker variant:
reading and writing a board a person made by hand, mapped by hand in
`korapy.toml`. This doc removes the "by hand". It changes no concept, no
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
  an established shape in korapy, not a new idea.
- `korapy github setup` is a built verb (`githubcli.cmd_setup` and
  `githubcli.setup_root`). It runs `ensure_labels` on every named
  `[[repo]]` entry, continuing past a `TrackerError`, and aggregates
  the exit code across entries: any failed repo is 3, else any refused
  label is 1, else 0 only when something was created, else 1. Today
  it creates labels only; no board half exists. `init` prints it as
  its next step and never runs it.
- `korapy new` is built, including `--create-remote` (`new.py`): after
  creating the remote it bootstraps labels through the same
  `ensure_labels` (`new._bootstrap_labels`); without the flag it
  prints `next: korapy github setup` and runs nothing outward. This
  doc designs only the board operation those built callers invoke; it
  does not re-design `new` or the label half of `setup`.
- Nothing creates a board. No `ensure_board` exists anywhere, and
  board mode assumes a person made the Project and mapped its fields
  by hand. That gap, and only that gap, is what this doc designs.

Two consequences. First, board provisioning extends the existing
`github setup` verb and joins the label bootstrap in that one
operation, so there is still exactly one code path that creates
outward tracker state (`06-korapy-new.md` section 4: "one code path
owns this"). No new verb is added, and the label half is not rebuilt.
Second, board mode today
assumes a person made the Project, created the fields, read the option
ids out of the provider, and typed them into `korapy.toml`. Every step
of that is a place to copy the wrong id. Provisioning writes the
mapping from what the provider returned, so the ids in the file are
the ids on the board by construction.

Non-goals: views and board layout (grouping, ordering, saved views are
people's presentation, never korapy's contract; the fields are the
standard, the views are not), provider workflows and automation rules,
deleting or renaming anything on a board (no delete operation is
designed anywhere in this doc), backfilling people-owned field values
(`07-board-fields.md` section 6 keeps that a people action), and a
second provider (section 6 only keeps the door open).

## 1. The standard board

Decision: korapy ships one standard board specification, as data in one
place, naming the seven concepts `07-board-fields.md` section 1 fixed
and nothing else. korapy code keeps naming concept keys only; the
standard names the default board spelling of each concept. A project's
`korapy.toml` remaps any name, and the remap wins everywhere (section 3
writes the effective names into the file, so after provisioning the
file never depends on the defaults again).

| Concept | BoardConfig key | Standard name | Kind | Owner |
| --- | --- | --- | --- | --- |
| `agent` | `agent_field` | `Agent` | text | korapy (outbox) |
| `status` | `status_field` | `Agent state` | text | korapy (outbox) |
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

- `Status`: a field korapy creates gets at least `Todo`,
  `In Progress` and `Done`. Those names are creation defaults only:
  korapy assigns them no meaning and never tops up a kept field's
  options by name (section 2, step 6). korapy carries the display
  name verbatim into `extra["board_status"]` and gives it no meaning; what a status
  allows is project eligibility policy, as today. A new GitHub Project
  ships a built-in `Status` single-select field with exactly these
  options, each with a color and a description (VERIFIED: section 9,
  item U1), beside built-in fields for title, assignees, labels,
  linked pull requests, milestone, repository, reviewers, parent
  issue, sub-issues progress, and creation, update and close times.
  Provisioning keeps the built-in and creates nothing for this
  concept, and never deletes a built-in field: deleting `Status`
  is refused, since only custom fields can be deleted.
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
  AGENTS.md: setting it is the maintainers' alone on korapy's own
  board, and the equivalent people's call elsewhere; korapy only
  provisions the option.
- `Area` and `Pool`: the standard names the fields and no options.
  Areas and pools are the project's own vocabulary, and no generic
  list is honest. A single-select field cannot be created with zero
  options (VERIFIED: section 9, item U2), so provisioning creates
  these two fields
  only when at least one option is declared for them. The only
  declarer in version one is the operator running setup, through
  repeatable `--area NAME` and `--pool NAME` flags on
  `korapy github setup` (decision O6): there is deliberately no
  `korapy.toml` key for the lists, because once the field exists its
  options are people's board state, and a config copy would be a
  second resolver for the same fact. Undeclared and unmapped, the
  concept stays out of the spec, which the reader already treats as
  absent; a later setup run with the flags creates the field then.
  Once a project's `korapy.toml` maps the concept (`area_column` or
  `pool_column` is set, whether by an earlier flagged setup run or by
  hand), it enters every later spec as **keep-only**: the field must
  exist with the right kind and is then kept exactly as found, and no
  option is required, checked or appended. Options are required only
  at creation. A keep-only field that is absent from the board cannot
  be created without declared options, so it is a refused line naming
  the `--area` or `--pool` flag as the fix. This is what makes a
  plain re-run after a flagged run a no-op: the spec still names the
  concept, the field is kept, and section 3 renders the same table.

### What korapy requires, what a team may add

Required for board mode to be healthy (doctor section 5 enforces):

- The Project resolves by owner plus number, or by node id.
- `Agent` and `Agent state` exist as text fields. Writes need them: an
  unmapped korapy-owned concept raises `UnknownOption` at mirror time
  and the write is dropped, so a standard board maps both.
- Every concept the project maps in `korapy.toml` exists on the board
  with the kind above, and every option id in `priority_map` exists on
  the live `Priority` field. This is the existing doctor contract.

The standard provisions all seven fields above (subject to the
`Area`/`Pool` rule). A team may then, without breaking the standard:

- add fields of any kind, for any purpose korapy does not know about;
- add options to people-owned columns. An option on `Priority` that
  `priority_map` does not name reads as concept-absent and keeps the
  stored value, exactly as an unmapped option does today;
- rename option display names (ids are stable, names are comments);
- add views, groupings and provider workflows.

A team breaks the standard, and the doctor says so, by deleting or
replacing a mapped field or a mapped option id, changing a mapped
field's kind, or using the two korapy-owned text fields for people
meaning (a person's edit there is handled by the settlement rules, but
the meaning is theirs to lose, per `07-board-fields.md` section 1).

Board title: the standard title for a board korapy creates is
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
the label bootstrap in `06-korapy-new.md` section 4.

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
    number: int | None = None       # config address: per-owner number, if the table has one
    project_id: str | None = None   # config address: node id, if the table has one
    priority_map: dict[str, str] | None = None  # the config's existing id -> rank map, if any

class BoardProvision(NamedTuple):
    owner: str
    number: int | None      # the provider's per-owner number, when it has one
    project_id: str         # the provider's opaque node id
    fields: tuple[FieldSpec, ...]  # concept -> live name, after the final read-back
    created: tuple[str, ...]  # "project", "field:Agent", "option:Priority/High", "link:example/repo"
    kept: tuple[str, ...]
    refused: tuple[str, ...]  # one fix line per refusal
    priority_map: dict[str, str]  # live option id -> rank, for korapy.toml

class BoardProvisioner(Protocol):
    def ensure_board(self, spec: BoardSpec) -> BoardProvision: ...
    def populate(self, project_id: str, repo: str) -> BoardPopulate: ...
```

The spec's address is exactly one of `number` and `project_id`, or
neither: one when the config already addresses a board (the same
exactly-one rule `settings._check_board` enforces on the table), and
neither in the bootstrap states where no table exists yet, when title
search or creation (steps 2 and 3) is the only way to find the board.
A spec carrying both, or a non-positive number, is refused by
`effective_spec` before any lookup; the spec is plain data built in
that one place, so no caller constructs an ambiguous address.
`priority_map` has no mutable default: it is `None` when the config
has no map, and `effective_spec` normalizes it to a fresh dict (a copy
of the config's map, never the config's own object) before the spec
reaches the planner, so one entry's spec can never share or mutate
another entry's map in a multi-entry run.

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
Its inputs are the entry's variant, the resolved `BoardConfig` (or
`None` in the bootstrap state), the entry's `tracker.repo`, and the
operator's setup flags, carried into `setup_root` as one
`trackers.SetupEntry` per entry (the wiring restructure below).
Derivation, in one place so no caller re-derives it: the owner is the
`--board-owner` flag when given, else the config's `project_owner`
when a table exists, else the tracker repo's owner; the address is
carried from the config unchanged when a table exists
(`BoardConfig.project_number` to `number`, `project_id` to
`project_id`, exactly one of them as `_check_board` already validated)
and is neither when there is no table, with no flag that overrides it
(a flag naming a different board than the config would be a second
address for the same entry); the title is
`<repo> board` from the tracker repo's name unless `--board-title`
overrides; each field name is the config's name for the concept when
mapped, else the standard name; the config's existing `priority_map`
rides into the spec (copied, per above) so the planner can resolve
renamed options by id (step 6); `Area` and `Pool` enter the spec with at least one declared
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
`setup_root` as an injected `provisioner_factory` seam, as part of the
setup wiring restructure below; tests inject a fake. Mutations go through
`gh.graphql`, so they are charged at mutation cost and rate-limit
answers raise `TrackerRateLimited` through the shared classifier,
exactly like board reads and writes. `gh api graphql` exits non-zero
on any `errors` entry even beside partial `data` (VERIFIED: section 9,
item U11), so the seam returns the response body for GraphQL calls
and the adapter classifies from the body, never from the exit code.
Nothing outside the adapter
knows a mutation name or a field-creation shape.

Whether an adapter supports provisioning is what the factory says:
it returns an object with a callable `ensure_board`, or it does not
build one at all. Asking setup to provision a board when the factory
yields nothing is a usage error counted as exit 4 in the aggregation
below ("this tracker does not provision boards"), never a silent
skip and never a missing-attribute crash on a repo tracker.

### Production wiring: the `cmd_setup` restructure

The built wiring cannot reach the bootstrap state, and ticket 4
restructures it; a fake-only `setup_root` test would pass while the
real verb still failed, so the restructure is designed here, not left
to the ticket. Today `cmd_setup` calls
`trackers.production_trackers(root, names, git=...)` before
`setup_root` runs, and that one call builds every entry's tracker up
front through `build_tracker`, which raises `ValueError` for board
mode without a `[repo.board]` table. One bootstrap entry therefore
fails the whole run with exit 4 before any entry is attempted, and
the `RepoTracker` it returns carries no `BoardConfig`, so
`setup_root` could not learn an entry's board config (or, for a
bootstrap entry, that it has none) even if construction succeeded.
`production_trackers` and `build_tracker` stay exactly as they are
for the fleet passes, where refusing board mode without a table is
correct; setup gets its own per-entry wiring beside them:

- `trackers.SetupEntry` (a `NamedTuple`: `name`, `tracker_repo`,
  `operators`, `variant`, `board`, `max_pages`), where `board` is
  the entry's resolved `BoardConfig` or `None` and `max_pages` is
  its resolved `board.poll_pages`. It is `RepoTracker` minus the
  built tracker, plus the board config: the plain-data half of an
  entry, with no construction in it.
- `trackers.setup_entry(root, name, *, git) -> SetupEntry` resolves
  one entry the way `production_trackers` resolves each of its
  entries (the shared `syncli.repo_operators` for the tracker repo
  and operators, then `settings.load(root=root, repo=name)` for
  `tracker.variant`, `board.poll_pages` and `resolved.board`) and
  builds nothing. A resolution failure raises, and `setup_root`
  counts it as that entry's 4 and continues; it never aborts the
  entries after it.
- `trackers.setup_tracker(entry, *, run=None, pacer=None)` builds
  the one tracker the label half needs, lazily, per entry, inside
  the loop, through `build_tracker` so the runner wiring stays in
  that one factory: a labels entry and a board entry with a table
  go through it exactly as today (variant, `entry.board`,
  `entry.max_pages`); a board entry with no table goes through it
  as a labels entry for the label half (labels are repository
  state and need no board), while its board half goes to the
  provisioner, which needs no `BoardConfig`. An unknown variant
  raises `ValueError`, counted as that entry's 4.
- `trackers.production_board_provisioner(*, run=None, pacer=None)`
  as above, built lazily per entry only when that entry's board
  ensure runs, from only the runner and the pacer.

`cmd_setup` then resolves only the root and the entry names (the
existing pre-loop 4s: no root, unparseable settings, unknown entry
name) and passes three injected seams into `setup_root`:
`entry_factory` (production: `setup_entry` bound to the root and
the git seam), `tracker_factory` (production: `setup_tracker`, now
taking a `SetupEntry` rather than a tracker-repo string), and
`provisioner_factory` (production: `production_board_provisioner`).
Inside the loop, per entry in order, `setup_root` resolves the
`SetupEntry`, builds that entry's tracker, runs the label half,
and, when the board half applies, builds the provisioner and calls
`effective_spec` with the entry's variant, its `board` (or `None`)
and its tracker repo. The variant and board config that
`effective_spec` needs therefore travel as data on the `SetupEntry`;
no caller re-loads settings to recover them, and no tracker is
constructed for an entry the loop has not reached. Tests inject all
three seams; ticket 4's acceptance includes a `cmd_setup`-level
test for a `variant = "board"` entry with no table: the production
`entry_factory`, `tracker_factory` and `provisioner_factory` run
for real against a temporary root, with only the `run` seam they
default to (the `gh` runners) and the git seam faked, proving the
real verb provisions it instead of exiting 4 before the loop.
`cmd_setup` gains no test-only parameter for this; the factories'
existing `run=None` defaults are the seam, as in `build_tracker`.

### The ensure algorithm

Each step checks its own precondition against the live board, so a
partial run converges on re-run and the board itself is the state; no
ledger is kept on disk (the `06-korapy-new.md` section 5 rule):

1. **Resolve the owner.** The owner login comes from the spec. This
   needs a new owner query, not an existing one: `BoardReader._fetch_project`
   resolves a *project* by number across user and organization (it
   asks both for `projectV2(number)` and accepts exactly one); it
   resolves neither an owner's type nor its node id. Provisioning
   asks for the login as both a `user` and an `organization` in one
   query, accepts exactly one set half, and reads that owner's node
   id for the create step (VERIFIED: section 9, item U11). An
   unknown login answers with both halves null and one NOT_FOUND
   error per half, which raises `ValueError` (exit 4 in the
   aggregation) before any mutation. The runner exits non-zero
   whenever the body carries any `errors` entry, even when one half
   succeeded, so the adapter reads the JSON body and decides from
   `data`, never from the exit code. The standard is
   one board per tracker repo, but the operation takes the repo to
   link as data and does not forbid other repos linking the same
   board later.
2. **Find the Project.** By the spec's address first, when it has
   one: by node id (`project_id`), else by owner plus `number`.
   A board found by address is the board, whatever its current title
   is: titles are never consulted on an addressed run, so a board a
   person renamed after provisioning is found again by its address
   and no duplicate is created. An addressed board that does not
   resolve (deleted, or a number or id that names nothing under that
   owner) never falls through to title search or creation: a typo'd
   address must not silently mint a second board and then collide
   with the existing table in section 3. It raises `ValueError`
   before any mutation, counted as exit 4 for the entry in the
   aggregation below (a configuration error: the config names a
   board that is not there), with the fix line "the board in the
   config no longer exists: remove the table and re-run". Removing
   the table returns the entry to the bootstrap state, where title
   search and creation are again allowed; correcting the address by
   hand is the other fix, and the line says so by naming the table.
   Only a spec with no address (the bootstrap states) searches by
   exact title under the owner (paged list, exact string match,
   nodes validated like every other board answer), and only during
   provisioning. Zero title matches means create. Two or more title
   matches raises `ValueError` (exit 4 at the CLI) naming every
   matching number, before any mutation: korapy never picks one (the
   same ambiguity in `_fetch_single` already fails closed the same
   way).
3. **Create the Project when missing.** One create mutation with the
   owner id and title: `createProjectV2(ownerId, title)`
   (VERIFIED: section 9, item U3). Titles are not unique per owner,
   so the create is never guarded by a title check (the racing
   duplicate at the tail of this section is the accepted risk).
   Where the API returns the new id form for the owner, the adapter
   prefers it over the legacy node id, which answers with a
   DEPRECATION warning naming `next_global_id`. Visibility: private
   by default, the `06-korapy-new.md` section 3 rule for remotes
   applied to boards. Create takes no visibility input (VERIFIED:
   section 9, item U4); the design therefore reads the new Project
   back (a new user-owned Project reports `public: false`) and, when
   it reports public and the spec asks for private, sends one update
   to make it private before anything else is put on it. The update
   input carries `public`. Create also accepts `repositoryId`, which
   links the repository in the same step: when the spec names a repo
   the adapter passes it, and step 4 then reads the link as kept.
   Making a just-created board private is part of
   creating it as specced; it is the only update an ensure ever sends.
   The correction gates everything after it: when the update errors,
   provisioning stops before the link and field steps and the error
   propagates as `TrackerError` (counted as exit 3 for the entry);
   when the update reports success but the board still reads public
   on the re-read, provisioning likewise stops before link and
   fields, with a refused line naming the manual fix (counted as
   exit 1). Korapy never puts fields, links or items on a board it
   knows is not private as specced; a re-run after the fix converges.
   An existing Project's visibility is never flipped. When an existing
   board the config addresses directly (by number or id) is public
   while the spec asks for private, the report carries a refused line
   with the manual fix and provisioning continues: the config naming
   that board is the people's decision to use it. When korapy found
   the board by title search and it is public while the spec wants
   private, provisioning stops before any field step, exit 1 with the
   fix line: adopting a public board a person made, on a title match
   alone, is not a default korapy takes.
4. **Link the repository.** When the spec names a repo, link it to the
   Project with `linkProjectV2ToRepository(projectId, repositoryId)`
   (VERIFIED: section 9, item U5). The board reader does not need
   the link (it queries by owner plus number); people do, since the
   link is what surfaces the board from the repository. Already
   linked reads as kept: linking the same repository twice succeeds
   both times. A repo
   the account cannot administer refuses this step only (R5 in
   `01-onboarding.md`: someone else's repository), with a fix line,
   and the rest of the provision stands.
5. **Ensure fields.** Read the live schema once (the `_SCHEMA_BY_NUMBER`
   and `_SCHEMA_BY_ID` shapes, extended with each option's id, color
   and description, which step 6's whole-list resend needs; the spike
   showed no further datum missing), as a raw field list for
   the planner, not through
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
     single-select: `createProjectV2Field` with `dataType` TEXT or
     SINGLE_SELECT, the options riding along at creation with name,
     color and description, answered back with ids (VERIFIED:
     section 9, item U6).
   Built-in fields the spec does not name (titles, assignees and the
   like) are ignored, like any other team field. A spec field whose
   name matches a built-in of the right kind (the `Status` case,
   VERIFIED: U1) is kept like any existing field. Korapy never
   deletes a field: deleting a built-in is refused by the provider,
   and anything beyond creating and keeping is outside this design
   (section 0 designs no delete operation).
6. **Ensure options, rename-is-free.** Appending by missing *name*
   would undo a person's rename: rename `High` to `P1` (the id is
   stable, the map still valid) and a name-based top-up would append a
   second, duplicate-meaning `High` to a people's field. So on a kept
   `Priority` field the planner resolves ranks through the spec's
   existing `priority_map` ids first: a rank whose mapped id is live
   on the field is present, whatever that option is now called; then
   a rank whose standard name is live is present. Only a rank with
   neither a mapped live id nor the standard name is appended, under
   its standard name. The equivalent rule for `Status`: korapy keeps
   no id map for it and gives its names no meaning, so there is no
   evidence by which a missing standard name is missing rather than
   renamed; korapy therefore never appends options to a kept `Status`
   field at all. Its standard names are creation defaults (section 1),
   and a kept field's options are wholly people's. Keep-only fields
   are never topped up either. The provider's option update
   (`updateProjectV2Field` with `singleSelectOptions`) replaces the
   whole option list (VERIFIED with a refinement: section 9, item
   U7), so the one append update korapy may send (on `Priority`
   only) is the existing list, in its existing order, with the
   missing ranks' names appended. Because the whole list is resent,
   the planner's live-schema data carries each existing option's id,
   color and description beside its name, and the append resends
   every carried-over option with its id and its color and
   description unchanged; only the appended options omit the id and
   get provider defaults. Resending without ids would give every
   option a new id, and the update input requires `color` and
   `description` on every option, so when any live option cannot be
   resent in full, the planner refuses the append instead and prints
   a fix line ("add the `<rank>` option to the field on the board by
   hand, then re-run"), because resetting people's option colors is
   an alteration step 5 forbids, and rewriting ids would silently
   invalidate every `priority_map` in the wild. Options are never
   removed and never renamed by korapy.
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
| Bad spec or config (unresolvable owner, ambiguous title, the config-addressed board no longer resolves, a different board table already in the file, unknown concept in an extended spec, no provisioner for the adapter) | 4 |
| Admission slot | 75 is never used; setup starts no agents |

Across entries, setup adopts the aggregation the built verb already
uses in `githubcli.setup_root`, extended with where board 4s rank.
Every named entry is attempted, in entry order; a per-entry failure
of any kind is recorded and the loop continues with the next entry,
never aborting the rest (the built loop already continues past a
`TrackerError`; ticket 4 gives board `ValueError`s (the
config-addressed board that no longer resolves included),
per-entry lazy resolution and construction failures, different-table
refusals and provisioner-missing errors the same continue treatment,
each counted under the 4 precedence below).
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
both look before either creates (titles are not unique, VERIFIED:
section 9, item U3): the
create is not guarded by a title check, and no post-create re-list
uncreates the second board. Duplicate titles are a documented risk
of the operator command, flagged for a person to resolve, and the
flagging does not depend on a later setup run happening to
title-search again (once either run writes a table, later runs
resolve by address and never title-search): the doctor carries a
duplicate-title warn row that lists the boards under the configured
owner sharing the configured board's title and names every number
(section 5, ticket 6), so the orphan is visible on the next doctor
run even though every addressed check on the kept board passes.
An unaddressed setup run also refuses at step 2 naming both
numbers, and a person deletes one board by hand; korapy designs no
delete for it. Setup is an operator command, not a fleet pass; no
lock is designed for it, and the failure is closed and visible.

### The token scope

PARTIAL (section 9, item U8): the spike ran every mutation in this
section (create, link, field, option, item) with a token carrying
`gist`, `project`, `read:org`, `repo` and `workflow`, adding no other
scope, so the `project` scope suffices for board writes. What the
spike did not reproduce is the refusal: with no token lacking the
scope available, no refusal text was observed, so the error
classification stays UNVERIFIED and is tested with canned payloads
only until observed. Board reads and writes today ride on whatever
token `gh` already holds, so a host can run label mode for years and
only meet this at the first board call. The design states the fix
line, never runs it: `gh auth refresh -s project`. The doctor row in
section 5 keys on the shared classifier, never on a hard-coded
refusal string, until the refusal text is observed.

### Who invokes it

| Caller | Behavior |
| --- | --- |
| `korapy github setup` | Runs it (labels first, then the board, then the mapping write). The verb exists (`githubcli`); the board half extends it (ticket 4). The verb is the operator's approval, per `06-korapy-new.md` section 3. |
| `korapy new --create-remote` | Runs it after creating the repository and the labels, through the same operation and provisioner factory, never a second implementation. `new` exists; ticket 7 adds the board call after its built `_bootstrap_labels`. |
| `korapy new` without the flag | Prints `korapy github setup`, runs nothing outward. |
| `korapy init` | Prints `korapy github setup`, exactly as today; never runs it. |
| `korapy doctor` | Prints it as the fix line, never runs it (outward fixes are printed, `01-onboarding.md` section 6). |
| `sync`, `mirror`, `drive`, `launch` | Never. A missing field at pass time stays an error line and a doctor finding (`07-board-fields.md` section 4); fleet passes create nothing. |

Which repos setup boards: for each `[[repo]]` entry setup is asked to
cover, the label bootstrap always runs; the board ensure runs when the
entry's `tracker.variant` is `board`, or when the operator passes
`--board` for a labels entry to pre-provision before a flip (section 3
writes the inert table; the flip stays the separate config edit of
`07-board-fields.md` section 6). A labels entry without `--board` is
labels-only, byte-identical to the `06` design.

`setup --plan` prints what it would do and does none of it, on either
half of the verb. The label half becomes report-only through the
read that already exists for exactly this: `ensure_labels` always
mutates, so plan never calls it; instead it calls
`Tracker.missing_labels(repo)`, the built read-only listing
`GithubTracker.ensure_labels` itself starts from and the doctor
already uses, and reports each returned name as would-create (every
bootstrap label not returned is kept). No new label seam is designed
or built; one resolver already owns the absent-labels fact. The
board half reads the live board (a read is the only way to know
created from kept), lists the planned creations, and shows the
mapping write as the `init.plan_diff` of the target file. No
mutation of any kind and no file write happen under `--plan`. A plan
in a bootstrap state (no config address) against a board that does
not exist lists the full creation set from the spec; a plan whose
spec carries an address that no longer resolves plans nothing for
that entry and counts it as the exit-4 configuration error of step
2, like a real run. Plan exit codes follow the built would-change precedent
(`new` and `init` exit 0 when a plan would change something): a
would-create counts as created for the 0/1 split, so the cross-entry
aggregation above applies unchanged with "created" read as "created
or would create", and the same 4-over-3-over-1-over-0 precedence
(0 when something would change and nothing failed or refused, 1 when
nothing would or something was refused, 3 unreachable, 4 usage or
conflict, including an addressed board that no longer resolves).
Exit codes are a public interface, so a plan that lists creations
never exits 1 merely because it created nothing.

## 3. Writing the mapping into `korapy.toml`

Decision: the provision's identifiers are written by machine, through
`init`'s TOML machinery, into the one `korapy.toml` that already
drives the repo entry. No person copies an id, and no second
`korapy.toml` writer appears (`06-korapy-new.md` section 1: there is
exactly one writer of that file; this design adds one helper to it,
not a rival).

### Which file

The file is the ops root's `korapy.toml` for the entry: in the inline
layout the project's own file, in the split layout the ops root's
file and never the project clone, where korapy writes nothing tracked
(R1 in `01-onboarding.md`). The root is the one root the built verb
already resolves: `settings.find_root` returns the nearest single
directory at or above the working directory holding a `korapy.toml`
(it finds one root, never one of several candidates), and
`githubcli.cmd_setup` runs against that root today. Setup adds no new
way to find it. No root above the working directory, or an entry name
that root does not hold, is the built verb's pre-loop exit 4, with
`korapy init` as the fix when there is no root: provisioning never
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
stdlib parses TOML but does not write it, so like every other korapy
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
project_owner = "example"    # board owner, written by korapy github setup
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
resync, `korapy doctor`. When it lands on an entry already in board
mode (the bootstrap state: variant flipped first, doctor failing on
the missing table until setup runs), the next step printed is
`korapy doctor`.

## 4. Populating a new board, and the first sync

Decision: adopting existing issues is an explicit, item-add-only step
of setup (`korapy github setup --populate`), separate from ensuring
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
   back by hand, and pull requests are never board items korapy
   creates (the reader tolerates PR items people added; it does not
   put them there). External issues (an author who is not an operator)
   are included: board membership writes to the board, not to the
   issue, and external items stay read-only for every item write, as
   today (decision O8).
3. Add each open issue not already present, one add-item mutation per
   issue by its node id: `addProjectV2ItemById(projectId, contentId)`
   (VERIFIED: section 9, item U9). The add is idempotent: adding an
   issue already present returns the same item id. An
   already-present answer, if the pre-check raced a person adding the
   same issue, reads as kept, never as a duplicate korapy then tries
   to clean up: adoption has no removal path at all. Immediately
   after a link or create, the first adds can answer UNPROCESSABLE
   with a temporary move conflict ("Your attempt to move this item
   created a temporary conflict. Please try again.") or a generic
   execution failure carrying a request id; the adapter retries these
   bounded and classifies them transient, never a refusal.

Adoption sets **no field value on any item**: not `Status`, not
`Priority`, never the korapy-owned texts. It adds items only; it
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
  value, and empty korapy-owned texts echo nothing, so label-derived
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
| Board mode, no `[repo.board]` table | fail (exists) | `korapy github setup` (creates the board and writes the table) |
| Table names a board that does not resolve (deleted, wrong owner or number, no read access) | fail, naming owner and number | the board in the config no longer exists: remove the table and re-run `korapy github setup`, or correct the address by hand (setup refuses the same state with exit 4 and this same line; it never recreates over a stale address) |
| Token lacks the board scope | fail, naming the missing scope | `gh auth refresh -s project` (scope verified for writes, refusal classification UNVERIFIED, U8; printed for the person, who owns their login) |
| Board rate-limited during the check | fail (exists, marked transient) | wait for the cooldown, re-run `korapy doctor` |
| Mapped field missing on the live board | fail, naming repo, concept and field name | `korapy github setup` (creates the missing field, alters nothing) |
| Mapped field present with the wrong kind | fail, naming concept, field and both kinds | rename or delete the field on the board, then `korapy github setup` |
| A `priority_map` option id is gone from the board | fail (exists, `check_options`), naming field, id and rank | re-add the option on the board, or delete the board table and re-run `korapy github setup` |
| A live priority option the map does not name | warn, naming the option's display name | add its id to `priority_map`, or leave it: those items keep their stored rank |
| Variant flip with queued writes | fail (exists, `flip_refusal`) | drain the outbox, then flip |
| Label and board priority disagree; unparseable `blocked_by` | fail (exists, `parity_lines`) | backfill on the board, then resync |
| Board provisioned but variant still `labels` | warn, naming the table | the `07` section 6 flip steps, or remove the table |
| Another board under the same owner shares the configured board's title | warn, naming the title and every matching number | delete the board the config does not address, by hand (the racing-setups duplicate of section 2; korapy deletes no board) |
| Open issues missing from the board | fail, naming the count and the first few numbers | `korapy github setup --populate` (needs the section 8 ticket 6 seam; not in the first doctor ticket) |

Six implementation notes, so the rows above are buildable rather
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
  seam rather than growing a second matcher in `boarddoctor`. The
  spike (U8) confirmed the `project` scope suffices for writes but did
  not observe the refusal text, so the row still ships behind no
  hard-coded refusal string: only the classifier decides, and the
  refusal classification is tested with canned payloads until
  observed.
- **The warn rows need warn plumbing.** `boarddoctor.BoardRow.status`
  is only `"ok"` or `"fail"` today; `doctor.Check` already carries
  `"warn"` and warn-only rows do not fail the doctor. Ticket 6 widens
  `BoardRow.status` to `"ok" | "warn" | "fail"` and passes it through
  to the `Check` unchanged. Row precedence inside `_check_site` is
  fail over warn over ok: any fail finding makes the row fail, else
  any warn finding (a live priority option the map does not name,
  the duplicate-title row below) makes it warn, else ok.
- **The duplicate-title warn lists titles under the owner.** It
  needs data the addressed schema fetch does not return: the
  configured board's live title, and the titles and numbers of the
  other boards under the same owner. Ticket 6 widens what the
  `fetch` seam returns beside the resolved schema (the board's title
  and the owner's board list, the same paged title list step 2 of
  provisioning reads, through the adapter and never a second
  resolver in `boarddoctor`), and the pure comparison warns when
  another listed board shares that exact title. It is warn, not
  fail: the addressed board is healthy, and which of the two a
  person deletes is a people's call. A title list that fails to
  fetch folds into the row's existing fetch-failure handling, never
  into a silent ok. The labels-with-table warn above is exempt, as
  stated there: that row fetches nothing.
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
  back a concept with labels, exactly as korapy's own label adapter
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
  map owned by config, as `priority_map` already is, and korapy code
  outside the adapter must never parse, compare or mint an option key.
  This is today's rule; provisioning must not weaken it by treating
  the keys it harvests as anything more than strings to write down.
- **No title uniqueness or title lookup.** Already true here (titles
  are a provisioning-time search key only, ambiguity refused), and
  the interface must keep runtime addressing free of titles so an
  adapter with no title search can still implement every read.

And the mirror rule for korapy itself: nothing outside
`github/board.py` and its provisioner may learn a mutation name, a
URL shape or a `dataType` spelling from this work. The day a second
adapter is needed it is a new module and its tests (`00-design.md`
section 2), not a fork of the setup CLI.

## 7. Korapy's own board

Korapy adopts its own standard board once the provisioning above is
built, and not by an agent ticket: creating boards is on the
AGENTS.md approval-gate list, so the run below happens in a
maintainer's own session, on the maintainer's explicit go-ahead in
that session. Nothing in this design creates korapy's board, and the
rollout in section 8 deliberately ends before this step.

When approved, the steps are the ordinary adoption path with no
special cases:

1. `korapy github setup --board --populate` against korapy's own
   tracker repo, from the root that drives korapy. This works on
   korapy's labels entry precisely because the provisioner factory
   needs no `BoardConfig` (section 2). The mapping lands
   in whichever `korapy.toml` that is: this repository carries no
   `korapy.toml` of its own today, so the file written is the driving
   ops root's, per section 3, and it is committed there like any
   other config change (through a PR, if that root takes PRs).
2. Drain the outbox through the mirror until empty.
3. Backfill the people-owned fields from the labels on the board
   itself, a people pass over the open issues, using the doctor
   parity report as the checklist.
4. Flip `tracker.variant` to `board`, resync, `korapy doctor`.
5. Leave the labels in place. Rollback is the flip undone plus a
   resync (`07-board-fields.md` section 6); the board itself is never
   deleted by korapy, and a board abandoned by a roll back stays as a
   people's artifact until a person removes it by hand.

Until then korapy stays in label mode, and the standard's first real
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
   fields, visibility defaults, option-append survival of existing
   ids, colors and descriptions when the whole list is resent with
   them, the token scope and its error text, the already-present answers, title
   uniqueness, the owner lookup). U10 is not the spike's to settle:
   GitLab facts stay UNVERIFIED until a GitLab adapter is itself
   designed. Acceptance:
   each of U1 to U9 and U11 is marked verified or false with the command that
   showed it, in an amendment to this doc; where a fact is false, the
   affected section above is amended before ticket 2 starts (in
   particular, if U7 fails, section 2 step 6 ships as its refused-line
   variant). No korapy code changes.
1. **Types, the standard spec, and the pure planner.** `tracker.py`
   gains `FieldSpec` (with `keep_only`), `BoardSpec` (with the
   exactly-one-or-none `number`/`project_id` address and the
   existing `priority_map` as `None`-or-dict, no mutable default),
   `BoardProvision` (with live `fields`),
   the `BoardProvisioner` protocol and `STANDARD_BOARD`;
   `effective_spec` resolves config-names-else-standard in one place,
   including carrying the address from `BoardConfig` unchanged,
   normalizing a `None` priority map to a fresh dict, and
   config-mapped `Area` and `Pool` as keep-only; a pure
   planner maps (spec, live schema as plain data, each option
   carrying id, name, color and description) to the ordered
   create/keep/refuse action list the adapter will execute.
   Acceptance: `effective_spec` tests show a board table's number
   (and, in a second case, its node id) carried into the spec, no
   address when there is no table, both-address and non-positive
   number refused, and two specs built in sequence sharing no
   priority-map object; planner tests cover empty board, exact board, wrong
   kind, missing option, extra team fields and options (all kept,
   none planned for change), and the five-rank map built from live
   ids; a renamed priority option (`High` renamed, id unchanged, the
   spec's map naming that id) plans no append and keeps its rank,
   while a rank with neither a live mapped id nor its standard name
   plans exactly one append whose resent option list carries every
   existing option's color and description unchanged; a kept
   `Status` field plans no option append whatever its option names are; duplicate field names and
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
   a re-run over an exactly-standard board sends no mutation; a
   renamed-board re-run (the spec carries the board's number, the
   canned board's title no longer matches the spec title) resolves
   by address, sends no create and no title search, and reports
   all-kept; a spec whose address resolves to nothing raises
   `ValueError` with the remove-the-table fix line before any
   mutation and sends no title search and no create; an
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
4. **Extend `korapy github setup`, including its production
   wiring.** The verb and its label half are built
   (`githubcli.setup_root`, `ensure_labels` on the `Tracker`
   protocol); this ticket adds no verb and rebuilds no label code.
   It first performs the section 2 `cmd_setup` restructure:
   `trackers.SetupEntry`, `setup_entry` and `setup_tracker` beside
   the untouched `production_trackers`; `cmd_setup` stops building
   every tracker up front and passes `entry_factory`,
   `tracker_factory` (now taking a `SetupEntry`) and
   `provisioner_factory` (production:
   `trackers.production_board_provisioner`) into `setup_root`, which
   resolves and builds per entry, lazily, inside the loop, a board
   entry with no table getting a labels tracker for its label half
   and the provisioner for its board half. On that wiring it adds
   the flags `--board`, `--populate`, `--plan`, `--area`, `--pool`,
   `--board-owner` and `--board-title`, the per-entry board ensure
   (board-mode entries, or any entry with `--board`) after the label
   ensure, the section 3 mapping write, the cross-entry aggregation
   of section 2 (4 over 3 over 1 over 0, every entry attempted), and
   the next-step lines (flip steps for labels entries, doctor
   otherwise). Acceptance through fake entry, tracker and
   provisioner seams: a labels entry with `--board` provisions and
   writes the inert table (the factory needed no `BoardConfig`); a
   board-variant entry with no `[repo.board]` table provisions and
   writes the table that makes it whole, and the same case passes
   at `cmd_setup` level with the production `entry_factory`,
   `tracker_factory` and `provisioner_factory` in place and only
   the `run` and git seams faked, proving the real verb no longer
   exits 4 before the loop and that its `SetupEntry` carries
   variant `board` with `board is None` into `setup_root`; an entry
   whose lazy resolution or construction fails counts 4 and the
   entries after it still run; a plain re-run after a run
   with `--area` and `--pool` is a no-op, exit 1, with no
   different-table refusal; mixed multi-entry runs aggregate as
   section 2 states (entry A creates and its mapping file stands,
   entry B raises `TrackerError`, entry C hits a different table: all
   three attempted, final exit 4; A and B without C exit 3; created
   plus refused exits 1; all-kept exits 1); every row of the section 2
   per-board exit table; `--plan` creates no label (the fake tracker
   sees `missing_labels` and no `ensure_labels`, and no new label
   seam exists), sends no board mutation and writes no file byte,
   and its exits count would-create as created (a plan listing
   creations exits 0, a plan over a converged root exits 1, and
   mixed plans keep the 4-over-3 precedence); the `--area`,
   `--pool`, `--board-owner` and `--board-title` flags reach
   `effective_spec` and nowhere else; a labels entry without
   `--board` behaves byte-identically to the built verb today; the
   README Commands row for `github setup` is updated for the board
   half and the new flags in this ticket.
5. **Population.** The adapter's populate operation and its setup
   wiring (may land with ticket 4 if small): open issues only,
   presence pre-read, add-missing only, no field value ever written.
   Acceptance: an already-present issue is kept without a mutation, a
   closed issue and a PR are never added, an external author's open
   issue is added (or not, per O8), partial failure reports per issue
   and a re-run adds only the remainder.
6. **Doctor rows.** The `boarddoctor` raw-schema comparison (missing
   field, wrong kind, unmapped option warn), the scope row over the
   exposed classifier, the provisioned-but-labels warn, the
   duplicate-title warn (the widened `fetch` seam returns the
   configured board's title and the owner's board list beside the
   schema; another board sharing that exact title warns, naming the
   title and every matching number), fix lines pointing at
   `korapy github setup`; the warn plumbing
   (`BoardRow.status` gains `"warn"`, passed through to `doctor.Check`,
   fail over warn over ok per row) and the site-inclusion rule (a
   labels entry with a `[repo.board]` table becomes a site answered
   warn-only from config, with no `fetch` and no store call for that
   row; a labels entry without a table still gets no row); then, in
   the same ticket or a follow-up if it grows, the missing-from-board
   row over its second injected fetch. Acceptance: each row of the
   section 5 table names repo and field against fake seams; the
   duplicate-title warn fires for a fake owner list holding two
   boards of the configured title (and stays ok for a second board
   of a different title, and for the configured board itself being
   the only match), leaving the doctor exit at 0; the
   labels-with-table warn is produced with the fake `fetch` asserting
   it was never called (so it carries no duplicate-title check);
   doctor exit codes unchanged (fail rows exit 1,
   warn-only exits 0); no row runs anything.
7. **`new --create-remote` wiring.** `korapy new` is built; this is a
   small extension of it, not a blocked ticket. In the
   `--create-remote` path, after the built `_bootstrap_labels`, invoke
   the same board ensure (the `production_board_provisioner` factory
   and `effective_spec`, never a second implementation) for the new
   entry and write its mapping through ticket 3's writer. Outcomes
   map onto `new`'s existing codes by the `_bootstrap_labels`
   precedent: created counts as changed; refused prints
   `next: korapy github setup` and the run ends 1; `TrackerError`
   prints the same next step and the run ends 3; a spec `ValueError`
   is a usage error, 4. Without `--create-remote`, print
   `korapy github setup` exactly as today. Acceptance: fake-forge,
   fake-tracker and fake-provisioner tests show the one operation
   invoked once, no second provisioning path, the four outcome
   mappings above, and the printed-not-run default unchanged.
8. **Korapy's own adoption.** Not an agent ticket. Held for the
   maintainer's go-ahead in their own session, run per section 7.

## 9. Unverified facts and open decisions

UNVERIFIED means: believed true of GitHub Projects v2 or GitLab, not
yet run, and not to be treated as fact by any implementation ticket.
VERIFIED means the maintainer-run spike of ticket 0 showed it on real
GitHub, against throwaway Projects that were created, exercised and
deleted; this doc records mutation names and input shapes only, with
generic stand-ins (`example-owner`, `PVT_xxx`) and no ids or logins.
PARTIAL means the spike showed part of the claim. FALSE as written
means the spike refuted the claim as stated, and the refined fact
beside it is what implementation tickets use. Ticket 0 is recorded:
U1 to U6 VERIFIED, U7 FALSE with a refinement, U8 PARTIAL, U9 and U11
VERIFIED with refinements, U10 still UNVERIFIED. No open decision O1
to O8 changes on these results. Decisions O1 to O8 are the
maintainer's; each block has one line under 60 characters per option
and one marked recommended.

- U1. VERIFIED. A new Project ships built-in fields (Title,
  Assignees, Labels, Linked pull requests, Milestone, Repository,
  Reviewers, Parent issue, Sub-issues progress, Created, Updated,
  Closed) and a single-select `Status` with `Todo`, `In Progress`
  and `Done`, each with a color and a description. Shown by listing
  the new Project's fields, and by `deleteProjectV2Field` on
  `Status` failing UNPROCESSABLE with "Only custom fields can be
  deleted."
- U2. VERIFIED. `createProjectV2Field` with dataType SINGLE_SELECT
  and zero options fails with "At least one singleSelectOption is
  required for data_type SINGLE_SELECT."
- U3. VERIFIED. `createProjectV2(ownerId: PVT_xxx, title: ...)`
  takes an owner node id and a title; two creates with the same
  title both succeed, so titles are not unique per owner. The legacy
  node id form still answers but with a DEPRECATION warning naming
  `next_global_id`;
  the adapter prefers the new id form where the API returns it.
- U4. VERIFIED, refined. `CreateProjectV2Input` carries `ownerId`,
  `title`, `repositoryId` and `teamId`, and no visibility; a new
  user-owned Project reports `public: false`. `UpdateProjectV2Input`
  carries `public`, so the read-back plus make-private correction in
  section 2 step 3 stands as designed. Create also accepts
  `repositoryId`, which links the repository in the same step.
- U5. VERIFIED. `linkProjectV2ToRepository(projectId, repositoryId)`
  links a repository; linking the same repository a second time
  succeeds again, so a repeated link reads as kept.
- U6. VERIFIED. `createProjectV2Field` takes `dataType` TEXT or
  SINGLE_SELECT; single-select options at creation carry `name`,
  `color` and `description`, and come back with ids.
- U7. FALSE as written, refined. `updateProjectV2Field(fieldId,
  singleSelectOptions)` replaces the whole option list. Resending
  the existing options WITHOUT their ids gives every option a new
  id; resending each existing option WITH its `id` keeps every id,
  color and description, and a new option without an `id` is
  appended with a fresh id. `ProjectV2SingleSelectFieldOptionInput`
  carries `id`, `name`, `color` and `description`, with `color` and
  `description` required on every option (omitting either is a
  schema error). Section 2 step 6 therefore ships only in the strict
  form: read the live options with ids, colors and descriptions,
  resend all of them with their ids, append the new ranks; refuse
  with a fix line when any live option cannot be resent in full.
- U8. PARTIAL. A token carrying `gist`, `project`, `read:org`,
  `repo` and `workflow` ran every mutation above with no other scope
  added, so `project` suffices for board writes. The refusal text
  for a token lacking the scope was not reproduced (no such token
  was available), so the error classification stays UNVERIFIED:
  test it with canned payloads only until the text is observed, and
  keep the doctor scope row behind the classifier, not behind a
  hard-coded string.
- U9. VERIFIED, refined. `addProjectV2ItemById(projectId, contentId)`
  adds an issue by node id and is idempotent: adding the same issue
  again returns the same item id, so a raced add reads as kept.
  Immediately after a link or create, the first adds can answer
  UNPROCESSABLE with a temporary move conflict ("Your attempt to
  move this item created a temporary conflict. Please try again.")
  or a generic execution failure carrying a request id; retrying
  after a few seconds succeeded. The adapter retries these bounded
  and classifies them transient, never a refusal.
- U10. UNVERIFIED. Every GitLab statement in section 6. Unchanged:
  not the spike's to settle.
- U11. VERIFIED, refined. Resolving an owner login by asking
  `user(login: example-owner)` and `organization(login:
  example-owner)` in one query: for a user
  login the body has the user half set and the organization half
  null, plus a NOT_FOUND error naming the organization login ("Could
  not resolve to an Organization with the login of ..."); for an
  unknown login both halves are null with two NOT_FOUND errors.
  `gh api` exits non-zero in both cases, even when one half
  succeeded, and `gh api graphql` exits non-zero on any `errors`
  entry even beside partial `data`. The adapter therefore reads the
  JSON body and decides from `data`, never from the exit code, and
  the `gh` seam returns the body for GraphQL calls for the adapter
  to classify.

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

O7. Title for a board korapy creates:
Options:
1. "<repo> board" from the tracker repo name (recommended)
2. The entry name from korapy.toml, unchanged
3. No default; setup requires a --board-title flag

O8. External authors' open issues during population:
Options:
1. Add them; board membership writes no issue (recommended)
2. Operators' issues only; external stays off-board
