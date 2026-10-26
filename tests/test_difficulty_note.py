# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Difficulty note (task 2026-10-26_difficulty-note): an optional one-line note from Delivery and the Reviewer, shown to the owner.

Run:  PYTHONPATH=. python3 -m unittest tests.test_difficulty_note -v
"""
from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from harness import board, control, board_mcp, board_viewer, difficulty_note
from tests.test_requirements_contract import context, delivery_with_direction

TASK = "TASK-DIFFICULTY"


def args_for(command: str, agent: str, request: str = "", difficulty: str = "") -> argparse.Namespace:
    return argparse.Namespace(command=command, agent=agent, request=request, difficulty=difficulty)


class DifficultyNoteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        self.builder = delivery_with_direction(self.root, TASK)
        session = control.create(self.root, "claude_reviewer")
        self.reviewer = board.register(self.root, "qa", TASK, vendor="Anthropic", session_id=session["id"])

    def notes(self):
        return board.snapshot(self.root).get("difficulty_notes", {}).get(TASK, [])

    def test_the_flag_exists_on_both_commands_and_in_the_tool_schemas_as_optional(self):
        parser = board.build_parser()
        for command, extra in (("request-review", ["--ledger", "l", "--summary", "s", "--test-command", "t"]),
                               ("qa-result", ["--request", "r", "--result", "passed", "--summary", "s"])):
            self.assertEqual(parser.parse_args([command, "--agent", "a", *extra]).difficulty, "")
            self.assertEqual(parser.parse_args([command, "--agent", "a", *extra, "--difficulty", "x"]).difficulty, "x")
        for role, tool in (("engineering", "request-review"), ("qa", "qa-result")):
            schema = next(item for item in board_mcp.tool_definitions(role) if item["name"] == tool)["inputSchema"]
            self.assertIn("difficulty", schema["properties"])
            self.assertNotIn("difficulty", schema["required"])

    def test_a_note_is_stored_with_role_step_and_time_and_shown_as_one_line_per_role(self):
        board._keep_difficulty_note(self.root, args_for("request-review", self.builder["id"], difficulty="the build was broken"), {"id": "REQ-1"})
        board._keep_difficulty_note(self.root, args_for("qa-result", self.reviewer["id"], "REQ-1", "the requirement was vague"), {})
        board._keep_difficulty_note(self.root, args_for("request-review", self.builder["id"], difficulty="a tool was missing"), {"id": "REQ-2"})
        stored = self.notes()
        self.assertEqual([(n["role"], n["step"], n["text"]) for n in stored],
                         [("builder", "REQ-1", "the build was broken"), ("reviewer", "REQ-1", "the requirement was vague"), ("builder", "REQ-2", "a tool was missing")])
        self.assertTrue(all(n["at"] for n in stored))
        state = board.snapshot(self.root)
        self.assertEqual(difficulty_note.line(state, TASK),
                         "What slowed the builder: a tool was missing · What slowed the reviewer: the requirement was vague")
        self.assertEqual(board_viewer._compact_dashboard_state(state, [TASK])["task_difficulty"][TASK]["line"], difficulty_note.line(state, TASK))

    def test_a_missing_or_empty_note_stores_nothing_and_shows_nothing(self):
        for text in ("", "   ", None):
            board._keep_difficulty_note(self.root, args_for("request-review", self.builder["id"], difficulty=text), {"id": "REQ-1"})
        self.assertEqual(self.notes(), [])
        state = board.snapshot(self.root)
        self.assertEqual(difficulty_note.line(state, TASK), "")
        self.assertEqual(board_viewer._compact_dashboard_state(state, [TASK])["task_difficulty"][TASK]["line"], "")

    def test_an_overlong_note_is_trimmed_never_refused(self):
        board._keep_difficulty_note(self.root, args_for("request-review", self.builder["id"], difficulty="slow " * 500), {"id": "REQ-1"})
        text = self.notes()[0]["text"]
        self.assertLessEqual(len(text), difficulty_note.max_chars())
        self.assertTrue(text.startswith("slow slow"))

    def test_a_problem_keeping_the_note_never_fails_the_command(self):
        board._keep_difficulty_note(self.root, args_for("qa-result", "no-such-agent", "NO-SUCH-REQUEST", "text"), {})
        self.assertEqual(self.notes(), [])

    def test_the_history_row_carries_the_line_and_the_page_escapes_it(self):
        board._keep_difficulty_note(self.root, args_for("request-review", self.builder["id"], difficulty="<img src=x onerror=alert(1)>"), {"id": "REQ-1"})
        state = board.snapshot(self.root)
        self.assertEqual(difficulty_note.line(state, TASK), "What slowed the builder: <img src=x onerror=alert(1)>")
        # the page draws both lines through esc(), never as raw markup
        source = Path(board_viewer.__file__).read_text(encoding="utf-8")
        self.assertIn("data-task-difficulty-history>${esc(item.difficulty_line)}", source)
        self.assertIn("data-task-difficulty=\"${esc(name)}\">${esc(state.task_difficulty[name].line)}", source)


if __name__ == "__main__":
    unittest.main()
