# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof for backlog #12: one View app button runs the reviewed candidate.

Real board page, real worker endpoint, real preview supervisor, real headless
Chrome. The probe clicks View app on the release card, watches the spinner and
elapsed seconds, and reports where the new tab was sent; the test then fetches
that address and checks it is the reviewed candidate running with the owner's
settings. window.open is recorded instead of opened because a headless page
cannot open a tab without a real user gesture; everything behind it is real.

Run:  PYTHONPATH=. python3 -m unittest tests.test_view_app_rendered -v
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
from urllib.request import urlopen

from harness import board, board_viewer, browser_acceptance, release_preview
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_release_preview import STUDIO_APP, _git

PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const button = () => document.querySelector('.release-response .view-app');
  for (let attempt = 0; attempt < 300 && !button(); attempt++) await new Promise(r => setTimeout(r, 100));
  const card = document.querySelector('.release-preview');
  const before = {
    buttons: Array.from(document.querySelectorAll('.release-preview button')).map(b => b.textContent.trim()),
    inputs: document.querySelectorAll('.release-preview input').length,
    visible: visible(button()),
    fontSize: button() ? parseFloat(getComputedStyle(button()).fontSize) : 0,
    text: (card?.textContent || '').replace(/\s+/g, ' ').trim(),
  };
  let sentTo = '';
  const tab = {closed: false, document: {title: '', body: {}}, location: {set href(value) { sentTo = value; }}, close() { this.closed = true; }};
  window.open = () => tab;
  button()?.click();
  const seen = [];
  const failedLine = () => (document.querySelector('.release-preview .preview-hint')?.textContent || '').startsWith('The app could not start');
  for (let attempt = 0; attempt < 900 && !sentTo && !failedLine(); attempt++) {
    await new Promise(r => setTimeout(r, 100));
    const hint = (document.querySelector('.release-preview .preview-hint')?.textContent || '').trim();
    if (hint && !seen.includes(hint)) seen.push(hint);
  }
  const again = button();
  const after = {sentTo, seen, spinnerWhileStarting: seen.some(line => /Starting the app… \d+ s/.test(line)),
                 hint: (document.querySelector('.release-preview .preview-hint')?.textContent || '').trim(),
                 runYourself: (document.querySelector('.release-preview .preview-run')?.textContent || '').trim(),
                 tab: {closed: tab.closed, title: tab.document.title, body: String(tab.document.body.innerHTML || '')},
                 retryButton: again ? {text: again.textContent.trim(), disabled: again.disabled, visible: visible(again)} : null};
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({before, after})});
})();
</script>
"""


class RenderedViewAppTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.root = base / "studio"
        self.root.mkdir()
        (self.root / "settings.json").write_text('{"owner": "gerard"}')
        (self.root / ".env").write_text("RELAY_TOKEN=owner-secret\n")
        (self.root / ".workspace" / "client-a").mkdir(parents=True)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        _git(self.workspace, "init", "-q")
        (self.workspace / "app.py").write_text(STUDIO_APP)
        (self.workspace / "version.txt").write_text("candidate-studio")
        _git(self.workspace, "add", "app.py", "version.txt")
        _git(self.workspace, "commit", "-qm", "studio candidate")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})["TASK"] = {
                "task": "TASK", "status": "VISUAL_TEST_REQUIRED", "head_commit": self.commit,
                "cto_id": "cto-0001-test", "recorded_at": board.now(),
                "owner_test_steps": ["Open the new expert page"],
            }
            state.setdefault("task_workspaces", {})["TASK"] = str(self.workspace)

    def click_view_app(self) -> dict:
        supervisor = release_preview.ReleasePreviewSupervisor(self.root, tick_seconds=3600)
        supervisor.start()
        self.addCleanup(supervisor.shutdown)
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="View app proof", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="view-app-proof",
            chat_action_token="view-app-token",
        ))
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
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        return sink["value"]

    def test_view_app_click_opens_the_running_reviewed_candidate(self):
        value = self.click_view_app()
        before, after = value["before"], value["after"]
        self.assertTrue(before["visible"], "a blank render is not a pass: " + json.dumps(before))
        self.assertEqual(before["buttons"], ["View app"], before)
        self.assertEqual(before["inputs"], 0, "no command or port box")
        self.assertGreaterEqual(before["fontSize"], 12)
        self.assertNotIn("{port}", before["text"])
        self.assertTrue(after["spinnerWhileStarting"], after)
        self.assertTrue(after["sentTo"].startswith("http://127.0.0.1:"), after)
        with urlopen(after["sentTo"], timeout=5) as response:
            served = json.loads(response.read())
        self.assertEqual(served["version"], "candidate-studio")
        self.assertEqual(served["settings"], '{"owner": "gerard"}')
        self.assertEqual(served["workspace"], ["client-a"])
        self.assertEqual(board.snapshot(self.root)["releases"]["TASK"]["preview"]["status"], "ready")


    def configure(self, command: str) -> None:
        from harness import workspace_settings
        workspace_settings.update_preview(self.root, {
            "command": command, "url_template": "http://127.0.0.1:{port}/", "startup_timeout_seconds": 20,
        })

    def test_a_configured_command_click_reaches_the_app(self):
        # Review r1 B1: this path stayed on "Starting…" with no URL.
        from tests.test_release_preview import SERVE_SCRIPT
        (self.workspace / "serve.py").write_text(SERVE_SCRIPT)
        _git(self.workspace, "add", "serve.py")
        _git(self.workspace, "commit", "-qm", "serve script")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        with board.locked_state(self.root) as state:
            state["releases"]["TASK"]["head_commit"] = self.commit
        self.configure("python3 serve.py --port {port}")
        after = self.click_view_app()["after"]
        self.assertTrue(after["sentTo"].startswith("http://127.0.0.1:"), after)
        with urlopen(after["sentTo"], timeout=5) as response:
            self.assertEqual(response.read().decode(), "candidate-studio")

    def test_a_failed_start_shows_one_line_the_command_and_a_retry(self):
        self.configure("python3 -c 'import sys; sys.exit(3)' --port {port}")
        after = self.click_view_app()["after"]
        self.assertEqual(after["sentTo"], "")
        self.assertTrue(after["hint"].startswith("The app could not start: "), after)
        self.assertRegex(after["runYourself"], r"^cd .+/source && python3 -c 'import sys; sys\.exit\(3\)' --port \d+$")
        self.assertEqual(after["retryButton"], {"text": "View app", "disabled": False, "visible": True})
        # Owner, 2026-10-02: the tab never opens empty and vanishes; it says why.
        self.assertFalse(after["tab"]["closed"], after)
        self.assertEqual(after["tab"]["title"], "The app could not start")
        self.assertIn("The preview command exited before serving its URL", after["tab"]["body"])
        self.assertIn("a command to run it yourself", after["tab"]["body"])

    def test_an_app_that_ignores_port_opens_where_it_really_listens(self):
        # The owner's failing app (project temp, zip-temperature-web) read its own
        # port variable with a fixed default; the click showed a blank tab that vanished.
        import os
        from unittest import mock
        from tests.test_release_preview import OWN_PORT_APP, _free_loopback_port
        own_port = _free_loopback_port()
        (self.workspace / "app.py").write_text(OWN_PORT_APP)
        _git(self.workspace, "commit", "-qam", "app with its own port")
        self.commit = _git(self.workspace, "rev-parse", "HEAD")
        with board.locked_state(self.root) as state:
            state["releases"]["TASK"]["head_commit"] = self.commit
        with mock.patch.dict(os.environ, {"OWN_APP_PORT": str(own_port)}):
            after = self.click_view_app()["after"]
        self.assertEqual(after["sentTo"], f"http://127.0.0.1:{own_port}/", after)
        with urlopen(after["sentTo"], timeout=5) as response:
            self.assertEqual(response.read(), b"zip temperature")


if __name__ == "__main__":
    unittest.main()
