"""`corral snapshot`: one read-only view of the host.

Composes what already exists and adds no new state: the agent registry,
the quota pool and ledger, the admission caps and the host disks. Every
section is read independently, so one unreadable section errors while the
rest still print. Exits 0 when the document printed (even with section
errors), 3 when nothing at all could be read, 4 on a usage or settings
error. No GitHub, no network, no service control in this module.
"""

import argparse
import json
import os
import shutil
import socket
import sys
import time

OK, UNREACHABLE, USAGE = 0, 3, 4

#: The document schema version, printed as `schema_version`.
SCHEMA_VERSION = 1


def _err(text):
    print(f"corral snapshot: {text}", file=sys.stderr)


def _section_error(exc):
    """A failed section: `{"error": text}`, never an empty message."""
    text = str(exc) if str(exc) else repr(exc)
    return {"error": text}


def _agent_row(entry, state):
    """One agent row from a registry entry and its status (pure).

    Fields missing on the entry read as null, never absent.
    """
    from corral import registry

    ticket = entry.get("ticket")
    if not isinstance(ticket, dict):
        ticket = {}
    held = registry.ticket_of(entry)
    return {
        "id": entry.get("id"),
        "project": ticket.get("project") or None,
        "repo": held[0] if held else None,
        "number": held[1] if held else None,
        "runtime": entry.get("runtime"),
        "model": entry.get("model"),
        "state": state,
        "started_at": entry.get("started_at"),
        "last_seen": entry.get("last_seen"),
        "note": entry.get("note"),
    }


def _sort_key(row):
    """Sort rows by `(project, repo, number, id)`; tickets without a number sort last."""
    return (
        row["project"] or "",
        row["repo"] or "",
        row["number"] is None,
        row["number"] or 0,
        row["id"] or "",
    )


def _agents_and_header(registry_view):
    """`(fleet_header, agents)`: both error when the registry cannot be read."""
    rows = [_agent_row(entry, state) for entry, state in registry_view]
    rows.sort(key=_sort_key)
    header = {
        "host": None,  # filled in by build, which owns `host`
        "now": None,  # filled in by build, which owns `now`
        "agents_live": sum(1 for row in rows if row["state"] == "live"),
        "agents_stale": sum(1 for row in rows if row["state"] == "stale"),
        "agents_finished": sum(1 for row in rows if row["state"] == "finished"),
    }
    return header, rows


def _capacity(registry_view, settings, host):
    """`{total_cap, repo_cap, live, by_project, pending}` (pure).

    `live` counts counted tickets, not entries: one ticket holds one slot
    no matter how many entries name it, exactly like
    `registry.counted_tickets`; like it, only this host's entries count.
    `pending` is not tracked yet and always reads `[]`.
    """
    from corral import registry

    total_cap = settings["admission.total_cap"]
    repo_cap = settings["admission.repo_cap"]
    tickets = {}
    if settings.get("registry.count_toward_cap", True):
        for entry, state in registry_view:
            if state != registry.LIVE or entry.get("host") != host:
                continue
            held = registry.ticket_of(entry)
            if held is None or held in tickets:
                continue
            ticket = entry.get("ticket")
            project = ticket.get("project") if isinstance(ticket, dict) else None
            tickets[held] = project or ""
    by_project = {}
    for held in sorted(tickets, key=lambda held: (tickets[held], held[0], held[1])):
        by_project.setdefault(tickets[held], 0)
        by_project[tickets[held]] += 1
    return {
        "total_cap": total_cap,
        "repo_cap": repo_cap,
        "live": len(tickets),
        "by_project": by_project,
        "pending": [],
    }


def _disk(usage_fn):
    """`{paths: [...]}` for the state dir and the cache dir.

    A path that cannot be read gives `{path, error}`; an unresolvable
    directory gives a section `{"error": ...}`.
    """
    from corral import paths

    try:
        names = (paths.state_dir(), paths.cache_dir())
    except Exception as exc:  # noqa: BLE001 - one broken section must not break the rest
        return _section_error(exc)
    out = []
    for name in names:
        try:
            total, used, free = usage_fn(name)
        except Exception as exc:  # noqa: BLE001 - per-path failure, not a section failure
            out.append({"path": name, "error": str(exc) if str(exc) else repr(exc)})
        else:
            out.append({"path": name, "total": total, "used": used, "free": free})
    return {"paths": out}


def build(now, host, registry_view, quota_view, settings, usage_fn=None):
    """The snapshot document from plain data (pure, except disk paths).

    `registry_view` is `[(entry, status)]` as `registry.visible` returns,
    or the exception reading it raised. `quota_view` is the dict
    `quotacli.snapshot` returns, or the exception reading it raised.
    `settings` is a plain dict of the `admission.*` and
    `registry.count_toward_cap` values, or the exception reading it
    raised. `usage_fn(path)` reports `(total, used, free)` and defaults
    to `shutil.disk_usage`. Sections fail independently: a broken one
    reads `{"error": ...}` while the rest still print.
    """
    if usage_fn is None:
        usage_fn = shutil.disk_usage
    if isinstance(registry_view, Exception):
        header = agents = _section_error(registry_view)
    else:
        try:
            header, agents = _agents_and_header(registry_view)
        except Exception as exc:  # noqa: BLE001 - one broken section must not break the rest
            header = agents = _section_error(exc)
        else:
            header["host"] = host
            header["now"] = now
    if isinstance(quota_view, Exception):
        quota = _section_error(quota_view)
    else:
        quota = quota_view
    if isinstance(settings, Exception) or isinstance(registry_view, Exception):
        capacity = _section_error(settings if isinstance(settings, Exception) else registry_view)
    else:
        try:
            capacity = _capacity(registry_view, settings, host)
        except Exception as exc:  # noqa: BLE001 - one broken section must not break the rest
            capacity = _section_error(exc)
    try:
        disk = _disk(usage_fn)
    except Exception as exc:  # noqa: BLE001 - one broken section must not break the rest
        disk = _section_error(exc)
    return {
        "schema_version": SCHEMA_VERSION,
        "fleet_header": header,
        "agents": agents,
        "quota": quota,
        "capacity": capacity,
        "disk": disk,
    }


def _all_failed(doc):
    """Whether every readable section failed: nothing at all could be read."""
    return all(
        isinstance(doc.get(section), dict) and set(doc[section]) == {"error"}
        for section in ("agents", "quota", "capacity", "disk")
    )


def _show(doc):
    """The human view: one short table per section, agents first."""
    agents = doc.get("agents")
    if isinstance(agents, list):
        header = doc.get("fleet_header") or {}
        print(
            f"agents: {header.get('agents_live', 0)} live,"
            f" {header.get('agents_stale', 0)} stale,"
            f" {header.get('agents_finished', 0)} finished"
        )
        for row in agents:
            if row["repo"] is None:
                ticket = "-"
            else:
                project = f"{row['project']}/" if row["project"] else ""
                ticket = f"{project}{row['repo']} #{row['number']}"
            note = f"  {row['note']}" if row.get("note") else ""
            print(f"  {row['id']}  {row['runtime']}  {row['state']}  {ticket}{note}")
    else:
        _err(
            f"agents: {agents.get('error', 'unreadable') if isinstance(agents, dict) else 'unreadable'}"
        )
    header = doc.get("fleet_header")
    if isinstance(header, dict) and "host" in header:
        print(f"host: {header['host']}  now: {header['now']}")
    quota = doc.get("quota")
    if isinstance(quota, dict) and "providers" in quota:
        states = [
            f"{name}={info.get('state')}" for name, info in sorted(quota["providers"].items())
        ]
        print(f"quota: {', '.join(states) if states else 'no providers'}")
    else:
        _err(
            f"quota: {quota.get('error', 'unreadable') if isinstance(quota, dict) else 'unreadable'}"
        )
    capacity = doc.get("capacity")
    if isinstance(capacity, dict) and "live" in capacity:
        projects = ", ".join(
            f"{name or '-'}={count}" for name, count in sorted(capacity["by_project"].items())
        )
        print(
            f"capacity: {capacity['live']}/{capacity['total_cap']} live"
            f" (repo cap {capacity['repo_cap']})" + (f"  {projects}" if projects else "")
        )
    else:
        _err(
            f"capacity: {capacity.get('error', 'unreadable') if isinstance(capacity, dict) else 'unreadable'}"
        )
    disk = doc.get("disk")
    if isinstance(disk, dict) and "paths" in disk:
        for item in disk["paths"]:
            if "error" in item:
                print(f"  {item['path']}: {item['error']}")
            else:
                print(
                    f"  {item['path']}: total {item['total']} used {item['used']} free {item['free']}"
                )
    else:
        _err(f"disk: {disk.get('error', 'unreadable') if isinstance(disk, dict) else 'unreadable'}")


def _settings_root(start):
    """The ops root owning `start`, or None when no project claims it.

    The nearest directory above `start` holding `corral.toml` wins; else
    the registered project whose root or clone holds `start`. A broken
    registration or project file is skipped, never fatal: the caller
    falls back to the host defaults.
    """
    from corral import projects, settings

    root = settings.find_root(start)
    if root is not None:
        return root
    want = os.path.realpath(start)
    try:
        registered = projects.load_all()
    except projects.ProjectsError:
        return None
    for project in registered:
        if _holds(project.root, want):
            return project.root
    return None


def _holds(root, want):
    """Whether the project at `root` holds the path `want`.

    A project holds its root and the clones its `[[repo]]` entries
    point at. A file this cannot read holds nothing.
    """
    from corral import settings

    if want == os.path.realpath(root):
        return True
    try:
        names = settings.repo_names(root)
    except settings.SettingsError:
        return False
    for name in names:
        try:
            resolved = settings.load(root=root, repo=name)
        except settings.SettingsError:
            continue
        path = resolved["clone.path"]
        if not path:
            continue
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(root, path))
        candidate = os.path.realpath(path)
        if want == candidate or _below(want, candidate):
            return True
    return False


def _below(path, parent):
    """Whether real path `path` lies strictly below real path `parent`."""
    if path == parent:
        return False
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Run the `snapshot` verb."""
    from corral import accounts, admission, paths, quota_ledger, quotacli, registry, settings

    try:
        resolved = settings.load(root=_settings_root(os.getcwd()))
        cfg = registry.config(resolved)
        plain = {
            "admission.total_cap": resolved["admission.total_cap"],
            "admission.repo_cap": resolved["admission.repo_cap"],
            "registry.count_toward_cap": cfg["count_toward_cap"],
        }
        admission.validate_limits(
            admission.Limits(
                total=plain["admission.total_cap"], repo_cap=plain["admission.repo_cap"]
            )
        )
        low_pct = resolved["quota.low_pct"]
    except (settings.SettingsError, paths.PathError, admission.LimitsError) as err:
        _err(str(err))
        return USAGE
    now = time.time()
    host = socket.gethostname()
    try:
        registry_view = registry.visible(now, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - one broken reader must not break the rest
        registry_view = exc
    try:
        quota_view = quotacli.snapshot(
            accounts.providers(), quota_ledger.load(), now, low_pct=low_pct
        )
    except Exception as exc:  # noqa: BLE001 - one broken reader must not break the rest
        quota_view = exc
    doc = build(now, host, registry_view, quota_view, plain, shutil.disk_usage)
    if _all_failed(doc):
        _err("nothing to report: every section failed")
        return UNREACHABLE
    if args.json:
        print(json.dumps(doc))
    else:
        _show(doc)
    return OK


def add_parser(sub) -> None:
    """Register `snapshot` on the top-level subparsers."""
    p = sub.add_parser(
        "snapshot", help="one read-only view of the host: agents, quota, capacity, disks"
    )
    p.add_argument("--json", action="store_true", help="print the stable machine-readable document")
    p.set_defaults(func=cmd_snapshot)
