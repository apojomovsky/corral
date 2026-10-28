# Agent overlay

How corral's agents are driven. The project's `AGENTS.md` speaks to
everyone, including contributors and their own agents. This file speaks only
to the agents corral launches: worktrees, the review gate, takeoff, the
decision protocol, house preferences, what is private.

This file is yours to edit after `corral init`: a re-run never overwrites
it. In the inline layout it is a tracked file in the project, but still
separate from `AGENTS.md`, so contributors never read corral-only rules.

## Where a new rule goes

The first yes decides:

1. It mentions anything private (the ops side, a client, people, quota, accounts, internal priorities): it goes in the overlay. The leak guard enforces this.
2. It only makes sense because corral runs the agent: it goes in the overlay.
3. A contributor's PR would be rejected for not following it: it goes in the project `AGENTS.md`.
4. Otherwise, it is a preference for these agents only: it goes in the overlay.

When in doubt, put it in the overlay first. A rule that turns out to hold for
everyone is promoted with a PR to the project. If the overlay would
contradict the project's file, one of them is wrong: drop the overlay rule,
or change the project's `AGENTS.md` in a PR.
