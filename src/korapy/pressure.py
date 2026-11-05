"""Host pressure: load per CPU and available memory, with saturation backoff.

Two readings, each through an injected seam faked in tests: ``/proc/loadavg``
and ``/proc/meminfo`` (``MemAvailable``). A file that cannot be read parses
as unknown, and unknown never blocks (the ``disk.py`` precedent). A saturated
reading turns the budget verdict into a refusal even when shares are free;
backoff only delays new launches, never pauses or kills running agents.

Only the two threshold readings land here: per-slice use and the
pressure-stall files feed the doctor and the snapshot, which own them.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass

#: The files a reading comes from, one fact each, read through `_read_text`.
LOADAVG_PATH = "/proc/loadavg"
MEMINFO_PATH = "/proc/meminfo"

#: The saturation line when no `[resources.pressure]` table sets one.
DEFAULT_MAX_LOAD_PER_CPU = 2.0
DEFAULT_MIN_MEM_AVAILABLE_PCT = 10.0

#: The pressure files, read through these seams so tests inject fixtures.
_read_text: Callable[[str], str] = lambda path: open(path, encoding="utf-8").read()  # noqa: E731
_cpu_count: Callable[[], int | None] = os.cpu_count


@dataclass(frozen=True)
class Reading:
    """The measured host pressure. None reads as unknown, never blocking."""

    load_per_cpu: float | None = None
    mem_available_pct: float | None = None


def _parse_loadavg(text: str | None, ncpu) -> float | None:
    """The one-minute average divided by the CPU count, None when unknown."""
    if text is None:
        return None
    if isinstance(ncpu, bool) or not isinstance(ncpu, int) or ncpu <= 0:
        return None
    try:
        return float(text.split()[0]) / ncpu
    except (ValueError, IndexError):
        return None


def _parse_meminfo(text: str | None) -> float | None:
    """``MemAvailable`` as a percent of ``MemTotal``, None when unknown."""
    if text is None:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key.strip() not in ("MemTotal", "MemAvailable"):
            continue
        try:
            values[key.strip()] = int(rest.split()[0])
        except (ValueError, IndexError):
            return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total:
        return None
    if available is None or available < 0:
        return None
    return available / total * 100


def read(loadavg: str | None, meminfo: str | None, ncpu) -> Reading:
    """A Reading from fixture text: pure, never raising, unknown on garbage.

    `loadavg` and `meminfo` are the file contents, None when unreadable;
    `ncpu` is the CPU count, None when unknown. Every unparsable side
    reads as unknown instead of raising, so a stranger `/proc` format
    degrades to no backoff rather than a broken launch.
    """
    return Reading(
        load_per_cpu=_parse_loadavg(loadavg, ncpu),
        mem_available_pct=_parse_meminfo(meminfo),
    )


def production_read() -> Reading:
    """The live reading. Never raises: an unreadable host reads as unknown."""
    try:
        loadavg = _read_text(LOADAVG_PATH)
    except OSError:
        loadavg = None
    try:
        meminfo = _read_text(MEMINFO_PATH)
    except OSError:
        meminfo = None
    try:
        ncpu = _cpu_count()
    except Exception:  # noqa: BLE001 - any counter failure reads as unknown
        ncpu = None
    return read(loadavg, meminfo, ncpu)


def saturated(
    reading: Reading, *, max_load_per_cpu: float, min_mem_available_pct: float
) -> tuple[bool, str]:
    """Whether `reading` trips either threshold, with the tripped side named.

    Unknown sides never trip: a half-blind reading still decides on the
    half it can see. The detail names the side and both numbers, so the
    refusal reason carries them unchanged.
    """
    tripped = []
    if reading.load_per_cpu is not None and reading.load_per_cpu > max_load_per_cpu:
        tripped.append(f"load per cpu {reading.load_per_cpu:.1f} above max {max_load_per_cpu:.1f}")
    if reading.mem_available_pct is not None and reading.mem_available_pct < min_mem_available_pct:
        tripped.append(
            f"MemAvailable {reading.mem_available_pct:.1f}% below min {min_mem_available_pct:.1f}%"
        )
    if not tripped:
        return False, ""
    return True, "; ".join(tripped)
