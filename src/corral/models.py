"""Model declarations and the per-launch overlay: chains, re-route, refusal.

A project declares its roles in ``corral.toml``::

    [models.default]
    chain = ["provider-a/model-x", "provider-b/model-y"]

``settings.resolve`` is the one place that reads these tables (as
``Resolved.models``). This module checks them against the account pool,
removes exhausted providers from each chain (re-route), and refuses a
launch whose required role has no live provider left. The runtime
adapters lay the filtered chains over the per-launch config; the user's
home runtime config is read by the runtime itself and never written.
"""

from corral import quota, settings

#: The role an opencode launch runs as: opencode takes one model, not a table.
DEFAULT_ROLE = "default"


class ModelsError(settings.SettingsError):
    """A model declaration is unusable: unknown provider or bad id (exit 4)."""


class Exhausted(Exception):
    """No provider on a required role's chain is live: try later (exit 75)."""

    exit_code = 75


def _pool_names(providers) -> set:
    """Provider names, from names or account-pool entries."""
    return {getattr(provider, "name", provider) for provider in providers or ()}


def validate(chains, providers) -> None:
    """Check every chain entry against the account pool.

    An entry must be a non-empty model id that does not start with ``-``,
    and its provider (the part before ``provider/model``) must be in the
    pool. Raises ``ModelsError`` naming the role and the entry.
    """
    known = _pool_names(providers)
    for role, chain in (chains or {}).items():
        for entry in chain or ():
            if not isinstance(entry, str) or not entry or entry.startswith("-"):
                raise ModelsError(
                    f"role {role!r}: model {entry!r} must be a non-empty name "
                    "that does not start with '-'"
                )
            provider = quota.chain_provider(entry)
            if provider not in known:
                raise ModelsError(
                    f"role {role!r}: model {entry!r} names unknown provider {provider!r}"
                )


def _states(chains, ledger, now, *, stale_after, low_pct, unknown_blocks):
    """Each chained provider's state from the ledger (mirrors the gate)."""
    readings = (ledger or {}).get("readings", {}) or {}
    marks = (ledger or {}).get("marks", {}) or {}
    providers = {
        quota.chain_provider(entry) for chain in (chains or {}).values() for entry in chain or ()
    }
    return {
        provider: quota.provider_state(
            readings.get(provider),
            marks.get(provider),
            now,
            stale_after=stale_after,
            low_pct=low_pct,
        )
        for provider in sorted(providers)
    }


def reroute(
    chains,
    ledger,
    now,
    *,
    stale_after,
    low_pct=quota.DEFAULT_LOW_PCT,
    unknown_blocks=quota.DEFAULT_UNKNOWN_BLOCKS,
):
    """Each chain with providers that block it removed.

    An ``exhausted`` provider always leaves; an ``unknown`` one leaves only
    when ``unknown_blocks`` turns it blocking. Order is kept.
    """
    states = _states(
        chains,
        ledger,
        now,
        stale_after=stale_after,
        low_pct=low_pct,
        unknown_blocks=unknown_blocks,
    )
    dropped = {quota.EXHAUSTED} | ({quota.UNKNOWN} if unknown_blocks else set())
    return {
        role: [entry for entry in chain or () if states[quota.chain_provider(entry)] not in dropped]
        for role, chain in (chains or {}).items()
    }


def earliest_reset(providers, ledger, now):
    """The earliest known reset among `providers`, or None.

    Candidates are the reset of a live exhausted window and the ``until``
    of an active mark.
    """
    readings = (ledger or {}).get("readings", {}) or {}
    marks = (ledger or {}).get("marks", {}) or {}
    candidates = []
    for provider in providers or ():
        reading = readings.get(provider)
        if isinstance(reading, dict):
            for entry in (reading.get("windows") or {}).values():
                if not isinstance(entry, dict):
                    continue
                try:
                    if float(entry["used_pct"]) < 100:
                        continue
                    resets_at = entry.get("resets_at")
                    if resets_at is not None and float(resets_at) > now:
                        candidates.append(float(resets_at))
                except (KeyError, TypeError, ValueError):
                    continue
        mark = marks.get(provider)
        if isinstance(mark, dict):
            try:
                if now < float(mark["until"]):
                    candidates.append(float(mark["until"]))
            except (KeyError, TypeError, ValueError):
                pass
    return min(candidates) if candidates else None


def check(
    chains,
    ledger,
    now,
    *,
    roles=None,
    stale_after,
    low_pct=quota.DEFAULT_LOW_PCT,
    unknown_blocks=quota.DEFAULT_UNKNOWN_BLOCKS,
):
    """The re-routed chains, or refuse when a required role has none live.

    Every declared role is required unless `roles` names a subset. A role
    whose state is ``exhausted`` raises ``Exhausted`` naming the role and
    its earliest known reset.
    """
    required = list(roles) if roles is not None else sorted(chains or {})
    states = _states(
        chains,
        ledger,
        now,
        stale_after=stale_after,
        low_pct=low_pct,
        unknown_blocks=unknown_blocks,
    )
    for role in required:
        providers = [quota.chain_provider(entry) for entry in (chains or {}).get(role) or ()]
        state = quota.role_state(providers, states, unknown_blocks=unknown_blocks)
        if state == quota.EXHAUSTED:
            reset = earliest_reset(providers, ledger, now)
            when = f"earliest reset at {reset}" if reset is not None else "no reset known"
            raise Exhausted(f"role {role!r} has no live provider ({when}): try later")
    return reroute(
        chains,
        ledger,
        now,
        stale_after=stale_after,
        low_pct=low_pct,
        unknown_blocks=unknown_blocks,
    )


def primary(chains, role=DEFAULT_ROLE):
    """The head of `role`'s chain: the model a single-model runtime runs."""
    chain = (chains or {}).get(role) or ()
    if not chain:
        raise ModelsError(f"no model for role {role!r}: its chain is empty or missing")
    return chain[0]


def bare_id(entry):
    """The model id without its provider prefix: the part after the first `/`.

    A bare id with no slash passes through unchanged.
    """
    return str(entry).split("/", 1)[-1]
