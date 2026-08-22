# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Rendered proof: the version bar shows, the check answers, Update now applies."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import browser_acceptance, project_manager
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_update_check import build_origin_and_clone

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 60; attempt++) {
    const version = document.querySelector('#app-version');
    if (version && version.textContent !== '…') break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const shown = document.querySelector('#app-version').textContent;
  document.querySelector('#update-open').click();
  await new Promise(resolve => setTimeout(resolve, 300));
  const dialogOpen = document.querySelector('#update-dialog').open;
  let statusText = '', applyVisible = false;
  for (let attempt = 0; attempt < 60; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 200));
    statusText = document.querySelector('#update-status').textContent;
    applyVisible = !document.querySelector('#update-apply').hidden;
    if (/available|up to date|could not/.test(statusText)) break;
  }
  let updated = false;
  if (applyVisible) {
    document.querySelector('#update-apply').click();
    for (let attempt = 0; attempt < 60; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 250));
      if (/Updating and restarting/.test(document.querySelector('#update-status').textContent)) { updated = true; break; }
    }
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({shown, dialogOpen, statusText, applyVisible, updated})});
})();
</script>
"""


class RenderedUpdateTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_version_bar_check_and_one_click_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            origin, clone = build_origin_and_clone(base)
            manager = project_manager.ProjectManager(base / "home", board_port=0)
            manager.installation_root = clone
            restarts = []
            manager.request_restart = lambda: restarts.append(True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
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
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
                reading = sink.get("value") or {}
            finally:
                process.close()
            self.assertTrue(reading, "Chrome reported nothing")
            self.assertEqual(reading["shown"], "v0.1.0", "installed version not displayed")
            self.assertTrue(reading["dialogOpen"], "the update dialog did not open")
            self.assertIn("v0.1.6 is available", reading["statusText"])
            self.assertTrue(reading["applyVisible"], "Update now button not offered")
            self.assertTrue(reading["updated"], "Update now click did not start the update")
            self.assertEqual(restarts, [True], "restart seam did not fire")
            from harness import update_check
            self.assertEqual(update_check.installed_version(clone), "v0.1.6", "clone not updated")


if __name__ == "__main__":
    unittest.main()
