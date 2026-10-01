# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stop all always finishes, even when a CLI is stuck exiting (owner bug, 2026-10-01).

After Stop all, two Claude CLIs sat in macOS state "?Es" (stuck in kernel
exit). Their supervisors blocked for ever in an unbounded wait() after SIGKILL,
so the agents never went offline and control kept them "stopping".

The stuck child is reproduced in a REAL supervisor process: a sitecustomize
on the supervisor's path makes the CLI child's Popen behave like an exiting
process the OS never reaps (poll() keeps answering "running", wait() never
returns), while the real child ignores SIGTERM and dies on SIGKILL.
"""
from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, control, interactive_supervisor

ROOT = Path(__file__).resolve().parents[1]
MARKER = "STUCK-EXIT-CHILD"
STUCK_POPEN = textwrap.dedent(f'''
    import subprocess, time
    _Popen = subprocess.Popen
    class _StuckExit(_Popen):
        """A child the OS never finishes reaping after it is killed (macOS "?Es")."""
        def _stuck(self):
            return any("{MARKER}" in str(part) for part in (self.args or []))
        def poll(self):
            return None if self._stuck() else super().poll()
        def wait(self, timeout=None):
            if not self._stuck():
                return super().wait(timeout)
            if timeout is None:
                while True:
                    time.sleep(3600)
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(self.args, timeout)
    subprocess.Popen = _StuckExit
''')


class SupervisorStuckChildTests(unittest.TestCase):
    def test_a_child_stuck_exiting_never_holds_the_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            shim = Path(tmp) / "shim"
            shim.mkdir()
            (shim / "sitecustomize.py").write_text(STUCK_POPEN, encoding="utf-8")
            session = control.create(root, "claude_cto")
            agent = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            cli = f"import signal,time,sys; signal.signal(signal.SIGTERM, lambda *_: None); sys.argv.append('{MARKER}'); time.sleep(60)"
            env = {**os.environ, "PYTHONPATH": f"{shim}{os.pathsep}{ROOT}"}
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root),
                 "--session-id", session["id"], "--agent-id", agent["id"], "--",
                 sys.executable, "-c", cli, MARKER],
                stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=env,
            )
            os.close(slave)
            try:
                self._read_until(master, b"interactive supervisor ready")
                time.sleep(.3)
                started = time.monotonic()
                os.kill(process.pid, signal.SIGTERM)
                # 1 s grace + 5 s bounded wait after SIGKILL; never "for ever".
                code = process.wait(timeout=20)
                elapsed = time.monotonic() - started
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                os.close(master)
            self.assertLess(elapsed, 15, "the supervisor returned within its bound")
            self.assertEqual(code, interactive_supervisor.STUCK_EXIT_CODE)
            self.assertNotIn(agent["id"], {
                key for key, value in board.snapshot(root)["agents"].items() if value.get("active")
            }, "the agent is offline")
            transcript = next((root / ".harness").rglob(f"*{session['id']}*")).read_text(encoding="utf-8", errors="replace")
            self.assertIn("did not finish exiting after it was stopped", transcript)

    def test_a_normal_child_still_stops_as_before(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            self.assertTrue(interactive_supervisor._stop_child_group(child))
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    READ_TIMEOUT_CEILING = 30

    def _read_until(self, master, needle):
        deadline, output = time.monotonic() + self.READ_TIMEOUT_CEILING, b""
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], .1)
            if master in readable:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if needle in output:
                return output
        self.fail(f"did not receive {needle!r}; got {output[-400:]!r}")


class ControlStaleStoppingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def stopping(self, kind, seconds_ago, field="stop_requested_at", status="stopping"):
        session = control.create(self.root, kind)
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].update({
                "status": status, "pid": os.getpid(),  # a live process: the supervisor is still there
                field: (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(),
            })
        return session["id"]

    def test_a_stop_overdue_with_a_live_supervisor_is_recorded_stopped(self):
        stuck = self.stopping("claude_reviewer", control.STOP_RECONCILE_SECONDS + 5)
        fresh = self.stopping("codex_delivery", 2)
        paused = self.stopping("claude_cto", control.STOP_RECONCILE_SECONDS + 5, "pause_requested_at", "pausing")
        snapshot = control.snapshot(self.root)
        by_id = {item["id"]: item for item in snapshot["sessions"]}
        self.assertEqual(by_id[stuck]["status"], "stopped")
        self.assertIn("did not finish exiting", by_id[stuck]["reason"])
        self.assertTrue(by_id[stuck]["ended_at"])
        self.assertEqual(by_id[fresh]["status"], "stopping", "a fresh stop is left to finish normally")
        self.assertEqual(by_id[paused]["status"], "paused")
        self.assertEqual(snapshot["active_counts"]["claude_reviewer"], 0)
        self.assertEqual(snapshot["active_counts"]["claude_cto"], 0)


if __name__ == "__main__":
    unittest.main()
