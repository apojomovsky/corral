"""The forge interface: branches, pull requests, their state, merges.

Only the adapters (today: GitHub through ``gh``) know provider URLs or
API shapes. Everything else takes a ``Forge`` object. Nothing here
imports ``gh`` or knows a provider's URL layout.
"""

from typing import NamedTuple, Protocol

#: Pull request states, as the forge reports them.
OPEN = "open"
MERGED = "merged"
CLOSED = "closed"


class ForgeError(Exception):
    """The forge could not be reached or answered something unusable.

    A missing pull request is a ``ForgeError`` too, never ``None``:
    callers fail closed. Exit code 3 through the CLI: a remote or
    service unreachable.
    """

    exit_code = 3


class PullRequest(NamedTuple):
    repo: str  # "owner/name" as the forge names it
    number: int
    state: str  # "open" | "merged" | "closed"
    base_ref: str  # the branch the PR targets
    base_sha: str  # the tip of base_ref, fetched fresh
    head_ref: str
    head_sha: str
    author: str
    from_fork: bool
    labels: tuple[str, ...]


class Forge(Protocol):
    """Branches on the remote, pull requests, their state, merges."""

    def pull_request(self, repo: str, number: int) -> PullRequest:
        """One pull request, fetched fresh. Raises ``ForgeError`` when
        the forge is unreachable, the answer is unusable, or the PR is
        missing."""
        ...

    def pr_index(self, repo: str) -> dict[str, str]:
        """Every pull request ever opened against ``repo``, keyed by
        head branch name, valued ``"open"`` | ``"merged"`` | ``"closed"``.
        Raises ``ForgeError`` when the forge is unreachable or answers
        something unusable."""
        ...
