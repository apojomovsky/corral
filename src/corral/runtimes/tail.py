"""An agent's live activity, from its session file, in one line per step.

Three event formats are read, told apart by their shape: omp's session
`.jsonl` (assistant messages with tool calls and text), opencode's
`--format json` events (a `part` with a tool use or text), and muse's
`--json` stream (records with a `payload_type` and a `payload`). Each
shows the agent's tool calls with their intent or description, and what
it says. Thinking, tool output, steps and the prompt are left out, except
that a muse run header shows the prompt that started the run.
"""

import json
import sys

WIDTH = 200


def _one_line(text):
    return " ".join(text.split())[:WIDTH]


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    """`value` when it is a non-blank string, else None: any other type is no text."""
    return value if isinstance(value, str) and value.strip() else None


def render(line):
    """Display lines for one session entry; [] for anything else."""
    try:
        entry = json.loads(line)
    except ValueError:
        return []
    if not isinstance(entry, dict):
        return []
    if isinstance(entry.get("part"), dict):
        return _render_event(entry)
    if isinstance(entry.get("payload_type"), str):
        return _render_muse(entry)
    message = _dict(entry.get("message"))
    if message.get("role") != "assistant":
        return []
    content = message.get("content")
    out = []
    for item in content if isinstance(content, list) else ():
        kind = _dict(item).get("type")
        if kind == "toolCall":
            args = json.dumps(item.get("arguments") or {})
            label = item.get("intent") or item.get("name") or "tool"
            out.append(_one_line(f"-> {label} | {args}"))
        elif kind == "text" and _text(item.get("text")):
            out.append("   " + _one_line(item["text"]))
    return out


def _render_event(entry):
    """One opencode `--format json` event."""
    part = entry["part"]
    if entry.get("type") == "tool_use":
        args = _dict(_dict(part.get("state")).get("input"))
        label = args.get("description") or part.get("tool") or "tool"
        return [_one_line(f"-> {label} | {json.dumps(args)}")]
    if entry.get("type") == "text" and _text(part.get("text")):
        return ["   " + _one_line(part["text"])]
    return []


#: Muse `--json` records shown as the agent's visible text, with the
#: payload key holding that text. One table, so a listed type always has
#: its key: a type with no usable key shows nothing instead of throwing.
_MUSE_TEXT = {
    "run.lifecycle.started": "prompt",
    "run.output.delta": "text",
    "run.terminal.completed": "text",
}


def _render_muse(entry):
    """One muse `--json` stream record.

    The run header shows the starting prompt, a delta and the terminal
    record show their text. Usage, subagent roster noise, reconciliation
    and link records show nothing. `tool.result` also shows nothing: its
    render rule waits on the first real tool call fixture. Anything else
    is skipped, so unknown record types never misrender.
    """
    key = _MUSE_TEXT.get(entry["payload_type"])
    if key is None:
        return []
    payload = _dict(entry.get("payload"))
    text = _text(payload.get(key))
    if text is None:
        return []
    return ["   " + _one_line(text)]


def show(path, last=None, out=None, offset=0):
    """Print the complete entries from `offset`, only the last `last` when given.

    Returns the offset after the last complete line, so a caller that polls the
    file can resume there and pick up a line the agent was still writing.
    """
    out = out or sys.stdout
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n") + 1
    lines = []
    for raw in data[:end].splitlines():
        lines.extend(render(raw.decode("utf-8", "replace")))
    if last is not None:
        lines = lines[max(0, len(lines) - last) :]
    for line in lines:
        print(line, file=out, flush=True)
    return offset + end
