# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome) of item 24: failed attempts in plain words on Mission Control.

Run:  PYTHONPATH=. python3 -m unittest tests.test_failed_attempts_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control, project_memory
from tests import test_board as _board_tests
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  document.getElementById('tab-advanced-btn')?.click();   // the agent terminals live in the Advanced tab since the Mission Control redesign
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  for (let i = 0; i < 150 && !document.querySelector('#agents .agent-row .attempts'); i++) await wait(100);
  await wait(400);
  const text = node => node ? node.innerText : '';
  const card = document.querySelector('#tasks .task .attempts');
  const reading = {
    progress: text(card),
    progressHasDetails: Boolean(card && card.querySelector('details > summary')),
    update: text(document.querySelector('#tasks .delivery-brief')),
    rows: Array.from(document.querySelectorAll('#agents .agent-row')).map(row => ({role: row.querySelector('.agent-meta')?.textContent || '', text: row.innerText})),
  };
  // keyboard: the summary takes focus and the browser toggles it with Enter/Space (click is what those keys do); it survives a refresh
  const summary = document.querySelector('#agents .agent-row .attempts details > summary');
  summary.focus();
  reading.focused = document.activeElement === summary;
  summary.click();
  reading.openAfterToggle = summary.parentElement.open;
  while (refreshing) await wait(20);
  await refresh();
  const again = document.querySelector('#agents .agent-row .attempts details > summary');
  reading.stillOpen = again.parentElement.open;
  reading.stillFocused = document.activeElement === again;
  reading.techVisible = again.parentElement.querySelector('.attempt-tech').getBoundingClientRect().height > 0;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""

DECODE = "'utf-8' codec can't decode byte 0xf8 in position 493: invalid start byte"


class RenderedFailedAttemptsTests(unittest.TestCase):
    delivery = _board_tests.BoardTests.delivery

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project_memory.initialize(self.root, project_name="Failed attempts proof", description="Facts.")
        session = control.create(self.root, "claude_reviewer")
        self.qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])

    def test_the_owner_sees_the_failed_attempts_on_the_progress_card_and_both_agent_cards(self):
        dev = self.delivery("AUSTIN")
        with board.locked_state(self.root) as state:
            state["task_briefs"]["AUSTIN"] = {"agent_id": dev["id"], "plan": "Build it.", "update": "Writing the program now.",
                                              "updated_at": "2026-10-22T14:38:24+00:00"}
            state["qa_requests"]["req-1"] = {"id": "req-1", "task": "AUSTIN", "phase": "final_acceptance", "chunk": "final", "subtask": "",
                                             "status": "claimed", "claimed_by": self.qa["id"], "cycle": 1, "developer_id": dev["id"], "review_wait_started_at": "", "requested_at": "2026-10-22T14:30:00+00:00"}
            state["delivery_attempt_failures"] = [
                {"id": "review-AUSTIN-final-01", "task": "AUSTIN", "phase": "final_acceptance", "chunk": "final", "subtask": "",
                 "result": "failed", "result_summary": DECODE, "completed_at": f"2026-10-22T14:4{n}:42+00:00", "delivery_state": "failed",
                 "status": "cancelled", "route_state": "delivery_failed_cancelled"} for n in range(3)]
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Failed attempts proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="failed-attempts", chat_action_token="owner-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1300, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe; a blank render is not a pass")
        reading = sink["value"]
        shown = json.dumps(reading, indent=1)[:3000]
        self.assertIn("Test failed 3 times", reading["progress"], shown)
        self.assertIn("could not read what the test printed", reading["progress"], shown)
        self.assertTrue(reading["progressHasDetails"], shown)
        self.assertIn("older than the latest failed test", reading["update"], shown)
        texts = [row["text"] for row in reading["rows"]]
        self.assertTrue(any("Test failed 3 times" in text and "Delivery" in text for text in texts), shown)
        reviewer = [text for text in texts if "Independent Reviewer" in text]
        self.assertTrue(reviewer and "No test by this reviewer has failed" in reviewer[0] and "waits on Delivery's own test run" in reviewer[0]
                        and "Test failed 3 times" in reviewer[0], shown)
        self.assertTrue(reading["focused"] and reading["openAfterToggle"], shown)
        self.assertTrue(reading["stillOpen"] and reading["stillFocused"] and reading["techVisible"], shown)


if __name__ == "__main__":
    unittest.main()
