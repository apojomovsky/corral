"""`corral takeoff`: the project-level gate before a PR.

The ritual an agent passes before opening a pull request, driven by
settings: the tree is a worktree on a ticket branch, clean and merged up
with the remote base; every commit passes the commit-msg rules; no added
line and no PR body carries an em-dash; the body (when given) links the
ticket and carries a review record; nothing leaks a private term; the
head commit has an approving review record; and the configured takeoff
commands pass.

The base is the SHA `git ls-remote` reports for the default branch,
fetched fresh from the remote and verified present in the clone: a local
tracking ref is never trusted. The remote read (`ls_remote`) and the
check runner (`run_cmd`) are injected seams with no default, so tests use
fakes and temp repositories; the CLI wires the real ones. Every other git
call here is a local verb. Exit codes: 0 every check ok or skipped,
1 a check failed, 3 the remote is unreachable, 4 usage or configuration.
"""

import functools
import importlib.machinery
import importlib.util
import os
import re
import subprocess

from typing import NamedTuple

from corral import detect, gitenv, leakcli, leakguard, mergeup, reviewrecord, settings, worktrees
from corral.github import gh

#: The version stamped on every `--json` document.
SCHEMA_VERSION = 1

#: Statuses a check reports: passed, failed, or not applicable.
OK, FAIL, SKIPPED = "ok", "fail", "skipped"

#: Never written literally: the commit-msg hook refuses it anywhere.
EM_DASH = chr(0x2014)
settings.register("checks.takeoff", list, [], "takeoff gate commands, run in order in the worktree")
settings.register("checks.timeout_s", int, 1800, "seconds before a takeoff command is killed")

settings.register(
    "checks.no_em_dash", bool, True, "takeoff refuses added lines and bodies with an em-dash"
)
settings.register(
    "review.required", bool, True, "takeoff needs an approving review record for the head"
)


class TakeoffError(Exception):
    """Usage or configuration keeps takeoff from running (exit 4)."""


class LsRemoteError(Exception):
    """The remote could not be read (exit 3, fail closed)."""


class Check(NamedTuple):
    """One gate row: the check name, its status, and detail plus fix lines."""

    name: str
    status: str
    detail: str
    fix: str


class Result(NamedTuple):
    """The gate outcome: the remote base SHA (None when unread) and every check."""

    base: str | None
    checks: list[Check]
    exit_code: int


def production_ls_remote(branch, cwd):
    """The SHA `git ls-remote` reports for `origin`'s `branch` (the one seam)."""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=gitenv.clean_env(),
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as err:
        raise LsRemoteError(
            f"cannot read origin/refs/heads/{branch} from the remote: {err}"
        ) from None
    if proc.returncode != 0:
        raise LsRemoteError((proc.stderr or "").strip() or "git ls-remote failed")
    for line in (proc.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and re.fullmatch(r"[0-9a-fA-F]+", fields[0]):
            return fields[0].lower()
    raise LsRemoteError(f"origin has no branch {branch!r}")


def production_run_cmd(cmd, cwd, timeout=None):
    """Run one takeoff command string in the worktree (the other seam).

    `timeout` kills a hanging command; None means the registered
    `checks.timeout_s` default, so direct calls agree with the gate.
    """
    if timeout is None:
        default = settings.REGISTRY["checks.timeout_s"].default
        timeout = default if isinstance(default, int) else 1800
    return subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=gitenv.clean_env(),
        timeout=timeout,
    )


def _git(args, cwd):
    """A local git call: a CompletedProcess; a missing git fails closed."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=gitenv.clean_env(),
            timeout=worktrees.GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise TakeoffError(f"cannot run git in {cwd}: {err}") from None


def _commit_rules():
    """The packaged commit-msg hook as a module: one rule for hook and gate."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "commit-msg")
    loader = importlib.machinery.SourceFileLoader("corral_takeoff_commit_msg", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise TakeoffError(f"{path}: cannot load the commit-msg hook")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _ticket_repo(ops_root, main_clone):
    """`(repo, matched)`: the [[repo]] entry whose clone.path is the main clone.

    Falls back to the main clone's directory name when no ops root or no
    entry covers it, so the gate still runs: the fallback derives the same
    name for writing and reading a record. Two entries covering the same
    clone are refused instead of picked by name.
    """
    if ops_root is not None:
        try:
            name = settings.covering_repo(ops_root, main_clone)
        except settings.SettingsError as err:
            raise TakeoffError(str(err)) from None
        if name is not None:
            return name, True
    return os.path.basename(main_clone), False


def _check_base(branch, cwd, ls_remote):
    """The base check, or `(None, fail, unreachable)` when the remote is mute."""
    try:
        sha = ls_remote(branch, cwd)
    except (LsRemoteError, subprocess.TimeoutExpired, OSError) as err:
        detail = f"cannot read origin/refs/heads/{branch} from the remote: {err}"
        return None, Check("base", FAIL, detail, f"git fetch origin {branch}"), True
    if not sha or _git(["cat-file", "-e", sha], cwd).returncode != 0:
        return (
            None,
            Check("base", FAIL, f"base {sha} is not in this clone", f"git fetch origin {branch}"),
            False,
        )
    return sha, Check("base", OK, sha, ""), False


def _check_worktree(toplevel, main_clone, root_setting, base_sha, default_branch):
    """The worktree check plus the branch name and its ticket number (or None)."""
    try:
        real_root = worktrees.root(main_clone, root_setting)
    except worktrees.WorktreeError as err:
        raise TakeoffError(str(err)) from None
    if not worktrees.inside(toplevel, real_root):
        return (
            Check(
                "worktree",
                FAIL,
                f"{toplevel} is not a worktree under {real_root}/",
                f"git worktree add {real_root}/<n>-<slug> -b <type>/<n>-<slug> "
                f"origin/{default_branch}",
            ),
            "",
            None,
        )
    branch = _git(["branch", "--show-current"], toplevel).stdout.strip()
    if not branch:
        return (
            Check(
                "worktree",
                FAIL,
                "the tree is on a detached HEAD",
                "check out the ticket branch for this ticket",
            ),
            "",
            None,
        )
    match = worktrees.TICKET_BRANCH_RE.match(branch)
    if match is None:
        return (
            Check(
                "worktree",
                FAIL,
                f"branch {branch!r} is not <type>/<issue>-<slug>",
                "check out the ticket branch for this ticket",
            ),
            branch,
            None,
        )
    status = _git(["status", "--porcelain"], toplevel)
    if status.returncode != 0:
        raise TakeoffError(f"cannot read the worktree status: {(status.stderr or '').strip()}")
    if status.stdout.strip():
        return (
            Check(
                "worktree",
                FAIL,
                "uncommitted changes: commit or stash them",
                "commit or stash the uncommitted changes",
            ),
            branch,
            int(match.group("number")),
        )
    behind = _git(["rev-list", "--count", f"HEAD..{base_sha}", "--"], toplevel)
    count = (behind.stdout or "").strip()
    if behind.returncode != 0:
        raise TakeoffError(f"cannot compare HEAD with {base_sha}: {(behind.stderr or '').strip()}")
    if count != "0":
        return (
            Check(
                "worktree",
                FAIL,
                f"{count} commit(s) behind the base",
                f"git merge {base_sha}",
            ),
            branch,
            int(match.group("number")),
        )
    return Check("worktree", OK, "", ""), branch, int(match.group("number"))


def _leak_terms(ops_root):
    """The private terms guarding `ops_root`, usable before any check runs."""
    try:
        return leakcli.guard_terms(ops_root)
    except settings.SettingsError as err:
        raise TakeoffError(str(err)) from None


def _scrub(detail, terms):
    """`detail` with every private term replaced, so takeoff output stays pastable.

    The rows that echo repository content (commit messages, added lines,
    command output) are scrubbed with the same terms the leak check uses:
    with no leak the text is untouched, with one the term reads `[redacted]`.
    """
    for term in terms:
        if term.kind == "pattern":
            try:
                detail = re.compile(term.text, re.IGNORECASE).sub("[redacted]", detail)
            except re.error:
                continue
        elif term.text:
            detail = re.compile(re.escape(term.text), re.IGNORECASE).sub("[redacted]", detail)
    return detail


def _check_commits(rules, conventional, toplevel, base, scrub):
    """Every commit in `<base>..HEAD` passes the commit-msg rules."""
    if not conventional:
        return Check("commits", SKIPPED, "git.commits.conventional is false", "")
    log = _git(["log", "--format=%H%x00%B%x01", f"{base}..HEAD"], toplevel)
    if log.returncode != 0:
        raise TakeoffError(f"cannot list {base}..HEAD: {(log.stderr or '').strip()}")
    found = []
    for entry in log.stdout.split("\x01"):
        if not entry.strip():
            continue
        sha, _, message = entry.strip().partition("\x00")
        parents = _git(["rev-list", "--parents", "-n", "1", sha], toplevel).stdout.split()
        for problem in rules.problems(message, len(parents) > 2):
            found.append(f"commit {sha[:7]}: {problem}")
    if found:
        return Check(
            "commits",
            FAIL,
            scrub("; ".join(found)),
            "reword the commits so each passes the commit-msg rules",
        )
    return Check("commits", OK, "", "")


def _added_lines_with(base, toplevel, needle):
    """`[(line)]` excerpts of added lines carrying `needle` in `<base>...HEAD`."""
    diff = _git(["diff", "--no-color", "--no-ext-diff", f"{base}...HEAD", "--"], toplevel)
    if diff.returncode != 0:
        raise TakeoffError(f"cannot diff {base}...HEAD: {(diff.stderr or '').strip()}")
    return [
        line[1:80].strip()
        for line in diff.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++") and needle in line
    ]


def _check_em_dash(enabled, toplevel, base, body_text, scrub):
    """No added line and no PR body carries an em-dash."""
    if not enabled:
        return Check("em-dash", SKIPPED, "checks.no_em_dash is false", "")
    found = [
        f"em-dash in an added line: {excerpt!r}"
        for excerpt in _added_lines_with(base, toplevel, EM_DASH)
    ]
    if body_text is not None and EM_DASH in body_text:
        found.append("em-dash in the PR body")
    if found:
        return Check(
            "em-dash",
            FAIL,
            scrub("; ".join(found)),
            "replace each em-dash with a comma, colon or period",
        )
    return Check("em-dash", OK, "", "")


def _check_body(text, number):
    """The PR body: real newlines, a link to the ticket, a review record."""
    prose = gh.body_text_outside_code(text)
    found = []
    if "\\n" in prose or "\\r" in prose:
        found.append("literal \\n in the PR body: write it to a file with real newlines")
    if number is None:
        found.append("cannot parse the ticket number from the branch: no link to check")
    elif number not in set(gh.body_linked_issue_numbers(text)):
        found.append(
            f"the body does not link #{number}: start it with `Closes #{number}` "
            "(or `Refs` for a partial win)"
        )
    if not gh.has_review_record(text):
        found.append("no `## Review` section, or it is empty")
    if found:
        return Check(
            "body",
            FAIL,
            "; ".join(found),
            "give the body real newlines, a closing link and a `## Review` record",
        )
    return Check("body", OK, "", "")


def _check_leak(toplevel, base, body_file, found_terms):
    """No commit message, branch name, added line or body leaks a private term."""
    try:
        messages, branch, diff, body = leakcli.collect(base, toplevel, body_file=body_file)
    except leakcli.CollectError as err:
        raise TakeoffError(str(err)) from None
    findings = leakguard.check_messages(messages, found_terms)
    if branch:
        findings.extend(leakguard.check_branch(branch, found_terms))
    findings.extend(leakguard.check_diff(diff, found_terms))
    if body is not None:
        findings.extend(leakguard.check_body(body, found_terms))
    if findings:
        # The detail names the count only: takeoff output may be pasted
        # into a PR, and finding lines would name the matched terms.
        return Check(
            "leak",
            FAIL,
            f"{len(findings)} private-term finding(s); "
            "run `corral leakcheck` on the ops side for the terms",
            "remove the private terms from the commits, branch, diff and body",
        )
    return Check("leak", OK, "", "")


def _check_review(required, repo, number, toplevel, base):
    """The head commit carries an approving review record."""
    if not required:
        return Check("review", SKIPPED, "review.required is false", "")
    if number is None:
        return Check(
            "review",
            FAIL,
            "cannot parse the ticket number from the branch: no record to look up",
            "check out the ticket branch for this ticket",
        )
    head = _git(["rev-parse", "HEAD"], toplevel).stdout.strip()
    record, found = mergeup.find_record(
        lambda commit: reviewrecord.read_record(repo, number, commit), head, base, toplevel
    )
    short = reviewrecord.commit_id(head)
    inherited = (
        f" (inherited from {reviewrecord.commit_id(found)} across a clean merge-up)"
        if found and found != head
        else ""
    )
    fix = (
        f"ask a reviewer for a verdict on {short}, then record it: "
        f"`corral review record {repo} {number} --commit {head} "
        "--verdict <approve|block> --author <change-author> --reviewer <reviewer-id>`"
    )
    if record is None:
        return Check("review", FAIL, f"no approving review record for commit {short}", fix)
    if not reviewrecord.approves(record):
        return Check(
            "review",
            FAIL,
            f"the review record for commit {short} is a block: answer its "
            "findings with a new commit and a fresh review",
            fix,
        )
    if inherited:
        return Check("review", OK, f"approved{inherited}", "")
    return Check("review", OK, "", "")


def _expired_tail(err):
    """The last 20 lines of a timed-out command's partial output."""
    out = err.stdout
    errs = err.stderr
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    if isinstance(errs, bytes):
        errs = errs.decode("utf-8", "replace")
    return "\n".join(((out or "") + (errs or "")).splitlines()[-20:])


def _check_commands(commands, test_command, with_test, toplevel, run_cmd, scrub, timeout_s):
    """Each configured takeoff command exits 0, with the tail shown on failure."""
    if not isinstance(commands, list) or any(not isinstance(c, str) for c in commands):
        raise TakeoffError("checks.takeoff must be a list of command strings")
    # The default is [checks.test] when it is set, else empty; --test adds
    # checks.test when it is not already listed.
    wanted = list(commands) or ([test_command] if test_command else [])
    if with_test and test_command and test_command not in wanted:
        wanted.append(test_command)
    if not wanted:
        return Check("checks", OK, "no takeoff commands configured", "")
    for cmd in wanted:
        try:
            proc = run_cmd(cmd, toplevel)
        except subprocess.TimeoutExpired as err:
            tail = _expired_tail(err)
            detail = f"`{cmd}` exceeded checks.timeout_s ({timeout_s}s)"
            if tail.strip():
                detail += f":\n{tail}"
            return Check(
                "checks",
                FAIL,
                scrub(detail),
                f"raise checks.timeout_s or fix the hang: run `{cmd}` in the worktree",
            )
        except Exception as err:
            return Check(
                "checks",
                FAIL,
                f"`{cmd}` could not run: {err}",
                f"run `{cmd}` in the worktree and fix what it reports",
            )
        if proc is None or getattr(proc, "returncode", 1) != 0:
            code = getattr(proc, "returncode", "?")
            text = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
            tail = "\n".join(text.splitlines()[-20:])
            detail = f"`{cmd}` exited {code}"
            if tail.strip():
                detail += f":\n{tail}"
            return Check(
                "checks",
                FAIL,
                scrub(detail),
                f"run `{cmd}` in the worktree and fix what it reports",
            )
    return Check("checks", OK, "; ".join(f"`{c}`" for c in wanted) if wanted else "", "")


def run_takeoff(path, *, body_file=None, with_test=False, ops_dir=None, ls_remote, run_cmd):
    """Run the takeoff gate over `path`; returns a Result (exit 3 only early).

    `ls_remote(branch, cwd)` returns the remote base SHA or raises
    LsRemoteError; `run_cmd(cmd, cwd)` runs one takeoff command. Both are
    required: no default may silently answer for the remote or the suite.
    Usage and configuration problems raise TakeoffError (exit 4).
    """
    if ls_remote is None or run_cmd is None:
        raise TypeError("run_takeoff needs ls_remote and run_cmd seams")
    if not path or not os.path.isdir(path):
        raise TakeoffError(f"{path}: not a directory")
    toplevel_proc = _git(["rev-parse", "--show-toplevel"], os.path.realpath(path))
    if toplevel_proc.returncode != 0:
        raise TakeoffError(f"{path}: not a git checkout")
    toplevel = os.path.realpath(toplevel_proc.stdout.strip() or path)
    try:
        ops_root = leakcli.find_ops_root(toplevel, ops_dir)
    except (leakcli.CollectError, settings.SettingsError) as err:
        raise TakeoffError(str(err)) from None
    main_clone = os.path.realpath(gitenv.worktree_main_checkout(toplevel) or toplevel)
    repo, matched = _ticket_repo(ops_root, main_clone)
    try:
        if matched:
            resolved = settings.load(root=ops_root, repo=repo)
        else:
            resolved = settings.load(root=ops_root)
    except settings.SettingsError as err:
        raise TakeoffError(str(err)) from None
    if ops_root is None:
        default_branch = detect.default_branch(toplevel, _git) or resolved["clone.default_branch"]
    else:
        default_branch = resolved["clone.default_branch"]
    if not isinstance(default_branch, str) or not default_branch:
        raise TakeoffError("clone.default_branch must be a non-empty branch name")
    base, base_check, unreachable = _check_base(default_branch, toplevel, ls_remote)
    if base is None:
        return Result(None, [base_check], 3 if unreachable else 1)
    checks = [base_check]
    worktree_check, _branch, number = _check_worktree(
        toplevel, main_clone, resolved["git.worktrees"], base, default_branch
    )
    checks.append(worktree_check)
    rules = _commit_rules()
    found_terms = _leak_terms(ops_root)

    def scrub(detail):
        return _scrub(detail, found_terms)

    checks.append(
        _check_commits(rules, resolved["git.commits.conventional"], toplevel, base, scrub)
    )
    if body_file is not None:
        try:
            with open(body_file, encoding="utf-8") as handle:
                body_text: str | None = handle.read()
        except (OSError, UnicodeDecodeError) as err:
            raise TakeoffError(f"cannot read body file {body_file}: {err}") from None
    else:
        body_text = None
    checks.append(_check_em_dash(resolved["checks.no_em_dash"], toplevel, base, body_text, scrub))
    if body_text is None:
        checks.append(Check("body", SKIPPED, "no --body file given", ""))
    else:
        checks.append(_check_body(body_text, number))
    checks.append(_check_leak(toplevel, base, body_file, found_terms))
    checks.append(_check_review(resolved["review.required"], repo, number, toplevel, base))
    timeout_s = resolved["checks.timeout_s"]
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        raise TakeoffError("checks.timeout_s must be a positive number of seconds")
    if run_cmd is production_run_cmd:
        run_cmd = functools.partial(production_run_cmd, timeout=timeout_s)
    checks.append(
        _check_commands(
            resolved["checks.takeoff"],
            resolved["checks.test"],
            with_test,
            toplevel,
            run_cmd,
            scrub,
            timeout_s,
        )
    )
    if any(check.status == FAIL for check in checks):
        return Result(base, checks, 1)
    return Result(base, checks, 0)
