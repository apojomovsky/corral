"""`corral leakcheck`: the leak guard as a verb.

Scans the commit messages in ``<base>..HEAD``, the current branch name, the
added diff lines of that range, and a PR body file when given, against the
ops root's private terms. Finding lines name the matched term, so this output
stays on the ops side: never paste it into the shared side.
"""

import argparse
import json
import os
import socket
import subprocess
import sys

from corral import diffscope, gitenv, leakguard, projects, settings
from corral.gitenv import clean_env

OK, LEAK, USAGE = 0, 1, 4

#: The version stamped on every `--json` document.
SCHEMA_VERSION = 1


class CollectError(Exception):
    """The range, the branch, the diff or the body file could not be read."""


def _git(args, cwd):
    """A local git call. Only local verbs run here; the network guard in the
    suite would fail the run on anything reaching the network."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=60, env=clean_env()
    )


def collect(base, cwd, *, git=_git, body_file=None):
    """`(messages, branch, diff, body)` for the range (all seams injected).

    `messages` is a list of ``(sha, message)`` pairs, `branch` the current
    branch name (empty when detached), `diff` the unified diff of the range,
    and `body` the body file text (None when no file was given).
    """
    proc = git(["log", "--format=format:%H%x00%B%x1e", f"{base}..HEAD"], cwd)
    if proc.returncode != 0:
        raise CollectError(f"cannot list {base}..HEAD: {(proc.stderr or '').strip()}")
    messages = []
    for record in proc.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, _, message = record.partition("\x00")
        messages.append((sha, message.strip("\n")))
    proc = git(["branch", "--show-current"], cwd)
    if proc.returncode != 0:
        raise CollectError(f"cannot read the branch name: {(proc.stderr or '').strip()}")
    branch = proc.stdout.strip()
    proc = git(["diff", "--no-color", "--no-ext-diff", f"{base}..HEAD", "--"], cwd)
    if proc.returncode != 0:
        raise CollectError(f"cannot diff {base}..HEAD: {(proc.stderr or '').strip()}")
    body = None
    if body_file is not None:
        try:
            with open(body_file, encoding="utf-8") as handle:
                body = handle.read()
        except (OSError, UnicodeDecodeError) as err:
            reason = getattr(err, "strerror", None) or str(err)
            raise CollectError(f"cannot read body file {body_file}: {reason}") from None
    return messages, branch, proc.stdout, body


def _checkout_root(cwd):
    """The checkout holding `cwd`: the directory with `.git` at or above it."""
    here = os.path.realpath(cwd)
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return os.path.realpath(cwd)
        here = parent


def _hostname():
    """The machine host name (a seam, so tests inject a fake)."""
    return socket.gethostname()


def _homedir():
    """The user's home directory (a seam, so tests inject a fake)."""
    return os.path.expanduser("~")


def find_ops_root(cwd, flag=None):
    """The ops root whose terms guard `cwd`, or None.

    `--ops` wins. Otherwise the nearest corral.toml above the clone covers
    the inline layout, and a registered project whose repo entry's
    `clone.path` matches the checkout covers the split layout (a worktree
    resolves through its main checkout, from any subdirectory). None means
    no ops root was found and only the clone-independent built-ins apply.
    A broken registration or ops file is an error, never a silent fallback:
    a gate fails closed.
    """
    if flag is not None:
        if not os.path.isfile(os.path.join(flag, settings.PROJECT_FILE)):
            raise CollectError(f"--ops {flag} has no {settings.PROJECT_FILE}")
        return os.path.abspath(flag)
    root = settings.find_root(cwd)
    if root is not None:
        return root
    checkout = _checkout_root(cwd)
    candidates = {os.path.realpath(cwd), checkout}
    main_checkout = gitenv.worktree_main_checkout(checkout)
    if main_checkout:
        candidates.add(os.path.realpath(main_checkout))
    registered = projects.load_all()
    errors = []
    matches: list[str] = []
    for project in registered:
        try:
            covered = any(
                settings.covering_repo(project.root, candidate) is not None
                for candidate in candidates
            )
        except settings.SettingsError as err:
            errors.append(err)
            continue
        if covered and project.root not in matches:
            matches.append(project.root)
    if len(matches) > 1:
        raise CollectError(
            "this clone matches several registered ops roots, so which terms guard it is "
            f"ambiguous: {', '.join(matches)}; pass --ops DIR"
        )
    if matches:
        return matches[0]
    if errors:
        raise errors[0]
    return None


def guard_terms(ops_root):
    """The private terms guarding `ops_root`, for every gate that needs them.

    One definition shared by `leakcheck` and `takeoff`: the host and home
    seams stay here, so both gates guard the same terms. `SettingsError`
    propagates; each gate reports it in its own way.
    """
    return leakguard.terms(
        ops_root,
        host=_hostname(),
        home=_homedir(),
        ops_name=os.path.basename(os.path.abspath(ops_root)) if ops_root else "",
    )


def _as_json(findings):
    return {
        "schema_version": SCHEMA_VERSION,
        "findings": [
            {
                "where": finding.where,
                "term": finding.term.text,
                "source": finding.term.source,
                "line": finding.line,
                "column": finding.column,
                "excerpt": finding.excerpt,
            }
            for finding in findings
        ],
    }


def cmd_leakcheck(args):
    """Run the verb. Finding lines name the matched term: ops side only."""
    cwd = os.getcwd()
    base = diffscope.default_base(args.base)
    try:
        ops_root = find_ops_root(cwd, args.ops)
    except (CollectError, settings.SettingsError) as err:
        print(f"corral leakcheck: {err}", file=sys.stderr)
        return USAGE
    try:
        found_terms = guard_terms(ops_root)
    except settings.SettingsError as err:
        print(f"corral leakcheck: {err}", file=sys.stderr)
        return USAGE
    try:
        messages, branch, diff, body = collect(base, cwd, body_file=args.body)
    except CollectError as err:
        print(f"corral leakcheck: {err}", file=sys.stderr)
        return USAGE
    findings = leakguard.check_messages(messages, found_terms)
    if branch:
        findings.extend(leakguard.check_branch(branch, found_terms))
    findings.extend(leakguard.check_diff(diff, found_terms))
    if body is not None:
        findings.extend(leakguard.check_body(body, found_terms))
    if ops_root is None:
        print(
            "corral leakcheck: no ops root found; only the clone-independent built-ins apply",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(_as_json(findings), indent=2, sort_keys=True))
        return LEAK if findings else OK
    for finding in findings:
        print(f"{finding.where}: {finding.term.text} ({finding.term.source})")
        print(f"  {finding.excerpt}")
    return LEAK if findings else OK


def add_parser(sub) -> None:
    """Register `leakcheck` on the top-level subparsers."""
    from corral.diffscope import BASE_HELP

    p = sub.add_parser(
        "leakcheck",
        help="scan messages, branch, body and added diff lines for private terms",
        description="Scan <base>..HEAD (commit messages, added diff lines), "
        "the current branch name, and a PR body file when given, against "
        "the ops root's private terms. Exit 1 names each finding. Finding "
        "lines name the matched term, so keep this output on the ops side: "
        "never paste it into the shared side.",
    )
    p.add_argument("--base", default=None, help=f"base ref to scan from ({BASE_HELP})")
    p.add_argument("--body", default=None, metavar="FILE", help="PR body file to scan")
    p.add_argument("--json", action="store_true", help="print the stable document")
    p.add_argument(
        "--ops",
        default=None,
        metavar="DIR",
        help="the ops root holding corral.toml (default: discovered from the clone)",
    )
    p.set_defaults(func=cmd_leakcheck)
