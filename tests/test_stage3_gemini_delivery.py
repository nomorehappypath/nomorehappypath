# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 3, Gemini, through the real supervisor and the REAL hook gate (spec T3).

A stand-in Antigravity CLI plays a TUI: a typed line starts a turn of several model invocations; each one runs
the real `hook_gate.py gemini PreInvocation` as the CLI's own child, takes the `injectSteps` it prints and
records it in `transcript.jsonl` as `SYSTEM_SDK` (a typed line is `USER_EXPLICIT`) - the receipt the harness
reads. The line shapes are those of Antigravity CLI 1.3.1 (T0, §13).
"""
from __future__ import annotations

import json
import os
import pty
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness import board, control

ROOT = Path(__file__).resolve().parents[1]

FAKE_AGY = r'''#!PYTHON
import json, os, select, subprocess, sys, time, tty
base = os.environ["FAKE_BASE"]
mode = os.environ.get("FAKE_MODE", "normal")
transcript = os.path.join(base, "transcript.jsonl")
conversation = "conv-fake-1"
open(transcript, "a").close()
tty.setraw(0)
step = [0]
turns = [0]
def write(source, kind, content):
    with open(transcript, "a") as handle:
        handle.write(json.dumps({"step_index": step[0], "source": source, "type": kind, "status": "DONE",
                                 "created_at": "2026-10-08T00:00:00Z", "content": content}) + "\n")
    step[0] += 1
def hook(event, **extra):
    payload = {"conversationId": conversation, "transcriptPath": transcript, "modelName": "gemini-3.8-flash-low", **extra}
    done = subprocess.run([sys.executable, "-E", os.path.join(ROOT, "harness", "hook_gate.py"), "gemini", event],
                          input=json.dumps(payload).encode(), capture_output=True, env=os.environ)
    try:
        reply = json.loads(done.stdout or b"{}")
    except ValueError:
        reply = {}
    return [item.get("userMessage") for item in reply.get("injectSteps", [])]
def turn(prompt):
    if mode == "reject" and prompt.startswith("[SYSTEM CONTROL"):
        # The provider refuses the request (quota, an error): nothing is recorded as the user's input.
        os.write(1, b"REJECTED\r\n")
        return
    write("USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>\n" + prompt + "\n</USER_REQUEST>")
    os.write(1, ("TURN:" + prompt.splitlines()[0][:60] + "\r\n").encode())
    turns[0] += 1
    # the first turn is the long one the test sends its messages into; later (idle wake-up) turns are short
    # The first turn is the long one. By default it lasts a number of steps; with FAKE_UNTIL_FILE it lasts until the
    # test creates `end-turn` (a slow machine must not end the turn before the test has sent everything).
    steps = 1
    if turns[0] == 1:
        steps = 100000 if os.environ.get("FAKE_UNTIL_FILE") else int(os.environ.get("FAKE_INVOCATIONS", "4"))
    started = time.time()
    for _ in range(steps):
        if os.environ.get("FAKE_UNTIL_FILE") and turns[0] == 1 and (os.path.exists(os.path.join(base, "end-turn")) or time.time() - started > 240):
            break
        for text in hook("PreInvocation"):
            if mode != "no_receipt":
                write("SYSTEM_SDK", "USER_INPUT", "<USER_REQUEST>\n" + text + "\n</USER_REQUEST>")
            os.write(1, ("INJECTED:" + text.splitlines()[0] + "\r\n").encode())
        time.sleep(float(os.environ.get("FAKE_STEP", "0.3")))
    write("MODEL", "PLANNER_RESPONSE", "done")
    hook("Stop", fullyIdle=True, terminationReason="NO_TOOL_CALL")
    os.write(1, b"IDLE_NOW\r\n")
os.write(1, b"GEMINI_READY\r\n")
typed = b""
while True:
    if select.select([0], [], [], 0.2)[0]:
        typed += os.read(0, 4096)
        while b"\r" in typed:
            line, _, typed = typed.partition(b"\r")
            text = line.decode(errors="replace").replace("\x1b[200~", "").replace("\x1b[201~", "")
            if text.strip():
                if text.startswith("[SYSTEM CONTROL"):
                    os.write(1, ("TYPED:" + text.splitlines()[0] + "\r\n").encode())
                turn(text)
'''


class Stage3GeminiTests(unittest.TestCase):
    READ_CEILING = 40

    def launch(self, mode="normal", *, receipt_timeout=5.0, busy_wait=900.0, invocations=4, step=0.3, until_file=False):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hn3g-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        self.root = self.base / "p"
        self.root.mkdir()
        cli = self.base / "agy"
        cli.write_text(FAKE_AGY.replace("#!PYTHON", "#!" + sys.executable).replace(
            'os.path.join(ROOT, "harness"', f'os.path.join({str(ROOT)!r}, "harness"'))
        cli.chmod(0o755)
        settings = {"delivery": {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "high"},
                    "cto": {"provider": "claude", "model": "opus", "effort": "high"},
                    "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}}
        self.session = control.create(self.root, "codex_delivery", settings_override=settings)
        self.agent = board.register(self.root, "engineering", "AWAITING_OWNER_DIRECTION", vendor="Google",
                                    session_id=self.session["id"])
        self.runtime = self.base / "rt"
        master, slave = pty.openpty()
        command = [sys.executable, str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(self.root),
                   "--session-id", self.session["id"], "--agent-id", self.agent["id"], "--provider", "gemini",
                   "--gemini-delivery", "--runtime-dir", str(self.runtime), "--receipt-timeout", str(receipt_timeout),
                   "--app-server-timeout", "3", "--busy-wait", str(busy_wait), "--", str(cli)]
        environment = {**os.environ, "FAKE_MODE": mode, "FAKE_BASE": str(self.base), "FAKE_INVOCATIONS": str(invocations),
                       "FAKE_STEP": str(step), "HARNESS_MANAGED_SESSION": self.session["id"],
                       **({"FAKE_UNTIL_FILE": "1"} if until_file else {})}
        self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=environment)
        os.close(slave)
        self.master = master
        self.addCleanup(self._stop)
        self.output = b""
        self.read_until(b"GEMINI_READY")

    def _stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        os.close(self.master)

    def read_until(self, needle):
        deadline = time.monotonic() + self.READ_CEILING
        while time.monotonic() < deadline:
            self._read_more(0.1)
            if needle in self.output:
                return
        self.fail(f"did not see {needle!r}; got {self.output[-800:]!r}")

    def _read_more(self, wait=0.3):
        while select.select([self.master], [], [], wait)[0]:
            try:
                self.output += os.read(self.master, 65536)
            except OSError:
                break
            wait = 0.05

    def wait_for(self, predicate, what="condition"):
        deadline = time.monotonic() + self.READ_CEILING
        while time.monotonic() < deadline:
            self._read_more(0.05)
            if predicate():
                return
        self.fail(f"timed out waiting for {what}; output tail {self.output[-600:]!r}")

    def owner_types(self, text):
        os.write(self.master, text.encode() + b"\r")

    def status(self, queued):
        return control.instruction_receipt(self.root, queued["id"])["status"]

    def transcript_lines(self):
        return [json.loads(line) for line in (self.base / "transcript.jsonl").read_text().splitlines() if line.strip()]

    def start_working(self):
        """The owner starts a turn; the first hook call makes the channel ready (the real board reports the turn)."""
        self.owner_types("owner starts the work")
        self.read_until(b"TURN:owner starts the work")
        control.record_turn_state(self.root, self.session["id"], "working")
        self.wait_for(lambda: self._harness_log().count(b"gemini conversation id recorded") >= 1, "the conversation id from the hook")

    def _harness_log(self):
        from harness import conversation
        path = conversation.transcript_path(self.root, self.session["id"])
        return path.read_bytes() if path.exists() else b""

    # ------------------------------------------------------------------------------------------------

    def test_a_message_to_a_working_session_is_injected_by_the_hook_never_typed_and_delivered_on_its_receipt(self):
        self.launch(invocations=12, step=0.4)
        self.start_working()
        queued = control.enqueue_instruction(self.root, self.session["id"], "Claim the review now.", "test-controller")
        self.read_until(b"INJECTED:[SYSTEM CONTROL \xe2\x80\x94 test-controller] Claim the review now.")
        self.wait_for(lambda: self.status(queued) == "delivered", "delivered")
        self.assertNotIn(b"TYPED:", self.output, "nothing was typed into the window")
        sdk = [line for line in self.transcript_lines() if line["source"] == "SYSTEM_SDK"]
        self.assertEqual(len(sdk), 1)
        self.assertIn(f"(harness delivery harness-{queued['id']})", sdk[0]["content"])
        self.assertEqual(control.cli_session(self.root, self.session["id"])["cli_session_id"], "conv-fake-1",
                         "the conversation id came from the hook's payload")

    def test_fifty_messages_to_a_working_session_then_five_to_an_idle_one_each_delivered_only_on_its_receipt(self):
        self.launch(step=0.12, until_file=True)
        self.start_working()
        busy = [control.enqueue_instruction(self.root, self.session["id"], f"busy message {index}", "test-controller")
                for index in range(50)]
        self.wait_for(lambda: all(self.status(item) == "delivered" for item in busy), "all 50 delivered")
        (self.base / "end-turn").write_text("1")
        self.assertNotIn(b"TYPED:", self.output, "none of the 50 was typed")
        self.wait_for(lambda: b"IDLE_NOW" in self.output, "the turn to end")
        control.record_turn_state(self.root, self.session["id"], "idle_at_prompt")
        idle = [control.enqueue_instruction(self.root, self.session["id"], f"idle message {index}", "test-controller")
                for index in range(5)]
        self.wait_for(lambda: all(self.status(item) == "delivered" for item in idle), "all 5 delivered")
        lines = self.transcript_lines()
        for item in busy + idle:
            marker = f"(harness delivery harness-{item['id']})"
            hits = [line for line in lines if marker in str(line.get("content")) and line["type"] == "USER_INPUT"]
            self.assertEqual(len(hits), 1, f"{marker}: none lost, none duplicated, none merged")
        injected = [line for line in lines if line["source"] == "SYSTEM_SDK"]
        typed = [line for line in lines if line["source"] == "USER_EXPLICIT" and "idle message" in line["content"]]
        self.assertEqual((len(injected), len(typed)), (50, 5), "busy ones injected, idle ones woke the session by typing")
        order = [int(line["content"].split("busy message ")[1].split()[0].split("\n")[0]) for line in injected]
        self.assertEqual(order, sorted(order), "in the order they were sent")

    def test_an_injected_message_with_no_receipt_is_never_acknowledged_and_is_sent_at_most_once_more(self):
        self.launch(mode="no_receipt", receipt_timeout=1.0, busy_wait=2.0, invocations=40, step=0.4)
        self.start_working()
        queued = control.enqueue_instruction(self.root, self.session["id"], "No receipt will come.", "test-controller")
        self.wait_for(lambda: any(e["kind"] == "plumbing_receipt_missing" for e in board.snapshot(self.root)["events"]), "the missing receipt")
        self.wait_for(lambda: b"stays unconfirmed" in self._harness_log(), "the second miss")
        self._read_more(1.0)
        self.assertLessEqual(self.output.count(b"INJECTED:"), 2, "handed over, then once more after the transcript was checked: no more")
        self.assertNotIn(b"TYPED:", self.output)
        self.assertEqual(self.status(queued), "posted", "NEVER delivered without its receipt: it stays recoverable")
        self.assertEqual(control.reclaim_stranded(self.root, self.session["id"]), 1, "the next start of this terminal takes it back")

    def test_input_the_provider_rejected_leaves_no_receipt_so_it_is_not_delivered_and_not_lost(self):
        # Codex's review R1 (2026-10-08): a quota-rejected request left no USER_INPUT, yet the message was acknowledged
        # after the wait and could never be recovered.
        self.launch(mode="reject", receipt_timeout=1.0, busy_wait=2.0, invocations=2, step=0.2)
        self.start_working()
        self.wait_for(lambda: b"IDLE_NOW" in self.output, "the first turn to end")
        control.record_turn_state(self.root, self.session["id"], "idle_at_prompt")
        queued = control.enqueue_instruction(self.root, self.session["id"], "Please review notes.txt now.", "reviewer-challenge")
        self.read_until(b"REJECTED")
        self.wait_for(lambda: b"stays unconfirmed" in self._harness_log(), "the miss to be reported")
        self.assertEqual(self.status(queued), "posted", "not delivered: the provider never recorded it")
        self.assertFalse([line for line in self.transcript_lines() if "Please review notes.txt" in str(line.get("content"))])
        self.assertEqual(control.reclaim_stranded(self.root, self.session["id"]), 1, "recoverable after a restart")
        queued_again = control.instruction_receipt(self.root, queued["id"])
        self.assertEqual(queued_again["status"], "queued")
        self.assertEqual(queued_again["carried_from"], self.session["id"], "checked against the conversation before any resend")

    def test_a_message_waiting_for_a_hook_that_never_comes_is_typed_once_the_turn_is_over(self):
        self.launch(invocations=2, step=0.2)
        self.start_working()
        self.wait_for(lambda: b"IDLE_NOW" in self.output, "the turn to end")
        control.record_turn_state(self.root, self.session["id"], "idle_at_prompt")
        queued = control.enqueue_instruction(self.root, self.session["id"], "Wake up.", "test-controller")
        self.read_until(b"TYPED:[SYSTEM CONTROL")
        self.wait_for(lambda: self.status(queued) == "delivered", "delivered on the typed receipt")
        self.assertEqual(self.output.count(b"TYPED:"), 1)

    def test_a_connection_that_is_not_this_sessions_hook_gets_nothing_and_is_recorded(self):
        self.launch(invocations=12, step=0.4)
        self.start_working()
        queued = control.enqueue_instruction(self.root, self.session["id"], "SECRET_FOR_THE_HOOK_ONLY", "test-controller")
        stolen = []
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and self.status(queued) != "delivered":
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.runtime / "g.sock"))
                probe.sendall((json.dumps({"type": "hook", "event": "PreInvocation", "session": self.session["id"],
                                           "conversation": "x", "transcript": "/nonexistent"}) + "\n").encode())
                probe.settimeout(2)
                stolen.append(probe.recv(4096))
            except OSError:
                stolen.append(b"")
            finally:
                probe.close()
            time.sleep(0.05)
        self.assertTrue(stolen and all(b"SECRET_FOR_THE_HOOK_ONLY" not in reply for reply in stolen), "an outsider is told nothing")
        self.wait_for(lambda: self.status(queued) == "delivered", "the real hook still delivered it")
        refused = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "plumbing_channel_refused"]
        self.assertTrue(refused and "not this session's hook" in refused[0]["message"], refused)

    def test_an_unreachable_socket_means_no_message_and_the_hook_still_answers(self):
        from harness import gemini_delivery
        with self.assertRaises(OSError):
            gemini_delivery.ask("/nonexistent/g.sock", {}, "PreInvocation")


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()
