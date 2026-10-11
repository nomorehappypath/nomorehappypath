# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The supervisor's end of Gemini delivery (spec T3-2): who may ask for a message, and what counts as a receipt.

Admission is decided from the connecting process: it must run THIS harness's hook script, as a descendant of THIS
session's CLI, and name THIS session. Another Gemini session's hook, a Claude or Codex session's process and an
agent's own script are all refused, told nothing, and recorded.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import gemini_delivery
from harness.gemini_delivery import GeminiInbox, consumed_entry, delivery_marker

HOOK = f"python3 -E {gemini_delivery.HOOK_SCRIPT} gemini PreInvocation"


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hgi-")
        self.addCleanup(self._tmp.cleanup)
        self.events: list[tuple[str, str]] = []
        self.peer = {"pid": 100, "command": HOOK, "mine": True}
        self.inbox = GeminiInbox(
            str(Path(self._tmp.name) / "g.sock"), session_id="session-A", is_cli=lambda pid: self.peer["mine"],
            peer_pid=lambda connection: self.peer["pid"], command_of=lambda pid: self.peer["command"],
            on_event=lambda kind, detail: self.events.append((kind, detail)), turn_state=lambda: ("working", time.time()), busy_wait=5)
        self.inbox.start()
        self.addCleanup(self.inbox.stop)

    def ask(self, session="session-A", event="PreInvocation", **extra) -> str:
        request = {"type": "hook", "event": event, "session": session, "conversation": "c-1", "transcript": str(Path(self._tmp.name) / "t.jsonl"), **extra}
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(self.inbox.socket_path)
            connection.sendall((json.dumps(request) + "\n").encode())
            return str(json.loads(connection.makefile("rb").readline() or b"{}").get("inject") or "")
        except (OSError, ValueError):
            return ""
        finally:
            connection.close()

    def queue(self, client_id="harness-1", text="secret instruction"):
        with self.inbox.lock:
            self.inbox.pending[client_id] = text
            self.inbox.transcript = self.inbox.transcript or "x"

    def test_this_sessions_own_hook_is_answered_with_the_message_exactly_once(self):
        self.queue()
        self.assertEqual(self.ask(), "secret instruction")
        self.assertEqual(self.ask(), "", "a message handed over is never handed over again")
        self.assertEqual(self.inbox.offered, {"harness-1"})

    def test_a_hook_of_another_session_is_refused_told_nothing_and_recorded(self):
        self.queue()
        self.peer["mine"] = False                      # a Gemini hook, but a descendant of ANOTHER session's CLI
        self.assertEqual(self.ask(), "")
        self.assertIn("harness-1", self.inbox.pending, "nothing was taken from the queue")
        self.assertEqual([kind for kind, _ in self.events], ["refused"])
        self.assertIn("not this session's hook", self.events[0][1])

    def test_a_process_that_is_not_the_harness_hook_is_refused(self):
        self.queue()
        for command in ("python3 -c 'steal()'", "/bin/sh -c 'nc -U g.sock'", "python3 -E /tmp/agent-copy/hook_gate.py gemini PreInvocation"):
            self.peer["command"] = command
            self.assertEqual(self.ask(), "", command)
        self.assertEqual(len([e for e in self.events if e[0] == "refused"]), 3)
        self.assertIn("harness-1", self.inbox.pending)

    def test_a_hook_naming_a_different_session_is_refused(self):
        self.queue()
        self.assertEqual(self.ask(session="session-B"), "")
        self.assertIn("does not name this session", self.events[-1][1])
        self.assertIn("harness-1", self.inbox.pending)

    def test_stop_and_other_events_never_carry_a_message_and_a_conversation_id_is_reported_once(self):
        self.queue()
        self.assertEqual(self.ask(event="Stop"), "")
        self.assertEqual(self.ask(event="PreInvocation"), "secret instruction")
        self.ask()
        self.assertEqual([kind for kind, _ in self.events], ["conversation"], "reported on first sight only")

    def test_two_messages_go_to_two_calls_in_the_order_they_were_queued(self):
        self.queue("harness-1", "first")
        self.queue("harness-2", "second")
        self.assertEqual((self.ask(), self.ask(), self.ask()), ("first", "second", ""))

    def test_the_receipt_is_the_harness_message_in_the_transcript_not_the_models_words(self):
        marker = delivery_marker("harness-9")
        def line(source, kind, content): return json.dumps({"source": source, "type": kind, "content": content})
        self.assertTrue(consumed_entry(line("SYSTEM_SDK", "USER_INPUT", f"<USER_REQUEST>\ngo\n\n{marker}\n</USER_REQUEST>"), marker))
        self.assertTrue(consumed_entry(line("USER_EXPLICIT", "USER_INPUT", f"typed\n{marker}"), marker))
        self.assertFalse(consumed_entry(line("MODEL", "PLANNER_RESPONSE", f"I will quote {marker}"), marker), "the model quoting an id proves nothing")
        self.assertFalse(consumed_entry(line("MODEL", "GENERIC", f"output {marker}"), marker))
        self.assertFalse(consumed_entry("not json " + marker, marker))
        self.assertFalse(consumed_entry(line("SYSTEM_SDK", "USER_INPUT", "no marker here"), marker))


class ReviewFindingsTests(unittest.TestCase):
    """Defects found by the pre-review audit of this candidate (2026-10-07), each reproduced first."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hgr-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_a_carried_message_the_resumed_conversation_already_holds_is_not_sent_again(self):
        # After a relaunch no hook has run yet, so the inbox does not know the transcript - but the recorded
        # conversation's transcript is known, and it is where the last run's delivery is.
        transcript = self.base / "t.jsonl"
        transcript.write_text(json.dumps({"source": "SYSTEM_SDK", "type": "USER_INPUT", "content": f"x\n{delivery_marker('harness-3')}"}) + "\n")
        inbox = GeminiInbox(str(self.base / "g.sock"), session_id="s", is_cli=lambda p: True, peer_pid=lambda c: 1,
                            command_of=lambda p: "", transcript_hint=lambda: str(transcript))
        self.assertEqual(inbox.transcript, "")
        self.assertTrue(inbox.already_delivered("harness-3", 1), "the supervisor then acknowledges it instead of typing it twice")
        self.assertFalse(inbox.already_delivered("harness-4", 1))

    def test_a_message_whose_reply_never_reached_the_hook_is_not_counted_as_handed_over(self):
        inbox = GeminiInbox(str(self.base / "g.sock"), session_id="s", is_cli=lambda p: True, peer_pid=lambda c: 1, command_of=lambda p: HOOK)
        inbox.transcript = "x"
        inbox.pending["harness-1"] = "first"
        inbox.pending["harness-2"] = "second"
        reply = inbox._answer({"event": "PreInvocation"})
        self.assertEqual((reply["inject"], reply["client_id"]), ("first", "harness-1"))
        inbox._take_back(reply["client_id"], reply["inject"])             # what _serve does when sendall fails
        self.assertEqual(list(inbox.pending), ["harness-1", "harness-2"], "back in front of the next one")
        self.assertNotIn("harness-1", inbox.offered)
        self.assertEqual(inbox._answer({"event": "PreInvocation"})["inject"], "first")

    def test_a_real_broken_connection_puts_the_message_back(self):
        events = []
        inbox = GeminiInbox(str(self.base / "g.sock"), session_id="s", is_cli=lambda p: True, peer_pid=lambda c: 1,
                            command_of=lambda p: HOOK, on_event=lambda k, d: events.append(k))
        inbox.start()
        self.addCleanup(inbox.stop)
        inbox.transcript = "x"
        inbox.pending["harness-1"] = "first"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(inbox.socket_path)
        connection.sendall((json.dumps({"type": "hook", "event": "PreInvocation", "session": "s"}) + "\n").encode())
        connection.shutdown(socket.SHUT_RDWR)       # the hook gives up before it hears the answer
        connection.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "harness-1" not in inbox.pending:
            time.sleep(0.05)
        # Either the answer raced the close (then it is offered) or it was taken back; it is never lost.
        self.assertTrue("harness-1" in inbox.pending or "harness-1" in inbox.offered)

    def test_the_hook_is_recognised_by_the_file_it_runs_even_through_a_symlinked_install(self):
        link = self.base / "linked"
        link.symlink_to(Path(gemini_delivery.HOOK_SCRIPT).parent)
        via_link = f"/usr/bin/python3 -E {link}/hook_gate.py gemini PreInvocation"
        self.assertTrue(gemini_delivery.is_gate_command(via_link))
        self.assertTrue(gemini_delivery.is_gate_command(f"python3 -E '{gemini_delivery.HOOK_SCRIPT}' gemini Stop"))
        self.assertFalse(gemini_delivery.is_gate_command(f"python3 -E {link}/hook_gate.py claude PreToolUse"), "Claude's gate is not Gemini's")
        copy = self.base / "copy"
        copy.mkdir()
        (copy / "hook_gate.py").write_text("# an agent's own copy\n")
        self.assertFalse(gemini_delivery.is_gate_command(f"python3 -E {copy}/hook_gate.py gemini PreInvocation"))
        self.assertFalse(gemini_delivery.is_gate_command("python3 -c 'print(1)' hook_gate.py gemini"))
        self.assertFalse(gemini_delivery.is_gate_command("unbalanced 'quote"))


class DeliveryOutcomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hgo-")
        self.addCleanup(self._tmp.cleanup)
        self.state = ["working"]
        self.transcript = Path(self._tmp.name) / "t.jsonl"
        self.transcript.write_text("")
        self.inbox = GeminiInbox(
            str(Path(self._tmp.name) / "g.sock"), session_id="s", is_cli=lambda pid: True, peer_pid=lambda c: 1,
            command_of=lambda pid: HOOK, turn_state=lambda: (self.state[0], time.time()), busy_wait=3)
        self.inbox.transcript = str(self.transcript)

    def hook_asks_soon(self, delay=0.3):
        def run():
            time.sleep(delay)
            self.inbox._answer({"event": "PreInvocation", "transcript": str(self.transcript), "conversation": "c"})
        threading.Thread(target=run, daemon=True).start()

    def test_an_idle_session_or_one_with_no_hook_yet_is_not_ready_so_the_supervisor_types(self):
        self.state[0] = "idle_at_prompt"
        self.assertEqual(self.inbox.deliver("harness-1", "x", response_timeout=1, receipt_timeout=1), "not_ready")
        self.state[0] = "working"
        self.inbox.transcript = ""
        self.assertEqual(self.inbox.deliver("harness-1", "x", response_timeout=1, receipt_timeout=1), "not_ready")
        self.assertEqual(self.inbox.pending, {})

    def test_a_message_the_turn_ends_before_a_hook_asked_for_is_withdrawn_for_typing(self):
        def finish():
            time.sleep(0.6)
            self.state[0] = "idle_at_prompt"
        threading.Thread(target=finish, daemon=True).start()
        self.assertEqual(self.inbox.deliver("harness-1", "x", response_timeout=1, receipt_timeout=1), "not_ready")
        self.assertEqual(self.inbox.pending, {}, "withdrawn: the typed copy is the only one")
        self.assertEqual(self.inbox.offered, set())

    def test_delivered_only_when_the_receipt_appears_and_posted_otherwise_without_a_second_hand_over(self):
        self.hook_asks_soon()
        threading.Thread(target=lambda: (time.sleep(0.8), self.transcript.write_text(json.dumps({
            "source": "SYSTEM_SDK", "type": "USER_INPUT", "content": f"hello\n\n{delivery_marker('harness-1')}"}) + "\n")), daemon=True).start()
        self.assertEqual(self.inbox.deliver("harness-1", "hello", response_timeout=1, receipt_timeout=3), "delivered")
        self.hook_asks_soon()
        self.assertEqual(self.inbox.deliver("harness-2", "again", response_timeout=1, receipt_timeout=0.5), "posted")
        self.assertIn("harness-2", self.inbox.awaiting)
        self.assertEqual(self.inbox._answer({"event": "PreInvocation"})["inject"], "", "posted is never handed over again")

    def test_a_message_already_in_the_transcript_is_delivered_without_being_sent(self):
        self.transcript.write_text(json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT",
                                               "content": f"typed earlier {delivery_marker('harness-5')}"}) + "\n")
        self.assertTrue(self.inbox.already_delivered("harness-5", 1))
        self.assertEqual(self.inbox.deliver("harness-5", "x", response_timeout=1, receipt_timeout=1), "delivered")
        self.assertEqual(self.inbox.pending, {})

    def test_a_typed_message_is_tracked_until_its_receipt_or_the_wait_runs_out(self):
        self.inbox.busy_wait = 0.3
        self.inbox.expect_receipt("harness-7")
        self.assertEqual(self.inbox.take_receipts(), ([], []))
        time.sleep(0.4)
        self.assertEqual(self.inbox.take_receipts(), ([], ["harness-7"]), "overdue is reported, never typed twice")
        self.inbox.expect_receipt("harness-8")
        self.transcript.write_text(json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT",
                                               "content": f"typed {delivery_marker('harness-8')}"}) + "\n")
        self.assertEqual(self.inbox.take_receipts(), (["harness-8"], []))


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()
