"""A clean merge-up of the base inherits the review of the branch it merged into.

A review record is keyed to the head commit. Merging the base into a
reviewed branch makes a new head with no record, though the merge added
nothing a reviewer had to judge: the base's own commits were reviewed on
the base. Without inheritance, every merge-up strands a reviewed PR at
the merge gate.

A head inherits its first parent's record when, and only when:

* it is a two-parent merge;
* the second parent is reachable from the PR base, so what came in is
  base history, not another feature branch;
* the merge is exactly what git computes on its own: its tree equals
  ``git merge-tree --write-tree <first> <second>``, which is conflict
  free. A conflict resolved by hand (taking one side whole included,
  which leaves ``git show --cc`` empty) differs from that tree or
  conflicts. A clean merge of edits to one file at different hunks
  matches it.

Anything else (a resolution, a merge of a feature branch, an octopus, a
missing object, git older than 2.38) does not inherit and needs a fresh
review. Every git failure reads as "does not inherit", so the gate fails
closed. A parent whose record is a ``block`` is never inherited.

Takeoff and the merge path both resolve a head through ``find_record``,
so the two keep agreeing.
"""

import re
import subprocess

from corral.gitenv import clean_env

#: Merge-ups walked back through the first parents. A branch merged up
#: this many times in a row without a review in between is not a case to
#: be generous about.
MAX_DEPTH = 10

GIT_TIMEOUT_S = 30

_HEX_ID = re.compile(r"[0-9a-fA-F]+")


def _git(args, cwd):
    """The one git seam here: a CompletedProcess, or None when git cannot run."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _ok(proc):
    return proc is not None and proc.returncode == 0


def resolve_commit(commit, cwd):
    """The full SHA of ``commit`` in ``cwd``'s repository, else None.

    A hex id is read as an object prefix first, so a branch or tag named
    like a SHA cannot shadow the commit the reviewer meant; a prefix that
    matches several commits is refused (None). Anything else (``HEAD``, a
    branch) goes through ``git rev-parse --verify``.
    """
    commit = str(commit or "").strip()
    if not commit or commit.startswith("-"):
        return None

    def verify(ref):
        proc = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
        return proc.stdout.strip() if _ok(proc) else ""

    if _HEX_ID.fullmatch(commit):
        if len(commit) < 4:
            # Below git's minimum abbreviation: never an object prefix,
            # and rev-parse would read it as a branch or tag instead.
            return None
        listed = _git(["rev-parse", f"--disambiguate={commit}"], cwd)
        found = {verify(c) for c in listed.stdout.split()} - {""} if _ok(listed) else set()
        if len(found) > 1:
            return None
        if found:
            return found.pop()
    return verify(commit) or None


def clean_mergeup_parent(head, base_ref, cwd):
    """The first parent of ``head`` when it is a clean merge-up, else "".

    ``base_ref`` is any ref git resolves in ``cwd`` (``origin/master``).
    """
    if not head or not base_ref or not cwd or head.startswith("-") or base_ref.startswith("-"):
        return ""
    proc = _git(["rev-list", "--parents", "-n", "1", head, "--"], cwd)
    if not _ok(proc):
        return ""
    fields = proc.stdout.split()
    if len(fields) != 3:
        return ""
    first, second = fields[1], fields[2]
    if not _ok(_git(["merge-base", "--is-ancestor", second, base_ref], cwd)):
        return ""
    # merge-tree exits 1 on conflicts; on a clean merge its first output
    # line is the tree git itself would commit.
    merged = _git(["merge-tree", "--write-tree", "--no-messages", first, second], cwd)
    if not _ok(merged):
        return ""
    tree = _git(["rev-parse", head + "^{tree}"], cwd)
    if not _ok(tree):
        return ""
    want = merged.stdout.split("\n", 1)[0].strip()
    if not want or want != tree.stdout.strip():
        return ""
    return first


def find_record(read, head, base_ref, cwd):
    """``(record, commit)`` for ``head``, else ``(None, "")``.

    ``read(commit)`` returns one commit's record or None. The head's own
    record wins; otherwise the first parent of each clean merge-up is
    tried in turn. ``commit`` is the commit the record belongs to. An
    inherited ``block`` is no approval: it stops the walk.
    """
    commit = head
    for _ in range(MAX_DEPTH + 1):
        record = read(commit)
        if record is not None:
            if commit != head and record.get("verdict") == "block":
                break
            return record, commit
        commit = clean_mergeup_parent(commit, base_ref, cwd)
        if not commit:
            break
    return None, ""
