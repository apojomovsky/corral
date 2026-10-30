"""Activity lines for both runtime event formats, and `show` over a file.

Offline: every session file is a temp file written by the test.
"""

import io
import json
import os
import tempfile
import unittest

from corral.runtimes import tail


def entry(role, *content):
    return json.dumps({"type": "message", "message": {"role": role, "content": list(content)}})


CALL = {
    "type": "toolCall",
    "name": "bash",
    "intent": "Running the suite",
    "arguments": {"command": "make test"},
}
SAID = {"type": "text", "text": "Tests pass.\nOpening the PR."}


class RenderOmp(unittest.TestCase):
    def test_a_tool_call_shows_its_intent_and_arguments(self):
        self.assertEqual(
            tail.render(entry("assistant", CALL)),
            ['-> Running the suite | {"command": "make test"}'],
        )

    def test_a_tool_call_without_intent_shows_its_name(self):
        bare = {"type": "toolCall", "name": "read", "arguments": {}}
        self.assertEqual(tail.render(entry("assistant", bare)), ["-> read | {}"])

    def test_assistant_text_is_one_line(self):
        self.assertEqual(tail.render(entry("assistant", SAID)), ["   Tests pass. Opening the PR."])

    def test_blank_text_is_left_out(self):
        self.assertEqual(tail.render(entry("assistant", {"type": "text", "text": "  "})), [])
        self.assertEqual(tail.render(entry("assistant", {"type": "text", "text": None})), [])

    def test_thinking_tool_results_and_the_prompt_are_left_out(self):
        self.assertEqual(tail.render(entry("assistant", {"type": "thinking", "thinking": "x"})), [])
        self.assertEqual(tail.render(entry("toolResult", {"type": "text", "text": "out"})), [])
        self.assertEqual(tail.render(entry("user", {"type": "text", "text": "prompt"})), [])

    def test_lines_that_are_not_messages_are_skipped(self):
        for line in (
            "",
            "not json",
            "[]",
            "null",
            json.dumps({"type": "custom", "data": {}}),
            json.dumps({"message": {"role": "assistant", "content": "plain"}}),
            json.dumps({"message": {"role": "assistant", "content": [None, 3]}}),
        ):
            self.assertEqual(tail.render(line), [], line)

    def test_long_text_is_cut(self):
        long = {"type": "text", "text": "y" * 500}
        self.assertLessEqual(len(tail.render(entry("assistant", long))[0]), 3 + tail.WIDTH)


def event(kind, part):
    return json.dumps({"type": kind, "sessionID": "ses_1", "part": part})


class RenderOpencode(unittest.TestCase):
    def test_a_tool_use_shows_its_description_or_its_name(self):
        bash = {
            "tool": "bash",
            "state": {"input": {"command": "make test", "description": "Running the suite"}},
        }
        self.assertEqual(
            tail.render(event("tool_use", bash)),
            ['-> Running the suite | {"command": "make test", "description": "Running the suite"}'],
        )
        bare = {"tool": "read", "state": {"input": {"filePath": "a"}}}
        self.assertEqual(tail.render(event("tool_use", bare)), ['-> read | {"filePath": "a"}'])

    def test_a_tool_use_with_no_state_still_renders(self):
        self.assertEqual(
            tail.render(event("tool_use", {"tool": "glob", "state": "odd"})), ["-> glob | {}"]
        )

    def test_text_is_one_line_and_steps_are_left_out(self):
        self.assertEqual(tail.render(event("text", {"text": "All\ngreen."})), ["   All green."])
        self.assertEqual(tail.render(event("step_start", {})), [])
        self.assertEqual(tail.render(event("step_finish", {"reason": "stop"})), [])


class Show(unittest.TestCase):
    def path(self, *lines, tail_text=""):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        handle.write("".join(line + "\n" for line in lines) + tail_text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def said(self, text):
        return entry("assistant", {"type": "text", "text": text})

    def test_only_the_last_entries_are_shown(self):
        path = self.path(self.said("one"), self.said("two"), self.said("three"))
        out = io.StringIO()
        tail.show(path, last=2, out=out)
        self.assertEqual(out.getvalue(), "   two\n   three\n")

    def test_last_zero_shows_nothing_and_no_last_shows_all(self):
        path = self.path(self.said("one"), self.said("two"))
        out = io.StringIO()
        tail.show(path, last=0, out=out)
        self.assertEqual(out.getvalue(), "")
        tail.show(path, out=out)
        self.assertEqual(out.getvalue(), "   one\n   two\n")

    def test_the_offset_moves_past_complete_lines_only(self):
        path = self.path(self.said("one"), tail_text=self.said("half"))
        out = io.StringIO()
        offset = tail.show(path, out=out)
        self.assertEqual(out.getvalue(), "   one\n")
        self.assertEqual(offset, len(self.said("one").encode()) + 1)

    def test_a_later_call_from_the_offset_picks_up_new_lines_only(self):
        path = self.path(self.said("one"))
        out = io.StringIO()
        offset = tail.show(path, out=out)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(self.said("two") + "\n")
        tail.show(path, out=out, offset=offset)
        self.assertEqual(out.getvalue(), "   one\n   two\n")

    def test_both_formats_can_be_read_from_one_file(self):
        path = self.path(self.said("omp says"), event("text", {"text": "opencode says"}))
        out = io.StringIO()
        tail.show(path, out=out)
        self.assertEqual(out.getvalue(), "   omp says\n   opencode says\n")


def mline(payload_type, payload=None, sequence=1):
    return json.dumps(
        {"payload_type": payload_type, "payload": payload or {}, "sequence": sequence}
    )


class RenderMuse(unittest.TestCase):
    def test_a_started_run_shows_the_prompt(self):
        line = mline("run.lifecycle.started", {"prompt": "Do the thing."})
        self.assertEqual(tail.render(line), ["   Do the thing."])

    def test_a_started_run_without_a_prompt_shows_nothing(self):
        for payload in ({}, {"prompt": "  "}, {"prompt": None}, {"prompt": 5}):
            with self.subTest(payload=payload):
                self.assertEqual(tail.render(mline("run.lifecycle.started", payload)), [])

    def test_output_deltas_show_text_as_one_line(self):
        line = mline("run.output.delta", {"text": "Working\non it."})
        self.assertEqual(tail.render(line), ["   Working on it."])

    def test_a_completed_run_shows_its_final_text(self):
        line = mline(
            "run.terminal.completed",
            {"terminal": "completed", "reason": "done", "text": "All green."},
        )
        self.assertEqual(tail.render(line), ["   All green."])

    def test_a_completed_run_without_text_shows_nothing(self):
        line = mline("run.terminal.completed", {"terminal": "completed", "reason": "done"})
        self.assertEqual(tail.render(line), [])

    def test_usage_task_reconciliation_and_link_records_show_nothing(self):
        quiet = [
            mline("run.usage", {"calls": 2, "allowance": None}),
            mline("task.lifecycle.proposed", {}),
            mline("task.lifecycle.scheduled", {}),
            mline("task.lifecycle.started", {}),
            mline("task.lifecycle.completed", {}),
            mline("task.lifecycle.failed", {}),
            mline("reconciliation.done", {}),
            mline("session.run.linked", {}),
            mline("task.stream.linked", {}),
        ]
        for line in quiet:
            with self.subTest(line=line):
                self.assertEqual(tail.render(line), [])

    def test_tool_results_are_skipped_until_their_shape_lands(self):
        line = mline(
            "tool.result",
            {"tool_name": "bash", "correlation_facts": {"outcome": "ok"}},
        )
        self.assertEqual(tail.render(line), [])

    def test_unknown_payload_types_and_bad_lines_show_nothing(self):
        lines = [
            mline("run.something.new", {"text": "hi"}),
            json.dumps({"payload_type": None, "payload": {}}),
            json.dumps({"payload": {"text": "hi"}}),
            json.dumps({"payload_type": "run.output.delta", "payload": {"text": 5}}),
            json.dumps({"payload_type": "run.output.delta", "payload": "odd"}),
            "not json",
            "",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(tail.render(line), [])

    def test_long_muse_text_is_cut(self):
        line = mline("run.output.delta", {"text": "y" * 500})
        self.assertLessEqual(len(tail.render(line)[0]), 3 + tail.WIDTH)


class ShowMuse(unittest.TestCase):
    def path(self, *lines, tail_text=""):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        handle.write("".join(line + "\n" for line in lines) + tail_text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_muse_lines_read_through_one_file_with_the_other_formats(self):
        path = self.path(
            mline("run.lifecycle.started", {"prompt": "Do the thing."}),
            mline("run.output.delta", {"text": "Working on it."}),
            entry("assistant", {"type": "text", "text": "omp says"}),
            event("text", {"text": "opencode says"}),
            mline("run.terminal.completed", {"terminal": "completed", "text": "Done."}),
        )
        out = io.StringIO()
        tail.show(path, out=out)
        self.assertEqual(
            out.getvalue(),
            "   Do the thing.\n   Working on it.\n   omp says\n   opencode says\n   Done.\n",
        )


class NonStringText(unittest.TestCase):
    """A text field that is not a string is no text: it renders as nothing, never a crash."""

    VALUES = (5, 0.5, True, ["x"], {"a": 1})

    def test_omp_shape(self):
        for value in self.VALUES:
            with self.subTest(value=value):
                line = entry("assistant", {"type": "text", "text": value})
                self.assertEqual(tail.render(line), [])

    def test_opencode_shape(self):
        for value in self.VALUES:
            with self.subTest(value=value):
                self.assertEqual(tail.render(event("text", {"text": value})), [])


if __name__ == "__main__":
    unittest.main()
