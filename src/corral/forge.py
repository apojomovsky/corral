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


class ForgeExists(ForgeError):
    """The repository already exists: the create lost a race.

    A subclass of ``ForgeError`` so callers that do not handle the race
    still fail closed. Exit code 2 through the CLI: race lost, the same
    code ``launch`` uses when a ticket is already live.
    """

    exit_code = 2


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
    """Branches on the remote, pull requests, their state, merges.

    The remote-repository side (`current_user`, `repo_exists`,
    `create_repo`, `repo_empty`, `clone_url`, `describe_create`) lets
    `new --create-remote` stay provider-neutral: only the adapters know
    provider URL shapes or command syntax.
    """

    def current_user(self) -> str:
        """The authenticating user's login, fresh. Raises ``ForgeError``
        when the forge is unreachable or answers something unusable."""
        ...

    def repo_exists(self, repo: str) -> bool:
        """Whether ``owner/name`` exists on the forge. A missing repo is
        ``False``; anything else unusable raises ``ForgeError``."""
        ...

    def create_repo(self, owner: str, name: str, *, private: bool = True) -> str:
        """Create ``owner/name`` and return its clone URL.

        Private by default. An already-existing repo is ``ForgeExists``
        (the pre-check raced); any other failure is ``ForgeError``."""
        ...

    def repo_empty(self, repo: str) -> bool:
        """Whether ``owner/name`` holds zero refs. The adoption proof for
        ``--resume``: only a check that succeeds and reports empty reads
        as empty. A failed check raises ``ForgeError``, never ``False``."""
        ...

    def clone_url(self, repo: str) -> str:
        """The clone URL of ``owner/name``. Raises ``ForgeError`` when
        the forge is unreachable, the repo is missing, or the answer is
        unusable."""
        ...

    def describe_create(self, name: str, owner: str | None = None, *, private: bool = True) -> str:
        """The exact create command `--create-remote` would run, as text.

        Pure: it never calls the runner. ``owner`` is omitted when the
        operator did not give one (the provider fills in the current
        user); a malformed name or owner is ``ForgeError``, so callers
        map it to a usage error without any network read.
        """
        ...

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
