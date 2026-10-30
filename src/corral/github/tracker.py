"""The GitHub tracker adapter: issues and PRs through the REST API.

GitHub answers PRs on the issues feed; only the pulls endpoint says
whether one comes from a fork. The payload shapes (``user.login``,
``pull_request``, ``updated_at``, ``labels[].name``) stay in this
module: everything else sees ``TrackerItem``.
"""

import json

from corral import tracker as iface
from corral.github.gh import is_rate_limited, retry_after_s

#: One page of the issues feed; the maximum the endpoint serves.
PER_PAGE: int = 100

#: Priority labels, highest first. The adapter derives the plain string
#: the store keeps from the highest-ranked ``priority:<name>`` label.
PRIORITIES = ("urgent", "high", "normal", "low", "backlog")
_PRIORITY_PREFIX = "priority:"

#: Corral-owned fields live on the tracker as labels. One label per field
#: at most; when several share a prefix the smallest wins and the rest
#: are pruned on the next write of that field.
_FIELD_PREFIX = {"agent": "agent:", "status": "status:"}

#: The only people-owned label the adapter ever adds. The decision
#: protocol holds a ticket for a human with this label; the adapter
#: exposes no general label write, so any other name is refused.
NEEDS_DECISION = "needs-decision"


def priority_of(names) -> str:
    """The highest-ranked ``priority:*`` suffix in ``names``, else ``""``.

    Unknown suffixes are ignored. Pure, so the store and the tests share it.
    """
    best = ""
    for name in names or ():
        if not isinstance(name, str) or not name.startswith(_PRIORITY_PREFIX):
            continue
        suffix = name[len(_PRIORITY_PREFIX) :]
        if suffix in PRIORITIES and (not best or PRIORITIES.index(suffix) < PRIORITIES.index(best)):
            best = suffix
    return best


def field_value_of(names, field) -> str:
    """The tracker's value for one corral-owned field from label ``names``.

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


class GithubTracker:
    """``corral.tracker.Tracker`` over ``gh api`` REST calls."""

    def __init__(self, run):
        """``run(args)`` returns stdout and raises when ``gh`` fails."""
        self._run = run
        self._forks: dict[tuple[str, int], bool] = {}

    def _get(self, path):
        try:
            return self._run(["api", path])
        except Exception as err:
            raise iface.TrackerError(f"tracker unreachable: {err}") from err

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

    def _parse(self, repo, payload) -> iface.TrackerItem:
        """One issues-endpoint object as a ``TrackerItem``.

        Anything short of the full shape is a ``TrackerError``, never a
        guess; a PR whose fork info cannot be read is ``from_fork``.
        """
        if not isinstance(payload, dict):
            raise _fail(f"item is not an object: {payload!r}")
        number = payload.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _fail(f"bad number {number!r}")
        kind = "pr" if "pull_request" in payload else "issue"
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
            from_fork=self._from_fork(repo, number) if kind == "pr" else False,
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

    def _write_error(self, err: Exception) -> iface.TrackerError:
        """One failed label or comment call as the tracker's error. Rate-limit text
        maps to ``TrackerRateLimited`` through the shared classifier, so
        the mirror pass stops; anything else fails closed as
        ``TrackerError``."""
        text = str(err) or repr(err)
        if is_rate_limited(text):
            return iface.TrackerRateLimited(text, retry_after_s(text))
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

    def write(self, repo: str, number: int, field: str, value: str) -> None:
        """Set one corral-owned field through the issues-labels endpoint.

        The wanted ``<field>:<value>`` label is added (creating it on first
        use) and every other label with the field's prefix is removed, so
        one field leaves one label behind. An already-matching tracker
        writes nothing; ``value == ""`` only removes. Anything outside the
        corral-owned fields is refused with ``ValueError``.
        """
        try:
            prefix = _FIELD_PREFIX[field]
        except KeyError:
            raise ValueError(f"refusing to write non-corral field {field!r}") from None
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
