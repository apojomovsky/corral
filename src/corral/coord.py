"""Coordinator lease and logbook, one set per project.

Everything lives under the project root, ``<state>/projects/<project>/``
(``project_root``), so two projects' coordinators never share a lease.

Lease: one JSON file, ``coord/lease.json`` under the project root,
guarded by ``fcntl.flock`` on ``coord/lease.lock`` for every
read-modify-write; writes are atomic (temp file plus ``os.replace``). The
holder is the nearest ancestor of the calling process whose
``comm``/argv[0] basename is ``claude`` or ``omp``; ``pid_start`` is
``/proc/<pid>/stat`` field 22 so a reused pid is never mistaken for the
holder.

Free when: no file, ``holder`` is null (released), the holder pid is gone
or its start time differs, or the heartbeat is older than ``COORD_TTL_S``.
``generation`` increases on every acquire and takeover and never resets.

Logbook (section 5): ``coord/journal.jsonl``, append-only, one JSON object
per line, guarded by ``fcntl.flock`` on ``coord/journal.lock``; rotation
runs inside the same lock as the append. ``proc``/``now`` parameters are
the injectable seams for the process table and the clock: a ``proc``
object implements ``comm(pid)``, ``argv0(pid)``, ``ppid(pid)`` and
``start(pid)`` (each None when the pid is gone). ``require_current`` is
the fencing helper for section 4; nothing else here calls it.
"""

import contextlib
import datetime
import fcntl
import json
import os
import re
import tempfile

from corral import paths

COORD_TTL_S = 900
LEASE_DIRNAME = "coord"
LEASE_NAME = "lease.json"
LOCK_NAME = "lease.lock"

_RUNTIME_NAMES = {"claude": "claude-code", "omp": "omp"}


class LeaseHeldError(Exception):
    """`acquire` refused: the lease is held by a live, fresh holder."""

    def __init__(self, message, lease):
        super().__init__(message)
        self.lease = lease


class StaleLeaseError(Exception):
    """A `heartbeat`/`release` generation is not the current one."""

    def __init__(self, message, current, given):
        super().__init__(message)
        self.current = current
        self.given = given


class UsageError(Exception):
    """Missing flag or bad value (maps to exit 4 at the CLI)."""


PROJECT_ENV = "CORRAL_PROJECT"
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def project_root(project=None, env=None):
    """``<state>/projects/<project>``: the one place a project's state lives.

    The project comes from ``project``, else ``CORRAL_PROJECT``. Neither is
    a usage error: a silent default would let two projects share one lease.
    The name must be a plain path segment.
    """
    env = os.environ if env is None else env
    name = project or env.get(PROJECT_ENV) or ""
    if not name:
        raise UsageError(f"no project: pass --project or set {PROJECT_ENV}")
    if not _PROJECT_NAME.match(name) or name in (".", ".."):
        raise UsageError(f"bad project name {name!r}: letters, digits, '.', '_', '-'")
    return os.path.join(paths.state_dir(env), "projects", name)


def lease_path(root=None):
    """Lease file path under the project root."""
    base = root if root is not None else project_root()
    return os.path.join(base, LEASE_DIRNAME, LEASE_NAME)


def lock_path(root=None):
    """Lock file path guarding the lease file."""
    base = root if root is not None else project_root()
    return os.path.join(base, LEASE_DIRNAME, LOCK_NAME)


def utcnow():
    """Current aware UTC time (module seam the tests patch via `now`)."""
    return datetime.datetime.now(datetime.timezone.utc)


def _as_moment(now=None):
    moment = now if now is not None else utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def _parse_time(value):
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


class RealProc:
    """Live ``/proc`` process table."""

    @staticmethod
    def comm(pid):
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None

    @staticmethod
    def argv0(pid):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                data = handle.read().split(b"\0")
        except OSError:
            return None
        if not data or not data[0]:
            return None
        try:
            return data[0].decode("utf-8", "replace")
        except ValueError:
            return None

    @staticmethod
    def _stat_after(pid):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return None
        cut = text.rfind(")")
        if cut < 0:
            return None
        return text[cut + 1 :].split()

    def ppid(self, pid):
        fields = self._stat_after(pid)
        if not fields or len(fields) < 2:
            return None
        try:
            return int(fields[1])
        except ValueError:
            return None

    def start(self, pid):
        fields = self._stat_after(pid)
        if not fields or len(fields) < 20:
            return None
        return fields[19]


def _runtime_of(comm, argv0):
    """Runtime for one process: claude->claude-code, omp->omp, else None."""
    names = set()
    if comm:
        names.add(comm.strip())
    if argv0:
        names.add(os.path.basename(argv0))
        # A rewritten process title packs argv into one string
        # (Claude Code background sessions: "claude bg-spare ...").
        head = argv0.split()
        if head:
            names.add(os.path.basename(head[0]))
    if "claude" in names:
        return _RUNTIME_NAMES["claude"]
    if "omp" in names:
        return _RUNTIME_NAMES["omp"]
    return None


def find_holder(self_pid=None, proc=None):
    """Nearest ancestor that is a coordinator runtime, or (None, None).

    Walks parents from `self_pid` (default: this process). Returns
    ``(pid, runtime)``; runtime is ``claude-code`` or ``omp``.
    """
    table = proc if proc is not None else RealProc()
    current = self_pid if self_pid is not None else os.getpid()
    try:
        parent = table.ppid(current)
    except Exception:
        return None, None
    for _ in range(64):
        if parent is None or parent <= 1:
            return None, None
        try:
            runtime = _runtime_of(table.comm(parent), table.argv0(parent))
        except Exception:
            return None, None
        if runtime is not None:
            return parent, runtime
        try:
            parent = table.ppid(parent)
        except Exception:
            return None, None
    return None, None


def pid_start_of(pid, proc=None):
    """Start-time token for `pid` (``/proc`` stat field 22), else None."""
    table = proc if proc is not None else RealProc()
    try:
        return table.start(pid)
    except Exception:
        return None


def _holder_identity(holder, self_pid=None, proc=None):
    """(holder id, runtime, pid, pid_start) for an acquire/takeover."""
    pid, runtime = find_holder(self_pid=self_pid, proc=proc)
    if pid is None:
        pid = self_pid if self_pid is not None else os.getpid()
        runtime = "unknown"
    start = pid_start_of(pid, proc=proc)
    if holder is None:
        holder = f"{runtime}:{pid}"
    return holder, runtime, pid, start


@contextlib.contextmanager
def _locked(exclusive=True, root=None):
    path = lock_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_lease(root=None):
    try:
        with open(lease_path(root), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "corrupt"
    return data if isinstance(data, dict) else "corrupt"


def _write_lease(data, root=None):
    path = lease_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".lease-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _heartbeat_age_s(lease, now):
    if not isinstance(lease, dict):
        return None
    moment = _parse_time(lease.get("heartbeat_at"))
    if moment is None:
        return None
    return int((now - moment).total_seconds())


def _pid_alive(lease, proc=None):
    if not isinstance(lease, dict):
        return False
    pid = lease.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    current_start = pid_start_of(pid, proc=proc)
    if current_start is None:
        return False
    return current_start == lease.get("pid_start")


def describe(root=None, now=None, proc=None):
    """Lease status dict with exactly the `status --json` keys."""
    moment = _as_moment(now)
    with _locked(exclusive=False, root=root):
        data = _read_lease(root)
    if data is None:
        return {
            "free": True,
            "reason": "no-lease",
            "holder": None,
            "runtime": None,
            "pid": None,
            "pid_alive": False,
            "heartbeat_age_s": None,
            "generation": 0,
        }
    if data == "corrupt":
        return {
            "free": True,
            "reason": "corrupt",
            "holder": None,
            "runtime": None,
            "pid": None,
            "pid_alive": False,
            "heartbeat_age_s": None,
            "generation": 0,
        }
    generation = data.get("generation")
    generation = generation if isinstance(generation, int) else 0
    base = {
        "holder": data.get("holder"),
        "runtime": data.get("runtime"),
        "pid": data.get("pid"),
        "generation": generation,
    }
    if data.get("holder") is None:
        return {
            **base,
            "free": True,
            "reason": "released",
            "pid_alive": False,
            "heartbeat_age_s": None,
        }
    alive = _pid_alive(data, proc=proc)
    if not alive:
        pid = data.get("pid")
        gone = pid_start_of(pid, proc=proc) if isinstance(pid, int) else None
        reason = "pid-reused" if gone is not None else "dead-pid"
        return {
            **base,
            "free": True,
            "reason": reason,
            "pid_alive": False,
            "heartbeat_age_s": _heartbeat_age_s(data, moment),
        }
    age = _heartbeat_age_s(data, moment)
    if age is None or age > COORD_TTL_S:
        return {
            **base,
            "free": True,
            "reason": "stale-heartbeat",
            "pid_alive": True,
            "heartbeat_age_s": age,
        }
    return {**base, "free": False, "reason": "held", "pid_alive": True, "heartbeat_age_s": age}


def acquire(holder=None, root=None, now=None, proc=None, self_pid=None):
    """Take the lease when free; else raise `LeaseHeldError`."""
    moment = _as_moment(now)
    with _locked(exclusive=True, root=root):
        data = _read_lease(root)
        live = None
        if isinstance(data, dict) and data.get("holder") is not None:
            if _pid_alive(data, proc=proc):
                age = _heartbeat_age_s(data, moment)
                if age is not None and age <= COORD_TTL_S:
                    live = data
        if live is not None:
            raise LeaseHeldError(
                f"lease held by {live.get('holder')} "
                f"since {live.get('acquired_at')} "
                f"(generation {live.get('generation')})",
                live,
            )
        last = 0
        if isinstance(data, dict) and isinstance(data.get("generation"), int):
            last = data["generation"]
        ident, runtime, pid, start = _holder_identity(holder, self_pid=self_pid, proc=proc)
        stamp = moment.isoformat()
        lease = {
            "holder": ident,
            "runtime": runtime,
            "pid": pid,
            "pid_start": start,
            "acquired_at": stamp,
            "heartbeat_at": stamp,
            "generation": last + 1,
        }
        _write_lease(lease, root)
        return lease


def _current_generation(root=None):
    data = _read_lease(root)
    if isinstance(data, dict) and isinstance(data.get("generation"), int):
        return data["generation"]
    return 0


def _stale_message(current):
    return f"not the active coordinator (lease generation {current} is current)"


def heartbeat(generation, root=None, now=None, proc=None):
    """Refresh `heartbeat_at` when `generation` is current."""
    _ = proc
    moment = _as_moment(now)
    with _locked(exclusive=True, root=root):
        data = _read_lease(root)
        current = (
            _current_generation(root)
            if not isinstance(data, dict)
            else (data.get("generation") if isinstance(data.get("generation"), int) else 0)
        )
        if generation != current:
            raise StaleLeaseError(_stale_message(current), current, generation)
        if not isinstance(data, dict) or data.get("holder") is None:
            raise StaleLeaseError(_stale_message(current), current, generation)
        data["heartbeat_at"] = moment.isoformat()
        _write_lease(data, root)
        return data


def release(generation, root=None, now=None, proc=None):
    """Drop the lease when `generation` is current; keep the generation."""
    _ = (now, proc)
    with _locked(exclusive=True, root=root):
        data = _read_lease(root)
        current = (
            data.get("generation")
            if isinstance(data, dict) and isinstance(data.get("generation"), int)
            else 0
        )
        if generation != current:
            raise StaleLeaseError(_stale_message(current), current, generation)
        if not isinstance(data, dict) or data.get("holder") is None:
            raise StaleLeaseError(_stale_message(current), current, generation)
        kept = {"holder": None, "generation": current}
        _write_lease(kept, root)
        return kept


def takeover(holder=None, force=False, root=None, now=None, proc=None, self_pid=None):
    """Take the lease even when held; `force` is required (CLI enforces)."""
    if not force:
        raise UsageError("takeover needs --force")
    moment = _as_moment(now)
    with _locked(exclusive=True, root=root):
        current = _current_generation(root)
        ident, runtime, pid, start = _holder_identity(holder, self_pid=self_pid, proc=proc)
        stamp = moment.isoformat()
        lease = {
            "holder": ident,
            "runtime": runtime,
            "pid": pid,
            "pid_start": start,
            "acquired_at": stamp,
            "heartbeat_at": stamp,
            "generation": current + 1,
        }
        _write_lease(lease, root)
        return lease


def require_current(generation, root=None, now=None, proc=None):
    """Fencing helper (section 4): raise unless `generation` is current."""
    _ = (now, proc)
    with _locked(exclusive=False, root=root):
        current = _current_generation(root)
    if generation != current:
        raise StaleLeaseError(_stale_message(current), current, generation)
    return current


JOURNAL_NAME = "journal.jsonl"
JOURNAL_LOCK_NAME = "journal.lock"
JOURNAL_ARCHIVE_PREFIX = "journal-"
JOURNAL_MAX = 500
ARCHIVE_KEEP_DAYS = 14
KINDS = ("decision", "action", "incident", "followup", "done", "handoff", "note")
HUMAN_PR_LABELS = frozenset({"needs-decision", "needs-human"})
#: The coordinator's rules: the agent overlay in the ops root (onboarding, 3a).
RULES_PATH = "agents.overlay.md"


def journal_path(root=None):
    """Live logbook file under the project root."""
    base = root if root is not None else project_root()
    return os.path.join(base, LEASE_DIRNAME, JOURNAL_NAME)


def journal_lock_path(root=None):
    """Lock file guarding the logbook and its archives."""
    base = root if root is not None else project_root()
    return os.path.join(base, LEASE_DIRNAME, JOURNAL_LOCK_NAME)


@contextlib.contextmanager
def _journal_locked(exclusive=True, root=None):
    path = journal_lock_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _parse_entry(line):
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _read_entries(root=None):
    try:
        with open(journal_path(root), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        if not line.strip():
            continue
        entry = _parse_entry(line)
        if entry is not None:
            entries.append(entry)
    return entries


def read_entries(root=None):
    """Live logbook entries, oldest first (shared lock, no rotation)."""
    with _journal_locked(exclusive=False, root=root):
        return _read_entries(root)


def open_followup_ids(entries):
    """Ids opened by a `followup` entry and not closed by a `done`."""
    opened = [e.get("id") for e in entries if e.get("kind") == "followup" and e.get("id")]
    closed = {e.get("id") for e in entries if e.get("kind") == "done" and e.get("id")}
    return [i for i in opened if i not in closed]


def open_followups(entries):
    """Open `followup` entries, oldest first."""
    wanted = set(open_followup_ids(entries))
    return [e for e in entries if e.get("kind") == "followup" and e.get("id") in wanted]


def newest_handoff(entries):
    """Newest `handoff` entry, or None."""
    for entry in reversed(entries):
        if entry.get("kind") == "handoff":
            return entry
    return None


def _archive_stamp(entry):
    at = entry.get("at") or ""
    return at[:10] if len(at) >= 10 else ""


def _prune_archives(root, now):
    day = _as_moment(now).date()
    kept, removed = [], []
    directory = os.path.join(root if root is not None else project_root(), LEASE_DIRNAME)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return kept, removed
    for name in names:
        if not (name.startswith(JOURNAL_ARCHIVE_PREFIX) and name.endswith(".jsonl")):
            continue
        stamp = name[len(JOURNAL_ARCHIVE_PREFIX) : -len(".jsonl")]
        try:
            file_day = datetime.date.fromisoformat(stamp)
        except ValueError:
            continue
        if (day - file_day).days > ARCHIVE_KEEP_DAYS:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(directory, name))
            removed.append(name)
        else:
            kept.append(name)
    return kept, removed


def _rotate(entries, root, now):
    """Rotation inside the append lock; returns (live, archived_names).

    Past 500 entries the oldest move to `journal-YYYY-MM-DD.jsonl` by
    entry date; archives older than 14 days are deleted. Open follow-ups
    are never rotated out: they stay in the live file.
    """
    base = root if root is not None else project_root()
    pruned = _prune_archives(base if root is not None else None, now)[1]
    if len(entries) <= JOURNAL_MAX:
        return entries, pruned
    wanted = set(open_followup_ids(entries))
    pinned = [e for e in entries if e.get("kind") == "followup" and e.get("id") in wanted]
    rest = [e for e in entries if not (e.get("kind") == "followup" and e.get("id") in wanted)]
    keep_rest = max(0, JOURNAL_MAX - len(pinned))
    live_rest = rest[-keep_rest:] if keep_rest else []
    live_ids = {id(e) for e in pinned} | {id(e) for e in live_rest}
    live = [e for e in entries if id(e) in live_ids]
    by_date = {}
    today = _as_moment(now).date().isoformat()
    for entry in entries:
        if id(entry) in live_ids:
            continue
        by_date.setdefault(_archive_stamp(entry) or today, []).append(entry)
    names = list(pruned)
    for stamp, rows in sorted(by_date.items()):
        path = os.path.join(base, LEASE_DIRNAME, f"{JOURNAL_ARCHIVE_PREFIX}{stamp}.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            for entry in rows:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        names.append(os.path.basename(path))
    return live, sorted(set(names))


def _write_entries(entries, root=None):
    path = journal_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=os.path.dirname(path), delete=False
    )
    try:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(handle.name)
        raise


def new_followup_id():
    """Fresh `followup` id: 8 hex chars."""
    return os.urandom(4).hex()


def append_entry(
    kind, text, refs=(), holder="", generation=None, entry_id=None, root=None, now=None
):
    """Append one logbook entry; rotation runs in the same lock."""
    if kind not in KINDS:
        raise UsageError(f"unknown kind {kind!r} (one of {', '.join(KINDS)})")
    moment = _as_moment(now).isoformat()
    entry = {
        "at": moment,
        "holder": holder,
        "generation": generation,
        "kind": kind,
        "refs": list(refs),
        "text": text,
    }
    if entry_id is not None:
        entry["id"] = entry_id
    with _journal_locked(exclusive=True, root=root):
        entries = _read_entries(root)
        entries.append(entry)
        live, _ = _rotate(entries, root, now)
        _write_entries(live, root)
    return entry


def followup_add(text, refs=(), holder="", generation=None, root=None, now=None):
    """Open a follow-up; returns the entry with its 8-hex-char id."""
    return append_entry(
        "followup",
        text,
        refs=refs,
        holder=holder,
        generation=generation,
        entry_id=new_followup_id(),
        root=root,
        now=now,
    )


def followup_done(entry_id, holder="", generation=None, root=None, now=None):
    """Close a follow-up: a `done` entry carrying the id it closes."""
    return append_entry(
        "done",
        f"closed follow-up {entry_id}",
        holder=holder,
        generation=generation,
        entry_id=entry_id,
        root=root,
        now=now,
    )


def is_human_pr(pr):
    """A PR waiting on the human: it carries one of HUMAN_PR_LABELS."""
    labels = pr.get("labels") or []
    names = {row.get("name") if isinstance(row, dict) else row for row in labels}
    return bool(names & set(HUMAN_PR_LABELS))


def brief(
    root=None,
    now=None,
    proc=None,
    entries_n=30,
    snapshot_fn=None,
    prs_fn=None,
    rules_path=RULES_PATH,
    events_fn=None,
):
    """Everything a fresh coordinator needs; makes no write."""
    lease = describe(root=root, now=now, proc=proc)
    with _journal_locked(exclusive=False, root=root):
        entries = _read_entries(root)
    snapshot = snapshot_fn() if snapshot_fn is not None else {}
    prs = prs_fn() if prs_fn is not None else []
    if events_fn is not None:
        events = events_fn()
    else:
        try:
            from . import coordevents as _events

            events = _events.unresolved(root=root)
        except Exception:
            events = []
    return {
        "lease": lease,
        "handoff": newest_handoff(entries),
        "followups": open_followups(entries),
        "entries": entries[-entries_n:] if entries_n > 0 else [],
        "attention": snapshot.get("attention", []),
        "fleet": snapshot.get("fleet", {}),
        "human_prs": [p for p in prs if is_human_pr(p)],
        "events": events or [],
        "rules_path": rules_path,
    }


def render_brief_text(data):
    """Text `brief`: lease, handoff, follow-ups, entries, snapshot, PRs."""
    lines = []
    lease = data.get("lease") or {}
    if lease.get("free"):
        lines.append(f"lease: free ({lease.get('reason')}, generation {lease.get('generation')})")
    else:
        lines.append(
            f"lease: held by {lease.get('holder')} "
            f"(runtime {lease.get('runtime')}, pid {lease.get('pid')}, "
            f"heartbeat {lease.get('heartbeat_age_s')}s ago, "
            f"generation {lease.get('generation')})"
        )
    handoff = data.get("handoff")
    if handoff is None:
        lines.append("handoff: none")
    else:
        lines.append(f"handoff [{handoff.get('at')}]: {handoff.get('text')}")
    followups = data.get("followups") or []
    if not followups:
        lines.append("follow-ups: none open")
    else:
        lines.append("follow-ups:")
        for entry in followups:
            refs = " [" + ", ".join(entry.get("refs") or []) + "]" if entry.get("refs") else ""
            lines.append(f"- {entry.get('id')}: {entry.get('text')}{refs}")
    entries = data.get("entries") or []
    lines.append(f"entries (last {len(entries)}):")
    for entry in entries:
        lines.append(f"- [{entry.get('at')}] {entry.get('kind')}: {entry.get('text')}")
    for item in data.get("attention") or []:
        first = item.get("question") or item.get("reason") or item.get("title") or ""
        first = first.splitlines()[0][:100] if first else ""
        lines.append(f"! {item.get('kind')} {item.get('ref')}: {first}")
    events = data.get("events") or []
    if not events:
        lines.append("events: none waiting")
    else:
        lines.append("events:")
        for event in events:
            first = str(event.get("evidence") or "").splitlines()
            first = first[0][:100] if first and first[0] else ""
            lines.append(f"- {event.get('id')} {event.get('kind')} {event.get('ref')}: {first}")
    fleet = data.get("fleet") or {}
    lines.append(
        f"fleet: board_open={fleet.get('board_open', 0)} "
        f"prs_open={fleet.get('prs_open', 0)} "
        f"units_active={fleet.get('units_active', 0)}"
    )
    prs = data.get("human_prs") or []
    if not prs:
        lines.append("human PRs: none waiting")
    else:
        lines.append("human PRs:")
        for pr in prs:
            lines.append(
                f"- {pr.get('repo', '')}#{pr.get('number')}: "
                f"{pr.get('title', '')} {pr.get('url', '')}".rstrip()
            )
    lines.append(f"rules: {data.get('rules_path')}")
    return "\n".join(lines) + "\n"
