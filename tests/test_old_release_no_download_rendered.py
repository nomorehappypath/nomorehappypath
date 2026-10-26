# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): an old release of a Windows program says plainly why it cannot be opened.

Run:  PYTHONPATH=. python3 -m unittest tests.test_old_release_no_download_rendered -v

Owner 2026-10-21 (why-windows): "View app" on a program released before downloads existed failed with
"there is nothing here we know how to start". The card now says it is a program, that no download was kept,
and what to ask Delivery for.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board, board_viewer, browser_acceptance
from tests import test_get_the_build as support
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelector(".release-preview") || document.querySelector(".history-item")) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const history = document.querySelector('#history');
  if (history && !history.open) history.open = true;
  let historyNotice = '';
  for (let attempt = 0; attempt < 80 && !document.querySelector('.release-preview'); attempt++) {
    const item = document.querySelector('.history-item .release-nodownload');
    if (item) { historyNotice = item.textContent; break; }
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const historyAll = (document.querySelector('#history-list') || {}).textContent || 'NO-HISTORY-LIST';
  const preview = document.querySelector('.release-preview');
  const text = selector => Array.from(document.querySelectorAll(selector)).map(node => node.innerText).join('\n');
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    previewText: preview ? preview.innerText : '', hasBuild: Boolean(document.querySelector('.release-build')),
    noticeText: text('.task .release-nodownload'), historyNotice, historyAll, acceptedText: text('.release-response'),
  })});
})();
</script>
"""


class OldReleaseNoDownloadTests(support.MachineFixture):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        super().setUp()
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS} | {"head_commit": "c" * 40}
        board.record_release_ready(self.root, self.cto["id"], support.TASK, checks)
        with board.locked_state(self.root) as state:
            state["releases"][support.TASK]["preview"] = {
                "status": "failed", "requested": "view_app",
                "error": "there is nothing here we know how to start (no app.py, server.py, main.py, npm start script or index.html)",
            }

    def render(self) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root, project_name="Old release proof"))
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
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1300, height=1000)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe; a blank render is not a pass")
        return sink["value"]

    def test_the_card_explains_instead_of_showing_the_raw_error(self):
        reading = self.render()
        self.assertFalse(reading["hasBuild"])
        text = reading["previewText"]
        self.assertIn("no download was kept", text)
        self.assertNotIn("The app could not start", text)
        self.assertNotIn("no app.py", text)
        notice = reading["noticeText"]
        self.assertIn("No download is available for this release", notice)
        self.assertIn("Build ", notice)
        self.assertIn("for download", notice)
        self.assertIn("resume the project if it is paused", notice)

    def test_the_notice_is_also_on_the_accepted_card(self):
        with board.locked_state(self.root) as state:
            state["release_decisions"][support.TASK] = {"task": support.TASK, "decision": "accepted", "recorded_at": board.now()}
        with board.locked_state(self.root) as state:
            state["releases"][support.TASK]["status"] = "RELEASE_ACCEPTED"
        reading = self.render()
        self.assertIn("No download is available for this release", reading["historyNotice"], reading["historyAll"][:400])
        self.assertIn("Build ", reading["historyNotice"], reading["historyAll"][:400])

    def test_an_ordinary_crash_keeps_its_reason(self):
        with board.locked_state(self.root) as state:
            state["releases"][support.TASK]["preview"] = {"status": "failed", "requested": "view_app", "error": "port 8000 is already in use"}
        reading = self.render()
        self.assertIn("The app could not start: port 8000 is already in use", reading["previewText"])
        self.assertEqual(reading["noticeText"], "")


from tests.release_fixture_support import allow_fixture_releases  # noqa: E402

setUpModule, tearDownModule = allow_fixture_releases()   # bare release fixtures: production demands a final review (2026-10-24)


if __name__ == "__main__":
    unittest.main()
