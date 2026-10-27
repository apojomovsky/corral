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

Records are plain files, not signatures: the reviewer and author ids are
what the processes said they were. Separation is as strong as the
process separation on the host, which is what the review gate asks for.
"""

import hashlib
import json
import os
import tempfile
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


def _full(commit):
    """The lowercase commit text a record stores and compares against."""
    if isinstance(commit, dict):
        commit = commit.get("sha") or commit.get("id") or ""
    return str(commit or "").strip().lower()


def write_record(
    repo,
    number,
    commit,
    reviewer,
    verdict,
    author,
    findings=None,
    question="",
    session="",
    sessions_root=None,
    now=None,
):
    """Write one review record; returns the path. Raises ValueError.

    ``reviewer`` is the reviewing agent's id, ``author`` the agent that
    wrote the change: both are required, and a record naming the author
    as its own reviewer is refused. ``verdict`` is one of
    :data:`VERDICTS`, ``findings`` the findings it stated (each a dict
    with at least a severity).

    A commit is reviewed once. The file is created exclusively, so the
    first record wins even when two reviewers race: a later write is a
    no-op when it repeats the same reviewer and verdict (a retried
    reviewer loses nothing), and refused otherwise.
    """
    verdict = str(verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"review verdict must be one of {VERDICTS}, got {verdict!r}")
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("a review record needs the reviewer's agent id")
    author = str(author or "").strip()
    if not author:
        raise ValueError("a review record needs the change's author, to refuse a self-review")
    if author == reviewer:
        raise ValueError(f"{repo} {number}: the author ({author}) cannot be its own reviewer")
    if not commit_id(commit):
        raise ValueError("a review record needs the reviewed commit")
    path = record_path(repo, number, commit, sessions_root)
    payload = {
        "repo": repo,
        "number": int(number),
        "commit": _full(commit),
        "reviewer": reviewer,
        "author": author,
        "verdict": verdict,
        "findings": list(findings or []),
        "question": str(question or ""),
        "session": str(session or ""),
        "at": float(now if now is not None else time.time()),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".review.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # link() fails when the name exists: a create-exclusive publish
        # of a file that is already complete.
        os.link(tmp, path)
    except FileExistsError:
        existing = _load(path) or {}
        if existing.get("reviewer") == reviewer and existing.get("verdict") == verdict:
            return path
        raise ValueError(
            f"{repo} {number} commit {commit_id(commit)} was already reviewed "
            f"({existing.get('verdict')!r} by {existing.get('reviewer')!r}); "
            "a new review needs a new commit"
        ) from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
    """The record for exactly this commit, or None.

    File names carry a 12-digit prefix; the stored commit must also be
    the one asked for, so two commits sharing a prefix never share a
    record.
    """
    payload = _load(record_path(repo, number, commit, sessions_root))
    if payload is None or payload.get("commit") != _full(commit):
        return None
    return payload


def approves(record):
    """Whether a record lets a change through: approve, with or without findings."""
    return record is not None and record.get("verdict") in ("approve", "approve-with-findings")
