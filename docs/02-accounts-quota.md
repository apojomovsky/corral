# 02: accounts, models and quota

Status: proposed. Milestone 1, step 3.

corral names no provider. A provider's quota format is the one thing every deployment does differently, so it sits behind a reader that the host installs. corral owns what is the same everywhere: the account pool, the ledger, reactive marks, the gate over a model chain, and the overlay that keeps an exhausted provider out of a launch.

## Accounts

`~/.config/corral/accounts.toml` is the host's pool. Every project on the host shares it.

```toml
[[provider]]
name = "provider-a"                 # the prefix of a model id: provider-a/model-x
reader = "my_tools.quota:ProviderA" # an entry point, see below
cooldown = "1h"                     # how long a rate-limit mark blocks, unless the error says
stale_after = "15m"                 # a reading older than this is stale
```

`corral deps` and the doctor check that the file parses, that provider names are unique, and that each reader imports.

## Readers

A reader is a Python class named by an entry point. It is the only provider-specific code, and it lives in the host's ops root, never in corral.

```python
class Reader(Protocol):
    def read(self, now: float) -> Reading: ...
    def parse_limit_error(self, text: str, now: float) -> float | None: ...

class Reading(NamedTuple):
    windows: Mapping[str, Window]     # window id -> usage

class Window(NamedTuple):
    used_pct: float                   # 0..100
    resets_at: float | None           # epoch seconds
```

- `read` fetches current usage. It may raise `ReaderError`; the ledger records the failure, never a guess.
- `parse_limit_error` turns a session's error text into "blocked until T", or `None` when the text is not this provider's rate-limit message. That is how a session tail posts a reactive mark without corral knowing any provider's wording.
- Readers get their credentials at call time and never return them. corral logs usage numbers only.
- Tests inject fake readers. corral ships none for a real provider.

## The ledger

One file in the host state dir, `quota.json`, shared by every project. Atomic writes under one lock.

- `readings`: provider to windows and `read_at`.
- `marks`: provider to `until`, `reason`, `source`. A mark only moves later, never earlier.

The ledger is data. The decisions below are pure functions over it.

## States and the gate

A provider is `ok`, `low` (a window at or above the `quota.low_pct` setting), `exhausted` (a window at 100, or an active mark), or `unknown` (no reading, or a stale one, and no mark).

- **A mark always blocks.** An exhausted window blocks until its reset.
- **`unknown` does not block.** A dead reader must not stop the fleet. It shows in `corral quota` and in the doctor, and a reactive mark still blocks. A setting, `quota.unknown_blocks`, turns that around for a host that prefers certainty. Default `false`.
- **A role passes when any provider on its chain passes.** The gate is evaluated per project over that project's own chains, against the shared ledger, so a project whose chains avoid an exhausted provider keeps running.

## Models

A project declares its roles in `corral.toml`:

```toml
[models.default]
chain = ["provider-a/model-x", "provider-b/model-y"]
```

corral checks every chain entry against the pool. An unknown provider is a configuration error (exit 4). The per-launch overlay carries the chain with exhausted providers removed (re-route). When the whole chain of a required role is exhausted, the launch is refused with exit 75, "try later". The user's home runtime config is read, never written.

## Surface

- `corral quota [--json]`: one view of the pool and the ledger.
- `corral quota refresh [provider]`: run the readers, record the result.
- `corral quota mark <provider> [--until T] [--reason R]`: a manual or session-posted mark.
- Library: `corral.quota.gate(...)`, per the design doc, section 2.

## Not in this step

Pacing (a baseline share per window), calibration and tapering belong to a reader or a later step. They are generic in idea, but existing implementations tie them to one provider's windows, so they get their own design.

## Slices

1. The ledger and marks: pure, with atomic writes.
2. The accounts file, the reader protocol and loading.
3. States and the gate.
4. Model declarations, validation and the overlay.
5. `corral quota` verbs and the launch integration.
