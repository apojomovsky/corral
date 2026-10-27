"""Settings: one resolver for every value a project or host can set.

Layers, later winning (docs/01-onboarding.md, section 2):

1. built-in defaults, from the registry below;
2. the host config, ``<config_dir>/config.toml``;
3. the ops root's ``corral.toml``: its shared tables, then the ``[[repo]]``
   entry named for this repository;
4. ``CORRAL_*`` environment variables (``agents.cap`` is ``CORRAL_AGENTS_CAP``);
5. CLI flags.

The host config's ``[limits]`` table sets ceilings on numeric settings. A
value above its ceiling is clamped, whatever layer set it, and the clamp is
recorded so ``config show`` and ``doctor`` can report it.

Secrets are never settings: tokens and keys are read at call time from
their own stores. Nothing in the registry holds one.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field

from corral import paths

DEFAULT = "default"
HOST = "host"
PROJECT = "project"
REPO = "repo"
ENV = "env"
FLAG = "flag"

HOST_CONFIG_NAME = "config.toml"
PROJECT_FILE = "corral.toml"


class SettingsError(ValueError):
    """A settings file or value is unusable (exit 4, configuration error)."""


@dataclass(frozen=True)
class Setting:
    key: str
    type: type
    default: object
    help: str

    @property
    def env(self) -> str:
        return "CORRAL_" + self.key.upper().replace(".", "_")


REGISTRY: dict[str, Setting] = {}


def register(key, type_, default, help_):
    """Declare a setting. Each module registers its own, once, at import."""
    if key in REGISTRY:
        raise ValueError(f"setting {key!r} is registered twice")
    if not isinstance(default, type_):
        raise ValueError(f"setting {key!r}: default {default!r} is not {type_.__name__}")
    REGISTRY[key] = Setting(key, type_, default, help_)
    return REGISTRY[key]


#: Modules that register settings of their own. `resolve` imports them
#: first, so a file setting one of their keys never reads as unknown, and
#: `config show` lists them. A test keeps this list complete.
PROVIDERS = ("corral.registry",)


def _load_providers():
    import importlib

    for name in PROVIDERS:
        importlib.import_module(name)


register("git.branch", str, "{type}/{issue}-{slug}", "branch name template")
register("git.worktrees", str, ".worktrees/", "worktree root, relative to the clone")
register("git.commits.conventional", bool, True, "enforce Conventional Commits")
register("board.ready", str, "ready", "label that makes an issue eligible")
register("agents.cap", int, 1, "this project's ceiling of live agents on the host")
register("agents.runtime", str, "omp", "agent runtime: omp or opencode")


@dataclass(frozen=True)
class Value:
    value: object
    origin: str
    clamped_from: object = None


@dataclass
class Resolved:
    values: dict[str, Value] = field(default_factory=dict)

    def __getitem__(self, key):
        return self.values[key].value


def _flatten(table, where, prefix=""):
    """Dotted keys to scalars. A key reached twice (a quoted `"a.b"` beside a
    `[a] b`) and an empty table are errors, never silently dropped."""
    out = {}
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            if not value:
                raise SettingsError(f"{where}: {dotted} is an empty table")
            items = _flatten(value, where, dotted + ".").items()
        else:
            items = [(dotted, value)]
        for k, v in items:
            if k in out:
                raise SettingsError(f"{where}: {k} is set twice")
            out[k] = v
    return out


def _check(key, value, where):
    setting = REGISTRY.get(key)
    if setting is None:
        raise SettingsError(f"{where}: unknown setting {key!r}")
    # bool is a subclass of int; a cap of `true` is a mistake, not 1.
    if setting.type is int and isinstance(value, bool) or not isinstance(value, setting.type):
        raise SettingsError(
            f"{where}: {key} must be {setting.type.__name__}, not {type(value).__name__}"
        )
    return value


_INT = re.compile(r"-?[0-9]+")


def _parse_env(setting, raw):
    if setting.type is bool:
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise SettingsError(f"{setting.env}={raw!r}: expected true or false")
    if setting.type is int:
        if not _INT.fullmatch(raw.strip()):
            raise SettingsError(f"{setting.env}={raw!r}: expected an integer")
        return int(raw)
    return raw


def _load(path):
    """The parsed TOML at `path`, or {} when there is none."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise SettingsError(f"{path}: {err}") from None


def find_root(start):
    """The nearest directory at or above `start` holding corral.toml, else None."""
    here = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(here, PROJECT_FILE)):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def resolve(
    host: Mapping | None = None,
    project: Mapping | None = None,
    repo: str | None = None,
    env: Mapping[str, str] | None = None,
    flags: Mapping[str, object] | None = None,
    host_name: str = "host config",
    project_name: str = PROJECT_FILE,
) -> Resolved:
    """Every registered setting, resolved through the five layers (pure)."""
    _load_providers()
    host = dict(host or {})
    project = dict(project or {})
    env = os.environ if env is None else env
    result = {key: Value(s.default, DEFAULT) for key, s in REGISTRY.items()}

    limits = host.pop("limits", {})
    if not isinstance(limits, dict):
        raise SettingsError(f"{host_name}: [limits] must be a table")
    for key, value in _flatten(host, host_name).items():
        result[key] = Value(_check(key, value, host_name), HOST)

    repos = project.pop("repo", [])
    if not isinstance(repos, list) or not all(isinstance(r, dict) for r in repos):
        raise SettingsError(f"{project_name}: [[repo]] must be an array of tables")
    for key, value in _flatten(project, project_name).items():
        result[key] = Value(_check(key, value, project_name), PROJECT)
    entries = {}
    for index, raw in enumerate(repos, 1):
        entry = dict(raw)
        name = entry.pop("name", None)
        if not isinstance(name, str) or not name:
            raise SettingsError(f"{project_name}: [[repo]] number {index} needs a name")
        if name in entries:
            raise SettingsError(f"{project_name}: [[repo]] {name!r} appears twice")
        where = f"{project_name} [[repo]] {name}"
        entries[name] = {k: _check(k, v, where) for k, v in _flatten(entry, where).items()}
    if repo is not None:
        if repo not in entries:
            raise SettingsError(f"{project_name}: no [[repo]] named {repo!r}")
        for key, value in entries[repo].items():
            result[key] = Value(value, REPO)

    for key, setting in REGISTRY.items():
        raw = env.get(setting.env)
        if raw is not None and raw != "":
            result[key] = Value(_parse_env(setting, raw), ENV)
    for key, value in (flags or {}).items():
        if value is not None:
            result[key] = Value(_check(key, value, "flag"), FLAG)

    for key, ceiling in _flatten(limits, f"{host_name} [limits]").items():
        setting = REGISTRY.get(key)
        if setting is None or setting.type is not int:
            raise SettingsError(f"{host_name}: [limits] {key!r} is not a numeric setting")
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
            raise SettingsError(f"{host_name}: [limits] {key} must be an int of 0 or more")
        current = result[key]
        if isinstance(current.value, int) and current.value > ceiling:
            result[key] = Value(ceiling, current.origin, clamped_from=current.value)
    return Resolved(result)


def load(root=None, repo=None, env=None, flags=None):
    """Resolve from the files on disk: the host config and the ops root's corral.toml."""
    env = os.environ if env is None else env
    host_path = os.path.join(paths.config_dir(env), HOST_CONFIG_NAME)
    project_path = os.path.join(root, PROJECT_FILE) if root else None
    return resolve(
        host=_load(host_path),
        project=_load(project_path) if project_path else {},
        repo=repo,
        env=env,
        flags=flags,
        host_name=host_path,
        project_name=project_path or PROJECT_FILE,
    )


def render(resolved, origin=False):
    """`config show` text: one `key = value` line per setting, sorted."""
    lines = []
    for key in sorted(resolved.values):
        item = resolved.values[key]
        line = f"{key} = {item.value!r}"
        if origin:
            line += f"  # {item.origin}"
            if item.clamped_from is not None:
                line += f", clamped from {item.clamped_from!r} by the host limit"
        lines.append(line)
    return "\n".join(lines) + "\n"
