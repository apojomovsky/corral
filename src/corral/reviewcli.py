"""`corral review`: write and read review records.

`record` is run by the reviewer process, never the author's shell: it
states the verdict for one commit of a ticket, and names the change's
author so a self-review is refused. `show` prints the record a commit
has, inherited across clean merge-ups of the PR base.

The base is authoritative, never a local ref the author's clone
controls: `show` needs `--pr`, and the base is the PR's `base_sha`
from the forge, fetched fresh. The commit under review must be the
PR's `head_sha` (`--head` pins it explicitly and must match). With no
authoritative base (no `--pr`, an unreachable forge, or a base sha
absent from the clone) the verb fails closed.

Exit codes: 0 the commit has an approving record (`approve` or
`approve-with-findings`), 1 no record, a `block`, or no authoritative
base, 3 the forge is unreachable, 4 usage (a bad verdict, a missing
author, a self-review, a commit that does not resolve, a commit
already reviewed, a bad `--forge-repo`).
"""

import argparse
import json
import os
import re
import subprocess
import sys

OK, NO, UNREACHABLE, USAGE = 0, 1, 3, 4

AGENT_ID_ENV = "CORRAL_AGENT_ID"

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")

#: `owner/name` for the forge lookup.
_FORGE_REPO = re.compile(r"[^/\s]+/[^/\s]+")


def _err(text):
    print(f"corral: {text}", file=sys.stderr)


def _git(args, cwd):
    """A local git seam for the origin lookup. None when git cannot run."""
    from corral.gitenv import clean_env

    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _production_forge():
    """The live forge over the production `gh` runner."""
    from corral.github import forge as ghforge
    from corral.github.gh import paced_run

    return ghforge.GithubForge(paced_run)


def _err(text):
    print(f"corral: {text}", file=sys.stderr)


def _commit(raw, cwd):
    """The full SHA a reviewer named, or None when it cannot be trusted.

    A short or mistyped id would be hashed into a record file nobody ever
    looks up, so only an id git resolves here, or a full SHA from a
    reviewer outside the reviewed checkout, is taken.
    """
    from corral import mergeup

    raw = str(raw or "").strip()
    resolved = mergeup.resolve_commit(raw, cwd)
    if resolved:
        return resolved
    if _FULL_SHA.fullmatch(raw):
        return raw.lower()
    return None


def cmd_record(args: argparse.Namespace) -> int:
    from corral import reviewrecord

    reviewer = args.reviewer or os.environ.get(AGENT_ID_ENV, "")
    if not reviewer:
        _err(f"review record needs --reviewer (or {AGENT_ID_ENV})")
        return USAGE
    commit = _commit(args.commit, os.getcwd())
    if commit is None:
        _err(
            f"review record refused: --commit {args.commit!r} does not resolve to one "
            "commit here; pass the full sha (git rev-parse HEAD) or run from the "
            "reviewed checkout"
        )
        return USAGE
    findings = []
    for raw in args.finding:
        severity, sep, text = str(raw).partition(":")
        if not sep:
            severity, text = "", severity
        findings.append({"severity": severity.strip() or "note", "text": text.strip()})
    try:
        path = reviewrecord.write_record(
            args.repo,
            args.number,
            commit,
            reviewer=reviewer,
            verdict=args.verdict,
            findings=findings,
            question=args.question,
            session=args.session,
            author=args.author,
        )
    except ValueError as err:
        _err(f"review record refused: {err}")
        return USAGE
    print(path)
    return OK


def cmd_show(args: argparse.Namespace, forge=None) -> int:
    from corral import forge as iface, mergeup, reviewrecord
    from corral.github import forge as ghforge

    if args.pr is None:
        _err("review show needs --pr N: the base is the PR's base_sha from the forge")
        return NO
    forge_repo = args.forge_repo or ghforge.origin_repo(os.getcwd(), _git)
    if forge_repo is None:
        _err("review show needs --forge-repo owner/name: no forge repo in the origin remote")
        return NO
    if not _FORGE_REPO.fullmatch(forge_repo):
        _err(f"review show needs --forge-repo as owner/name, got {forge_repo!r}")
        return USAGE
    adapter = forge if forge is not None else _production_forge()
    try:
        pr = adapter.pull_request(forge_repo, args.pr)
    except iface.ForgeError as err:
        _err(f"review show: forge unreachable: {err}")
        return UNREACHABLE
    cwd = os.getcwd()
    commit = _commit(args.commit, cwd)
    if commit is None:
        _err(f"--commit {args.commit!r} does not resolve to one commit here")
        return USAGE
    if args.head is not None and args.head != pr.head_sha:
        _err(f"--head {args.head} is not the head {pr.head_sha} of {forge_repo}#{args.pr}")
        return NO
    want = args.head or pr.head_sha
    if commit != want:
        _err(f"commit {commit} is not the head {want} of {forge_repo}#{args.pr}")
        return NO
    if mergeup.resolve_commit(pr.base_sha, cwd) is None:
        _err(f"PR base {pr.base_sha} is not present in this clone")
        return NO
    record, found = mergeup.find_record(
        lambda c: reviewrecord.read_record(args.repo, args.number, c),
        commit,
        pr.base_sha,
        cwd,
    )
    if record is None:
        _err(
            f"no review record for {args.repo} {args.number} commit {reviewrecord.commit_id(commit)}"
        )
        return NO
    if args.json:
        print(
            json.dumps(
                dict(record, inherited_from=found if found != commit else None), sort_keys=True
            )
        )
        return OK if reviewrecord.approves(record) else NO
    inherited = (
        ""
        if found == commit
        else f" (inherited from {reviewrecord.commit_id(found)} across a clean merge-up)"
    )
    print(f"{record['verdict']} by {record['reviewer']} on {record['commit']}{inherited}")
    for finding in record.get("findings") or []:
        print(f"- {finding.get('severity', 'note')}: {finding.get('text', '')}")
    return OK if reviewrecord.approves(record) else NO


def add_parser(sub) -> None:
    """Register `review` and its verbs on the top-level subparsers."""
    from corral import reviewrecord

    ticket = argparse.ArgumentParser(add_help=False)
    ticket.add_argument("repo", help="the ticket's repository name")
    ticket.add_argument("number", type=int, help="the ticket number")
    review = sub.add_parser("review", help="review records: proof a separate reviewer looked")
    verbs = review.add_subparsers(dest="review_verb", required=True)
    p = verbs.add_parser("record", parents=[ticket], help="state a verdict on one commit")
    p.add_argument(
        "--commit", required=True, help="the reviewed commit (a full sha, or a ref here)"
    )
    p.add_argument("--verdict", required=True, choices=reviewrecord.VERDICTS, help="the verdict")
    p.add_argument("--finding", action="append", default=[], help="severity:text (repeatable)")
    p.add_argument("--reviewer", default=None, help=f"the reviewer's agent id (or {AGENT_ID_ENV})")
    p.add_argument(
        "--author", required=True, help="the change's author; refused as its own reviewer"
    )
    p.add_argument("--question", default="", help="the one question the review answered")
    p.add_argument("--session", default="", help="the reviewer's own session")
    p.set_defaults(func=cmd_record)
    p = verbs.add_parser("show", parents=[ticket], help="the record a commit has, or inherits")
    p.add_argument(
        "--commit", default="HEAD", help="the commit (default HEAD, must be the PR head)"
    )
    p.add_argument(
        "--pr",
        type=int,
        default=None,
        help="the PR number: the base is the PR's base_sha from the forge (required)",
    )
    p.add_argument(
        "--forge-repo",
        default=None,
        help="owner/name for the forge lookup (default: the clone's origin remote)",
    )
    p.add_argument(
        "--head",
        default=None,
        help="pin the expected PR head sha (must equal the forge head)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_show)
