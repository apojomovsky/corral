"""The only module that touches a subprocess. Everything else is pure.

`run` tracks its in-flight `gh` child so a SIGTERM landing mid-pass can
kill it instead of waiting it out: a daemon loop flags `request_stop`
from its signal handler and every later `run`
self-terminates until the process exits.
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import threading
import time

#: Marker the pacing shim (the `gh` on agents' PATH) honors: `run` already
#: charges the shared bucket (and sets the shared cooldown on errors)
#: through `graphql`/`paced_run`, so a `gh` it spawns inside an agent
#: unit must not be charged or cooled down a second time by the shim.
#: The shim still logs the call.
PACED_MARKER = "CORRAL_GH_PACED"

_active_lock = threading.Lock()
_active_procs = set()
_stop = threading.Event()
#: communicate() quantum: only caps the wait, a finishing child returns
#: at once. _KILL_AFTER_S bounds the SIGTERM grace before SIGKILL.
_POLL_S = 0.5
_KILL_AFTER_S = 5.0
GH_TIMEOUT_S = 30
_SCOPE_PHRASES = ("missing required scopes",)
#: Single-token markers that also occur inside repo names, URLs, and issue
#: refs (`scope-tool`, `#502`). They match only standing alone: the
#: guards are the characters that can glue a marker into a name, path, or
#: ref (word chars plus `-` for repo names, `/` for owner/repo and URLs,
#: `#` for issue refs). Multi-word markers carry their own spaces, which
#: never appear in a repo name, so they stay plain substrings.
_NAME_GLUE = "A-Za-z0-9_/#-"
_SCOPE_TOKENS = (r"scopes?", "insufficient")
_UNAVAILABLE_PHRASES = ("connection refused", "dial tcp", "no such host", "temporarily unavailable")
_UNAVAILABLE_TOKENS = ("timeout", "502", "503", "504")
#: GitHub's rate-limit wording (REST, GraphQL, secondary limits). A rate-limited
#: call is retryable once the budget resets, never a usage error, so it is
#: "unavailable" (API, exit 3) rather than "other" (config, exit 4).
_RATE_LIMIT_PHRASES = ("rate limit", "abuse detection")
#: `ratelimit` is the fused spelling; `rate_limited` is the GraphQL error
#: `type`, which carries underscores where a REST answer has spaces.
_RATE_LIMIT_TOKENS = ("ratelimit", "rate_limited")


def _anchored(token: str) -> "re.Pattern[str]":
    """A token matcher that ignores the token inside names/paths/refs."""
    return re.compile(r"(?<![%s])%s(?![%s])" % (_NAME_GLUE, token, _NAME_GLUE), re.IGNORECASE)


_SCOPE_RES = tuple(_anchored(token) for token in _SCOPE_TOKENS)
_UNAVAILABLE_RES = tuple(_anchored(token) for token in _UNAVAILABLE_TOKENS)
_RATE_LIMIT_RES = tuple(_anchored(token) for token in _RATE_LIMIT_TOKENS)
#: Bucket markers for rate_limit_kind: a secondary answer carries no
#: usable reset, and a GraphQL answer names its keyword or its error `type`.
#: Single tokens, so they use the same anchored matchers as above.
_SECONDARY_RES = (_anchored("secondary"), _anchored("abuse"))
_GRAPHQL_RES = (_anchored("graphql"), _anchored("rate_limited"))


class GhError(Exception):
    """A `gh` invocation failed. `kind` classifies it for the caller's exit code."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def is_rate_limited(text: str | None) -> bool:
    low = (text or "").lower()
    if any(m in low for m in _RATE_LIMIT_PHRASES):
        return True
    return any(rx.search(text or "") is not None for rx in _RATE_LIMIT_RES)


def rate_limit_kind(text: str | None) -> str | None:
    """Which bucket tripped: "secondary", "graphql", "rest", or None.

    None when the text names no rate limit. Secondary first, since an abuse
    answer can also carry primary wording. GraphQL next, on its keyword or
    its error `type`; anything else rate-limited is a REST primary answer.
    """
    if not is_rate_limited(text):
        return None
    if any(rx.search(text or "") is not None for rx in _SECONDARY_RES):
        return "secondary"
    if any(rx.search(text or "") is not None for rx in _GRAPHQL_RES):
        return "graphql"
    return "rest"


def _classify(stderr: str) -> str:
    if is_rate_limited(stderr):
        return "unavailable"
    low = stderr.lower()
    if any(m in low for m in _SCOPE_PHRASES):
        return "scope"
    if any(rx.search(stderr) is not None for rx in _SCOPE_RES):
        return "scope"
    if any(m in low for m in _UNAVAILABLE_PHRASES):
        return "unavailable"
    if any(rx.search(stderr) is not None for rx in _UNAVAILABLE_RES):
        return "unavailable"
    return "other"


def _signal_group(proc, sig):
    """Signal the child's process group, else the child itself.

    `run` starts each child as a session leader, so grandchildren die
    with it and no pipe-holder outlives the stop to wedge communicate().
    Never raises.
    """
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            os.killpg(pid, sig)
            return
        except (OSError, TypeError, ValueError):
            pass
    try:
        proc.send_signal(sig)
    except (OSError, TypeError, ValueError, AttributeError):
        pass


def request_stop():
    """Flag shutdown and SIGTERM every in-flight `gh` child.

    The flag is sticky for the process lifetime: the daemon exits right
    after, so no later `run` should block again. Returns the number of
    children signaled. Never raises. The lock is taken without blocking
    so a signal handler calling this while the guarded section runs
    cannot deadlock; a child registered concurrently self-terminates on
    the flag right after registering (see `run`).
    """
    _stop.set()
    if _active_lock.acquire(blocking=False):
        try:
            procs = list(_active_procs)
        finally:
            _active_lock.release()
    else:
        return 0
    count = 0
    for proc in procs:
        _signal_group(proc, signal.SIGTERM)
        count += 1
    return count


def stop_requested():
    """Whether `request_stop` flagged a shutdown."""
    return _stop.is_set()


def clear_stop():
    """Drop the shutdown flag and forget tracked children (tests only)."""
    _stop.clear()
    with _active_lock:
        _active_procs.clear()


def run(args: list[str], timeout: float | None = GH_TIMEOUT_S, paced: bool = False) -> str:
    """Run `gh` with `args`. Raises GhError on a non-zero exit or a stall.

    `paced` marks the child for the agent-unit shim (the `gh` on agents' PATH):
    only `graphql` and `paced_run` set it, since they already charged the
    shared bucket (and cool down on errors) upstream. Every other caller
    leaves it unset so the shim paces the call as usual.
    """
    # Session leader: stop signals reach the whole group, and an
    # interactive Ctrl-C no longer lands on `gh` directly (it exits on
    # the closing pipe instead).
    env = dict(os.environ, **{PACED_MARKER: "1"}) if paced else None
    proc = subprocess.Popen(
        ["gh", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    with _active_lock:
        _active_procs.add(proc)
    try:
        if _stop.is_set():
            # A stop flagged between spawn and register (or before the
            # spawn): do not block on a child nobody will kill.
            _signal_group(proc, signal.SIGTERM)
        stdout, stderr = _communicate_stop_aware(proc, timeout)
    finally:
        with _active_lock:
            _active_procs.discard(proc)
    if proc.returncode != 0:
        err = (stderr or "").strip()
        raise GhError(_classify(err), err or f"gh exited {proc.returncode}")
    return stdout


def _communicate_stop_aware(proc, timeout):
    """communicate(), escalating to SIGKILL when stopped.

    The poll quantum only caps the wait; a normally finishing child
    returns at once. After `request_stop` the child gets SIGTERM (sent
    by the flagger) plus SIGKILL here when it still lingers past
    _KILL_AFTER_S: a SIGTERM-ignoring child must not hold the stop past
    a few seconds. Past the overall `timeout` (None waits unbounded,
    like `run watch` needs) the group is killed and the stall
    raises GhError, matching the old `subprocess.run(timeout=...)`.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        quantum = _POLL_S
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _signal_group(proc, signal.SIGKILL)
                raise GhError("unavailable", f"gh timed out after {timeout}s")
            quantum = min(quantum, remaining)
        try:
            return proc.communicate(timeout=quantum)
        except subprocess.TimeoutExpired:
            if not _stop.is_set():
                continue
            _signal_group(proc, signal.SIGTERM)
            try:
                return proc.communicate(timeout=_KILL_AFTER_S)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                return proc.communicate()


#: GraphQL pacing. GitHub's secondary limit is 2000
#: secondary points per minute per account for the GraphQL endpoint (900
#: for REST, 100 concurrent shared by both, plus CPU and content caps);
#: the primary budget is 5000 points per hour. Both are shared by every
#: process on the machine, so the pacer is one token bucket in a file.
#: Secondary points differ from primary points: a GraphQL read costs 1
#: and a mutation 5, whatever the primary cost (URL below).
#: https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api
GRAPHQL_CALL_COST = 3.0
#: Secondary cost of a GraphQL mutation (primary cost is ~1, so this is
#: conservative on both scales).
GRAPHQL_MUTATION_COST = 5.0
#: Sustained use caps at 3600 points per hour (72% of the primary
#: budget), leaving the rest for traffic outside the bucket. Capacity
#: 120 covers the largest measured single-pass burst (a cold pass at 74
#: points plus a full archived-inclusive read at 36) with headroom,
#: while bounding the machine-wide burst to a small fraction of the
#: secondary cap even when every call is a mutation.
GRAPHQL_CAPACITY = 120.0
GRAPHQL_REFILL_PER_MIN = 60.0
GRAPHQL_MAX_WAIT_S = 120.0
GRAPHQL_COOLDOWN_S = 60.0
GRAPHQL_MAX_COOLDOWN_S = 3600.0
#: Secondary costs of REST calls (1 for GET/HEAD, 5 for mutative verbs).
REST_READ_COST = 1.0
REST_WRITE_COST = 5.0
#: A paced wait naps in slices this long, so a stop request ends it within
#: one slice; systemd's stop timeout is 90 s.
_PACING_NAP_S = 5.0
#: `gh` prints no Retry-After header, so this parses the seconds out of
#: the message text ("retry after 62", "please wait 62 seconds"). Live
#: secondary-limit answers often name no seconds ("wait a few minutes"),
#: and then the default cooldown below applies.
_RETRY_AFTER = re.compile(r"(?:retry[- ]after|\bwait\b)\D{0,4}(\d+)", re.IGNORECASE)


def retry_after_s(text: str) -> float:
    """Seconds a rate-limit answer asks for; a default when it names none."""
    match = _RETRY_AFTER.search(text or "")
    return float(match.group(1)) if match else GRAPHQL_COOLDOWN_S


class GraphqlPacer:
    """A token bucket over GraphQL points, shared machine-wide.

    State is one JSON file read and written under `flock`, so every process
    on the machine charges the same bucket. A call takes points before it
    runs (GRAPHQL_CALL_COST, more for a mutation) and sleeps while the
    bucket is short; it gives up after `max_wait_s` with
    GhError("unavailable") (exit 3, retryable). A rate-limit answer sets a
    cooldown every process honors, because the limit is per account and one
    process's backoff must hold for all of them. A hit that lands while a
    cooldown is still active doubles it (exponential backoff), so a
    caller that keeps retrying into a ban spaces itself out instead of
    prolonging it; the asked-for `Retry-After` seconds are always honored
    as a floor. `now` and `sleep` are injectable for tests.
    """

    def __init__(
        self,
        path: str,
        capacity: float = GRAPHQL_CAPACITY,
        refill_per_min: float = GRAPHQL_REFILL_PER_MIN,
        max_wait_s: float = GRAPHQL_MAX_WAIT_S,
        now=time.time,
        sleep=time.sleep,
    ):
        self.path = path
        self.capacity = capacity
        self.refill_per_min = refill_per_min
        self.max_wait_s = max_wait_s
        self.now = now
        self.sleep = sleep

    def acquire(self, cost: float = GRAPHQL_CALL_COST) -> None:
        waited = 0.0
        while True:
            wait = self._update(lambda state, now: self._take(state, now, cost))
            if wait <= 0.0:
                return
            if stop_requested():
                raise GhError(
                    "unavailable",
                    f"GraphQL pacing: stop requested while waiting for "
                    f"{cost:g} points; retry later",
                )
            if waited + wait > self.max_wait_s:
                raise GhError(
                    "unavailable",
                    f"GraphQL pacing: {cost:g} points unavailable for "
                    f"{waited + wait:.0f}s; retry later",
                )
            nap = min(wait, _PACING_NAP_S)
            self.sleep(nap)
            waited += nap

    def charge(self, cost: float) -> None:
        """Deduct `cost` without waiting, for a call that goes through anyway.

        A call whose wait exceeded the cap still hit the API, so it must still
        draw on the bucket. The balance may go negative, down to -capacity, so
        the debt stays bounded and later callers wait it off.
        """

        def take(state, now):
            state["tokens"] = max(-self.capacity, state["tokens"] - cost)
            return 0.0

        self._update(take)

    def cooldown(self, seconds: float) -> None:
        seconds = min(seconds, GRAPHQL_MAX_COOLDOWN_S)

        def change(state, now):
            if now < state["cooldown_until"]:
                # Still backed off: double the last window, keeping what
                # the answer asked for as the floor.
                previous = state.get("backoff_s", seconds) or seconds
                seconds_eff = max(seconds, min(previous * 2, GRAPHQL_MAX_COOLDOWN_S))
            else:
                seconds_eff = seconds
            state["backoff_s"] = seconds_eff
            state["cooldown_until"] = max(state["cooldown_until"], now + seconds_eff)
            return 0.0

        self._update(change)

    def cooldown_remaining(self) -> float:
        """Seconds of shared cooldown left, 0 when calls may proceed.

        Read-only: it never charges the bucket, so a caller (a snapshot)
        can fail fast without spending an API call into a ban.
        """

        def peek(state, now):
            return max(0.0, state["cooldown_until"] - now)

        return self._update(peek)

    def _take(self, state, now, cost):
        if state["cooldown_until"] > now:
            return state["cooldown_until"] - now
        if state["tokens"] >= cost:
            state["tokens"] -= cost
            return 0.0
        return (cost - state["tokens"]) * 60.0 / self.refill_per_min

    def _update(self, change) -> float:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    now = self.now()
                    state = self._read(now)
                    elapsed = max(0.0, now - state["updated"])
                    state["tokens"] = min(
                        self.capacity, state["tokens"] + elapsed * self.refill_per_min / 60.0
                    )
                    state["updated"] = now
                    wait = change(state, now)
                    self._write(state)
                    return wait
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError as err:
            raise GhError("unavailable", f"GraphQL pacing state {self.path}: {err}") from err

    def _read(self, now: float) -> dict:
        try:
            with open(self.path) as handle:
                raw = json.load(handle)
            return {
                "tokens": float(raw["tokens"]),
                "updated": float(raw["updated"]),
                "cooldown_until": float(raw["cooldown_until"]),
                "backoff_s": float(raw.get("backoff_s", 0.0)),
            }
        except FileNotFoundError:
            return {
                "tokens": self.capacity,
                "updated": now,
                "cooldown_until": 0.0,
                "backoff_s": 0.0,
            }
        except (OSError, ValueError, KeyError, TypeError):
            # Unreadable state may hide a cooldown another process just wrote,
            # so back off once instead of resuming at a full burst.
            return {
                "tokens": self.capacity,
                "updated": now,
                "cooldown_until": now + GRAPHQL_COOLDOWN_S,
                "backoff_s": GRAPHQL_COOLDOWN_S,
            }

    def _write(self, state: dict) -> None:
        from corral.paths import atomic_write_text

        atomic_write_text(self.path, json.dumps(state))


def _pacer() -> GraphqlPacer:
    """The machine-wide pacer, in corral's state dir."""
    from corral.paths import state_dir

    return GraphqlPacer(os.path.join(state_dir(), "graphql-pacing.json"))


def graphql(query: str, run=None, pacer=None, **variables) -> dict:
    """Run a GraphQL query and return its `data` payload, paced by the
    machine-wide bucket. A mutation costs GRAPHQL_MUTATION_COST, a
    read GRAPHQL_CALL_COST; a rate-limit answer starts a shared cooldown.

    `run` and `pacer` are injected for tests and default to the real
    ones: `run(args, paced=True)` runs `gh`, and the pacer meters the
    shared bucket. Only GraphQL variable names may follow them."""
    active_pacer = _pacer() if pacer is None else pacer
    # The parameter shadows the module's `run`: fall back to it by name,
    # so tests patching the module attribute still steer the default.
    active_run = globals()["run"] if run is None else run
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # gh needs -F for typed (int, bool) values and -f for strings.
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        value_str = json.dumps(value) if isinstance(value, bool) else value
        args += [flag, f"{key}={value_str}"]
    cost = GRAPHQL_MUTATION_COST if re.search(r"\bmutation\b", query) else GRAPHQL_CALL_COST
    active_pacer.acquire(cost=cost)
    try:
        payload = json.loads(active_run(args, paced=True))
    except GhError as err:
        if is_rate_limited(str(err)):
            active_pacer.cooldown(retry_after_s(str(err)))
        raise
    if payload.get("errors"):
        errors = payload["errors"]
        text = json.dumps(errors)
        # Structured first: a RATE_LIMITED entry is rate limited even
        # when its message names a repo carrying a marker word.
        types = {entry.get("type") for entry in errors if isinstance(entry, dict)}
        if "RATE_LIMITED" in types or is_rate_limited(text):
            active_pacer.cooldown(retry_after_s(text))
            raise GhError("unavailable", text)
        raise GhError("other", text)
    return payload["data"]


def paced_run(
    args: list[str],
    cost: float = REST_READ_COST,
    timeout: float | None = GH_TIMEOUT_S,
    run=None,
    pacer=None,
) -> str:
    """Run a REST `gh` call through the shared bucket.

    `run` charges nothing, so unpaced REST bursts (launch title reads, list
    probes) all draw on the same per-account secondary budget the bucket
    meters. Pass REST_WRITE_COST for mutative calls. A rate-limit answer
    starts the shared cooldown, like `graphql` does.

    `run` and `pacer` are injected for tests and default to the real
    ones, the same way the REST adapter takes `run`.
    """
    active_pacer = _pacer() if pacer is None else pacer
    # The parameter shadows the module's `run`: fall back to it by name,
    # so tests patching the module attribute still steer the default.
    active_run = globals()["run"] if run is None else run
    active_pacer.acquire(cost=cost)
    try:
        return active_run(args, timeout=timeout, paced=True)
    except GhError as err:
        if is_rate_limited(str(err)):
            active_pacer.cooldown(retry_after_s(str(err)))
        raise


def pacer_cooldown_remaining() -> float:
    """Seconds of shared GraphQL cooldown left, 0 when calls may proceed.

    Never raises: only a lock/makedirs failure reads as no cooldown, so a
    fail-fast check cannot break callers that would otherwise proceed. A
    corrupt state file fails closed through the pacer's one-cooldown
    fallback, like any other paced call.
    """
    try:
        return _pacer().cooldown_remaining()
    except GhError:
        return 0.0


def default_branch(owner: str, repo: str, gh_run=None, cache=None) -> str:
    """Default branch of ``owner/repo`` from the REST repo object.

    The REST field is ``default_branch`` (snake case). ``""`` when the
    answer is empty; a failed read raises, and the caller picks the
    fallback. ``cache`` is a per-pass dict keyed by ``owner/repo``:
    a hit costs no call, and only non-empty answers are stored.
    """
    key = f"{owner}/{repo}"
    if cache is not None and cache.get(key):
        return cache[key]
    out = (gh_run or run)(["api", f"repos/{key}", "--jq", ".default_branch"])
    branch = (out or "").strip()
    if branch and cache is not None:
        cache[key] = branch
    return branch


def normalize_pr_body(body: str) -> str:
    """Replace literal ``\\n`` escapes with real newlines.

    oh-my-pi serializes PR bodies with escaped newlines that GitHub then
    renders literally (``\\n`` visible in the body). This heals a body that
    already contains those escapes while leaving correctly formed bodies
    untouched. Handles ``\\r\\n`` first so a Windows-escaped sequence does
    not become ``\\r`` plus newline.
    """
    if not body or "\\n" not in body and "\\r" not in body:
        return body
    # \\r\\n -> newline first, then remaining \\n / \\r
    return body.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


def contains_literal_escapes(body: str) -> bool:
    """Whether body contains literal ``\\n`` / ``\\r`` escapes instead of newlines.

    Code fences (```...```) and inline code (`...`) are stripped before
    checking so documenting the bug (e.g. ``--body "a\\nb"``) does not
    trip the gate. Mirrors the CI workflow stripping in
    .github/workflows/pr-body.yml.
    """
    if not body:
        return False
    import re as _re

    stripped = _re.sub(r"```[\s\S]*?```", "", body)
    stripped = _re.sub(r"`[^`]*`", "", stripped)
    return "\\n" in stripped or "\\r" in stripped


def _split_outside_fences(body: str) -> list[tuple[str, bool]]:
    """Split body into (line, outside_fence) pairs, tracking fenced blocks.

    A fence opens on a line whose leading run is three or more backticks or
    tildes. It closes only on a bare run of the same character at least as
    long as the opener, so a ``~~~`` block is not ended by a ```` ``` ````
    line and vice versa. Fence lines are returned with outside=False.
    """
    import re as _re

    rows = []
    fence: tuple[str, int] | None = None
    for line in body.split("\n"):
        stripped = line.strip()
        if fence is None:
            m = _re.match(r"(`{3,}|~{3,})", line.lstrip())
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                rows.append((line, False))
                continue
        else:
            char, length = fence
            m = _re.fullmatch(r"(`+|~+)", stripped)
            if m and m.group(1)[0] == char and len(m.group(1)) >= length:
                fence = None
            rows.append((line, False))
            continue
        rows.append((line, True))
    return rows


def extract_review_section(body: str) -> str | None:
    """Return the text under the `## Review` heading, or None when absent.

    The heading match is case-insensitive and accepts levels 1-6, so
    `## Review` and `### review` both count. Headings inside fenced code
    blocks are ignored, so quoting the template does not fake a record.
    The section runs to the next heading outside a fence, or end of body.
    It is the review-gate record a merge gate requires; takeoff and any
    merge gate share this parser so both agree on what counts.
    """
    import re as _re

    if not body:
        return None
    rows = _split_outside_fences(body)
    start = None
    for i, (line, outside) in enumerate(rows):
        if outside and _re.match(r"#{1,6}\s+review\s*$", line, _re.IGNORECASE):
            start = i
            break
    if start is None:
        return None
    collected = []
    for line, outside in rows[start + 1 :]:
        if outside and _re.match(r"#{1,6}\s+\S", line):
            break
        collected.append(line)
    return "\n".join(collected)


def has_review_record(body: str) -> bool:
    """Whether body carries a non-empty review-gate record.

    Present-but-blank (whitespace only) does not count: `"No findings"`
    is the valid empty record. Shared by takeoff's advisory pr-body check
    and any merge gate.
    """
    section = extract_review_section(body)
    return section is not None and section.strip() != ""


def has_readme_na_note(body: str) -> bool:
    """Whether body excuses a missing README update with a ``README: n/a`` note.

    The takeoff ``check_readme`` advisory warns when the diff touches
    user-visible surface without touching ``README.md``; a ``README: n/a``
    line (case-insensitive, ``na`` without the slash accepted) records the
    developing agent's judgment that no README change is needed, the same
    way ``## Review`` records the review. Notes inside fenced code blocks
    do not count, so quoting the convention does not excuse the diff.
    """
    import re as _re

    if not body:
        return False
    text = "\n".join(line for line, outside in _split_outside_fences(body) if outside)
    return _re.search(r"readme\s*:\s*n/?a\b", text, _re.IGNORECASE) is not None


def closing_issue_numbers(pr: dict) -> list[int]:
    """Issue numbers from ``closingIssuesReferences``.

    This is what GitHub populates from close keywords (``Closes #N``),
    and what a coordinator reads to link tickets, so
    takeoff warns off this field rather than regexing the body: a bare
    ``#N`` mention or a ``(#N)`` title suffix does not close anything.
    Tolerates the field missing or null (no references yet) and skips
    entries without an integer number.
    """
    refs = (pr or {}).get("closingIssuesReferences")
    if refs is None:
        refs = (pr or {}).get("closingIssuesRefs") or []
    numbers: list[int] = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        number = ref.get("number")
        if isinstance(number, bool):
            continue
        if isinstance(number, int):
            pass
        elif isinstance(number, str) and number.isdigit():
            number = int(number)
        else:
            continue
        if number not in numbers:
            numbers.append(number)
    return numbers


_REPO_TOKEN = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_REF_TOKEN = r"(?:%s#\d+|#\d+)" % _REPO_TOKEN
_REF_LIST = r"(?:%s(?:[ \t]*(?:,|and|&)?[ \t]*%s)*)" % (_REF_TOKEN, _REF_TOKEN)


def body_text_outside_code(body: str) -> str:
    """Body lines outside fenced blocks with inline code spans stripped."""
    import re as _re

    return "\n".join(
        _re.sub(r"(`+).*?\1", "", line) for line, outside in _split_outside_fences(body) if outside
    )


#: The pre-public name, kept working for the callers inside this module.
_body_text_outside_code = body_text_outside_code


def _parse_ref_segment(segment: str, default_repo: str | None) -> list[tuple[str | None, int]]:
    """(repo, number) pairs from one keyword's ref list segment.

    Bare ``#N`` resolves to ``default_repo`` (None when the caller knows
    no repo); ``owner/repo#N`` keeps its qualifier. Order-preserving,
    deduplicated.
    """
    import re as _re

    pairs: list[tuple[str | None, int]] = []
    for match in _re.finditer(r"(%s)?#(\d+)" % _REPO_TOKEN, segment):
        repo = match.group(1)
        if repo is None:
            repo = default_repo
        number = int(match.group(2))
        if (repo, number) not in pairs:
            pairs.append((repo, number))
    return pairs


def refs_issue_numbers(body: str) -> list[int]:
    """Issue numbers a PR body names with ``Refs #N``.

    A partial win lands with ``Refs #N`` rather than
    ``Closes #N``: GitHub does not close the issue on merge, so the
    coordinator links it through this parser instead of
    ``closingIssuesReferences``. Only ``#N`` tokens outside fenced code
    count, so quoting the convention does not link a ticket. Accepts a
    list after one keyword (``Refs #12, #13 and #14``), case-insensitive.
    Same-repo bare refs only; cross-repo ``owner/repo#N`` links parse
    via ``refs_issue_refs``.
    """
    return [n for (_repo, n) in refs_issue_refs(body) if _repo is None]


def refs_issue_refs(body: str, default_repo: str | None = None) -> list[tuple[str | None, int]]:
    """(repo, number) pairs a PR body names with ``Refs``.

    Accepts bare ``#N`` (resolves to ``default_repo``) and cross-repo
    ``owner/repo#N`` in the list after one keyword
    (``Refs #12, owner/repo#13 and #14``), case-insensitive. Only tokens
    outside fenced code and inline code spans count. ``default_repo``
    is the PR's own ``owner/repo``; None leaves bare refs unqualified.
    """
    import re as _re

    if not body:
        return []
    text = _body_text_outside_code(body)
    pairs: list[tuple[str | None, int]] = []
    keyword = _re.compile(r"\brefs\b:?[ \t]*(" + _REF_LIST + r")", _re.IGNORECASE)
    for match in keyword.finditer(text):
        for pair in _parse_ref_segment(match.group(1), default_repo):
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def closes_issue_refs_from_body(
    body: str, default_repo: str | None = None
) -> list[tuple[str | None, int]]:
    """(repo, number) pairs a PR body names with a close keyword.

    Same keyword set as ``closes_issue_numbers_from_body`` (``Closes``,
    ``Fixes``, ...) plus cross-repo ``owner/repo#N`` in the list after
    one keyword (``Closes #12, owner/repo#13``), case-insensitive.
    Bare ``#N`` resolves to ``default_repo``; None leaves it unqualified.
    Only tokens outside fenced code and inline code spans count.
    """
    import re as _re

    if not body:
        return []
    text = _body_text_outside_code(body)
    pairs: list[tuple[str | None, int]] = []
    keyword = _re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?[ \t]*(" + _REF_LIST + r")", _re.IGNORECASE
    )
    for match in keyword.finditer(text):
        for pair in _parse_ref_segment(match.group(1), default_repo):
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def closes_issue_numbers_from_body(body: str) -> list[int]:
    """Issue numbers a PR body names with a close keyword.

    The CI gate (``.github/workflows/pr-body.yml``) sees only the event
    payload, not ``closingIssuesReferences``, so it parses the body's
    close keywords (``Closes #N``, ``Fixes #N``, ...) the way GitHub
    does. Only ``#N`` tokens outside fenced code count, so quoting the
    convention does not link a ticket. Accepts a list after one keyword
    (``Closes #12, #13 and #14``), case-insensitive.
    """
    import re as _re

    if not body:
        return []
    text = "\n".join(
        _re.sub(r"(`+).*?\1", "", line) for line, outside in _split_outside_fences(body) if outside
    )
    numbers: list[int] = []
    keyword = _re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?[ \t]*"
        r"((?:#\d+(?:[ \t]*(?:,|and|&)?[ \t]*))*#\d+)",
        _re.IGNORECASE,
    )
    for match in keyword.finditer(text):
        for token in _re.findall(r"#(\d+)", match.group(1)):
            number = int(token)
            if number not in numbers:
                numbers.append(number)
    return numbers


def body_linked_issue_numbers(body: str) -> list[int]:
    """Ticket numbers a PR body links: closes first, then refs.

    The CI gate's view of ``takeoff.check_pr_body``'s linked set when only
    the body text is available: close-keyword numbers plus ``Refs #N``
    numbers not already listed. An empty result stays advisory, matching
    the local gate.
    """
    numbers = closes_issue_numbers_from_body(body)
    return numbers + [n for n in refs_issue_numbers(body) if n not in numbers]


def body_linked_issue_refs(
    body: str, default_repo: str | None = None
) -> list[tuple[str | None, int]]:
    """Ticket pairs a PR body links: closes first, then refs.

    Repo-qualified view of ``body_linked_issue_numbers``: close-keyword
    pairs plus ``Refs`` pairs not already listed, deduplicated. Bare
    ``#N`` resolves to ``default_repo``; None leaves it unqualified.
    """
    pairs = closes_issue_refs_from_body(body, default_repo)
    return pairs + [p for p in refs_issue_refs(body, default_repo) if p not in pairs]


def closing_issue_refs(pr: dict, default_repo: str | None = None) -> list[tuple[str | None, int]]:
    """(repo, number) pairs from ``closingIssuesReferences``.

    Repo-qualified view of ``closing_issue_numbers``: each entry keeps
    its ``repository`` (``nameWithOwner``, or ``name`` plus
    ``owner.login``); entries without repo info resolve to
    ``default_repo``. Skips entries without an integer number.
    """
    refs = (pr or {}).get("closingIssuesReferences")
    if refs is None:
        refs = (pr or {}).get("closingIssuesRefs") or []
    pairs: list[tuple[str | None, int]] = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        number = ref.get("number")
        if isinstance(number, bool):
            continue
        if isinstance(number, int):
            pass
        elif isinstance(number, str) and number.isdigit():
            number = int(number)
        else:
            continue
        repo = default_repo
        child = ref.get("repository")
        if isinstance(child, dict):
            full = child.get("nameWithOwner")
            if isinstance(full, str) and full:
                repo = full
            else:
                name = child.get("name")
                owner = (child.get("owner") or {}).get("login", "")
                if isinstance(name, str) and name and owner:
                    repo = f"{owner}/{name}"
        if (repo, number) not in pairs:
            pairs.append((repo, number))
    return pairs


def linked_issue_refs(pr: dict) -> list[tuple[str, int]]:
    """Ticket pairs a PR links: ``closes`` first, then ``refs``.

    ``closes`` is what GitHub closes on merge; ``refs`` is a partial win that
    leaves the ticket open. Both count as links to the board, so merge gates
    and status derivation share this list.
    """
    linked: list[tuple[str, int]] = []
    for full, number in list(pr.get("closes") or []) + list(pr.get("refs") or []):
        if (full, number) not in linked:
            linked.append((full, number))
    return linked


def branch_issue_number(branch: str | None) -> int | None:
    """Ticket number from a ``<type>/<n>-<slug>`` branch, else None.

    Matches the branch convention in docs/01-design.md (e.g.
    ``feat/119-closes-warning`` yields 119). Branches that do not follow
    it (``master``, ``fix/tests-diskwatch-stop-flake-113``) yield None,
    so the caller falls back to the generic empty-closing-refs warning.
    """
    import re as _re

    if not branch:
        return None
    match = _re.match(r"[^/\s]+/(\d+)(?:-|$)", branch.strip())
    if not match:
        return None
    return int(match.group(1))


def branch_ticket_linked(
    expected: int | None,
    pairs: list[tuple[str | None, int]],
    default_repo: str | None = None,
    siblings: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """True when a linked set satisfies the branch ticket ``expected``.

    The branch's own ticket counts as linked when some pair carries its
    number and is one of: an unqualified ``#N``, the PR's own repo
    (``default_repo``), or a sibling: one of the ``owner/name`` repos
    the same ops root drives, where tickets may live apart from the
    code (``Refs acme/app-ops#450`` on ``fix/450-...`` in ``acme/app``).
    Names match case-insensitively, as GitHub treats them.

    Residual risk, accepted by design: a branch number is not repo
    qualified, so an unrelated same-number ticket in a sibling also
    passes. Any repo outside the PR's own and its siblings, a different
    number, or no ``default_repo`` for a qualified pair, still fails.
    Pure: no I/O.
    """
    if expected is None:
        return False
    allowed = {s.lower() for s in siblings}
    if default_repo:
        allowed.add(default_repo.lower())
    for repo, number in pairs:
        if number != expected:
            continue
        if repo is None:
            return True
        if default_repo and repo.lower() in allowed:
            return True
    return False


def latest_checks(rollup) -> list:
    """The newest ``statusCheckRollup`` entry per check, in rollup order.

    A re-run or a reopen leaves the superseded run on the head commit
    beside the new one, and the newer one is the
    check's state. Check runs key on (workflowName, name), commit
    statuses on their context, and the two shapes never share a key.
    The newest by ``startedAt`` wins, so a newer run in progress reads
    pending; a run still queued carries GitHub's zero time and stays
    behind the older one until it starts, which errs towards red.
    Entries without a ``startedAt`` are all kept: their order is
    unknown.
    """
    entries = [c for c in rollup or [] if isinstance(c, dict)]
    newest = {}
    undated = set()
    for index, check in enumerate(entries):
        started = check.get("startedAt")
        if not isinstance(started, str) or not started:
            undated.add(index)
            continue
        if check.get("context") and not (check.get("name") or check.get("workflowName")):
            key = ("status", "", check["context"])
        else:
            key = ("run", check.get("workflowName") or "", check.get("name") or "")
        if key not in newest or started >= entries[newest[key]]["startedAt"]:
            newest[key] = index
    keep = undated | set(newest.values())
    return [check for index, check in enumerate(entries) if index in keep]
