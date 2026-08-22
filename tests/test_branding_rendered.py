# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Rendered proof of the NoMoreHappyPath branding on both real pages.

Source-text greps missed the board's own top bar once already; only a real
headless-Chrome read of the rendered pages counts as seeing the brand.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, browser_acceptance, control, project_manager, project_memory
from harness.project_context import ProjectContext

BRAND = "NoMoreHappyPath"


from tests.environment_support import require_loopback  # noqa: F401 - shared guard

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('__BRAND_SELECTOR__')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const brand = document.querySelector('__BRAND_SELECTOR__');
  const logo = document.querySelector('__LOGO_SELECTOR__');
  const box = brand ? brand.getBoundingClientRect() : {width: 0, height: 0, top: -1};
  const logoBox = logo ? logo.getBoundingClientRect() : {width: 0, height: 0};
  let iconStatus = 0, iconType = '';
  try {
    const icon = await fetch('__ICON_PATH__', {cache: 'no-store'});
    iconStatus = icon.status; iconType = icon.headers.get('Content-Type') || '';
  } catch (error) {}
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      title: document.title,
      brandText: brand ? brand.textContent.trim() : '',
      brandVisible: box.width > 0 && box.height > 0 && box.top < 100,
      logoVisible: logoBox.width >= 20 && logoBox.height >= 20,
      logoSrc: logo ? logo.getAttribute('src') : '',
      iconStatus, iconType,
    }),
  });
})();
</script>
"""


def probe_proxy(target: str, sink: dict, script: str):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            return

        def reply(self, status, payload, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            try:
                with urlopen(target.rstrip("/") + self.path, timeout=15) as response:
                    payload = response.read()
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/json")
            except HTTPError as error:
                self.reply(error.code, error.read(), "application/json")
                return
            if content_type.startswith("text/html"):
                payload = payload.decode().replace("</body>", script + "</body>", 1).encode()
            self.reply(status, payload, content_type)

        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/__probe__":
                sink["value"] = json.loads(data or b"{}")
                self.reply(200, b"{}", "application/json")
                return
            request = Request(target.rstrip("/") + self.path, data=data, method="POST",
                              headers={"Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=15) as response:
                    self.reply(response.status, response.read(),
                               response.headers.get("Content-Type", "application/json"))
            except HTTPError as error:
                self.reply(error.code, error.read(), "application/json")

    return Proxy


class RenderedBrandingTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def render(self, target_url: str, script: str) -> dict:
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(target_url, sink, script))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name),
            width=1280, height=850,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no branding probe")
        return sink["value"]

    def assert_branded(self, reading: dict):
        self.assertEqual(reading["title"], BRAND, "tab title must be the brand alone")
        self.assertIn(BRAND, reading["brandText"])
        self.assertNotIn("Harness", reading["brandText"])
        self.assertTrue(reading["brandVisible"], "brand block not visibly rendered in the top bar")
        self.assertTrue(reading["logoVisible"], f"logo not visibly rendered: {reading}")
        self.assertIn("favicon.png", reading["logoSrc"])
        self.assertEqual(reading["iconStatus"], 200)
        self.assertIn("image/png", reading["iconType"])

    def test_projects_page_renders_the_brand_and_icon(self):
        home = self.base / "home"
        manager = project_manager.ProjectManager(home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        script = (PROBE.replace("__BRAND_SELECTOR__", ".brand")
                       .replace("__LOGO_SELECTOR__", ".brand-logo")
                       .replace("__ICON_PATH__", "/favicon.png"))
        reading = self.render(f"http://127.0.0.1:{server.server_address[1]}", script)
        self.assert_branded(reading)

    def test_open_project_board_renders_the_brand_and_icon(self):
        code = self.base / "code"; code.mkdir()
        context = ProjectContext(code, self.base / "data", self.base / "workspaces")
        control.initialize(context)
        board.snapshot(context)
        project_memory.initialize(context, project_name="Brand proof", description="Facts.")
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            context, project_name="Brand proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.base / "home", project_id="brand-proof",
            chat_action_token="brand-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        script = (PROBE.replace("__BRAND_SELECTOR__", ".project-brand")
                       .replace("__LOGO_SELECTOR__", ".project-brand img")
                       .replace("__ICON_PATH__", "/favicon.png"))
        reading = self.render(f"http://127.0.0.1:{server.server_address[1]}", script)
        self.assert_branded(reading)


if __name__ == "__main__":
    unittest.main()
