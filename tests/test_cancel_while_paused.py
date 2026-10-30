"""The owner's Cancel task works on a paused project (2026-10-28: "i paused the project no way anymore to cancel the task").

Proved through the real HTTP path with a real pause, not an enabled button: a confirmed cancel is accepted, the task is
cancelled, the project stays paused, a later resume does not bring the cancelled agent back, and every other change stays
read-only while paused.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, contract, control
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements

TASK = "TASK-PAUSED-CANCEL"


class CancelWhilePausedTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        session = control.create(self.root, "codex_delivery")
        self.agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        objective = f"OWNER DIRECTION — {TASK}"
        board.record_owner_direction(self.root, session["id"], objective)
        board.begin_task(self.root, self.agent["id"], TASK)
        agreed_requirements(self.root, self.agent["id"], f"Final agreed requirements for {TASK}: preserve the requested delivery.")
        contract.create_contract(self.root, TASK, objective, ["delivery"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def pause(self):
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        self.assertEqual(board.pause_state(self.root)["status"], "paused")

    def post(self, path: str, body: dict | None = None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                return reply.status, json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_a_confirmed_cancel_succeeds_and_the_project_stays_paused(self):
        self.pause()
        status, reply = self.post(f"/api/tasks/{TASK}/cancel")
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["cancelled_tasks"], [TASK])
        state = board.snapshot(self.root)
        self.assertIn(TASK, state["cancelled_tasks"])
        self.assertEqual(state["agents"][self.agent["id"]]["status"], "cancelled")
        self.assertEqual(board.pause_state(self.root)["status"], "paused")
        self.assertEqual(board_viewer.dashboard_payload(self.root)["live_tasks"], [])

    def test_a_second_cancel_is_refused_in_plain_words_and_nothing_resumes(self):
        self.pause()
        self.assertEqual(self.post(f"/api/tasks/{TASK}/cancel")[0], 200)
        status, reply = self.post(f"/api/tasks/{TASK}/cancel")
        self.assertEqual(status, 400)
        self.assertIn("already cancelled", reply["error"])
        self.assertEqual(board.pause_state(self.root)["status"], "paused")

    def test_the_paused_pointers_of_the_cancelled_task_are_dropped_so_a_resume_cannot_bring_it_back(self):
        self.pause()
        self.assertIn(self.agent["id"], board.pause_state(self.root)["agents"])
        self.assertEqual(self.post(f"/api/tasks/{TASK}/cancel")[0], 200)
        self.assertNotIn(self.agent["id"], board.pause_state(self.root)["agents"])
        resume = board.begin_project_resume(self.root)
        board.finish_project_resume(self.root, resume["resume_id"])
        state = board.snapshot(self.root)
        self.assertEqual(state["project_pause"]["status"], "active")
        self.assertEqual(state["agents"][self.agent["id"]]["status"], "cancelled")
        self.assertFalse(state["agents"][self.agent["id"]]["write_authority"])

    def test_every_other_change_stays_read_only_while_paused(self):
        self.pause()
        status, reply = self.post(f"/api/agents/{self.agent['id']}/owner-message", {"message_type": "direction", "text": "more"})
        self.assertEqual(status, 409)
        self.assertIn("paused and read-only", reply["error"])
        status, _ = self.post(f"/api/agents/{self.agent['id']}/recover")
        self.assertEqual(status, 409)
        self.assertNotIn(TASK, board.snapshot(self.root).get("cancelled_tasks", {}))

    def test_cancel_is_still_refused_while_the_project_is_resuming(self):
        self.pause()
        board.begin_project_resume(self.root)
        self.assertEqual(board.pause_state(self.root)["status"], "resuming")
        status, reply = self.post(f"/api/tasks/{TASK}/cancel")
        self.assertEqual(status, 409)
        self.assertNotIn(TASK, board.snapshot(self.root).get("cancelled_tasks", {}))


if __name__ == "__main__":
    unittest.main()
