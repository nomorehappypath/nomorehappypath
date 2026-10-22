# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Give direction with no agents running starts the three agents itself (open-list item B, finding 25), in a real browser.

The terminals are stubbed (no window opens); everything else is the real page, the real viewer server and the real board.
Run:  PYTHONPATH=. python3 -m unittest tests.test_direction_starts_agents_rendered -v
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board, board_viewer, browser_acceptance, global_settings, project_memory
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

HEAD = r"""
<script>
(async () => {
  const wait = async (test, tries = 900) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const q = s => document.querySelector(s);
  const post = value => fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)});
  const result = {};
  const give = async () => {
    await wait(() => q('#give-direction') && !q('#give-direction').hidden && !q('#give-direction').disabled);
    q('#give-direction').click();
    await wait(() => q('#owner-message-dialog').open);
    result.help = q('#owner-message-help').textContent;
    result.attachmentsHidden = q('#owner-message-attachments').hidden;
    q('#owner-message-text').value = 'Build me a tiny program that prints the temperature.';
    q('#owner-message-text').dispatchEvent(new Event('input'));
    q('#owner-message-submit').click();
    await wait(() => !q('#owner-message-dialog').open);
  };
"""

NORMAL = HEAD + r"""
  try {
    await give();
    await wait(() => !q('#direction-start-note').hidden);
    result.noteWhileStarting = q('#direction-start-note').textContent;
    await post({phase: 'sent', ...result});
    await wait(() => /direction was sent to Delivery/.test(q('#notice').textContent), 1200);
    result.finalNotice = q('#notice').textContent;
    await wait(() => q('#direction-start-note').hidden);
    result.noteGone = q('#direction-start-note').hidden;
    result.stored = window.localStorage.getItem('pending-direction:');
    result.phase = 'done';
  } catch (error) { result.error = String(error && error.stack || error); result.phase = 'done'; }
  await post(result);
})();
</script>
"""

PAUSED = HEAD + r"""
  try {
    await wait(() => !q('#paused-banner').hidden);
    await give();
    await wait(() => !q('#direction-start-note').hidden);
    result.pausedNote = q('#direction-start-note').textContent;
    result.pausedButtonsStillOff = q('#codex').disabled;
    await post({phase: 'saved', ...result});
    await wait(() => q('#paused-banner').hidden && /Starting|starting/.test(q('#direction-start-note').textContent + q('#notice').textContent), 1200);
    result.afterResume = q('#direction-start-note').textContent;
    result.phase = 'done';
  } catch (error) { result.error = String(error && error.stack || error); result.phase = 'done'; }
  await post(result);
})();
</script>
"""


class DirectionStartsAgentsRenderedTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "project"
        self.root.mkdir()
        project_memory.initialize(self.root, project_name="Direction starts agents", description="Facts.")
        self.home = Path(self._tmp.name).resolve() / "home"
        global_settings.update_agent_settings(self.home, {
            "delivery": {"provider": "codex", "model": "gpt-6-sol", "effort": "high"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
            "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
        })
        self.launched: list[dict] = []
        patcher = mock.patch("harness.board_viewer.launch_terminal", side_effect=self._fake_launch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_launch(self, root, session, *, manager_home=None):
        self.launched.append(dict(session))
        return None

    def open_page(self, script):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Direction starts agents", manager_url="http://127.0.0.1:1/", settings_home=self.home,
            project_id="direction", chat_action_token="owner-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", self.sink, script))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        self.addCleanup(process.close)

    def wait_for(self, test, seconds, what):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if test():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}; page said {self.sink.get('value')}; launched {[s['kind'] for s in self.launched]}")

    def phase(self, name, seconds=120):
        self.wait_for(lambda: self.sink.get("value", {}).get("phase") == name, seconds, f"phase {name}")
        return dict(self.sink["value"])

    def register_waiting_delivery(self):
        delivery = next(s for s in self.launched if s["kind"] == "codex_delivery")
        return board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, "Delivery", "OpenAI", delivery["id"])

    def test_direction_with_nothing_running_starts_the_three_agents_once_and_then_reaches_delivery(self):
        self.open_page(NORMAL)
        sent = self.phase("sent")
        self.assertIn("Delivery, the Independent Reviewer and the CTO start by themselves", sent["help"])
        self.assertIn("Your terminal may ask you to trust this folder; choose the Yes option it shows, once", sent["help"])
        self.assertTrue(sent["attachmentsHidden"], "documents cannot be kept across the start, so the box is not offered")
        self.wait_for(lambda: len(self.launched) >= 3, 90, "three launches")
        self.assertEqual(sorted(s["kind"] for s in self.launched), ["claude_cto", "claude_reviewer", "codex_delivery"])
        agent = self.register_waiting_delivery()
        done = self.phase("done", 180)
        self.assertNotIn("error", done, json.dumps(done))
        self.assertIn("direction was sent to Delivery", done["finalNotice"])
        self.assertTrue(done["noteGone"])
        time.sleep(3)   # several refreshes later: still exactly three launches, never a second Delivery
        self.assertEqual(len(self.launched), 3)
        with board.locked_state(self.root) as state:
            recorded = json.dumps(state.get("owner_directions") or {})
        self.assertIn("tiny program that prints the temperature", recorded)
        self.assertTrue(agent["id"])

    def test_a_paused_project_keeps_the_direction_and_starts_the_agents_only_on_resume(self):
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        self.open_page(PAUSED)
        saved = self.phase("saved")
        self.assertIn("This project is paused", saved["pausedNote"])
        self.assertIn("When you Resume the project", saved["pausedNote"])
        time.sleep(3)
        self.assertEqual(self.launched, [], "a paused project is not written to and nothing starts")
        resume = board.begin_project_resume(self.root)
        board.finish_project_resume(self.root, resume["resume_id"])
        self.wait_for(lambda: len(self.launched) >= 3, 120, "three launches after Resume")
        self.assertEqual(sorted(s["kind"] for s in self.launched), ["claude_cto", "claude_reviewer", "codex_delivery"])
        self.assertEqual(self.phase("done", 120).get("error"), None)


if __name__ == "__main__":
    unittest.main()
