# Docs

| Document | What it is |
| --- | --- |
| [`00-design.md`](00-design.md) | the design: the problem, where corral's line falls, and how the pieces fit |
| [`01-onboarding.md`](01-onboarding.md) | onboarding a project in the inline or split layout, and the layout decisions behind it |
| [`02-accounts-quota.md`](02-accounts-quota.md) | the accounts file, quota readers, the ledger, and the gate over model chains |
| [`03-muse-runtime.md`](03-muse-runtime.md) | design for the muse runtime adapter: argv, overlay, sessions, resume, errors, quota, sandbox, tests |
| [`04-dispatch-loop.md`](04-dispatch-loop.md) | the dispatch loop: process model, eligibility, ordering, claims, lifecycle, safety |
| [`05-dashboard-api.md`](05-dashboard-api.md) | design for the read-only dashboard API: routes, loopback HTTP plus SSE, token rules, schema stability, test plan, tickets |
| [`06-corral-new.md`](06-corral-new.md) | design for `corral new`: scaffold templates, the `init` writer, the forge side, label bootstrap, idempotence, test plan, tickets |
| [`07-board-fields.md`](07-board-fields.md) | the board-field tracker variant: project board fields behind the tracker interface, and the tickets to build it |
| [`adr/`](adr/) | decision records; none yet |

`00-design.md` and `02-accounts-quota.md` are designs: how the system works and what each slice builds. `01-onboarding.md` records decisions (the R1 to R7 requirements and what each one settled) alongside the flow they shape. New decision records go in `adr/`.
