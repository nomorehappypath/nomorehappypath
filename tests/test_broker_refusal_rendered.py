# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof that a refused Git write reads as BLOCKED with its reason.

On 2026-09-21 a Delivery agent's every write was refused for 21 minutes while
Mission Control described it as stalled and the watchdog kept "recovering"
it. The refusal is produced here through the real broker path — a governed
commit whose nonce replays — and the page is read in headless Chrome, because
the board state carrying the reason is not the same as the owner seeing it.
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
from unittest import mock

from harness import board, board_viewer, browser_acceptance, contract, control, git_broker, project_memory
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (document.querySelector('#agents .agent-row .badge')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const row = document.querySelector('#agents .agent-row');
  const view = row ? Array.from(row.querySelectorAll('button')).find(b => b.textContent.trim() === 'View status') : null;
  if (view) view.click();
  await new Promise(resolve => setTimeout(resolve, 300));
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      agentsText: document.querySelector('#agents').textContent.trim(),
      badges: Array.from(document.querySelectorAll('#agents .badge')).map(node => node.textContent.trim()),
      tasksText: document.querySelector('#tasks').textContent.trim(),
      dialogOpen: Boolean(document.querySelector('#status-dialog')?.open),
      dialogText: (document.querySelector('#status-dialog-body')?.textContent || '').trim(),
    }),
  });
})();
</script>
"""


class RenderedBrokerRefusalTests(unittest.TestCase):
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
        self.begun = board.begin_task(self.root, self.delivery["id"], "GIT-MODEL")
        contract.create_contract(self.root, "GIT-MODEL", "Implement one governed Git change.", ["governed change"])
        agreed_requirements(self.root, self.delivery["id"], "Implement and verify the governed change.")
        board.define_delivery_plan(self.root, self.delivery["id"], "atomic", "One cohesive fixture")
        project_memory.initialize(self.root, project_name="Refusal proof", description="Facts.")

    def refuse_a_real_write(self) -> str:
        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("first\n", encoding="utf-8")
        board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "first governed commit")
        (workspace / "product.txt").write_text("second\n", encoding="utf-8")
        with mock.patch.object(board, "_next_broker_nonce", return_value=1):
            with self.assertRaises(git_broker.ReplayError) as caught:
                board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "replayed nonce")
        return str(caught.exception)

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Refusal proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="refusal-proof",
            chat_action_token="refusal-token",
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
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=850,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]

    def test_a_refused_write_renders_as_blocked_with_its_reason_not_as_stalled(self):
        reason = self.refuse_a_real_write()
        self.assertIn("replay refused", reason)
        reading = self.render()
        self.assertIn("BLOCKED — GIT WRITE REFUSED", reading["badges"], json.dumps(reading, indent=2))
        self.assertIn("BLOCKED — GIT WRITE REFUSED", reading["tasksText"])
        self.assertNotIn("stopped checking the board", reading["agentsText"])
        self.assertTrue(reading["dialogOpen"], "View status did not open the status dialog")
        self.assertIn("refused by the Git broker", reading["dialogText"])
        self.assertIn("replay refused", reading["dialogText"])
        self.assertIn("recover-git", reading["dialogText"])


if __name__ == "__main__":
    unittest.main()
