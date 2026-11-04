# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof of the card an Accept shows when the project uploads on accept (task 2026-11-03_github-upload-on-accept).

The Accept goes through the real board path with a destination; the upload record is then put into each state the card can show
(uploading, uploaded, failed, diverged) and the page is read in headless Chrome. A project with no destination must still show
today's "Upload approved version…" button. The "Upload again" button is clicked for real against a working repository.
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

from harness import board, board_viewer, browser_acceptance, upload_on_accept
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_upload_on_accept import UploadBase

PROBE = r"""
<script>
(async () => {
  const find = () => document.querySelector('.upload-card') || Array.from(document.querySelectorAll('.release-response.recorded')).find(n => /Upload approved version|Accepted/.test(n.textContent));
  for (let attempt = 0; attempt < 400 && !find(); attempt++) await new Promise(resolve => setTimeout(resolve, 100));
  const card = find();
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const read = () => { const c = find(); return {text: c ? c.textContent.trim() : '', visible: visible(c), buttons: c ? Array.from(c.querySelectorAll('button')).map(b => b.textContent.trim()) : [], spinner: Boolean(c && c.querySelector('.spin'))}; };
  const before = read();
  let after = null;
  if (location.search.includes('click') || window.__clickAgain) {
    const button = card && Array.from(card.querySelectorAll('button')).find(b => b.textContent.trim() === 'Upload again');
    if (button) button.click();
    for (let attempt = 0; attempt < 300; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 100));
      if (/Accepted and uploaded/.test((find() || {}).textContent || '')) break;
    }
    after = read();
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({before, after})});
})();
</script>
"""


HISTORY_PROBE = r"""
<script>
(async () => {
  const line = () => document.querySelector('[data-task-upload-history]');
  document.querySelector('#history').open = true;    // the history loads when it is opened
  for (let attempt = 0; attempt < 400 && !line(); attempt++) await new Promise(resolve => setTimeout(resolve, 100));
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({line: line() ? line().textContent.trim() : '', live: Boolean(document.querySelector('.upload-card'))})});
})();
</script>
"""


class RenderedUploadCardTests(UploadBase):
    def setUp(self):
        require_loopback()
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        super().setUp()

    def render(self, probe=PROBE) -> dict:
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Upload card proof", manager_url="http://127.0.0.1:1/", project_id="upload-card-proof", chat_action_token="t"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, probe))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]

    def set_upload(self, **fields):
        with board.locked_state(self.root) as state:
            state["release_decisions"]["GIT-MODEL"]["upload"].update(fields)

    def test_the_card_in_each_state(self):
        self.accept()
        where = f"{self.remote} (release)"

        self.set_upload(status="running", attempts=[{"started_at": "2026-11-03T12:00:00+00:00", "outcome": "running"}])
        card = self.render()["before"]
        self.assertTrue(card["visible"], json.dumps(card))
        self.assertIn(f"Accepted. Uploading to {where}", card["text"])
        self.assertTrue(card["spinner"])
        self.assertEqual(card["buttons"], [])

        # Finished work leaves the live area; its history row carries the same plain line.
        self.set_upload(status="uploaded", attempts=[{"outcome": "uploaded"}])
        reading = self.render(HISTORY_PROBE)
        self.assertEqual(reading["line"], f"Accepted and uploaded to {where}")
        self.assertFalse(reading["live"])

        self.set_upload(status="uploaded", uploaded_at=board.now(), attempts=[{"outcome": "uploaded"}])
        card = self.render()["before"]       # just finished: still on the live card for a few minutes
        self.assertIn(f"Accepted and uploaded to {where}", card["text"])
        self.assertEqual(card["buttons"], [])

        self.set_upload(status="failed", attempts=[{"outcome": "failed", "reason": upload_on_accept.REASON_SIGN_IN, "output": "secret-looking git output"}])
        card = self.render()["before"]
        self.assertIn("not uploaded", card["text"])
        self.assertIn(upload_on_accept.REASON_SIGN_IN, card["text"])
        self.assertNotIn("secret-looking git output", card["text"])
        self.assertEqual(card["buttons"], ["Upload again"])

        self.set_upload(status="diverged", attempts=[{"outcome": "diverged", "reason": upload_on_accept.REASON_DIVERGED}])
        card = self.render()["before"]
        self.assertIn("already has work this project does not have", card["text"])
        self.assertEqual(card["buttons"], ["Upload again"])

    def test_upload_again_is_a_real_click_that_ends_uploaded(self):
        self.accept()
        self.set_upload(status="failed", attempts=[{"outcome": "failed", "reason": upload_on_accept.REASON_NETWORK}])
        reading = self.render(PROBE.replace("location.search.includes('click') || window.__clickAgain", "true"))
        self.assertEqual(reading["before"]["buttons"], ["Upload again"])
        self.assertIn("Accepted and uploaded", reading["after"]["text"], json.dumps(reading))
        self.assertEqual(self.remote_tip(), self.main())
        self.assertEqual(len(self.upload()["attempts"]), 2)

    def test_a_project_without_a_destination_keeps_the_upload_approved_version_button(self):
        """The page's own card function, given an Accept that saved no upload, still offers today's button."""
        self.accept(destination=None)
        probe = """<script>(async()=>{
          const state={releases:{T:{status:'VISUAL_TEST_REQUIRED'}},release_decisions:{T:{decision:'accepted',upload:null}},git_acceptances:{T:{commit:'abc'}}};
          const holder=document.createElement('div');holder.innerHTML=releaseResponseHtml(state,'T');
          await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({before:{text:holder.textContent.trim(),buttons:Array.from(holder.querySelectorAll('button')).map(b=>b.textContent.trim())}})});
        })();</script>"""
        card = self.render(probe)["before"]
        self.assertEqual(card["buttons"], ["Upload approved version…"], json.dumps(card))
        self.assertNotIn("Uploading to", card["text"])


if __name__ == "__main__":
    unittest.main()
