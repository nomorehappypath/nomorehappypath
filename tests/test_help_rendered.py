# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Rendered proof that the Help page shows its guidance, not just carries it."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import browser_acceptance, project_manager
from tests.test_branding_rendered import probe_proxy, require_loopback
from tests.environment_support import require_loopback

PROBE = r"""
<script>
(async () => {
  document.querySelector('[data-page="help"]').click();
  for (let attempt = 0; attempt < 50; attempt++) {
    const page = document.querySelector('#help-page');
    if (page && !page.hidden) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const titles = Array.from(document.querySelectorAll('#help-page h2'))
    .filter(node => node.getBoundingClientRect().width > 0)
    .map(node => node.textContent.trim());
  const tables = document.querySelectorAll('#help-page .help-table').length;
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({titles, tables, hidden: document.querySelector('#help-page').hidden}),
  });
})();
</script>
"""


class RenderedHelpTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def test_help_page_visibly_renders_every_guide_section(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            manager = project_manager.ProjectManager(home, board_port=0)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), project_manager.make_handler(manager),
            )
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
                Path(profile.name), width=1280, height=900,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
            finally:
                process.close()
        reading = sink.get("value") or {}
        self.assertTrue(reading, "Chrome reported nothing for the Help page")
        self.assertFalse(reading["hidden"], "Help page did not open")
        for title in (
            "What This App Needs To Run",
            "Switch On Project Chat (OpenAI Key)",
            "Configure The Agents (Settings)",
            "Frame A Good Task",
            "Talking To The Agents Directly",
            "Why It Takes Its Time (No Happy Path)",
            "Pause, Close, Or Remove A Project",
            "Your Responsibility, And What The Agents Can Do",
            "When Something Looks Wrong",
            "Ask About This Project",
            "Legal — No Warranty, No Liability",
        ):
            self.assertIn(title, reading["titles"], f"section not visibly rendered: {title}")
        self.assertGreaterEqual(reading["tables"], 3, "help tables missing from render")


if __name__ == "__main__":
    unittest.main()
