"""Lint for every comment block added by a PR: mechanical rules only.

Every added comment block is extracted from the diff and classified as
a file header, doc comment, or explanatory block. The rules are all
stateless: line caps, decoration, narrative prose, and em-dashes. There
is no adjudication state to maintain, which is what lets the gate run at
pre-push and in CI.
"""

import hashlib
import re
import os
import subprocess
from typing import NamedTuple

from . import diffscope

HEADER = "header"
DOC = "doc"
EXPLANATORY = "explanatory"

# Leading comment punctuation. Order is load-bearing twice over: `///`
# must precede `//`, and both `/**` and `/*` must precede `//`, or `//`
# swallows the first two characters of `/**` and leaves a stray star.
_MARKER = re.compile(r"^(///|//!|/\*\*|/\*|//|\*/|\*|#)")


def normalize(lines):
    """Marker-free, indent-free, whitespace-collapsed text of a block."""
    out = []
    for raw in lines:
        text = raw.strip()
        text = _MARKER.sub("", text, count=1)
        text = text.replace("*/", "")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return "\n".join(out)


def block_id(kind, normalized):
    """Stable id for a block. Location is excluded on purpose."""
    payload = f"{kind}\n{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


_EXT_SYNTAX = {
    ".c": "c",
    ".h": "c",
    ".rs": "rust",
    ".py": "hash",
    ".sh": "hash",
    ".toml": "hash",
    ".yml": "hash",
    ".yaml": "hash",
    ".cmake": "hash",
    ".mk": "hash",
}
_NAME_SYNTAX = {"Makefile": "hash", "CMakeLists.txt": "hash"}

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def syntax_for(path):
    """Comment syntax for a path, or None when the file is not scanned."""
    name = os.path.basename(path)
    if name in _NAME_SYNTAX:
        return _NAME_SYNTAX[name]
    if name.startswith("Makefile.") or name.startswith("Dockerfile"):
        return "hash"
    return _EXT_SYNTAX.get(os.path.splitext(name)[1])


class _Scanner:
    """Stateful `is this line a comment` test for one file."""

    def __init__(self, syntax):
        self.syntax = syntax
        self.in_block = False

    def is_comment(self, trimmed):
        if self.syntax == "hash":
            return trimmed.startswith("#") and not trimmed.startswith("#!")
        if self.in_block:
            if "*/" in trimmed:
                self.in_block = False
            return True
        if trimmed.startswith("//"):
            return True
        if trimmed.startswith("/*"):
            if "*/" not in trimmed:
                self.in_block = True
            return True
        return False


def _block_open_before(file_lines, line_no):
    """True when new-file line `line_no` sits inside an open /* block.

    Scans the file rather than the diff, because a hunk that edits the
    middle of a block comment shows only ` * text`, which is otherwise
    indistinguishable from a dereference. String literals containing
    `/*` are not modelled; that is a known and accepted imprecision.
    """
    open_block = False
    for raw in file_lines[: max(line_no - 1, 0)]:
        text = raw
        while True:
            if open_block:
                index = text.find("*/")
                if index < 0:
                    break
                text = text[index + 2 :]
                open_block = False
            else:
                index = text.find("/*")
                if index < 0:
                    break
                text = text[index + 2 :]
                open_block = True
    return open_block


def parse_diff(diff_text, syntax, file_lines=None):
    """Contiguous runs of added comment lines, as (start_line, lines).

    `file_lines` is the new file's content on disk. It is optional so the
    parser stays testable on its own, but callers should pass it: without
    it, lines added inside an existing block comment are missed.
    """
    scanner = _Scanner(syntax)
    runs = []
    line_no = 0
    start = 0
    current = []

    def flush():
        nonlocal start, current
        if current:
            runs.append((start, current))
        start, current = 0, []

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            flush()
            match = _HUNK.match(line)
            if match:
                line_no = int(match.group(1))
                scanner.in_block = bool(
                    file_lines and syntax != "hash" and _block_open_before(file_lines, line_no)
                )
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            content = line[1:]
            if scanner.is_comment(content.strip()):
                if not current:
                    start = line_no
                current.append(content)
            else:
                flush()
            line_no += 1
            continue
        # Context and removed lines: removed lines do not exist in the
        # new file, so neither advances the new-file counter here
        # (-U0 emits no context lines).
        flush()
    flush()
    return runs


def header_span(file_lines, syntax):
    """1-based inclusive range of the file's leading comment block."""
    index = 0
    while index < len(file_lines):
        stripped = file_lines[index].strip()
        if stripped and not stripped.startswith("#!"):
            break
        index += 1
    if index >= len(file_lines):
        return None
    first = file_lines[index].strip()
    start = index + 1
    if syntax == "hash":
        if not first.startswith("#"):
            return None
        while index < len(file_lines) and file_lines[index].strip().startswith("#"):
            index += 1
        return (start, index)
    if first.startswith("/*"):
        while index < len(file_lines):
            if "*/" in file_lines[index]:
                return (start, index + 1)
            index += 1
        return (start, len(file_lines))
    if first.startswith("//"):
        while index < len(file_lines) and file_lines[index].strip().startswith("//"):
            index += 1
        return (start, index)
    return None


def classify(first_line, start, span, syntax):
    """HEADER, DOC or EXPLANATORY for a block, decided by position."""
    trimmed = first_line.strip()
    is_rust_doc = syntax == "rust" and (trimmed.startswith("///") or trimmed.startswith("//!"))
    if span and span[0] <= start <= span[1] and not is_rust_doc:
        return HEADER
    if is_rust_doc or trimmed.startswith("/**"):
        return DOC
    return EXPLANATORY


# The vendored pattern lives in diffscope so every first-party gate shares
# one rule.
_EXCLUDED = diffscope.EXCLUDED


class Block(NamedTuple):
    id: str
    kind: str
    path: str
    start: int
    end: int
    lines: list
    normalized: str
    header_lines: int


def _git(args, cwd=None):
    # Scrubbed: hook-inherited GIT_* vars would redirect
    # every call away from `cwd` to the outer repo.
    from .gitenv import clean_env

    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=60, env=clean_env()
    )


def _read_lines(path, cwd=None):
    """File content as lines, or None when it is unreadable."""
    full = os.path.join(cwd, path) if cwd else path
    try:
        with open(full, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return None


def _lint_context(path):
    return (syntax_for(path), diffscope.is_excluded(path))


def changed(base, cwd=None):
    """Files with new content in base...HEAD; a pure move has none."""
    return [
        c
        for c in diffscope.content_changes(base, _git, cwd=cwd, context=_lint_context)
        if not diffscope.is_excluded(c.path)
    ]


def changed_files(base, cwd=None):
    return [c.path for c in changed(base, cwd=cwd)]


def extract(base, cwd=None):
    """Every comment block added by base...HEAD, classified and hashed."""
    blocks = []
    for change in changed(base, cwd=cwd):
        path = change.path
        syntax = syntax_for(path)
        if syntax is None:
            continue
        file_lines = _read_lines(path, cwd=cwd)
        if file_lines is None:
            continue
        span = header_span(file_lines, syntax)
        if change.old_path:
            # Blob to blob: the old path may still exist in HEAD (a move
            # that left a compatibility symlink never pairs as a rename,
            # and a range diff then charges the whole new file). The base
            # blob is the copy source, so only truly new lines surface.
            diff = _git(["diff", "-U0", f"{base}:{change.old_path}", f"HEAD:{path}"], cwd=cwd)
            if diff.returncode != 0:
                # The base blob is gone (deleted and re-added mid-range):
                # fall back to a paired range diff.
                diff = _git(
                    ["diff", "-M", "-U0", f"{base}...HEAD", "--", change.old_path, path], cwd=cwd
                ).stdout
            else:
                diff = diff.stdout
        else:
            diff = _git(["diff", "-M", "-U0", f"{base}...HEAD", "--", path], cwd=cwd).stdout
        for start, lines in parse_diff(diff, syntax, file_lines):
            kind = classify(lines[0], start, span, syntax)
            normalized = normalize(lines)
            if not normalized:
                continue
            blocks.append(
                Block(
                    id=block_id(kind, normalized),
                    kind=kind,
                    path=path,
                    start=start,
                    end=start + len(lines) - 1,
                    lines=lines,
                    normalized=normalized,
                    header_lines=(span[1] - span[0] + 1) if (kind == HEADER and span) else 0,
                )
            )
    return blocks


HARD_CAP = 8

_TEST_PATH = re.compile(r"(^|/)(tests?|examples?)/")


class Violation(NamedTuple):
    path: str
    start: int
    end: int
    block_id: str
    message: str


def is_test_path(path):
    return bool(_TEST_PATH.search(path))


def _violation(block, message):
    return Violation(block.path, block.start, block.end, block.id, message)


def check_line_rules(block):
    """The length rule for a block's class, or None when it passes."""
    if block.kind == HEADER:
        # Charged against the whole on-disk header: otherwise appending one
        # line to a 25-line header escapes the cap permanently.
        if not is_test_path(block.path) and block.header_lines > HARD_CAP:
            return _violation(
                block, (f"header is {block.header_lines} lines, rephrase to fit {HARD_CAP}")
            )
        return None
    if block.kind == DOC:
        return None
    if len(block.lines) > HARD_CAP:
        return _violation(block, (f"over hard cap ({len(block.lines)} lines), cut or split it"))
    return None


EM_DASH = "\u2014"

# Narrow on purpose. A bare `fixed` would flag every fixed-point comment
# in numeric code, so each pattern needs the narrative shape around it.
_NARRATIVE = [
    re.compile(r"\bfixed\s+(it|this|the\s+\w+)\s+by\b", re.I),
    re.compile(r"\bverified\s+(by|with|via)\b", re.I),
    re.compile(r"\bas\s+requested\b", re.I),
    re.compile(r"\bnow\s+(works|passes|correct|correctly)\b", re.I),
    re.compile(r"\bused\s+to\s+(be|do|have)\b", re.I),
    re.compile(r"\bpreviously\s+(this|it|we)\b", re.I),
]
_DECORATION = re.compile(r"@file\b|@brief\s*$", re.I)
_ASCII_ART = re.compile(r"[─-╿]")


def check_content(block):
    """Class-independent content failures. Empty list when clean."""
    found = []
    if _DECORATION.search(block.normalized):
        found.append(
            _violation(block, ("decoration: @file or bare @brief repeats the filename, delete it"))
        )
    for pattern in _NARRATIVE:
        if pattern.search(block.normalized):
            found.append(
                _violation(
                    block,
                    ("narrative: verification and iteration prose belongs in the PR, not the tree"),
                )
            )
            break
    for line in block.normalized.splitlines():
        if EM_DASH in line and not _ASCII_ART.search(line):
            found.append(
                _violation(block, ("em-dash: use a comma, a colon, or a period and a new sentence"))
            )
            break
    return found


def verify(base, cwd=None):
    """Every lint violation in base...HEAD. Empty list means clean."""
    found = []
    for block in extract(base, cwd=cwd):
        found.extend(check_content(block))
        rule = check_line_rules(block)
        if rule:
            found.append(rule)
    return found


def render_blocks(blocks):
    """Human-readable listing of every added block in the range."""
    if not blocks:
        return "no comment blocks added in this range"
    lines = []
    for block in blocks:
        lines.append(
            f"  {block.path}:{block.start}-{block.end} "
            f"[{block.kind} {len(block.lines)}L] {block.id}"
        )
        for raw in block.lines:
            lines.append(f"      {raw}")
        lines.append("")
    return "\n".join(lines)


def render_violations(found):
    if not found:
        return "prose lint clean"
    lines = ["== prose lint =="]
    for item in found:
        lines.append(
            f"  [FAIL] {item.path}:{item.start}-{item.end} {item.block_id}: {item.message}"
        )
    lines.append("")
    lines.append(f"== {len(found)} blocking; fix each and re-run ==")
    return "\n".join(lines)
