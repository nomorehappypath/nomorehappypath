# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Typing into an agent from Mission Control on macOS (2026-10-27_view-agent-input-500).

The macOS terminal host had no input_session, so POST /api/sessions/<id>/input returned a 500
("'_TerminalHost' object has no attribute 'input_session'"), and a headless app still raised a Terminal window on
View agent. These tests use a REAL detached `screen` session running a fake child (never a Claude or Codex agent).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import agent_view
from harness.platform_support import defaults

CHILD = r'''
import os, sys, time, tty
tty.setraw(0)
os.write(1, b"AGENT READY\r\n")
data = b""
end = time.time() + 20
while time.time() < end and len(data) < int(sys.argv[2]):
    data += os.read(0, 100)
open(sys.argv[1], "wb").write(data)
time.sleep(3)
'''


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/bin/screen"), "macOS screen host")
class ViewAgentInputTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="vai-", dir="/tmp"))
        self.addCleanup(self.cleanup)
        self.root = self.base / "p"
        self.root.mkdir()
        self.received = self.base / "received.bin"
        (self.base / "child.py").write_text(CHILD)
        self.session_id = "codex_delivery-vai" + str(int(time.time() * 1000) % 100000)
        environment = {**os.environ, defaults.HEADLESS_ENVIRONMENT: "1"}
        self.windows = mock.patch.object(defaults.subprocess, "run", wraps=subprocess.run)
        with mock.patch.dict(os.environ, environment, clear=True):
            self.spawn = self.windows.start()
            self.addCleanup(self.windows.stop)
            defaults.TERMINAL_HOST.open_session(
                self.session_id, [sys.executable, str(self.base / "child.py"), str(self.received), "11", "--data-root", str(self.root / ".harness")],
                color_rgb=(0, 0, 0))
        self.sockets = self.root / ".harness" / "control" / "terminal-sockets"
        snapshot = {"sessions": [{"id": self.session_id, "status": "running"}]}
        patcher = mock.patch.object(agent_view.control, "snapshot", return_value=snapshot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cleanup(self):
        subprocess.run(["/usr/bin/screen", "-S", "nmhp-" + self.session_id, "-X", "quit"],
                       env={**os.environ, "SCREENDIR": str(self.sockets)}, capture_output=True, timeout=10)
        shutil.rmtree(self.base, ignore_errors=True)

    def wait_for(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return False

    def test_typing_from_mission_control_reaches_the_agent(self):
        # Old code: AttributeError on the macOS host (a 500). Keys, Enter and Ctrl+C must arrive byte for byte.
        self.assertTrue(self.wait_for(lambda: "AGENT READY" in agent_view.capture(self.root, self.session_id)["screen"]))
        self.assertEqual(agent_view.send_input(self.root, self.session_id, "héllo\r\x03\x1b[A"), {"sent": True})
        self.assertTrue(self.wait_for(lambda: self.received.exists() and self.received.stat().st_size >= 11))
        self.assertEqual(self.received.read_bytes(), "héllo\r\x03\x1b[A".encode())

    def test_headless_view_shows_the_screen_and_opens_no_window(self):
        with mock.patch.dict(os.environ, {defaults.HEADLESS_ENVIRONMENT: "1"}):
            self.spawn.reset_mock()
            with mock.patch.object(defaults.TERMINAL_HOST, "view_session") as native:
                opened = agent_view.open_view(self.root, self.session_id)
        self.assertEqual(opened["mode"], "browser")
        native.assert_not_called()
        self.assertFalse([c for c in self.spawn.call_args_list if "osascript" in str(c.args[0][0])])

    def test_view_without_headless_still_opens_the_terminal_window(self):
        environment = {k: v for k, v in os.environ.items() if k != defaults.HEADLESS_ENVIRONMENT}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                defaults.TERMINAL_HOST, "view_session", return_value={"mode": "native", "session_id": self.session_id}) as native:
            self.assertEqual(agent_view.open_view(self.root, self.session_id)["mode"], "native")
        native.assert_called_once()

    def test_input_that_cannot_be_delivered_is_a_plain_error_not_a_crash(self):
        subprocess.run(["/usr/bin/screen", "-S", "nmhp-" + self.session_id, "-X", "quit"],
                       env={**os.environ, "SCREENDIR": str(self.sockets)}, capture_output=True, timeout=10)
        self.assertTrue(self.wait_for(lambda: not list(self.sockets.glob("*." + "nmhp-" + self.session_id))))
        with self.assertRaisesRegex(ValueError, "live terminal is unavailable"):
            agent_view.send_input(self.root, self.session_id, "x")
        with self.assertRaisesRegex(ValueError, "live terminal is unavailable"):
            agent_view.capture(self.root, self.session_id)
        # A host with no input support at all also answers in words (ValueError becomes a 400), never AttributeError.
        with mock.patch.object(agent_view.platform_support, "terminal_host", return_value=object()):
            with self.assertRaisesRegex(ValueError, "cannot send input"):
                agent_view.send_input(self.root, self.session_id, "x")


if __name__ == "__main__":
    unittest.main()
