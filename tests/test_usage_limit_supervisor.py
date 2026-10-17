# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The supervisor holds a terminal that shows its provider's usage-limit message and nudges it once (2026-10-16_quota-handoff).

End to end: the real supervisor around a fake CLI that prints the Claude CLI's own limit line (wording measured on Claude Code 2.1.293).
Decision tests: the nudge's send-time preconditions, with the machine inputs pinned (a fixed past announced time, a recorder in place
of the terminal write, no host clock dependence beyond "now is later than the announced time").
"""
from __future__ import annotations

import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from harness import attention, board, control, interactive_supervisor

ROOT = Path(__file__).resolve().parents[1]


def _wait_for(check, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


class HeldTerminalEndToEndTests(unittest.TestCase):
    def read_until(self, master, needle, timeout=20):
        deadline = time.monotonic() + timeout
        output = b""
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], .1)
            if master in readable:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if needle in output:
                return output
        self.fail(f"did not receive {needle!r}; got {output[-300:]!r}")

    def session_record(self, root, session_id):
        return next(item for item in control.snapshot(root)["sessions"] if item["id"] == session_id)

    def test_a_held_terminal_takes_nothing_records_the_incident_and_resumes_when_the_line_goes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_reviewer")
            agent = board.register(root, "qa", "REVIEW", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,select,time,tty\n"
                "tty.setraw(0)\n"
                "os.write(1,\"You've hit your session limit \\u00b7 resets 11:59pm (America/Chicago)\\r\\nLIMIT_ON_SCREEN\\r\\n\".encode())\n"
                "time.sleep(5)\n"
                "ready,_,_=select.select([0],[],[],0)\n"
                "os.write(1,b'GOT_WHILE_HELD' if ready else b'NOTHING_WHILE_HELD')\n"
                "for i in range(40): os.write(1,b'\\r\\nfiller %d'%i)\n"
                "got=b''\n"
                "while b'\\r' not in got: got+=os.read(0,4096)\n"
                "os.write(1,b'\\r\\nRECEIVED:'+got[:80])\n"
            )
            command = ["python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"],
                       "--agent-id", agent["id"], "--provider", "claude", "--", "python3", "-c", child]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self.read_until(master, b"LIMIT_ON_SCREEN")
                held = _wait_for(lambda: (r := self.session_record(root, session["id"])).get("limit_seen_at") and r)
                self.assertEqual(held["attention_reason"], attention.limit_reason("claude"))
                self.assertEqual(held["limit_kind"], "announced")
                self.assertTrue(datetime.fromisoformat(held["limit_resets_at"]) > datetime.now().astimezone())
                control.enqueue_instruction(root, session["id"], "Review the pending request.", "independent-review")
                output = self.read_until(master, b"NOTHING_WHILE_HELD", timeout=15)
                self.assertNotIn(b"GOT_WHILE_HELD", output)
                self.assertNotIn(b"Review the pending request", output)
                released = _wait_for(lambda: (r := self.session_record(root, session["id"])).get("attention_reason") is None and r, timeout=15)
                self.assertTrue(released.get("last_limit_seen_at"))
                self.assertTrue(released.get("limit_cleared_at"))
                self.assertNotIn("limit_seen_at", released)
                received = self.read_until(master, b"RECEIVED:", timeout=15)
                self.assertIn(b"Review the pending request", received)
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                os.close(master)


class NudgeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session = control.create(self.root, "claude_reviewer")
        # The announced time is 3 hours in the past on the real clock, so the nudge is due now.
        self.past = datetime.now().astimezone() - timedelta(hours=3)
        line = f"You've hit your session limit · resets {(self.past + timedelta(minutes=1)).strftime('%I:%M%p').lstrip('0').lower()}\r\n"
        self.watch = attention.PromptWatch(height=40, provider="claude", clock=lambda: self.past)
        self.change = self.watch.feed(line.encode())
        self.incident = {key: value for key, value in self.watch.incident.items() if not key.startswith("_")}
        control.record_attention(self.root, self.session["id"], self.change[1], limit=self.incident)
        self.sent: list = []
        self.notes: list = []
        self.attempts: dict = {}

    def nudge(self, **overrides):
        arguments = dict(attempts=self.attempts, max_attempts=2, transcript=type("T", (), {"note": lambda _s, text: self.notes.append(text)})(),
                         fd=-1, typed=bytearray(), last_owner_key_at=float("-inf"), owner_undecided=False, ready=True)
        arguments.update(overrides)
        with patch.object(interactive_supervisor, "_submit_controller_message", lambda fd, source, text: self.sent.append((source, text))):
            return interactive_supervisor._maybe_nudge_limit(self.root, self.session["id"], self.watch, **arguments)

    def record(self):
        return next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == self.session["id"])

    def test_the_nudge_goes_once_after_the_announced_time_and_is_recorded_only_when_written(self):
        self.assertTrue(self.nudge())
        self.assertEqual(self.sent, [("harness", attention.REVIEWER_NUDGE_TEXT)])
        self.assertTrue(self.record()["limit_nudged_at"])
        self.assertFalse(self.nudge())          # once per incident
        self.assertEqual(len(self.sent), 1)

    def test_a_delivery_agent_gets_the_plain_continue_line(self):
        delivery = control.create(self.root, "codex_delivery")
        control.record_attention(self.root, delivery["id"], attention.limit_reason("codex"), limit=self.incident)
        with patch.object(interactive_supervisor, "_submit_controller_message", lambda fd, source, text: self.sent.append(text)):
            interactive_supervisor._maybe_nudge_limit(self.root, delivery["id"], self.watch, self.attempts, 2, type("T", (), {"note": lambda _s, t: None})(),
                                                      -1, bytearray(), float("-inf"), False, True)
        self.assertEqual(self.sent, [attention.NUDGE_TEXT])

    def test_not_before_the_announced_time_plus_the_wait(self):
        future = datetime.now().astimezone() + timedelta(hours=2)
        watch = attention.PromptWatch(height=40, provider="claude", clock=lambda: future)
        watch.feed(f"You've hit your session limit · resets {(future + timedelta(minutes=1)).strftime('%I:%M%p').lstrip('0').lower()}\r\n".encode())
        self.watch = watch
        self.assertFalse(self.nudge())
        self.assertEqual(self.sent, [])

    def test_a_menu_on_screen_blocks_it_with_a_plain_note_and_does_not_use_up_the_attempt(self):
        self.watch.screen.feed(b"\r\nDo you want to proceed?\r\n")
        self.assertFalse(self.nudge())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.record()["limit_nudge_note"], "not sent: a menu is open")
        self.assertNotIn(str(self.incident["seen_at"]), self.attempts)

    def test_the_owner_typing_blocks_it_and_is_not_counted(self):
        self.assertFalse(self.nudge(owner_undecided=True))
        self.assertFalse(self.nudge(typed=bytearray(b"half a sentence"), last_owner_key_at=time.monotonic()))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.record()["limit_nudge_note"], "not sent: the owner was typing")
        self.assertEqual(self.attempts, {})

    def test_a_failed_write_is_noted_not_recorded_as_sent_and_retried_once(self):
        def failing(fd, source, text):
            raise OSError("pty closed")
        with patch.object(interactive_supervisor, "_submit_controller_message", failing):
            for _ in range(4):
                interactive_supervisor._maybe_nudge_limit(self.root, self.session["id"], self.watch, self.attempts, 2,
                                                          type("T", (), {"note": lambda _s, t: None})(), -1, bytearray(), float("-inf"), False, True)
        self.assertNotIn("limit_nudged_at", self.record())
        self.assertEqual(self.attempts[str(self.incident["seen_at"])], 2)         # one send and one retry, then no more
        self.assertIn("the write failed", self.record()["limit_nudge_note"])

    def test_a_paused_project_or_a_session_that_ended_or_lost_the_line_gets_nothing(self):
        with patch.object(board, "pause_state", return_value={"status": "paused"}):
            self.assertFalse(self.nudge())
        self.assertEqual(self.sent, [])
        for index in range(30):
            self.watch.feed(f"agent output {index}\r\n".encode())
        self.assertFalse(self.nudge())          # the line left the screen: nothing to nudge
        self.assertEqual(self.sent, [])

    def test_nothing_is_sent_while_a_delivery_is_in_flight(self):
        self.assertFalse(self.nudge(ready=False))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
