# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The inbox relay checks who is on the other end of each connection BEFORE it sends anything.

Security review 2026-10-04 (P4, live): the relay wrote the session's
authentication frame and the message to whatever listened on the inbox path,
and only then reported who that was. Now:
- a post goes only to the CLI the supervisor verified (the connected
  socket's peer, so a path swapped after the check cannot redirect it);
- the relay talks only to a handover listener that is an ANCESTOR of the
  process serving the inbox - the supervisor that started the CLI.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import inbox_relay

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "harness" / "inbox_relay.py"


def _short_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hnrelay-")


class Listener:
    """A Unix socket in THIS process that records every byte it is sent."""

    def __init__(self, path: str):
        self.received = b""
        self.connected = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(4)
        self.server.settimeout(10)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            connection, _ = self.server.accept()
        except OSError:
            return
        self.connected.set()
        connection.settimeout(3)
        try:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                self.received += chunk
        except OSError:
            pass
        connection.close()

    def close(self):
        self.server.close()


INBOX_SERVER = ("import os, socket, sys, time\n"
                "s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(4)\n"
                "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
                "time.sleep(20)\n")


class HandoverSendOrderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = _short_dir()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.handover = Listener(str(self.base / "h.sock"))
        self.addCleanup(self.handover.close)

    def start_inbox(self, *, detached: bool) -> None:
        pid_file = self.base / "inbox.pid"
        command = [sys.executable, "-c", INBOX_SERVER, str(self.base / "inbox.sock"), str(pid_file)]
        if detached:
            # Not started by the handover's process: re-parented away from it.
            command = ["/bin/sh", "-c", '"$@" >/dev/null 2>&1 &', "sh", *command]
            subprocess.run(command, check=True)
        else:
            child = subprocess.Popen(command)
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        self.assertTrue(pid_file.exists())
        if detached:
            pid = int(pid_file.read_text())
            self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))

    def run_relay(self) -> int:
        environment = {**os.environ, "CLAUDE_CODE_MESSAGING_SOCKET": str(self.base / "inbox.sock"),
                       "CLAUDE_CODE_MESSAGING_TOKEN": "tok-relay", "HARNESS_MANAGED_SESSION": "session-1"}
        relay = subprocess.Popen([sys.executable, "-E", str(RELAY), str(self.base / "h.sock"), "transcript"], env=environment)
        self.addCleanup(lambda: relay.poll() is None and relay.kill())
        try:
            return relay.wait(timeout=8)
        except subprocess.TimeoutExpired:
            return -1                                 # still serving: it accepted this handover

    def test_a_handover_listener_that_did_not_start_the_cli_is_sent_nothing(self):
        self.start_inbox(detached=True)
        self.assertEqual(self.run_relay(), 2, "the relay refuses and exits")
        self.handover.connected.wait(3)
        time.sleep(0.5)
        self.assertEqual(self.handover.received, b"", "not even the hello")

    def test_control_the_supervisor_that_started_the_cli_gets_the_hello(self):
        self.start_inbox(detached=False)
        self.run_relay()
        self.handover.connected.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b"hello" not in self.handover.received:
            time.sleep(0.05)
        self.assertIn(b'"type": "hello"', self.handover.received)


class PostSendOrderTests(unittest.TestCase):
    def test_a_post_reaches_only_the_verified_listener(self):
        with _short_dir() as directory:
            inbox = Listener(os.path.join(directory, "inbox.sock"))
            self.addCleanup(inbox.close)
            with self.assertRaises(inbox_relay.ListenerRefused):
                inbox_relay._post(os.path.join(directory, "inbox.sock"), "tok", "secret text", expected_listener=os.getpid() + 999999)
            inbox.connected.wait(3)
            time.sleep(0.3)
            self.assertEqual(inbox.received, b"", "no auth frame, no message")

    def test_control_the_verified_listener_receives_the_post(self):
        with _short_dir() as directory:
            inbox = Listener(os.path.join(directory, "inbox.sock"))
            self.addCleanup(inbox.close)
            self.assertEqual(inbox_relay._post(os.path.join(directory, "inbox.sock"), "tok", "hello", expected_listener=os.getpid()),
                             os.getpid())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and b"hello" not in inbox.received:
                time.sleep(0.05)
            self.assertIn(b'"type": "auth"', inbox.received)


if __name__ == "__main__":
    unittest.main()
