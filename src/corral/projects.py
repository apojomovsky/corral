"""The host's project registry: which projects share this host.

Each project registers one file, ``projects.d/<name>.toml`` in the corral
config dir, naming its unit prefix, its ops root, and its share of the
host: a guaranteed floor and a ceiling. An optional ``eligibility``
entry names the policy module the dispatch pass asks for startable
tickets. Launch admission reads the registry, so one project's agents
count against the host total no matter which project starts them.
"""

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
    registers no candidates.
    """

    name: str
    unit_prefix: str
    root: str
    floor: int = 0
    ceiling: int | None = None
    eligibility: str | None = None


def projects_dir(config_dir: str | None = None) -> str:
    """The registry directory: `projects.d` in the corral config dir."""
    return os.path.join(config_dir or paths.config_dir(), PROJECTS_DIR)


def _fail(path: str, text: str) -> ProjectsError:
    return ProjectsError(f"{path}: {text}")


def _plain(value: object) -> bool:
    return isinstance(value, str) and bool(_NAME.fullmatch(value))


def _count(value: object, what: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, f"{what} must be an integer")
    if value < 0:
        raise _fail(path, f"{what} {value} must be zero or more")
    return value


def _project(stem: str, path: str, raw: object) -> Project:
    if not isinstance(raw, dict):
        raise _fail(path, "the file must hold a TOML table")
    name = raw.get("name")
    if name != stem or not _plain(name):
        raise _fail(path, f"name {name!r} must equal the file stem {stem!r}")
    prefix = raw.get("unit_prefix")
    if not isinstance(prefix, str) or not prefix:
        raise _fail(path, "unit_prefix must be a non-empty string")
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
    never calls a policy.
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
            problem = eligibility.check_entry(project.eligibility, project=project.name)
            if problem is not None:
                problems.append(problem)
    return problems
