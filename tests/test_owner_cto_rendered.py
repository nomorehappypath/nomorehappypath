# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof of the owner ↔ CTO channel in Mission Control.

The owner-action card with its Copy button, the "Message the CTO" composer,
and the "CTO is not responding - it may need /login" notice are read in
headless Chrome from the real page, with the buttons actually clicked.
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
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const card = () => document.querySelector('#owner-actions .owner-action');
  for (let attempt = 0; attempt < 120 && !card(); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const action = card();
  const copy = action ? Array.from(action.querySelectorAll('button')).find(b => b.textContent.trim() === 'Copy') : null;
  const reading = {
    cardPresent: Boolean(action), cardVisible: visible(action),
    cardText: action ? action.textContent.trim() : '',
    command: action ? (action.querySelector('pre')?.textContent || '') : '',
    copyVisible: visible(copy), copyLabel: '',
  };
  if (copy) {
    copy.click();
    for (let attempt = 0; attempt < 30 && copy.textContent.trim() === 'Copy'; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    reading.copyLabel = copy.textContent.trim();
    reading.selectedText = String(window.getSelection() || '').trim();
  }
  const ctoRow = Array.from(document.querySelectorAll('#agents .agent-row')).find(row => (row.textContent || '').includes('Role: cto'));
  const message = ctoRow ? Array.from(ctoRow.querySelectorAll('button')).find(b => b.textContent.trim() === 'Message the CTO') : null;
  reading.messageVisible = visible(message);
  if (message) {
    message.click();
    await new Promise(resolve => setTimeout(resolve, 300));
    reading.dialogOpen = Boolean(document.querySelector('#owner-message-dialog')?.open);
    reading.dialogTitle = (document.querySelector('#owner-message-title')?.textContent || '').trim();
    reading.submitLabel = (document.querySelector('#owner-message-submit')?.textContent || '').trim();
  }
  reading.attention = (document.querySelector('#attention')?.textContent || '').trim();
  reading.badges = Array.from(document.querySelectorAll('#agents .badge')).map(n => n.textContent.trim());
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""


class RenderedOwnerCtoTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project_memory.initialize(self.root, project_name="Owner channel proof", description="Facts.")
        self.session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.session["id"])

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Owner channel proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="owner-channel-proof",
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
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]

    def test_the_card_the_copy_button_the_composer_and_the_login_notice_render(self):
        board.record_owner_action(
            self.root, self.cto["id"], "Run the film GPU proof",
            command="bash /tmp/release_film.sh", why="The Studio runtime only runs outside the agent sandbox.",
        )
        with board.locked_state(self.root) as state:
            state["agents"][self.cto["id"]].update({
                "recovery_state": "unresponsive", "liveness": "stalled", "liveness_note": board.CTO_UNRESPONSIVE_NOTE,
            })
        reading = self.render()
        self.assertTrue(reading["cardPresent"], json.dumps(reading, indent=2))
        self.assertTrue(reading["cardVisible"], "the owner-action card has no geometry")
        self.assertIn("Run the film GPU proof", reading["cardText"])
        self.assertIn("outside the agent sandbox", reading["cardText"])
        self.assertEqual(reading["command"], "bash /tmp/release_film.sh")
        self.assertTrue(reading["copyVisible"], "Copy is not visible")
        # Headless Chrome has no clipboard permission for a synthetic click, so
        # the proof is the fallback: the command is selected for one keystroke.
        # A real click in the owner's browser takes the clipboard path first.
        self.assertIn(reading["copyLabel"], {"Copied", "Selected — press ⌘C"}, json.dumps(reading, indent=2))
        if reading["copyLabel"] != "Copied":
            self.assertEqual(reading["selectedText"], "bash /tmp/release_film.sh")
        self.assertTrue(reading["messageVisible"], "Message the CTO is not visible on the CTO card")
        self.assertTrue(reading.get("dialogOpen"), "the composer did not open")
        self.assertEqual(reading.get("dialogTitle"), "Message the CTO")
        self.assertEqual(reading.get("submitLabel"), "Send to the CTO")
        self.assertIn("CTO is not responding - it may need /login", reading["attention"])
        self.assertIn("CTO NOT RESPONDING", reading["badges"])

    def test_a_cleared_action_leaves_the_page(self):
        action = board.record_owner_action(self.root, self.cto["id"], "Open the folder", command="open ~/Desktop")
        board.clear_owner_action(self.root, self.cto["id"], action["id"], "opened")
        reading = self.render()
        self.assertFalse(reading["cardPresent"], json.dumps(reading, indent=2))


if __name__ == "__main__":
    unittest.main()
