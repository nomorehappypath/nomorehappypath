# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The headless review driver runs the owner's steps through the app's own API.

Owner's order, 2026-10-03 21:10: reviews run with no person and no browser
clicking. The driver (scripts/review_driver.py) is exercised here against the
real manager and a real opened project on a scratch home - create, open, the
rendered page, Go ahead, the "waiting for a person" guard, and close. Agents
are not launched (no windows); their steps are seeded on the board as the
agents would leave them.
"""
from __future__ import annotations

import importlib.util
import io
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, control, project_manager, project_registry as registry
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_driver", ROOT / "scripts" / "review_driver.py")
review_driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_driver)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class DriverTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "manager"
        self.parent = base / "projects"
        self.parent.mkdir()
        self.manager = project_manager.ProjectManager(self.home, board_port=free_port())
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(self.manager))
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        self.manager.manager_url = origin + "/"
        self.manager.public_board_url = origin + project_manager.PROJECT_ROUTE
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.manager._stop_worker)
        self.driver = review_driver.Driver(origin, base / "evidence", self.home)
        review_driver.ATTENTION_GRACE_SECONDS = 0

    def quietly(self, step, *args):
        with redirect_stdout(io.StringIO()):
            return step(*args)

    def context(self, entry: dict) -> ProjectContext:
        return registry.context_for_entry(registry._find(registry.load(self.home), entry["id"]))

    def test_the_owner_path_runs_through_the_api_with_no_browser(self):
        entry = self.quietly(self.driver.create, str(self.parent), "driver-p1", "driver test")
        self.assertTrue(Path(entry["code_root"]).is_dir())
        self.quietly(self.driver.open, entry["id"])
        self.assertTrue((self.driver.evidence / "open.json").is_file())

        # The Delivery agent filed its requirements proposal, as it would on the board.
        context = self.context(entry)
        session = control.create(context, "codex_delivery")
        agent = board.register(context, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        with board.locked_state(context) as state:
            state["agents"][agent["id"]]["task"] = "salute"
            state.setdefault("requirement_proposals", {})["salute"] = {
                "status": "awaiting_owner", "agent_id": agent["id"], "version": 1, "text": "Create salute.py."}
        decided = self.quietly(self.driver.go_ahead, "")
        self.assertEqual(decided["task"], "salute")
        self.assertEqual(board.snapshot(context)["requirement_proposals"]["salute"]["status"], "accepted")
        routed = [entry["text"] for entry in control.take_instructions(context, session["id"])]
        self.assertTrue(any("GO AHEAD" in text for text in routed), "the agent is told, as from the page's button")

        # Nothing is accepted yet: the page check says so, with what the page showed.
        try:
            with self.assertRaises(review_driver.StepFailed) as raised:
                self.quietly(self.driver.rendered, "salute")
        except (OSError, RuntimeError) as error:
            self.skipTest(f"no headless browser here: {error}")
        self.assertIn("accepted in task history []", str(raised.exception))
        self.assertIn("driver-p1", (self.driver.evidence / "rendered-visible.txt").read_text(), "the rendered page, read with no click")
        self.assertTrue((self.driver.evidence / "rendered.png").stat().st_size > 0)

        self.assertEqual(self.quietly(self.driver._clean)["left"], [], "nothing of this app runs; no runtime folder")
        self.quietly(self.driver.close, entry["id"])

    def test_an_agent_waiting_for_a_person_fails_the_step_by_name(self):
        entry = self.quietly(self.driver.create, str(self.parent), "driver-p2", "driver test")
        self.quietly(self.driver.open, entry["id"])
        context = self.context(entry)
        session = control.create(context, "claude_reviewer")
        with control.locked_state(context) as state:
            state["sessions"][session["id"]].update(status="running", attention_reason="is asking whether you trust this project folder",
                                                     attention_since=control.now())
        with self.assertRaises(review_driver.StepFailed) as raised:
            self.quietly(self.driver.wait, "release-ready", lambda: None, 30, 0.2)
        self.assertIn(session["id"], str(raised.exception))
        self.assertIn("waiting for a person", str(raised.exception))
        self.assertTrue((self.driver.evidence / "release-ready-blocked.json").is_file())


if __name__ == "__main__":
    unittest.main()
