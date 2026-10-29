# Security

## Reporting

Do not open a public issue for a vulnerability. Report it through GitHub's private vulnerability reporting for this repository, so the fix can land before the details are public. Describe the impact and the reproduction; never paste secrets, tokens or account material into the report.

## Scope

In scope is the tool's own code, for example:

- a deletion without proof (a branch, worktree, cache, image or session removed without showing nothing is lost);
- a secret logged or stored (a token, credential or quota payload beyond usage numbers);
- a path escaping a declared root (a state, session, cache or ops root, or the worktree layout).

Out of scope are the agent runtimes, the providers' quota APIs, and any particular deployment of corral.

## Supported versions

Only the latest release is supported. Update to it and retest before reporting.
