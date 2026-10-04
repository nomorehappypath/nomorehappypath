# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 3, Codex: the supervisor's multiplexer and delivery with receipt.

Spec docs/specs/PLUMBING_MODERNIZATION.md, Stage 3 and §9 test 6. A stdio
app-server stub and a WebSocket TUI stub stand in for codex-cli; the wire
shapes are the ones measured from 0.160.0 (tests/stage3_stubs.py).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness import codex_app_server
from tests import stage3_stubs


def _short_dir() -> tempfile.TemporaryDirectory:
    # AF_UNIX paths are limited to ~104 bytes on macOS.
    return tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hn3-")


class MultiplexerFixture(unittest.TestCase):
    mode = "normal"
    steer = "consume"

    def setUp(self):
        self._tmp = _short_dir()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        (base / "stub.py").write_text(stage3_stubs.APP_SERVER)
        self.server_log = base / "server.log"
        self.events: list[tuple[str, str]] = []
        self.admitted = {os.getpid()}
        self.mux = codex_app_server.CodexMultiplexer(
            str(base / "t.sock"), [sys.executable, str(base / "stub.py")],
            environment={**os.environ, "STUB_MODE": self.mode, "STUB_STEER": self.steer, "STUB_LOG": str(self.server_log)},
            admit=lambda pid: pid in self.admitted, peer_pid=lambda connection: os.getpid(),
            on_event=lambda kind, detail: self.events.append((kind, detail)),
        )
        self.mux.start()
        self.addCleanup(self.mux.stop)

    def connect_tui(self) -> stage3_stubs.WebSocketClient:
        tui = stage3_stubs.WebSocketClient(self.mux.socket_path)
        self.addCleanup(tui.close)
        self.assertIn(b"101 Switching Protocols", tui.handshake())
        tui.send({"id": "initialize", "method": "initialize", "params": {"clientInfo": {"name": "codex-tui"}}})
        tui.receive_until(lambda m: m.get("id") == "initialize")
        tui.send({"method": "initialized"})
        tui.send({"id": 1, "method": "thread/start", "params": {}})
        tui.receive_until(lambda m: m.get("method") == "thread/started")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.mux.ready():
            time.sleep(0.05)
        return tui


def _closed_without_handshake(client: stage3_stubs.WebSocketClient) -> bool:
    try:
        client.socket.sendall(b"GET /rpc HTTP/1.1\r\n\r\n")
        return client.socket.recv(100) == b""
    except (BrokenPipeError, ConnectionResetError):
        return True


class RelayTests(MultiplexerFixture):
    def test_the_tui_drives_the_stdio_server_and_the_thread_is_learned(self):
        tui = self.connect_tui()
        self.assertTrue(self.mux.ready())
        self.assertEqual(self.mux.thread_id, "th-1")
        self.assertEqual(self.mux.policy["approvalPolicy"], "never")
        self.assertIn(("thread", "th-1"), self.events)
        self.assertTrue(self.server_log.read_text().splitlines()[0].startswith('{"id":"initialize"'),
                        "the TUI's initialize is the server's initialize")
        tui.close()

    def test_a_process_that_is_not_this_sessions_terminal_is_refused(self):
        self.admitted.clear()
        intruder = stage3_stubs.WebSocketClient(self.mux.socket_path)
        self.addCleanup(intruder.close)
        self.assertTrue(_closed_without_handshake(intruder), "the connection is closed without a handshake")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.events:
            time.sleep(0.05)
        self.assertEqual(self.events[0][0], "refused")

    def test_a_second_terminal_is_refused_while_one_is_attached(self):
        self.connect_tui()
        second = stage3_stubs.WebSocketClient(self.mux.socket_path)
        self.addCleanup(second.close)
        self.assertTrue(_closed_without_handshake(second))
        self.assertTrue(any(kind == "refused" for kind, _ in self.events))

    def test_the_socket_is_private_to_the_owner(self):
        self.assertEqual(os.stat(self.mux.socket_path).st_mode & 0o777, 0o600)


class DeliveryTests(MultiplexerFixture):
    def test_a_message_is_delivered_only_with_the_turn_id_and_its_client_id(self):
        tui = self.connect_tui()
        result = self.mux.deliver("harness-7", "[SYSTEM CONTROL — test] hello", response_timeout=5, receipt_timeout=5)
        self.assertEqual(result, "delivered")
        sent = [json.loads(line) for line in self.server_log.read_text().splitlines() if '"turn/start"' in line]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["params"]["clientUserMessageId"], "harness-7")
        self.assertTrue(sent[0]["id"].startswith("hn:"), "the harness uses its own id namespace")
        # The owner's TUI sees the turn exactly like any other: the notifications fan out to it.
        seen = tui.receive_until(lambda m: m.get("method") == "turn/completed")
        self.assertTrue(any(m.get("method") == "item/started" for m in seen))
        self.assertFalse(any(str(m.get("id", "")).startswith("hn:") for m in seen),
                         "the harness's responses never reach the TUI")

    def test_nothing_is_sent_before_the_terminal_has_a_thread(self):
        self.assertEqual(self.mux.deliver("harness-1", "x", response_timeout=1, receipt_timeout=1), "not_ready")
        self.assertFalse(self.server_log.exists() and '"turn/start"' in self.server_log.read_text())

    def test_a_retry_finds_a_delivery_whose_receipt_was_late(self):
        self.connect_tui()
        self.assertEqual(self.mux.deliver("harness-9", "x", response_timeout=5, receipt_timeout=5), "delivered")
        self.assertTrue(self.mux.already_delivered("harness-9", 5))
        self.assertFalse(self.mux.already_delivered("harness-10", 5))


class NoReceiptTests(MultiplexerFixture):
    mode = "no_receipt"

    def test_a_turn_without_the_matching_user_message_is_never_marked_delivered(self):
        self.connect_tui()
        self.assertEqual(self.mux.deliver("harness-3", "x", response_timeout=5, receipt_timeout=1), "unconfirmed")


class RefusedTurnTests(MultiplexerFixture):
    mode = "error"

    def test_a_refused_turn_start_is_not_delivered(self):
        self.connect_tui()
        self.assertEqual(self.mux.deliver("harness-4", "x", response_timeout=5, receipt_timeout=1), "refused")


class ServerRequestTests(MultiplexerFixture):
    mode = "approval"

    def test_a_server_request_goes_to_the_terminal_so_the_owner_answers_it(self):
        tui = self.connect_tui()
        self.mux.deliver("harness-5", "x", response_timeout=5, receipt_timeout=5)
        seen = tui.receive_until(lambda m: m.get("method") == "item/commandExecution/requestApproval")
        approval = seen[-1]
        tui.send({"id": approval["id"], "result": {"decision": "accept"}})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and '"decision"' not in self.server_log.read_text():
            time.sleep(0.05)
        answers = [json.loads(line) for line in self.server_log.read_text().splitlines() if '"decision"' in line]
        self.assertEqual(answers, [{"id": 900, "result": {"decision": "accept"}}], "the owner's answer reaches the server")


class WorkerTests(MultiplexerFixture):
    def test_the_worker_reports_the_outcome_without_blocking_the_caller(self):
        self.connect_tui()
        worker = codex_app_server.DeliveryWorker(self.mux, response_timeout=5, receipt_timeout=5)
        worker.submit({"id": "abc", "source": "test", "text": "hi"}, "[SYSTEM CONTROL — test] hi")
        deadline = time.monotonic() + 10
        outcome = None
        while time.monotonic() < deadline and outcome is None:
            outcome = worker.take_outcome()
            time.sleep(0.05)
        self.assertEqual(outcome, ({"id": "abc", "source": "test", "text": "hi"}, "delivered"))
        self.assertFalse(worker.busy())


if __name__ == "__main__":
    unittest.main()


class HelperThreadTests(MultiplexerFixture):
    """The real TUI starts its own thread AND a hidden ephemeral helper (codex-cli 0.160.0)."""

    def setUp(self):
        super().setUp()
        self.mux.expected_policy = {"approvalPolicy": "never", "writableRoots": ["/w2"], "networkAccess": True}

    def test_only_the_non_ephemeral_thread_is_the_sessions_and_messages_go_there(self):
        tui = self.connect_tui()                                   # startup thread: th-1
        tui.send({"id": "temporary-structured-1", "method": "thread/start", "params": {"ephemeral": True}})
        tui.receive_until(lambda m: m.get("id") == "temporary-structured-1")
        self.assertEqual(self.mux.thread_id, "th-1", "the helper thread never becomes the session's thread")
        self.assertEqual([detail for kind, detail in self.events if kind == "thread"], ["th-1"], "the resume id is the real thread")
        self.assertFalse(any(kind == "policy_mismatch" for kind, _ in self.events), "the helper's own sandbox is not the session's")
        self.assertEqual(self.mux.deliver("harness-8", "x", response_timeout=5, receipt_timeout=5), "delivered")
        starts = [json.loads(line) for line in self.server_log.read_text().splitlines() if '"turn/start"' in line]
        self.assertEqual(starts[-1]["params"]["threadId"], "th-1")
