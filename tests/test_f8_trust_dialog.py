# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""F-8: the harness never answers a menu the owner must answer.

Found by every end-to-end run of the plumbing program (2026-10-02): the first
time Claude Code opens a folder it does not trust - after an agent's `git
init`, so every new project - it asks "Is this a project you created or one you
trust?" with the default "No, exit". The supervisor typed the review assignment
and its Enter into that menu, which chose "No, exit" and closed the reviewer;
self-heal restarted it three times and gave up. The same was true of any menu.
"""
from __future__ import annotations

import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from harness import attention, board, control

ROOT = Path(__file__).resolve().parents[1]
# The reviewer's screen from the incident transcript (claude_reviewer-5b04297ea5.log), as rendered.
INCIDENT = ("Accessing workspace:\n/Users/owner/projects/greeter\nQuick safety check: Is this a project you "
            "created or one you trust? (Like your own code, a well-known open source project, or work from your team).\n"
            "Claude Code'll be able to read, edit, and execute files here.\nSecurity guide\n❯ No, exit\n"
            "  Yes, I trust this folder\nEnter to confirm · Esc to cancel")


class DetectionTests(unittest.TestCase):
    def test_the_incident_trust_menu_is_recognised_and_holds_input(self):
        for text in (INCIDENT, INCIDENT.replace(" ", "")):   # the transcript recorded it both ways
            reason = attention.detect(text)
            self.assertEqual(reason, attention.TRUST_REASON)
            self.assertTrue(attention.holds_harness_input(reason))

    def test_every_owner_menu_holds_input_but_an_agent_status_line_does_not(self):
        self.assertTrue(attention.holds_harness_input(attention.LOGIN_REASON))
        self.assertTrue(attention.holds_harness_input(attention.detect("Do you want to proceed?")))
        self.assertFalse(attention.holds_harness_input(attention.detect("OBJECTIVE STATUS: BLOCKED. USER ACTION: Needed")),
                         "an agent at its own prompt keeps receiving messages")
        self.assertFalse(attention.holds_harness_input(None))


class SupervisorTests(unittest.TestCase):
    """The real supervisor in a PTY, with a child that shows the trust menu and records what it is typed."""

    def _read_until(self, fd, marker, timeout):
        deadline, seen = time.monotonic() + timeout, b""
        while marker not in seen and time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    seen += os.read(fd, 65536)
                except OSError:
                    break
        return seen

    def run_child(self, screen: bytes) -> tuple[bytes, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_reviewer")
            agent = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            child = ("import os,tty\ntty.setraw(0)\n"
                     f"os.write(1,b'CHILD_READY\\r\\n'+{screen!r})\n"
                     "seen=b''\nwhile b'[SYSTEM CONTROL' not in seen: seen += os.read(0,4096)\n"
                     "os.write(1,b'GOT_CONTROL\\n')\n")
            process = subprocess.Popen([
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root),
                "--session-id", session["id"], "--agent-id", agent["id"], "--", "python3", "-c", child,
            ], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"CHILD_READY", 10)
                time.sleep(0.5)
                queued = control.enqueue_instruction(root, session["id"], "REVIEW REQUEST ROUTED: r-1. claim it.", "review-assignment")
                seen = self._read_until(master, b"GOT_CONTROL", 3.0)
                with control.locked_state(root) as state:
                    receipt = dict(state["instruction_receipts"][queued["id"]])
                return seen, receipt
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)

    def test_nothing_is_typed_into_the_trust_menu_and_the_message_stays_queued(self):
        seen, receipt = self.run_child(INCIDENT.encode("utf-8").replace(b"\n", b"\r\n"))
        self.assertNotIn(b"GOT_CONTROL", seen, "a harness message was typed into the trust menu")
        self.assertEqual(receipt["status"], "queued", "untaken: it is delivered once the owner answers")

    def test_an_agent_at_its_own_prompt_still_receives_the_message(self):
        seen, receipt = self.run_child(b"OBJECTIVE STATUS: BLOCKED\r\nUSER ACTION: Needed\r\n> ")
        self.assertIn(b"GOT_CONTROL", seen)


if __name__ == "__main__":
    unittest.main()
