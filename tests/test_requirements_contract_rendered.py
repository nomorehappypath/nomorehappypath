# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Rendered proof: the owner sees and can use the Go ahead / Modify buttons."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance, control
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_requirements_contract import context, delivery_with_direction

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('[data-req-go]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const go = document.querySelector('[data-req-go]');
  const modify = document.querySelector('[data-req-modify]');
  const panel = go ? go.closest('.requirements-proposal') : null;
  const before = {
    goVisible: Boolean(go) && go.getBoundingClientRect().width > 0,
    modifyVisible: Boolean(modify) && modify.getBoundingClientRect().width > 0,
    panelText: panel ? panel.textContent : '',
  };
  let dialogOpen = false, dialogTitle = '';
  if (modify) {
    modify.click();
    await new Promise(resolve => setTimeout(resolve, 300));
    const dialog = document.querySelector('#owner-message-dialog');
    dialogOpen = Boolean(dialog && dialog.open);
    dialogTitle = document.querySelector('#owner-message-title')?.textContent || '';
    if (dialogOpen) dialog.close();
  }
  let accepted = false;
  if (go) {
    window.confirm = () => true;
    go.click();
    for (let attempt = 0; attempt < 40; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 250));
      const note = document.querySelector('[id^="req-decision-note-"]');
      if (note && /Recorded/.test(note.textContent)) { accepted = true; break; }
      if (!document.querySelector('[data-req-go]')) { accepted = true; break; }
    }
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({before, dialogOpen, dialogTitle, accepted})});
})();
</script>
"""


class RenderedRequirementsTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_buttons_render_modify_opens_composer_go_ahead_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            agent = delivery_with_direction(root, "TASK-RENDERED")
            board.record_requirement_proposal(
                root, agent["id"],
                "Final agreed requirements: a hello page with a visible greeting.",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            sink: dict = {}
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(
                f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE,
            ))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            self.addCleanup(proxy.server_close)
            self.addCleanup(proxy.shutdown)
            profile = tempfile.TemporaryDirectory()
            self.addCleanup(profile.cleanup)
            process = browser_acceptance.launch(
                f"http://127.0.0.1:{proxy.server_address[1]}/",
                Path(profile.name), width=1300, height=950,
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
                reading = sink.get("value") or {}
            finally:
                process.close()

            self.assertTrue(reading, "Chrome reported nothing")
            before = reading["before"]
            self.assertTrue(before["goVisible"], "Go ahead button not visibly rendered")
            self.assertTrue(before["modifyVisible"], "Modify button not visibly rendered")
            self.assertIn("visible greeting", before["panelText"], "proposal text not shown")
            self.assertIn("your decision", before["panelText"])
            self.assertTrue(reading["dialogOpen"], "Modify did not open the composer dialog")
            self.assertIn("Modify the requirements", reading["dialogTitle"])
            self.assertTrue(reading["accepted"], "Go ahead click did not record")
            proposal = board.snapshot(root)["requirement_proposals"]["TASK-RENDERED"]
            self.assertEqual(proposal["status"], "accepted", "board did not record the click")

    def test_degraded_state_shows_honest_notice_never_buttons(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = context(Path(temporary))
            delivery_with_direction(root, "TASK-NO-PROPOSAL")
            page_source = None
            server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            probe = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 60; attempt++) {
    if (document.querySelector('.requirements-pending')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const pending = document.querySelector('.requirements-pending');
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      pendingText: pending ? pending.textContent : '',
      buttons: document.querySelectorAll('[data-req-go]').length,
    })});
})();
</script>
"""
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
                f"http://127.0.0.1:{proxy.server_address[1]}/",
                Path(profile.name), width=1300, height=950,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
                reading = sink.get("value") or {}
            finally:
                process.close()
            self.assertTrue(reading, "Chrome reported nothing")
            self.assertEqual(reading["buttons"], 0, "buttons must never render without a filed proposal")
            self.assertIn("has not filed its requirements proposal yet", reading["pendingText"])


if __name__ == "__main__":
    unittest.main()
