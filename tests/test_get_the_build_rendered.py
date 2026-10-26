# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome): the owner gets the program, not a View app that cannot start.

Run:  PYTHONPATH=. python3 -m unittest tests.test_get_the_build_rendered -v

Owner 2026-10-21: "the view app button ... 'There is nothing here we know how to start' ... if the app is on the VM we
need a way". A program task shows Get the build (file, size, SHA-256, where and from which commit it was built,
the steps), no View app, and a download that is exactly the certified bytes; History keeps it; a damaged file says so.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from harness import board, board_viewer, browser_acceptance, build_collection
from tests import test_get_the_build as support
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelector('.task .release-build') || document.querySelector('.history-item .release-build')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const block = document.querySelector('.task .release-build');
  const link = block ? block.querySelector('a.build-download') : null;
  let status = 0, length = 0, digest = '', type = '';
  if (link) {
    const response = await fetch(link.getAttribute('href'), {cache: 'no-store'});
    status = response.status; type = response.headers.get('Content-Type') || '';
    const bytes = new Uint8Array(await response.arrayBuffer());
    length = bytes.length;
    digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))).map(b => b.toString(16).padStart(2, '0')).join('');
  }
  const history = document.querySelector('#history');
  if (history && !history.open) history.open = true;
  let historyText = '';
  for (let attempt = 0; attempt < 60; attempt++) {
    const item = document.querySelector('.history-item .release-build');
    if (item) { historyText = item.textContent; break; }
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    blockVisible: visible(block), blockText: block ? block.innerText : '', hasViewApp: Boolean(document.querySelector('.release-response button.view-app')),
    seeItRunning: document.body.innerText.includes('See it running before you decide'),
    linkText: link ? link.innerText : '', href: link ? link.getAttribute('href') : '', downloadAttribute: link ? link.hasAttribute('download') : false,
    status, length, digest, type, historyText,
    acceptButtons: Array.from(document.querySelectorAll('.release-response button')).map(node => node.innerText),
  })});
})();
</script>
"""


class RenderedBuildTests(support.MachineFixture):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        super().setUp()
        self.build(("why.bin", "why.txt"))
        board.collect_build(self.root, support.TASK, self.cto["id"])
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS} | {"head_commit": "c" * 40, "build_required": True, "build_collected": True}
        board.record_release_ready(self.root, self.cto["id"], support.TASK, checks)
        self.manifest_id = board.snapshot(self.root)["builds"][support.TASK]["manifest_id"]

    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root, project_name="Get the build proof"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def render(self) -> dict:
        server = self.serve()
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

    def test_the_owner_sees_get_the_build_and_no_view_app_and_downloads_the_certified_bytes(self):
        reading = self.render()
        self.assertTrue(reading["blockVisible"], reading)
        text = reading["blockText"]
        for needle in ("Get the build", "why.bin", "why.txt", support.sha(support.CONTENT["why.bin"]), "Linux x86-64", "chmod +x why.bin", "a usage message",
                       "cannot confirm who made this program", "Built and tested", "c" * 10):
            self.assertIn(needle, text)
        self.assertFalse(reading["hasViewApp"], "View app is not shown for a program")
        self.assertFalse(reading["seeItRunning"])
        self.assertEqual(reading["linkText"], "Download")
        self.assertTrue(reading["downloadAttribute"])
        self.assertIn(f"/api/builds/{self.manifest_id}/why.bin", reading["href"])
        self.assertEqual((reading["status"], reading["length"], reading["digest"]), (200, len(support.CONTENT["why.bin"]), support.sha(support.CONTENT["why.bin"])))
        self.assertEqual(reading["type"], "application/octet-stream")
        self.assertIn("Accepted", reading["acceptButtons"], "the owner can still accept")

    def test_the_real_server_sends_an_attachment_and_refuses_what_is_not_the_build(self):
        server = self.serve()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(f"{base}/api/builds/{self.manifest_id}/why.bin", timeout=20) as response:
            body = response.read()
            self.assertEqual(body, support.CONTENT["why.bin"])
            self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="why.bin"')
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["Content-Length"], str(len(body)))
        for path in (f"/api/builds/{self.manifest_id}/manifest.json", f"/api/builds/{self.manifest_id}/..%2Fx", "/api/builds/b-0000000000000000/why.bin", f"/api/builds/{self.manifest_id}"):
            with self.assertRaises(HTTPError, msg=path) as raised:
                urlopen(base + path, timeout=20)
            self.assertEqual(raised.exception.code, 404, path)
        file = build_collection.manifest_path(self.root, support.TASK, self.manifest_id).parent / "why.bin"
        file.write_bytes(support.CONTENT["why.bin"][:-1] + b"X")
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{base}/api/builds/{self.manifest_id}/why.bin", timeout=20)
        self.assertEqual(raised.exception.code, 409)
        self.assertIn("no longer matches", json.loads(raised.exception.read())["error"])

    def test_a_damaged_file_is_explained_and_not_offered(self):
        (build_collection.manifest_path(self.root, support.TASK, self.manifest_id).parent / "why.bin").unlink()
        reading = self.render()
        self.assertIn("cannot be downloaded now", reading["blockText"])
        self.assertEqual(reading["linkText"], "")
        self.assertFalse(reading["hasViewApp"])

    def test_history_keeps_the_build_after_acceptance(self):
        with board.locked_state(self.root) as state:
            state["release_decisions"][support.TASK] = {"task": support.TASK, "decision": "accepted", "recorded_at": board.now()}
            state["releases"][support.TASK]["status"] = "RELEASE_ACCEPTED"
        reading = self.render()
        self.assertIn("why.bin", reading["historyText"])
        self.assertIn(support.sha(support.CONTENT["why.bin"]), reading["historyText"])


from tests.release_fixture_support import allow_fixture_releases  # noqa: E402

setUpModule, tearDownModule = allow_fixture_releases()   # bare release fixtures: production demands a final review (2026-10-24)


if __name__ == "__main__":
    unittest.main()
