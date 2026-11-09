# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stop/Resume truth (task 2026-11-08_accept-after-repair, R4): a builder the owner stopped stays stopped through Resume.

2026-11-08 17:00 on the owner's clone project: he stopped the Finish Support builder at 03:45, paused, resumed. Resume
restored that builder as "the saved Delivery owner" (the task had a failed review), relaunched its window and set it
active, although the board still held it stopped with no write authority. Because a kept task is handed to a new builder
only when no active builder holds it, the builder he then started parked itself "waiting for instructions"."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board

TASK = "finish-support"


class StoppedBuilderStaysStoppedThroughResume(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", session_id="cto-session")
        self.builder = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="old-builder-session")
        stopped_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with board.locked_state(self.root, operation="test") as state:
            state.setdefault("task_owner_directions", {})[TASK] = "Finish Support: release the work that is already done."
            state["agents"][self.builder["id"]].update({
                "task": TASK, "active": False, "write_authority": False, "status": "stopped", "liveness": "offline",
                "liveness_note": "terminal stopped by owner", "stopped_by_owner_at": stopped_at,
            })
            # the newest final review of the task failed: the shape that made Resume want a Delivery owner back
            state.setdefault("qa_requests", {})["review-final-05"] = {
                "id": "review-final-05", "task": TASK, "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "cycle": 5, "status": "failed", "developer_id": self.builder["id"], "requested_at": stopped_at,
                "subtask": "", "chunk": "", "claimed_by": "", "review_wait_started_at": "",
            }
            state["project_pause"] = {"status": "paused", "pause_id": "pause-1", "agents": {}, "reviews": {}}

    def agent(self):
        return board.snapshot(self.root)["agents"][self.builder["id"]]

    def test_resume_does_not_stage_relaunch_or_activate_the_stopped_builder(self):
        pause = board.begin_project_resume(self.root)
        staged = board.stage_required_delivery_resumes(self.root, pause["resume_id"])
        self.assertEqual(staged["staged"], [], "a builder the owner stopped is never the saved Delivery owner")
        self.assertNotIn(self.builder["id"], board.snapshot(self.root)["project_pause"].get("agents", {}))
        board.finish_project_resume(self.root, pause["resume_id"])
        agent = self.agent()
        self.assertFalse(agent["active"])
        self.assertEqual(agent["status"], "stopped")
        self.assertFalse(agent.get("write_authority"))
        # nobody is both stopped and active, now or after the resume
        for a in board.snapshot(self.root)["agents"].values():
            self.assertFalse(a.get("status") == "stopped" and a.get("active"), a["id"])

    def test_a_new_builder_takes_the_kept_task_at_start(self):
        pause = board.begin_project_resume(self.root)
        board.stage_required_delivery_resumes(self.root, pause["resume_id"])
        board.finish_project_resume(self.root, pause["resume_id"])
        source, task = board._stopped_task_to_resume(board.snapshot(self.root), "")
        self.assertEqual((source, task), (self.builder["id"], TASK), "the kept task is free for the next builder")
        replacement = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="new-builder-session")
        self.assertEqual(replacement["task"], TASK, "the builder the owner starts holds the task at once")

    def test_a_relaunched_terminal_of_the_stopped_builder_does_not_make_it_active(self):
        pause = board.begin_project_resume(self.root)
        with board.locked_state(self.root, allow_paused=True, operation="test") as state:
            # the shape of the defect: the stopped builder's session was saved for relaunch anyway
            state["project_pause"]["agents"][self.builder["id"]] = {"session_id": "old-builder-session", "task": TASK, "status": "stopped"}
        same = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="old-builder-session")
        self.assertEqual(same["id"], self.builder["id"])
        self.assertFalse(same["active"], "the owner's Stop outranks the relaunch")
        kinds = [e["kind"] for e in board.snapshot(self.root)["events"] if e.get("agent_id") == self.builder["id"]]
        self.assertIn("resume_skipped_stopped_agent", kinds)
        self.assertNotIn("agent_resume_session_attached", kinds)


if __name__ == "__main__":
    unittest.main()
