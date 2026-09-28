# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof of backlog #10 in headless Chrome, on the real Mission Control page.

The owner presses Stop on a working Delivery: the prompt and the notice say the
task is kept, the task card stays with "STOPPED — TASK KEPT" and a Cancel task
button, and only pressing Cancel task (with its plain confirm) removes it.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, browser_acceptance, control, project_memory
from tests import test_stop_keeps_task as _stop_tests
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 150) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const prompts = [];
  window.confirm = message => { prompts.push(message); return true; };
  const card = () => document.querySelector('#tasks .task[data-task="PLANNER"]');
  const stopButton = () => Array.from(document.querySelectorAll('#agents .agent-row button, #sessions button'))
    .find(button => button.textContent.trim() === 'Stop terminal' || button.textContent.trim() === 'Stop');
  const reading = {};
  reading.cardBefore = Boolean(await wait(card));
  await wait(stopButton);
  reading.stopFound = Boolean(stopButton());
  if (stopButton()) stopButton().click();
  const notice = () => (document.querySelector('#notice')?.textContent || '').trim();
  await wait(() => /Agent stopped/.test(notice()));
  reading.stopPrompt = prompts[0] || '';
  reading.stopNotice = notice();
  reading.noticeVisible = visible(document.querySelector('#notice'));
  await wait(() => /STOPPED — TASK KEPT/.test(card()?.textContent || ''));
  reading.cardAfterStop = card() ? card().textContent.replace(/\s+/g, ' ') : '';
  reading.cardVisible = visible(card());
  const cancel = () => card()?.querySelector('button.cancel-task');
  reading.cancelVisible = visible(cancel());
  reading.cancelLabel = cancel() ? cancel().textContent.trim() : '';
  if (cancel()) cancel().click();
  await wait(() => /Task cancelled/.test(notice()));
  reading.cancelPrompt = prompts[1] || '';
  reading.cancelNotice = notice();
  await wait(() => !card());
  reading.cardGone = !card();
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""


class RenderedStopKeepsTaskTests(_stop_tests._Fixture):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        super().setUp()
        self.root = Path(self.tmp.name).resolve()
        project_memory.initialize(self.root, project_name="Stop keeps task proof", description="Facts.")

    def test_stop_keeps_the_task_and_only_cancel_task_removes_it(self):
        self.working_task("PLANNER")
        launcher = patch("harness.board_viewer.launch_terminal")
        launcher.start()
        self.addCleanup(launcher.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Stop keeps task proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="stop-keeps-task-proof",
            chat_action_token="owner-token",
        ))
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
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1100)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        dump = json.dumps(reading, indent=2, ensure_ascii=False)[:4000]
        self.assertTrue(reading["cardBefore"], "the task card never rendered; a blank render is not a pass\n" + dump)
        self.assertTrue(reading["stopFound"], dump)
        # criterion 4: the wording
        self.assertIn("This closes the agent. The task is kept with all its work", reading["stopPrompt"], dump)
        self.assertNotIn("removed", reading["stopPrompt"])
        self.assertEqual(reading["stopNotice"], "Agent stopped. The task is kept — start a new agent to carry on.", dump)
        self.assertTrue(reading["noticeVisible"])
        # criterion 1: the task stays, and says what to do
        self.assertTrue(reading["cardVisible"], dump)
        self.assertIn("STOPPED — TASK KEPT", reading["cardAfterStop"])
        self.assertIn("Start a new Delivery agent to carry on", reading["cardAfterStop"])
        # criterion 3: Cancel task is on the task, asks plainly, and only it removes the task
        self.assertTrue(reading["cancelVisible"], dump)
        self.assertEqual(reading["cancelLabel"], "Cancel task")
        self.assertIn("Its board records and its workspace will be removed", reading["cancelPrompt"], dump)
        self.assertIn("Task cancelled", reading["cancelNotice"])
        self.assertTrue(reading["cardGone"], dump)
        state = board.snapshot(self.root)
        self.assertIn("PLANNER", state["cancelled_tasks"])
        self.assertEqual(control.snapshot(self.root)["active_counts"]["codex_delivery"], 0)


if __name__ == "__main__":
    unittest.main()
