# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Gemini conversation identity, resume and the readable view (spec M-13), on real transcript lines."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import conversation, control

# Lines as the Antigravity CLI 1.3.1 wrote them (a tool-using turn, recorded 2026-10-07).
TRANSCRIPT = [
    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-10-08T02:41:50Z",
     "content": "<USER_REQUEST>\nRun the shell command: echo hi-from-tool.\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nlocal time\n</ADDITIONAL_METADATA>"},
    {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-10-08T02:41:51Z",
     "tool_calls": [{"name": "run_command", "args": {"CommandLine": "\"echo hi-from-tool\"", "Cwd": "\"/p\""}}]},
    {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-10-08T02:41:53Z",
     "content": "The command exited with code 0.\nOutput:\nhi-from-tool\r\n"},
    {"step_index": 3, "source": "SYSTEM_SDK", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-10-08T02:41:54Z",
     "content": "<USER_REQUEST>\n[SYSTEM CONTROL — cto] Review r1 is ready. (harness delivery d1)\n</USER_REQUEST>"},
    {"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-10-08T02:41:55Z",
     "input_tokens": 11965, "content": "done", "thinking": "private reasoning"},
]


def write_conversation(home: Path, conversation_id: str, first_text: str = "", mtime: float | None = None) -> Path:
    logs = home / ".gemini" / "antigravity-cli" / "brain" / conversation_id / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    lines = [dict(TRANSCRIPT[0], content=f"<USER_REQUEST>\n{first_text or 'hello'}\n</USER_REQUEST>")] + TRANSCRIPT[1:]
    path = logs / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


class GeminiConversationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_readable_view_has_owner_agent_tool_and_harness_lines_and_no_reasoning(self):
        path = write_conversation(self.home, "c1")
        entries = conversation.read_gemini_conversation(path)
        self.assertEqual([(e["who"], e["kind"]) for e in entries],
                         [("owner", "text"), ("agent", "tool"), ("owner", "text"), ("agent", "text")])
        self.assertEqual(entries[1]["text"], "run_command: echo hi-from-tool")
        self.assertIn("[SYSTEM CONTROL — cto]", entries[2]["text"])
        self.assertNotIn("<USER_REQUEST>", " ".join(e["text"] for e in entries))
        self.assertNotIn("private reasoning", json.dumps(entries))
        self.assertEqual(conversation.read_vendor_conversation("gemini", "c1"), entries)

    def test_a_missing_or_hostile_conversation_id_has_no_path(self):
        self.assertIsNone(conversation.gemini_transcript_path("nope"))
        self.assertIsNone(conversation.gemini_transcript_path("../../etc/passwd"))
        self.assertIsNone(conversation.read_vendor_conversation("gemini", "nope"))

    def test_the_conversation_is_found_by_this_launchs_marker_not_by_being_new(self):
        write_conversation(self.home, "owners-own", "my own agy session, no marker")
        write_conversation(self.home, "other-role", "kick off HARNESS_MANAGED_SESSION=claude_reviewer-aaa#1")
        write_conversation(self.home, "ours", "kick off HARNESS_MANAGED_SESSION=codex_delivery-bbb#2")
        since = time.time() - 60
        self.assertEqual(conversation.discover_gemini_session_id(since, "HARNESS_MANAGED_SESSION=codex_delivery-bbb#2"), "ours")
        self.assertIsNone(conversation.discover_gemini_session_id(since, "HARNESS_MANAGED_SESSION=codex_delivery-bbb#3"))
        old = time.time() - 3600
        write_conversation(self.home, "stale", "HARNESS_MANAGED_SESSION=x#1", mtime=old)
        self.assertIsNone(conversation.discover_gemini_session_id(since, "HARNESS_MANAGED_SESSION=x#1"))
        with self.assertRaises(ValueError):
            conversation.discover_gemini_session_id(since, "")

    def test_plan_resumes_a_stored_conversation_and_starts_fresh_with_a_marker_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            from harness.project_context import project_context
            root = project_context(Path(tmp))
            session = {"id": "codex_delivery-abc", "kind": "codex_delivery", "provider": "gemini", "status": "launching"}
            with mock.patch.object(control, "cli_session", return_value={"cli_session_id": "c1", "cli_launches": 1}), \
                    mock.patch.object(control, "clear_cli_session") as cleared:
                write_conversation(self.home, "c1")
                plan = conversation.plan_cli_launch(root, session["id"], "gemini")
                self.assertEqual((plan["mode"], plan["cli_session_id"]), ("resume", "c1"))
                self.assertIn("Gemini conversation c1", plan["reason"])
                cleared.assert_not_called()
            with mock.patch.object(control, "cli_session", return_value={"cli_session_id": "gone", "cli_launches": 1}), \
                    mock.patch.object(control, "clear_cli_session") as cleared:
                plan = conversation.plan_cli_launch(root, session["id"], "gemini")
                self.assertEqual(plan["mode"], "fresh")
                self.assertTrue(plan["codex_marker"].endswith("#2"))
                cleared.assert_called_once()

    def test_the_supervisor_discovers_the_gemini_id_only_while_none_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            from harness.project_context import project_context
            root = project_context(Path(tmp))
            with mock.patch.object(control, "cli_session", return_value={"cli_launches": 1}):
                self.assertTrue(conversation.codex_discovery_state(root, "s-1", "gemini")[0])
                self.assertFalse(conversation.codex_discovery_state(root, "s-1", "claude")[0])
            with mock.patch.object(control, "cli_session", return_value={"cli_launches": 1, "cli_session_id": "c1"}):
                self.assertFalse(conversation.codex_discovery_state(root, "s-1", "gemini")[0])


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()
