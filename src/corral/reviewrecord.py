"""Review records: proof that a separate reviewer looked at a change.

The review gate says a change is reviewed before it lands. A ``## Review``
paragraph is free text anyone can type, so it proves nothing. The record
makes the review a machine fact instead of a promise:

* A review runs as its own process, against a diff, and writes a record
  under the ticket's session dir (``<sessions>/<repo>-<n>/reviews/``).
* The record names the reviewer (its agent id, its own session), the
  commit it read and its verdict. It is written once and never edited in
  place: a re-review after a fix writes a new record against the new
  commit.
* Takeoff and the merge path read the same record, so one proof serves
  both gates.

The record is small and boring on purpose: JSON, one file per reviewed
commit, under the host's session root, written by the reviewer process
itself. It holds no verdict the reviewer did not state.
"""

import hashlib
import json
import os
import time

from corral import paths

#: Where review records live under a ticket's session dir.
REVIEWS_DIRNAME = "reviews"

#: The verdicts a reviewer may state.
VERDICTS = ("approve", "approve-with-findings", "block")

#: The record file name prefix.
RECORD_PREFIX = "review-"

_HEX = frozenset("0123456789abcdef")


def reviews_dir(repo, number, sessions_root=None):
    """The reviews dir of one ticket."""
    return os.path.join(paths.ticket_session_dir(repo, number, root=sessions_root), REVIEWS_DIRNAME)


def commit_id(commit):
    """A short, stable id for a commit: its first 12 hex digits."""
    if isinstance(commit, dict):
        commit = commit.get("sha") or commit.get("id") or ""
    commit = str(commit or "").strip().lower()
    if not commit:
        return ""
    if len(commit) >= 12 and set(commit) <= _HEX:
        return commit[:12]
    return hashlib.sha256(commit.encode("utf-8")).hexdigest()[:12]


def record_path(repo, number, commit, sessions_root=None):
    """The record file for one commit of one ticket."""
    name = f"{RECORD_PREFIX}{commit_id(commit)}.json"
    return os.path.join(reviews_dir(repo, number, sessions_root), name)


def write_record(
    repo,
    number,
    commit,
    reviewer,
    verdict,
    findings=None,
    question="",
    session="",
    sessions_root=None,
    now=None,
    author="",
):
    """Write one review record; returns the path.

    ``reviewer`` is the reviewing agent's id, ``verdict`` one of
    :data:`VERDICTS`, ``findings`` the findings it stated (each a dict
    with at least a severity). ``author`` is the agent that wrote the
    change; a record naming the author as its own reviewer is refused.
    Writing the same commit again is allowed only with the same verdict,
    so a reviewer re-stating itself loses nothing, while a changed
    verdict on a reviewed commit is refused: that needs a new commit and
    a new record. Raises ValueError.
    """
    verdict = str(verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"review verdict must be one of {VERDICTS}, got {verdict!r}")
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("a review record needs the reviewer's agent id")
    author = str(author or "").strip()
    if author and author == reviewer:
        raise ValueError(f"{repo} {number}: the author ({author}) cannot be its own reviewer")
    if not commit_id(commit):
        raise ValueError("a review record needs the reviewed commit")
    path = record_path(repo, number, commit, sessions_root)
    if os.path.exists(path):
        existing = _load(path) or {}
        if existing.get("verdict") != verdict:
            raise ValueError(
                f"{repo} {number} commit {commit_id(commit)} was already reviewed as "
                f"{existing.get('verdict')!r}; a changed verdict needs a new commit"
            )
    payload = {
        "repo": repo,
        "number": int(number),
        "commit": commit_id(commit),
        "reviewer": reviewer,
        "verdict": verdict,
        "findings": list(findings or []),
        "question": str(question or ""),
        "session": str(session or ""),
        "at": float(now if now is not None else time.time()),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    paths.atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")
    return path


def _load(path):
    """The record at ``path`` when it is a valid one, else None."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("verdict") not in VERDICTS:
        return None
    if not payload.get("reviewer"):
        return None
    return payload


def read_record(repo, number, commit, sessions_root=None):
    """The record for one commit, or None."""
    return _load(record_path(repo, number, commit, sessions_root))


def records(repo, number, sessions_root=None):
    """Every valid record of a ticket, newest first."""
    directory = reviews_dir(repo, number, sessions_root)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith(RECORD_PREFIX) and name.endswith(".json"):
            payload = _load(os.path.join(directory, name))
            if payload is not None:
                out.append(payload)
    out.sort(key=lambda p: p.get("at", 0), reverse=True)
    return out


def latest_record(repo, number, sessions_root=None):
    """The newest record of a ticket, or None."""
    found = records(repo, number, sessions_root)
    return found[0] if found else None
