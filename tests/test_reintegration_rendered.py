# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof that a task needing re-integration shows one plain line.

On 2026-09-25 the only trace of a stale base was a control-plane incident
repeated every cycle in the event log. The marker is produced here through
the real board path and the task card is read in headless Chrome.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, browser_acceptance, contract, control, project_memory
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const find = () => Array.from(document.querySelectorAll('#tasks .next')).find(n => n.textContent.includes('A newer version of main was accepted'));
  for (let attempt = 0; attempt < 400 && !find(); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const line = find();
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      present: Boolean(line),
      visible: visible(line),
      text: line ? line.textContent.trim() : '',
      badges: Array.from(document.querySelectorAll('#tasks .badge')).map(n => n.textContent.trim()),
      tasksText: (document.querySelector('#tasks')?.textContent || '').trim().slice(0, 400),
    }),
  });
})();
</script>
"""


class RenderedReintegrationTests(unittest.TestCase):
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
        for arguments in (
            ["init", "-b", "main"], ["config", "user.name", "Fixture"],
            ["config", "user.email", "fixture@example.invalid"],
        ):
            subprocess.run(["/usr/bin/git", *arguments], cwd=self.root, check=True, capture_output=True)
        (self.root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
        (self.root / "product.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "add", ".gitignore", "product.txt"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["/usr/bin/git", "commit", "-q", "-m", "base"], cwd=self.root, check=True, capture_output=True)
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Implement one governed Git change.")
        board.begin_task(self.root, self.delivery["id"], "GIT-MODEL")
        contract.create_contract(self.root, "GIT-MODEL", "Implement one governed Git change.", ["governed change"])
        agreed_requirements(self.root, self.delivery["id"], "Implement and verify the governed change.")
        board.define_delivery_plan(self.root, self.delivery["id"], "atomic", "One cohesive fixture")
        project_memory.initialize(self.root, project_name="Re-integration proof", description="Facts.")

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Re-integration proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="reintegration-proof",
            chat_action_token="reintegration-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE),
        )
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]

    def test_the_task_shows_one_plain_line_and_the_badge_names_the_state(self):
        with patch("harness.control.enqueue_instruction", return_value={"id": "wake-1"}):
            marker = board.route_reintegration(self.root, "GIT-MODEL", "main advanced when another task was accepted", source="owner-accept")
        self.assertEqual(marker["delivery"], "active")
        reading = self.render()
        self.assertTrue(reading["present"], json.dumps(reading, indent=2))
        self.assertTrue(reading["visible"], "the re-integration line has no geometry")
        self.assertIn("Delivery is merging it in", reading["text"])
        self.assertNotIn("reintegrate-main", reading["text"])
        self.assertIn("RE-INTEGRATION NEEDED", reading["badges"], json.dumps(reading, indent=2))


if __name__ == "__main__":
    unittest.main()
