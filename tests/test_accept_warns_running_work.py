# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item F: Accept warns plainly first while the test copy is running.

Owner, 2026-10-01: "When the owner clicks Accept while a job is running in the
View app copy, warn plainly first ('A task is still running in the test copy;
Accept will stop it')." Accepting closes the release and the preview
supervisor stops its test copy. The real Accepted button is pressed in
headless Chrome against the real board page.
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

from harness import board, board_viewer, browser_acceptance, release_preview
from tests import test_accept_retry_rendered, test_board_viewer
from tests.test_branding_rendered import probe_proxy

_fixture = vars(test_accept_retry_rendered.RenderedAcceptRetryTests)  # helpers only

PROBE = r"""
<script>
(async () => {
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const asked = [];
  let answer = false;
  window.confirm = message => { asked.push(message); return answer; };
  // A saved acceptance replaces the release card, so its Accepted button is gone.
  const button = () => Array.from(document.querySelectorAll('.release-response button')).find(b => b.textContent.trim() === 'Accepted');
  for (let attempt = 0; attempt < 400 && !button(); attempt++) await pause(100);
  const found = Boolean(button());
  if (found) button().click();
  await pause(4500);
  const afterCancel = {asked: asked.length, buttonStill: Boolean(button())};
  answer = true;
  if (button()) button().click();
  let accepted = false;
  for (let attempt = 0; attempt < 200 && !accepted; attempt++) { await pause(100); accepted = !button(); }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    found, afterCancel, messages: asked, accepted,
  })});
})();
</script>
"""


class TestCopyRunningTests(unittest.TestCase):
    def running(self, preview):
        viewer = test_board_viewer.BoardViewerTests("run")
        state = {"releases": {"T": {"preview": preview}}}
        return viewer.run_node(f"process.stdout.write(JSON.stringify(testCopyRunning({json.dumps(state)}, 'T')));")

    def test_only_a_running_or_starting_test_copy_counts(self):
        self.assertTrue(self.running({"status": "ready", "url": "http://127.0.0.1:5173/"}))
        self.assertTrue(self.running({"status": "starting", "requested": "view_app"}))
        for preview in ({}, {"status": "failed"}, {"status": "stopped"}, {"status": "app_bundle"}):
            with self.subTest(preview=preview):
                self.assertFalse(self.running(preview))

    def test_the_supervisor_really_stops_the_test_copy_once_the_release_is_decided(self):
        """The warning's claim: accepting closes the release, and the supervisor stops its test copy."""
        stopped = []

        class FakePreview:
            head_commit = "abc"
            directory = Path(tempfile.mkdtemp())

            def stop(self):
                stopped.append(True)

        with tempfile.TemporaryDirectory() as temporary:
            supervisor = release_preview.ReleasePreviewSupervisor(Path(temporary))
            supervisor.previews["T"] = FakePreview()
            state = {"releases": {"T": {"status": "VISUAL_TEST_REQUIRED", "head_commit": "abc"}},
                     "release_decisions": {"T": {"decision": "accepted"}}}
            with patch.object(release_preview.board, "snapshot", return_value=state), \
                    patch.object(release_preview.board, "pause_state", return_value={"status": "paused"}):
                report = supervisor.tick()
        self.assertEqual(report["stopped"], ["T"])
        self.assertEqual(stopped, [True])


class RenderedAcceptWarningTests(unittest.TestCase):
    setUp = _fixture["setUp"]
    git = _fixture["git"]
    certified_candidate = _fixture["certified_candidate"]

    def press_accepted(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Accept warning proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="accept-warning-proof",
            chat_action_token="warning-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]

    def test_running_test_copy_warns_cancel_saves_nothing_and_ok_accepts(self):
        self.certified_candidate()
        with board.locked_state(self.root) as state:
            release = state["releases"]["GIT-MODEL"]
            release["preview"] = {
                "status": "ready", "url": "http://127.0.0.1:9/", "requested": "view_app",
                "head_commit": release.get("head_commit", ""),
            }
        reading = self.press_accepted()
        self.assertTrue(reading["found"], json.dumps(reading, indent=2))
        self.assertEqual(reading["afterCancel"], {"asked": 1, "buttonStill": True}, "Cancel saved nothing")
        message = reading["messages"][0]
        self.assertIn("The test copy of this release is still running on this computer (View app).", message)
        self.assertIn("If a task is still running in the test copy, Accept will stop it", message)
        self.assertTrue(message.endswith("Accept anyway?"))
        self.assertEqual(len(reading["messages"]), 2, "OK on the second press accepted")
        self.assertTrue(reading["accepted"])
        self.assertEqual(board.snapshot(self.root)["release_decisions"]["GIT-MODEL"]["decision"], "accepted")

    def test_with_no_test_copy_running_accepted_saves_at_once_with_no_dialog(self):
        self.certified_candidate()
        reading = self.press_accepted()
        self.assertTrue(reading["found"], json.dumps(reading, indent=2))
        self.assertEqual(reading["messages"], [])
        self.assertFalse(reading["afterCancel"]["buttonStill"], json.dumps(reading, indent=2))
        self.assertEqual(board.snapshot(self.root)["release_decisions"]["GIT-MODEL"]["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
