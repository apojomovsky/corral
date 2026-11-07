"""Project eligibility: which tickets each project wants started.

Each registered project names an entry point in its `projects.d` file::

    eligibility = "blue_policy:eligible"

On every dispatch pass korapy loads that entry point once per project
and calls it as ``eligible(rows, *, now)``. The function is pure by
contract: korapy supplies everything as arguments, including `now`, so
no tracker, forge, network, clock or settings access happens inside.
Policy code runs in-process as a korapy library, so only trust policy
modules the host operator trusts; a policy that needs the outside
world is a configuration error in that project, never a new seam.

The entry is either a dotted ``module:function`` name or a
``path/to/policy.py:function`` file path. Korapy's callers pass the
project's registered ops root as `base`: a relative file path
resolves against that root rather than the process working directory,
and a dotted module imports with that root first on the import search
path, so a policy file sitting beside `korapy.toml` needs no
installation and the same entry loads from a shell and under a unit.
The previous search path is restored before returning, also on
failure. When the same module name exists both in the ops root and
elsewhere, the ops root's copy wins; `LoadedPolicy.source` records
which file was loaded.
A project without the entry registers no candidates; loading failures
are configuration errors (exit 4) naming the project and the entry,
blocking only that project.
"""

import importlib
import importlib.util
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from korapy import accounts, board

#: The request kinds a policy may return (section 2 of `04-dispatch-loop.md`).
KINDS = ("start", "resume", "review", "fix")


class EligibilityError(ValueError):
    """A policy entry is unusable or a policy answer is invalid (exit 4)."""

    exit_code = 4


@dataclass(frozen=True)
class LinkedPull:
    """One pull request linked to a ticket, as plain data."""

    number: int
    state: str = ""
    checks: str = ""
    review: str = ""


@dataclass(frozen=True)
class TicketRow:
    """One ticket as the policy sees it: frozen plain data from the store.

    `extra` carries the board concepts (priority, area, pool, blocked-by
    refs and the like); it defaults to None and reads as {} so label-only
    rows are unchanged. `pulls` carries the ticket's linked pull requests
    from the tracker (None when that read failed, so readers fail closed);
    it defaults to () for rows built without a tracker read, such as plans.
    `pulls_error` carries the first error that read reported for the
    ticket, so skips can name the cause; it defaults to empty.
    """

    repo: str
    number: int
    title: str = ""
    labels: tuple = ()
    priority: str = ""
    state: str = "open"
    agent: str = ""
    status: str = ""
    external: bool = False
    pulls: tuple | None = ()
    pulls_error: str = ""
    extra: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        extra = self.extra
        if isinstance(extra, Mapping):
            object.__setattr__(self, "extra", dict(extra))
        else:
            object.__setattr__(self, "extra", {})


@dataclass(frozen=True)
class StartRequest:
    """One ticket the policy wants worked on, with its dispatch kind.

    `weight` orders within the project only: heaviest first, ties by
    `(repo, number)`. Later passes re-read live state before every
    start, so a request is a wish, never a reservation.
    """

    repo: str
    number: int
    kind: str
    weight: float = 0.0


def row_from_item(
    item: board.Item, *, pulls: tuple | None = (), pulls_error: str = ""
) -> TicketRow:
    """The policy view of one board store item. Pure.

    `pulls` is the ticket's linked pull requests from the tracker (None
    when that read failed, so readers fail closed); the store carries
    no pull rows, so callers pass what the pass read. `pulls_error` is
    the first error that read reported for the ticket, so skips can name
    the cause. Board concepts ride along in extra, copied on read so no
    two rows share one dict; a missing value reads as carrying nothing.
    """
    return TicketRow(
        repo=item.repo,
        number=item.number,
        title=item.title,
        labels=tuple(item.labels or ()),
        priority=board.effective_priority(item.priority, item.extra),
        state=item.state,
        agent=item.agent,
        status=item.status,
        external=item.external,
        pulls=pulls,
        pulls_error=pulls_error,
        extra=dict(item.extra) if isinstance(item.extra, dict) else {},
    )


def rows_from_items(
    items: Sequence[board.Item],
    *,
    pulls_by_key: Mapping[tuple[str, int], tuple | None] | None = None,
    pulls_errors: Mapping[tuple[str, int], str] | None = None,
) -> list[TicketRow]:
    """The policy view of every given item, in the given order. Pure.

    `pulls_by_key` maps (repo, number) to the linked pulls the pass
    read through the tracker (None marks an unreadable ticket); a
    ticket missing from the map reads as carrying no pulls. With no
    map every row reads empty, which is what plans build: they never
    contact the tracker. `pulls_errors` maps (repo, number) to the
    first error that read reported, so skips can name the cause; a
    ticket missing from it reads as reporting none.
    """
    rows = []
    for item in items:
        pulls: tuple | None = ()
        pulls_error = ""
        if pulls_by_key is not None:
            pulls = pulls_by_key.get((item.repo, item.number), ())
        if pulls_errors is not None:
            pulls_error = pulls_errors.get((item.repo, item.number), "")
        rows.append(row_from_item(item, pulls=pulls, pulls_error=pulls_error))
    return rows


def _import_dotted(head: str, *, entry: str, project: str, base: str | None = None) -> Any:
    """The module `head` names, with the ops root first when `base` is given.

    The import runs through the shared import-path helper accounts uses
    for readers, so prepending and restoring happen in exactly one
    place. Prepending (never appending) keeps an ops-root copy winning
    over an installed copy of the same name.
    """
    dirs = [base] if base else []
    with accounts._with_reader_path(dirs):
        try:
            return importlib.import_module(head)
        except Exception as err:
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} does not import: {err}"
            ) from None


def _resolve(
    entry: str, *, project: str, base: str | None = None
) -> tuple[Callable[..., Any], str | None]:
    """The callable an entry names plus the file its module loaded from.

    Dotted or file path; never called here. `source` is the module
    file (None when the module reports none), so a shadowed name shows
    which copy won.
    """
    head, sep, attrs = entry.partition(":")
    if not sep or not head or not attrs:
        raise EligibilityError(
            f"project {project}: eligibility {entry!r} must look like "
            "module:function or path/to/policy.py:function"
        )
    if "/" in head or os.sep in head or head.endswith(".py"):
        module, source = _load_file(head, entry=entry, project=project, base=base)
        target: Any = module
    else:
        module = _import_dotted(head, entry=entry, project=project, base=base)
        source = getattr(module, "__file__", None)
        target = module
    for attr in attrs.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError:
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} has no {attr!r}"
            ) from None
    if not callable(target):
        raise EligibilityError(f"project {project}: eligibility {entry!r} is not callable")
    return target, source


def _load_file(path: str, *, entry: str, project: str, base: str | None = None) -> tuple[Any, str]:
    """The module at `path` plus the absolute file it loaded from.

    A relative `path` resolves against `base`, the project's registered
    ops root; without a base it stays relative to the working directory.
    The module imports under a name derived from the path.
    """
    if base is not None and not os.path.isabs(path):
        path = os.path.join(base, path)
    if not os.path.isfile(path):
        raise EligibilityError(f"project {project}: eligibility {entry!r} has no file {path!r}")
    name = "korapy_policy_" + os.path.abspath(path).encode().hex()
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise EligibilityError(f"project {project}: eligibility {entry!r} cannot load {path!r}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception as err:
        raise EligibilityError(
            f"project {project}: eligibility {entry!r} does not import: {err}"
        ) from None
    return module, os.path.abspath(path)


@dataclass(frozen=True)
class LoadedPolicy:
    """One project's policy: the loaded entry plus who named it.

    `eligible(rows, *, now)` is the pure call: it runs the entry over
    the rows and returns the validated requests heaviest first. A bad
    answer is an EligibilityError naming the project and the entry.
    `source` is the file the entry's module loaded from (None when the
    module reports none), so a shadowed name shows which copy won.
    """

    project: str
    entry: str
    func: Callable[..., Any] = field(compare=False)
    source: str | None = None

    def eligible(self, rows: Sequence[TicketRow], *, now: float) -> list[StartRequest]:
        try:
            found = self.func(list(rows), now=now)
        except EligibilityError as err:
            raise EligibilityError(
                f"project {self.project}: eligibility {self.entry!r} failed: {err}"
            ) from None
        except Exception as err:
            raise EligibilityError(
                f"project {self.project}: eligibility {self.entry!r} failed: {err}"
            ) from None
        return _ordered(found, project=self.project, entry=self.entry)


def _ordered(found: Any, *, project: str, entry: str) -> list[StartRequest]:
    """The validated requests, heaviest first, ties by (repo, number). Pure."""
    if not isinstance(found, (list, tuple)):
        raise EligibilityError(
            f"project {project}: eligibility {entry!r} must return a list, "
            f"got {type(found).__name__}"
        )
    for request in found:
        if not isinstance(request, StartRequest):
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} must return "
                f"StartRequest rows, got {type(request).__name__}"
            )
        if not isinstance(request.repo, str):
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} carries a "
                f"non-string repo {request.repo!r}"
            )
        if not isinstance(request.number, int) or isinstance(request.number, bool):
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} carries a "
                f"non-integer number {request.number!r}"
            )
        if request.kind not in KINDS:
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} names unknown kind {request.kind!r}"
            )
        if (
            not isinstance(request.weight, (int, float))
            or isinstance(request.weight, bool)
            or not math.isfinite(request.weight)
        ):
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} carries a "
                f"non-finite weight {request.weight!r}"
            )
    return sorted(found, key=lambda request: (-request.weight, request.repo, request.number))


def load_policy(entry: str, *, project: str, base: str | None = None) -> LoadedPolicy:
    """The policy an entry names. Resolving imports the module but never
    calls the entry function; the module's own top-level code runs on
    import, which is the in-process trust boundary for policy code.

    `base` is the project's registered ops root: a relative file path
    resolves against it, and a dotted module imports with it first on
    the import search path (restored before returning, also on
    failure), so the ops root's copy wins over an installed one.
    `source` on the returned policy records which file the module
    loaded from. Raises EligibilityError (exit 4) naming the project
    and the entry.
    """
    func, source = _resolve(entry, project=project, base=base)
    return LoadedPolicy(project=project, entry=entry, func=func, source=source)


def check_entry(entry: str, *, project: str, base: str | None = None) -> str | None:
    """None when the entry resolves to a callable, else the error text.

    Resolving imports the module but never calls the entry function;
    the module's own top-level code runs on import. A resolver-side
    helper so health checks report a bad entry without raising.
    """
    try:
        load_policy(entry, project=project, base=base)
    except EligibilityError as err:
        return str(err)
    return None


class Outcome(NamedTuple):
    """One project's eligibility answer: requests, or a skip line.

    `skipped` is None on success. A project without the entry registers
    no candidates; the caller records `skipped` as the logbook line.
    """

    requests: list[StartRequest]
    skipped: str | None


def for_project(
    project: Any,
    items: Sequence[board.Item],
    *,
    now: float,
    pulls_by_key: Mapping[tuple[str, int], tuple | None] | None = None,
    pulls_errors: Mapping[tuple[str, int], str] | None = None,
) -> Outcome:
    """One project's ordered requests over these store items.

    `pulls_by_key` maps (repo, number) to the linked pulls the pass
    read through the tracker, so the policy and the gate read the
    same row pulls; with none every row reads empty. `pulls_errors`
    maps (repo, number) to the first error that read reported, so the
    rows carry the cause with them. Raises EligibilityError (exit 4,
    naming the project and the entry) when the entry does not load or
    the answer is invalid. Blocks only this project: the caller moves
    on to the next one.
    """
    if not project.eligibility:
        return Outcome([], f"project {project.name} has no eligibility entry: skipped")
    policy = load_policy(project.eligibility, project=project.name, base=project.root)
    return Outcome(
        policy.eligible(
            rows_from_items(items, pulls_by_key=pulls_by_key, pulls_errors=pulls_errors),
            now=now,
        ),
        None,
    )
