"""Settings: one resolver for every value a project or host can set.

Layers, later winning (docs/01-onboarding.md, section 2):

1. built-in defaults, from the registry below;
2. the host config, ``<config_dir>/config.toml``;
3. the ops root's ``korapy.toml``: its shared tables, then the ``[[repo]]``
   entry named for this repository;
4. ``KORAPY_*`` environment variables (``agents.cap`` is ``KORAPY_AGENTS_CAP``);
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

from korapy import paths
from korapy.trackers import VARIANTS

if TYPE_CHECKING:
    from korapy.resources import Tables

DEFAULT = "default"
HOST = "host"
PROJECT = "project"
REPO = "repo"
ENV = "env"
FLAG = "flag"

HOST_CONFIG_NAME = "config.toml"
PROJECT_FILE = "korapy.toml"


class SettingsError(ValueError):
    """A settings file or value is unusable (exit 4, configuration error)."""


@dataclass(frozen=True)
class Setting:
    key: str
    type: type
    default: object
    help: str
    #: An optional range check: takes the parsed value, returns it unchanged,
    #: and raises ValueError with the rule when it fails. The settings layer
    #: reports the failure naming the file and key, so the check only states
    #: the rule, never where the value came from.
    check: Callable[[object], object] | None = None

    @property
    def env(self) -> str:
        return "KORAPY_" + self.key.upper().replace(".", "_")


REGISTRY: dict[str, Setting] = {}


def register(key, type_, default, help_, check=None):
    """Declare a setting. Each module registers its own, once, at import."""
    if key in REGISTRY:
        raise ValueError(f"setting {key!r} is registered twice")
    if not isinstance(default, type_):
        raise ValueError(f"setting {key!r}: default {default!r} is not {type_.__name__}")
    if check is not None and not callable(check):
        raise ValueError(f"setting {key!r}: check is not callable")
    REGISTRY[key] = Setting(key, type_, default, help_, check)
    return REGISTRY[key]


#: Modules that register settings of their own. `resolve` imports them
#: first, so a file setting one of their keys never reads as unknown, and
#: `config show` lists them. A test keeps this list complete.
PROVIDERS = (
    "korapy.admission",
    "korapy.budget",
    "korapy.detect",
    "korapy.disk",
    "korapy.drive",
    "korapy.hold",
    "korapy.hub",
    "korapy.launch",
    "korapy.merge",
    "korapy.prompts",
    "korapy.quota",
    "korapy.registry",
    "korapy.resources",
    "korapy.serve",
    "korapy.sync",
    "korapy.takeoff",
    "korapy.units",
)


def _load_providers():
    import importlib

    for name in PROVIDERS:
        importlib.import_module(name)


register("git.branch", str, "{type}/{issue}-{slug}", "branch name template")
register("git.worktrees", str, ".worktrees/", "worktree root, relative to the clone")
register("git.commits.conventional", bool, True, "enforce Conventional Commits")
register("board.ready", str, "ready", "label that makes an issue eligible")
register("agents.cap", int, 1, "this project's ceiling of live agents on the host")


#: Settings the profiles cutover replaced: the old key maps to the new
#: ``[profiles.*]`` home the error names. One fact, one place: `_check`
#: rejects these wherever they appear (host config, project file, repo
#: entry, flags), and `resolve` rejects them in the environment too.
REMOVED_KEYS = {
    "agents.runtime": "[profiles.<name>] runtime",
}


class Private(NamedTuple):
    """The ``[private]`` lists from the ops root's korapy.toml: plain terms
    and regular expressions, both matched case-insensitively."""

    terms: list[str]
    patterns: list[str]


class BoardConfig(NamedTuple):
    """The ``[board]`` table of one ``[[repo]]`` entry: how the board slice
    finds the project board and maps its fields to concepts.

    Korapy code names concept keys only (the column attributes below); the
    project's field and option names live in the project's own korapy.toml.
    ``priority_map`` keys are opaque board option ids, never resolved
    against the registry; display names are comments only. Columns default
    to ``""`` (unmapped); at least the board address (the owner plus the
    number or the node id) is always present."""

    project_owner: str
    project_number: int | None
    project_id: str | None
    agent_field: str
    status_field: str
    status_column: str
    priority_column: str
    area_column: str
    pool_column: str
    blocked_by_column: str
    priority_map: dict[str, str]


@dataclass(frozen=True)
class Value:
    value: object
    origin: str
    clamped_from: object = None


@dataclass
class Resolved:
    values: dict[str, Value] = field(default_factory=dict)
    #: The project's private terms and patterns, from ``[private]`` in
    #: korapy.toml. Like the removed ``[models]`` tables, this is
    #: PROJECT-level only: the host config and ``[[repo]]`` entries cannot
    #: set it, and read as unknown settings there.
    private: Private = field(default_factory=lambda: Private(terms=[], patterns=[]))
    #: The selected repo's ``[board]`` table, or None when the entry has
    #: none. The board slice reads this; it never re-reads korapy.toml.
    board: BoardConfig | None = None
    #: The ``[resources]`` tables from the host config, or None when it
    #: sets none. The budget code reads this; it never re-reads the host
    #: config. Project files and repo entries cannot set it: the host
    #: budget is one machine fact, and it reads as unknown there.
    resources: "Tables | None" = None
    #: Raw ``[profiles.<name>]`` tables, the host config shadowing the
    #: project file per name. The launch path validates them through
    #: ``profiles.validate``; this stays unchecked here so the shape and
    #: pool errors name the profile in one place.
    profiles: dict = field(default_factory=dict)
    #: The committed profile defaults: ``{"default": name | None, "repos":
    #: {repo: name}, "roles": {role: name}}`` from ``[profile]``,
    #: ``[[repo]] profile`` and ``[role.<name>]`` in the project file.
    #: The launch path feeds this to ``profiles.resolve`` as ``committed``.
    #: Committed means committed: the host config cannot set these, and a
    #: host ``[profile]`` or ``[role]`` table reads as unknown.
    profile_committed: dict = field(default_factory=dict)
    #: Where each declared profile came from: name to the winning source
    #: path (the host config path when both files declare it). `profile
    #: show` and the doctor read this; `resolve` above is the one place
    #: that merges the two files, so it records both sides here.
    profile_sources: dict = field(default_factory=dict)
    #: Shadowed names: name to the committed source path the local table
    #: hides. Empty when no name is declared in both files.
    profile_shadowed: dict = field(default_factory=dict)

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


def _apply_check(setting, value, where):
    """The setting's range check on `value`, else `value` unchanged.

    A failure is a SettingsError naming where and key, so the check itself
    only states the rule. This is the one place range checks run: every
    layer's values pass through here.
    """
    if setting.check is None:
        return value
    try:
        return setting.check(value)
    except ValueError as err:
        raise SettingsError(f"{where}: {setting.key} {err}") from None


def _check(key, value, where):
    if key in REMOVED_KEYS:
        raise SettingsError(
            f"{where}: {key} was replaced by {REMOVED_KEYS[key]}: "
            "declare [profiles.default] (or another profile) with a "
            "runtime and select it, then remove this key"
        )
    setting = REGISTRY.get(key)
    if setting is None:
        raise SettingsError(f"{where}: unknown setting {key!r}")
    # bool is a subclass of int; a cap of `true` is a mistake, not 1.
    if setting.type is int and isinstance(value, bool) or not isinstance(value, setting.type):
        raise SettingsError(
            f"{where}: {key} must be {setting.type.__name__}, not {type(value).__name__}"
        )
    return _apply_check(setting, value, where)


def _reject_models(where):
    """Refuse a top-level ``[models]`` table: profiles own the chains now.

    The profiles cutover moved every ``[models.<role>]`` table under
    ``[profiles.default]`` (or another profile) as its ``models`` table.
    There is no shim: two readers for one fact is a bug even when they
    agree. The refusal is exit 4, like the old shape check.
    """
    raise SettingsError(
        f"{where}: [models] was replaced by [profiles.<name>] models: "
        "move each [models.<role>] table under [profiles.default] "
        "(runtime plus per-role chains), then remove this table"
    )


def _check_profile_tables(raw, where):
    """Raw ``[profiles.<name>]`` tables: a table of tables, carried unchecked.

    Values are validated later by ``profiles.validate`` (the one owner);
    this only refuses a non-table, so a typo like ``profiles = "x"``
    fails here naming the file.
    """
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: [profiles] must be a table of profiles")
    return dict(raw)


def _check_profile_default(raw, where):
    """The ``[profile]`` table as the committed global default name or None."""
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: [profile] must be a table with a default")
    for key in raw:
        if key != "default":
            raise SettingsError(f"{where}: [profile] has no key {key!r}: want default")
    default = raw.get("default")
    if default is not None and (not isinstance(default, str) or not default):
        raise SettingsError(f"{where}: [profile] default must be a non-empty string")
    return default


def _check_role_profiles(raw, where):
    """The ``[role.<name>]`` tables as ``{role: profile name}``."""
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: [role] must be a table of roles")
    out = {}
    for role, table in raw.items():
        if not isinstance(role, str) or not role:
            raise SettingsError(f"{where}: [role] names must be non-empty strings")
        if not isinstance(table, dict) or set(table) != {"profile"}:
            raise SettingsError(f"{where}: [role.{role}] must hold exactly a profile name")
        name = table["profile"]
        if not isinstance(name, str) or not name:
            raise SettingsError(f"{where}: [role.{role}] profile must be a non-empty string")
        out[role] = name
    return out


def _check_private(raw, where):
    """The ``[private]`` table as a Private. Shape and regex errors are exit 4."""
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: [private] must be a table of term and pattern lists")
    for key in raw:
        if key not in ("terms", "patterns"):
            raise SettingsError(f"{where}: [private] has no key {key!r}")
    checked = {}
    for key in ("terms", "patterns"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(e, str) and e for e in value):
            raise SettingsError(f"{where}: [private] {key} must be a list of non-empty strings")
        checked[key] = list(value)
    for pattern in checked["patterns"]:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as err:
            raise SettingsError(
                f"{where}: [private] pattern {pattern!r} is invalid: {err}"
            ) from None
    return Private(terms=checked["terms"], patterns=checked["patterns"])


#: Concept keys naming the board address: the owner plus exactly one of
#: the number or the node id.
_BOARD_ADDRESS = ("project_owner", "project_number", "project_id")

#: Concept keys holding a board field or column name; all default to
#: unmapped when the table omits them.
_BOARD_COLUMNS = (
    "agent_field",
    "status_field",
    "status_column",
    "priority_column",
    "area_column",
    "pool_column",
    "blocked_by_column",
)

#: The concept keys a ``[[repo]]`` entry's ``[board]`` table may hold, on
#: the ``[private]`` precedent: strict keys, so a typo fails here instead
#: of silently running label mode. Derived from the
#: address and column groups above (plus ``priority_map``), so a concept
#: added to one group is never silently dropped by the other.
_BOARD_KEYS = frozenset(_BOARD_ADDRESS + _BOARD_COLUMNS + ("priority_map",))


def _split_board(raw, where):
    """A ``[[repo]]`` entry's ``board`` value as ``(scalars, concepts)``.

    Keys registered as ``board.*`` settings keep their scalar path;
    concept keys (the board address, the field and column names, and
    ``priority_map``) go to ``_check_board``. Anything else is an
    unknown setting, as before."""
    if raw is None:
        return {}, {}
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: [board] must be a table")
    if not raw:
        raise SettingsError(f"{where}: [board] is an empty table")
    scalars, concepts = {}, {}
    for key, value in raw.items():
        if f"board.{key}" in REGISTRY:
            scalars[key] = value
        elif key in _BOARD_KEYS:
            concepts[key] = value
        else:
            raise SettingsError(f"{where}: unknown setting 'board.{key}'")
    return scalars, concepts


def _check_board(raw, where):
    """One ``[[repo]]`` entry's ``[board]`` concept keys as a BoardConfig.

    Shape errors are exit 4. The option ids in ``priority_map`` are opaque:
    they are kept verbatim and never resolved against the registry."""
    if raw is None:
        return None
    owner = raw.get("project_owner")
    if not isinstance(owner, str) or not owner:
        raise SettingsError(f"{where}: [board] project_owner must be a non-empty string")
    number = raw.get("project_number")
    if number is not None and (
        isinstance(number, bool) or not isinstance(number, int) or number < 1
    ):
        raise SettingsError(f"{where}: [board] project_number must be a positive integer")
    node = raw.get("project_id")
    if node is not None and (not isinstance(node, str) or not node):
        raise SettingsError(f"{where}: [board] project_id must be a non-empty string")
    if (number is None) == (node is None):
        raise SettingsError(f"{where}: [board] needs exactly one of project_number or project_id")
    columns = {}
    for key in _BOARD_COLUMNS:
        if key not in raw:
            columns[key] = ""
            continue
        value = raw[key]
        if not isinstance(value, str) or not value:
            raise SettingsError(f"{where}: [board] {key} must be a non-empty string")
        columns[key] = value
    mapping = raw.get("priority_map", {})
    if not isinstance(mapping, dict):
        raise SettingsError(f"{where}: [board] priority_map must be a table of option ids")
    checked = {}
    for option, rank in mapping.items():
        if not isinstance(option, str) or not option:
            raise SettingsError(f"{where}: [board] priority_map keys must be non-empty strings")
        if not isinstance(rank, str) or not rank:
            raise SettingsError(
                f"{where}: [board] priority_map {option!r} must be a non-empty rank string"
            )
        checked[option] = rank
    if checked and not columns["priority_column"]:
        raise SettingsError(
            f"{where}: [board] priority_map needs priority_column to name the priority field"
        )
    return BoardConfig(
        project_owner=owner,
        project_number=number,
        project_id=node,
        priority_map=checked,
        **columns,
    )


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


def _check_variant(value, where):
    """A ``tracker.variant`` value as given, else a naming ``SettingsError``.

    The one validator for the variant, used for every ``[[repo]]`` entry
    and for the resolved value (so an environment or flag typo fails the
    same way as a file typo instead of silently running label mode).
    """
    if value not in VARIANTS:
        raise SettingsError(f"{where}: tracker.variant {value!r} must be one of {sorted(VARIANTS)}")
    return value


def _repo_entries(repos, project_name):
    """``({name: {dotted key: checked value}}, {name: BoardConfig | None},
    {name: profile name})`` for each ``[[repo]]`` entry, in file order.
    The one validation of entry names and keys, including every entry's
    ``[board]`` table and ``profile`` pin."""
    if not isinstance(repos, list) or not all(isinstance(r, dict) for r in repos):
        raise SettingsError(f"{project_name}: [[repo]] must be an array of tables")
    entries: dict = {}
    boards: dict = {}
    repo_profiles: dict = {}
    for index, raw in enumerate(repos, 1):
        entry = dict(raw)
        name = entry.pop("name", None)
        if not isinstance(name, str) or not name:
            raise SettingsError(f"{project_name}: [[repo]] number {index} needs a name")
        if name in entries:
            raise SettingsError(f"{project_name}: [[repo]] {name!r} appears twice")
        where = f"{project_name} [[repo]] {name}"
        if "models" in entry:
            _reject_models(where)
        profile = entry.pop("profile", None)
        if profile is not None and (not isinstance(profile, str) or not profile):
            raise SettingsError(f"{where}: profile must be a non-empty string")
        scalars, concepts = _split_board(entry.pop("board", None), where)
        for key, value in scalars.items():
            dotted = f"board.{key}"
            if dotted in entry:
                raise SettingsError(f"{where}: {dotted} is set twice")
            entry[dotted] = value
        boards[name] = _check_board(concepts or None, where)
        entries[name] = {k: _check(k, v, where) for k, v in _flatten(entry, where).items()}
        if profile is not None:
            repo_profiles[name] = profile
        _check_variant(
            entries[name].get("tracker.variant", REGISTRY["tracker.variant"].default),
            where,
        )
    return entries, boards, repo_profiles


def repo_names(root):
    """Names of the ``[[repo]]`` entries in the ops root's korapy.toml,
    in file order. Empty when there is no root or no file."""
    _load_providers()
    if not root:
        return []
    project_path = os.path.join(root, PROJECT_FILE)
    project = _load(project_path)
    entries, _, _ = _repo_entries(project.get("repo", []), project_path)
    return list(entries)


def covering_repo(root, clone):
    """The `[[repo]]` entry name whose `clone.path` covers `clone`, else None.

    A relative `clone.path` resolves against the ops root, and the match
    compares real paths, so the answer never depends on the working
    directory. Several entries covering the same clone are a SettingsError:
    the entry name keys hooks and ticket records, so it must stay
    unambiguous.
    """
    _load_providers()
    if not root:
        return None
    project_path = os.path.join(root, PROJECT_FILE)
    entries, _, _ = _repo_entries(_load(project_path).get("repo", []), project_path)
    want = os.path.realpath(clone)
    matched = []
    for name, values in entries.items():
        path = values.get("clone.path")
        if not path:
            continue
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(root, path))
        if os.path.realpath(path) == want:
            matched.append(name)
    if len(matched) > 1:
        raise SettingsError(
            f"{project_path}: several [[repo]] entries match this clone "
            f"({', '.join(matched)}): give each entry a distinct clone.path"
        )
    return matched[0] if matched else None


def private(root):
    """The ``[private]`` terms and patterns in the ops root's korapy.toml.

    Empty when there is no root or no file. Shape and regex errors are
    SettingsError (exit 4). The host config is never read: private terms
    live on the ops side only, so only the ops root's file counts."""
    if not root:
        return Private(terms=[], patterns=[])
    project_path = os.path.join(root, PROJECT_FILE)
    return resolve(project=_load(project_path), env={}, project_name=project_path).private


def find_root(start):
    """The nearest directory at or above `start` holding korapy.toml, else None."""
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
    resource_tables = host.pop("resources", None)
    if "models" in host:
        _reject_models(host_name)
    host_profiles = _check_profile_tables(host.pop("profiles", {}), host_name)
    if not isinstance(limits, dict):
        raise SettingsError(f"{host_name}: [limits] must be a table")
    nested_gone = None
    has_nested_gone = isinstance(resource_tables, dict) and "gone_s" in resource_tables
    if has_nested_gone:
        assert isinstance(resource_tables, dict)
        resource_tables = dict(resource_tables)
        nested_gone = resource_tables.pop("gone_s")
        if not resource_tables:
            resource_tables = None
    flat_host = _flatten(host, host_name)
    if has_nested_gone and "resources.gone_s" in flat_host:
        raise SettingsError(f"{host_name}: resources.gone_s is set twice")
    for key, value in flat_host.items():
        result[key] = Value(_check(key, value, host_name), HOST)
    if has_nested_gone:
        result["resources.gone_s"] = Value(_check("resources.gone_s", nested_gone, host_name), HOST)

    repos = project.pop("repo", [])
    if "models" in project:
        _reject_models(project_name)
    private = _check_private(project.pop("private", {}), project_name)
    project_profiles = _check_profile_tables(project.pop("profiles", {}), project_name)
    profile_default = _check_profile_default(project.pop("profile", {}), project_name)
    role_profiles = _check_role_profiles(project.pop("role", {}), project_name)
    for key, value in _flatten(project, project_name).items():
        result[key] = Value(_check(key, value, project_name), PROJECT)
    entries, boards, repo_profiles = _repo_entries(repos, project_name)
    if repo is not None:
        if repo not in entries:
            raise SettingsError(f"{project_name}: no [[repo]] named {repo!r}")
        for key, value in entries[repo].items():
            result[key] = Value(value, REPO)

    for key, setting in REGISTRY.items():
        raw = env.get(setting.env)
        if raw is not None and raw != "":
            parsed = _parse_env(setting, raw)
            result[key] = Value(_apply_check(setting, parsed, setting.env), ENV)
    for key in REMOVED_KEYS:
        env_key = "KORAPY_" + key.upper().replace(".", "_")
        raw = env.get(env_key)
        if raw is not None and raw != "":
            raise SettingsError(
                f"{env_key}={raw!r} was replaced by {REMOVED_KEYS[key]}: "
                "declare [profiles.default] (or another profile) with a "
                "runtime and select it, then unset this variable"
            )
    for key, value in (flags or {}).items():
        if value is not None:
            result[key] = Value(_check(key, value, "flag"), FLAG)
    if repo is not None:
        _check_variant(result["tracker.variant"].value, f"{project_name} [[repo]] {repo}")

    for key, ceiling in _flatten(limits, f"{host_name} [limits]").items():
        setting = REGISTRY.get(key)
        if setting is None or setting.type is not int:
            raise SettingsError(f"{host_name}: [limits] {key!r} is not a numeric setting")
        if isinstance(ceiling, bool) or not isinstance(ceiling, int):
            raise SettingsError(f"{host_name}: [limits] {key} must be an int of 0 or more")
        # The ceiling is a value too: a checked setting refuses 0 (or less)
        # here, naming the host limits file and key, instead of clamping a
        # 0 in unchecked and dividing by it later.
        ceiling = _apply_check(setting, ceiling, f"{host_name} [limits]")
        if ceiling < 0:
            raise SettingsError(f"{host_name}: [limits] {key} must be an int of 0 or more")
        current = result[key]
        if isinstance(current.value, int) and current.value > ceiling:
            result[key] = Value(ceiling, current.origin, clamped_from=current.value)
    from korapy import (
        resources,
    )  # Lazy: providers load above; resources registers its settings there.

    board = boards[repo] if repo is not None else None
    return Resolved(
        result,
        private,
        board,
        resources.check_tables(resource_tables, host_name),
        profiles={**project_profiles, **host_profiles},
        profile_committed={
            "default": profile_default,
            "repos": repo_profiles,
            "roles": role_profiles,
        },
        profile_sources={
            **{name: project_name for name in project_profiles},
            **{name: host_name for name in host_profiles},
        },
        profile_shadowed={name: project_name for name in project_profiles if name in host_profiles},
    )


def load(root=None, repo=None, env=None, flags=None):
    """Resolve from the files on disk: the host config and the ops root's korapy.toml."""
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
