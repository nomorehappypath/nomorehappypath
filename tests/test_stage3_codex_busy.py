# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Stage 3 review r3b: a Codex agent that works in one long turn still gets every message.

What failed (real run, 2026-10-03, reviewer-stage3-r3b-p1): after QA passed,
the release coordinator sent the Codex Delivery agent "FINAL PASS ... Complete
the release" five times. None arrived. The agent waits on the board INSIDE its
turn, so its thread was never idle, and Stage 3 gave messages to idle threads
only. Typing reaches a busy Codex (Enter steers the running turn); now the
app-server does the same with `turn/steer`.

Measured on codex-cli 0.160.0 (scratch home, stub model): a steered message is
used when the model next reads, carrying its clientId - the receipt; a stale
turn id is refused; an interrupted turn drops the steered message unused, so
it is safe to send again.

Also found in that run: a pause force-stops a terminal after 3 s, before a
Stage 3 supervisor finishes its own clean-up, and the messages it had taken
stayed "taken" for ever. A relaunched terminal now takes them back.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from harness import control
from tests import test_stage3_codex_multiplexer, test_stage3_codex_supervisor


class BusyFixture(test_stage3_codex_multiplexer.MultiplexerFixture):
    mode = "busy"

    def start_long_turn(self, tui):
        tui.send({"id": 2, "method": "turn/start", "params": {"threadId": "th-1", "clientUserMessageId": "owner-turn",
                  "input": [{"type": "text", "text": "work", "text_elements": []}]}})
        tui.receive_until(lambda m: m.get("method") == "turn/started")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.mux.active_turn:
            time.sleep(0.05)
        self.assertTrue(self.mux.thread_busy)

    def steers(self) -> list[dict]:
        return [json.loads(line) for line in self.server_log.read_text().splitlines() if '"turn/steer"' in line]


class BusyThreadTests(BusyFixture):
    def test_a_busy_thread_takes_the_message_into_its_running_turn(self):
        tui = self.connect_tui()
        self.start_long_turn(tui)
        self.assertTrue(self.mux.ready(), "busy is not 'not ready': the running turn can be steered")
        self.assertEqual(self.mux.deliver("harness-21", "FINAL PASS", response_timeout=5, receipt_timeout=5), "delivered")
        steer = self.steers()
        self.assertEqual(len(steer), 1)
        self.assertEqual(steer[0]["params"]["expectedTurnId"], self.mux.active_turn)
        self.assertEqual(steer[0]["params"]["clientUserMessageId"], "harness-21")
        self.assertEqual(self.mux.take_receipts(), ([], []), "answered by deliver itself, never reported twice")


class HeldSteerTests(BusyFixture):
    steer = "hold"

    def test_a_steer_not_yet_read_is_posted_then_its_receipt_arrives(self):
        tui = self.connect_tui()
        self.start_long_turn(tui)
        self.assertEqual(self.mux.deliver("harness-22", "x", response_timeout=5, receipt_timeout=0.5), "posted")
        self.mux.request("stub/consume", {}, 5)
        deadline = time.monotonic() + 5
        seen: list[str] = []
        while time.monotonic() < deadline and not seen:
            seen = self.mux.take_receipts()[0]
            time.sleep(0.05)
        self.assertEqual(seen, ["harness-22"])
        self.assertEqual(self.mux.take_dropped(), [])

    def test_a_steer_whose_turn_is_interrupted_is_reported_dropped(self):
        tui = self.connect_tui()
        self.start_long_turn(tui)
        self.assertEqual(self.mux.deliver("harness-23", "x", response_timeout=5, receipt_timeout=0.5), "posted")
        self.mux.request("stub/interrupt", {}, 5)
        deadline = time.monotonic() + 5
        dropped: list[str] = []
        while time.monotonic() < deadline and not dropped:
            dropped = self.mux.take_dropped()
            time.sleep(0.05)
        self.assertEqual(dropped, ["harness-23"])
        self.assertEqual(self.mux.take_receipts(), ([], []))

    def test_a_stale_turn_is_refused_and_the_message_waits(self):
        tui = self.connect_tui()
        self.start_long_turn(tui)
        with self.mux.lock:
            self.mux.active_turn = "turn-gone"
        self.assertEqual(self.mux.deliver("harness-24", "x", response_timeout=5, receipt_timeout=0.5), "not_ready")


class BusyAgentEndToEndTests(test_stage3_codex_supervisor.Stage3SupervisorFixture):
    """Through the real supervisor: the agent's own turn never ends."""

    def test_messages_reach_an_agent_whose_turn_never_ends(self):
        process, master = self.launch("busy")
        first = control.enqueue_instruction(self.root, self.session["id"], "FINAL PASS is certified.", "python-final-pass-routing")
        second = control.enqueue_instruction(self.root, self.session["id"], "Complete the release.", "python-final-pass-routing")
        self.read_until(master, f"TURN:harness-{second['id']}".encode())
        for queued in (first, second):
            self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        self.assertNotIn(b"TYPED:", self.output, "nothing typed")
        self.assertNotIn(b"PLUMBING FALLBACK", self.output)


class DroppedSteerEndToEndTests(test_stage3_codex_supervisor.Stage3SupervisorFixture):
    steer = "drop_first"

    def test_a_message_dropped_by_its_turn_is_sent_again_once(self):
        process, master = self.launch("busy")
        queued = control.enqueue_instruction(self.root, self.session["id"], "Complete the release.", "python-final-pass-routing")
        self.read_until(master, f"TURN:harness-{queued['id']}".encode())
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        sent = [line for line in self.server_log.read_text().splitlines() if f"harness-{queued['id']}" in line
                and ('"turn/steer"' in line or '"turn/start"' in line)]
        self.assertEqual(len(sent), 2, "steered once (dropped unused), then sent once more")
        self.assertEqual(self.output.count(f"TURN:harness-{queued['id']}".encode()), 1, "it arrived exactly once")


class ReclaimTests(unittest.TestCase):
    """A terminal killed mid-delivery: its relaunch takes back what it held."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.session = control.create(self.root, "codex_delivery")

    def test_taken_and_posted_messages_of_a_killed_run_are_queued_again_once(self):
        direction = control.enqueue_instruction(self.root, self.session["id"], "OWNER DIRECTION: build it.", "owner-direction")
        go = control.enqueue_instruction(self.root, self.session["id"], "GO AHEAD", "owner-requirements-go_ahead")
        done = control.enqueue_instruction(self.root, self.session["id"], "Delivered earlier.", "x")
        later = control.enqueue_instruction(self.root, self.session["id"], "Still queued.", "y")
        taken = control.take_instructions(self.root, self.session["id"])
        control.return_instructions(self.root, self.session["id"], [entry for entry in taken if entry["id"] == later["id"]])
        control.mark_posted(self.root, self.session["id"], go["id"], next(e for e in taken if e["id"] == go["id"]))
        control.acknowledge_instruction(self.root, self.session["id"], done["id"])
        # The run is SIGKILLed here: no clean-up. The relaunch starts:
        self.assertEqual(control.reclaim_stranded(self.root, self.session["id"]), 2)
        self.assertEqual(control.reclaim_stranded(self.root, self.session["id"]), 0, "never queued twice")
        inbox = control.take_instructions(self.root, self.session["id"])
        self.assertEqual([entry["id"] for entry in inbox], [direction["id"], go["id"], later["id"]],
                         "the stranded ones first, in their order, then what was waiting")
        self.assertEqual({entry["id"]: entry["text"] for entry in inbox}[direction["id"]], "OWNER DIRECTION: build it.")
        self.assertTrue(all(entry.get("carried_from") == self.session["id"] for entry in inbox[:2]),
                        "checked against the conversation before sending: it may have arrived")
        self.assertEqual(control.instruction_receipt(self.root, done["id"])["status"], "delivered", "delivered stays delivered")

    def test_a_pause_notice_still_in_flight_never_reaches_the_resumed_agent(self):
        # Driver run r4 (2026-10-03): the notice was in flight when the pause
        # stopped the terminal, the relaunch took it back, and the resumed agent
        # was told "Project pause requested" after the owner had resumed.
        control.attach(self.root, self.session["id"], os.getpid())
        notice = control.enqueue_instruction(self.root, self.session["id"], "Project pause requested.", control.PAUSE_NOTICE_SOURCE)
        work = control.enqueue_instruction(self.root, self.session["id"], "FINAL PASS is certified.", "python-final-pass-routing")
        taken = control.take_instructions(self.root, self.session["id"])
        control.mark_posted(self.root, self.session["id"], notice["id"], next(e for e in taken if e["id"] == notice["id"]))
        with control.locked_state(self.root) as state:              # the pause stopped it
            state["sessions"][self.session["id"]].update(status="paused", pid=None, pause_requested_at=control.now())
        control.prepare_resume_sessions(self.root, [self.session["id"]])
        self.assertEqual(control.instruction_receipt(self.root, notice["id"])["status"], "withdrawn")
        self.assertEqual(control.reclaim_stranded(self.root, self.session["id"]), 1, "the real work is still taken back")
        self.assertEqual([entry["id"] for entry in control.take_instructions(self.root, self.session["id"])], [work["id"]])


if __name__ == "__main__":
    unittest.main()
