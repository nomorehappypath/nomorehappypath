# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""After the owner's Accept, the task's running Delivery terminal stops, and says so.

Owner, 2026-10-03: "it should say task accepted, terminating agent or
something". After Accept, the board's cold-state cleanup moved the accepted
task's Delivery agent away while its terminal was still running, so Mission
Control showed that terminal as "Waiting to attach … Terminal is starting;
board agent registration is pending" (pre-existing since 453934b).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import board, control

TASK = "zip-task"


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # The Delivery terminal: a real process the stop path can signal.
        self.terminal = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self._end_terminal)
        self.session = control.create(self.root, "codex_delivery")
        control.attach(self.root, self.session["id"], self.terminal.pid)
        agent = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI",
                               session_id=self.session["id"])
        self.agent_id = agent["id"]
        with board.locked_state(self.root) as state:      # it worked the task; development complete
            state["agents"][self.agent_id].update(task=TASK, active=False, status="complete")

    def _end_terminal(self):
        if self.terminal.poll() is None:
            self.terminal.kill()
        self.terminal.wait(timeout=10)

    def accept(self):
        with board.locked_state(self.root) as state:
            state.setdefault("release_decisions", {})[TASK] = {"decision": "accepted", "recorded_at": board.now()}

    def later_board_write(self):
        """Any board write that changes something runs the cold-state cleanup."""
        with board.locked_state(self.root) as state:
            board._event(state, "test_marker", None, {"task": TASK, "message": "a later board write"})

    def card_agents(self) -> list[str]:
        """The board agents Mission Control can show for this terminal."""
        return [agent["id"] for agent in board.snapshot(self.root).get("agents", {}).values()
                if agent.get("session_id") == self.session["id"]]


class ColdStateTests(Fixture):
    def test_an_accepted_tasks_agent_stays_while_its_terminal_runs(self):
        self.accept()
        self.later_board_write()
        self.assertEqual(self.card_agents(), [self.agent_id],
                         "a running terminal keeps its agent: its card never falls back to 'starting'")

    def test_once_the_terminal_has_ended_the_agent_is_archived_as_before(self):
        self.accept()
        self._end_terminal()
        self.later_board_write()
        self.assertEqual(self.card_agents(), [])


class AcceptStopsTheTerminalTests(Fixture):
    def run_acceptance(self, outcome):
        self.accept()
        with mock.patch.object(board, "accept_owner_release", side_effect=outcome):
            return board._run_owner_acceptance(self.root, TASK, {})

    def test_a_completed_accept_stops_the_running_delivery_terminal_and_says_why(self):
        response = self.run_acceptance(lambda root, task: {"status": "accepted"})
        self.assertEqual(response["delivery_stopped"], [self.session["id"]])
        agent = board.snapshot(self.root)["agents"][self.agent_id]
        self.assertEqual(agent["status_note"], "Task accepted, Dev agent stopped.")
        kinds = [event["kind"] for event in board.snapshot(self.root)["events"]]
        self.assertIn("delivery_stopped_after_acceptance", kinds)
        self.terminal.wait(timeout=10)                   # the terminal received the stop
        self.assertIsNotNone(self.terminal.poll())

    def test_a_refused_accept_stops_nothing(self):
        def refuse(root, task):
            raise ValueError("main moved")
        response = self.run_acceptance(refuse)
        self.assertNotIn("delivery_stopped", response)
        time.sleep(0.3)
        self.assertIsNone(self.terminal.poll(), "the terminal keeps running: the work is not in main yet")

    def test_a_failure_to_stop_never_fails_the_accept(self):
        with mock.patch.object(control, "stop", side_effect=OSError("signal failed")):
            response = self.run_acceptance(lambda root, task: {"status": "accepted"})
        self.assertEqual(response["git_acceptance"], {"status": "accepted"})


if __name__ == "__main__":
    unittest.main()


class RenderedCardTests(Fixture):
    """Real board page, real headless Chrome: the card of an accepted task's running terminal."""

    def setUp(self):
        from tests.environment_support import require_loopback
        require_loopback()
        super().setUp()

    PROBE = r"""<script>(async()=>{for(let i=0;i<200&&!document.querySelector('#agents .agent-row');i++)await new Promise(r=>setTimeout(r,100));
    const rows=Array.from(document.querySelectorAll('#agents .agent-row')).map(r=>r.textContent.replace(/\s+/g,' ').trim());
    await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows})});})();</script>"""

    def rendered_rows(self) -> list[str]:
        import threading
        from http.server import ThreadingHTTPServer
        from harness import board_viewer, browser_acceptance
        from tests.environment_support import require_loopback
        from tests.test_branding_rendered import probe_proxy
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="accepted card", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="accepted-card", chat_action_token="t"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, self.PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]["rows"]

    def test_the_card_says_task_accepted_never_starting_or_clarifying(self):
        with board.locked_state(self.root) as state:
            state.setdefault("git_acceptances", {})[TASK] = {"accepted_at": board.now(), "commit": "0" * 40}
        self.accept()
        rows = self.rendered_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("TASK ACCEPTED", rows[0])
        self.assertIn("Task accepted. The work is in main; this Delivery terminal is closing.", rows[0])
        for wrong in ("Terminal is starting", "Waiting to attach", "clarifying", "Send clarification"):
            self.assertNotIn(wrong, rows[0])
