#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Takeoff ritual before a PR: `make pre-pr-check PR_BODY=<file> [TEST=1]`.

Blocking checks (exit 1, with a fix line each):
  - the tree is a worktree under `.worktrees/`, on a `<type>/<n>-<slug>` branch
  - the tree is clean, and not behind `origin/master`
  - every commit since `origin/master` passes the commit-msg rules
  - no added line carries an em-dash
  - the PR body has real newlines, links the branch's ticket with
    `Closes #N` or `Refs #N`, and has a non-empty `## Review` section
  - with --test, the suite passes

Exit 4 on a usage error (no body file).
"""

import argparse
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "origin/master"
BRANCH_RE = re.compile(r"^[a-z]+/(\d+)-[\w.-]+$")
LINK_RE = re.compile(r"\b(?:closes|fixes|resolves|refs)\s+#(\d+)\b", re.IGNORECASE)
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
REVIEW_RE = re.compile(r"^## Review[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
EM_DASH = "—"


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def commit_rules():
    """The commit-msg hook as a module, so takeoff and the hook share one rule."""
    path = os.path.join(ROOT, ".githooks", "commit-msg")
    loader = importlib.machinery.SourceFileLoader("commit_msg_hook", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def check_tree(cwd):
    found = []
    top = git(["rev-parse", "--show-toplevel"], cwd).stdout.strip()
    if f"{os.sep}.worktrees{os.sep}" not in top + os.sep:
        found.append(
            f"{top} is not a worktree under .worktrees/: "
            "git worktree add .worktrees/<n>-<slug> -b <type>/<n>-<slug> origin/master"
        )
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()
    if not BRANCH_RE.match(branch):
        found.append(f"branch {branch!r} is not <type>/<issue>-<slug>")
    if git(["status", "--porcelain"], cwd).stdout.strip():
        found.append("uncommitted changes: commit or stash them")
    if git(["rev-parse", "--verify", "--quiet", BASE], cwd).returncode != 0:
        found.append(f"{BASE} does not resolve: git fetch origin")
        return found, branch
    behind = git(["rev-list", "--count", f"HEAD..{BASE}"], cwd).stdout.strip()
    if behind not in ("", "0"):
        found.append(f"{behind} commit(s) behind {BASE}: git merge {BASE}")
    return found, branch


def check_commits(cwd, rules):
    found = []
    log = git(["log", "--format=%H%x00%B%x01", f"{BASE}..HEAD"], cwd).stdout
    for entry in filter(str.strip, log.split("\x01")):
        sha, _, message = entry.strip().partition("\x00")
        merge = len(git(["rev-list", "--parents", "-n1", sha], cwd).stdout.split()) > 2
        for problem in rules.problems(message, merge):
            found.append(f"commit {sha[:7]}: {problem}")
    diff = git(["diff", f"{BASE}...HEAD"], cwd).stdout
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++") and EM_DASH in line:
            found.append(f"em-dash in an added line: {line[1:80].strip()!r}")
    return found


def check_body(text, branch):
    found = []
    prose = CODE_RE.sub("", text)
    if "\\n" in prose or "\\r" in prose:
        found.append("literal \\n in the PR body: write it to a file with real newlines")
    match = BRANCH_RE.match(branch or "")
    linked = {int(n) for n in LINK_RE.findall(prose)}
    if match and int(match.group(1)) not in linked:
        found.append(
            f"the body does not link #{match.group(1)}: start it with "
            f"`Closes #{match.group(1)}` (or `Refs` for a partial win)"
        )
    review = REVIEW_RE.search(text)
    if not review or not review.group(1).strip():
        found.append("no `## Review` section, or it is empty (AGENTS.md, review gate)")
    if EM_DASH in text:
        found.append("em-dash in the PR body")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--body", required=True, help="the PR body file")
    parser.add_argument("--test", action="store_true", help="also run the suite")
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args(argv)
    try:
        with open(args.body, encoding="utf-8") as handle:
            body = handle.read()
    except OSError as err:
        print(
            f"pre-pr-check: cannot read the PR body ({err}); pass PR_BODY=<file>", file=sys.stderr
        )
        return 4
    found, branch = check_tree(args.cwd)
    found += check_commits(args.cwd, commit_rules())
    found += check_body(body, branch)
    if args.test and not found:
        proc = subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "python",
                "-m",
                "unittest",
                "discover",
                "-t",
                ".",
                "-s",
                "tests",
            ],
            cwd=args.cwd,
        )
        if proc.returncode != 0:
            found.append("the test suite failed (make test)")
    for problem in found:
        print(f"pre-pr-check: {problem}", file=sys.stderr)
    if not found:
        print("pre-pr-check: ok")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
