"""The GitHub forge adapter: pull requests through the REST API.

The only module besides ``corral.github.gh`` that knows GitHub's URL
and response shapes. The ``gh`` runner is injected (``run(args)`` returns
stdout, raises on failure); production passes ``corral.github.gh``'s
runner, tests pass a fake. Nothing here imports ``gh``: the seam stays
a plain callable so a fake needs no subprocess.

Reads use the REST API only. The host's GraphQL budget is shared with
other tools and is not spent on reads the REST budget serves.
"""

import json
import re

from corral import forge

#: One page of the PR list; the maximum the endpoint serves.
PER_PAGE = 100

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[^/\s]+/[^/\s]+")
_SCP = re.compile(r"^(?:[^@/:]+@)?github\.com:(.+)$", re.IGNORECASE)
_URL = re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([^/]+)/(.+)$", re.IGNORECASE)


def _shape(text):
    return forge.ForgeError(f"unusable forge answer: {text}")


def _merged(payload):
    """Whether a PR object (single or list item) is merged."""
    if not isinstance(payload, dict):
        return False
    if payload.get("merged") is True:
        return True
    return bool(payload.get("merged_at"))


def _pr_state(payload):
    """``"open"`` | ``"merged"`` | ``"closed"`` for a PR object."""
    if _merged(payload):
        return forge.MERGED
    state = payload.get("state")
    if state == "open":
        return forge.OPEN
    if state == "closed":
        return forge.CLOSED
    raise _shape(f"unknown PR state {state!r}")


def _sha(value, field):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _shape(f"bad {field} {value!r}")
    return value


def _parse_pull(payload, number):
    """A single-PR response to a ``PullRequest``. Raises ``ForgeError``
    on anything that is not the expected shape, never a guess."""
    if not isinstance(payload, dict):
        raise _shape(f"PR {number} is not an object")
    if not isinstance(payload.get("number"), int) or payload.get("number") != number:
        raise _shape(f"PR number is {payload.get('number')!r}, want {number}")
    base = payload.get("base")
    head = payload.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise _shape(f"PR {number} has no base/head objects")
    base_repo = base.get("repo") or {}
    head_repo = head.get("repo")
    base_name = base_repo.get("full_name") if isinstance(base_repo, dict) else None
    if not isinstance(base_name, str) or not _REPO.fullmatch(base_name):
        raise _shape(f"PR {number} has no base repo name")
    if head_repo is not None:
        if not isinstance(head_repo, dict):
            raise _shape(f"PR {number} has a malformed head repo")
        head_name = head_repo.get("full_name")
        if not isinstance(head_name, str) or not _REPO.fullmatch(head_name):
            raise _shape(f"PR {number} has no head repo name")
    else:
        head_name = None
    user = payload.get("user") or {}
    author = user.get("login") if isinstance(user, dict) else None
    if not isinstance(author, str) or not author:
        raise _shape(f"PR {number} has no author login")
    labels = payload.get("labels")
    if labels is None:
        names: list[str] = []
    else:
        if not isinstance(labels, list):
            raise _shape(f"PR {number} labels are not a list")
        names = []
        for label in labels:
            name = label.get("name") if isinstance(label, dict) else None
            if not isinstance(name, str):
                raise _shape(f"PR {number} has a malformed label")
            names.append(name)
    if not isinstance(base.get("ref"), str) or not base["ref"]:
        raise _shape(f"PR {number} has no base ref")
    if not isinstance(head.get("ref"), str) or not head["ref"]:
        raise _shape(f"PR {number} has no head ref")
    return forge.PullRequest(
        repo=base_name,
        number=number,
        state=_pr_state(payload),
        base_ref=base["ref"],
        base_sha=_sha(base.get("sha"), "base sha"),
        head_ref=head["ref"],
        head_sha=_sha(head.get("sha"), "head sha"),
        author=author,
        from_fork=head_name is None or head_name != base_name,
        labels=tuple(names),
    )


def _parse_index_item(item):
    """``(head branch, state)`` for one PR-list entry."""
    if not isinstance(item, dict):
        raise _shape(f"PR list entry is not an object: {item!r}")
    head = item.get("head") or {}
    ref = head.get("ref") if isinstance(head, dict) else None
    if not isinstance(ref, str) or not ref:
        raise _shape("PR list entry has no head ref")
    return ref, _pr_state(item)


class GithubForge:
    """``corral.forge.Forge`` over ``gh api`` REST calls."""

    def __init__(self, run):
        """``run(args)`` returns stdout and raises when ``gh`` fails."""
        self._run = run

    def _get(self, path):
        try:
            return self._run(["api", path])
        except Exception as err:
            raise forge.ForgeError(f"forge unreachable: {err}") from err

    def pull_request(self, repo: str, number: int) -> forge.PullRequest:
        if not _REPO.fullmatch(repo or ""):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        try:
            payload = json.loads(self._get(f"repos/{repo}/pulls/{number}"))
        except forge.ForgeError:
            raise
        except Exception as err:
            raise forge.ForgeError(f"unusable answer for PR {number}: {err}") from err
        return _parse_pull(payload, number)

    def pr_index(self, repo: str) -> dict[str, str]:
        """``{head branch: state}`` for every PR ever opened on ``repo``.

        Pages ``state=all`` until a short page ends the list. A missing
        or malformed page is a ``ForgeError``, never a partial index: a
        truncated index would read merged branches as unknown and keep
        what the pass should delete.
        """
        if not _REPO.fullmatch(repo or ""):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        index: dict[str, str] = {}
        page = 1
        while True:
            try:
                payload = json.loads(
                    self._get(f"repos/{repo}/pulls?state=all&per_page={PER_PAGE}&page={page}")
                )
            except forge.ForgeError:
                raise
            except Exception as err:
                raise forge.ForgeError(f"unusable PR list page {page}: {err}") from err
            if not isinstance(payload, list):
                raise _shape(f"PR list page {page} is not a list")
            for item in payload:
                ref, state = _parse_index_item(item)
                index[ref] = state
            if len(payload) < PER_PAGE:
                return index
            page += 1


def repo_from_remote_url(url):
    """``owner/name`` from a GitHub remote URL, else ``None``.

        Accepts ``git@github.com:owner/name(.git)`` and
    ``https://``/``ssh://``/``git://`` URLs on ``github.com``. Any other
        remote (another host, a local path) is not a forge repo.
    """
    text = (url or "").strip()
    if not text:
        return None
    match = _SCP.match(text)
    if match:
        path = match.group(1)
    else:
        match = _URL.match(text)
        if not match or match.group(1).lower() != "github.com":
            return None
        path = match.group(2)
    path = path.rstrip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    repo = f"{parts[0]}/{parts[1]}"
    return repo if _REPO.fullmatch(repo) else None


def origin_repo(cwd, run):
    """``owner/name`` from the clone's ``origin`` remote, else ``None``.

    ``run(args, cwd)`` is the git seam. Nothing derivable (no origin,
    an unparsable URL, a non-GitHub remote) reads as ``None`` so the
    caller falls back to an empty index, never a guess.
    """
    try:
        proc = run(["remote", "get-url", "origin"], cwd)
    except Exception:
        return None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return None
    return repo_from_remote_url((getattr(proc, "stdout", "") or "").strip())
