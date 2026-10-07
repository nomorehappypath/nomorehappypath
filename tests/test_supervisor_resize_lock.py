# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A real resize during a control update must not freeze the terminal."""
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness import board, control

ROOT = Path(__file__).resolve().parents[1]


class SupervisorResizeLockTests(unittest.TestCase):
    def test_resize_during_control_lock_keeps_keyboard_paste_and_status_working(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = control.create(root, "claude_reviewer")
            agent = board.register(root, "qa", board.AWAITING_OWNER_DIRECTION,
                                   vendor="Anthropic", session_id=session["id"])
            driver = r'''
import os, signal, sys
from pathlib import Path
from harness import attention, control, interactive_supervisor
root = Path(sys.argv[1])
original = control.take_instructions
sent = False
def take(root, session_id):
    global sent
    if not sent:
        sent = True
        with control.locked_state(root):
            os.kill(os.getpid(), signal.SIGWINCH)
    return original(root, session_id)
control.take_instructions = take
# A prompt becoming clear on resize exercises the callback's state write.
attention.PromptWatch.resize = lambda self, height: ('clear', None)
child = "import os,tty; tty.setraw(0); os.write(1,b'CHILD_READY\\n'); data=b''\nwhile b'\\r' not in data: data+=os.read(0,4096)\nos.write(1,b'INPUT_OK:'+data+b'\\n')"
raise SystemExit(interactive_supervisor.run(root, sys.argv[2], sys.argv[3],
                                          [sys.executable, '-c', child]))
'''
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [sys.executable, "-c", driver, str(root), session["id"], agent["id"]],
                cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            output = bytearray()
            try:
                deadline = time.monotonic() + 8
                sent = False
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                    if b"CHILD_READY" in output and not sent:
                        time.sleep(.2)
                        os.write(master, b"typed then pasted text\r")
                        sent = True
                    if b"INPUT_OK:typed then pasted text" in output:
                        break
                self.assertIn(b"INPUT_OK:typed then pasted text", output,
                              "SIGWINCH froze the supervisor while it held the shared control lock")
                self.assertEqual(process.wait(timeout=8), 0)
                self.assertEqual(len(control.snapshot(root)["sessions"]), 1)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=8)
                os.close(master)
