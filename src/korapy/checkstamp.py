"""Proof that the check matrix just passed, so takeoff can reuse it.

`make check` runs the suite once per supported interpreter; `make
pre-pr-check TEST=1` used to run it yet again. After a green matrix the
Makefile writes one stamp line under the cache dir: `<tree>
<pythons...> <epoch>`. The tree is `HEAD^{tree}` of the checkout, which
names every tracked file; the takeoff gate refuses a dirty tree before
the stamp is ever read, so the tree alone identifies the tested
content. The stamp proves only the identical tree, the identical
interpreter set, and an age within `STAMP_MAX_AGE_S`.

This is not the pre-push stamp (`src/korapy/push.py`, `.githooks/pre-push`):
that one proves the hook's own single-interpreter suite at push time, so it
is keyed on the hook digest (`<tree> <hook-digest>`), lives under the git
dir, and never expires. This one proves the multi-interpreter matrix at
check time, so it is keyed on the interpreter set, lives under the cache
dir, and expires after a day. The two gates differ in key, place and
lifetime on purpose; a pre-push-shaped line reads as corrupt here, never
as proof. `tests/test_checkstamp.py::TwoStamps` pins that difference.

Pure core, thin seams: `stamp_status` decides over plain data (the file
text, the tree, the interpreters, now); the file read, the git calls
and the clock stay at the edges, in the Makefile and the takeoff
script, so tests inject all three. Anything unusable reads as a miss,
never as a hit: a missing, corrupt, stale or foreign stamp just runs
the suite again.
"""

import os

from korapy import paths

STAMP_NAME = "korapy-check-stamp"

#: A proof older than this is spent: the tree is identical, but the host
#: may have moved on (a flaky test, a rebuilt interpreter). One day.
STAMP_MAX_AGE_S = 24 * 3600

#: The interpreters the matrix stamps: the same set `make check` runs.
#: Mirrors the Makefile's `PYTHONS` (pinned by an agreement test) for
#: readers, like the pre-push hook, that cannot query make at gate time.
MATRIX_PYTHONS = ("3.11", "3.14")


def stamp_path(env=None):
    """The stamp file: `STAMP_NAME` under the cache dir, resolved here only."""
    env = os.environ if env is None else env
    return os.path.join(paths.cache_dir(env), STAMP_NAME)


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def read_stamp(env=None, read=None):
    """The stamp file text, "" when missing or unreadable.

    A miss, not an error: the caller runs the suite again.
    """
    read = read or read_text
    try:
        return read(stamp_path(env)) or ""
    except OSError:
        return ""


def normalize_pythons(pythons):
    """The interpreter set as a sorted list; a string splits on whitespace."""
    if pythons is None:
        return []
    if isinstance(pythons, str):
        pythons = pythons.split()
    return sorted(pythons)


def format_stamp(tree, pythons, now):
    """The stamp line the matrix writes: `<tree> <pythons...> <epoch>`."""
    return " ".join([tree, *normalize_pythons(pythons), str(int(now))]) + "\n"


def parse_stamp(text):
    """`(tree, pythons, epoch)` for a well-formed stamp line, else None."""
    parts = (text or "").split()
    if len(parts) < 3:
        return None
    try:
        return parts[0], parts[1:-1], int(parts[-1])
    except ValueError:
        return None


def stamp_status(text, tree, pythons, now, max_age=STAMP_MAX_AGE_S):
    """Why the stamp does or does not prove this tree: one status word.

    `text` is the stamp file content ("" or None when missing), `tree`
    the current `HEAD^{tree}` ("" when unresolvable), `pythons` the
    expected interpreters (a string or a list, None when unknown), `now`
    epoch seconds. Returns "fresh" only for the identical tree, the
    identical interpreter set, and an age within `max_age`; every other
    outcome ("missing", "corrupt", "different-tree", "different-pythons",
    "stale") runs the suite again.
    """
    if not (text or "").strip():
        return "missing"
    parsed = parse_stamp(text)
    if parsed is None:
        return "corrupt"
    stamped_tree, stamped_pythons, epoch = parsed
    if not tree or stamped_tree != tree:
        return "different-tree"
    if normalize_pythons(pythons) != sorted(stamped_pythons):
        return "different-pythons"
    if not 0 <= now - epoch <= max_age:
        return "stale"
    return "fresh"
