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

from corral import forge, paths

#: One page of the PR list; the maximum the endpoint serves.
PER_PAGE = 100

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[^/\s]+/[^/\s]+")


def is_repo_name(text):
    """Whether `text` is an `owner/name` the forge can look up. The one definition."""
    return isinstance(text, str) and _REPO.fullmatch(text) is not None


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


#: The provider states where the head is not green but no action can
#: turn it green by waiting: the merge gate refuses until the branch
#: is updated, reviewed, or fixed. Only ``clean`` reads as passing and
#: only ``unknown`` (the provider has not finished computing) reads as
#: pending; anything unnamed is a ``ForgeError``, never a guess.
_FAILING_MERGEABLE = frozenset({"blocked", "behind", "unstable", "dirty", "draft", "has_hooks"})


def _checks_state(payload, number):
    """``"passing"`` | ``"failing"`` | ``"pending"`` for a PR object."""
    raw = payload.get("mergeable_state")
    if raw == "clean":
        return "passing"
    if raw == "unknown":
        return "pending"
    if isinstance(raw, str) and raw in _FAILING_MERGEABLE:
        return "failing"
    raise _shape(f"PR {number} has an unusable checks state {raw!r}")


def _merge_sha(payload, number, state):
    """The merge commit SHA once merged, else ``None``.

    An open PR may carry a preview merge commit; only a merged PR
    reports the SHA the verify step ties to the pinned head.
    """
    if state != forge.MERGED:
        return None
    return _sha(payload.get("merge_commit_sha"), f"PR {number} merge sha")


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
    state = _pr_state(payload)
    return forge.PullRequest(
        repo=base_name,
        number=number,
        state=state,
        base_ref=base["ref"],
        base_sha=_sha(base.get("sha"), "base sha"),
        head_ref=head["ref"],
        head_sha=_sha(head.get("sha"), "head sha"),
        author=author,
        from_fork=head_name is None or head_name != base_name,
        labels=tuple(names),
        checks_state=_checks_state(payload, number),
        merge_sha=_merge_sha(payload, number, state),
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


def _check_create_shape(owner, name):
    """``(owner, name)`` for a remote create, else ``ForgeError``.

    The name follows the one rule every repo-named path shares
    (``paths.plain_repo_name``); the owner holds no slash or
    whitespace. The check is fail-closed and pure, so callers map it
    to a usage error without any network read.
    """
    if not isinstance(owner, str) or not owner or "/" in owner or any(ch.isspace() for ch in owner):
        raise _shape(f"bad owner {owner!r}")
    if not paths.plain_repo_name(name):
        raise _shape(f"bad name {name!r}")
    return owner, name


#: gh's missing-repo answer. The status matches inside its parentheses,
#: so a port number like 4040 never reads as missing.
_NOT_FOUND_RE = re.compile(r"\(HTTP 404\)", re.IGNORECASE)


def _is_not_found(err):
    """Whether a failed `gh` call answered repo-not-found (else unreachable).

    Only gh's own shape counts: an unauthorized answer (a 401 or 403)
    reads as unreachable, never as missing.
    """
    return _NOT_FOUND_RE.search(str(err)) is not None


def _is_already_exists(err):
    """Whether a failed `gh repo create` lost a race to an existing repo."""
    return "already exists" in str(err).lower()


#: The merge endpoint's refusal answers. The provider documents 405 (the
#: merge cannot be performed: the PR was merged or closed, or a rule
#: blocks it) and 409 (a sha was sent and the head no longer matches)
#: for ``PUT pulls/{n}/merge``. Anything else (a dead connection, a 403,
#: a 404, a 422) reads as unreachable, never as a refusal. The status
#: matches inside its parentheses, so a port number never reads as one.
_MERGE_REFUSED_RE = re.compile(r"\(HTTP (405|409)\)", re.IGNORECASE)


def _is_merge_refused(err):
    """Whether a failed merge PUT is a provider refusal (else unreachable)."""
    return _MERGE_REFUSED_RE.search(str(err)) is not None


class GithubForge:
    """``corral.forge.Forge`` over ``gh api`` REST calls."""

    def __init__(self, run):
        """``run(args)`` returns stdout and raises when ``gh`` fails."""
        self._run = run

    def current_user(self) -> str:
        try:
            payload = json.loads(self._get("user"))
        except forge.ForgeError:
            raise
        except Exception as err:
            raise forge.ForgeError(f"unusable forge user: {err}") from err
        login = payload.get("login") if isinstance(payload, dict) else None
        if not isinstance(login, str) or not login:
            raise _shape(f"bad forge user {payload!r}")
        return login

    def repo_exists(self, repo: str) -> bool:
        """Whether `owner/name` exists, with one blind spot: the API answers
        a private repository the token cannot see with 404, the same as a
        missing one, so such a repo reads as `False` and the pre-check
        passes. Behavior is unchanged (the adapter cannot tell the two
        apart); the create then reports the already-exists failure."""
        if not _REPO.fullmatch(repo or ""):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        try:
            self._run(["api", f"repos/{repo}"])
        except Exception as err:
            if _is_not_found(err):
                return False
            raise forge.ForgeError(f"forge unreachable: {err}") from err
        return True

    def create_repo(self, owner: str, name: str, *, private: bool = True) -> str:
        owner, name = _check_create_shape(owner, name)
        target = f"{owner}/{name}"
        visibility = "--private" if private else "--public"
        try:
            self._run(["repo", "create", target, visibility])
        except Exception as err:
            if _is_already_exists(err):
                raise forge.ForgeExists(f"remote {target} already exists: {err}") from err
            raise forge.ForgeError(f"forge unreachable: {err}") from err
        return self.clone_url(target)

    def repo_empty(self, repo: str) -> bool:
        if not _REPO.fullmatch(repo or ""):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        try:
            branches = json.loads(self._get(f"repos/{repo}/branches"))
            tags = json.loads(self._get(f"repos/{repo}/tags"))
        except forge.ForgeError:
            raise
        except Exception as err:
            raise forge.ForgeError(f"unusable empty check for {repo}: {err}") from err
        if not isinstance(branches, list):
            raise _shape(f"bad branches for {repo}: {branches!r}")
        if not isinstance(tags, list):
            raise _shape(f"bad tags for {repo}: {tags!r}")
        return not branches and not tags

    def clone_url(self, repo: str) -> str:
        if not _REPO.fullmatch(repo or ""):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        try:
            payload = json.loads(self._get(f"repos/{repo}"))
        except forge.ForgeError:
            raise
        except Exception as err:
            raise forge.ForgeError(f"unusable forge repo {repo}: {err}") from err
        url = payload.get("clone_url") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url:
            raise _shape(f"bad clone url for {repo}: {payload!r}")
        return url

    def describe_create(self, name: str, owner: str | None = None, *, private: bool = True) -> str:
        if owner is None:
            _check_create_shape("owner", name)
        else:
            _check_create_shape(owner, name)
        visibility = "--private" if private else "--public"
        target = f"{owner}/{name}" if owner else name
        return f"gh repo create {target} {visibility}"

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

    def merge(self, repo: str, number: int, *, head_sha: str, method: str) -> str:
        """Squash-merge PR ``number`` pinned to ``head_sha``.

        The pin travels in the merge call itself, so the provider
        merges exactly the approved head or nothing. The fresh
        post-merge read must report ``merged`` with the reported merge
        SHA tied to that head; anything else fails closed because the
        merge happened but is unverified. A PR that is not open at the
        pinned head is ``ForgeRefused``, never merged.
        """
        if method != "squash":
            raise forge.ForgeError(f"unsupported merge method {method!r}")
        if not _REPO.fullmatch(repo or ""):
            raise forge.ForgeError(f"unusable forge repo {repo!r}")
        _sha(head_sha, "head sha")
        current = self.pull_request(repo, number)
        if current.state != forge.OPEN:
            raise forge.ForgeRefused(f"PR {number} is {current.state}, nothing to merge")
        if current.head_sha != head_sha:
            raise forge.ForgeRefused(f"PR {number} head moved: want {head_sha}")
        try:
            raw = self._run(
                [
                    "api",
                    "-X",
                    "PUT",
                    f"repos/{repo}/pulls/{number}/merge",
                    "-f",
                    "merge_method=squash",
                    "-f",
                    f"sha={head_sha}",
                ]
            )
        except Exception as err:
            if _is_merge_refused(err):
                raise forge.ForgeRefused(f"PR {number} merge refused: {err}") from err
            raise forge.ForgeError(f"forge unreachable during merge of PR {number}: {err}") from err
        try:
            payload = json.loads(raw)
        except Exception as err:
            raise forge.ForgeError(f"unusable merge answer for PR {number}: {err}") from err
        if not isinstance(payload, dict) or payload.get("merged") is not True:
            raise _shape(f"PR {number} merge was not accepted")
        reported = _sha(payload.get("sha"), f"PR {number} merge sha")
        verified = self.pull_request(repo, number)
        if verified.state != forge.MERGED or verified.merge_sha != reported:
            raise forge.ForgeError(f"PR {number} merge unverified: want {reported}")
        return reported

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
