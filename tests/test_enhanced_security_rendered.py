# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof: the owner sees and can use the Enhanced security checkbox on a task card.

Owner 2026-10-12: "a check box per task ... once task is reviewed we cannot
changed it". The card shows the checkbox with its one-sentence explanation,
ticking it records the choice on the board, the saved note survives the
two-second refresh, and a reviewed task shows the locked text instead.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('[data-security-task]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const box = document.querySelector('[data-security-task]');
  const card = box ? box.closest('.security-choice') : null;
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const before = {
    boxVisible: visible(box), checked: Boolean(box && box.checked),
    cardText: card ? card.innerText : '',
  };
  let saved = false, noteAfterRefresh = '', checkedAfter = null;
  if (box) {
    box.click();
    for (let attempt = 0; attempt < 60; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 250));
      const note = document.querySelector('[id^="security-note-"]');
      if (note && /Saved/.test(note.textContent)) { saved = true; break; }
    }
    // one full refresh cycle later the note and the checked state must survive
    await new Promise(resolve => setTimeout(resolve, 2600));
    const note = document.querySelector('[id^="security-note-"]');
    noteAfterRefresh = note ? note.textContent : '';
    const after = document.querySelector('[data-security-task]');
    checkedAfter = after ? after.checked : null;
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({before, saved, noteAfterRefresh, checkedAfter})});
})();
</script>
"""

LOCKED_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('[data-security-card]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const card = document.querySelector('[data-security-card]');
  const rect = card ? card.getBoundingClientRect() : null;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      visible: Boolean(rect) && rect.width > 0 && rect.height > 0,
      text: card ? card.innerText : '',
      hasCheckbox: Boolean(document.querySelector('[data-security-task]')),
    })});
})();
</script>
"""


class RenderedEnhancedSecurityTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def _render(self, root: Path, probe: str, wait: float = 75) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(
            f"http://127.0.0.1:{server.server_address[1]}", sink, probe,
        ))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1300, height=950,
        )
        try:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
            return sink.get("value") or {}
        finally:
            process.close()

    def test_checkbox_renders_ticking_it_records_and_the_note_survives_a_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            delivery_with_direction(root, "TASK-RENDERED")
            reading = self._render(root, PROBE)
            self.assertTrue(reading, "Chrome reported nothing")
            before = reading["before"]
            self.assertTrue(before["boxVisible"], "the Enhanced security checkbox is not visibly rendered")
            self.assertFalse(before["checked"], "a new task must start Off")
            for words in ("Enhanced security for this task", "ways the app could be attacked", "tests that try them",
                          "a record of what protects it", "before Delivery's first review request", "it then locks"):
                self.assertIn(words, before["cardText"], words)
            self.assertTrue(reading["saved"], "ticking the box did not report a save")
            self.assertIn("Saved: enhanced security On", reading["noteAfterRefresh"], "the note did not survive the refresh")
            self.assertTrue(reading["checkedAfter"], "the box did not stay ticked after the refresh")
            record = board.snapshot(root)["task_security"]["TASK-RENDERED"]
            self.assertEqual(record["enhanced"], True, "the board did not record the click")
            self.assertEqual(record["revision"], 1)

    def test_a_reviewed_task_shows_the_locked_text_and_no_checkbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            delivery_with_direction(root, "TASK-LOCKED")
            board.set_task_security(root, "TASK-LOCKED", True)
            path = root / ".harness" / "board" / "state.json"
            state = json.loads(path.read_text())
            state["task_security"]["TASK-LOCKED"]["locked"] = True
            path.write_text(json.dumps(state))
            reading = self._render(root, LOCKED_PROBE)
            self.assertTrue(reading, "Chrome reported nothing")
            self.assertTrue(reading["visible"], "the locked security line is not visibly rendered")
            self.assertIn("Enhanced security: On (locked at review)", reading["text"])
            self.assertIn("start a new task", reading["text"])
            self.assertFalse(reading["hasCheckbox"], "a locked task must not offer the checkbox")


if __name__ == "__main__":
    unittest.main()
