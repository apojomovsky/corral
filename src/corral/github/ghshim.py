"""Paced accounting for agent `gh` calls: costs, the call log and its summary.

Agents that call `gh` directly bypass the shared GraphQL pacer in
`corral.github.gh`, and their bursts trip GitHub's secondary limits for
every process on the host. The shim (`corral.github.gh_shim`) is the `gh`
that agent units find first on PATH. For each call it takes points from
the shared bucket, starts the shared cooldown on a rate-limit answer, and
appends one redacted line to the call log. `corral gh-calls` summarizes
that log.

The log keeps the first argv word and, when it looks like a plain word,
the second. Anything else in the second slot (a URL, a query string, a
clone address with credentials) is logged as `<arg>`, so tokens and
`--body` content never reach the disk. GraphQL calls also carry one shape
token: the root selection names only, never query text or variables.

Past ROTATE_BYTES the log moves to `<path>.1`, replacing the previous copy.
Writers do not lock the rotation, so under concurrent writers a rotation
can race and drop at most one generation of log lines. Nothing leaks into
the wrong file, and the pacer state is never touched by rotation.

`CORRAL_GH_PACED=1` (gh.PACED_MARKER) is a trusted-environment marker. Corral's
own paced runner sets it on its children, which already paid the bucket. An
agent that sets it by hand only skips its own accounting; it gains no
other access.
"""

import json
import os
import re
import shlex
import sys
import time

from corral import paths
from corral.github import gh
from corral.reviewcli import AGENT_ID_ENV

LOG_NAME = "gh-calls.jsonl"
ROTATE_BYTES = 5 * 1024 * 1024

SHIM_NAME = "gh"
SHIM_DIR_NAME = "dispatch"
#: Names the directory the shim executable sits in. The shim skips that
#: directory when it searches PATH for the real `gh`, so it never runs itself.
SHIM_DIR_ENV = "CORRAL_GH_SHIM_DIR"
#: Nesting depth the shim stamps into its child's environment. A gh that calls
#: gh again (an alias or extension) runs one level deeper; a shim that starts
#: at MAX_DEPTH or deeper refuses, so runaway nesting stops.
DEPTH_ENV = "CORRAL_GH_SHIM_DEPTH"
MAX_DEPTH = 3
#: The state dir the wrapper exports, so every agent shares one bucket and log.
STATE_ENV = "CORRAL_STATE"
#: A line the wrapper always contains. PATH candidates carrying it are wrappers
#: of this shim and are skipped, so two shim directories cannot loop.
WRAPPER_MARKER = "# corral-gh-shim"
#: Seconds the real gh may run before the shim stops it. Agents run long
#: commands such as `pr checks --watch` and `gh run watch`, so this is generous.
TIMEOUT_S = 3600.0
#: Exit code when the shim stops a timed-out gh (the timeout(1) convention).
TIMED_OUT_EXIT = 124
#: Bytes of stdout and stderr kept (the last ones) for the rate-limit check.
TAIL_BYTES = 64 * 1024

#: `gh` entry points that never reach the API, so the shim logs them but
#: charges nothing (auth status reads the local keyring, config/alias are
#: local files, completion/help print text).
_LOCAL_SECOND = {
    "auth": {"status"},
    "config": set(),
    "alias": set(),
    "extension": {"list"},
}
_LOCAL_FIRST = {"completion", "help", "--help", "-h", "--version", "-V"}

#: Subcommand words that mutate state, charged at the write cost. A
#: heuristic, not a rule: a missed word undercharges one call, and the
#: bucket is conservative enough to absorb it.
_WRITE_WORDS = frozenset(
    {
        "add",
        "cancel",
        "close",
        "comment",
        "create",
        "delete",
        "disable",
        "edit",
        "enable",
        "fork",
        "install",
        "lock",
        "merge",
        "pin",
        "remove",
        "reopen",
        "rerun",
        "resolve",
        "review",
        "run",
        "set",
        "sync",
        "transfer",
        "unlock",
        "unpin",
        "upload",
        "item-add",
        "item-edit",
        "item-delete",
        "item-archive",
        "ready",
        "update-branch",
    }
)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_SINCE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdSMHD])?\s*$")
_SINCE_MULTIPLIER = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def log_path(directory=None):
    """The call log, `<state>/gh-calls.jsonl`; `directory` overrides the state dir."""
    base = paths.state_dir() if directory is None else directory
    return os.path.join(base, LOG_NAME)


def shim_dir(state=None):
    """Where the shim executable is installed: `<state>/dispatch` by default."""
    base = paths.state_dir() if state is None else state
    return os.path.join(base, SHIM_DIR_NAME)


def install_shim(directory=None, state=None):
    """Write the `gh` wrapper into `directory` (default `shim_dir()`); return its path.

    The wrapper names its own directory in SHIM_DIR_ENV, so the shim can skip
    it on PATH, exports CORRAL_STATE as the state dir at install time (`state`,
    default `paths.state_dir()`), and runs the interpreter that installed
    corral. The export wins over whatever the agent's environment says, so
    every unit shares one bucket and one log. It is not put on any agent's
    PATH here.
    """
    target_dir = shim_dir() if directory is None else directory
    state_dir = paths.state_dir() if state is None else state
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, SHIM_NAME)
    body = (
        "#!/bin/sh\n"
        f"{WRAPPER_MARKER}\n"
        f"{STATE_ENV}={shlex.quote(os.path.realpath(state_dir))} "
        f"{SHIM_DIR_ENV}={shlex.quote(os.path.realpath(target_dir))} "
        f'exec {shlex.quote(sys.executable)} -P -m corral.github.gh_shim "$@"\n'
    )
    paths.atomic_write_text(target, body)
    os.chmod(target, 0o755)
    return target


def caller_name(env=None):
    """Who made the call: the agent id from CORRAL_AGENT_ID, else "unknown"."""
    env = os.environ if env is None else env
    return (env.get(AGENT_ID_ENV) or "").strip() or "unknown"


def paced_marker_set(env=None):
    """Whether this call already went through the shared bucket.

    `gh.run(paced=True)` sets the marker on its children, which already
    charged the bucket and cooled it down on errors.
    """
    env = os.environ if env is None else env
    return env.get(gh.PACED_MARKER) == "1"


#: A second word that is a plain name (`list`, `create`). Anything else, such as
#: `repos/o/r`, a URL with a query string or credentials, is logged as <arg>.
_PLAIN_WORD = re.compile(r"[A-Za-z][\w-]*")


def short_argv(argv):
    """The first argv word verbatim, and the second only when it is a plain word."""
    words = [str(word) for word in list(argv or [])[:2]]
    if len(words) > 1 and not _PLAIN_WORD.fullmatch(words[1]):
        words[1] = "<arg>"
    return words


def cost_for(argv):
    """Shared-bucket cost of a `gh` call, or None when it never hits the API.

    Mirrors the `paced_run`/`graphql` split: GraphQL 3 (5 for a mutation),
    REST 1 a read and 5 a mutative call. When the cost cannot be known (a
    GraphQL query read from a file or stdin) the higher cost applies.
    """
    words = [str(word) for word in (argv or [])]
    if not words:
        return None
    first = words[0]
    if first in _LOCAL_FIRST:
        return None
    rest = words[1:]
    if first in _LOCAL_SECOND and _is_local(first, rest):
        # `auth status`, every `config`/`alias` read, `extension list`:
        # local files, never the API. Other subcommands (`auth login`,
        # `extension install`) fall through to normal costing below.
        return None
    if first == "api":
        return _api_cost(words)
    if rest and rest[0] in _WRITE_WORDS:
        return gh.REST_WRITE_COST
    return gh.REST_READ_COST


def _api_cost(words):
    """Cost of a `gh api` call: GraphQL by its query, REST by its method."""
    rest = words[1:]
    if _is_graphql(words):
        query = _query_text(words)
        if query is None or query.lstrip().startswith("@"):
            # Query from a file or stdin: unknown, so assume the mutation cost.
            return gh.GRAPHQL_MUTATION_COST
        if re.search(r"\bmutation\b", " ".join(words)):
            return gh.GRAPHQL_MUTATION_COST
        return gh.GRAPHQL_CALL_COST
    method = _api_method(rest)
    if method is None:
        # gh sends a request with fields as POST, so fields make a write.
        return gh.REST_WRITE_COST if _has_body_flag(rest) else gh.REST_READ_COST
    if method == "GET":
        return gh.REST_READ_COST
    return gh.REST_WRITE_COST if method in _MUTATING_METHODS else gh.REST_READ_COST


def _api_method(rest):
    """The explicit HTTP method upper-cased (`-X POST`, `--method=post`, `-XPOST`), or None."""
    for index, word in enumerate(rest):
        if word in ("-X", "--method") and index + 1 < len(rest):
            return rest[index + 1].upper()
        if word.startswith("--method="):
            return word[len("--method=") :].upper()
        if word.startswith("-X") and len(word) > 2:
            return word[2:].upper()
    return None


#: Flags that send a request body or fields, so the call is a write unless
#: the method is explicitly GET. Attached forms (`-fkey=v`, `--field=k=v`)
#: are matched by prefix.
_BODY_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input")


def _has_body_flag(rest):
    for word in rest:
        if word in _BODY_FLAGS:
            return True
        if any(word.startswith(flag + "=") for flag in _BODY_FLAGS if flag.startswith("--")):
            return True
        if word[:2] in ("-f", "-F") and len(word) > 2:
            return True
    return False


def _is_graphql(words):
    """Whether argv is a GraphQL call through `gh api`."""
    return (
        len(words) > 1 and words[0] == "api" and (words[1] == "graphql" or "graphql" in words[1:])
    )


#: Flags whose following (or `=`-attached) value can carry the GraphQL
#: query text (`-f query=...` in `gh.graphql`, `--raw-field`/`--field`
#: for hand-rolled agent calls).
_QUERY_FLAGS = ("-f", "-F", "--raw-field", "--field")

#: Longest shape token kept in the log; our shapes sit far below it.
_SHAPE_MAX_LEN = 80

#: GraphQL keywords that are never shape names.
_SKIP_WORDS = frozenset({"query", "mutation", "subscription", "fragment", "on"})


def _query_text(argv):
    """The GraphQL query text in argv, or None when no `query=` is set."""
    words = [str(word) for word in (argv or [])]
    for index, word in enumerate(words):
        if word in _QUERY_FLAGS:
            if index + 1 >= len(words):
                return None
            candidate = words[index + 1]
        else:
            candidate = None
            for flag in _QUERY_FLAGS:
                if word.startswith(flag + "="):
                    candidate = word[len(flag) + 1 :]
                    break
                if flag in ("-f", "-F") and word.startswith(flag) and (len(word) > len(flag)):
                    candidate = word[len(flag) :].lstrip("=")
                    break
            if candidate is None:
                continue
        if candidate.startswith("query="):
            return candidate[len("query=") :]
    return None


def _stripped(query):
    """Query text without strings or `#` comments (brace traps removed)."""
    chars = []
    i, end = 0, len(query)
    while i < end:
        ch = query[i]
        if ch == "#":
            while i < end and query[i] != "\n":
                i += 1
            continue
        if ch in "\"'":
            i += 1
            while i < end and query[i] != ch:
                if query[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        chars.append(ch)
        i += 1
    return "".join(chars)


def _preceded_by_spread(text, index):
    """Whether only whitespace and `...` sit right before index."""
    j = index - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    return text[max(0, j - 2) : j + 1] == "..."


def _root_fields(query):
    """Sorted root selection names of one GraphQL operation.

    Strings and `#` comments are stripped first so braces inside them
    never count; arguments, directives and `$variables` never sit at
    depth 1 outside parentheses, so only real selections survive. []
    means the text was unreadable.
    """
    text = _stripped(query)
    start = text.find("{")
    if start < 0:
        return []
    names = set()
    depth = 0
    parens = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            if depth <= 0:
                break
            i += 1
        elif ch == "(":
            parens += 1
            i += 1
        elif ch == ")":
            parens = max(0, parens - 1)
            i += 1
        elif ch == "@":
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] in "_-"):
                i += 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if depth == 1 and parens == 0 and word not in _SKIP_WORDS:
                after = j
                while after < len(text) and text[after] in " \t\r\n":
                    after += 1
                if after < len(text) and text[after] == ":":
                    pass  # alias: caller-chosen, the real name follows
                elif _preceded_by_spread(text, i):
                    pass  # named fragment spread, not a schema root
                else:
                    names.add(word)
            i = j
        else:
            i += 1
    return sorted(names)


def shape_for(argv):
    """One query-shape token per GraphQL call, else None.

    The token names the operation's root selections (`user`,
    `repository+user`, `node`, `mutation:addProjectV2ItemById`), so a day
    of calls splits board reads from item reads at a glance. Roots are
    schema names, never query text, variables or secrets.
    """
    words = [str(word) for word in (argv or [])]
    if not _is_graphql(words):
        return None
    query = _query_text(words)
    if not query:
        return None
    head = _stripped(query).lstrip()
    mutation = head.startswith("mutation") and (
        len(head) == len("mutation")
        or not (head[len("mutation")].isalnum() or head[len("mutation")] == "_")
    )
    roots = _root_fields(query)
    if not roots:
        return None
    token = "+".join(roots[:5])
    if mutation:
        token = "mutation:" + token
    return token[:_SHAPE_MAX_LEN] or None


def _is_local(first, rest):
    """Whether a locally-handled entry point stays off the bucket."""
    allowed = _LOCAL_SECOND.get(first)
    if allowed is None:
        return False
    if first in ("config", "alias"):
        return True
    return bool(rest) and rest[0] in allowed


def pace_before(cost):
    """Take `cost` points from the shared bucket; (waited_s, capped).

    Waits rather than fails, like `GraphqlPacer.acquire`. When the wait
    would exceed the pacer's cap the call goes through anyway, `capped` is
    true, and the cost is still charged (the balance may go negative), so
    later callers wait for it and the shim never blocks forever.
    """
    pacer = gh._pacer()
    start = time.monotonic()
    try:
        pacer.acquire(cost=cost)
        return time.monotonic() - start, False
    except gh.GhError:
        pacer.charge(cost)
        return time.monotonic() - start, True


def pace_after(stderr_text):
    """Start the shared cooldown on a rate-limit answer in stderr; whether one was set."""
    if not gh.is_rate_limited(stderr_text or ""):
        return False
    gh._pacer().cooldown(gh.retry_after_s(stderr_text))
    return True


def _payload_limit_text(stdout_text):
    """The rate-limit message in a JSON error payload on stdout, else None.

    Only a parsed payload counts: a top-level `errors` list with an item typed
    RATE_LIMITED, or a top-level `message` that `gh.is_rate_limited` matches.
    Free text that merely mentions a rate limit does not.
    """
    try:
        payload = json.loads(stdout_text or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and item.get("type") == "RATE_LIMITED":
                message = item.get("message")
                return message if isinstance(message, str) and message else "rate limit exceeded"
    message = payload.get("message")
    if isinstance(message, str) and gh.is_rate_limited(message):
        return message
    return None


def pace_after_payload(stdout_text, exit_code):
    """Start the shared cooldown on a rate-limit JSON payload from a failed call.

    Inspected only when gh exits non-zero, so a successful answer whose
    content mentions a rate limit never cools the host down. Whether one was set.
    """
    if exit_code == 0:
        return False
    limit = _payload_limit_text(stdout_text)
    if limit is None:
        return False
    gh._pacer().cooldown(gh.retry_after_s(limit))
    return True


def _append_record(path, rec):
    """Append one record as a JSON line, rotating past ROTATE_BYTES.

    Once the file reaches ROTATE_BYTES it moves to `<path>.1`, replacing the
    previous generation, so disk use is bounded at about twice the limit.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        if os.path.getsize(path) >= ROTATE_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, sort_keys=True) + "\n")


def append_call(
    path=None, *, caller, argv, exit_code, duration_s, throttled, paced=False, shape=None
):
    """Append one redacted call record, rotating past ROTATE_BYTES.

    `paced` flags calls the shared bucket already handled (the shim passed
    them through); `summarize` counts them like any other call. `shape`
    defaults to `shape_for(argv)`, so every GraphQL record carries its
    query-shape token without the caller passing one.
    """
    if path is None:
        path = log_path()
    if shape is None:
        try:
            shape = shape_for(argv)
        except Exception:
            shape = None
    _append_record(
        path,
        {
            "ts": time.time(),
            "caller": caller,
            "argv": short_argv(argv),
            "shape": shape,
            "exit": exit_code,
            "duration_s": round(duration_s, 3),
            "throttled": bool(throttled),
            "paced": bool(paced),
        },
    )


def parse_since(text, now=None):
    """Epoch cutoff for `--since 1h`; None means the whole log.

    Accepts `<n>[s|m|h|d]` (bare `<n>` is seconds). Raises ValueError on
    anything else.
    """
    if text is None or str(text).strip() == "":
        return None
    match = _SINCE_RE.match(str(text))
    if not match:
        raise ValueError("bad --since %r: want like 30m, 1h, 7d" % (text,))
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    now = time.time() if now is None else now
    return now - amount * _SINCE_MULTIPLIER[unit]


def summarize(path=None, *, since=None):
    """Fold the log into per-caller-per-minute counts plus throttled totals.

    `since` is an epoch cutoff (see `parse_since`). Bad lines are skipped,
    so a half-written tail never breaks the summary. Returns a plain dict
    with `calls`, `throttled`, `since`, `by_minute` [{minute, caller,
    calls}] sorted by minute then caller, `by_caller` {caller: calls},
    and `by_shape` {shape: calls} over GraphQL records only (pre-token
    GraphQL lines count as `unknown`).
    """
    if path is None:
        path = log_path()
    per_minute = {}
    per_caller = {}
    per_shape = {}
    calls = 0
    throttled = 0
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
            stamp = float(rec["ts"])
            caller = str(rec["caller"])
        except (ValueError, KeyError, TypeError):
            continue
        if since is not None and stamp < since:
            continue
        calls += 1
        if rec.get("throttled"):
            throttled += 1
        per_caller[caller] = per_caller.get(caller, 0) + 1
        minute = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(stamp))
        per_minute[(minute, caller)] = per_minute.get((minute, caller), 0) + 1
        shape = rec.get("shape")
        if not isinstance(shape, str):
            words = [str(word) for word in (rec.get("argv") or [])]
            shape = "unknown" if _is_graphql(words) else None
        if shape is not None:
            per_shape[shape] = per_shape.get(shape, 0) + 1
    rows = [
        {"minute": minute, "caller": caller, "calls": count}
        for (minute, caller), count in sorted(per_minute.items())
    ]
    return {
        "calls": calls,
        "throttled": throttled,
        "since": since,
        "by_minute": rows,
        "by_caller": dict(sorted(per_caller.items())),
        "by_shape": dict(sorted(per_shape.items())),
    }


def _shape_lines(summary):
    """`shape <token> <count>` lines, count-descending then by token."""
    return [
        "shape %s %d" % (shape, count)
        for shape, count in sorted(
            summary.get("by_shape", {}).items(), key=lambda item: (-item[1], item[0])
        )
    ]


def render_by_shape(summary):
    """`gh-calls --by-shape` output: per-shape counts only.

    Same `shape <token> <count>` lines as `render`, without the per-minute
    rows, so a day of calls splits board reads (`user`) from item reads
    (`node`, `repository+user`) at a glance.
    """
    lines = ["gh-calls: %d calls, %d throttled" % (summary["calls"], summary["throttled"])]
    lines.extend(_shape_lines(summary))
    return "\n".join(lines) + "\n"


def render(summary):
    """Human-readable `gh-calls` output: per-caller-per-minute rates."""
    lines = ["gh-calls: %d calls, %d throttled" % (summary["calls"], summary["throttled"])]
    for row in summary["by_minute"]:
        lines.append("%s %s %d" % (row["minute"], row["caller"], row["calls"]))
    lines.extend(_shape_lines(summary))
    return "\n".join(lines) + "\n"
