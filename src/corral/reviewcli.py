"""`corral review`: write and read review records.

`record` is run by the reviewer process, never the author's shell: it
states the verdict for one commit of a ticket, and names the change's
author so a self-review is refused. `show` prints the record a commit
has, inherited across clean merge-ups of `--base`.

`--base` is trusted as given: a gate must pass the base the forge
reports for the PR, not a local ref the author's clone controls, or a
merge of unreviewed work could pass as a merge-up. A full sha is also
taken on trust when it does not resolve here, for a reviewer outside
the reviewed checkout.

Exit codes: 0 the commit has an approving record (`approve` or
`approve-with-findings`), 1 no record or a `block`, 4 usage (a bad
verdict, a missing author, a self-review, a commit that does not
resolve, a commit already reviewed).
"""

import argparse
import json
import os
import re
import sys

OK, NO, USAGE = 0, 1, 4

AGENT_ID_ENV = "CORRAL_AGENT_ID"

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


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


def cmd_show(args: argparse.Namespace) -> int:
    from corral import mergeup, reviewrecord
    from corral.diffscope import default_base

    cwd = os.getcwd()
    commit = _commit(args.commit, cwd)
    if commit is None:
        _err(f"--commit {args.commit!r} does not resolve to one commit here")
        return USAGE
    record, found = mergeup.find_record(
        lambda c: reviewrecord.read_record(args.repo, args.number, c),
        commit,
        default_base(args.base),
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
    p.add_argument("--commit", default="HEAD", help="the commit (default HEAD)")
    p.add_argument("--base", default=None, help="the PR base (default origin/master or $BASE_REF)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_show)
