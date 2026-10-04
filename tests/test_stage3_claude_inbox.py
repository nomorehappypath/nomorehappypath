# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 3, Claude, end to end through the real supervisor, hook gate and relay.

A stand-in Claude CLI binds its own inbox socket (as Claude Code does), runs
the REAL SessionStart hook command (`harness/hook_gate.py`, which starts the
REAL `harness/inbox_relay.py`) and writes each message it receives on its
inbox into its transcript, the receipt the harness reads. The wire format is
the one Claude Code 2.1.288 documents in its own debug log.
"""
from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness import board, control

ROOT = Path(__file__).resolve().parents[1]

FAKE_CLAUDE = r'''#!PYTHON
import json, os, select, socket, subprocess, sys, time, tty
mode = os.environ.get("FAKE_MODE", "normal")
base = os.environ["FAKE_BASE"]
inbox_path = os.path.join(base, "inbox.sock")
transcript = os.path.join(base, "transcript.jsonl")
open(transcript, "a").close()
tty.setraw(0)
if mode == "lingering":
    # Like a CLI stuck in exit (review r2): the process serving the inbox
    # outlives a Stop, so the relay never ends by itself.
    child = subprocess.Popen([sys.executable, "-c",
        "import os,socket,sys,time\nif os.path.exists(sys.argv[1]): os.unlink(sys.argv[1])\ns=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(8)\n"
        "while True:\n c,_=s.accept(); c.close()", inbox_path], start_new_session=True)
    open(os.path.join(base, "lingering.pid"), "w").write(str(child.pid))
    while not os.path.exists(inbox_path): pass
    inbox = None
elif mode == "hijack":
    # Something else serves the inbox path: not this CLI.
    # Another agent's process: NOT in this CLI's process tree (detached, as an
    # agent's own listener would be), holding the inbox path.
    subprocess.run(["/bin/sh", "-c", '"$0" -c "import socket,time,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(5); time.sleep(20)" "$1" >/dev/null 2>&1 &', sys.executable, inbox_path])
    while not os.path.exists(inbox_path): pass
    inbox = None
else:
    if os.path.exists(inbox_path): os.unlink(inbox_path)
    inbox = socket.socket(socket.AF_UNIX); inbox.bind(inbox_path); inbox.listen(8)
env = dict(os.environ, CLAUDE_CODE_MESSAGING_SOCKET=inbox_path, CLAUDE_CODE_MESSAGING_TOKEN="tok-123")
subprocess.run([sys.executable, "-E", os.path.join(ROOT, "harness", "hook_gate.py"), "claude", "SessionStart"],
               input=json.dumps({"transcript_path": transcript}).encode(), env=env, capture_output=True)
os.write(1, b"CLAUDE_READY\r\n")
typed = b""
pending = []
sockets = [0] + ([inbox] if inbox else [])
replaced = False
while True:
    if mode == "replaced" and not replaced and os.path.exists(os.path.join(base, "replace-now")):
        # Security review 2026-10-04 (P4): AFTER the relay was admitted, a
        # process outside this CLI's tree takes over the inbox path and keeps
        # every byte it is sent.
        replaced = True
        subprocess.Popen([sys.executable, "-c",
            "import os,socket,sys,time\nos.unlink(sys.argv[1]); s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(8)\n"
            "open(sys.argv[2],'wb').close(); s.settimeout(30)\n"
            "while True:\n c,_=s.accept(); c.settimeout(1)\n try:\n  d=c.recv(65536)\n except OSError:\n  d=b''\n"
            " open(sys.argv[2],'ab').write(b'CONNECTED:'+d+b'\\n'); c.close()",
            inbox_path, os.path.join(base, "impostor.log")], start_new_session=True)
    for due, content in [item for item in pending if item[0] <= time.time()]:
        pending.remove((due, content))
        with open(transcript, "a") as handle:
            handle.write(json.dumps({"type": "attachment", "renderedRole": "system", "userType": "external",
                                     "attachment": {"type": "queued_command", "prompt": content, "isMeta": True}}) + "\n")
    readable, _, _ = select.select(sockets, [], [], 0.2)
    for ready in readable:
        if ready == 0:
            typed += os.read(0, 4096)
            if b"\r" in typed:
                os.write(1, b"TYPED:" + typed.replace(b"\r", b"<ENTER>") + b"\r\n"); typed = b""
        elif ready is inbox:
            connection, _ = inbox.accept()
            data = b""
            connection.settimeout(1)
            try:
                while True:
                    chunk = connection.recv(65536)
                    if not chunk: break
                    data += chunk
            except socket.timeout:
                pass
            connection.close()
            for line in data.decode().splitlines():
                message = json.loads(line)
                if message.get("type") == "user":
                    content = message["message"]["content"]
                    if mode == "busy":
                        # Mid-turn, as the real CLI records it: an "enqueue"
                        # queue-operation at once (NOT a consumption), and the
                        # "queued_command" attachment when its running tool
                        # call ends - after the receipt window.
                        with open(transcript, "a") as handle:
                            handle.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "content": content}) + "\n")
                        pending.append((time.time() + float(os.environ.get("FAKE_BUSY_SECONDS", "3")), content))
                    elif mode != "no_transcript":
                        with open(transcript, "a") as handle:
                            handle.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
                    os.write(1, ("INBOX:" + content.splitlines()[0] + "\r\n").encode())
                elif message.get("type") == "auth":
                    os.write(1, ("AUTH:" + message.get("token", "") + "\r\n").encode())
'''


class Stage3ClaudeTests(unittest.TestCase):
    READ_CEILING = 30

    def launch(self, mode: str, receipt_timeout: float = 5.0, start_timeout: float = 20.0, busy_wait: float = 900.0,
               *, relaunch: bool = False):
        if not relaunch:
            self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hn3c-")
            self.addCleanup(self._tmp.cleanup)
            self.base = Path(os.path.realpath(self._tmp.name))
            self.root = self.base / "p"
            self.root.mkdir()
        cli = self.base / "claude"
        cli.write_text(FAKE_CLAUDE.replace("#!PYTHON", "#!" + sys.executable).replace(
            'os.path.join(ROOT, "harness"', f'os.path.join({str(ROOT)!r}, "harness"'))
        cli.chmod(0o755)
        # A relaunch is a NEW session of the same role, continuing the ended one
        # (same project, same Claude conversation file).
        self.session = control.create(self.root, "claude_reviewer")
        self.agent = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=self.session["id"])
        self.runtime = self.base / "rt"
        master, slave = pty.openpty()
        command = [
            sys.executable, str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(self.root),
            "--session-id", self.session["id"], "--agent-id", self.agent["id"], "--provider", "claude",
            "--claude-inbox", "--runtime-dir", str(self.runtime), "--receipt-timeout", str(receipt_timeout),
            "--app-server-timeout", str(start_timeout), "--busy-wait", str(busy_wait), "--", str(cli),
        ]
        environment = {**os.environ, "FAKE_MODE": mode, "FAKE_BASE": str(self.base),
                       "HARNESS_MANAGED_SESSION": self.session["id"]}
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=environment)
        os.close(slave)
        self.addCleanup(self._stop, process, master)
        self.output = b""
        self.read_until(master, b"CLAUDE_READY")
        return process, master

    def _stop(self, process, master):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)
        subprocess.run(["pkill", "-f", f"inbox_relay.py {self.runtime}"], capture_output=True)

    def read_until(self, master, needle):
        deadline = time.monotonic() + self.READ_CEILING
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    self.output += os.read(master, 65536)
                except OSError:
                    break
            if needle in self.output:
                return self.output
        self.fail(f"did not see {needle!r}; got {self.output[-800:]!r}")

    def wait_for(self, predicate):
        deadline = time.monotonic() + self.READ_CEILING
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail("timed out")

    def relays(self) -> list[str]:
        listing = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
        return [line for line in listing.splitlines() if "inbox_relay.py" in line and str(self.runtime) in line]

    def test_a_harness_message_reaches_the_inbox_through_the_relay_and_is_never_typed(self):
        process, master = self.launch("normal")
        queued = control.enqueue_instruction(self.root, self.session["id"], "Claim the review now.", "test-controller")
        self.read_until(master, b"INBOX:[SYSTEM CONTROL \xe2\x80\x94 test-controller] Claim the review now.")
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        self.assertIn(b"AUTH:tok-123", self.output, "the relay opens with the session's own token")
        self.assertNotIn(b"TYPED:", self.output, "nothing was typed into the terminal")
        transcript = (self.base / "transcript.jsonl").read_text()
        self.assertIn(f"(harness delivery harness-{queued['id']})", transcript, "the receipt the harness read")

    def test_a_message_consumed_mid_turn_is_posted_once_even_when_its_receipt_is_late(self):
        # Stage 3 review r1 (2026-10-03, real Claude Code): a message consumed
        # mid-turn reached the transcript only after the turn; the turn had been
        # started by an inbox message, so no UserPromptSubmit fired and the
        # session still looked idle; at 30 s the "miss" posted it again and the
        # reviewer saw the same marker twice. Modelled exactly: stale idle turn
        # state, the transcript line written 3 s after a 1 s receipt window.
        process, master = self.launch("busy", receipt_timeout=1.0)
        control.record_turn_state(self.root, self.session["id"], "idle_at_prompt")
        queued = control.enqueue_instruction(self.root, self.session["id"], "T4 midturn marker.", "review-t4-midturn")
        self.wait_for(lambda: b"receipt seen in the session transcript" in self._harness_transcript())
        time.sleep(1.0)
        self._read_more(master)
        self.assertEqual(self.output.count(b"INBOX:"), 1, "posted once, never again")
        self.assertNotIn(b"TYPED:", self.output, "never typed either")
        self.assertEqual(control.instruction_receipt(self.root, queued["id"])["status"], "delivered")
        kinds = [event["kind"] for event in board.snapshot(self.root)["events"]]
        self.assertNotIn("plumbing_fallback", kinds)
        self.assertNotIn("plumbing_receipt_missing", kinds)

    def test_a_posted_message_without_any_receipt_is_reported_and_never_posted_again(self):
        process, master = self.launch("no_transcript", receipt_timeout=1.0, busy_wait=2.0)
        queued = control.enqueue_instruction(self.root, self.session["id"], "Claim the review now.", "test-controller")
        self.wait_for(lambda: any(event["kind"] == "plumbing_receipt_missing" for event in board.snapshot(self.root)["events"]))
        time.sleep(1.0)
        self._read_more(master)
        self.assertEqual(self.output.count(b"INBOX:"), 1, "a posted message is never posted again")
        self.assertNotIn(b"TYPED:", self.output, "nor typed: it may already have been consumed")
        self.assertEqual(control.instruction_receipt(self.root, queued["id"])["status"], "delivered")

    def test_repeated_missing_receipts_send_new_messages_back_to_typing(self):
        process, master = self.launch("no_transcript", receipt_timeout=1.0, busy_wait=2.0)
        for text in ("First message.", "Second message."):
            control.enqueue_instruction(self.root, self.session["id"], text, "test-controller")
        self.wait_for(lambda: any(event["kind"] == "plumbing_fallback" for event in board.snapshot(self.root)["events"]))
        third = control.enqueue_instruction(self.root, self.session["id"], "Third message.", "test-controller")
        self.read_until(master, b"TYPED:")
        self.assertIn(b"Third message.", self.output[self.output.index(b"TYPED:"):])
        self.wait_for(lambda: control.instruction_receipt(self.root, third["id"])["status"] == "delivered")
        self.assertEqual(self.output.count(b"INBOX:"), 2, "the first two were posted once each, never again")

    def end_session(self, process, master):
        """The CLI dies before it consumes what was posted (the supervisor ends with it)."""
        control.record_cli_session(self.root, self.session["id"], "cli-conversation-1", "claude")
        process.terminate()
        process.wait(timeout=15)
        self.wait_for(lambda: control.snapshot(self.root)["sessions"] and all(
            s["status"] not in control.ACTIVE_STATUSES for s in control.snapshot(self.root)["sessions"] if s["id"] == self.session["id"]))
        subprocess.run(["pkill", "-f", f"inbox_relay.py {self.runtime}"], capture_output=True)

    def test_a_posted_message_lost_with_its_cli_arrives_exactly_once_after_the_relaunch(self):
        process, master = self.launch("busy", receipt_timeout=1.0)          # it never reaches the transcript
        queued = control.enqueue_instruction(self.root, self.session["id"], "Carry this over.", "review-assignment")
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "posted")
        self.end_session(process, master)
        first = self.session["id"]
        process, master = self.launch("normal", receipt_timeout=5.0, relaunch=True)
        self.assertEqual(control.cli_session(self.root, self.session["id"])["continues_session"], first)
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        self._read_more(master)
        self.assertEqual(self.output.count(b"INBOX:"), 1, "the relaunched CLI received it exactly once")
        self.assertIn(b"Carry this over.", self.output)
        self.assertEqual(control.instruction_receipt(self.root, queued["id"])["session_id"], self.session["id"])

    def test_a_message_the_dead_cli_did_consume_is_never_sent_again_after_the_relaunch(self):
        process, master = self.launch("busy", receipt_timeout=1.0)
        queued = control.enqueue_instruction(self.root, self.session["id"], "Already done.", "review-assignment")
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "posted")
        self.end_session(process, master)
        # It had consumed it MID-TURN, recorded the way the real CLI records that
        # (review r2: a "queued_command" attachment, no "user" token). The
        # resumed CLI shares this conversation.
        with open(self.base / "transcript.jsonl", "a") as handle:
            handle.write(json.dumps({"type": "attachment", "renderedRole": "system", "userType": "external",
                                     "attachment": {"type": "queued_command", "isMeta": True, "prompt":
                                     f"[SYSTEM CONTROL — review-assignment] Already done.\n\n(harness delivery harness-{queued['id']})"}}) + "\n")
        process, master = self.launch("normal", receipt_timeout=5.0, relaunch=True)
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        time.sleep(1.0)
        self._read_more(master)
        self.assertNotIn(b"INBOX:", self.output, "never sent twice")
        self.assertNotIn(b"TYPED:", self.output)

    def _harness_transcript(self) -> bytes:
        from harness import conversation
        path = conversation.transcript_path(self.root, self.session["id"])
        return path.read_bytes() if path.exists() else b""

    def _read_more(self, master) -> bytes:
        more = b""
        while select.select([master], [], [], 0.3)[0]:
            try:
                more += os.read(master, 65536)
            except OSError:
                break
        self.output += more
        return b""

    def test_an_inbox_socket_served_by_another_process_gets_nothing_and_is_reported(self):
        process, master = self.launch("hijack", start_timeout=2.0)
        queued = control.enqueue_instruction(self.root, self.session["id"], "Claim the review now.", "test-controller")
        self.read_until(master, b"TYPED:")
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        refused = [event for event in board.snapshot(self.root)["events"] if event["kind"] == "plumbing_channel_refused"]
        self.assertTrue(refused, "the refusal is a board event the CTO sees")
        # Since 2026-10-04 the relay itself will not say hello to a supervisor
        # when the inbox is not served by that supervisor's CLI; either way the
        # impostor is refused before anything is sent.
        self.assertTrue(any("not this session's CLI" in event["message"] or "does not belong to this session" in event["message"]
                            for event in refused), refused)

    def test_an_inbox_replaced_after_admission_receives_no_byte_and_the_session_types(self):
        # Security review 2026-10-04 (P4, live): the relay wrote the auth frame and
        # the message BEFORE checking who was listening; the refusal came after.
        process, master = self.launch("replaced")
        self.wait_for(lambda: bool(self.relays()))
        first = control.enqueue_instruction(self.root, self.session["id"], "Before the swap.", "test-controller")
        self.read_until(master, b"INBOX:[SYSTEM CONTROL")
        self.wait_for(lambda: control.instruction_receipt(self.root, first["id"])["status"] == "delivered")
        (self.base / "replace-now").write_text("1")
        self.wait_for(lambda: (self.base / "impostor.log").exists())
        self.addCleanup(lambda: subprocess.run(["pkill", "-f", str(self.base / "impostor.log")], capture_output=True))
        queued = control.enqueue_instruction(self.root, self.session["id"], "IMPOSTOR_MUST_NOT_SEE_THIS", "test-controller")
        self.read_until(master, b"TYPED:")
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        seen = (self.base / "impostor.log").read_bytes()
        self.assertNotIn(b"tok-123", seen, "no authentication frame reaches the impostor")
        self.assertNotIn(b"IMPOSTOR_MUST_NOT_SEE_THIS", seen, "no message reaches the impostor")
        self.assertNotIn(b'"type"', seen, "not a single frame")
        refused = [event for event in board.snapshot(self.root)["events"] if event["kind"] == "plumbing_channel_refused"]
        self.assertTrue(any("nothing was sent" in event["message"] for event in refused), refused)

    def test_stop_ends_the_session_and_its_relay_even_when_the_cli_lingers(self):
        process, master = self.launch("lingering")
        self.wait_for(lambda: bool(self.relays()))
        lingering = int((self.base / "lingering.pid").read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(lingering)], capture_output=True))
        started = time.monotonic()
        process.terminate()                                    # what Stop all sends the supervisor
        process.wait(timeout=30)
        self.assertLess(time.monotonic() - started, 15, "the supervisor's teardown is bounded")
        self.wait_for(lambda: not self.relays())
        self.assertFalse(self.runtime.exists(), "no runtime directory left")

    def test_the_relay_ends_with_the_session(self):
        process, master = self.launch("normal")
        self.wait_for(lambda: bool(self.relays()))
        process.terminate()
        process.wait(timeout=10)
        self.wait_for(lambda: not self.relays())
        self.assertFalse(self.runtime.exists())


if __name__ == "__main__":
    unittest.main()


class RunnerTests(unittest.TestCase):
    """The real runner under a real terminal: Claude Stage 3 only when switched on and proven."""

    from tests import test_board_surface as _surface
    served = _surface.BoardSurfaceAuthenticationTests.served
    bootstrap_served = _surface.BoardSurfaceAuthenticationTests.bootstrap_served

    def setUp(self):
        import shutil
        from harness import global_settings, project_registry
        from harness.project_context import ProjectContext
        from tests.environment_support import require_loopback
        if not Path("/usr/bin/sandbox-exec").exists() and not shutil.which("bwrap"):
            self.skipTest("no write-confinement primitive on this platform")
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hn3cr-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        code = self.base / "code"; code.mkdir()
        from tests.environment_support import home_outside_temp_space
        self.home = home_outside_temp_space(self, ".hn3home-")
        self.context = ProjectContext(code, self.home / "projects" / "p1" / "data", self.home / "projects" / "p1" / "workspaces")
        control.initialize(self.context)
        project_registry.save(self.home, {"version": project_registry.REGISTRY_VERSION, "projects": [
            {"id": "p1", "name": "project", "code_root": str(code), "data_root": str(self.context.data_root),
             "workspace_root": str(self.context.workspace_root)}]})
        self.capture = self.base / "argv.jsonl"
        self.global_settings = global_settings

    def fake_claude(self) -> Path:
        path = self.base / "fake-claude"
        path.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        f"open({str(self.capture)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], "
                        "'handover': os.environ.get('HARNESS_INBOX_HANDOVER', '')}) + '\\n')\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def launch(self, on: bool) -> dict:
        from harness import cli_capabilities
        from harness.board_surface import SessionTokenAuthority
        environment = {"HARNESS_CLAUDE_BIN": str(self.fake_claude()), "CLAUDE_CONFIG_DIR": str(self.base / "claude-config")}
        settings = self.global_settings.load(self.home)
        settings["plumbing"]["stage1_hooks_enabled"] = on
        settings["plumbing"]["stage3_claude_socket_delivery_enabled"] = on
        self.global_settings._write(self.home, settings)
        source = {**os.environ, **environment}
        identity = cli_capabilities.binary_identity("claude", source_environment=source)
        needed = (cli_capabilities.STAGE_REQUIREMENTS["stage3_claude_socket_delivery"]["claude"]
                  + cli_capabilities.STAGE_REQUIREMENTS["stage1_hooks"]["claude"])
        static = [name for name in needed if name in cli_capabilities.STATIC_ITEMS["claude"]]
        path = cli_capabilities.cache_path(self.home, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": identity, "probed_at": "test", "probe_version": cli_capabilities.PROBE_VERSION, "static": {name: True for name in static}}),
                        encoding="utf-8")
        cli_capabilities.record_live(self.home, "claude", {name: True for name in needed if name not in static},
                                     auth_mode="test", source_environment=source)
        session = control.create(self.context, "claude_reviewer")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        master, slave = pty.openpty()
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            process = subprocess.Popen([
                "/bin/bash", str(ROOT / "scripts" / "run_managed_agent.sh"), "--root", str(self.context.code_root),
                "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
                "--python", os.path.realpath(sys.executable), "--session-id", session["id"], "--kind", "claude_reviewer",
                "--manager-home", str(self.home), "--board-bootstrap", bootstrap,
            ], cwd=self.context.code_root, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                env={**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root), **environment})
            os.close(slave)
            output = b""
            deadline = time.monotonic() + 60
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            if process.poll() is None:
                process.kill()
            os.close(master)
        self.assertEqual(process.wait(timeout=10), 0, output[-1500:])
        launches = [json.loads(line) for line in self.capture.read_text().splitlines()]
        launches = [entry for entry in launches if entry["argv"] != ["--version"]]
        self.assertEqual(len(launches), 1)
        return launches[0]

    def test_switched_on_and_proven_the_session_gets_its_relay_note_and_no_inter_agent_tools(self):
        from harness import claude_inbox
        launch = self.launch(on=True)
        argv = launch["argv"]
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings["permissions"]["deny"], ["SendMessage", "ListAgents"])
        self.assertIn("SessionStart", settings["hooks"], "the hook that starts the relay")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], claude_inbox.AUTHORITY_NOTE)
        self.assertEqual(argv[argv.index("--system-prompt-snapshot") + 1], "off")
        self.assertTrue(launch["handover"].startswith(f"{os.path.realpath(self.home)}/rt/"), launch["handover"])
        self.assertTrue(launch["handover"].endswith("/h.sock"))
        self.assertNotIn("crossSessionInbound", settings, "the inbound policy is never loosened")

    def test_switched_off_the_launch_is_todays(self):
        launch = self.launch(on=False)
        self.assertNotIn("--append-system-prompt", launch["argv"])
        self.assertNotIn("--settings", launch["argv"])
        self.assertEqual(launch["handover"], "")


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def record(self, live: dict, static: dict) -> dict:
        return {"identity": {}, "static": static, "live": live}

    def test_proving_mode_runs_a_stage_whose_live_items_are_unproven_but_never_false_ones(self):
        from harness import cli_capabilities, global_settings
        settings = global_settings.load(self.home)
        settings["plumbing"].update(stage1_hooks_enabled=True, stage3_claude_socket_delivery_enabled=True)
        needed = cli_capabilities.STAGE_REQUIREMENTS["stage3_claude_socket_delivery"]["claude"]
        static = {name: True for name in needed if name in cli_capabilities.STATIC_ITEMS["claude"]}
        status = cli_capabilities.stage_status(self.home, "stage3_claude_socket_delivery", "claude", settings=settings,
                                               records={"claude": self.record({}, static)})
        self.assertFalse(status["enabled"], "proving is off: unproven gates like false")
        settings["plumbing"]["prove_live_items"] = True
        status = cli_capabilities.stage_status(self.home, "stage3_claude_socket_delivery", "claude", settings=settings,
                                               records={"claude": self.record({}, static)})
        self.assertTrue(status["enabled"])
        self.assertTrue(status["reason"].startswith("PLUMBING PROVING stage3_claude_socket_delivery unproven:"))
        status = cli_capabilities.stage_status(self.home, "stage3_claude_socket_delivery", "claude", settings=settings,
                                               records={"claude": self.record({"claude.inbox_receipt": False}, static)})
        self.assertFalse(status["enabled"], "a capability proven false still blocks")
        missing_static = dict(static, **{"claude.inbox_relay_admission": False})
        status = cli_capabilities.stage_status(self.home, "stage3_claude_socket_delivery", "claude", settings=settings,
                                               records={"claude": self.record({}, missing_static)})
        self.assertFalse(status["enabled"], "a static capability is never waived")

    def test_a_live_result_needs_evidence_and_survives_a_full_spike_write(self):
        from harness import cli_capabilities
        with self.assertRaises(ValueError):
            cli_capabilities.record_live_item(self.home, "claude", "claude.inbox_receipt", True, "")
        cli_capabilities.record_live_item(self.home, "claude", "claude.inbox_receipt", True, "run B transcript, line 42")
        cli_capabilities.record_live(self.home, "claude", {"claude.inbox_receipt": True, "cross_agent_isolation": "UNPROVEN"},
                                     auth_mode="none")
        identity = cli_capabilities.binary_identity("claude")
        stored = json.loads(cli_capabilities.cache_path(self.home, identity, live=True).read_text())
        self.assertEqual(stored["evidence"]["claude.inbox_receipt"], "run B transcript, line 42")


class InboxTeardownTests(unittest.TestCase):
    def test_stop_returns_at_once_while_a_reading_thread_waits_on_the_relay(self):
        import socket
        import threading
        from harness import claude_inbox
        inbox = claude_inbox.ClaudeInbox("/nonexistent", session_id="s", cli_pid=lambda: 0, is_cli=lambda pid: True,
                                         peer_pid=lambda c: 0, command_of=lambda pid: "")
        supervisor_side, relay_side = socket.socketpair()
        self.addCleanup(relay_side.close)                      # the relay never closes its end
        reader = supervisor_side.makefile("rb")
        inbox.relay, inbox.relay_reader = supervisor_side, reader
        threading.Thread(target=inbox._read_loop, args=(supervisor_side, reader), daemon=True).start()
        time.sleep(0.3)                                        # the reading thread now waits on the relay
        started = time.monotonic()
        stopper = threading.Thread(target=inbox.stop, daemon=True)
        stopper.start()
        stopper.join(timeout=5)
        self.assertFalse(stopper.is_alive(), "stop() blocked behind the reading thread")
        self.assertLess(time.monotonic() - started, 2)


class ConsumptionReceiptTests(unittest.TestCase):
    """The real conversation-file shapes (measured, Claude Code 2.1.28x)."""

    MARK = "(harness delivery harness-abc123)"

    def test_only_real_consumption_counts(self):
        from harness.claude_inbox import consumed_entry
        text = f"[SYSTEM CONTROL — review] x\n\n{self.MARK}"
        enqueue = json.dumps({"type": "queue-operation", "operation": "enqueue", "content": text})
        started_turn = json.dumps({"type": "user", "isMeta": True, "message": {"role": "user", "content": text}})
        mid_turn = json.dumps({"type": "attachment", "renderedRole": "system", "userType": "external",
                               "attachment": {"type": "queued_command", "prompt": text, "isMeta": True}})
        quoted_by_model = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": f"I saw {self.MARK}"}})
        self.assertFalse(consumed_entry(enqueue, self.MARK), "queued is not consumed")
        self.assertTrue(consumed_entry(started_turn, self.MARK))
        self.assertTrue(consumed_entry(mid_turn, self.MARK), "the mid-turn shape carries no 'user' token")
        self.assertFalse(consumed_entry(quoted_by_model, self.MARK), "the model quoting it is not a delivery")
        self.assertFalse(consumed_entry("not json " + self.MARK, self.MARK))
