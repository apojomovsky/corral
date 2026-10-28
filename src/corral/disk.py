"""Host disk pressure, a reclaim plan, and the launch gate.

The pressure reading and the reclaim plan are pure over injected seams;
only `diskcli` builds the production ones. Nothing here deletes: `plan`
lists candidates, `apply` removes only the safe `pr-body` and `session`
rows after re-verifying each proof. Worktree reclaim stays with
`corral gc --apply`, which owns those proofs.
"""

import os
import re
import shutil
from dataclasses import dataclass, field

from corral import paths, registry, settings

#: Pressure levels. `unknown` (a path that cannot be read) blocks nothing.
OK = "ok"
LOW = "low"
CRITICAL = "critical"
UNKNOWN = "unknown"

#: Candidate kinds.
PR_BODY = "pr-body"
SESSION = "session"
WORKTREE = "worktree"
UNPROVEN = "unproven"

GB = 1024**3

#: A `pr-body` draft older than this is stale enough to reclaim.
PR_BODY_MAX_AGE_S = 7 * 24 * 60 * 60

#: Immediate subdirectories above this size with no reclaim rule are listed.
BIG_DIR_BYTES = GB

_PR_BODY_RE = re.compile(r"pr-[0-9A-Za-z_.-]+\.md\Z")

settings.register("disk.min_free_pct", int, 10, "free-space percent under which the disk reads low")
settings.register("disk.min_free_gb", int, 5, "free gigabytes under which the disk reads low")
settings.register(
    "disk.session_retention_days",
    int,
    14,
    "days before a finished ticket's session dir is a reclaim candidate",
)


@dataclass(frozen=True)
class Reading:
    """One filesystem's free space. `total`/`free` are None when unreadable."""

    path: str
    total: int | None
    free: int | None


@dataclass(frozen=True)
class Candidate:
    """One reclaim candidate: what, where, how big, why, and whether proven."""

    kind: str
    path: str
    bytes: int
    proof: str
    safe: bool


@dataclass(frozen=True)
class Plan:
    """The pressure reading plus the reclaim candidates, largest first."""

    pressure: str
    readings: tuple
    candidates: tuple
    notes: tuple


@dataclass
class ApplyResult:
    """What `--apply` removed and what it kept, each kept row with its reason."""

    removed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)


def production_usage(path):
    """The live seam: `(total, used, free)` for the filesystem holding `path`."""
    used = shutil.disk_usage(path)
    return (used.total, used.used, used.free)


def read(dir_paths, *, usage):
    """One Reading per directory. `usage(path)` is required (no default).

    A path whose usage cannot be read becomes an unknown reading: it is
    reported, never blocking.
    """
    out = []
    for path in dir_paths:
        try:
            total, _used, free = usage(path)
        except Exception:  # noqa: BLE001 - any failure reads as unknown
            out.append(Reading(path, None, None))
            continue
        if not isinstance(total, int) or not isinstance(free, int) or total <= 0 or free < 0:
            out.append(Reading(path, None, None))
            continue
        out.append(Reading(path, total, free))
    return out


def _under(reading, pct, gb_bytes):
    if reading.free is None or reading.total is None:
        return False
    return reading.free / reading.total * 100 < pct or reading.free < gb_bytes


def pressure(readings, *, min_free_pct, min_free_gb):
    """The worst level over `readings`: `critical` under half of either
    threshold, `low` under either threshold, `unknown` when a path could
    not be read and nothing worse fired, else `ok`."""
    if any(_under(r, min_free_pct / 2, min_free_gb * GB / 2) for r in readings):
        return CRITICAL
    if any(_under(r, min_free_pct, min_free_gb * GB) for r in readings):
        return LOW
    if any(r.free is None or r.total is None for r in readings):
        return UNKNOWN
    return OK


def measure(path):
    """Production directory-size seam: bytes under `path`, one stat per file.

    Symlinks are never followed and the walk never crosses a mount boundary
    (entries on another device are skipped). Unreadable entries count as
    zero bytes rather than failing the plan.
    """
    try:
        root_dev = os.stat(path).st_dev
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                listing = list(entries)
        except OSError:
            continue
        for entry in listing:
            try:
                if entry.is_symlink():
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_dev != root_dev:
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            else:
                total += st.st_size
    return total


def newest_mtime(path):
    """The newest mtime under `path`, or None when nothing is readable."""
    try:
        latest = os.lstat(path).st_mtime
    except OSError:
        return None
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                listing = list(entries)
        except OSError:
            continue
        for entry in listing:
            try:
                if entry.is_symlink():
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_mtime > latest:
                latest = st.st_mtime
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
    return latest


def _inside(real, roots):
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def _done_tickets(entries, now):
    """`{(repo, number)}` whose every registry entry is finished or gone."""
    states: dict[tuple, list[str]] = {}
    for entry in entries:
        ticket = registry.ticket_of(entry)
        if ticket is None:
            continue
        states.setdefault(ticket, []).append(registry.status(entry, now))
    return {
        ticket
        for ticket, found in states.items()
        if found and all(s in (registry.FINISHED, registry.GONE) for s in found)
    }


def _session_dirname(ticket):
    repo, number = ticket
    return f"{repo}-{number}"


def plan(
    *,
    state_dir,
    cache_dir,
    sessions_dir,
    roots,
    min_free_pct,
    min_free_gb,
    session_retention_days,
    usage,
    walk,
    now,
    entries,
    list_fn,
    gc_rows,
):
    """The pressure reading plus the reclaim candidates, largest first.

    `usage(path)` and `walk(path)` are the required seams (no defaults).
    `entries` is `registry.load_all()` output, `list_fn()` returns the live
    `(repo, number)` tickets (None when the listing itself failed), and
    `gc_rows` is `[(path, proof, planned)]` carried from `gc.plan`. Paths
    that resolve outside `roots` are dropped with a note, never planned.
    """
    real_roots = tuple(os.path.realpath(r) for r in roots)
    readings = read([state_dir, cache_dir, sessions_dir], usage=usage)
    level = pressure(readings, min_free_pct=min_free_pct, min_free_gb=min_free_gb)
    candidates: list[Candidate] = []
    notes: list[str] = []
    cutoff = now - session_retention_days * 24 * 60 * 60

    try:
        names = sorted(os.listdir(cache_dir))
    except OSError:
        names = []
    for name in names:
        if not _PR_BODY_RE.fullmatch(name):
            continue
        path = os.path.join(cache_dir, name)
        try:
            is_link = os.path.islink(path)
            is_file = os.path.isfile(path) and not is_link
        except OSError:
            continue
        real = os.path.realpath(path)
        if is_link:
            notes.append(f"dropped {path}: is a symlink")
            continue
        if not _inside(real, real_roots):
            notes.append(f"dropped {path}: resolves outside a declared root")
            continue
        if not is_file:
            continue
        try:
            age = now - os.stat(path).st_mtime
            size = os.stat(path).st_size
        except OSError:
            continue
        if age < PR_BODY_MAX_AGE_S:
            continue
        candidates.append(
            Candidate(
                PR_BODY,
                path,
                size,
                f"pr-body draft {name} untouched for {int(age // 86400)}d, "
                "a regular file inside the cache dir",
                True,
            )
        )

    try:
        live = list_fn()
    except Exception:  # noqa: BLE001 - an unreadable listing keeps everything
        live = None
    done = _done_tickets(entries, now)
    if live is None:
        notes.append("live units unreadable: session candidates are kept")
    session_names: set[str] = set()
    try:
        session_dirs = sorted(os.listdir(sessions_dir))
    except OSError:
        session_dirs = []
    for name in session_dirs:
        path = os.path.join(sessions_dir, name)
        try:
            is_link = os.path.islink(path)
            is_dir = os.path.isdir(path) and not is_link
        except OSError:
            continue
        real = os.path.realpath(path)
        if is_link:
            notes.append(f"dropped {path}: is a symlink")
            continue
        if not _inside(real, real_roots):
            notes.append(f"dropped {path}: resolves outside a declared root")
            continue
        if not is_dir:
            continue
        session_names.add(name)
        ticket = None
        for known in done:
            if _session_dirname(known) == name:
                ticket = known
                break
        if ticket is None:
            for entry in entries:
                held = registry.ticket_of(entry)
                if held is not None and _session_dirname(held) == name:
                    ticket = held
                    break
        if ticket is None:
            candidates.append(
                Candidate(
                    UNPROVEN,
                    path,
                    walk(path),
                    f"session dir {name} has no registry entry: cannot prove nothing is lost",
                    False,
                )
            )
            continue
        state = "done" if ticket in done else "open"
        is_live = live is not None and ticket in live
        latest = newest_mtime(path)
        old = latest is not None and latest < cutoff
        reasons = []
        if state != "done":
            reasons.append("the registry entry is still open")
        if live is None:
            reasons.append("the live listing failed")
        elif is_live:
            reasons.append("a live unit holds it")
        if latest is None:
            reasons.append("its modification time is unreadable")
        elif not old:
            reasons.append("it was modified within the retention")
        if state == "done" and not is_live and live is not None and old:
            candidates.append(
                Candidate(
                    SESSION,
                    path,
                    walk(path),
                    f"registry {state}; no live unit; "
                    f"untouched since retention ({session_retention_days}d)",
                    True,
                )
            )
        else:
            candidates.append(
                Candidate(SESSION, path, walk(path), f"kept: {'; '.join(reasons)}", False)
            )

    for gc_path, proof, planned in gc_rows:
        if not planned:
            continue
        real = os.path.realpath(gc_path)
        if not _inside(real, real_roots):
            notes.append(f"dropped {gc_path}: resolves outside a declared root")
            continue
        if os.path.islink(gc_path):
            notes.append(f"dropped {gc_path}: is a symlink")
            continue
        candidates.append(Candidate(WORKTREE, gc_path, walk(gc_path), proof, True))

    subdirs: list[str] = []
    for top in (state_dir, cache_dir, sessions_dir):
        try:
            top_names = sorted(os.listdir(top))
        except OSError:
            continue
        for name in top_names:
            if top == sessions_dir and name in session_names:
                continue
            path = os.path.join(top, name)
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    continue
            except OSError:
                continue
            if not _inside(os.path.realpath(path), real_roots):
                notes.append(f"dropped {path}: resolves outside a declared root")
                continue
            subdirs.append(path)
    for path in subdirs:
        size = walk(path)
        if size >= BIG_DIR_BYTES:
            candidates.append(
                Candidate(UNPROVEN, path, size, f"{size} bytes with no reclaim rule", False)
            )

    candidates.sort(key=lambda c: c.bytes, reverse=True)
    return Plan(level, tuple(readings), tuple(candidates), tuple(notes))


def apply(candidates, *, remove, roots, session_retention_days, now, entries, list_fn):
    """Remove only the safe `pr-body` and `session` candidates, re-verified.

    `remove(path)` is the required destruction seam (no default). Each
    removal re-verifies its proof just before deleting: regular file (not a
    symlink) inside a declared root and still older than 7 days, or a
    session dir inside a declared root whose entry is still done, with
    still no live unit and still older than the retention. Anything else
    is kept with its reason. Returns an ApplyResult.
    """
    real_roots = tuple(os.path.realpath(r) for r in roots)
    result = ApplyResult()
    try:
        live = list_fn()
    except Exception as err:  # noqa: BLE001 - an unreadable listing keeps everything
        live = None
        live_error = str(err) or "live listing failed"
    else:
        live_error = ""
    done = _done_tickets(entries, now)
    cutoff = now - session_retention_days * 24 * 60 * 60
    for candidate in candidates:
        if candidate.kind not in (PR_BODY, SESSION) or not candidate.safe:
            result.kept.append((candidate.path, "not eligible for --apply"))
            continue
        real = os.path.realpath(candidate.path)
        if not _inside(real, real_roots):
            result.kept.append((candidate.path, "resolves outside a declared root"))
            continue
        if os.path.islink(candidate.path):
            result.kept.append((candidate.path, "is a symlink"))
            continue
        if candidate.kind == PR_BODY:
            if not _PR_BODY_RE.fullmatch(os.path.basename(candidate.path)):
                result.kept.append((candidate.path, "no longer matches pr-*.md"))
                continue
            try:
                st = os.stat(candidate.path)
                is_file = os.path.isfile(candidate.path)
            except OSError:
                result.kept.append((candidate.path, "no longer readable"))
                continue
            if not is_file:
                result.kept.append((candidate.path, "not a regular file"))
                continue
            if now - st.st_mtime < PR_BODY_MAX_AGE_S:
                result.kept.append((candidate.path, "touched since the plan"))
                continue
        else:
            try:
                is_dir = os.path.isdir(candidate.path)
            except OSError:
                result.kept.append((candidate.path, "no longer readable"))
                continue
            if not is_dir:
                result.kept.append((candidate.path, "not a directory"))
                continue
            name = os.path.basename(candidate.path)
            ticket = next(
                (known for known in done if _session_dirname(known) == name),
                None,
            )
            if ticket is None:
                result.kept.append((candidate.path, "registry entry is no longer done"))
                continue
            if live is None:
                result.kept.append((candidate.path, f"live listing failed: {live_error}"))
                continue
            if ticket in live:
                result.kept.append((candidate.path, "a live unit holds it now"))
                continue
            latest = newest_mtime(candidate.path)
            if latest is None or latest >= cutoff:
                result.kept.append((candidate.path, "touched since the plan"))
                continue
        try:
            remove(candidate.path)
        except Exception as err:  # noqa: BLE001 - one failure must not stop the rest
            result.kept.append((candidate.path, str(err) or "removal failed"))
        else:
            result.removed.append(candidate.path)
    return result


def format_gb(value):
    """`value` bytes as `1.2 GiB`."""
    return f"{value / GB:.1f} GiB"


def refusal_text(resolved, *, usage):
    """The launch refusal when the pressure is critical, else None.

    `usage(path)` is the required seam (no default): unreadable paths read
    as unknown and never refuse.
    """
    readings = read([paths.state_dir(), paths.cache_dir(), paths.sessions_dir()], usage=usage)
    level = pressure(
        readings,
        min_free_pct=resolved["disk.min_free_pct"],
        min_free_gb=resolved["disk.min_free_gb"],
    )
    if level != CRITICAL:
        return None
    worst = min(
        (r for r in readings if r.free is not None),
        key=lambda r: r.free,
        default=None,
    )
    if worst is None:
        return "disk critically low, try later"
    return f"disk critically low, try later: {worst.path} has {format_gb(worst.free)} free"
