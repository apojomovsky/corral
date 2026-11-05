"""Agent profiles: named declarations of the whole agent setup.

A profile answers the agent setup in one place: which program runs, which
model chains it may use, and that program's own options::

    [profiles.default]
    runtime = "muse"

    [profiles.default.models.default]
    chain = ["provider-a/model-x", "provider-b/model-y"]

    [profiles.default.options]
    reasoning_effort = "high"

Validation has one owner: `validate` here, reusing `models.validate` for
chains and each runtime adapter's `options_schema()` for options. Every
reader of profiles goes through it; a launch that resolves an invalid
profile fails with SettingsError (exit 4) before anything is made. Nothing
here ever falls back to another profile silently: `lookup` refuses an
undeclared name instead of substituting the default.
"""

import json
import math
import os
import re
import tomllib

from dataclasses import dataclass, field

from corral import accounts, models, paths, runtimes, settings

#: The profile used when nothing else selects one.
DEFAULT_PROFILE_NAME = "default"

#: Profile names are label-safe on purpose, so a ticket label can carry one.
_NAME = re.compile(r"[a-z0-9-]+")

#: The only keys a `[profiles.<name>]` table may hold; anything else is a typo.
_PROFILE_KEYS = frozenset({"runtime", "models", "options"})

#: Option values are flat scalars only, so they render into `-c KEY=VALUE`
#: style overlays and JSON configs with no second encoding rule.
_SCALARS = (str, int, float, bool)


class ProfileError(settings.SettingsError):
    """A profile declaration is unusable: bad shape or value (exit 4)."""


@dataclass(frozen=True)
class Profile:
    """One validated profile: its name, runtime, role chains and options."""

    name: str
    runtime: str
    models: dict[str, list[str]] = field(default_factory=dict)
    options: dict = field(default_factory=dict)


#: The shipped default setup. Lower layers fall back to it, so resolution
#: always succeeds; it carries no chains or options of its own. The runtime
#: comes from `runtimes.DEFAULT_RUNTIME`: one fact, one place, so the
#: cutover changes no default.
BUILTIN_DEFAULT = Profile(name=DEFAULT_PROFILE_NAME, runtime=runtimes.DEFAULT_RUNTIME)


def _want_type(entry):
    """The expected type from a schema entry: a type, or a (type, meaning[, allowed]) tuple."""
    if isinstance(entry, tuple):
        return entry[0]
    return entry


def _want_allowed(entry):
    """The allowed values from a schema entry, or None when unconstrained."""
    if isinstance(entry, tuple) and len(entry) == 3:
        return entry[2]
    return None


def _matches(value, want) -> bool:
    """Whether an option value has the schema's expected scalar type."""
    if want is bool:
        return isinstance(value, bool)
    if want is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if want is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want is str:
        return isinstance(value, str)
    return isinstance(value, want)


def _check_chains(name: str, raw) -> dict[str, list[str]]:
    """`raw` as role to chain lists, the same shape `[models.<role>]` carries."""
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {name!r}: [models] must be a table of roles, not {type(raw).__name__}"
        )
    chains: dict[str, list[str]] = {}
    for role, table in raw.items():
        if not isinstance(role, str) or not role:
            raise ProfileError(f"profile {name!r}: [models] role names must be non-empty strings")
        if not isinstance(table, dict) or set(table) != {"chain"}:
            raise ProfileError(f"profile {name!r}: [models.{role}] must hold exactly a chain list")
        chain = table["chain"]
        if not isinstance(chain, list) or not chain:
            raise ProfileError(
                f"profile {name!r}: [models.{role}] chain must be a non-empty list of model ids"
            )
        for entry in chain:
            if not isinstance(entry, str) or not entry:
                raise ProfileError(
                    f"profile {name!r}: [models.{role}] chain entries must be non-empty strings"
                )
        chains[role] = list(chain)
    return chains


def _check_options(name: str, runtime: str, raw) -> dict:
    """`raw` as validated options: flat scalars, every key in the schema."""
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile {name!r}: [options] must be a table of scalars, not {type(raw).__name__}"
        )
    schema = runtimes.get(runtime).options_schema()
    options = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ProfileError(f"profile {name!r}: option names must be non-empty strings")
        if not isinstance(value, _SCALARS):
            raise ProfileError(
                f"profile {name!r}: option {key!r} must be a string, number "
                f"or boolean, not {type(value).__name__}"
            )
        if key not in schema:
            raise ProfileError(
                f"profile {name!r}: unknown option {key!r} for runtime "
                f"{runtime!r}: remove it or declare it in the adapter schema"
            )
        want = _want_type(schema[key])
        if not _matches(value, want):
            want_name = getattr(want, "__name__", str(want))
            raise ProfileError(
                f"profile {name!r}: option {key!r} must be {want_name}, not {type(value).__name__}"
            )
        allowed = _want_allowed(schema[key])
        if allowed is not None and value not in allowed:
            want_values = ", ".join(sorted(str(item) for item in allowed))
            raise ProfileError(
                f"profile {name!r}: option {key!r} must be one of {want_values}, not {value!r}"
            )
        options[key] = value
    return options


def _parse(name, table, providers) -> Profile:
    """One raw `[profiles.<name>]` table as a validated Profile."""
    if not isinstance(name, str) or not name or _NAME.fullmatch(name) is None:
        raise ProfileError(f"profile {name!r}: names are non-empty lowercase [a-z0-9-]")
    if not isinstance(table, dict):
        raise ProfileError(f"profile {name!r}: must be a table with runtime, models and options")
    for key in table:
        if key not in _PROFILE_KEYS:
            raise ProfileError(
                f"profile {name!r}: unknown key {key!r}: "
                "want only 'runtime', 'models' and 'options'"
            )
    runtime = table.get("runtime")
    if runtime in runtimes.REMOVED:
        raise ProfileError(f"profile {name!r}: {runtimes.REMOVED_MESSAGE}")
    if not isinstance(runtime, str) or runtime not in runtimes.NAMES:
        raise ProfileError(
            f"profile {name!r}: unknown runtime {runtime!r}: "
            f"want one of {', '.join(runtimes.NAMES)}"
        )
    chains = _check_chains(name, table.get("models", {}))
    try:
        models.validate(chains, providers)
    except models.ModelsError as err:
        raise models.ModelsError(f"profile {name!r}: {err}") from None
    return Profile(
        name=name,
        runtime=runtime,
        models=chains,
        options=_check_options(name, runtime, table.get("options", {})),
    )


def validate(declarations, providers) -> dict[str, Profile]:
    """Raw `[profiles.<name>]` tables as validated Profiles.

    Every bad shape (bad name, unknown runtime, unknown provider, unknown
    option key) raises SettingsError naming the value. Declarations without
    a `default` entry validate as-is: no default is ever injected.
    """
    if not isinstance(declarations, dict):
        raise ProfileError(
            f"[profiles] must be a table of profiles, not {type(declarations).__name__}"
        )
    return {name: _parse(name, table, providers) for name, table in declarations.items()}


def lookup(parsed: dict[str, Profile], name: str) -> Profile:
    """The validated profile called `name`; never a silent substitution."""
    if not isinstance(name, str) or not isinstance(parsed, dict) or name not in parsed:
        known = sorted(parsed) if isinstance(parsed, dict) else []
        hint = f"declared profiles: {', '.join(known)}" if known else "no profiles declared"
        raise ProfileError(f"unknown profile {name!r}: declare [profiles.{name}] ({hint})")
    return parsed[name]


#: Local selections live outside `settings.resolve` in their own file in
#: the host config dir, so an operator switch always beats committed
#: defaults. This module is the only reader and writer of that file.
SELECTIONS_FILE = "profiles.toml"

#: A ticket label carrying a profile assignment starts with this prefix.
LABEL_PREFIX = "profile:"

#: The board `extra` concept carrying a ticket's profile assignment: a
#: people-owned text field holding the exact profile name, never an
#: option id. Read verbatim; an unknown name fails closed at resolve.
EXTRA_CONCEPT = "profile"

#: Scope values a temporary override may carry. Layer 3 never beats the
#: layer-2 ticket assignment, so no ticket scope exists here.
GLOBAL_SCOPE = "global"


def selections_path(config_dir: str | None = None) -> str:
    """The selections file: `profiles.toml` in the host config dir."""
    return os.path.join(config_dir or paths.config_dir(), SELECTIONS_FILE)


def pin_key(repo: str, number: int) -> str:
    """The selections key for one ticket's local pin: `repo#number`."""
    return f"{repo}#{number}"


@dataclass(frozen=True)
class Override:
    """A temporary layer-3 selection: `profile` until the `until` instant.

    `scope` is `global`, `project:<repo>` or `role:<role>`; it never names
    a ticket. Past `until` the resolver ignores the override, and it stays
    on disk, listed as expired, until explicitly reset.
    """

    scope: str
    profile: str
    until: float
    reason: str = ""


@dataclass(frozen=True)
class Selections:
    """The local selections from `profiles.toml`: one slot per layer.

    `default` is the layer-6 local default; `projects` and `roles` map a
    repo or role to its layer-4 or layer-5 selection; `pins` maps a
    `pin_key` to the layer-2 ticket pin; `override` is the layer-3
    temporary override, if one stands. Names may dangle: a selection
    naming an undeclared profile loads cleanly and fails only when it
    wins a resolution (exit 4), so the doctor can report it.
    """

    default: str | None = None
    projects: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)
    override: Override | None = None


def empty_selections() -> Selections:
    """Selections with nothing stored: every layer falls through."""
    return Selections()


@dataclass(frozen=True)
class Provenance:
    """Why a resolution picked its profile: the deciding layer and source.

    `layer` is 1 to 6 (`flag`, `ticket`, `temp`, `project`, `role`,
    `default`); `source` names what carried the value (flag text, label,
    board field, pin, file path); `until` is the override expiry, layer 3
    only.
    """

    layer: int
    layer_name: str
    source: str
    profile: str
    until: float | None = None


@dataclass(frozen=True)
class Considered:
    """One layer as `explain` saw it: its value and what happened to it.

    `state` is `selected` for the winner, `shadowed` for a value a more
    specific layer beat, `absent` for no value, `expired` for an override
    past `until`, `out-of-scope` for an override scoped elsewhere, and
    `unknown` for a value naming an undeclared profile that lost (a
    winning unknown name fails closed in `resolve` instead).
    """

    layer: int
    layer_name: str
    source: str
    profile: str | None
    state: str


def _check_name(value, where: str) -> str:
    """`value` as a stored profile name: a non-empty string, else exit 4."""
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{where}: profile names must be non-empty strings")
    return value


def _check_scope(value, where: str) -> str:
    """`value` as an override scope: global, project:<repo> or role:<role>."""
    if not isinstance(value, str) or not value:
        raise ProfileError(
            f"{where}: scope must be {GLOBAL_SCOPE!r}, 'project:<repo>' or 'role:<role>'"
        )
    if value == GLOBAL_SCOPE:
        return value
    for prefix in ("project:", "role:"):
        if value.startswith(prefix) and value[len(prefix) :]:
            return value
    raise ProfileError(
        f"{where}: scope {value!r} must be {GLOBAL_SCOPE!r}, "
        "'project:<repo>' or 'role:<role>' (never a ticket: layer 3 "
        "cannot beat the layer-2 assignment)"
    )


def _check_until(value, where: str) -> float:
    """`value` as an override expiry: finite epoch seconds, else exit 4."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where}: until must be epoch seconds, not {value!r}")
    moment = float(value)
    if not math.isfinite(moment):
        raise ProfileError(f"{where}: until must be finite epoch seconds, not {value!r}")
    return moment


def _check_override(raw, where: str) -> Override:
    """`raw` as an Override: exactly scope, profile, until and reason."""
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: [override] must be a table with scope and profile")
    for key in raw:
        if key not in ("scope", "profile", "until", "reason"):
            raise ProfileError(
                f"{where}: [override] has no key {key!r}: want scope, profile, until, reason"
            )
    if "scope" not in raw or "profile" not in raw or "until" not in raw:
        raise ProfileError(f"{where}: [override] needs scope, profile and until")
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        raise ProfileError(f"{where}: [override] reason must be a string")
    return Override(
        scope=_check_scope(raw["scope"], where),
        profile=_check_name(raw["profile"], where),
        until=_check_until(raw["until"], where),
        reason=reason,
    )


def override_until(
    temp: object = None, until: object = None, *, now: object, where: str = "override"
) -> float | None:
    """The override expiry for `--temp`/`--until`: epoch seconds, or None.

    Exactly one of `temp` (a duration like `2h`, counted from `now`) or
    `until` (epoch seconds) may be given; neither reads as no override.
    Durations share `accounts.parse_duration` with quota marks, so both
    accept the same strings; a bad duration or instant is exit 4 naming
    the value.
    """
    if temp is not None and until is not None:
        raise ProfileError(f"{where}: pass exactly one of --temp or --until, not both")
    moment = _check_until(now, where)
    if temp is None and until is None:
        return None
    if temp is not None:
        try:
            seconds = accounts.parse_duration(temp, f"{where} --temp")
        except accounts.AccountsError as err:
            raise ProfileError(str(err)) from None
        return moment + seconds
    if isinstance(until, str):
        try:
            until = float(until.strip())
        except ValueError:
            raise ProfileError(f"{where}: --until must be epoch seconds, not {until!r}") from None
    return _check_until(until, where)


def is_expired(override: Override, now: object) -> bool:
    """Whether `override` is past its `until` at `now`: expired at equality.

    The resolver treats the override as absent from `until` on, and it
    stays on disk, listed as expired, until explicitly reset.
    """
    if not isinstance(override, Override):
        raise ProfileError(f"is_expired: want an Override, not {override!r}")
    return _check_until(now, "is_expired") >= override.until


def _check_pin_key(key, where: str) -> None:
    """A pins-table key as a `pin_key`: `repo#number`, else exit 4."""
    if not isinstance(key, str) or not key:
        raise ProfileError(f"{where}: pin names must be non-empty strings")
    repo, sep, number = key.rpartition("#")
    if not sep or not repo or not (number.isascii() and number.isdigit()) or number[0] == "0":
        raise ProfileError(
            f"{where}: pin {key!r} must look like 'owner/name#12': "
            "pins are written by pin_key, never by hand"
        )


def _check_slot(raw, where: str) -> str:
    """A `[default]`, `[projects.<repo>]`, `[roles.<role>]` or `[pins.<k>]`
    table as its profile name: exactly a `profile` key, else exit 4."""
    if not isinstance(raw, dict) or set(raw) != {"profile"}:
        raise ProfileError(f"{where}: must hold exactly a profile name")
    return _check_name(raw["profile"], where)


def load_selections(path: str | None = None) -> Selections:
    """The selections in `path` (default: the host config dir's file).

    A missing file reads as empty. A corrupt file, an unknown table, or a
    bad shape (bad scope, bad until, bad pin key) is exit 4. A selection
    naming an undeclared profile loads cleanly: it fails only when it wins
    a resolution, so the doctor can report it.
    """
    target = path or selections_path()
    try:
        with open(target, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return empty_selections()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise ProfileError(f"{target}: {err}") from None
    if not isinstance(raw, dict):
        raise ProfileError(f"{target}: want a TOML table")
    for key in raw:
        if key not in ("default", "override", "pins", "projects", "roles"):
            raise ProfileError(
                f"{target}: unknown table {key!r}: want default, override, pins, projects, roles"
            )
    for section in ("projects", "roles", "pins"):
        if section in raw and not isinstance(raw[section], dict):
            raise ProfileError(f"{target}: [{section}] must be a table")
    default = None
    if "default" in raw:
        default = _check_slot(raw["default"], f"{target} [default]")
    projects = {}
    for repo, table in (raw.get("projects") or {}).items():
        if not isinstance(repo, str) or not repo:
            raise ProfileError(f"{target}: [projects] names must be non-empty strings")
        projects[repo] = _check_slot(table, f"{target} [projects.{repo}]")
    roles = {}
    for role, table in (raw.get("roles") or {}).items():
        if not isinstance(role, str) or not role:
            raise ProfileError(f"{target}: [roles] names must be non-empty strings")
        roles[role] = _check_slot(table, f"{target} [roles.{role}]")
    pins = {}
    for key, table in (raw.get("pins") or {}).items():
        _check_pin_key(key, target)
        pins[key] = _check_slot(table, f"{target} [pins.{key}]")
    override = None
    if "override" in raw:
        override = _check_override(raw["override"], target)
    return Selections(default=default, projects=projects, roles=roles, pins=pins, override=override)


def _toml_key(key: str) -> str:
    """`key` as a TOML table key: bare when plain, quoted otherwise."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return _toml_str(key)


def _toml_str(value: str) -> str:
    """`value` as a TOML basic string, with its escapes rendered."""
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        + '"'
    )


def _render(selections: Selections) -> str:
    """`selections` as TOML text: one section per stored value, sorted."""
    lines = []
    if selections.default is not None:
        lines += ["[default]", f"profile = {_toml_str(selections.default)}", ""]
    override = selections.override
    if override is not None:
        lines += [
            "[override]",
            f"scope = {_toml_str(override.scope)}",
            f"profile = {_toml_str(override.profile)}",
            f"until = {override.until!r}",
            f"reason = {_toml_str(override.reason)}",
            "",
        ]
    for section in ("pins", "projects", "roles"):
        for key in sorted(getattr(selections, section)):
            lines += [
                f"[{section}.{_toml_key(key)}]",
                f"profile = {_toml_str(getattr(selections, section)[key])}",
                "",
            ]
    return "\n".join(lines)


def save_selections(selections: Selections, path: str | None = None) -> str:
    """Write `selections` to `path` atomically; the one writer of the file.

    Creates the parent directory first, so a fresh host config dir works.
    Returns the path written.
    """
    target = path or selections_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    paths.atomic_write_text(target, _render(selections))
    return target


def ticket_assignment(
    *,
    labels=(),
    extra=None,
    repo: str = "",
    number: int = 0,
) -> tuple[str | None, str]:
    """One ticket's shared assignment as `(name, source)`: both absent reads None.

    `labels` carries `profile:<name>` (labels mode); `extra` carries the
    board `profile` text field verbatim (board mode). Two distinct label
    names, an empty label name, a non-string board value, or a label and
    field naming different profiles all refuse closed (exit 4): a ticket
    carries at most one assignment, and guessing would bill the wrong
    setup. `repo` and `number` only name the ticket in the source text.
    """
    if not isinstance(labels, (list, tuple)):
        raise ProfileError(
            f"ticket {repo}#{number}: labels must be a list or tuple, not {type(labels).__name__}"
        )
    names: list[str] = []
    for label in labels:
        if not isinstance(label, str):
            raise ProfileError(
                f"ticket {repo}#{number}: label {label!r} must be a string, "
                "not a hand-corrupted row: repair the store row"
            )
        if not label.startswith(LABEL_PREFIX):
            continue
        name = label[len(LABEL_PREFIX) :]
        if not name:
            raise ProfileError(
                f"ticket {repo}#{number}: label {label!r} names no profile: "
                "remove it or write profile:<name>"
            )
        if name not in names:
            names.append(name)
    if len(names) > 1:
        joined = ", ".join(repr(f"{LABEL_PREFIX}{name}") for name in names)
        raise ProfileError(
            f"ticket {repo}#{number}: several profile labels ({joined}): "
            "keep exactly one profile:<name> label"
        )
    from_label = names[0] if names else None
    if extra is None:
        from_field = None
    elif not isinstance(extra, dict):
        raise ProfileError(
            f"ticket {repo}#{number}: board extra must be an object, not {type(extra).__name__}"
        )
    else:
        raw = extra.get(EXTRA_CONCEPT)
        if raw is None or raw == "":
            from_field = None
        elif not isinstance(raw, str):
            raise ProfileError(
                f"ticket {repo}#{number}: board field 'profile' must hold "
                f"the exact profile name, not {raw!r}"
            )
        else:
            from_field = raw
    if from_label is None:
        if from_field is None:
            return None, ""
        return from_field, f"board field 'profile' on {repo}#{number}"
    if from_field is None:
        return from_label, f"label '{LABEL_PREFIX}{from_label}' on {repo}#{number}"
    if from_label == from_field:
        return (
            from_label,
            f"label '{LABEL_PREFIX}{from_label}' and board field 'profile' on {repo}#{number}",
        )
    raise ProfileError(
        f"ticket {repo}#{number}: label '{LABEL_PREFIX}{from_label}' and board "
        f"field 'profile' name different profiles "
        f"({from_label!r} versus {from_field!r}): keep exactly one assignment"
    )


def load_tracker_assignment(store, tracker, *, repo, number, now, stale_after):
    """One ticket's shared assignment from its store row, live when stale.

    Returns (name, source, None): `ticket_assignment` over the row's
    labels and board `extra`, with name None when the ticket carries no
    assignment. The cached row stands when fresh; past `stale_after` the
    assignment is re-read live through `tracker`, exactly like the claim
    freshness rule, and the live reading is used, not stored (the sync
    pass owns store writes). With `tracker` None there is no live check,
    so the cached row stands. Returns (None, "", reason) when the store
    holds no row: the ticket is withdrawn and the launch skips it. A
    stale row with an unreachable tracker raises `TrackerError` (exit 3),
    corrupt store content raises `StoreError` (exit 4), and a corrupt
    shared value raises `ProfileError` (exit 4): none of them skip. A
    truncated database raises a bare `sqlite3.Error` instead: the launch
    caller maps both store failures to exit 4, mirroring
    `drive.refresh_row`.
    """
    found, fresh = store.item(repo, number, now=now, max_age=stale_after)
    if found is None:
        return None, "", f"{repo}#{number}: no store row, skipped as withdrawn"
    if not fresh and tracker is not None:
        live = tracker.item(repo, number)
        name, source = ticket_assignment(
            labels=live.labels, extra=live.extra, repo=repo, number=number
        )
        return name, source, None
    name, source = ticket_assignment(
        labels=found.labels, extra=found.extra, repo=repo, number=number
    )
    return name, source, None


@dataclass(frozen=True)
class _Committed:
    """Committed defaults as resolve reads them: the global name plus maps."""

    default: str | None
    repos: dict[str, str]
    roles: dict[str, str]


def _check_request(repo, role, number, now, committed) -> tuple[float, _Committed]:
    """The resolve inputs as `(now, committed)`: shapes fail closed (exit 4)."""
    if not isinstance(repo, str) or not repo:
        raise ProfileError(f"resolve: repo must be a non-empty string, not {repo!r}")
    if not isinstance(role, str) or not role:
        raise ProfileError(f"resolve: role must be a non-empty string, not {role!r}")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ProfileError(f"resolve: number must be a positive integer, not {number!r}")
    moment = _check_until(now, "resolve")
    if committed is None:
        committed = {}
    if not isinstance(committed, dict):
        raise ProfileError(
            f"resolve: committed defaults must be a table, not {type(committed).__name__}"
        )
    for key in committed:
        if key not in ("default", "repos", "roles"):
            raise ProfileError(
                f"resolve: committed defaults have no key {key!r}: want default, repos, roles"
            )
    default = committed.get("default")
    if default is not None:
        _check_name(default, "resolve: committed default")
    tables: dict[str, dict[str, str]] = {"repos": {}, "roles": {}}
    for key, layer in (("repos", "project"), ("roles", "role")):
        raw = committed.get(key)
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ProfileError(
                f"resolve: committed {key} must be a table, not {type(raw).__name__}"
            )
        for entry, name in raw.items():
            if not isinstance(entry, str) or not entry:
                raise ProfileError(f"resolve: committed {layer} names must be non-empty strings")
            tables[key][entry] = _check_name(name, f"resolve: committed {layer} {entry!r}")
    return moment, _Committed(default=default, repos=tables["repos"], roles=tables["roles"])


def _override_in_scope(scope: str, repo: str, role: str) -> bool:
    """Whether an override `scope` covers this `(repo, role)` resolution."""
    if scope == GLOBAL_SCOPE:
        return True
    if scope.startswith("project:"):
        return scope[len("project:") :] == repo
    if scope.startswith("role:"):
        return scope[len("role:") :] == role
    raise ProfileError(
        f"resolve: override scope {scope!r} must be {GLOBAL_SCOPE!r}, "
        "'project:<repo>' or 'role:<role>'"
    )


def explain(
    parsed: dict[str, Profile],
    selections: Selections,
    *,
    repo: str,
    role: str,
    number: int,
    now,
    flag: str | None = None,
    tracker: str | None = None,
    tracker_source: str = "",
    committed: dict | None = None,
    selections_source: str = SELECTIONS_FILE,
) -> list[Considered]:
    """Every layer's value for one ticket and role, most specific first.

    Pure: `parsed` are the validated profiles, `selections` the local
    file, `tracker` the shared assignment (`ticket_assignment`) with its
    `tracker_source`, and `committed` the committed defaults
    (`{"default": name, "repos": {repo: name}, "roles": {role: name}}`).
    Exactly one entry reads `selected`. A tracker/pin disagreement fails
    closed (exit 4) naming both sources; nothing else here raises for a
    dangling name (a losing unknown name reads `unknown`, a winning one
    fails in `resolve`), so the operator verb can print the whole chain.
    """
    moment, defaults = _check_request(repo, role, number, now, committed)
    if selections is None:
        selections = empty_selections()
    values: list[tuple[int, str, str, str | None, str]] = []
    if flag is None:
        values.append((1, "flag", "", None, "absent"))
    else:
        values.append((1, "flag", f"--profile {flag}", flag, "value"))
    pin = selections.pins.get(pin_key(repo, number))
    if tracker is None and pin is None:
        values.append((2, "ticket", "", None, "absent"))
    elif tracker is not None and pin is not None and tracker != pin:
        raise ProfileError(
            f"ticket {repo}#{number}: {tracker_source or 'the tracker assignment'} "
            f"names {tracker!r} but the pin in {selections_source} names {pin!r}: "
            "clear one of them; the launch refuses while they disagree"
        )
    elif tracker is not None:
        if pin is not None:
            source = f"{tracker_source} and the pin in {selections_source}"
        else:
            source = tracker_source
        values.append((2, "ticket", source, tracker, "value"))
    else:
        values.append((2, "ticket", f"pin in {selections_source}", pin, "value"))
    override = selections.override
    if override is None:
        values.append((3, "temp", "", None, "absent"))
    else:
        source = (
            f"override in {selections_source} (scope {override.scope}, until {override.until!r})"
        )
        if not _override_in_scope(override.scope, repo, role):
            values.append((3, "temp", source, override.profile, "out-of-scope"))
        elif is_expired(override, moment):
            values.append((3, "temp", source, override.profile, "expired"))
        else:
            values.append((3, "temp", source, override.profile, "value"))
    local_project = selections.projects.get(repo)
    if local_project is not None:
        values.append(
            (
                4,
                "project",
                f"project {repo!r} selection in {selections_source}",
                local_project,
                "value",
            )
        )
    elif defaults.repos.get(repo) is not None:
        values.append(
            (4, "project", f"committed [[repo]] {repo!r} profile", defaults.repos[repo], "value")
        )
    else:
        values.append((4, "project", "", None, "absent"))
    local_role = selections.roles.get(role)
    if local_role is not None:
        values.append(
            (5, "role", f"role {role!r} selection in {selections_source}", local_role, "value")
        )
    elif defaults.roles.get(role) is not None:
        values.append(
            (5, "role", f"committed [role.{role}] profile", defaults.roles[role], "value")
        )
    else:
        values.append((5, "role", "", None, "absent"))
    if selections.default is not None:
        values.append(
            (6, "default", f"local default in {selections_source}", selections.default, "value")
        )
    elif defaults.default is not None:
        values.append((6, "default", "committed profile.default", defaults.default, "value"))
    else:
        values.append((6, "default", "built-in default", None, "builtin"))
    winner = next(
        (index for index, entry in enumerate(values) if entry[4] in ("value", "builtin")),
        None,
    )
    chain = []
    for index, (layer, name, source, profile, kind) in enumerate(values):
        if index == winner:
            state = "selected"
        elif kind == "value" and (not isinstance(parsed, dict) or profile not in parsed):
            state = "unknown"
        elif kind == "value":
            state = "shadowed"
        else:
            state = kind
        chain.append(
            Considered(layer=layer, layer_name=name, source=source, profile=profile, state=state)
        )
    return chain


def resolve(
    parsed: dict[str, Profile],
    selections: Selections,
    *,
    repo: str,
    role: str,
    number: int,
    now,
    flag: str | None = None,
    tracker: str | None = None,
    tracker_source: str = "",
    committed: dict | None = None,
    selections_source: str = SELECTIONS_FILE,
) -> tuple[Profile, Provenance]:
    """One `(Profile, Provenance)` for a ticket and role: six layers in order.

    Layer 1 is the `--profile` flag; 2 the ticket assignment (the tracker
    value, else the local pin); 3 the live temporary override; 4 and 5 the
    project and role selections (local beats committed); 6 the default
    (local, else committed, else the built-in). Every reader goes through
    here, so local always wins and provenance always names the layer and
    source. A winning unknown name fails with ProfileError (exit 4); only
    the winner is looked up, so a shadowed dangling selection waits for
    the doctor instead of breaking unrelated launches.
    """
    chain = explain(
        parsed,
        selections,
        repo=repo,
        role=role,
        number=number,
        now=now,
        flag=flag,
        tracker=tracker,
        tracker_source=tracker_source,
        committed=committed,
        selections_source=selections_source,
    )
    winner = next(entry for entry in chain if entry.state == "selected")
    if winner.profile is None:
        return BUILTIN_DEFAULT, Provenance(
            layer=6,
            layer_name="default",
            source="built-in default",
            profile=BUILTIN_DEFAULT.name,
        )
    found = lookup(parsed, winner.profile)
    until = None
    if winner.layer == 3 and selections is not None and selections.override is not None:
        until = selections.override.until
    return found, Provenance(
        layer=winner.layer,
        layer_name=winner.layer_name,
        source=winner.source,
        profile=found.name,
        until=until,
    )


#: The session file holding the profile a session was launched on, next to
#: the runtime record. Absent for sessions launched before profiles recorded.
PROFILE_FILE = "profile"


@dataclass(frozen=True)
class RecordedProfile:
    """The profile a session was launched on: its name, runtime and model head.

    `layer`, `layer_name` and `source` are the resolution provenance, and
    `until` the override expiry for a layer-3 launch. A resume reads this
    back instead of resolving again, so an agent started under an override
    keeps its setup after the override expires.
    """

    name: str
    runtime: str
    model: str | None
    layer: int
    layer_name: str
    source: str
    until: float | None = None


def _check_recorded(raw: object, where: str) -> RecordedProfile:
    """`raw` as a RecordedProfile: a full record, else exit 4.

    Every shape a hand edit can produce (a missing key, an empty name, an
    unknown runtime, a layer outside 1 to 6, a bad `until`) fails closed:
    guessing would bill the wrong setup.
    """
    if not isinstance(raw, dict):
        raise ProfileError(
            f"{where}: the profile record must be an object, not {type(raw).__name__}"
        )
    for key in raw:
        if key not in ("profile", "runtime", "model", "layer", "layer_name", "source", "until"):
            raise ProfileError(f"{where}: the profile record has no key {key!r}")
    for key in ("profile", "runtime", "layer", "layer_name", "source"):
        if key not in raw:
            raise ProfileError(f"{where}: the profile record needs {key!r}")
    name = _check_name(raw["profile"], where)
    runtime = raw["runtime"]
    if runtime in runtimes.REMOVED:
        raise ProfileError(f"{where}: {runtimes.REMOVED_MESSAGE}")
    if not isinstance(runtime, str) or runtime not in runtimes.NAMES:
        raise ProfileError(
            f"{where}: the profile record names unknown runtime {runtime!r}: "
            f"want one of {', '.join(runtimes.NAMES)}"
        )
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ProfileError(
            f"{where}: the profile record model must be a model id or null, not {model!r}"
        )
    layer = raw.get("layer")
    if isinstance(layer, bool) or not isinstance(layer, int) or not 1 <= layer <= 6:
        raise ProfileError(f"{where}: the profile record layer must be 1 to 6, not {layer!r}")
    for key in ("layer_name", "source"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise ProfileError(f"{where}: the profile record {key} must be a non-empty string")
    until = raw.get("until")
    if until is not None:
        until = _check_until(until, where)
    return RecordedProfile(
        name=name,
        runtime=runtime,
        model=model,
        layer=layer,
        layer_name=raw["layer_name"],
        source=raw["source"],
        until=until,
    )


def record_profile(
    session_dir: str,
    profile: Profile,
    provenance: Provenance,
    model: str | None = None,
    runtime: str | None = None,
) -> None:
    """Write the resolved profile into `session_dir`, next to the runtime record.

    Records the name, runtime, model head and provenance, so a resume reuses
    the launch setup even after an override expires. `runtime` is the run's
    effective runtime: the `--runtime` refinement when one was given, else
    the profile's runtime. The resume reruns the recorded runtime, so
    recording the unrefined one would silently switch the refined run back.
    Validates before writing: a bad value raises and nothing is written.
    """
    if not isinstance(profile, Profile):
        raise ProfileError(f"profile record: want a Profile, not {type(profile).__name__}")
    if not isinstance(provenance, Provenance):
        raise ProfileError(f"profile record: want a Provenance, not {type(provenance).__name__}")
    if model is not None and (not isinstance(model, str) or not model):
        raise ProfileError(f"profile record: model must be a model id or null, not {model!r}")
    if runtime is not None and (not isinstance(runtime, str) or not runtime):
        raise ProfileError(f"profile record: runtime must be a runtime name, not {runtime!r}")
    target = os.path.join(session_dir, PROFILE_FILE)
    stored = _check_recorded(
        {
            "profile": profile.name,
            "runtime": runtime if runtime is not None else profile.runtime,
            "model": model,
            "layer": provenance.layer,
            "layer_name": provenance.layer_name,
            "source": provenance.source,
            "until": provenance.until,
        },
        target,
    )
    os.makedirs(session_dir, exist_ok=True)
    paths.atomic_write_text(
        target,
        json.dumps(
            {
                "profile": stored.name,
                "runtime": stored.runtime,
                "model": stored.model,
                "layer": stored.layer,
                "layer_name": stored.layer_name,
                "source": stored.source,
                "until": stored.until,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def recorded_profile(session_dir: str) -> RecordedProfile | None:
    """The profile the session was launched on; None when none was recorded.

    Sessions launched before profiles recorded read as None, so the caller
    falls back to its own rule. A corrupt record fails closed (exit 4),
    exactly like an unknown runtime at launch.
    """
    target = os.path.join(session_dir, PROFILE_FILE)
    try:
        with open(target, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise ProfileError(f"{target}: {err}") from None
    if not text.strip():
        raise ProfileError(f"{target}: the profile record is empty")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as err:
        raise ProfileError(f"{target}: {err}") from None
    return _check_recorded(raw, target)
