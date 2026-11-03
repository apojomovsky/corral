# 0002: remove opencode support

Status: accepted (decided by the maintainer in session, ticket 343; closes the opencode 2.x cutover, ticket 87).

## Context

`system_deps.toml` pins one supported major per runtime, so the
opencode entry (`below = "2"`) turned red on any host with opencode
2.x installed: `corral doctor` showed a failing `deps:opencode` row.
The planned cutover to opencode 2.x (ticket 87) was still open, and
the maintainer decided to drop the runtime for now instead of
porting it: opencode support removed; re-adding is a new adapter
ticket.

## Decision

One cutover PR deletes the `opencode` adapter, its `system_deps.toml`
entry, its tail rendering and its tests, with no compatibility shim
and no leftover. Any config that still names opencode (a runtime, a
model chain provider, a profile) is refused with exit 4 and a message
that says opencode support was removed.

## Consequences

- `runtimes.NAMES` is `("omp", "muse")`; the adapter protocol, the
  profile option schemas and the session record stay generic, holding
  only what the remaining runtimes use.
- `corral doctor` has no `deps:opencode` row, and `corral deps` lists
  no opencode entry. A host with opencode 2.x installed reads green.
- A session recorded as opencode refuses at launch with the removal
  message; the ticket resumes under omp or muse instead.
