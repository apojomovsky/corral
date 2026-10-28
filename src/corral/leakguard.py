"""The leak guard: private terms never cross from the ops side to the shared side.

The ops root's ``corral.toml`` lists private terms under ``[private]``: plain
strings in ``terms``, regular expressions in ``patterns``. Everything here is
a pure function over injected terms, so tests pass fakes and the CLI passes
the real host name, home directory and ops root.
"""

import os
import re
from typing import NamedTuple

from corral import settings

#: A term's source for a configured entry.
CONFIG_SOURCE = "corral.toml"

#: A built-in shorter than this is ignored: a one-letter host name must not
#: match everything.
MIN_BUILTIN_LEN = 4

#: The excerpt is the matching line, stripped and cut to this many characters.
EXCERPT_LEN = 80


class Term(NamedTuple):
    """One private rule: a plain ``term`` or a ``pattern`` regex.

    ``source`` says where it came from: ``corral.toml``, or one of the
    built-ins.
    """

    kind: str
    text: str
    source: str


class Match(NamedTuple):
    """A term hit: the term, the 1-based line and column, and the excerpt."""

    term: Term
    line: int
    column: int
    excerpt: str


class Finding(NamedTuple):
    """A match plus where it was found: ``commit <sha8>``, ``branch``,
    ``body`` or ``diff <path>:<line>``."""

    term: Term
    where: str
    line: int
    column: int
    excerpt: str


class LeakError(Exception):
    """``guard_text`` found private terms (exit 1). The tracker adapter
    refuses the write when this reaches it."""

    exit_code = 1

    def __init__(self, findings):
        super().__init__(f"{len(findings)} private term(s) found")
        self.findings = list(findings)


def terms(ops_root, *, host, home, ops_name):
    """The configured terms plus the built-ins corral always adds.

    The built-ins are the ops repository's own directory name, the absolute
    ops root path, the machine host name and the user's home directory path;
    they come in through arguments so tests inject fakes. With no ops root
    only the clone-independent built-ins (host and home) apply.
    """
    configured = settings.private(ops_root)
    found = [Term("term", text, CONFIG_SOURCE) for text in configured.terms]
    found.extend(Term("pattern", text, CONFIG_SOURCE) for text in configured.patterns)
    candidates = [
        (ops_name, "ops directory name"),
        (os.path.abspath(ops_root), "ops root path") if ops_root else None,
        (host, "host name"),
        (home, "home directory"),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        text, source = candidate
        if text and len(text) >= MIN_BUILTIN_LEN:
            found.append(Term("term", text, source))
    return found


def _excerpt(line):
    """The matching line, stripped and cut to EXCERPT_LEN characters."""
    return line.strip()[:EXCERPT_LEN]


def _compile(terms):
    """Each term as a ``(term, matcher)`` pair; the matcher maps a line to a
    ``(start, end)`` span or None. The one place that compiles patterns."""
    compiled = []
    for term in terms:
        if term.kind == "pattern":
            regex = re.compile(term.text, re.IGNORECASE)
            compiled.append((term, lambda line, regex=regex: _span(regex.search(line))))
        else:
            lowered = term.text.lower()
            compiled.append((term, lambda line, lowered=lowered: _substr(line, lowered)))
    return compiled


def _line_matches(line, compiled):
    """The first hit of each term on one line, or nothing."""
    out = []
    for term, matcher in compiled:
        hit = matcher(line)
        if hit is not None:
            start, _ = hit
            out.append((term, start))
    return out


def scan(text, terms):
    """The first hit of each term on each line of `text` (pure)."""
    compiled = _compile(terms)
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for term, start in _line_matches(line, compiled):
            found.append(Match(term, number, start + 1, _excerpt(line)))
    return found


def _span(match):
    return match.span() if match is not None else None


def _substr(line, lowered):
    start = line.lower().find(lowered)
    return (start, start + len(lowered)) if start >= 0 else None


def check_messages(commits, terms):
    """Scan each ``(sha, message)`` pair; `where` is ``commit <sha8>``."""
    found = []
    for sha, message in commits:
        for match in scan(message, terms):
            found.append(
                Finding(match.term, f"commit {sha[:8]}", match.line, match.column, match.excerpt)
            )
    return found


def check_branch(name, terms):
    """Scan a branch name; `where` is ``branch``."""
    return [
        Finding(match.term, "branch", match.line, match.column, match.excerpt)
        for match in scan(name, terms)
    ]


def check_body(text, terms):
    """Scan a PR body; `where` is ``body``."""
    return [
        Finding(match.term, "body", match.line, match.column, match.excerpt)
        for match in scan(text, terms)
    ]


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_path(marker):
    path = marker.strip().split("\t")[0]
    if path.startswith("b/"):
        return path[2:]
    return path


def check_diff(unified, terms):
    """Scan the added lines of a unified diff (pure).

    Only ``+`` lines inside a hunk count: context, removed lines and the
    ``---``/``+++`` headers never match. Headers are recognized by position
    (outside a hunk), never by prefix, so added content starting with ``++``
    still scans. `where` is ``diff <path>:<line>`` in new-file coordinates.
    """
    compiled = _compile(terms)
    found = []
    path = None
    lineno = 0
    in_hunk = False
    for raw in unified.splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
            path = None
        elif raw.startswith("@@ "):
            hit = _HUNK.match(raw)
            lineno = int(hit.group(1)) if hit else 0
            in_hunk = True
        elif not in_hunk:
            if raw.startswith("+++ "):
                path = _diff_path(raw[4:])
        elif raw.startswith("+"):
            content = raw[1:]
            if path is not None:
                for term, start in _line_matches(content, compiled):
                    found.append(
                        Finding(term, f"diff {path}:{lineno}", lineno, start + 1, _excerpt(content))
                    )
            lineno += 1
        elif raw.startswith(" "):
            lineno += 1
    return found


def guard_text(text, terms):
    """Scan `text`, raising LeakError (exit 1) on the first anything found.

    This is the function tracker writes will call before posting.
    """
    found = scan(text, terms)
    if found:
        raise LeakError(found)
    return found
