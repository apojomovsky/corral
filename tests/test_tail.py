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


if __name__ == "__main__":
    unittest.main()
