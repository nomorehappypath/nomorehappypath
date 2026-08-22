# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Rendered geometry regression tests for the Projects landing page.

These tests use the real manager HTTP surface and headless Chrome. Source-text
assertions cannot detect a project card whose grid row is shorter than its own
content, which was the root cause of the owner's clipped-project report.

Run: PYTHONPATH=. python3 -m unittest tests.test_project_manager_rendered -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import browser_acceptance, project_manager, project_registry as registry
from tests.environment_support import require_loopback


MEASURE_SCRIPT = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelectorAll('#projects .project').length) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await new Promise(resolve => setTimeout(resolve, 250));
  const list = document.querySelector('#projects');
  const cards = Array.from(document.querySelectorAll('#projects .project'));
  const documentBox = document.scrollingElement;
  const cardDetails = cards.map(card => {
    const box = card.getBoundingClientRect();
    return {
      left: +box.left.toFixed(1),
      right: +box.right.toFixed(1),
      top: +box.top.toFixed(1),
      bottom: +box.bottom.toFixed(1),
      width: +box.width.toFixed(1),
      height: +box.height.toFixed(1),
      clientHeight: card.clientHeight,
      naturalHeight: card.scrollHeight,
      hiddenPixels: card.scrollHeight - card.clientHeight,
    };
  });
  const listStyle = getComputedStyle(list);
  const listBox = list.getBoundingClientRect();
  list.scrollTop = list.scrollHeight;
  await new Promise(resolve => setTimeout(resolve, 100));
  const lastBox = cards[cards.length - 1].getBoundingClientRect();
  const result = {
    viewport: {width: innerWidth, height: innerHeight},
    document: {
      clientHeight: documentBox.clientHeight,
      scrollHeight: documentBox.scrollHeight,
      clientWidth: documentBox.clientWidth,
      scrollWidth: documentBox.scrollWidth,
    },
    list: {
      clientHeight: list.clientHeight,
      scrollHeight: list.scrollHeight,
      bottom: listBox.bottom,
      left: listBox.left,
      right: listBox.right,
      width: listBox.width,
      overflowY: listStyle.overflowY,
      gridAutoRows: listStyle.gridAutoRows,
      gridTemplateColumns: listStyle.gridTemplateColumns,
      gridTemplateRows: listStyle.gridTemplateRows,
    },
    cardDetails,
    lastCardAfterScroll: {bottom: lastBox.bottom, listBottom: listBox.bottom},
  };
  await fetch('/__layout_result__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(result),
  });
})();
</script>
"""

STOPPED_BOARD_SCRIPT = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (document.querySelector('#projects .project')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await new Promise(resolve => setTimeout(resolve, 250));
  const card = document.querySelector('#projects .project');
  const badges = Array.from(card.querySelectorAll('.badge')).map(node => ({
    text: node.textContent.trim(),
    className: node.className,
    title: node.getAttribute('title') || '',
  }));
  await fetch('/__layout_result__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      cardClass: card.className,
      badges,
      actions: card.querySelector('.actions').textContent.trim(),
    }),
  });
})();
</script>
"""


def chrome_binary() -> str:
    """Compatibility helper for modules that only need availability checks."""
    try:
        return browser_acceptance.resolve_binary()
    except FileNotFoundError as error:
        raise unittest.SkipTest(str(error)) from error


class SafeBrowserResolutionTests(unittest.TestCase):
    def executable(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("browser", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_configured_safe_binary_is_honored(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = self.executable(Path(temporary) / "chrome-headless-shell")
            with mock.patch.dict(os.environ, {"CHROME_BIN": str(browser)}):
                self.assertEqual(chrome_binary(), str(browser.resolve()))

    def test_configured_macos_app_bundle_is_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = self.executable(
                Path(temporary) / "Owner Browser.app" / "Contents" / "MacOS" / "Browser"
            )
            with (
                mock.patch.dict(os.environ, {"CHROME_BIN": str(browser)}),
                mock.patch.object(shutil, "which", return_value="/safe/fallback"),
            ):
                with self.assertRaisesRegex(ValueError, "outside every macOS .app"):
                    chrome_binary()

    def test_playwright_headless_shell_is_discovered_without_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            browser = self.executable(
                home
                / "Library/Caches/ms-playwright/chromium_headless_shell-9999"
                / "chrome-headless-shell-mac-arm64/chrome-headless-shell"
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(shutil, "which", return_value=None),
            ):
                self.assertEqual(chrome_binary(), str(browser.resolve()))

    def test_missing_safe_browser_skips_instead_of_opening_an_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(Path, "home", return_value=Path(temporary)),
                mock.patch.object(shutil, "which", return_value=None),
            ):
                with self.assertRaisesRegex(unittest.SkipTest, "no process-isolated"):
                    chrome_binary()


def proxy_handler(manager_url: str, result_sink: dict, injected_script: str | None = None):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            return

        def reply(self, status: int, payload: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            try:
                with urlopen(manager_url.rstrip("/") + self.path, timeout=15) as response:
                    payload = response.read()
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/json")
            except HTTPError as error:
                self.reply(error.code, error.read(), "application/json")
                return
            if self.path == "/":
                page = payload.decode().replace(
                    "</body>", (injected_script or MEASURE_SCRIPT) + "</body>", 1
                )
                payload = page.encode()
                content_type = "text/html; charset=utf-8"
            self.reply(status, payload, content_type)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length)
            if self.path != "/__layout_result__":
                request = Request(
                    manager_url.rstrip("/") + self.path,
                    data=data, method="POST",
                    headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                )
                try:
                    with urlopen(request, timeout=15) as response:
                        self.reply(
                            response.status, response.read(),
                            response.headers.get("Content-Type", "application/json"),
                        )
                except HTTPError as error:
                    self.reply(error.code, error.read(), "application/json")
                return
            result_sink["value"] = json.loads(data or b"{}")
            self.reply(200, b"{}", "application/json")

    return Proxy


class RenderedProjectsTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def measure(self, projects: int, width: int, height: int) -> dict:
        case_root = self.base / f"case-{projects}-{width}x{height}"
        home = case_root / "home"
        home.mkdir(parents=True)
        for index in range(projects):
            code_root = case_root / "code" / f"project-{index}"
            code_root.mkdir(parents=True)
            registry.register(
                home,
                f"Rendered Project {index}",
                code_root,
                kind="scaffold",
                description=(
                    "A realistic project description that wraps across the card so "
                    "the rendered test can detect content clipped by a shrinking row."
                ),
            )

        manager = project_manager.ProjectManager(home, board_port=0)
        manager_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager)
        )
        manager_thread = threading.Thread(
            target=manager_server.serve_forever, daemon=True
        )
        manager_thread.start()
        manager_url = f"http://127.0.0.1:{manager_server.server_address[1]}"

        result_sink: dict = {}
        proxy_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), proxy_handler(manager_url, result_sink)
        )
        proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
        proxy_thread.start()
        page_url = f"http://127.0.0.1:{proxy_server.server_address[1]}/"

        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)

        def launch(window_width: int, window_height: int) -> dict:
            result_sink.pop("value", None)
            process = browser_acceptance.launch(
                page_url, Path(profile.name), width=window_width, height=window_height,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if "value" in result_sink:
                        return result_sink["value"]
                    time.sleep(0.1)
                raise AssertionError(
                    f"Chrome did not report Projects geometry for {width}x{height}"
                )
            finally:
                process.close()

        try:
            requested_width, requested_height = width, height
            reading = launch(requested_width, requested_height)
            for _ in range(4):
                viewport = reading["viewport"]
                width_delta = width - viewport["width"]
                height_delta = height - viewport["height"]
                if abs(width_delta) <= 1 and abs(height_delta) <= 1:
                    break
                requested_width += width_delta
                requested_height += height_delta
                reading = launch(requested_width, requested_height)
            self.assertAlmostEqual(reading["viewport"]["width"], width, delta=1)
            self.assertAlmostEqual(reading["viewport"]["height"], height, delta=1)
            return reading
        finally:
            proxy_server.shutdown()
            proxy_thread.join(timeout=3)
            proxy_server.server_close()
            manager_server.shutdown()
            manager_thread.join(timeout=3)
            manager_server.server_close()

    def test_project_cards_keep_natural_height_inside_the_scroll_window(self):
        for projects, height in ((2, 622), (3, 900), (6, 622)):
            with self.subTest(projects=projects, height=height):
                reading = self.measure(projects, 1370, height)
                hidden = [card["hiddenPixels"] for card in reading["cardDetails"]]
                self.assertLessEqual(
                    max(hidden),
                    4,
                    f"project cards hide their own details: {reading['cardDetails']}",
                )
                self.assertEqual(reading["list"]["gridAutoRows"], "max-content")
                self.assertEqual(reading["list"]["overflowY"], "auto")
                self.assertLessEqual(
                    reading["document"]["scrollHeight"],
                    reading["document"]["clientHeight"] + 1,
                )
                self.assertLessEqual(
                    reading["document"]["scrollWidth"],
                    reading["document"]["clientWidth"] + 1,
                )
                self.assertLessEqual(
                    reading["lastCardAfterScroll"]["bottom"],
                    reading["lastCardAfterScroll"]["listBottom"] + 1,
                )
                if projects == 6:
                    self.assertGreater(
                        reading["list"]["scrollHeight"],
                        reading["list"]["clientHeight"],
                    )

    def test_all_projects_window_shows_two_vertical_rows_before_scrolling(self):
        for height in (622, 900):
            with self.subTest(height=height):
                reading = self.measure(3, 1370, height)
                first, second = reading["cardDetails"][:2]
                self.assertGreater(second["top"], first["bottom"])
                self.assertAlmostEqual(first["left"], second["left"], delta=1)
                self.assertAlmostEqual(first["width"], second["width"], delta=1)
                self.assertLessEqual(second["bottom"], reading["list"]["bottom"] - 1)
                self.assertEqual(len(reading["list"]["gridTemplateColumns"].split()), 1)

    def test_stopped_board_renders_one_truthful_badge_and_resume_action(self):
        home = self.base / "stopped-home"
        code_root = self.base / "stopped-code"
        code_root.mkdir(parents=True)
        entry = registry.register(
            home, "Stopped board project", code_root, kind="scaffold",
            description="Its board worker exited after startup.",
        )
        manager = project_manager.ProjectManager(home, board_port=0)
        failure = "the board worker stopped unexpectedly (exit 17)"
        manager.worker_failure = {"project_id": entry["id"], "message": failure}

        manager_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), project_manager.make_handler(manager),
        )
        manager_thread = threading.Thread(target=manager_server.serve_forever, daemon=True)
        manager_thread.start()
        manager_url = f"http://127.0.0.1:{manager_server.server_address[1]}"

        result_sink: dict = {}
        proxy_server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            proxy_handler(manager_url, result_sink, STOPPED_BOARD_SCRIPT),
        )
        proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
        proxy_thread.start()
        page_url = f"http://127.0.0.1:{proxy_server.server_address[1]}/"

        profile = tempfile.TemporaryDirectory()
        process = browser_acceptance.launch(page_url, Path(profile.name), width=1400, height=1000)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in result_sink:
                time.sleep(0.1)
            self.assertIn("value", result_sink, "Chrome did not render the stopped project card")
            rendered = result_sink["value"]
        finally:
            process.close()
            profile.cleanup()
            proxy_server.shutdown()
            proxy_thread.join(timeout=3)
            proxy_server.server_close()
            manager_server.shutdown()
            manager_thread.join(timeout=3)
            manager_server.server_close()

        stopped = [badge for badge in rendered["badges"] if badge["text"] == "Board stopped"]
        self.assertEqual(len(stopped), 1, rendered["badges"])
        self.assertIn("stopped", rendered["cardClass"].split())
        self.assertIn("stopped", stopped[0]["className"].split())
        self.assertEqual(stopped[0]["title"], failure)
        self.assertIn("Open project", rendered["actions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
