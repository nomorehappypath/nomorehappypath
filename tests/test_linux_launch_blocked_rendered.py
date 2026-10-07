# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof: a Linux agent that cannot start tells the owner why, on the page.

On Ubuntu 24.04 the Dev, CTO and Reviewer opened a tmux session that vanished
with its own error. Now the launch is refused first and the real page shows the
plain reason. The page is the real board in headless Chrome and the Start
button's own handler runs; only the machine's answer to "can agents start" is
fixed, because this test machine is not necessarily Linux.
"""
from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board_viewer, browser_acceptance, control, platform_support
from harness.platform_support import linux
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

REASON = ("Linux is blocking the sandbox the agents run in (bwrap: setting up uid map: Permission denied). "
          "Fix it once, in Linux, with:  sudo bash /opt/nmhp/scripts/linux_enable_sandbox.sh")

PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 200 && typeof start !== 'function'; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await start('codex_delivery', 'blue');
  const notice = document.querySelector('#notice');
  const box = notice ? notice.getBoundingClientRect() : {width: 0, height: 0};
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: notice ? notice.textContent.trim() : '', visible: box.width > 0 && box.height > 0}),
  });
})();
</script>
"""


class LinuxLaunchBlockedRenderedTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main"], cwd=self.root, check=True, capture_output=True)
        self.addCleanup(mock.patch.stopall)

    def test_the_owner_reads_the_reason_and_no_session_is_left_running(self):
        mock.patch.object(platform_support, "terminal_host", return_value=linux.TERMINAL_HOST).start()
        mock.patch.object(linux, "launch_problem", return_value=REASON).start()
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Launch blocked", manager_url="http://127.0.0.1:1/",
            settings_home=self.root / ".harness" / "home", project_id="launch-blocked",
            chat_action_token="blocked-token",
        ))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(
            f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        reading = sink["value"]
        self.assertTrue(reading["visible"], "the notice has no geometry: the owner sees nothing")
        self.assertIn("Could not launch session", reading["text"])
        self.assertIn("setting up uid map: Permission denied", reading["text"])
        self.assertIn("scripts/linux_enable_sandbox.sh", reading["text"])
        self.assertEqual([s["status"] for s in control.snapshot(self.root)["sessions"]], ["failed"])


if __name__ == "__main__":
    unittest.main()
