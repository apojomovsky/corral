"""The runtime-neutral agent registry: who is running on this host.

Agents start from many places: corral's own units, a coordinator's
subagents, another runtime, a human. Any of them can ``register``,
``heartbeat`` and ``done``, so every one of them is visible, not only
the units corral launched.

State is one JSON file per agent under ``<state>/agents/``, written
atomically under one flock, so concurrent hooks never lose a write.

Liveness is pure (``status``): a dead ``pid`` on this host is ``gone`` at
once; no heartbeat for ``registry.stale_s`` reads ``stale``, for
``registry.gone_s`` ``gone`` (dropped, and its file removed on the next
write). A ``done`` entry is ``finished`` for ``RETAIN_S``.

A ticket is plain data (``project``, ``repo``, ``number``), never a
tracker's reference syntax. Cap accounting: a live agent on this host
holding a ticket counts toward the host's cap when
``registry.count_toward_cap`` is on; one ticket is one slot however many
agents hold it. A heartbeat or done for an agent already dropped is an
unknown id (exit 4): the agent registers again. Agents with
no ticket (reviewers, scouts, a coordinator) show but never count.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import time

from corral import paths, settings

DIR_NAME = "agents"
LOCK_NAME = ".lock"
RUNTIMES = ("claude-code", "omp", "opencode", "codex", "human")
OUTCOMES = ("ok", "failed", "stopped")
#: A finished or gone file is deleted after this long.
RETAIN_S = 24 * 60 * 60

LIVE, STALE, FINISHED, GONE = "live", "stale", "finished", "gone"

#: Where an entry's ticket came from. ``explicit`` (``register``, the
#: dispatch preamble) is authoritative; ``inferred`` (a hook guessing it
#: from the worktree) fills an empty slot or corrects an older inference,
#: never an explicit ticket.
EXPLICIT, INFERRED = "explicit", "inferred"

settings.register(
    "registry.stale_s", int, 10 * 60, "seconds without a heartbeat before an agent reads stale"
)
settings.register(
    "registry.gone_s", int, 60 * 60, "seconds without a heartbeat before an agent is dropped"
)
settings.register(
    "registry.count_toward_cap", bool, True, "live registered ticket workers take cap slots"
)

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class RegistryError(ValueError):
    """Bad input or an unknown id: the CLI maps it to exit 4."""


def config(resolved=None):
    """``{"stale_s", "gone_s", "count_toward_cap"}`` from the settings.

    Raises SettingsError when a value is out of range, or gone_s is
    below stale_s.
    """
    resolved = resolved if resolved is not None else settings.load()
    cfg = {
        "stale_s": resolved["registry.stale_s"],
        "gone_s": resolved["registry.gone_s"],
        "count_toward_cap": resolved["registry.count_toward_cap"],
    }
    for key in ("stale_s", "gone_s"):
        if cfg[key] <= 0:
            raise settings.SettingsError(f"registry.{key} must be a positive number of seconds")
    if cfg["gone_s"] < cfg["stale_s"]:
        raise settings.SettingsError("registry.gone_s must not be below registry.stale_s")
    return cfg


def default_config():
    return {
        key.split(".", 1)[1]: settings.REGISTRY[key].default
        for key in settings.REGISTRY
        if key.startswith("registry.")
    }


def agents_dir(path=None):
    return path or os.path.join(paths.state_dir(), DIR_NAME)


def _as_epoch(now):
    """``now`` as epoch seconds; datetimes are converted."""
    if isinstance(now, (int, float)):
        return float(now)
    return float(now.timestamp())


def _file_for(directory, agent_id):
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", agent_id)[:80].lstrip(".") or "_"
    if slug != agent_id:
        digest = hashlib.sha1(agent_id.encode()).hexdigest()[:8]
        slug = f"{slug}-{digest}"
    return os.path.join(directory, slug + ".json")


@contextlib.contextmanager
def _locked(directory):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, LOCK_NAME), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
        return None
    for key in ("started_at", "last_seen", "ended_at"):
        value = entry.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return None
    pid = entry.get("pid")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
        return None
    return entry


def _write(path, entry):
    paths.atomic_write_text(path, json.dumps(entry, sort_keys=True))


def _load_files(directory):
    """``[(file, entry)]`` for every readable entry, oldest start first."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    found = []
    for name in names:
        if name.endswith(".json"):
            target = os.path.join(directory, name)
            entry = _read(target)
            if entry is not None:
                found.append((target, entry))
    found.sort(key=lambda pair: (pair[1].get("started_at") or 0, pair[1]["id"]))
    return found


def load_all(path=None):
    """Every readable entry, oldest start first. Corrupt files are skipped."""
    return [entry for _, entry in _load_files(agents_dir(path))]


def pid_alive(pid):
    """Whether ``pid`` exists on this host (EPERM means it does).

    Signal 0 only probes; nothing is sent to the process.
    """
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    return True


def status(entry, now, cfg=None, alive=pid_alive, host=None):
    """``live`` | ``stale`` | ``finished`` | ``gone`` for one entry (pure).

    A pid is probed only when the entry was registered from this host.
    """
    cfg = cfg or default_config()
    moment = _as_epoch(now)
    last = entry.get("last_seen") or entry.get("started_at") or 0
    if entry.get("ended_at") is not None:
        return GONE if moment - entry["ended_at"] > RETAIN_S else FINISHED
    pid = entry.get("pid")
    if pid and entry.get("host") == (host or socket.gethostname()) and not alive(pid):
        return GONE
    age = moment - last
    if age >= cfg["gone_s"]:
        return GONE
    if age >= cfg["stale_s"]:
        return STALE
    return LIVE


def ticket_of(entry):
    """``(repo, number)`` an entry holds, or None.

    The cap key leaves the project out: an explicit ticket names it and a
    hook's inference may not, and one ticket must stay one slot. Two
    projects on one host sharing a repository name share its slots.
    """
    ticket = entry.get("ticket")
    if not isinstance(ticket, dict) or not ticket.get("repo") or not ticket.get("number"):
        return None
    return (ticket["repo"], ticket["number"])


def make_ticket(repo=None, number=None, project=None):
    """A validated ticket dict, or None when no repo and number are given."""
    if repo in (None, "") and number in (None, ""):
        if project:
            raise RegistryError("a ticket's project needs its repo and number too")
        return None
    repo = str(repo or "")
    if not _NAME.fullmatch(repo):
        raise RegistryError(f"bad repo {repo!r}: want a plain repository name")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise RegistryError(f"bad ticket number {number!r}: want a positive integer")
    if project not in (None, "") and not _NAME.fullmatch(str(project)):
        raise RegistryError(f"bad project {project!r}: want a plain name")
    return {"project": project or "", "repo": repo, "number": number}


def _check_ticket(ticket):
    if ticket is None:
        return None
    if not isinstance(ticket, dict) or set(ticket) - {"project", "repo", "number"}:
        raise RegistryError(f"bad ticket {ticket!r}: want project, repo and number")
    checked = make_ticket(ticket.get("repo"), ticket.get("number"), ticket.get("project"))
    if checked is None:
        raise RegistryError("a ticket needs its repo and number")
    return checked


def _gc(directory, now, cfg, alive):
    # The file that was read is the one removed, never a name derived
    # from its contents: a copied or hand-edited file must not take a
    # live agent's file with it.
    for target, entry in _load_files(directory):
        if status(entry, now, cfg, alive) == GONE:
            try:
                os.unlink(target)
            except OSError:
                pass


def register(
    agent_id,
    runtime,
    *,
    ticket=None,
    model=None,
    parent=None,
    pid=None,
    transcript=None,
    note=None,
    now=None,
    path=None,
    cfg=None,
    alive=pid_alive,
    host=None,
):
    """Record a live agent; idempotent (a re-register refreshes it).

    A re-register keeps the original ``started_at`` while the agent has
    not ended; after ``done`` it starts a fresh run. A ``ticket`` here is
    explicit; a re-register without one keeps the previous ticket and its
    source, so a hook retry never drops an inferred ticket.
    """
    if not agent_id or not agent_id.strip():
        raise RegistryError("agent id must not be empty")
    if runtime not in RUNTIMES:
        raise RegistryError(f"unknown runtime {runtime!r}: want one of {', '.join(RUNTIMES)}")
    ticket = _check_ticket(ticket)
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid < 1):
        raise RegistryError(f"bad pid {pid!r}: want a positive integer")
    moment = _as_epoch(time.time() if now is None else now)
    directory = agents_dir(path)
    with _locked(directory):
        target = _file_for(directory, agent_id)
        old = _read(target)
        running = old is not None and old.get("ended_at") is None
        started = old["started_at"] if running and old.get("started_at") else moment
        if ticket is None and running:
            ticket = old.get("ticket")
            source = old.get("ticket_source") if ticket else None
        else:
            source = EXPLICIT if ticket else None
        entry = {
            "id": agent_id,
            "runtime": runtime,
            "ticket": ticket,
            "ticket_source": source,
            "model": model,
            "parent": parent,
            "pid": pid,
            "transcript": transcript,
            "host": host or socket.gethostname(),
            "started_at": started,
            "last_seen": moment,
            "note": note or "",
            "ended_at": None,
            "outcome": None,
            "pr": None,
        }
        _write(target, entry)
        _gc(directory, moment, cfg or default_config(), alive)
    return entry


def _update(agent_id, path, change):
    directory = agents_dir(path)
    with _locked(directory):
        target = _file_for(directory, agent_id)
        entry = _read(target)
        if entry is None or entry.get("id") != agent_id:
            raise RegistryError(f"unknown agent {agent_id!r}")
        change(entry)
        _write(target, entry)
    return entry


def heartbeat(agent_id, *, note=None, ticket=None, now=None, path=None):
    """Refresh last-seen, the activity note, and an inferred ticket.

    ``ticket`` here is a hook's guess: it fills an empty slot or corrects
    an older inference, but an explicit ``register`` ticket always wins.
    """
    moment = _as_epoch(time.time() if now is None else now)
    ticket = _check_ticket(ticket)

    def change(entry):
        if entry.get("ended_at") is not None:
            raise RegistryError(f"agent {agent_id!r} already done")
        entry["last_seen"] = moment
        if note is not None:
            entry["note"] = note
        if ticket is not None and (
            not entry.get("ticket") or entry.get("ticket_source") == INFERRED
        ):
            entry["ticket"] = ticket
            entry["ticket_source"] = INFERRED

    return _update(agent_id, path, change)


def done(agent_id, *, outcome="ok", pr=None, now=None, path=None):
    """Close the entry; it reads ``finished`` until RETAIN_S passes."""
    if outcome not in OUTCOMES:
        raise RegistryError(f"unknown outcome {outcome!r}: want one of {', '.join(OUTCOMES)}")
    moment = _as_epoch(time.time() if now is None else now)

    def change(entry):
        if entry.get("ended_at") is not None:
            raise RegistryError(f"agent {agent_id!r} already done ({entry.get('outcome')})")
        entry["last_seen"] = moment
        entry["ended_at"] = moment
        entry["outcome"] = outcome
        if pr:
            entry["pr"] = pr

    return _update(agent_id, path, change)


def visible(now, cfg=None, path=None, alive=pid_alive, host=None):
    """``[(entry, status)]`` for entries that are not gone."""
    cfg = cfg or config()
    out = []
    for entry in load_all(path):
        state = status(entry, now, cfg, alive, host)
        if state != GONE:
            out.append((entry, state))
    return out


def counted_tickets(now=None, cfg=None, path=None, alive=pid_alive, host=None):
    """``{(repo, number)}`` of live registered workers that count toward the cap.

    Only a live entry holding a ticket counts: stale, finished and gone
    ones free the slot, and agents without a ticket never take one. Only
    this host's entries count: the cap is a host's.
    """
    host = host or socket.gethostname()
    cfg = cfg or config()
    if not cfg["count_toward_cap"]:
        return set()
    moment = time.time() if now is None else now
    out = set()
    for entry, state in visible(moment, cfg, path, alive, host):
        held = ticket_of(entry)
        if state == LIVE and held and entry.get("host") == host:
            out.add(held)
    return out
