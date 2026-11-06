"""The GitHub tracker adapter: issues and PRs through the REST API.

GitHub answers PRs on the issues feed; only the pulls endpoint says
whether one comes from a fork. The payload shapes (``user.login``,
``pull_request``, ``updated_at``, ``labels[].name``) stay in this
module: everything else sees ``TrackerItem``.
"""

import json
import re

from korapy import tracker as iface
from korapy.github import forge as ghforge
from korapy.github.gh import body_linked_issue_refs, is_rate_limited, retry_after_s
from korapy.tracker import NEEDS_DECISION, PRIORITIES, PRIORITY_PREFIX

#: One page of the issues feed; the maximum the endpoint serves.
PER_PAGE: int = 100

#: The bootstrap label colors, a provider detail the interface never
#: sees. Every `BOOTSTRAP_LABELS` name has an entry here; the ensure
#: tests assert the two agree.
_BOOTSTRAP_COLORS = {
    "ready": "0e8a16",
    "needs-triage": "fbca04",
    "needs-decision": "d93f0b",
    "hold": "5319e7",
    "priority:urgent": "b60205",
    "priority:high": "d93f0b",
    "priority:normal": "fbca04",
    "priority:low": "0e8a16",
    "priority:backlog": "cccccc",
}

#: A label create the account may not perform, in the provider's shape.
_REFUSED_RE = re.compile(r"\(HTTP 403\)", re.IGNORECASE)

#: The provider's validation status, in the provider's shape. One
#: pattern serves both classifiers: a label create that lost a race to
#: an existing label (the status plus the already-exists wording) reads
#: as kept, while any other 422 is a validation rejection. The tracker
#: answered either way, so neither is an unreachable tracker, and the
#: already-exists shape is matched first by its own callers, so it
#: never reaches the rejection mapping.
_HTTP_422_RE = re.compile(r"\(HTTP 422\)", re.IGNORECASE)
_ALREADY_EXISTS_PHRASE_RE = re.compile(r"already[_-]?exists", re.IGNORECASE)

#: Korapy-owned fields live on the tracker as labels. One label per field
#: at most; when several share a prefix the smallest wins and the rest
#: are pruned on the next write of that field.
_FIELD_PREFIX = {"agent": "agent:", "status": "status:"}


def priority_of(names) -> str:
    """The highest-ranked ``priority:*`` suffix in ``names``, else ``""``.

    Unknown suffixes are ignored. Pure, so the store and the tests share it.
    """
    best = ""
    for name in names or ():
        if not isinstance(name, str) or not name.startswith(PRIORITY_PREFIX):
            continue
        suffix = name[len(PRIORITY_PREFIX) :]
        if suffix in PRIORITIES and (not best or PRIORITIES.index(suffix) < PRIORITIES.index(best)):
            best = suffix
    return best


def field_value_of(names, field) -> str:
    """The tracker's value for one korapy-owned field from label ``names``.

    The smallest ``<field>:<value>`` label wins; anything else with the
    prefix is pruned on the next write of that field. Absent means ``""``.
    Pure, so the store and the tests share it.
    """
    prefix = _FIELD_PREFIX[field]
    best = None
    for name in names or ():
        if not isinstance(name, str) or not name.startswith(prefix):
            continue
        if best is None or name < best:
            best = name
    return "" if best is None else best[len(prefix) :]


def _fail(text):
    return iface.TrackerError(f"unusable tracker answer: {text}")


def milestone_of(payload) -> str | None:
    """The milestone title of an issues-endpoint object, else ``None``.

    A missing or null milestone reads as ``None``; anything short of a
    title string is a ``TrackerError``, never a guess. Pure, so the
    board reader shares it with this module's own parse.
    """
    if not isinstance(payload, dict):
        raise _fail(f"item is not an object: {payload!r}")
    milestone = payload.get("milestone")
    if milestone is None:
        return None
    if not isinstance(milestone, dict):
        raise _fail(f"bad milestone {milestone!r}")
    title = milestone.get("title")
    if not isinstance(title, str) or not title:
        raise _fail(f"bad milestone {milestone!r}")
    return title


def _parse_updated(text):
    """Epoch seconds for an ``updated_at`` value, else ``TrackerError``."""
    if not isinstance(text, str):
        raise _fail(f"bad updated_at {text!r}")
    try:
        return iface.parse_cursor(text)
    except iface.TrackerError:
        raise _fail(f"bad updated_at {text!r}") from None


def _parse_names(entries, field):
    """Label or assignee names from a payload list, else ``TrackerError``."""
    if not isinstance(entries, list):
        raise _fail(f"{field} is not a list")
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise _fail(f"bad {field} entry {entry!r}")
        key = "name" if field == "labels" else "login"
        name = entry.get(key)
        if not isinstance(name, str):
            raise _fail(f"bad {field} entry {entry!r}")
        names.append(name)
    return tuple(names)


def _is_refused(err):
    """Whether a failed label create is a permission refusal.

    A 403 in the provider's shape reads as refused (in someone else's
    organisation the account may not create labels); anything else fails
    closed through ``_write_error``.
    """
    return _REFUSED_RE.search(str(err)) is not None


def _is_already_exists(err):
    """Whether a failed label create lost a race to an existing label.

    Only the provider's own shape counts: the 422 status plus the
    already-exists wording, so a 422 for a malformed create still fails
    closed through ``_write_error``.
    """
    text = str(err)
    return (
        _HTTP_422_RE.search(text) is not None and _ALREADY_EXISTS_PHRASE_RE.search(text) is not None
    )


class GithubTracker:
    """``korapy.tracker.Tracker`` over ``gh api`` REST calls."""

    def __init__(self, run):
        """``run(args)`` returns stdout and raises when ``gh`` fails."""
        self._run = run
        self._forks: dict[tuple[str, int], bool] = {}

    def _get(self, path):
        try:
            return self._run(["api", path])
        except Exception as err:
            raise self._write_error(err) from err

    def _from_fork(self, repo, number) -> bool:
        """Whether PR ``number`` comes from a fork, cached per tracker.

        The pulls endpoint alone carries ``head.repo``. A pull payload
        that cannot be read reads as a fork: the item turns external
        instead of being trusted.
        """
        key = (repo, number)
        if key not in self._forks:
            try:
                payload = json.loads(self._get(f"repos/{repo}/pulls/{number}"))
            except (iface.TrackerError, ValueError):
                payload = None
            fork = None
            if isinstance(payload, dict):
                head = payload.get("head")
                head_repo = head.get("repo") if isinstance(head, dict) else None
                if isinstance(head_repo, dict):
                    fork = head_repo.get("fork")
            self._forks[key] = True if fork is None else bool(fork)
        return self._forks[key]

    def _parse(self, repo, payload, *, forks=True) -> iface.TrackerItem:
        """One issues- or pulls-endpoint object as a ``TrackerItem``.

        Anything short of the full shape is a ``TrackerError``, never a
        guess; a PR whose fork info cannot be read is ``from_fork``.
        Pulls-endpoint objects carry ``head`` instead of the issues
        endpoint's ``pull_request`` marker, which is how they read as
        ``kind="pr"``. With ``forks`` False the fork lookup is skipped
        (the item reads as not from a fork): the pulls index needs no
        fork info and must not spend a call per pull request on it.
        """
        if not isinstance(payload, dict):
            raise _fail(f"item is not an object: {payload!r}")
        number = payload.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _fail(f"bad number {number!r}")
        kind = "pr" if ("pull_request" in payload or "head" in payload) else "issue"
        title = payload.get("title")
        if not isinstance(title, str):
            raise _fail(f"bad title {title!r}")
        body = payload.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise _fail(f"bad body {body!r}")
        state = payload.get("state")
        if state not in ("open", "closed"):
            raise _fail(f"bad state {state!r}")
        user = payload.get("user")
        if user is None:
            author = ""
        elif isinstance(user, dict) and isinstance(user.get("login"), str):
            author = user["login"]
        else:
            raise _fail(f"bad user {user!r}")
        labels = _parse_names(payload.get("labels"), "labels")
        assignees = _parse_names(payload.get("assignees", []), "assignees")
        url = payload.get("html_url")
        if not isinstance(url, str):
            raise _fail(f"bad url {url!r}")
        updated_at = _parse_updated(payload.get("updated_at"))
        return iface.TrackerItem(
            repo=repo,
            number=number,
            kind=kind,
            title=title,
            body=body,
            state=state,
            author=author,
            labels=labels,
            priority=priority_of(labels),
            assignees=assignees,
            url=url,
            updated_at=updated_at,
            from_fork=(self._from_fork(repo, number) if forks else False)
            if kind == "pr"
            else False,
            agent=field_value_of(labels, "agent"),
            status=field_value_of(labels, "status"),
            # The REST feed carries no board concepts: an empty reading
            # keeps the stored board extra when the store merges by key.
            extra={},
        )

    def _page(self, path):
        """One feed page as a list of payloads, else ``TrackerError``."""
        try:
            payload = json.loads(self._get(path))
        except iface.TrackerError:
            raise
        except Exception as err:
            raise _fail(f"page is not JSON: {err}") from err
        if not isinstance(payload, list):
            raise _fail(f"page is not a list: {path}")
        return payload

    def _feed(self, repo, since):
        """Every issues-feed payload for ``repo`` since ``since`` (or all).

        Pages until a short page ends the list. A missing or malformed
        page is a ``TrackerError``, never a partial feed: a truncated
        feed would read deleted items as still open.
        """
        base = f"repos/{repo}/issues?state=all&sort=updated&direction=asc&per_page={PER_PAGE}"
        if since is not None:
            base += f"&since={since}"
        page = 1
        while True:
            path = base if page == 1 else f"{base}&page={page}"
            entries = self._page(path)
            for entry in entries:
                yield entry
            if len(entries) < PER_PAGE:
                return
            page += 1

    def item(self, repo: str, number: int) -> iface.TrackerItem:
        found, _ = self.read_issue(repo, number)
        return found

    def read_issue(self, repo: str, number: int) -> tuple[iface.TrackerItem, str | None]:
        """One issue plus its milestone title, fresh from the REST endpoint.

        The milestone rides along because the board reader overlays it as
        a concept; the plain ``item`` path keeps ignoring it. A missing
        or null milestone reads as ``None``.
        """
        try:
            payload = json.loads(self._get(f"repos/{repo}/issues/{number}"))
        except iface.TrackerError:
            raise
        except Exception as err:
            raise _fail(f"item {repo}#{number} is not JSON: {err}") from err
        return self._parse(repo, payload), milestone_of(payload)

    def changes(self, repo: str, cursor: str | None) -> tuple[list[iface.TrackerItem], str]:
        """Items updated since ``cursor``, and the cursor of the newest
        one seen. ``since`` is inclusive, so re-delivery of the newest
        item is expected and harmless: the store upsert is idempotent."""
        found = [self._parse(repo, entry) for entry in self._feed(repo, cursor)]
        if not found:
            # Nothing newer: keep the cursor, or the beginning of time
            # when there is none yet. Either way it never moves backwards.
            return [], cursor if cursor is not None else iface.format_cursor(0.0)
        newest = max(item.updated_at for item in found)
        if cursor:
            try:
                if iface.parse_cursor(cursor) >= newest:
                    return found, cursor
            except iface.TrackerError:
                pass
        return found, iface.format_cursor(newest)

    def items(self, repo: str):
        """Every item, oldest first, for a full resync."""
        for entry in self._feed(repo, None):
            yield self._parse(repo, entry)

    def pulls_index(self, repo: str, max_calls: int) -> iface.PullsIndex:
        """The open pull requests on ``repo``, keyed by linked ticket.

        Lists the repo's open pull requests oldest first, one paged
        listing shared by every ticket, and files each pull request
        under the tickets its title or body links through
        ``body_linked_issue_refs`` (close keywords and ``Refs`` lines,
        outside fenced code; same-repo links only). Pages until a
        short page ends the list, like the feed, or ``max_calls``
        pages run out (then the index is partial and ``complete`` is
        False). A missing or malformed page is a ``TrackerError``,
        never a partial list. The parse skips the per-pull fork
        lookup: the index needs no fork info, and one extra call per
        pull request is what the per-ticket lookup cost before.
        """
        by_issue: dict[int, list] = {}
        calls = 0
        page = 1
        budget = max(1, max_calls)
        while True:
            base = f"repos/{repo}/pulls?state=open&sort=updated&direction=asc&per_page={PER_PAGE}"
            entries = self._page(base if page == 1 else f"{base}&page={page}")
            calls += 1
            for entry in entries:
                item = self._parse(repo, entry, forks=False)
                text = f"{item.title}\n{item.body}"
                for linked_repo, linked_number in body_linked_issue_refs(text, repo):
                    if linked_repo == repo:
                        by_issue.setdefault(int(linked_number), []).append(item)
            if len(entries) < PER_PAGE:
                return iface.PullsIndex(by_issue, calls, True)
            if calls >= budget:
                return iface.PullsIndex(by_issue, calls, False)
            page += 1

    def _write_error(self, err: Exception) -> iface.TrackerError:
        """One failed tracker call as the tracker's error. Rate-limit text
        maps to ``TrackerRateLimited`` through the shared classifier, so
        the mirror pass stops; a validation rejection maps to
        ``TrackerRejected``, so callers report rejected, never
        unreachable; anything else fails closed as ``TrackerError``."""
        text = str(err) or repr(err)
        if is_rate_limited(text):
            return iface.TrackerRateLimited(text, retry_after_s(text))
        if _HTTP_422_RE.search(text) is not None:
            return iface.TrackerRejected(f"tracker rejected: {text}")
        return iface.TrackerError(f"tracker unreachable: {text}")

    def _label_names(self, repo: str, number: int) -> tuple[str, ...]:
        """The item's current label names, else ``TrackerError``."""
        try:
            payload = json.loads(self._run(["api", f"repos/{repo}/issues/{number}/labels"]))
        except Exception as err:
            raise self._write_error(err) from err
        return _parse_names(payload, "labels")

    def comment(self, repo: str, number: int, body: str) -> None:
        """Post one decision-protocol comment through the issues endpoint.

        The loop's own question only; there is no general comment edit.
        A failed call raises ``TrackerError`` through ``_write_error``.
        """
        try:
            self._run(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repo}/issues/{number}/comments",
                    "-f",
                    f"body={body}",
                ]
            )
        except Exception as err:
            raise self._write_error(err) from err

    def add_label(self, repo: str, number: int, label: str) -> None:
        """Add the one decision label through the issues-labels endpoint.

        Only ``NEEDS_DECISION`` is accepted; there is no general label
        write, so any other name is refused with ``ValueError`` and no
        ``gh`` call runs. A failed call raises ``TrackerError`` through
        ``_write_error``.
        """
        if label != NEEDS_DECISION:
            raise ValueError(f"refusing to add non-decision label {label!r}")
        try:
            self._run(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repo}/issues/{number}/labels",
                    "-f",
                    f"labels[]={label}",
                ]
            )
        except Exception as err:
            raise self._write_error(err) from err

    def _list_label_names(self, repo: str) -> set[str]:
        """Every label name on ``repo``, across all list pages.

        Pages ``per_page`` at a time until a short page ends the list,
        like ``pr_index``. A missing or malformed page fails closed: a
        truncated list would read a present label as missing, post a
        create, and lose on the already-exists answer.
        """
        have = set()
        page = 1
        while True:
            try:
                payload = json.loads(
                    self._run(["api", f"repos/{repo}/labels?per_page={PER_PAGE}&page={page}"])
                )
            except Exception as err:
                raise self._write_error(err) from err
            if not isinstance(payload, list):
                raise _fail(f"labels of {repo} are not a list")
            for entry in payload:
                name = entry.get("name") if isinstance(entry, dict) else None
                if not isinstance(name, str):
                    raise _fail(f"bad label entry {entry!r} on {repo}")
                have.add(name)
            if len(payload) < PER_PAGE:
                return have
            page += 1

    def _read_label_name(self, repo: str, name: str) -> str:
        """The one label ``name`` on ``repo``, fresh, else ``TrackerError``.

        The proof for a create that answered already-exists: only a
        label that reads back counts as kept. A failed read fails
        closed, never as kept.
        """
        try:
            payload = json.loads(self._run(["api", f"repos/{repo}/labels/{name}"]))
        except Exception as err:
            raise self._write_error(err) from err
        if not isinstance(payload, dict) or payload.get("name") != name:
            raise _fail(f"bad label {payload!r} on {repo}")
        return name

    def missing_labels(self, repo: str) -> tuple[str, ...]:
        """Names of the bootstrap labels absent on ``repo``, in bootstrap order.

        Read-only: one paged label listing, never a create. Shares the
        listing (and its fail-closed shape checks) with `ensure_labels`.
        """
        have = self._list_label_names(repo)
        return tuple(label.name for label in iface.BOOTSTRAP_LABELS if label.name not in have)

    def canonical_repo(self, repo: str) -> str:
        """The tracker's canonical `owner/name` for ``repo``.

        One repository read: after a rename the provider redirects the
        old name and answers the new `full_name`. Anything short of an
        `owner/name` shape fails closed; a failed call fails through
        `_write_error`, like every other REST read.
        """
        try:
            payload = json.loads(self._get(f"repos/{repo}"))
        except iface.TrackerError:
            raise
        except Exception as err:
            raise _fail(f"canonical repo {repo} is not JSON: {err}") from err
        name = payload.get("full_name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not ghforge.is_repo_name(name):
            raise _fail(f"bad canonical name {name!r} for {repo}")
        return name

    def ensure_labels(self, repo: str) -> iface.LabelReport:
        """Create the missing bootstrap labels on ``repo``, leave the rest alone.

        Missing labels are created with the adapter's colors; existing
        labels are left byte-identical, never overwritten. A 403 on a
        create reads as refused (the account may not create labels in
        someone else's organisation) and the pass continues; a 422
        already-exists on a create reads as kept once the label reads
        back (a concurrent setup created it first); anything else fails
        closed through ``_write_error``.
        """
        have = self._list_label_names(repo)
        created, kept, refused = [], [], []
        for wanted in iface.BOOTSTRAP_LABELS:
            if wanted.name in have:
                kept.append(wanted.name)
                continue
            try:
                self._run(
                    [
                        "api",
                        "--method",
                        "POST",
                        f"repos/{repo}/labels",
                        "-f",
                        f"name={wanted.name}",
                        "-f",
                        f"color={_BOOTSTRAP_COLORS[wanted.name]}",
                        "-f",
                        f"description={wanted.description}",
                    ]
                )
            except Exception as err:
                # A rate limit answers 403 too: it stays retryable and
                # never reads as a permission refusal.
                if is_rate_limited(str(err)):
                    raise self._write_error(err) from err
                if _is_refused(err):
                    refused.append(wanted.name)
                    continue
                if _is_already_exists(err):
                    kept.append(self._read_label_name(repo, wanted.name))
                    continue
                raise self._write_error(err) from err
            created.append(wanted.name)
        return iface.LabelReport(tuple(created), tuple(kept), tuple(refused))

    def write(self, repo: str, number: int, field: str, value: str) -> None:
        """Set one korapy-owned field through the issues-labels endpoint.

        The wanted ``<field>:<value>`` label is added (creating it on first
        use) and every other label with the field's prefix is removed, so
        one field leaves one label behind. An already-matching tracker
        writes nothing; ``value == ""`` only removes. Anything outside the
        korapy-owned fields is refused with ``ValueError``.
        """
        try:
            prefix = _FIELD_PREFIX[field]
        except KeyError:
            raise ValueError(f"refusing to write non-korapy field {field!r}") from None
        current = self._label_names(repo, number)
        want = f"{prefix}{value}" if value else None
        have = sorted(name for name in current if name.startswith(prefix))
        drop = [name for name in have if name != want]
        path = f"repos/{repo}/issues/{number}/labels"
        try:
            if want is not None and want not in have:
                self._run(["api", "--method", "POST", path, "-f", f"labels[]={want}"])
            for name in drop:
                self._run(["api", "--method", "DELETE", f"{path}/{name}"])
        except Exception as err:
            raise self._write_error(err) from err
