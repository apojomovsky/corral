"""Plan-only hygiene audit for one clone.

For every local branch the audit reports one stable category:

- `landed`: its tip is an ancestor of the base ref;
- `superseded`: its tip commit subject already sits in the base history
  under another name (a squash-merge under a different branch);
- `merged-pr`: the injected forge index reports a merged PR for it;
- `live-remote`: it still has an upstream on the remote;
- `no-upstream`: its commits live only here and nothing proves them landed;
- `unknown`: anything else.

Only the first three are safe to delete, and even those are never
deleted here: the plan prints the exact `git branch -d` command per
safe branch for a human to run. Doubt resolves to keeping.

Worktrees under the clone's `.worktrees/` root are planned for removal
when they are provably lossless: a registered worktree whose branch is
`landed` or `merged-pr`, whose tree is clean and which holds no commit
outside the base; or an unregistered directory git no longer tracks
whose content is provably empty of anything git could lose. A path
that escapes the root (after resolving symlinks) is refused, never
touched. `git worktree prune` is planned when git reports stale
entries.

Everything that deletes runs behind the required `remove` seam of
`apply` (no default); everything that reads git runs behind the
injectable `run` seam. The forge index is a required argument: the
caller passes `{branch: "merged" | "open" | "closed"}`. This module
never calls `gh` itself.
"""

import os
import subprocess
from dataclasses import dataclass, field

from corral import diffscope, worktrees
from corral.gitenv import clean_env

#: Branch verdicts, ordered from "provably resolved" to "do not touch".
LANDED = "landed"
SUPERSEDED = "superseded"
MERGED_PR = "merged-pr"
LIVE_REMOTE = "live-remote"
NO_UPSTREAM = "no-upstream"
UNKNOWN = "unknown"

#: Verdicts whose branch a human may delete.
SAFE = (LANDED, SUPERSEDED, MERGED_PR)


class GcError(RuntimeError):
    """The clone is unreachable or a removal failed (exits 3 through the CLI)."""


def git(args, cwd):
    """The one git seam: a CompletedProcess, never raised. Tests inject fakes."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=clean_env())


def _tail(proc):
    lines = (proc.stderr or "").strip().splitlines()
    return lines[-1] if lines else "git failed"


def _inside(path, parent):
    """True when real path `path` sits at or below real path `parent`."""
    return os.path.commonpath([os.path.realpath(path), parent]) == parent


@dataclass
class BranchInfo:
    name: str
    tip: str
    subject: str
    upstream: str | None
    gone: bool


@dataclass
class Audit:
    branch: BranchInfo
    category: str
    evidence: str


@dataclass
class RegisteredRow:
    path: str
    branch: str
    category: str | None
    planned: bool
    reason: str


@dataclass
class DanglingRow:
    path: str
    planned: bool
    reason: str


@dataclass
class Plan:
    repo: str
    base: str
    base_ok: bool
    root: str
    audits: list[Audit]
    registered: list[RegisteredRow] = field(default_factory=list)
    dangling: list[DanglingRow] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    prune: bool = False
    forge_note: str | None = None


@dataclass
class ApplyResult:
    pruned: bool = False
    prune_error: str | None = None
    removed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)


def classify(*, ancestor, subject_hit, pr_state, upstream, gone):
    """The pure verdict: `(category, evidence)` from plain facts, no git.

    `pr_state` is the forge index value for the branch (`"merged"`,
    `"open"`, `"closed"` or None when the index says nothing).
    """
    if ancestor:
        return LANDED, "tip is an ancestor of the base"
    if subject_hit:
        return SUPERSEDED, "tip subject already sits in the base history"
    if pr_state == "merged":
        return MERGED_PR, "a merged PR names this branch"
    if upstream is None:
        return NO_UPSTREAM, "never pushed; the only copy is local"
    if not gone:
        return LIVE_REMOTE, f"upstream {upstream} still exists"
    return UNKNOWN, "remote branch gone but no merge evidence found"


def local_branches(repo, run=git):
    """Every local branch except the current one, with tracking info."""
    fmt = "%(refname:short)\t%(objectname)\t%(subject)\t%(upstream:short)\t%(upstream:track)"
    out = []
    current = run(["branch", "--show-current"], cwd=repo).stdout.strip()
    proc = run(["branch", "--format", fmt], cwd=repo)
    if proc.returncode != 0:
        raise GcError(f"cannot list branches in {repo}: {_tail(proc)}")
    for line in proc.stdout.splitlines():
        parts = (line.split("\t") + ["", "", "", "", ""])[:5]
        name, tip, subject, upstream, track = parts
        if not name or name == current:
            continue
        out.append(
            BranchInfo(
                name=name,
                tip=tip,
                subject=subject,
                upstream=upstream or None,
                gone="gone" in track,
            )
        )
    return out


def _base_subjects(repo, base, run):
    """The set of commit subjects reachable from `base`; empty when unresolvable."""
    proc = run(["log", base, "--format=%s"], cwd=repo)
    if proc.returncode != 0:
        return set()
    return {line for line in proc.stdout.splitlines() if line}


def _is_ancestor(repo, tip, base, run):
    if not tip or not base:
        return False
    return run(["merge-base", "--is-ancestor", tip, base], cwd=repo).returncode == 0


def _ref_exists(repo, ref, run):
    proc = run(["rev-parse", "--verify", "-q", ref], cwd=repo)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def audit_branches(repo, base, *, pr_index, run=git):
    """One Audit per local branch (minus the current one).

    `pr_index` is required: `{branch: state}` from the forge seam.
    Never raises for an unresolvable base: verdicts that need it simply
    never fire (fail closed).
    """
    if run(["rev-parse", "--show-toplevel"], cwd=repo).returncode != 0:
        raise GcError(f"not a git clone: {repo}")
    base_ok = _ref_exists(repo, base, run)
    subjects = _base_subjects(repo, base, run) if base_ok else set()
    audits = []
    for branch in local_branches(repo, run):
        subject_hit = bool(branch.subject) and branch.subject in subjects
        category, evidence = classify(
            ancestor=base_ok and _is_ancestor(repo, branch.tip, base, run),
            subject_hit=subject_hit,
            pr_state=pr_index.get(branch.name),
            upstream=branch.upstream,
            gone=branch.gone,
        )
        audits.append(Audit(branch=branch, category=category, evidence=evidence))
    return audits


def registered_worktrees(repo, run=git):
    """`(path, branch)` per registered non-detached worktree."""
    proc = run(["worktree", "list", "--porcelain"], cwd=repo)
    if proc.returncode != 0:
        raise GcError(f"cannot list worktrees in {repo}: {_tail(proc)}")
    out = []
    path = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line.startswith("branch ") and path is not None:
            branch = line.split(" ", 1)[1].rsplit("refs/heads/", 1)[-1]
            out.append((path, branch))
            path = None
    return out


def _status_clean(path, run):
    """True when clean, False when dirty, None when git cannot tell."""
    proc = run(["status", "--porcelain"], cwd=path)
    if proc.returncode != 0:
        return None
    return not proc.stdout.strip()


def _outside_count(repo, ref, base, run):
    """Commits `ref` holds outside `base`; None when uncountable (keep)."""
    proc = run(["rev-list", "--count", f"{base}..{ref}"], cwd=repo)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _scan_root(root_real, known):
    """`(dangling, refused)` under the worktree root.

    `known` holds real paths git still registers. A directory that is
    an ancestor of a registered path (a leftover parent) is not
    dangling. A path escaping the root (after resolving symlinks) is
    refused. Plain files are not worktree remains and are skipped.
    """
    dangling, refused = [], []
    if not os.path.isdir(root_real):
        return dangling, refused
    for entry in sorted(os.listdir(root_real)):
        full = os.path.join(root_real, entry)
        if not os.path.lexists(full):
            continue
        if not os.path.isdir(full) and not os.path.islink(full):
            continue
        real = os.path.realpath(full)
        if os.path.commonpath([real, root_real]) != root_real:
            refused.append(full)
            continue
        if real in known or any(k.startswith(real + os.sep) for k in known):
            continue
        dangling.append(full)
    return dangling, refused


def prune_needed(repo, run=git):
    """True when git reports stale worktree entries (a dry run, read-only)."""
    proc = run(["worktree", "prune", "-n", "-v"], cwd=repo)
    if proc.returncode != 0:
        return False
    # git reports what it would prune on stderr, not stdout.
    return bool((proc.stdout or "").strip() or (proc.stderr or "").strip())


def _registered_row(repo, base, path, branch, categories, run):
    """The plan row for one registered worktree (read-only)."""
    category = categories.get(branch)
    if category not in (LANDED, MERGED_PR):
        return RegisteredRow(path, branch, category, False, f"branch is {category or 'unknown'}")
    clean = _status_clean(path, run)
    if clean is None:
        return RegisteredRow(path, branch, category, False, "cannot read tree state")
    if not clean:
        return RegisteredRow(path, branch, category, False, "tree is dirty")
    outside = _outside_count(repo, branch, base, run)
    if outside is None:
        return RegisteredRow(path, branch, category, False, "cannot count commits outside base")
    if outside:
        return RegisteredRow(
            path, branch, category, False, f"{outside} unpushed commit(s) outside the base"
        )
    return RegisteredRow(
        path, branch, category, True, f"branch {category}, clean, nothing outside {base}"
    )


def _dangling_row(repo, base, path, run):
    """The plan row for one unregistered directory (read-only).

    Removable only with proof: an empty directory, a lone dangling
    `.git` pointer with nothing beside it, or a still-valid checkout
    that is clean and holds nothing outside the base. Anything else is
    kept: untracked content git cannot account for.
    """
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return DanglingRow(path, False, "cannot read directory")
    if not entries:
        return DanglingRow(path, True, "empty directory")
    clean = _status_clean(path, run)
    if clean is not None:
        if not clean:
            return DanglingRow(path, False, "tree is dirty")
        head = run(["rev-parse", "--verify", "-q", "HEAD"], cwd=path)
        if head.returncode == 0 and head.stdout.strip():
            outside = _outside_count(path, "HEAD", base, run)
            if outside == 0:
                return DanglingRow(path, True, f"clean, nothing outside {base}")
            return DanglingRow(path, False, "holds commits outside the base")
        return DanglingRow(path, False, "cannot resolve HEAD")
    gitfile = os.path.join(path, ".git")
    if entries == [".git"] and os.path.isfile(gitfile) and not os.path.islink(gitfile):
        try:
            with open(gitfile, encoding="utf-8") as handle:
                line = handle.read().strip()
        except OSError:
            return DanglingRow(path, False, "cannot read .git pointer")
        if line.startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(path, target))
            if not os.path.exists(target):
                return DanglingRow(path, True, "dangling .git pointer, no other content")
    return DanglingRow(path, False, "unverified content: git cannot account for it")


def plan(repo, *, pr_index, root, base=None, run=git, forge_note=None):
    """The full audit for one clone. Reads only; never deletes.

    `pr_index` (the forge seam) and `root` (the `git.worktrees`
    setting value, resolved to a real path through `corral.worktrees`)
    are required. `base` defaults to `diffscope.default_base()`: no
    second base resolver lives here.
    """
    base = base or diffscope.default_base()
    main_proc = run(["rev-parse", "--show-toplevel"], cwd=repo)
    if main_proc.returncode != 0:
        raise GcError(f"not a git clone: {repo}")
    main = os.path.realpath(main_proc.stdout.strip())
    real_root = worktrees.root(main, root)
    audits = audit_branches(main, base, pr_index=pr_index, run=run)
    categories = {a.branch.name: a.category for a in audits}
    known = {os.path.realpath(path) for path, _ in registered_worktrees(main, run=run)}
    rows = []
    for path, branch in registered_worktrees(main, run=run):
        if os.path.realpath(path) == main:
            rows.append(RegisteredRow(path, branch, None, False, "main checkout"))
            continue
        if os.path.commonpath([os.path.realpath(path), real_root]) != real_root:
            rows.append(
                RegisteredRow(
                    path, branch, categories.get(branch), False, "outside the worktree root"
                )
            )
            continue
        rows.append(_registered_row(main, base, path, branch, categories, run))
    dangling_paths, refused = _scan_root(real_root, known)
    return Plan(
        repo=main,
        base=base,
        base_ok=_ref_exists(main, base, run),
        root=real_root,
        audits=audits,
        registered=rows,
        dangling=[_dangling_row(main, base, path, run) for path in dangling_paths],
        refused=refused,
        prune=prune_needed(main, run=run),
        forge_note=forge_note,
    )


def actionable(plan):
    """True when the plan holds anything worth doing or saying."""
    return (
        any(a.category in SAFE for a in plan.audits)
        or any(r.planned for r in plan.registered)
        or any(d.planned for d in plan.dangling)
        or plan.prune
    )


def format_plan(plan):
    """The human-readable plan: categories, worktrees, prune, notes."""
    lines = []
    if plan.forge_note:
        lines.append(f"note: {plan.forge_note}")
    if not plan.base_ok:
        lines.append(
            f"note: base ref {plan.base} does not resolve here: landed and superseded never fire"
        )
    by_cat: dict[str, list[Audit]] = {}
    for audit in plan.audits:
        by_cat.setdefault(audit.category, []).append(audit)
    order = [LANDED, SUPERSEDED, MERGED_PR, LIVE_REMOTE, NO_UPSTREAM, UNKNOWN]
    labels = {
        LANDED: "landed, safe to delete",
        SUPERSEDED: "tip already landed under another name, safe to delete",
        MERGED_PR: "merged PR found, safe to delete",
        LIVE_REMOTE: "remote branch still exists, DO NOT DELETE",
        NO_UPSTREAM: "never pushed anywhere, DO NOT DELETE without inspecting",
        UNKNOWN: "no evidence either way, needs manual review",
    }
    for cat in order:
        group = by_cat.get(cat)
        if not group:
            continue
        lines.append(f"{labels[cat]}:")
        for audit in sorted(group, key=lambda a: a.branch.name):
            lines.append(f"  {audit.branch.name}  ({audit.evidence})")
            if cat in SAFE:
                lines.append(f"    git branch -d {audit.branch.name}")
        lines.append("")
    if plan.registered:
        lines.append("registered worktrees:")
        for row in plan.registered:
            state = "planned for removal" if row.planned else f"kept ({row.reason})"
            lines.append(f"  {row.path}  ({row.branch})  {state}")
        lines.append("")
    if plan.dangling:
        lines.append("unregistered directories under the worktree root:")
        for row in plan.dangling:
            state = "planned for removal" if row.planned else f"kept ({row.reason})"
            lines.append(f"  {row.path}  {state}")
        lines.append("")
    if plan.refused:
        lines.append("refused (escape the worktree root, never touched):")
        for path in plan.refused:
            lines.append(f"  {path}")
        lines.append("")
    if plan.prune:
        lines.append("stale worktree entries: git worktree prune (runs under --apply)")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def plan_dict(plan):
    """The machine-readable plan."""
    return {
        "repo": plan.repo,
        "base": plan.base,
        "base_ok": plan.base_ok,
        "root": plan.root,
        "forge_note": plan.forge_note,
        "branches": [
            {
                "name": a.branch.name,
                "category": a.category,
                "evidence": a.evidence,
                "delete_command": f"git branch -d {a.branch.name}" if a.category in SAFE else None,
            }
            for a in sorted(plan.audits, key=lambda a: a.branch.name)
        ],
        "worktrees": [
            {
                "path": r.path,
                "branch": r.branch,
                "category": r.category,
                "planned": r.planned,
                "reason": r.reason,
            }
            for r in plan.registered
        ],
        "dangling": [
            {"path": d.path, "planned": d.planned, "reason": d.reason} for d in plan.dangling
        ],
        "refused": list(plan.refused),
        "prune": plan.prune,
    }


def apply(repo, *, pr_index, root, remove, base=None, run=git, forge_note=None):
    """Plan, then perform only the safe bookkeeping: prune and removals.

    `remove(path)` is the required destruction seam (no default): it
    deletes one planned path and raises on failure. Every candidate is
    re-verified just before removal: inside the declared root
    (symlinks resolved, escapes refused), clean tree, no commits
    outside the base, and for a registered worktree a `landed` or
    `merged-pr` branch. Returns `(plan, result)`.
    """
    checked = plan(repo, pr_index=pr_index, root=root, base=base, run=run, forge_note=forge_note)
    result = ApplyResult()
    if checked.prune:
        proc = run(["worktree", "prune"], cwd=checked.repo)
        if proc.returncode == 0:
            result.pruned = True
        else:
            result.prune_error = _tail(proc)
    for row in checked.registered:
        if not row.planned:
            continue
        fresh = _registered_row(
            checked.repo,
            checked.base,
            row.path,
            row.branch,
            {a.branch.name: a.category for a in checked.audits},
            run,
        )
        if not fresh.planned:
            result.kept.append((row.path, fresh.reason))
            continue
        try:
            remove(row.path)
        except Exception as err:  # noqa: BLE001 - one failure must not stop the rest
            result.kept.append((row.path, str(err) or "removal failed"))
        else:
            result.removed.append(row.path)
    for row in checked.dangling:
        if not row.planned:
            continue
        if os.path.commonpath([os.path.realpath(row.path), checked.root]) != checked.root:
            result.kept.append((row.path, "escapes the worktree root"))
            continue
        fresh = _dangling_row(checked.repo, checked.base, row.path, run)
        if not fresh.planned:
            result.kept.append((row.path, fresh.reason))
            continue
        try:
            remove(row.path)
        except Exception as err:  # noqa: BLE001 - one failure must not stop the rest
            result.kept.append((row.path, str(err) or "removal failed"))
        else:
            result.removed.append(row.path)
    for path in checked.refused:
        result.kept.append((path, "escapes the worktree root"))
    return checked, result
