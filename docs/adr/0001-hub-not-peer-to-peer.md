# 0001: hub, not peer-to-peer, for multi-host claims

Status: accepted (decided by the maintainer in session, ticket 196).

## Context

`00-design.md` section 4 planned several corral hosts as peers: every
host holds a full board copy, peers pull each other's logs, and a
claim needs an ack from every current peer. During a partition no new
claim is made anywhere.

That plan makes one host being down block new claims for everyone,
and it needs an ack roundtrip per claim across a changing membership.

## Decision

Multi-host corral is a hub, hosted on one host the maintainer owns,
reachable over a private network. The hub is a small service in front
of SQLite, standard library only, authenticated per host with bearer
tokens. Claims are linearizable through one hub database transaction.
The full design is `docs/08-hub-multihost.md`.

Kept from the peer plan: certainty over availability (a host acts
alone only where nothing can collide), the tracker as the durable
record, operator-driven host removal with fencing epochs, and resume
pinned to the session's host.

## Consequences

- A hub outage blocks new claims everywhere, but a single host
  outage blocks nothing: the failure domain that matters (one host
  down) no longer stops the fleet.
- Hosts keep running held work while the hub is unreachable and
  reconcile stale-first on reconnect; the hub never deletes work.
- Admission stays per host; the hub learns capacity and load but
  never decides from them.
- The peer log-pull, ack roundtrip, feed duty, and cap-share
  machinery in `00-design.md` section 4 are abandoned, not
  implemented.
