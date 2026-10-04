# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stages 3 + 4, Codex, on the REAL binary: the owner's exact path, headless.

The real `codex --remote` TUI on the line the supervisor builds, the harness
multiplexer, the real stdio app-server with the session's flags - and, in
place of a model, a local stub provider that records what the model is sent
(no sign-in, no model turn, scratch CODEX_HOME). Found this way on 2026-10-03:
the TUI's hidden helper thread was taken for the session's thread (messages
and the resume id went to it), and a killed node wrapper left the native
binary running. Skipped where codex is not installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import cli_capabilities, codex_app_server, global_settings
from harness.interactive_supervisor import _remote_tui_command

ROOT = Path(__file__).resolve().parents[1]
DIRECTIVE = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8").rstrip("\n")
MARKER = "HARNESS-DIRECTIVE-" + hashlib.sha256(DIRECTIVE.encode("utf-8")).hexdigest()[:16]
# A terminal answers these queries; a bare PTY must too, or the TUI waits.
TERMINAL_REPLIES = ((b"\x1b[6n", b"\x1b[1;1R"), (b"\x1b[c", b"\x1b[?62;22c"), (b"\x1b[?u", b"\x1b[?0u"),
                    (b"\x1b]10;?", b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"), (b"\x1b]11;?", b"\x1b]11;rgb:0000/0000/0000\x1b\\"))


def ours(marker: str) -> list[str]:
    listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [line for line in listing.splitlines() if marker in line and "ps -axo" not in line]


class RealCodexFixture(unittest.TestCase):
    def setUp(self):
        self.codex = global_settings.resolved_cli("codex").get("path", "")
        if not self.codex or not shutil.which(self.codex):
            self.skipTest("codex CLI not installed")
        self.scratch = Path(tempfile.mkdtemp(prefix="hn3real-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.marker = self.scratch.name                  # every process of this test carries it in its argv

    def assertNothingLeft(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and ours(self.marker):
            time.sleep(0.2)
        self.assertEqual(ours(self.marker), [], "nothing of ours may be left running")


class GateProbeLeavesNothingTests(RealCodexFixture):
    def test_the_gate_probe_ends_every_process_it_started(self):
        environment = cli_capabilities._scratch_environment(self.codex, self.scratch)
        result = cli_capabilities._probe_codex_stage3(self.codex, self.scratch, environment)
        self.assertEqual(result, {"codex.remote_tui_launch_line": True, "codex.server_policy_applies": True})
        self.assertNothingLeft()


class OwnersPathTests(RealCodexFixture):
    def setUp(self):
        from tests.environment_support import require_loopback
        require_loopback()
        super().setUp()
        self.requests: list[str] = []
        recorder = self.requests

        class Model(BaseHTTPRequestHandler):
            def do_POST(self):
                recorder.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8", "replace"))
                self.send_response(500)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_):
                return
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)
        self.home = self.scratch / "codex-home"
        self.workspace = self.scratch / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        with open(self.home / "config.toml", "w", encoding="utf-8") as handle:   # trusted, as the harness does (F-5)
            for path in {str(self.workspace), os.path.realpath(self.workspace)}:
                handle.write(f'[projects."{path}"]\ntrust_level = "trusted"\n\n')
        self.environment = {**os.environ, "CODEX_HOME": str(self.home)}
        self.server_flags = [
            "-c", 'model_provider="stub"', "-c", 'model="gpt-5"', "-c", "request_max_retries=0", "-c", "stream_max_retries=0",
            "-c", f'model_providers.stub={{name="stub", base_url="http://127.0.0.1:{self.model.server_address[1]}/v1", '
                  f'wire_api="responses", requires_openai_auth=false}}',
            "-c", "approval_policy=never", "-c", "sandbox_mode=workspace-write",
            "-c", "developer_instructions=" + json.dumps(MARKER + "\n" + DIRECTIVE),
        ]

    def drive(self, today: list[str]) -> tuple[codex_app_server.CodexMultiplexer, list[str], bytes]:
        runtime = Path(tempfile.mkdtemp(prefix="hn3rt-", dir="/tmp" if os.path.isdir("/tmp") else None))
        self.addCleanup(shutil.rmtree, runtime, True)
        mux = codex_app_server.CodexMultiplexer(
            str(runtime / "t.sock"), [self.codex, "app-server", "--listen", "stdio://", *self.server_flags],
            environment=self.environment, cwd=str(self.workspace), admit=lambda pid: True, peer_pid=lambda c: 1,
            expected_policy={"approvalPolicy": "never", "writableRoots": [], "networkAccess": False})
        mux.start()
        master, slave = pty.openpty()
        before = len(self.requests)
        process = subprocess.Popen(_remote_tui_command(today, mux.socket_path), stdin=slave, stdout=slave, stderr=slave,
                                   env=self.environment, cwd=str(self.workspace), start_new_session=True)
        os.close(slave)
        screen = b""
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and process.poll() is None and len(self.requests) == before:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    screen += chunk
                    for query, reply in TERMINAL_REPLIES:
                        if query in chunk:
                            os.write(master, reply)
            time.sleep(1)
        finally:
            codex_app_server.end_process_group(process)
            mux.stop()
            os.close(master)
        return mux, self.requests[before:], screen

    def test_the_rules_reach_the_model_fresh_and_after_resume_and_resume_finds_the_session(self):
        fresh_mux, fresh, _ = self.drive([self.codex, "--cd", str(self.workspace), "--model", "gpt-5",
                                          "-c", "approval_policy=never", "-c", 'developer_instructions="not these"', "hello"])
        self.assertTrue(fresh, "the model was asked")
        developer = [item for item in json.loads(fresh[0]).get("input", []) if isinstance(item, dict) and item.get("role") == "developer"]
        self.assertTrue(any(MARKER in json.dumps(item) for item in developer), "Stage 4: the directive is a developer message")
        self.assertFalse(any("not these" in body for body in fresh), "the terminal's own instructions never override")
        thread = fresh_mux.thread_id
        self.assertTrue(thread)
        resumed_mux, resumed, screen = self.drive([self.codex, "resume", "--model", "gpt-5", thread, "after resume"])
        self.assertNotIn(b"No saved session", screen, "the recorded id is the session's real thread")
        self.assertEqual(resumed_mux.thread_id, thread)
        self.assertTrue(resumed and any(MARKER in body for body in resumed), "the rules are in force after a resume")
        self.assertNothingLeft()


if __name__ == "__main__":
    unittest.main()


class SteerTests(RealCodexFixture):
    """Stage 3 review r3b: a busy thread is steered - measured on the real app-server, stub model."""

    def setUp(self):
        from tests.environment_support import require_loopback
        require_loopback()
        super().setUp()
        self.requests: list[str] = []
        recorder = self.requests

        class Model(BaseHTTPRequestHandler):
            def do_POST(self):
                recorder.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8", "replace"))
                time.sleep(6 if len(recorder) == 1 else 0)          # the first answer is slow: the turn stays busy
                number = len(recorder)
                events = [{"type": "response.created", "response": {"id": f"resp_{number}"}},
                          {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": f"msg_{number}",
                                                                         "content": [{"type": "output_text", "text": "ok"}]}},
                          {"type": "response.completed", "response": {"id": f"resp_{number}", "usage": {
                              "input_tokens": 1, "input_tokens_details": None, "output_tokens": 1,
                              "output_tokens_details": None, "total_tokens": 2}}}]
                body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                return
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)
        home, self.workspace = self.scratch / "codex-home", self.scratch / "workspace"
        home.mkdir()
        self.workspace.mkdir()
        (home / "config.toml").write_text("".join(f'[projects."{path}"]\ntrust_level = "trusted"\n\n'
                                                  for path in {str(self.workspace), os.path.realpath(self.workspace)}), encoding="utf-8")
        runtime = Path(tempfile.mkdtemp(prefix="hn3rt-", dir="/tmp" if os.path.isdir("/tmp") else None))
        self.addCleanup(shutil.rmtree, runtime, True)
        flags = ["-c", 'model_provider="stub"', "-c", 'model="gpt-5"', "-c", "request_max_retries=0", "-c", "stream_max_retries=0",
                 "-c", f'model_providers.stub={{name="stub", base_url="http://127.0.0.1:{self.model.server_address[1]}/v1", '
                       f'wire_api="responses", requires_openai_auth=false}}',
                 "-c", "approval_policy=never", "-c", "sandbox_mode=workspace-write"]
        self.mux = codex_app_server.CodexMultiplexer(
            str(runtime / "t.sock"), [self.codex, "app-server", "--listen", "stdio://", *flags],
            environment={**os.environ, "CODEX_HOME": str(home)}, cwd=str(self.workspace), admit=lambda pid: True, peer_pid=lambda c: 1,
            expected_policy={"approvalPolicy": "never", "writableRoots": [], "networkAccess": False})
        self.mux.start()
        self.addCleanup(self.mux.stop)
        self.assertNotIn("error", self.mux.request("initialize", {"clientInfo": {"name": "harness-test", "version": "1"}}, 30))
        with self.mux.lock:
            self.mux.initialized = True          # the owner's terminal's initialize does this; none is attached here
        thread = self.mux.request("thread/start", {"cwd": str(self.workspace)}, 30)
        self.assertNotIn("error", thread)
        with self.mux.lock:                      # as the terminal's own thread/start answer would record it
            self.mux.thread_id = self.mux.thread_id or thread["result"]["thread"]["id"]
        started = self.mux.request("turn/start", {"threadId": self.mux.thread_id, "clientUserMessageId": "owner-turn",
                                                  "input": [{"type": "text", "text": "work", "text_elements": []}]}, 30)
        self.assertNotIn("error", started)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.mux.active_turn:
            time.sleep(0.05)
        self.assertTrue(self.mux.thread_busy and self.mux.active_turn, "the agent is mid-turn")

    def test_a_message_joins_the_running_turn_and_its_receipt_is_the_delivery_id(self):
        self.assertEqual(self.mux.deliver("harness-steer-1", "STEERED-MARKER", response_timeout=30, receipt_timeout=30), "delivered")
        deadline = time.monotonic() + 15                  # the receipt is reported just before the model is asked
        while time.monotonic() < deadline and not any("STEERED-MARKER" in body for body in self.requests[1:]):
            time.sleep(0.1)
        self.assertTrue(any("STEERED-MARKER" in body for body in self.requests[1:]), "the model reads it within the same turn")

    def test_an_interrupted_turn_drops_the_steered_message_unused(self):
        self.assertEqual(self.mux.deliver("harness-steer-2", "DROPPED-MARKER", response_timeout=30, receipt_timeout=1), "posted")
        self.mux.request("turn/interrupt", {"threadId": self.mux.thread_id, "turnId": self.mux.active_turn}, 30)
        deadline = time.monotonic() + 15
        dropped: list[str] = []
        while time.monotonic() < deadline and not dropped:
            dropped = self.mux.take_dropped()
            time.sleep(0.1)
        self.assertEqual(dropped, ["harness-steer-2"], "never used: safe to send again")
        self.assertFalse(any("DROPPED-MARKER" in body for body in self.requests))
