# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof: the owner sees a routed-around finding on the task card with the CTO's reason.

Owner 2026-10-14: the CTO may set an in-scope finding aside as not material.
The task card must show that line (title and reason), and must no longer show
the red "Required before this task can finish" notice for it; "What happens
next" must no longer name an in-scope repair.
"""
from __future__ import annotations

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

REASON = "the owner's objective is a usable diagnosis of the existing deck, which does not need the fresh run"

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('[data-routed-around]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const line = document.querySelector('[data-routed-around]');
  const rect = line ? line.getBoundingClientRect() : null;
  const card = line ? line.closest('article, section, .task') : null;
  const nexts = Array.from(document.querySelectorAll('.next')).map(node => node.innerText);
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      visible: Boolean(rect) && rect.width > 0 && rect.height > 0,
      text: line ? line.innerText : '',
      fontSize: line ? getComputedStyle(line).fontSize : '',
      nexts,
      redNotice: nexts.some(text => /Required before this task can finish/.test(text)),
    })});
})();
</script>
"""

WAIVER_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('[data-owner-waived]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const line = document.querySelector('[data-owner-waived]');
  const rect = line ? line.getBoundingClientRect() : null;
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      visible: Boolean(rect) && rect.width > 0 && rect.height > 0,
      text: line ? line.innerText : '',
      fontSize: line ? getComputedStyle(line).fontSize : '',
    })});
})();
</script>
"""


class RenderedRoutedAroundFindingTests(unittest.TestCase):
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

    def test_the_owner_sees_the_set_aside_line_with_the_reason_and_no_red_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            delivery_with_direction(root, "TASK-ROUTED")
            finding = board.record_finding(root, "TASK-ROUTED", "Fresh run blocked by a browser permission",
                                           "The managed browser denies the local page", True)
            board.route_around_finding(root, finding["id"], REASON)
            reading = self._render(root, PROBE)
            self.assertTrue(reading, "Chrome reported nothing")
            self.assertTrue(reading["visible"], "the set-aside line is not visibly rendered")
            self.assertIn("Set aside by the CTO as not material", reading["text"])
            self.assertIn("Fresh run blocked by a browser permission", reading["text"])
            self.assertIn(REASON, reading["text"])
            self.assertIn("Delivery is not held on this", reading["text"])
            self.assertFalse(reading["redNotice"], "the red in-scope blocker must not show for a routed-around finding")
            self.assertTrue(any("What happens next" in text and "in-scope finding" not in text for text in reading["nexts"]),
                            reading["nexts"])
            self.assertGreaterEqual(float(reading["fontSize"].rstrip("px") or 0), 12.0, "readable at the size it ships")

    def test_the_owner_sees_the_deliverable_they_waived_with_their_own_words(self):
        from harness import control
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            delivery = delivery_with_direction(root, "TASK-WAIVED")
            cto_session = control.create(root, "claude_cto")
            cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
            board.create_contract(root, delivery["id"], "Diagnose the deck",
                                  ["An owner-readable diagnostic report", "A prior-versus-new real-run comparison"])
            words = "fresh run not required, the report on the existing deck is enough"
            message = board.record_owner_message(root, cto["id"], f"CTO: {words}.", "cto_message")["message"]
            board.waive_deliverable(root, cto["id"], "TASK-WAIVED", "A prior-versus-new real-run comparison", message["id"], words)
            reading = self._render(root, WAIVER_PROBE)
            self.assertTrue(reading, "Chrome reported nothing")
            self.assertTrue(reading["visible"], "the waived-deliverable line is not visibly rendered")
            self.assertIn("Waived by you", reading["text"])
            self.assertIn("A prior-versus-new real-run comparison", reading["text"])
            self.assertIn(words, reading["text"])
            self.assertIn("Not required any more", reading["text"])
            self.assertGreaterEqual(float(reading["fontSize"].rstrip("px") or 0), 12.0)


if __name__ == "__main__":
    unittest.main()
