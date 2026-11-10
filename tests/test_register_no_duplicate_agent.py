"""One terminal, one board agent (2026-11-09 incident).

A finished Delivery Agent's terminal kept calling `register` and got nine new empty agents on
the same session. After Pause/Resume the real agent came back too, ten agents shared one
session, and the board could no longer tell which one the terminal was: every command was
refused with "the authenticated session has no board agent", so the owner's Not accepted
never reached the builder.
"""
import tempfile
import unittest
from pathlib import Path

from harness import board


class RegisterNoDuplicateAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _agents_on(self, session_id):
        return [a for a in board.snapshot(self.root)["agents"].values() if a.get("session_id") == session_id]

    def test_finished_builder_does_not_register_again(self):
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-1")
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]].update({"task": "some-task", "status": "done", "active": False})
            state.setdefault("releases", {})["some-task"] = {"status": "VISUAL_TEST_REQUIRED", "cto_id": "cto-1", "recorded_at": "2026-11-09T00:00:00+00:00"}
        for _ in range(3):
            with self.assertRaisesRegex(ValueError, "finished its task"):
                board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-1")
        self.assertEqual([a["id"] for a in self._agents_on("term-1")], [dev["id"]])

    def test_repeat_register_reattaches_the_active_agent(self):
        old = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-2")
        with board.locked_state(self.root) as state:
            state["agents"][old["id"]].update({"task": "older-task", "status": "offline", "active": False})
        first = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-2")
        again = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-2")
        self.assertEqual(again["id"], first["id"])
        active = [a for a in self._agents_on("term-2") if a.get("active")]
        self.assertEqual([a["id"] for a in active], [first["id"]])

    def test_second_task_on_the_same_terminal_does_not_register_a_duplicate(self):
        """Codex review R1-1: task 1 accepted, task 2 finished and waiting for the owner."""
        first = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-3")
        with board.locked_state(self.root) as state:
            state["agents"][first["id"]].update({"task": "task-one", "status": "done", "active": False})
        second = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-3")
        with board.locked_state(self.root) as state:
            state["agents"][second["id"]].update({"task": "task-two", "status": "done", "active": False})
            state.setdefault("releases", {})["task-two"] = {"status": "VISUAL_TEST_REQUIRED", "cto_id": "cto-1", "recorded_at": "2026-11-09T00:00:00+00:00"}
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "finished its task"):
                board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="term-3")
        self.assertEqual(sorted(a["id"] for a in self._agents_on("term-3")), sorted([first["id"], second["id"]]))


if __name__ == "__main__":
    unittest.main()
