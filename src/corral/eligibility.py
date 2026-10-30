"""Project eligibility: which tickets each project wants started.

Each registered project names an entry point in its `projects.d` file::

    eligibility = "blue_policy:eligible"

On every dispatch pass corral loads that entry point once per project
and calls it as ``eligible(rows, *, now)``. The function is pure by
contract: corral supplies everything as arguments, including `now`, so
no tracker, forge, network, clock or settings access happens inside.
Policy code runs in-process as a corral library, so only trust policy
modules the host operator trusts; a policy that needs the outside
world is a configuration error in that project, never a new seam.

The entry is either a dotted ``module:function`` name or a
``path/to/policy.py:function`` file path. A relative file path resolves
against the project's registered ops root, never the process working
directory, so the same entry loads from a shell and under a unit.
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

from corral import board

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
    rows are unchanged.
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
    pulls: tuple = ()
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


def row_from_item(item: board.Item) -> TicketRow:
    """The policy view of one board store item. Pure."""
    # pulls stays empty here; the reviewer-scheduling pass (ticket 6 of
    # docs/04-dispatch-loop.md) populates it from linked pull requests.
    # Board concepts ride along in extra, copied on read so no two rows
    # share one dict; a missing value reads as carrying nothing.
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
        pulls=(),
        extra=dict(item.extra) if isinstance(item.extra, dict) else {},
    )


def rows_from_items(items: Sequence[board.Item]) -> list[TicketRow]:
    """The policy view of every given item, in the given order. Pure."""
    return [row_from_item(item) for item in items]


def _resolve(entry: str, *, project: str, base: str | None = None) -> Callable[..., Any]:
    """The callable an entry names, dotted or file path. Never called here."""
    head, sep, attrs = entry.partition(":")
    if not sep or not head or not attrs:
        raise EligibilityError(
            f"project {project}: eligibility {entry!r} must look like "
            "module:function or path/to/policy.py:function"
        )
    if "/" in head or os.sep in head or head.endswith(".py"):
        target: Any = _load_file(head, entry=entry, project=project, base=base)
    else:
        try:
            target = importlib.import_module(head)
        except Exception as err:
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} does not import: {err}"
            ) from None
    for attr in attrs.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError:
            raise EligibilityError(
                f"project {project}: eligibility {entry!r} has no {attr!r}"
            ) from None
    if not callable(target):
        raise EligibilityError(f"project {project}: eligibility {entry!r} is not callable")
    return target


def _load_file(path: str, *, entry: str, project: str, base: str | None = None) -> Any:
    """The module at `path`, imported under a name derived from the path.

    A relative `path` resolves against `base`, the project's registered
    ops root; without a base it stays relative to the working directory.
    """
    if base is not None and not os.path.isabs(path):
        path = os.path.join(base, path)
    if not os.path.isfile(path):
        raise EligibilityError(f"project {project}: eligibility {entry!r} has no file {path!r}")
    name = "corral_policy_" + os.path.abspath(path).encode().hex()
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise EligibilityError(f"project {project}: eligibility {entry!r} cannot load {path!r}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except EligibilityError:
        raise
    except Exception as err:
        raise EligibilityError(
            f"project {project}: eligibility {entry!r} does not import: {err}"
        ) from None
    return module


@dataclass(frozen=True)
class LoadedPolicy:
    """One project's policy: the loaded entry plus who named it.

    `eligible(rows, *, now)` is the pure call: it runs the entry over
    the rows and returns the validated requests heaviest first. A bad
    answer is an EligibilityError naming the project and the entry.
    """

    project: str
    entry: str
    func: Callable[..., Any] = field(compare=False)

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

    `base` is the project's registered ops root, against which a
    relative file path resolves. Raises EligibilityError (exit 4)
    naming the project and the entry.
    """
    return LoadedPolicy(
        project=project, entry=entry, func=_resolve(entry, project=project, base=base)
    )


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


def for_project(project: Any, items: Sequence[board.Item], *, now: float) -> Outcome:
    """One project's ordered requests over these store items.

    Raises EligibilityError (exit 4, naming the project and the entry)
    when the entry does not load or the answer is invalid. Blocks only
    this project: the caller moves on to the next one.
    """
    if not project.eligibility:
        return Outcome([], f"project {project.name} has no eligibility entry: skipped")
    policy = load_policy(project.eligibility, project=project.name, base=project.root)
    return Outcome(policy.eligible(rows_from_items(items), now=now), None)
