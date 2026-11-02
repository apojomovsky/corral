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

import re

from dataclasses import dataclass, field

from corral import models, runtimes, settings

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
#: always succeeds; it carries no chains or options of its own.
BUILTIN_DEFAULT = Profile(name=DEFAULT_PROFILE_NAME, runtime="muse")


def _want_type(entry):
    """The expected type from a schema entry: a type, or a (type, meaning) pair."""
    if isinstance(entry, tuple):
        return entry[0]
    return entry


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
    if isinstance(parsed, dict) and name in parsed:
        return parsed[name]
    known = sorted(parsed) if isinstance(parsed, dict) else []
    hint = f"declared profiles: {', '.join(known)}" if known else "no profiles declared"
    raise ProfileError(f"unknown profile {name!r}: declare [profiles.{name}] ({hint})")
