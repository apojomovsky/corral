# 06: `corral new`, for a project that does not exist yet

Status: design. Refines [`01-onboarding.md`](01-onboarding.md), section 5, which
sketches `new` in six lines. This doc decides the contract: what `new` creates,
how templates work, how the remote side stays safe, how labels are bootstrapped,
how re-runs converge, how `new` interacts with `init`, `uninit`, `takeoff` and
`doctor`, how it is tested, and which implementation tickets follow, in which
order.

`corral init` onboards a clone that already exists. `corral new` creates one:
it scaffolds a minimal repository on disk, then onboards it through the existing
`init` writer, so the rest of the path (`doctor`, `launch`, `takeoff`) is the
same as section 3 of `01-onboarding.md`. There is exactly one writer of
`corral.toml` (`init.render` and its apply path); `new` never renders or edits
that file itself.

## 1. What `corral new <name>` creates

Synopsis (flags mirror `init` where they mean the same thing):

```bash
corral new <name> [--path DIR] [--template NAME] [--template-dir DIR]
  [--ops [PATH]] [--ops-name NAME] [--create-remote] [--public]
  [--resume] [--plan] [--force]
  [--set key=value] [--runtime R] [--cap N] [--test-cmd CMD]
```

`<name>` is the project and repository name. It must satisfy the one rule
every repo-named path already shares (`paths.plain_repo_name`): one path part,
no `.git` suffix. Anything else is exit 4. The target directory defaults to
`<cwd>/<name>`; `--path DIR` names the parent explicitly. A target that exists,
is non-empty, and is not a tree `new` can prove it created (section 5) is exit
4: `new` never adopts an unrelated tree.

Steps, in order (each is re-runnable; section 5):

1. **Remote pre-check, only with `--create-remote`.** Resolve the intended
   `owner/name` and ask the forge whether it exists yet. When it exists, stop
   with exit 4 before writing anything locally, so a refusal leaves nothing
   behind. Section 3 decides the exact rules.
2. **Create and initialise the tree.** Create the directory, `git init -b`
   with the initial branch (`--default-branch X`, else `main`), and copy the
   template files (section 2). Template files are created only when missing;
   an existing identical file is kept, an existing different scaffold file is
   left alone and reported (scaffold files belong to the user after the first
   run, exactly like `agents.overlay.md`).
3. **Onboard through `init`.** Call `init.collect` (detection plus the
   `--set`/`--runtime`/`--cap`/`--test-cmd` overrides, unchanged) and then the
   same apply path `init` uses: `corral.toml`, `agents.overlay.md`
   (created only when missing), hooks (converged per file, same foreign-hook
   rules), the clone-local `core.hooksPath`, the `.git/info/exclude` line in
   the split layout, and the host registration. No second `corral.toml`
   renderer, no second hook converger: `new` passes different inputs to the
   same functions. `init` may need a small refactor so its apply entry point
   is callable in-process; the renderer stays one function.
4. **One initial commit.** `chore: scaffold the <name> project` (or the
   template's name substitution), containing the scaffold plus everything
   `init` wrote. Agents require clean trees and `takeoff` refuses dirty ones,
   so the first agent must start from a committed base. The commit goes
   through the installed `commit-msg` hook like any other commit.
5. **Remote and labels, only when asked.** Without `--create-remote`, `new`
   prints the outward commands (`gh repo create ...`, then
   `corral github setup`) and stops, exactly like `init` prints
   `corral github setup` and never runs anything outward itself. With
   `--create-remote`, `new` creates the repository through the forge
   interface, sets it as `origin`, and runs the label bootstrap (section 4).
   `new` never pushes: the operator reviews the tree, then runs
   `corral push`.
6. **Report the next step.** On success, print `next: corral doctor` (plus the
   outstanding outward commands when they were not run).

`--plan` prints the full diff of every file `new` would write, plus every
outward command it would run, and writes nothing: same contract as
`init --plan`. `--force` means what it means to `init` (replace a differing
`corral.toml` or registration) and nothing more: it never overwrites scaffold
files.

### The skeleton

The `blank` template (the default) writes:

- `README.md`: one line of what the project is (`{{name}}` substituted), plus
  build, test and contribute sections. A few lines; the project grows it.
- `AGENTS.md`: how to build, test and contribute to this code. Short and
  generic. It never mentions corral, the ops side, or anything private: the
  corral-only rules live in `agents.overlay.md`, which `init` writes next to
  `corral.toml` (see `01-onboarding.md`, section 3a).
- `.gitignore`: the worktree root (`.worktrees/`), editor junk, and per-type
  artifacts for richer templates.
- `Makefile`: `test`, `check` and `setup-hooks` targets. `check` runs the
  test target plus the hook-installed check, so CI and `takeoff` run the same
  commands. The scaffold's `test` target passes trivially on day one (it runs
  the template's own suite, which starts with a placeholder test the project
  replaces).
- `.github/workflows/check.yml`: runs `make check` on push and pull request.
  Minimal on purpose: it pins no versions beyond what the template needs.

`python-uv` adds corral's own layout on top of `blank`: a `pyproject.toml`
(uv, stdlib only, `unittest` discovery), a `tests/` directory with one
placeholder test, and a Makefile whose `test` target runs the suite the way
`detect.test_command` will find it (`uv run python -m unittest`). Because the
template mirrors what detection expects, `init.collect` on a `python-uv` tree
detects the test command with no `--test-cmd` flag.

### `--ops`

`new --ops` creates the pair: the project clone as above, plus the ops root
through `init.ensure_ops_root` (bare `--ops` uses the `<name>_ops` sibling,
`--ops-name NAME` overrides it, same defaults as `init`). The agent side then
follows the split path exactly: the `[[repo]]` entry is appended to the ops
`corral.toml`, hooks live under `<ops>/hooks/<name>/`, the clone is touched
only through `core.hooksPath` and `.git/info/exclude`. The inline layout is
the default because a brand-new project has no visibility question yet (no
remote, no contributors); `--ops` is for the operator who already knows the
project needs the split (private work behind a shared repo, several repos
under one ops root). The ops root's visibility default follows
`01-onboarding.md` (tickets live in the ops repository for a private project).

A fresh tree has no remote, so `init.collect` detects the default branch from
the current branch (`main` from step 2) and `detect.origin_slug` finds no
slug; the `[[repo]]` name is the directory name, which equals `<name>`. The
tracker repository stays unset until a remote exists; `doctor` warns (not
fails) while there is no remote to check (section 6).

## 2. Templates

A template is a **declared choice**, not a function of detection. Detection
reads a repository that already has content; `new` runs on an empty
directory, where there is nothing to detect. `--template NAME` picks a
packaged template (`blank` by default, `python-uv` for the uv layout).
An unknown name is exit 4 listing the known names.

Packaged templates live under `src/corral/templates/<name>/` and are read
with `importlib.resources`, the same mechanism as the hook scripts, the
overlay template and the prompt templates. One helper lists them
(`templates.names()`), one helper renders them; every reader goes through
those two, per one-fact-one-place. The file set is the template directory
recursively, minus nothing: what the author puts there is what lands.

Substitution is minimal and strict: files may contain `{{name}}`, replaced
with the project name. Any other `{{...}}` placeholder is exit 4 naming the
file and the placeholder (strict like prompt rendering: a silent passthrough
would ship a broken scaffold). Binary files are copied byte-identical;
substitution applies to UTF-8-decodable files only.

Templates stay generic by construction, and a test enforces it: rendering
every packaged template with a fixture name must produce no host name, no
home path, no account name, and no private term beyond the substituted name.
Packaged templates contain only stand-ins (`example.com` where a URL is
needed). Anything project-specific belongs in a user template.

A user supplies their own template with `--template-dir DIR`: the directory
is copied like a packaged one, with the same `{{name}}` rule and the same
strictness. `--template` and `--template-dir` are mutually exclusive
(exit 4). A missing or unreadable directory is exit 4. The ops-side analogue
(prompts eject, overlay editing) stays as is: templates cover the project
side only.

## 3. The forge side

Creating the remote repository is an outward, approval-gated action, so `new`
is plan-only by default: without `--create-remote` it prints the exact
`gh repo create` command it would run and runs nothing outward, mirroring how
`init` prints `corral github setup` instead of running it. The flag is the
approval: passing `--create-remote` in a headless session is an explicit
operator decision, and the default path is inert.

With `--create-remote`, creation goes through the forge interface
(`corral/forge.py`), implemented once by the GitHub adapter; nothing in `new`
calls `gh` or knows GitHub's URL shapes, per the design's section 2. The
operation takes the injected forge object, so tests use a fake at the same
seam as every other forge test.

Rules, with reasons:

- **Private by default.** R6 (the project may be private) plus the nothing
  personal rule: a public repository is forever, a private one can be opened
  later. `--create-remote` creates a private repository; `--public` makes it
  public, and `--public` without `--create-remote` is exit 4 (it would mean
  nothing).
- **Refuse when the repo exists.** The pre-check in step 1 reads the intended
  `owner/name` through the forge; when it exists, exit 4 naming it, before
  any local write. Overwriting or adopting a foreign repository would violate
  proof-before-removal (we cannot prove its content is ours or disposable).
- **Race is exit 2.** When the repository appears between the pre-check and
  the create call, the create fails with the provider's already-exists
  answer; `new` maps that to exit 2 (race lost), the same code `launch` uses
  when a ticket is already live. A re-run then converges or refuses per
  section 5, never retries blindly.
- **Unreachable is exit 3.** A forge or network failure is exit 3 (remote or
  service unreachable), never a partial success claim: files written locally
  are reported as written, the remote as not created, and the printed next
  step is the manual command.
- **Visibility is set once, at creation.** There is no flag to flip a remote
  public later; that stays a deliberate manual action outside corral.

## 4. Labels and board bootstrap

The board needs a fixed label set before the first agent runs. `new` does not
invent its own set: it bootstraps exactly the labels the rest of corral reads:

- `ready` (what `board.ready` defaults to; the eligibility label);
- `needs-triage`, `needs-decision` (the triage and decision flow);
- `hold` (blocks `resume` until a human removes it);
- `priority:urgent`, `priority:high`, `priority:normal`, `priority:low`,
  `priority:backlog` (the ranking `github/tracker.py` already parses; unknown
  `priority:*` suffixes are ignored, so this closed set is the whole
  vocabulary).

`repo:<name>` labels are not bootstrapped: they are per-repository and
unbounded, so triage creates them when a multi-repo ops root first needs one.
`agent:*` and `status:*` labels are written by corral itself on first use and
need no bootstrap either.

Creation goes through the tracker interface (`corral/tracker.py`), via one
idempotent ensure operation implemented once in the GitHub adapter (create
missing labels through `gh`; provider specifics such as color codes stay in
the adapter, the interface takes name plus description). Idempotent means:
missing labels are created, existing labels are left byte-identical (never
overwrite a human-edited color or description), and the report lists
`created`, `kept`, and `refused` separately. Refused labels (R5: in someone
else's organisation corral may not be allowed to create labels) are exit 1
with a fix line per label (ask an owner, or move the board to the ops side),
not fatal to the local scaffold.

One code path owns this: `corral github setup`, the command `init` already
prints as its next step, performs the ensure operation (plus any future
board setup). `new --create-remote` invokes the same operation after creating
the repository; without `--create-remote`, `new` prints
`corral github setup` for the operator to run, exactly like `init` does.
Labels are therefore never created by a second implementation, and never as a
side effect of a command whose name does not say so.

## 5. Idempotence and failure

Every step is safe to re-run, like `init`:

- Scaffold files are create-only (overlay rule): missing files are written,
  identical files are kept, differing files are left alone and reported.
  `--force` does not touch them.
- `corral.toml`, hooks, the exclude line and the registration converge
  through the `init` apply path, so a re-run that changes nothing exits 1
  (nothing to do), mirroring `init`'s exit codes.
- The initial commit runs only when the tree differs from `HEAD`; a clean
  re-run commits nothing.

A partial failure leaves the tree; the next run converges: each step checks
its own precondition (directory exists, git repo present, file missing or
identical, entry present, commit needed, remote exists) and skips what is
already true. No ledger of progress is kept on disk; the tree itself is the
state, so there is nothing stale to reconcile.

`--resume` exists for exactly one case: an interrupted `--create-remote` run
where the remote now exists. Plain re-run with `--create-remote` refuses when
the remote exists (section 3). With `--resume`, `new` adopts the remote only
when it is empty (no refs: nothing committed, nothing to lose, so the proof
is trivial) and continues with setting `origin` and the label bootstrap. A
non-empty remote is exit 4 even with `--resume`: its content cannot be proven
ours. Local scaffold convergence never needs `--resume`.

Exit codes (public interface, per `AGENTS.md`):

| Code | Meaning for `new` |
| --- | --- |
| 0 | success: something was written, created or bootstrapped |
| 1 | nothing to do (converged re-run), or a gate said no (a label refused) |
| 2 | race lost (the remote appeared between pre-check and create) |
| 3 | the forge, tracker or host is unreachable |
| 4 | usage or configuration error (bad name, non-empty target, remote exists, bad template or placeholder, `--public` without `--create-remote`) |
| 75 | never used by `new` (it consumes no admission slot) |

## 6. Interaction with `init`, `uninit`, `takeoff`, `doctor`

- **`init`.** `new` is a caller of `init`, not a sibling: same `collect`
  inputs, same renderer, same apply path, same flags with the same meanings.
  `init` on a `new`-created tree behaves like a re-run (exit 1 when
  converged). `init` needs no behaviour change for `new` beyond exposing its
  apply entry point in-process.
- **`uninit`.** Unchanged. It removes the hooks and the registration it can
  prove it wrote, and keeps `corral.toml`, the overlay and the ops entry.
  Scaffold files (`README.md`, `AGENTS.md`, `Makefile`, CI, template sources)
  are never removed: they may hold the project's only copy of new work, and
  deleting them would violate proof-before-removal. Tearing down a
  `new`-created project is `uninit` plus deleting the directories by hand.
- **`takeoff`.** Unchanged. The scaffold exists so `takeoff` passes on day
  one: the tree starts committed, the `check` target runs the suite, and the
  overlay carries the leak-guard terms. No takeoff gate special-cases a fresh
  project.
- **`doctor`.** No new check kinds beyond the `01-onboarding.md` section 6
  list; a `new`-created project must pass that list. Two nuances for fresh
  trees: while no remote is set, the remote-dependent rows (default branch on
  the remote, labels exist) warn instead of failing, with the fix
  (`gh repo create` / `corral github setup` / re-run with `--create-remote`);
  once a remote exists the rows are strict. `doctor --fix` stays within its
  existing bounds (local, safe, reversible); creating a remote or labels is
  outward and therefore printed, never applied.

## 7. Test plan

All tests run against fixture trees in temporary directories, with fakes at
the existing seams only: the `FakeGit` harness from `tests/test_init.py`
(reused, not redefined), a fake forge implementing `corral/forge.py`, a fake
tracker implementing `corral/tracker.py`, and a fake `gh` runner for the
adapter-level label tests. No test calls a real `gh`, the network, systemd,
or the real state and session dirs; `tests/suiteguard.py` keeps enforcing
that. Golden files cover each packaged template's full render.

Cases:

- each packaged template renders byte-identical goldens with a fixture name;
- re-running `new` on its own output exits 1 and changes no byte;
- `--plan` writes nothing (the tree is byte-identical before and after);
- `--template-dir` copies a user directory with `{{name}}` substitution;
  an unknown `{{...}}` placeholder is exit 4 naming file and placeholder;
- an unknown `--template` name is exit 4 listing the known names;
- a bad `<name>` (multi-part, `.git` suffix) is exit 4;
- an existing non-empty target that is not ours is exit 4 and untouched;
- `init` on a `new` tree converges (exit 1, no diff);
- `uninit` removes hooks and registration, keeps `corral.toml`, the overlay
  and every scaffold file;
- `--create-remote` with a fake forge creates private by default; `--public`
  without `--create-remote` is exit 4;
- the pre-check refuses an existing remote with exit 4 before any local
  write (target still absent afterwards);
- a race between pre-check and create maps to exit 2;
- `--resume` adopts an empty remote and continues to labels; a non-empty
  remote is exit 4;
- label ensure creates missing labels, leaves existing ones byte-identical,
  and reports refused ones with exit 1;
- template genericness: every packaged render contains no host name, home
  path, account name or private term beyond the substituted fixture name.

## 8. Implementation tickets, in order

Each ticket is one agent of work, with its acceptance criteria. Later tickets
build on earlier ones; nothing lands without the tests in section 7 that
cover it.

1. **Scaffold plus `--plan` (local only, no `init` wiring yet).** Template
   helpers (`names`, render with strict `{{name}}`), the `blank` and
   `python-uv` packaged templates with goldens, `corral new` argument parsing,
   target validation via `paths.plain_repo_name`, `git init -b`, create-only
   file writes, `--plan` output, exit 0/1/4 for the local cases. Acceptance:
   golden tests for both templates, `--plan` writes nothing, re-run exits 1.
2. **Onboard through the `init` writer.** Expose `init`'s apply path
   in-process, pass `collect` inputs through (including `--set`,
   `--runtime`, `--cap`, `--test-cmd`, `--ops`/`--ops-name` via
   `ensure_ops_root`), the initial commit, and the next-step report.
   Acceptance: `init` on a `new` tree converges, `uninit` keeps scaffold
   files, split-layout `new --ops` writes only local git state into the
   clone.
3. **`--create-remote` through the forge interface.** The create operation on
   the forge interface plus the GitHub adapter implementation, private by
   default, pre-check refusal (exit 4, nothing written), race mapping
   (exit 2), unreachable mapping (exit 3), `--resume` adoption of an empty
   remote only. Acceptance: fake-forge tests for every row of the exit table.
4. **Label bootstrap and `corral github setup`.** The idempotent ensure
   operation on the tracker interface plus the adapter, the `github setup`
   verb, `new` printing it by default and invoking it after `--create-remote`,
   refused-label reporting (exit 1 with fix lines). Acceptance: ensure is
   idempotent against a fake tracker, existing labels untouched, R5 refusal
   path tested.
5. **Doctor nuances, `--template-dir`, docs.** The warn-not-fail rows while
   no remote is set, user template directories, the `docs/README.md` row for
   the built manual (this doc's follow-up), and README freshness for the new
   verb. Acceptance: doctor tests for the pre-remote warnings, template-dir
   tests, Commands table row for `corral new`.
