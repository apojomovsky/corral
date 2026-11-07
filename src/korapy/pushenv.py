"""Pushing when the host has no usable SSH agent.

A clone whose origin is an SSH GitHub address cannot push when the
keyring SSH agent socket exists but refuses connections. The launch
detects that up front and gives the unit a per-launch git environment
that rewrites SSH GitHub addresses to HTTPS and authenticates through
the host's existing `gh` login (the `gh auth git-credential` helper).
Only environment variables change (`GIT_CONFIG_COUNT` and friends,
plus `GIT_TERMINAL_PROMPT=0`): nothing is written to the user's git
config or to the clone's config.

`decide` is the one pure decision over plain data (probe result,
origin URL form, `gh` login); `resolve` is the one resolver every
pushing path (launch, resume, reviewer launches, `korapy push`,
`korapy doctor`) reads it through. The SSH probe and the `gh` check
are seams with fakes. Tokens are never stored or logged: git invokes
the credential helper at push time, as in a normal `gh` setup.
"""

import os
import re
import subprocess
from dataclasses import dataclass

#: Exit code of the refusal: a configuration problem, like every
#: other launch refusal for a host that cannot do the work.
REFUSAL_EXIT_CODE = 4

#: The scp-like SSH GitHub address form git prints by default.
#: Matched case-sensitively, the way git matches `insteadOf` prefixes.
_SSH_SCP_PREFIX = "git@github.com:"

#: The explicit ssh-scheme SSH GitHub address form, with an optional
#: numeric port (`ssh://git@github.com/owner/repo.git`,
#: `ssh://git@github.com:22/owner/repo.git`). Matched
#: case-sensitively, the way git matches `insteadOf` prefixes.
_SSH_SCHEME_RE = re.compile(r"^ssh://git@github\.com(?::(\d+))?/")

#: Entries laid over the base environment for the HTTPS fallback: one
#: rewrite per rewritable SSH address form (scp-like, bare ssh-scheme,
#: and the explicit-port ssh-scheme form) and the host `gh` login as
#: the credential helper for github.com.
_FALLBACK_ENTRIES = (
    ("url.https://github.com/.insteadOf", "git@github.com:"),
    ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
    ("url.https://github.com/.insteadOf", "ssh://git@github.com:22/"),
    ("credential.https://github.com.helper", "!gh auth git-credential"),
)

#: A departing agent must never hang on a password prompt.
_TERMINAL_PROMPT = ("GIT_TERMINAL_PROMPT", "0")

#: Files that count as "a key that can be used" when no agent socket
#: answers: the default private key names ssh tries on its own. An
#: encrypted key still needs its agent; the probe cannot tell cheaply,
#: so the reading stays best-effort and the doctor row keeps it visible.
_KEY_FILES = ("id_ed25519", "id_ecdsa", "id_rsa")

#: The `ssh-add -l` exit codes that mean the socket answered: keys
#: listed, or no identities loaded (the agent is up either way).
#: Anything else (2 on a refused socket) falls through to the key files.
_AGENT_ANSWERED = (0, 1)

SSH_ADD_TIMEOUT_S = 10

HTTPS_NOTE = "no usable SSH agent: pushing over HTTPS with the host gh login"

REFUSAL = (
    "cannot push: no usable SSH agent and gh is not logged in. "
    "Restore the SSH agent (or add a key usable without one), "
    "or run `gh auth login`; refusing to start an agent that cannot push"
)


@dataclass(frozen=True)
class Decision:
    """How this launch pushes: "ssh", "https", or "refuse"."""

    mode: str
    note: str


class PushRefused(Exception):
    """Neither SSH nor the host `gh` login can push: exit 4."""

    exit_code = REFUSAL_EXIT_CODE


def is_ssh_github_url(url) -> bool:
    """True for the SSH GitHub address forms the fallback handles.

    The scp-like form, and the ssh-scheme form with an optional numeric
    port. Matched case-sensitively: git's `insteadOf` prefix match is
    case-sensitive, so an uppercase address takes the plain SSH path
    (or the refusal) rather than a fallback that would silently miss.
    """
    text = (url or "").strip()
    if text.startswith(_SSH_SCP_PREFIX):
        return True
    return _SSH_SCHEME_RE.match(text) is not None


def _can_rewrite(url) -> bool:
    """True when the fallback env rewrites this SSH GitHub address.

    The env carries one `insteadOf` entry per known form (scp-like,
    bare ssh-scheme, explicit `:22` ssh-scheme). A form the env has no
    entry for (another explicit port) is recognized but not rewritten:
    the decision refuses rather than starting an agent whose push the
    fallback cannot fix.
    """
    text = (url or "").strip()
    if text.startswith(_SSH_SCP_PREFIX):
        return True
    match = _SSH_SCHEME_RE.match(text)
    if match is None:
        return False
    return match.group(1) in (None, "22")


def decide(ssh_ok: bool, origin, gh_ok: bool) -> Decision:
    """The push path for an origin, given the probe and login readings.

    Origins that are not SSH GitHub addresses push as they always did:
    HTTPS remotes, other hosts' SSH remotes and local paths never need
    the agent, so a dead agent and a logged-out `gh` change nothing for
    them and no seam ever runs. An SSH GitHub address the fallback
    cannot rewrite (an explicit port with no `insteadOf` entry) refuses
    with both fixes, even when `gh` is logged in.
    """
    if not is_ssh_github_url(origin):
        return Decision("ssh", "")
    if ssh_ok:
        return Decision("ssh", "")
    if gh_ok and _can_rewrite(origin):
        return Decision("https", HTTPS_NOTE)
    return Decision("refuse", REFUSAL)


def https_env() -> dict:
    """The per-launch fallback environment: rewrites, helper, no prompts."""
    env = {}
    for index, (key, value) in enumerate(_FALLBACK_ENTRIES):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(len(_FALLBACK_ENTRIES))
    env[_TERMINAL_PROMPT[0]] = _TERMINAL_PROMPT[1]
    return env


def extra_env(decision: Decision, base=None) -> dict:
    """`base` (default: {}) plus the fallback entries for an https decision."""
    merged = dict(base or {})
    if decision.mode == "https":
        merged.update(https_env())
    return merged


def origin_url(clone, git):
    """The clone's `origin` URL, or None when unreadable. Read-only.

    `git(args, cwd)` is the git seam. Any failure (no git, no remote,
    an empty URL) reads as None: the caller pushes as before.
    """
    try:
        proc = git(["remote", "get-url", "origin"], clone)
    except Exception:  # noqa: BLE001 - a failed origin read pushes as before
        return None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return None
    return (getattr(proc, "stdout", "") or "").strip() or None


def default_ssh_probe(run=None, home=None) -> bool:
    """True when an agent socket answers or a default key file exists.

    `run(argv)` runs `ssh-add -l` (production: a local subprocess call);
    `home` is the directory holding `.ssh` (production: the user's
    home). Both injectable; tests pass fakes and temp dirs.
    """
    if run is None:

        def run(argv):
            return subprocess.run(argv, capture_output=True, text=True, timeout=SSH_ADD_TIMEOUT_S)

    try:
        proc = run(["ssh-add", "-l"])
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and getattr(proc, "returncode", 2) in _AGENT_ANSWERED:
        return True
    base = home if home is not None else os.path.expanduser("~")
    ssh_dir = os.path.join(base, ".ssh")
    return any(os.path.exists(os.path.join(ssh_dir, name)) for name in _KEY_FILES)


def default_gh_login(gh_run=None) -> bool:
    """True when the host `gh` is logged in. Read-only.

    `gh_run()` runs `gh auth status` (production: through the one gh
    seam); any failure reads as logged out. Tests pass a fake.
    """
    if gh_run is None:
        from korapy.github import gh as _gh

        def gh_run():
            _gh.run(["auth", "status"])

    try:
        gh_run()
    except Exception:  # noqa: BLE001 - any gh failure reads as logged out
        return False
    return True


def resolve(origin, *, ssh_probe=None, gh_login=None) -> Decision:
    """The one resolver for how this launch pushes.

    The seams run lazily: origins that are not SSH GitHub addresses
    decide without ever probing, so local and HTTPS clones never touch
    ssh-add or gh. `ssh_probe()` answers whether SSH can work,
    `gh_login()` whether the host `gh` is logged in; None takes the
    production default in each case.
    """
    if not is_ssh_github_url(origin):
        return Decision("ssh", "")
    probe = default_ssh_probe if ssh_probe is None else ssh_probe
    if probe():
        return Decision("ssh", "")
    login = default_gh_login if gh_login is None else gh_login
    return decide(False, origin, login())


def launch_env(clone, *, git, ssh_probe=None, gh_login=None) -> dict:
    """The extra unit environment for `clone`: the fallback, or {}.

    The one resolver (`resolve`) decides from the clone's origin URL;
    anything that refuses to push raises `PushRefused` (exit 4) naming
    both fixes instead of starting an agent that cannot push.
    """
    decision = resolve(origin_url(clone, git), ssh_probe=ssh_probe, gh_login=gh_login)
    if decision.mode == "refuse":
        raise PushRefused(decision.note)
    return extra_env(decision)
