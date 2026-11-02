"""The host's project registry: which projects share this host.

Each project registers one file, ``projects.d/<name>.toml`` in the corral
config dir, naming its unit prefix, its ops root, and its share of the
host: a guaranteed floor and a ceiling. An optional ``eligibility``
entry names the policy module the dispatch pass asks for startable
tickets. Launch admission reads the registry, so one project's agents
count against the host total no matter which project starts them.
"""

import math
import os
import re
import tomllib
from typing import NamedTuple

from corral import admission, eligibility, paths, settings

#: The registry directory, inside the corral config dir.
PROJECTS_DIR = "projects.d"

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class ProjectsError(ValueError):
    """A registration file is unusable (exit 4, configuration error)."""

    exit_code = 4


class Project(NamedTuple):
    """One registered project: its name, unit prefix, ops root and share.

    `eligibility` is the optional policy entry point (`module:function`
    or `path/to/policy.py:function`) the dispatch pass calls; None
    registers no candidates. `resources` is the normalized `[resources]`
    share table (weight, per-dimension floors and ceilings), or None when
    the file sets none; the budget gate reads it, never the file.
    """

    name: str
    unit_prefix: str
    root: str
    floor: int = 0
    ceiling: int | None = None
    eligibility: str | None = None
    resources: dict | None = None


def projects_dir(config_dir: str | None = None) -> str:
    """The registry directory: `projects.d` in the corral config dir."""
    return os.path.join(config_dir or paths.config_dir(), PROJECTS_DIR)


def _fail(path: str, text: str) -> ProjectsError:
    return ProjectsError(f"{path}: {text}")


def terminated(prefix: str) -> str:
    """`prefix` with the trailing separator every load normalizes to.

    The separator is part of the prefix: unit names read
    `<prefix><repo>-<number>`, as with the default `corral-agent-`.
    """
    return prefix if prefix.endswith("-") else prefix + "-"


def _plain(value: object) -> bool:
    return isinstance(value, str) and bool(_NAME.fullmatch(value))


def _count(value: object, what: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, f"{what} must be an integer")
    if value < 0:
        raise _fail(path, f"{what} {value} must be zero or more")
    return value


#: The top-level keys a registration file may hold: exactly the ones
#: ``_project`` reads below. Strict: any other key is an error naming the
#: file and the key, never a silent default (a misspelled share header
#: would otherwise load with no share).
_PROJECT_KEYS = frozenset(
    ("name", "unit_prefix", "root", "floor", "ceiling", "eligibility", "resources")
)

#: The keys a ``[resources]`` share table may hold: the remainder weight,
#: per-dimension floors, and optional per-dimension ceilings. Strict: any
#: other key is an error, never silently dropped.
_RESOURCE_KEYS = (
    "weight",
    "cpu_floor_m",
    "memory_floor_mb",
    "pids_floor",
    "cpu_ceiling_m",
    "memory_ceiling_mb",
    "pids_ceiling",
)

#: The share-table key holding each dimension's floor and ceiling.
_RESOURCE_FLOORS = {
    "cpu_m": "cpu_floor_m",
    "memory_mb": "memory_floor_mb",
    "pids": "pids_floor",
}
_RESOURCE_CEILINGS = {
    "cpu_m": "cpu_ceiling_m",
    "memory_mb": "memory_ceiling_mb",
    "pids": "pids_ceiling",
}


def _share_weight(value: object, path: str):
    """The remainder weight: a number of zero or more (floor only at zero)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, "[resources] weight must be a number")
    if not math.isfinite(value) or value < 0:
        raise _fail(path, f"[resources] weight {value!r} must be zero or more")
    return value


def _share_table(raw: object, path: str) -> dict | None:
    """The normalized ``[resources]`` share table, or None when absent.

    Missing floors read as zero, a missing weight as one, missing ceilings
    as no cap: the same defaults the budget core applies. Unknown keys, bad
    types, and a floor above its ceiling are errors naming the file.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _fail(path, "[resources] must be a table")
    if not raw:
        raise _fail(path, "[resources] is an empty table")
    for key in raw:
        if key not in _RESOURCE_KEYS:
            raise _fail(path, f"[resources] has no key {key!r}")
    weight = _share_weight(raw.get("weight", 1), path)
    share: dict = {"weight": weight}
    for dim in ("cpu_m", "memory_mb", "pids"):
        floor = _count(
            raw.get(_RESOURCE_FLOORS[dim], 0), f"[resources] {_RESOURCE_FLOORS[dim]}", path
        )
        ceiling = raw.get(_RESOURCE_CEILINGS[dim])
        if ceiling is not None:
            ceiling = _count(ceiling, f"[resources] {_RESOURCE_CEILINGS[dim]}", path)
        if ceiling is not None and floor > ceiling:
            raise _fail(
                path,
                f"[resources] {dim} floor {floor} is above its ceiling {ceiling}",
            )
        share[_RESOURCE_FLOORS[dim]] = floor
        share[_RESOURCE_CEILINGS[dim]] = ceiling
    return share


def _project(stem: str, path: str, raw: object) -> Project:
    if not isinstance(raw, dict):
        raise _fail(path, "the file must hold a TOML table")
    for key in raw:
        if key not in _PROJECT_KEYS:
            raise _fail(
                path,
                f"unknown key {key!r}: expected one of {', '.join(sorted(_PROJECT_KEYS))}",
            )
    name = raw.get("name")
    if name != stem or not _plain(name):
        raise _fail(path, f"name {name!r} must equal the file stem {stem!r}")
    prefix = raw.get("unit_prefix")
    if not isinstance(prefix, str) or not prefix:
        raise _fail(path, "unit_prefix must be a non-empty string")
    prefix = terminated(prefix)
    root = raw.get("root")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise _fail(path, f"root {root!r} must be an absolute path")
    floor = _count(raw.get("floor", 0), "floor", path)
    ceiling = raw.get("ceiling")
    if ceiling is not None:
        ceiling = _count(ceiling, "ceiling", path)
    if ceiling is not None and floor > ceiling:
        raise _fail(path, f"floor {floor} is above its ceiling {ceiling}")
    entry = raw.get("eligibility")
    if entry is not None:
        if not isinstance(entry, str) or not entry or ":" not in entry:
            raise _fail(
                path,
                f"eligibility {entry!r} must look like "
                "module:function or path/to/policy.py:function",
            )
    return Project(
        name=name,
        unit_prefix=prefix,
        root=os.path.normpath(root),
        floor=floor,
        ceiling=ceiling,
        eligibility=entry,
        resources=_share_table(raw.get("resources"), path),
    )


def load_all(config_dir: str | None = None) -> list[Project]:
    """Every registered project, sorted by name.

    A missing registry dir is an empty list. A bad file is a
    ProjectsError naming the file, never a silent skip.
    """
    directory = projects_dir(config_dir)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    except OSError as err:
        raise ProjectsError(f"{directory}: cannot list the registry: {err}") from None
    found = []
    seen: dict[str, str] = {}
    for name in names:
        if not name.endswith(".toml"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as err:
            raise ProjectsError(f"{path}: cannot read the registration: {err}") from None
        project = _project(os.path.splitext(name)[0], path, raw)
        if project.unit_prefix in seen:
            raise _fail(
                path,
                f"unit_prefix {project.unit_prefix!r} is already "
                f"registered by project {seen[project.unit_prefix]!r}",
            )
        seen[project.unit_prefix] = project.name
        found.append(project)
    return sorted(found, key=lambda project: project.name)


def for_root(root: str | None, registered: list[Project] | None = None) -> Project | None:
    """The project whose root equals the realpath of `root`, or None.

    None means no registration claims this ops root: a single-project
    host. Reads the registry when no project list is given.
    """
    if not root:
        return None
    want = os.path.realpath(root)
    for project in registered if registered is not None else load_all():
        if os.path.realpath(project.root) == want:
            return project
    return None


def unit_prefix(
    root: str | None, resolved: settings.Resolved, registered: list[Project] | None = None
) -> str:
    """This root's unit prefix, the one place that decides it.

    The registration's prefix when one claims the root, else the
    `agents.unit_prefix` setting. Launch, admission and stop read it
    through the target; the projects verb reads the registrations it
    comes from.
    """
    project = for_root(root, registered)
    if project is not None:
        return project.unit_prefix
    return resolved["agents.unit_prefix"]


def limits(found: list[Project], *, total_cap: int, repo_cap: int) -> admission.Limits:
    """The admission limits for these projects, validated.

    A ceiling above the host total is clamped to it. Floors that promise
    more than the total holds are a LimitsError naming the numbers.
    """
    table = {}
    for project in found:
        ceiling = project.ceiling
        if ceiling is not None and ceiling > total_cap:
            ceiling = total_cap
        table[project.name] = {"floor": project.floor, "ceiling": ceiling}
    built = admission.Limits(total=total_cap, projects=table, repo_cap=repo_cap)
    admission.validate_limits(built)
    return built


def check(config_dir: str | None = None) -> list[str]:
    """Problems with the registry: bad files, ceilings above the host total,
    and eligibility entries that do not resolve.

    [] means every registration is usable. A missing registry dir counts
    as usable: the host just holds one unregistered project. Resolving
    imports the policy module but never calls the entry function.
    """
    try:
        found = load_all(config_dir)
    except ProjectsError as err:
        return [str(err)]
    try:
        total = settings.load(root=None)["admission.total_cap"]
    except settings.SettingsError as err:
        return [str(err)]
    problems = []
    for project in found:
        if project.ceiling is not None and project.ceiling > total:
            problems.append(
                f"project {project.name} asks ceiling {project.ceiling}, host allows {total}"
            )
        if project.eligibility is not None:
            problem = eligibility.check_entry(
                project.eligibility, project=project.name, base=project.root
            )
            if problem is not None:
                problems.append(problem)
    return problems
