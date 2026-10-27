"""Coordinator event queue, one per project.

A project's daemon pass appends judgment-tier events here; a coordinator
session blocks on `corral coord wait` instead of running hand-written
polling loops. Append-only JSONL under the project root, one object per
line:

{"id": "<8 hex>", "kind": "<kind>", "ref": "<repo>#<n>",
 "evidence": "<text>", "state": "<state>", "first_seen": "<iso>",
 "resolved_at": null}

Dedupe key is (kind, ref, state): re-reporting an unresolved event
refreshes its evidence, never duplicates it. The state is the cause's
identity beyond its ref: a held merge's head SHA, a question's comment
body. A cause whose state changes is a new event, so a new head SHA or
a new question re-emits.

`record` resolves (sets `resolved_at`) every unresolved row whose kind
was synced in this call but whose key is absent: the cause is gone.
Kinds outside the sync scope are left alone, so partial callers cannot
clear each other. Resolved rows older than RESOLVED_KEEP_DAYS are
pruned on write.
"""

import contextlib
import fcntl
import json
import os
import time


#: Snapshot attention kinds mapped onto queue kinds. Anything else
#: passes through verbatim under its own attention kind.
ATTENTION_TO_EVENT = {
    "question": "coordinator-review",
    "held-merge": "held-merge",
    "stalled-merge": "held-merge",
}

#: Attention kinds treated as coordinator-tier when an item carries no
#: `tier` tag (queued merges resolve themselves, urgent and release asks
#: are human-tier).
DEFAULT_JUDGMENT_ATTENTION = frozenset({"question", "held-merge", "stalled-merge"})

EVENTS_DIRNAME = "coord"
EVENTS_NAME = "events.jsonl"
EVENTS_LOCK_NAME = "events.lock"

#: Resolved rows older than this are pruned on the next write.
RESOLVED_KEEP_DAYS = 14

#: `wait` poll interval: a short local poll of the queue file, no
#: GitHub calls. stdlib has no inotify; the poll is the mechanism.
WAIT_POLL_S = 0.2


class UsageError(Exception):
    """Missing flag or bad value (maps to exit 4 at the CLI)."""


class UnknownEventError(Exception):
    """`ack` named an id with no unresolved event."""

    def __init__(self, event_id):
        super().__init__(f"no unresolved event {event_id!r}")
        self.event_id = event_id


def events_path(root=None):
    """Queue file path under the project root."""
    from corral.coord import project_root

    base = root if root is not None else project_root()
    return os.path.join(base, EVENTS_DIRNAME, EVENTS_NAME)


def events_lock_path(root=None):
    """Lock file guarding the queue."""
    from corral.coord import project_root

    base = root if root is not None else project_root()
    return os.path.join(base, EVENTS_DIRNAME, EVENTS_LOCK_NAME)


@contextlib.contextmanager
def _locked(exclusive=True, root=None):
    path = events_lock_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _now_iso(now=None):
    import datetime

    moment = now
    if moment is None:
        moment = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(moment, (int, float)):
        moment = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
    if isinstance(moment, datetime.datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment.isoformat()
    return str(moment)


def _parse_row(line):
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _read_rows(root=None):
    try:
        with open(events_path(root), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return [row for line in lines for row in [_parse_row(line)] if row is not None]


def _resolved_old(row, now_epoch, keep_s):
    at = row.get("resolved_at")
    if not at:
        return False
    try:
        import datetime

        moment = datetime.datetime.fromisoformat(str(at))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return now_epoch - moment.timestamp() > keep_s
    except (ValueError, TypeError, OverflowError):
        return False


def _write_rows(rows, root=None):
    path = events_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _dedupe_key(kind, ref, state=""):
    return f"{kind}\x00{ref}\x00{state}"


def _row_state(row):
    """The state of a stored row; rows written without one read as ""."""
    return str(row.get("state") or "")


#: The state of a cause is a length-prefixed pair, so two different
#: causes never share one: the head SHA's length, a colon, the head
#: SHA, then the reason.
def hold_state(head_sha, reason):
    """The dedupe state of a held merge: its head SHA and its reason.

    The pair is the ticket's own key: once per (ref, head SHA,
    reason). A new head or a changed check state is a new state and
    re-emits; the same head and reason stay one event however many
    passes see them.
    """
    head = str(head_sha or "")
    return f"{len(head)}:{head}{reason or ''}"


def question_state(comment):
    """The dedupe state of a question: its comment body.

    The body is the question: a new comment is a new question and
    re-emits, the same body is one event.
    """
    return str(comment or "")


def read_events(root=None):
    """All queue rows, oldest first (shared lock, no pruning)."""
    with _locked(exclusive=False, root=root):
        return _read_rows(root)


def unresolved(root=None):
    """Unresolved queue rows, oldest first."""
    return [row for row in read_events(root) if not row.get("resolved_at")]


def new_event_id():
    """Fresh event id: 8 hex chars (matches the logbook follow-up ids)."""
    return os.urandom(4).hex()


def record(items, root=None, now=None, event_id_fn=None, kinds=None):
    """Sync judgment-tier facts into the queue; returns the rows written.

     `items` holds {"kind", "ref", "evidence", "state"} dicts (evidence
     and state optional). Malformed rows are dropped.

     The dedupe key is (kind, ref, state): the state is the cause's
     identity beyond its ref (a held merge's head SHA and reason, a
     question's comment body). One (kind, ref) may carry several live
     states at once (two held PRs on one ticket), and each of them has
     its own event: a fact whose state is already open refreshes that
     event's evidence, a fact with a new state opens its own, and a
     state that stops being reported resolves. An acked state stays
     acked while its cause is live, so a stopped agent awaiting a human
     produces one event per distinct state however many passes see it
    .

     Unresolved rows whose kind is in `kinds` but whose (kind, ref) is
     absent resolve themselves (their cause is gone). `kinds` defaults
     to the kinds present in `items`; a caller that observed a kind
     with zero facts (an empty hold list means no holds) passes it
     explicitly so gone causes still clear. Kinds outside `kinds` are
     untouched, so partial writers cannot clear each other. Resolved
     rows older than RESOLVED_KEEP_DAYS are pruned.
    """
    import datetime

    fresh = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        kind, ref = item.get("kind"), item.get("ref")
        if not kind or not ref:
            continue
        fresh.append(
            {
                "kind": str(kind),
                "ref": str(ref),
                "evidence": str(item.get("evidence") or ""),
                "state": str(item.get("state") or ""),
            }
        )
    moment_iso = _now_iso(now)
    if isinstance(now, (int, float)):
        now_epoch = float(now)
    elif isinstance(now, datetime.datetime):
        stamp = (
            now.timestamp()
            if now.tzinfo is not None
            else now.replace(tzinfo=datetime.timezone.utc).timestamp()
        )
        now_epoch = float(stamp)
    else:
        now_epoch = time.time()
    make_id = event_id_fn or new_event_id
    with _locked(exclusive=True, root=root):
        rows = _read_rows(root)
        # Live states are a SET per (kind, ref): one ticket can carry
        # several causes at once (two held PRs on one ticket), each
        # with its own state, and every one of them must stay visible.
        # A single "last state seen" per pair created one row and
        # immediately resolved it stale in the same pass (review of
        # so the coordinator never saw it.
        open_rows = {}
        acked_states = set()
        for row in rows:
            if not row.get("kind") or not row.get("ref"):
                continue
            pair = _dedupe_key(row["kind"], row["ref"])
            state = _row_state(row)
            if not row.get("resolved_at"):
                # A row written without a state keys as
                # "" so a stateless fact still dedupes against it; a
                # stateful fact for the same pair leaves it out of the
                # live set, so it resolves and the new state opens
                # (found in review).
                open_rows.setdefault(pair, {}).setdefault(state, row)
                if open_rows[pair][state] is not row:
                    # The same open key twice (a hand edit, never a
                    # clean write): the first row stays canonical and
                    # this duplicate resolves at once.
                    row["resolved_at"] = moment_iso
                    row["resolved_by"] = "stale"
            elif row.get("resolved_by") == "ack":
                acked_states.add((pair, state))
        live_states = {}
        for item in fresh:
            live_states.setdefault(_dedupe_key(item["kind"], item["ref"]), set()).add(item["state"])
        by_key = {
            (pair, state): row
            for pair, states in open_rows.items()
            for state, row in states.items()
        }
        seen = set()
        for item in fresh:
            pair = _dedupe_key(item["kind"], item["ref"])
            key = (pair, item["state"])
            seen.add(pair)
            if key in by_key:
                by_key[key]["evidence"] = item["evidence"]
                continue
            if key in acked_states:
                # The same state was already reported for this pair
                # and the cause is still there: the event exists (open
                # or acked), so no second one opens.
                continue
            row = {
                "id": make_id(),
                "kind": item["kind"],
                "ref": item["ref"],
                "evidence": item["evidence"],
                "state": item["state"],
                "first_seen": moment_iso,
                "resolved_at": None,
            }
            rows.append(row)
            by_key[key] = row
        synced_kinds = set(kinds) if kinds is not None else {item["kind"] for item in fresh}
        for row in rows:
            if row.get("kind") not in synced_kinds:
                continue
            pair = _dedupe_key(row.get("kind"), row.get("ref"))
            if pair not in seen:
                # The cause is gone: the kind was synced and the pair
                # was not seen, so nothing holds it any more.
                if not row.get("resolved_at"):
                    row["resolved_at"] = moment_iso
                    row["resolved_by"] = "gone"
                elif row.get("resolved_by") == "ack":
                    # An acked event whose cause went away: the ack
                    # stands, but the cause is gone, so its return
                    # re-emits.
                    row["resolved_by"] = "gone"
            elif _row_state(row) not in live_states.get(pair, ()):
                # This state is no longer among the live ones: the
                # cause moved on, or an acked state went away. Either
                # way its return is a new event.
                if not row.get("resolved_at"):
                    row["resolved_at"] = moment_iso
                    row["resolved_by"] = "stale"
                elif row.get("resolved_by") == "ack":
                    row["resolved_by"] = "gone"
        keep_s = RESOLVED_KEEP_DAYS * 86400
        rows = [
            row
            for row in rows
            if row.get("resolved_at") is None or not _resolved_old(row, now_epoch, keep_s)
        ]
        _write_rows(rows, root)
        return rows


def ack(event_id, root=None, now=None):
    """Resolve one unresolved event by id; returns the resolved row.

    The row keeps `resolved_by: "ack"`: the coordinator saw it, and
    the cause is still there. The next pass therefore does not open a
    second event for the same state. The cause going away
    turns the row into `resolved_by: "gone"`, and its return
    re-emits.
    """
    if not event_id:
        raise UsageError("ack needs an event id")
    moment_iso = _now_iso(now)
    with _locked(exclusive=True, root=root):
        rows = _read_rows(root)
        for row in rows:
            if row.get("id") == event_id and not row.get("resolved_at"):
                row["resolved_at"] = moment_iso
                row["resolved_by"] = "ack"
                _write_rows(rows, root)
                return row
    raise UnknownEventError(event_id)


def _attention_evidence(item):
    for key in ("question", "reason", "title"):
        text = item.get(key)
        if text:
            title = item.get("title") or ""
            if key != "title" and title and title not in str(text):
                return f"{title}: {text}"
            return str(text)
    return ""


def _attention_state(item):
    """The cause's identity beyond its ref, for the dedupe key.

    A held merge's state is its head SHA and its reason; a question's
    state is its comment body. A new head or a new question is a new
    cause and re-emits; the same cause is one event. An item carrying
    neither reads as the empty state, which dedupes on (kind, ref)
    alone.
    """
    head = str(item.get("head_sha") or "").strip()
    reason = str(item.get("reason") or "")
    if head or reason:
        return hold_state(head, reason)
    for key in ("question", "title"):
        text = item.get(key)
        if text:
            return question_state(text)
    return ""


def facts_from_attention(items):
    """Judgment-tier facts from snapshot attention items (pure).

    Each item is judged on its own: an item with a `tier` tag
    keeps only `tier == "judgment"`; an item escalated to the human
    after the coordinator grace stays a coordinator
    fact through its `escalated_from: "judgment"` tag, so the wake
    still sees what the coordinator never took. An untiered item
    (no tag, or `tier: null`) falls back to the known
    coordinator attention kinds. Each fact is {"kind", "ref",
    "evidence", "state"} for `record`. Malformed items and empty refs
    drop out.
    """
    facts = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        if item.get("tier") is not None:
            if item.get("tier") != "judgment" and item.get("escalated_from") != "judgment":
                continue
        elif item.get("kind") not in DEFAULT_JUDGMENT_ATTENTION:
            continue
        ref = item.get("ref")
        if not ref:
            continue
        kind = ATTENTION_TO_EVENT.get(item.get("kind"), item.get("kind"))
        facts.append(
            {
                "kind": kind,
                "ref": str(ref),
                "evidence": _attention_evidence(item),
                "state": _attention_state(item),
            }
        )
    return facts


def sync_questions_holds(
    questions, holds, root=None, now=None, kinds=("coordinator-review", "held-merge")
):
    """Record one pass's questions and holds; returns rows written.

     `questions` maps ref to (title, url, comment, ...) tuples (the
     snapshot shape); `holds` holds (pr, reason, files[, since]) tuples.
     The resolve scope defaults to both observed kinds, so an empty pass
     (no questions, no holds) still clears gone causes; kinds other
     writers own (supply, agents, ...) are untouched. A daemon
     passes only the kinds it actually observed, so a failed read
     never clears events it never saw. Never raises: a daemon
     pass calls this best-effort.

     Each fact carries a `state`: the cause's identity beyond its ref
    . A question's state is its comment body; a hold's state is
     its head SHA. The same cause with the same state is one event
     however many passes see it, so a stopped agent awaiting a human
     produces one event per distinct state instead of one per pass. A
     changed state (a new head SHA, a new question) is a new event and
     re-emits.
    """
    from corral.github import gh as _gh

    facts = []
    for ref, entry in (questions or {}).items():
        if not ref or not isinstance(entry, (list, tuple)) or not entry:
            continue
        title = entry[0] if len(entry) > 0 else ""
        comment = entry[2] if len(entry) > 2 else ""
        if title and comment and title not in str(comment):
            evidence = f"{title}: {comment}"
        else:
            evidence = str(comment or title or "")
        facts.append(
            {
                "kind": "coordinator-review",
                "ref": str(ref),
                "evidence": evidence,
                "state": question_state(comment),
            }
        )
    for hold in holds or []:
        if not isinstance(hold, (list, tuple)) or len(hold) < 2:
            continue
        pr, reason = hold[0], hold[1]
        if not isinstance(pr, dict):
            continue
        linked = _gh.linked_issue_refs(pr)
        if len(linked) == 1:
            full, number = linked[0]
            ref = f"{full.rsplit('/', 1)[-1]}#{number}"
        else:
            ref = f"{pr.get('repo')}#{pr.get('number')}"
        facts.append(
            {
                "kind": "held-merge",
                "ref": ref,
                "evidence": str(reason or ""),
                "state": hold_state(pr.get("head_sha"), reason),
            }
        )
    try:
        return record(facts, root=root, now=now, kinds=kinds)
    except Exception:
        return []


def wait_for_events(timeout=None, root=None, poll_s=WAIT_POLL_S, now_fn=None, sleep_fn=None):
    """Block until an unresolved event exists; no GitHub calls.

    Returns the unresolved batch, or None on timeout. `timeout` is
    seconds (None waits forever); non-positive times out at once when
    the queue is empty. Seams are injectable for tests.
    """
    clock = now_fn or time.time
    sleep = sleep_fn or time.sleep
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise UsageError("timeout needs seconds")
        if timeout != timeout or timeout < 0:
            raise UsageError("timeout needs seconds")
    start = clock()
    while True:
        batch = unresolved(root)
        if batch:
            return batch
        if timeout is not None and clock() - start >= timeout:
            return None
        sleep(poll_s)
