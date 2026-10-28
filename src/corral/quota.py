"""Provider states and the quota gate: pure decisions over the ledger.

The ledger (``corral.quota_ledger``) is data: usage readings per provider
plus reactive failure marks. This module decides from it: each provider is
``ok``, ``low``, ``exhausted`` or ``unknown``, each role passes when any
provider on its chain passes, and the gate refuses a launch only while a
required role has no passing provider.
"""

from corral import settings

OK = "ok"
LOW = "low"
EXHAUSTED = "exhausted"
UNKNOWN = "unknown"

DEFAULT_LOW_PCT = 90
DEFAULT_UNKNOWN_BLOCKS = False

settings.register(
    "quota.low_pct", int, DEFAULT_LOW_PCT, "usage percent at which a provider reads low"
)
settings.register(
    "quota.unknown_blocks",
    bool,
    DEFAULT_UNKNOWN_BLOCKS,
    "an unknown provider blocks its roles instead of passing them",
)


def _windows_live(windows, now):
    """Windows whose reset has not passed yet; a reset starts a fresh window."""
    return {
        window_id: entry
        for window_id, entry in windows.items()
        if entry.get("resets_at", now) > now
    }


def provider_state(reading, mark, now, *, stale_after, low_pct):
    """One provider's state from its ledger reading and mark.

    An active mark always means ``exhausted``. A live window at 100 is
    ``exhausted``; at or above ``low_pct`` it is ``low``. No reading, a
    failed reading, or a reading older than ``stale_after`` is ``unknown``.
    """
    if isinstance(mark, dict):
        try:
            if now < float(mark["until"]):
                return EXHAUSTED
        except (KeyError, TypeError, ValueError):
            pass
    if not isinstance(reading, dict):
        return UNKNOWN
    windows = reading.get("windows")
    if not isinstance(windows, dict):
        return UNKNOWN
    try:
        read_at = float(reading["read_at"])
    except (KeyError, TypeError, ValueError):
        return UNKNOWN
    if now - read_at > float(stale_after):
        return UNKNOWN
    live = _windows_live(windows, now)
    try:
        if any(float(entry["used_pct"]) >= 100 for entry in live.values()):
            return EXHAUSTED
        if any(float(entry["used_pct"]) >= float(low_pct) for entry in live.values()):
            return LOW
    except (KeyError, TypeError, ValueError):
        return UNKNOWN
    return OK


def role_state(chain, states, *, unknown_blocks=DEFAULT_UNKNOWN_BLOCKS):
    """One role's state: it passes when any provider on its chain passes.

    ``ok`` beats ``low``; an all-``exhausted`` chain is ``exhausted``. An
    ``unknown`` provider passes unless ``unknown_blocks`` turns it blocking.
    """
    if not chain:
        return UNKNOWN
    seen = [states.get(provider, UNKNOWN) for provider in chain]
    if any(state == OK for state in seen):
        return OK
    if any(state == LOW for state in seen):
        return LOW
    if all(state == EXHAUSTED for state in seen):
        return EXHAUSTED
    if unknown_blocks:
        return EXHAUSTED
    return UNKNOWN


def _chain_provider(entry):
    """The provider of a chain entry: the part before ``provider/model``."""
    return str(entry).split("/", 1)[0]


def gate(
    roles,
    chains,
    ledger,
    now,
    *,
    stale_after,
    low_pct=DEFAULT_LOW_PCT,
    unknown_blocks=DEFAULT_UNKNOWN_BLOCKS,
):
    """Whether `roles` may launch on `chains` against the shared `ledger`.

    Returns a verdict dict: ``shut`` plus ``blocked`` (each shut required
    role with its state and provider states), ``roles`` (every chained
    role's state) and ``states`` (every chained provider's state). A
    project whose chains avoid an exhausted provider is not shut.
    """
    readings = (ledger or {}).get("readings", {}) or {}
    marks = (ledger or {}).get("marks", {}) or {}
    providers = {
        _chain_provider(entry) for chain in (chains or {}).values() for entry in chain or ()
    }
    states = {
        provider: provider_state(
            readings.get(provider),
            marks.get(provider),
            now,
            stale_after=stale_after,
            low_pct=low_pct,
        )
        for provider in sorted(providers)
    }
    roles_states = {
        role: role_state(
            [_chain_provider(entry) for entry in chain or ()],
            states,
            unknown_blocks=unknown_blocks,
        )
        for role, chain in (chains or {}).items()
    }
    blocked = {
        role: {
            "state": roles_states[role],
            "providers": {
                provider: states[provider]
                for provider in [_chain_provider(entry) for entry in (chains or {})[role] or ()]
            },
        }
        for role in roles or ()
        if roles_states.get(role) == EXHAUSTED
    }
    return {
        "shut": bool(blocked),
        "blocked": blocked,
        "roles": roles_states,
        "states": states,
    }
