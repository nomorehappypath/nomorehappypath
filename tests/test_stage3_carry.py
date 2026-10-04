# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stage 3 review r2 audit: when a session ends, no message it had not delivered is lost.

A relaunch is a new session continuing the ended one. Every message the
ended session had not DELIVERED - still queued, taken but unsent, or posted
but not yet seen - moves to it under the SAME id, marked `carried_from`, so
the successor checks the conversation before sending. Delivered, withdrawn and
discarded messages never move.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from harness import control


class CarryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.first = control.create(self.root, "claude_reviewer")
        control.attach(self.root, self.first["id"], os.getpid())
        control.record_cli_session(self.root, self.first["id"], "cli-conversation-1", "claude")

    def end_first(self):
        with control.locked_state(self.root) as state:
            state["sessions"][self.first["id"]].update(status="exited", ended_at=control.now(), pid=None)

    def test_every_undelivered_message_moves_once_and_delivered_ones_never(self):
        queued = control.enqueue_instruction(self.root, self.first["id"], "Still queued.", "a")
        taken = control.enqueue_instruction(self.root, self.first["id"], "Taken, never sent.", "b")
        posted = control.enqueue_instruction(self.root, self.first["id"], "Posted, not seen.", "c")
        done = control.enqueue_instruction(self.root, self.first["id"], "Delivered.", "d")
        withdrawn = control.enqueue_instruction(self.root, self.first["id"], "Withdrawn.", "e")
        control.withdraw_instruction(self.root, withdrawn["id"])
        entries = control.take_instructions(self.root, self.first["id"])     # takes queued, taken, posted, done
        by_id = {entry["id"]: entry for entry in entries}
        control.return_instructions(self.root, self.first["id"], [by_id[queued["id"]]])   # back to queued
        control.mark_posted(self.root, self.first["id"], posted["id"], by_id[posted["id"]])
        control.acknowledge_instruction(self.root, self.first["id"], done["id"])
        self.end_first()
        successor = control.create(self.root, "claude_reviewer")
        self.assertEqual(successor["continues_session"], self.first["id"])
        inbox = control.take_instructions(self.root, successor["id"])
        self.assertEqual(sorted(entry["id"] for entry in inbox), sorted([queued["id"], taken["id"], posted["id"]]))
        self.assertEqual({entry["id"]: entry["text"] for entry in inbox}[posted["id"]], "Posted, not seen.")
        self.assertTrue(all(entry["carried_from"] == self.first["id"] for entry in inbox))
        for entry in inbox:
            self.assertEqual(control.instruction_receipt(self.root, entry["id"])["session_id"], successor["id"])
        self.assertEqual(control.instruction_receipt(self.root, done["id"])["status"], "delivered")
        self.assertEqual(control.instruction_receipt(self.root, withdrawn["id"])["status"], "withdrawn")
        third = control.create(self.root, "claude_reviewer")
        self.assertEqual(control.take_instructions(self.root, third["id"]), [], "never carried twice")


if __name__ == "__main__":
    unittest.main()
