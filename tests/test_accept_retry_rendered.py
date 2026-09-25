# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof that a failed Accept shows its reason and can be pressed again.

On 2026-09-25 three owner Accepts in a row saved the decision, then the Git
broker refused the fast-forward. The page showed a raw error once and then
"waiting for reintegration or recovery"; pressing Accept again was refused.
Here the failure is produced through the real board path, the card is read
in headless Chrome, and the retry button is clicked for real.
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

from harness import board, board_viewer, browser_acceptance, contract, control, project_memory
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_branding_rendered import probe_proxy
from tests import test_git_model_board

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const card = () => document.querySelector('.release-response.acceptance-failed');
  for (let attempt = 0; attempt < 400 && !card(); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const failed = card();
  const before = {
    present: Boolean(failed),
    visible: visible(failed),
    text: failed ? failed.textContent.trim() : '',
    tasksText: (document.querySelector('#tasks')?.textContent || '').trim().slice(0, 600),
  };
  const button = failed ? Array.from(failed.querySelectorAll('button')).find(b => b.textContent.trim() === 'Try Accept again') : null;
  before.buttonVisible = visible(button);
  if (button) button.click();
  // A completed Accept leaves the live area for Task history, so "done" is:
  // the failed card is gone and the history row reads OWNER ACCEPTED.
  let after = '';
  for (let attempt = 0; attempt < 200; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
    if (!card() && document.body.textContent.includes('OWNER ACCEPTED')) { after = 'card cleared; history reads OWNER ACCEPTED'; break; }
  }
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({before, after, notice: (document.querySelector('#notice')?.textContent || '').trim()}),
  });
})();
</script>
"""


class RenderedAcceptRetryTests(unittest.TestCase):
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
        self.base_commit = self.git("rev-parse", "HEAD").strip()
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
        project_memory.initialize(self.root, project_name="Accept retry proof", description="Facts.")

    def git(self, *arguments, cwd=None):
        result = subprocess.run(["/usr/bin/git", *arguments], cwd=cwd or self.root, capture_output=True, text=True)
        if result.returncode:
            self.fail(result.stderr or result.stdout)
        return result.stdout

    certified_candidate = test_git_model_board.GitModelBoardIntegrationTests.certified_candidate

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Accept retry proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="accept-retry-proof",
            chat_action_token="retry-token",
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

    def test_the_failed_accept_card_names_the_reason_and_the_retry_button_moves_main(self):
        committed = self.certified_candidate()
        with board.locked_state(self.root) as state:
            state["releases"]["GIT-MODEL"]["acceptance_manifest"] = []
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertEqual(response["git_acceptance"]["status"], "failed")
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base_commit)
        # The cause is fixed before the page is opened, so the click is a real retry.
        with board.locked_state(self.root) as state:
            state["releases"]["GIT-MODEL"]["acceptance_manifest"] = committed["manifest"]

        reading = self.render()

        before = reading["before"]
        self.assertTrue(before["present"], json.dumps(reading, indent=2))
        self.assertTrue(before["visible"], "the failed-acceptance card has no geometry")
        self.assertIn("Accepted, but not in main yet", before["text"])
        self.assertIn("press Accept again", before["text"])
        self.assertNotIn("manifest", before["text"])
        self.assertNotIn("reintegration or recovery", before["text"])
        self.assertTrue(before["buttonVisible"], "Try Accept again is not visible")
        self.assertIn("OWNER ACCEPTED", reading["after"], json.dumps(reading, indent=2))
        self.assertNotIn("OWNER ACCEPTED", before["tasksText"], "the badge must not read OWNER ACCEPTED while main has not moved")
        self.assertEqual(self.git("rev-parse", "main").strip(), committed["commit"])
        self.assertEqual((self.root / "product.txt").read_text(encoding="utf-8"), "accepted\n")


if __name__ == "__main__":
    unittest.main()
