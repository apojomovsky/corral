# 10: agent profiles (runtime, models and options)

Status: design, awaiting maintainer approval. No code ships with this doc.

Decided by the maintainer (do not reopen):

1. korapy lets the operator change not only the model backend (provider
   and model) but also the program that runs the agent: muse or omp.
   korapy knows how to set each program up (per-launch
   overlay; it never edits a home config, per AGENTS.md).
2. There is a default profile, the most used setup, and it can change
   over time.
3. A different profile is selectable for a project, for a role, for a
   single ticket, and for a limited time, from the same mechanism.
4. Nothing needs to be committed to switch: the operator's switches live
   in local, host-level configuration and reach the agent through the
   per-launch overlay. Committed project config may still declare
   defaults and named profiles.

It decides the ten points the ticket lists, with reasoning. Where it
changes a public surface (a setting, a launch flag, a CLI verb, the
snapshot schema) it says so; implementation waits for approval. The one
UNVERIFIED claim is marked as such with a safe read-only check.

## 0. Scope and non-goals

The slice adds: a profile object (section 1), one resolver with
provenance over an ordered layer list (section 2), per-ticket selection
through the tracker interface plus a local-only pin (section 3),
temporary overrides with expiry (section 4), operator verbs replacing
the narrower `models use` idea (section 5), adapter setup requirements
(section 6), fail-closed failure modes (section 7), a cross-runtime
fallback decision (section 8), a one-cutover migration (section 9), and
ordered implementation tickets (section 10).

Non-goals: a new runtime (muse and omp stay the two, per
`runtimes.NAMES`); a second provider (GitHub only, per `00-design.md`
section 2); prompt changes; dashboard writes (the snapshot stays
read-only, gaining one recorded field); any change to what an exit code
means (AGENTS.md fixes them).

No `models use` verb exists today (verified: `quotacli.py` ships only
`refresh`, `mark` and `unmark`; `agentcli.py` and `cli.py` carry no
model-selection verb), so profiles replace an idea, and nothing is
removed. `korapy launch --model` and `--runtime` stay as one-shot
refinements (section 5).

## 1. The profile object

Decision: a profile is a named, committed-or-local declaration that
answers the whole agent setup in one place: which program runs, which
model chains it may use, and that program's own options.

```toml
[profiles.default]
runtime = "muse"

[profiles.default.models.default]
chain = ["provider-a/model-x", "provider-b/model-y"]

[profiles.default.models.reviewer]
chain = ["provider-a/model-x"]

[profiles.default.options]
reasoning_effort = "high"
```

Fields:

- `name`: the table key. Lowercase `[a-z0-9-]`, non-empty. The charset
  is label-safe on purpose, so a ticket label can carry a profile name
  verbatim (section 3).
- `runtime`: exactly one of `runtimes.NAMES` (`omp`,
  `muse`). Anything else is a configuration error (exit 4).
- `models`: role to chain lists, the same shape `[models.<role>]`
  carries today. Every chain entry is validated against the accounts
  pool exactly as now (`models.validate`: non-empty id, provider prefix
  known), so an unknown provider stays exit 4.
- `options`: the runtime's own options as a flat string-to-scalar table
  (strings, numbers, booleans only; no nesting, so it renders into `-c
  KEY=VALUE` style overlays and JSON configs without a second encoding
  rule). Every key must appear in that runtime adapter's
  `options_schema()` (section 6); an unknown key is a configuration
  error (exit 4) naming the runtime and the key.

Common versus runtime-specific: the name, the runtime choice and the
model chains are common. They drive the parts that are runtime-blind:
the quota gate evaluates the resolved profile's chains, the snapshot
and registry record the name and runtime, and re-route plus refusal
(`models.check`, exit 75) work unchanged on the profile's chains.
Everything else is runtime-specific and lives in `options`, owned by
exactly one adapter through its schema. There are no shared option
keys in this design: a key two runtimes happened to spell the same
would need two validators and two meanings, which is a second resolver
for one fact. If a genuinely shared knob appears later, it gets its
own design, not an exception here.

Relation to the existing pieces:

- Model chains, accounts and quota fallback (`docs/02-accounts-quota.md`):
  profiles own the chains; the ledger, states and the gate do not
  change. The gate runs over the resolved profile's chains at launch,
  and the per-launch overlay still carries the chains with exhausted
  providers removed.
- Runtime adapters (`docs/03-muse-runtime.md` pattern): `with_models`,
  `argv`, `env`, `write_config`, `resume_handle` and `activity_file`
  keep their shapes. The adapter gains one read-only declaration,
  `options_schema()`, and lays validated `options` over the per-launch
  config the way it already lays chains. Precedence inside the
  per-launch config: the profile's options lay first, `--runtime-config`
  wins on shared keys, and the live chains lay over both through
  `with_models`. `--model` and `--runtime` refine the run on top (below)
  and are recorded in provenance. A `--runtime` refinement re-validates
  the profile's options against the effective adapter's schema: keys
  from one runtime never land in another's config, and an unknown key
  is a configuration error (exit 4) naming the key and the runtime. The
  launch records the raw `--runtime-config` beside the session config;
  a resume with `--profile` or `--runtime` rebuilds the session config
  from the new profile the same way, re-applying the recorded
  `--runtime-config` under the chains (still winning over the new
  profile's options); any other resume keeps the recorded setup. The
  new adapter's resume handle is checked before anything is rewritten,
  so a refused flagged resume leaves the session untouched.
- `korapy launch --model`: kept, narrowed in meaning. It overrides the
  head of the launched role's chain for that launch only (today it
  overrides the model the same way), and provenance records it as a
  flag refinement (section 2). `--runtime` likewise overrides the
  profile's runtime for one launch. Neither is a profile, and neither
  is stored.

Validation has one owner: `profiles.validate(declarations, pool)`
in the new `profiles.py`, reusing `models.validate` for chains and
each adapter's `options_schema()` for options. Every reader of
profiles goes through it; a launch that resolves an invalid profile
fails with exit 4 before anything is made (no worktree, no unit).

The built-in `default` profile is defined once, hardcoded in
`profiles.py`: the shipped default setup (runtime `omp` with no chains
or options of its own, per `runtimes.DEFAULT_RUNTIME`: one fact, one
place). Layer 6 falls back to it only when no `[profiles.default]`
table is declared, so resolution always succeeds.

## 2. Scopes and precedence

Decision: one ordered layer list, most specific first, with exactly one
resolver. Local selections always win over committed defaults within a
layer, and provenance always names both the layer and the source.

| # | Layer | Lives | Committed or local |
| --- | --- | --- | --- |
| 1 | flag | argv: `--profile NAME`, refined by `--model M` / `--runtime R` | ephemeral (this launch only) |
| 2 | ticket | tracker label `profile:<name>` (labels mode) or board `profile` concept (board mode, section 3); or a local pin | shared-visible (tracker) or local (pin) |
| 3 | temp | temporary override with `until` (section 4) | local |
| 4 | project | local per-project selection; else the repo's committed default | local wins, committed fallback |
| 5 | role | local per-role selection; else the committed per-role default | local wins, committed fallback |
| 6 | default | local `profile.default`; else the committed default; else the declared `[profiles.default]`; else the built-in `default` | local wins, committed fallback |

Concretely:

- Named profiles are declared in `korapy.toml` as `[profiles.<name>]`
  tables (committed). The host config directory may also declare
  `[profiles.<name>]` tables for operator-only setups; a local table
  with the same name shadows the committed one, and `profile show`
  (section 5) names the shadow so it is never silent.
- Selections live outside `settings.resolve`, in one new local file,
  `profiles.toml` in the host config dir (`paths.config_dir()`), plus
  the tracker for the shared ticket assignment. This placement is
  load-bearing: normal settings precedence lets `korapy.toml` beat the
  host config (`01-onboarding.md` section 2), which is backwards for an
  operator switch, so selections get their own resolver where local
  always wins. One fact, one place: `profiles.py` is the only module
  that reads `profiles.toml` and the only module that maps layers to a
  profile.
- The committed defaults are three small keys: `profile.default` (the
  global default name, built-in `"default"`), `[[repo]] profile` (the
  repo default), and `[role.<name>] profile` (the per-role default,
  e.g. reviewer). Each is optional; an absent key falls through to the
  next layer.
- The resolver is a pure function over plain data,
  `profiles.resolve(declarations, selections, ticket_assignment, *,
  repo, role, number, now)`, returning `(Profile, Provenance)`. All I/O
  (reading files, reading the ticket assignment) happens at injected
  seams, so tests drive it with fakes. Every reader goes through it:
  `launch`, `resume`, `drive` (for the pre-launch check), `snapshot`,
  `doctor` and `profile show`.
- `Provenance` names the deciding layer, the source that carried it
  (flag text, label, board field, pin, file path), the profile name,
  and, for layer 3, the `until` instant. `profile show` prints the
  whole chain considered, not just the winner, so an operator sees what
  a lower layer would have picked.
- `resume` reuses the profile recorded at launch (section 4) unless
  `--profile` (or `--model` / `--runtime`) is passed again, in which
  case the flag layer decides for the resumed run and the new value is
  recorded. A resumed run never silently inherits a changed default.

## 3. Per-ticket selection

Decision: a ticket carries at most one profile assignment, through the
tracker interface when the team should see it, or through a local pin
when only this host should know. Both feed layer 2; they never stack.

- Labels mode: a label `profile:<name>`. The board store already
  persists labels on every row (`board.Item.labels`), so launch reads
  the assignment from the store row, with a live re-read past the
  freshness bound exactly like claim paths (`04-dispatch-loop.md`
  section 4): a stale row with an unreachable tracker skips the launch
  instead of guessing.
- Board mode: a `profile` concept in `extra`, alongside the section 1
  concepts of `07-board-fields.md`. Carriage is a people-owned text
  field holding the exact profile name (no option-id map: profile names
  are already korapy-owned strings in a label-safe charset, so there is
  nothing to map). An unparseable or unknown name is carried verbatim
  and fails closed at resolve time (exit 4), never treated as empty.
- Local-only alternative: `korapy profile set --ticket REPO N NAME`
  writes a pin into `profiles.toml`. The pin is never mirrored, never
  touches the tracker, and never leaves the host.
- Both present: when the tracker assignment and the pin name different
  profiles, the launch fails with exit 4 naming both sources. When they
  agree, or only one is present, that one decides. This is the
  override-conflict rule of section 7 applied at the smallest scope.
- Ticket assignment never affects eligibility or claims: the dispatch
  pass claims tickets exactly as today, and the profile resolves at
  launch time. A ticket whose profile names an unknown or uninstallable
  runtime fails the launch (section 7), it does not reroute the claim.

UNVERIFIED: no surveyed repository uses a `profile:*` label prefix for
an unrelated automation today, so the prefix is assumed free. Settle it
per repository with one read-only command before the first ticket
assignment: `gh label list --repo owner/name --search profile` (or the
board field list for board repos). On a collision the prefix gets its
own design instead of an exception here.

## 4. Temporary overrides

Decision: a temporary override is a layer-3 selection with an `until`
instant, recorded next to the other selections in `profiles.toml`, and
it stays visible after expiry until explicitly reset.

```toml
[override]
scope = "project:example"   # global, project:<repo>, role:<role> (never ticket: layer 3 cannot beat layer 2)
profile = "fast-backend"
until = 1730000000          # epoch seconds; set with --temp 2h or --until
reason = "trying the new setup for the afternoon"
```

- Setting: `korapy profile set (--global | --project R | --role O)
  NAME --temp 2h` (or `--until <instant>`). `--temp` with `--ticket`
  is rejected (section 5). Durations reuse `accounts.parse_duration`:
  `<n>[s|m|h|d]`, bare seconds, floats (`2h`, `90`, `1.5d`).
- Expiry: past `until`, the resolver ignores the override as if it
  were absent. `profile show` keeps listing it marked `expired`, and
  the doctor reports it (section 5), until `korapy profile reset
  --temp` removes it. Expiry never edits anything else: no other layer
  moves, and no config is rewritten.
- A long-running agent started under an override is unaffected by its
  expiry. At launch korapy records the resolved profile (name, runtime,
  model head, provenance) in the session dir next to the existing
  `runtime` record (`runtimes.record` precedent). Resume reads the
  record back, exactly like `runtimes.recorded`. The snapshot shows the
  recorded profile; whether a fresh launch would resolve differently is
  answered by `profile show`, not by touching the live agent.

## 5. Operator surface

Decision: four verbs under `korapy profile`, one launch flag, one
snapshot field, and doctor rows. Nothing else changes.

| Verb | What it does |
| --- | --- |
| `korapy profile list [--json]` | every named profile (committed and local) with its runtime and role chains; a shadowed name is marked with its winning source |
| `korapy profile show [--repo R] [--role O] [--ticket N] [--json]` | the resolved profile for that ticket and role, with the full layer chain and provenance (which layer decided, from which source, and any expiry) |
| `korapy profile set (--global \| --project R \| --role O \| --ticket R N) NAME [--temp 2h \| --until T] [--reason R]` | write a local selection (layer 6, 4, 5, or the ticket pin in layer 2) or a temporary override (layer 3 with `--temp` / `--until`); `--temp` / `--until` with `--ticket` is rejected with exit 4 naming layer 2 as the shadow, with the fix (clear the ticket assignment or pin first) |
| `korapy profile reset (--global \| --project R \| --role O \| --ticket R N \| --temp)` | remove that local selection, pin, or override; exit 1 when there was nothing to remove |

`korapy launch` gains `--profile NAME` (layer 1). `--model` and
`--runtime` stay as one-shot refinements that beat the profile's values
for that launch only; `profile show` cannot predict them, and the
launch line in the session record notes them. `korapy resume` accepts
the same three flags with the section 2 semantics.

Exit codes follow AGENTS.md (meanings unchanged):

| Situation | Exit |
| --- | --- |
| list / show / set / reset ok | 0 |
| reset with nothing stored | 1, naming the missing piece |
| ticket already live, lost race (launch path, unchanged) | 2 |
| tracker unreachable while the ticket layer needs a live read | 3 |
| unknown profile name, unknown runtime, unknown option key, bad duration, override conflict, ticket assignment naming an unknown profile, runtime binary missing | 4, naming the value and the fix |
| every provider on the resolved chain exhausted | 75 (unchanged `models.Exhausted`) |

Doctor checks (each failing line names the fix; unknown runtime,
missing binary and expired override fail, the rest are notes until the
rollout tickets say otherwise):

- every declared profile validates (section 1); unknown runtime and
  unknown option key fail here too, before any launch;
- every referenced runtime's binary resolves (the `system_deps.toml`
  probe for that runtime); a missing binary fails with the install
  command, which the doctor prints but never runs (installing a runtime
  needs the maintainer's go-ahead);
- every selection, pin, override and ticket default names a declared
  profile; a dangling reference fails;
- an expired override is still on disk: fails until
  `profile reset --temp` (visibility, per section 4);
- a shadowed `[profiles.<name>]` (local over committed) is listed as a
  note with both sources.

Snapshot visibility: each agent row gains `profile`, the name recorded
at launch. This is a small additive schema change to the stable
snapshot document (`05-dashboard-api.md` reports both versions, so the
rollout ticket bumps `schema_version` there). No other snapshot shape
changes; drift between recorded and current resolution is a `profile
show` question, not a snapshot field (section 4).

`korapy config show` does not render selections: they live outside
`settings.resolve` by design (section 2). `profile show` is the
provenance view, and it prints the same origin style (`value  # from
<source>`) so the two verbs read alike.

## 6. Runtime setup

Decision: a runtime is selectable in a profile when its adapter
provides four things; adding one is a checklist, and the
one-major-version rule is untouched.

1. The `Runtime` protocol as today (`argv`, `env`, `write_config`,
   `with_models`, `resume_handle`, `activity_file`), writing only into
   the session dir it is given.
2. `options_schema()`: a read-only mapping of allowed option key to its
   type and one-line meaning. Unknown keys fail at validation (exit 4);
   the schema is pinned by tests so a new persisting flag fails the
   suite until the design is revisited (the `03-muse-runtime.md` flag
   allow-list precedent).
3. A `system_deps.toml` entry with a version probe and a `below` bound,
   so the doctor and `korapy deps` see the runtime exactly like the
   existing three.
4. Overlay behavior that never writes a home config: per-launch config
   travels by argv or a session-dir file, and a test pins that argv and
   env contain no home-config path.

Sandbox and permission options (yolo/approval modes, read-only reviewer
flags, workspace trust) are profile options validated by the adapter's
schema, not launch-mode special cases, except `reviewer=True`, which
stays a launch-mode flag: the reviewer prompt and the runtime's
read-only mode are selected by role, and the session records the role,
so resume keeps the same mode (launch.py documents this today).

Adding a new runtime is therefore: a module under
`src/korapy/runtimes/`, a `NAMES` entry, the `system_deps.toml` entry,
the options schema, and the section-10 tests. A new major version of an
existing runtime is a cutover, not a dialect: one PR ports the adapter
and deletes the old support, and the `below` bound starts failing
loudly the day the binary jumps ahead (AGENTS.md, one version of each
runtime).

## 7. Failure modes

Decision: every profile failure closes, names the value and the fix,
and never falls back to the default silently. A silent fallback would
run an agent on a setup nobody chose, billed to quota nobody gated;
the tests pin this with a case per row.

| Failure | Exit | Behavior |
| --- | --- | --- |
| `--profile` names an undeclared profile | 4 | launch refused before anything is made |
| profile declares an unknown runtime | 4 | refused at validation; doctor fails the declaration too |
| runtime binary missing at launch | 4 | refused with the doctor's install hint; nothing started |
| ticket assignment names an undeclared profile | 4 | launch refused; the claim stands, the ticket waits for a human |
| tracker assignment and local pin disagree | 4 | launch refused naming both sources |
| `profile set --ticket` with `--temp` / `--until` | 4 | refused: layer 3 can never beat the layer-2 ticket assignment; names the shadowing layer and the fix (clear the ticket assignment or pin first) |
| a second temporary override while one stands | 0 | setting it replaces the first, and `set` names what it replaced; a hand-edited file carrying two `[override]` tables is refused with exit 4 |
| expired override still on disk | 1 from doctor | resolver already ignores it; `reset --temp` clears it |
| resolved chain fully exhausted | 75 | unchanged refusal with the earliest reset |
| tracker unreachable for a ticket-layer live read | 3 | launch skipped on store data, per the claim freshness rule |

## 8. Cross-runtime fallback

Decision: no automatic cross-runtime fallback. When quota or an outage
takes out every provider on the resolved profile's chains, the launch
is refused with exit 75 naming the profile and its runtime, and the
operator switches profile explicitly (a flag, a pin, or a temporary
override). Rationale: runtimes are different programs with different
overlays, prompts and permission postures; an automatic jump would run
an agent the operator did not choose, break the recorded-runtime
invariant that resume depends on, and bill a different backend. The
manual switch is one command and keeps provenance honest.

Considered and rejected: an opt-in ordered `fallback = [...]` list of
profile names tried after refusal. It adds a second resolution pass
with its own provenance, complicates the exit-75 contract that projects
branch on, and has no requester today. If one arrives, it gets its own
design.

## 9. Migration

Decision: one cutover, no dual dialects. Profiles subsume the two
settings they replace, in a single PR:

1. `[models.<role>]` tables and `agents.runtime` move under
   `[profiles.default]` (runtime plus per-role chains); `profile.default
   = "default"` reproduces today's behavior byte for byte.
2. From that PR on, a `korapy.toml` still carrying top-level
   `[models]` or `agents.runtime` fails fast with `SettingsError`
   (exit 4) naming the new keys. No shim, no deprecation window: two
   readers for one fact is a bug even when they agree (AGENTS.md).
3. Session dirs, the registry and the snapshot keep recording the
   runtime as today; they gain the recorded profile name alongside
   (section 5). Old sessions without a profile record resolve as the
   `default` profile for display only; resume of such a session uses
   today's rule (recorded runtime, default chains).
4. The rollout tickets land in order (section 10); the migration ticket
   is the last code ticket, gated on all profile behavior passing
   first.

## 10. Rollout

Each ticket is one agent of work with its acceptance criteria. Tests
inject fakes at the production seams only (no real `gh`, systemd,
network, or state dirs; `suiteguard.py` keeps refusing them).

1. Declarations and validation. `profiles.py` with the `[profiles.<name>]`
   shape, name rules, chain validation through `models.validate`, and
   adapter `options_schema()` hooks (stub schemas for the three
   runtimes). Criteria: every section-1 shape loads; each bad shape
   (bad name, unknown runtime, unknown provider, unknown option key)
   is `SettingsError`/exit 4 naming the value; the no-silent-fallback
   test fails any default substitution.
2. Selections and the resolver. `profiles.toml` read/write, the six
   layers in order, local-beats-committed inside layers 4 to 6, and
   `Provenance` for every decision. Criteria: a fixture per layer wins
   alone; combined fixtures resolve to the most specific layer; `show`
   prints the full chain with sources.
3. Ticket assignment. Label `profile:<name>` reads from store rows and
   the board `profile` concept through `extra`, plus the local pin;
   conflict refuses exit 4. Criteria: labels-mode and board-mode
   fixtures resolve; stale-row live re-read skips on outage (exit 3);
   tracker/pin disagreement fails naming both.
4. Temporary overrides. Duration parsing, `until` handling, expiry
   semantics, session recording of the resolved profile, resume reuse.
   Criteria: override wins before `until` and is ignored after; expired
   entries list until reset; a session launched under an override
   resumes on its recorded profile after expiry; quota marks and temp
   overrides accept the same duration strings (`accounts.parse_duration`
   shared-parser test).
5. Launch and resume wiring. `--profile`/`--model`/`--runtime` flags,
   resolver call before worktree creation, session record extension,
   `Exhausted` path unchanged (exit 75). Criteria: launch tests through
   the existing fake seams; flag refinement recorded in provenance.
6. Operator verbs and doctor. `profile list/show/set/reset` with the
   section-5 table, doctor rows, snapshot `profile` field with the
   `schema_version` bump. Criteria: each verb's golden output;
   `--json` shapes; `set --ticket` with `--temp` / `--until` fails
   exit 4 naming layer 2 and the fix; README gains its Commands row in
   this ticket.
7. Adapter schemas and setup checks. Real `options_schema()` for omp
   and muse, home-proof tests, `system_deps.toml` probe tests.
   Criteria: the section-6 checklist passes per runtime; an unknown
   option key fails validation.
8. Migration cutover. Move `[models.*]`/`agents.runtime` under
   `[profiles.default]`, reject the old keys with exit 4, convert
   fixtures and docs. Criteria: old-key configs fail naming the new
   keys; the full suite plus `make check` green; no dual-dialect test
   remains.

## Options

Remaining open decisions, each with options; one marked recommended.

1. Local selections file: (1) separate `profiles.toml` in host config
   dir [recommended]; (2) inside `config.toml`; (3) in the state dir.
2. Expired override in doctor: (1) fail exit 1 until reset
   [recommended]; (2) note only, exit 0.
3. Tracker versus pin disagreement: (1) fail closed, exit 4
   [recommended]; (2) pin wins with a note.
4. Board-mode carriage: (1) text field with the exact name
   [recommended]; (2) option-id map like `priority_map`.
5. Snapshot drift display: (1) recorded profile only [recommended];
   (2) recorded plus current resolution.
6. `--model` / `--runtime` future: (1) keep as one-shot refinements
   [recommended]; (2) remove in favor of `--profile` only.
