# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Agents never ask the owner in their terminal (task 2026-10-23_no-terminal-questions).

The real launch script is run with a fake CLI that records its command line: every managed Claude agent has the CLI's question tool denied
(fresh and resumed), Codex has its question feature switched off, and the directives say questions go through the board.
Run:  PYTHONPATH=. python3 -m unittest tests.test_no_terminal_questions -v
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import tempfile

from harness import board, control
from tests.test_requirements_contract import context, delivery_with_direction
from tests import test_stage4_system_layer as stage4
from tests.test_conversation_memory import stage_relaunch

ROOT = Path(__file__).resolve().parents[1]


class LaunchLineTests(unittest.TestCase):
    setUp = stage4.Stage4RunnerTests.setUp
    fake = stage4.Stage4RunnerTests.fake
    switch = stage4.Stage4RunnerTests.switch
    launch = stage4.Stage4RunnerTests.launch
    require_confinement = stage4.Stage4RunnerTests.require_confinement
    served = stage4.Stage4RunnerTests.served
    bootstrap_served = stage4.Stage4RunnerTests.bootstrap_served

    def assert_question_tool_denied(self, argv):
        index = argv.index("--disallowedTools")
        self.assertEqual(argv[index + 1], "AskUserQuestion")
        self.assertTrue(argv[index + 2].startswith("--"), "the variadic option must not swallow the next argument")

    def test_every_managed_claude_role_has_the_question_tool_denied_fresh_and_resumed(self):
        self.require_confinement()
        self.switch("claude", stage4=False)
        for kind in ("claude_reviewer", "claude_cto"):
            with self.subTest(kind=kind):
                session = control.create(self.context, kind)
                argv = self.launch(session, kind)
                self.assert_question_tool_denied(argv)
                minted = argv[argv.index("--session-id") + 1]
                store = self.claude_dir / "projects" / "-code"; store.mkdir(parents=True, exist_ok=True)
                (store / f"{minted}.jsonl").write_text("{}\n", encoding="utf-8")
                stage_relaunch(self.context, session["id"])
                resumed = self.launch(session, kind)
                self.assertIn("--resume", resumed)
                self.assert_question_tool_denied(resumed)

    def test_codex_has_its_question_feature_switched_off(self):
        self.switch("codex", stage4=False)
        session = control.create(self.context, "codex_delivery")
        argv = self.launch(session, "codex_delivery")
        self.assertEqual(argv[argv.index("--disable") + 1], "default_mode_request_user_input")
        self.assertNotIn("--disallowedTools", argv)


class AskOwnerTests(unittest.TestCase):
    """Any managed agent raises one plain question on the board; the owner's answer comes back to its session; no CTO is needed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        self.agent = delivery_with_direction(self.root, "TASK-ASK")
        control.take_instructions(self.root, self.agent["session_id"])

    def test_the_question_is_open_deduplicated_and_limited(self):
        first = board.record_agent_question(self.root, self.agent["id"], "Should the forecast show Celsius or Fahrenheit?", ["Celsius", "Fahrenheit"])
        self.assertEqual((first["status"], first["task"]), ("open", "TASK-ASK"))
        again = board.record_agent_question(self.root, self.agent["id"], "Should the forecast show Celsius or Fahrenheit?", ["Celsius", "Fahrenheit"])
        self.assertEqual(again["id"], first["id"], "asking the same thing again is the same card")
        with self.assertRaisesRegex(ValueError, "already have a question open"):
            board.record_agent_question(self.root, self.agent["id"], "And how many days should it cover?")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "2 to 4 options"):
            board.record_agent_question(self.root, reviewer["id"], "A question long enough?", ["only one"])

    def test_the_answer_reaches_the_asking_agent_and_closes_the_card(self):
        entry = board.record_agent_question(self.root, self.agent["id"], "Should the forecast show Celsius or Fahrenheit?", ["Celsius", "Fahrenheit"])
        board.record_agent_question_answer(self.root, entry["id"], "", choice=1)
        self.assertEqual(board.snapshot(self.root)["agent_questions"][entry["id"]]["status"], "answered")
        sent = [item for item in control.take_instructions(self.root, self.agent["session_id"]) if item.get("source") == "owner-answer"]
        self.assertEqual(len(sent), 1)
        self.assertIn("[OWNER ANSWER]", sent[0]["text"])
        self.assertIn("Fahrenheit", sent[0]["text"])
        with self.assertRaisesRegex(ValueError, "not open any more"):
            board.record_agent_question_answer(self.root, entry["id"], "Celsius")
        board.record_agent_question(self.root, self.agent["id"], "And how many days should it cover?")   # the next one is allowed

    def test_the_operation_is_open_to_delivery_reviewer_and_cto_only_through_the_board_surface(self):
        from harness import board_surface
        for role in ("engineering", "qa", "cto"):
            self.assertIn(role, board_surface.AUTHORIZATION_MATRIX["ask-owner"])


class DirectiveTests(unittest.TestCase):
    def test_questions_go_through_the_board_in_every_directive_that_reaches_an_agent(self):
        agent = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8")
        self.assertIn("Questions for the owner go through the board, never the terminal", agent)
        self.assertIn("ask-owner --agent <your-id> --question", agent)
        self.assertIn("never ask one in your terminal or with the CLI's question widget", agent)
        self.assertNotIn("may ask\nclarifying questions in the visible CLI.", agent)


if __name__ == "__main__":
    unittest.main()
