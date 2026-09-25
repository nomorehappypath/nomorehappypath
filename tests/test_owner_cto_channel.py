# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner ↔ CTO channel (defects #9, #13, #14, #15, #20, #22 of 2026-09-25).

Owner-action cards, owner messages to the CTO, a monitoring cadence that
backs off instead of pinging every 15 s, a hot event window that routine
monitoring cannot fill, a supervisor that does not type over the owner, and
a plain "may need /login" notice after repeated silent stalls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import attention, board, control, interactive_supervisor

ROOT = Path(__file__).resolve().parents[1]


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class _CtoFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.session["id"])
        delivery_session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=delivery_session["id"],
        )
        self.delivery_session = delivery_session

    def events(self, kind: str) -> list[dict]:
        return [event for event in board.snapshot(self.root)["events"] if event["kind"] == kind]

    def cto_agent(self) -> dict:
        return board.snapshot(self.root)["agents"][self.cto["id"]]


class OwnerActionTests(_CtoFixture):
    def test_the_cto_pins_an_action_and_clears_it_with_an_outcome(self):
        action = board.record_owner_action(
            self.root, self.cto["id"], "Run the film GPU proof",
            command="bash /tmp/release_film.sh", why="The Studio runtime only runs outside the agent sandbox.",
        )
        self.assertEqual(action["status"], "open")
        self.assertEqual(action["command"], "bash /tmp/release_film.sh")
        state = board.snapshot(self.root)
        self.assertEqual(state["owner_actions"][action["id"]]["title"], "Run the film GPU proof")
        self.assertEqual(len(self.events("owner_action_recorded")), 1)
        # the same open card is not duplicated
        again = board.record_owner_action(self.root, self.cto["id"], "Run the film GPU proof", command="bash /tmp/release_film.sh")
        self.assertEqual(again["id"], action["id"])
        done = board.clear_owner_action(self.root, self.cto["id"], action["id"], "ran at 17:05; main advanced")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["outcome"], "ran at 17:05; main advanced")
        self.assertEqual(len(self.events("owner_action_cleared")), 1)
        with self.assertRaisesRegex(ValueError, "unknown owner action"):
            board.clear_owner_action(self.root, self.cto["id"], "action-nope", "x")

    def test_only_the_cto_records_or_clears_owner_actions(self):
        with self.assertRaisesRegex(ValueError, "only the active CTO"):
            board.record_owner_action(self.root, self.delivery["id"], "Do a thing", command="ls")
        action = board.record_owner_action(self.root, self.cto["id"], "Do a thing", command="ls")
        with self.assertRaisesRegex(ValueError, "only the active CTO"):
            board.clear_owner_action(self.root, self.delivery["id"], action["id"], "done")
        with self.assertRaisesRegex(ValueError, "title"):
            board.record_owner_action(self.root, self.cto["id"], "")

    def test_an_owner_action_status_note_becomes_a_card(self):
        board.status(self.root, self.cto["id"], "OWNER ACTION: Film Accept recorded but main did not advance. Reply 'yes' to let Claude fast-forward.", "waiting")
        state = board.snapshot(self.root)
        cards = [item for item in state["owner_actions"].values() if item["status"] == "open"]
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["title"].startswith("Film Accept recorded"))
        self.assertEqual(cards[0]["command"], "")
        # a Delivery note with the same prefix is just a note
        board.begin_task_free = None
        board.status(self.root, self.delivery["id"], "OWNER ACTION: none", "working")
        self.assertEqual(len([i for i in board.snapshot(self.root)["owner_actions"].values() if i["status"] == "open"]), 1)


class CtoMessageTests(_CtoFixture):
    def test_an_owner_message_to_the_cto_is_recorded_and_routed_whole(self):
        with patch("harness.control.enqueue_instruction", return_value={"id": "m-1"}) as enqueue:
            result = board.record_owner_message(self.root, self.cto["id"], "Please pause the film work until tomorrow.", "cto_message")
        self.assertEqual(result["message"]["type"], "cto_message")
        self.assertEqual(result["event"]["kind"], "owner_cto_message_received")
        state = board.snapshot(self.root)
        self.assertEqual([m["type"] for m in state["owner_messages"]], ["cto_message"])
        self.assertNotIn(self.session["id"], state.get("owner_directions", {}))
        enqueue.assert_called_once()
        routed = enqueue.call_args.args[2]
        self.assertTrue(routed.startswith("OWNER MESSAGE TO THE CTO:\nPlease pause the film work until tomorrow."))
        self.assertEqual(enqueue.call_args.kwargs["source"], "owner-cto-message")

    def test_the_cto_kind_is_refused_for_delivery_and_directions_are_refused_for_the_cto(self):
        with self.assertRaisesRegex(ValueError, "only the active CTO"):
            board.record_owner_message(self.root, self.delivery["id"], "hello", "cto_message")
        with self.assertRaisesRegex(ValueError, "only an active Delivery Agent"):
            board.record_owner_message(self.root, self.cto["id"], "hello", "direction")


class MonitoringCadenceTests(_CtoFixture):
    def prime(self, **fields):
        with board.locked_state(self.root) as state:
            state["agents"][self.delivery["id"]].update({"task": "WORK", "active": True, "last_poll_at": board.now()})
            agent = state["agents"][self.cto["id"]]
            agent["last_poll_at"] = _ago(900)  # the CTO's poll is always "old"
            agent.pop("recovery_state", None)
            agent.pop("automatic_recovery_requested_at", None)
            agent.update(fields)

    def routed(self) -> int:
        return len([e for e in self.events("agent_automatic_recovery_routed") if e["agent_id"] == self.cto["id"]])

    def test_no_second_cycle_is_routed_before_the_minimum_interval(self):
        self.prime()
        with patch("harness.control.enqueue_instruction", return_value={"id": "w-1", "source": "cto-monitoring-lease"}) as enqueue:
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1, "the first cycle is always due")
            self.assertTrue(self.cto_agent()["last_routine_cycle_at"])
            # the CTO posts status (a progress event) — this used to clear the bookkeeping and re-route at once
            board.status(self.root, self.cto["id"], "waiting; no change", "waiting")
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1, "a cycle 15 s after the last one must not be routed")

    def test_quiet_board_waits_the_quiet_interval_and_a_material_event_shortens_it(self):
        self.prime(last_routine_cycle_at=_ago(board.CTO_MONITOR_BUSY_INTERVAL_SECONDS + 10), last_routine_cycle_sequence=10**9)
        with patch("harness.control.enqueue_instruction", return_value={"id": "w", "source": "cto-monitoring-lease"}) as enqueue:
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 0, "nothing changed: the busy interval is not enough")
        self.prime(last_routine_cycle_at=_ago(board.CTO_MONITOR_QUIET_INTERVAL_SECONDS + 10), last_routine_cycle_sequence=10**9)
        with patch("harness.control.enqueue_instruction", return_value={"id": "w", "source": "cto-monitoring-lease"}) as enqueue:
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1, "nothing changed: the quiet interval has passed")
        # a material event since the last cycle (a Delivery status) makes the busy interval apply
        self.prime(last_routine_cycle_at=_ago(board.CTO_MONITOR_BUSY_INTERVAL_SECONDS + 10), last_routine_cycle_sequence=0)
        board.status(self.root, self.delivery["id"], "implemented the change", "working")
        with patch("harness.control.enqueue_instruction", return_value={"id": "w", "source": "cto-monitoring-lease"}) as enqueue:
            board.mark_stalled(self.root)
            self.assertEqual(enqueue.call_count, 1, "something changed: the busy interval applies")

    def test_routine_monitoring_events_do_not_pile_up_in_the_hot_window(self):
        with board.locked_state(self.root) as state:
            agent = state["agents"][self.cto["id"]]
            for _ in range(5):
                board._event(state, "board_polled", agent, {"task": "GLOBAL_MONITOR", "poll_counter": 1, "unseen": 0})
                board._event(state, "agent_standby", agent, {"task": "GLOBAL_MONITOR", "message": "standing by"})
            delivery = state["agents"][self.delivery["id"]]
            for index in range(3):
                board._event(state, "status_update", delivery, {"task": "WORK", "state": "working", "message": f"step {index}"})
        events = board.snapshot(self.root)["events"]
        self.assertEqual(len([e for e in events if e["kind"] == "board_polled" and e["agent_id"] == self.cto["id"]]), 1)
        self.assertEqual(len([e for e in events if e["kind"] == "agent_standby"]), 1)
        self.assertEqual(len([e for e in events if e["kind"] == "status_update" and e["agent_id"] == self.delivery["id"]]), 3)


class StallNoticeTests(_CtoFixture):
    def test_three_silent_stalls_name_login_and_stop_the_pings_until_a_poll(self):
        with board.locked_state(self.root) as state:
            state["agents"][self.delivery["id"]].update({"task": "WORK", "active": True, "last_poll_at": board.now()})
            state["agents"][self.cto["id"]].update({
                "last_poll_at": _ago(900), "recovery_state": "automatic_requested",
                "automatic_recovery_requested_at": _ago(board.AUTO_RECOVERY_GRACE_SECONDS + 5),
                "consecutive_stalls": board.CTO_UNRESPONSIVE_AFTER_STALLS - 1,
                "liveness": "recovering",
            })
        with patch("harness.control.enqueue_instruction", return_value={"id": "w", "source": "cto-monitoring-lease"}) as enqueue:
            board.mark_stalled(self.root)
            agent = self.cto_agent()
            self.assertEqual(agent["recovery_state"], "unresponsive")
            self.assertEqual(agent["liveness_note"], board.CTO_UNRESPONSIVE_NOTE)
            self.assertEqual(len(self.events("agent_unresponsive")), 1)
            self.assertIn("/login", self.events("agent_unresponsive")[0]["message"])
            board.mark_stalled(self.root)
            board.mark_stalled(self.root)
            enqueue.assert_not_called()
        board.poll(self.root, self.cto["id"])
        agent = self.cto_agent()
        self.assertEqual(agent["recovery_state"], "resumed")
        self.assertNotIn("consecutive_stalls", agent)
        self.assertEqual(agent["liveness"], "healthy")

    def test_a_login_prompt_on_screen_is_recognised_as_its_own_cause(self):
        self.assertIn("/login", attention.detect("Not logged in. Please run /login to continue."))
        self.assertIn("logged out", attention.detect("Authentication expired; run /login"))
        self.assertIsNone(attention.detect("Reading files… 42% done"))


class SupervisorTypingGateTests(unittest.TestCase):
    def test_delivery_waits_for_unsent_input_but_not_forever(self):
        allowed = interactive_supervisor._controller_delivery_allowed
        self.assertTrue(allowed(b"", 0.0, 100.0))
        self.assertFalse(allowed(b"half a sen", 100.0, 101.0))
        self.assertTrue(allowed(b"half a sen", 100.0, 100.0 + interactive_supervisor.OWNER_INPUT_HOLD_SECONDS))
        self.assertTrue(allowed(b"\x1b[?1;2c", 100.0, 101.0), "a terminal reply is not owner input")
        self.assertFalse(allowed(b"\x1b[200~pasted", 100.0, 101.0), "an open paste is owner input")

    def _read_until(self, fd: int, marker: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        seen = b""
        while marker not in seen and time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    seen += os.read(fd, 65536)
                except OSError:
                    break
        return seen

    def test_a_queued_message_is_not_typed_while_the_owner_is_mid_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            master, slave = pty.openpty()
            child = (
                "import os,tty\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY')\n"
                "seen=b''\n"
                "while b'[SYSTEM CONTROL' not in seen: seen += os.read(0,4096); os.write(1,b'ECHO:'+seen[-40:]+b'\\n')\n"
                "os.write(1,b'GOT_CONTROL\\n')\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root),
                "--session-id", session["id"], "--agent-id", agent["id"], "--", "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"CHILD_READY", 10)
                os.write(master, b"I was in the middle of")  # no Enter
                time.sleep(0.3)
                control.enqueue_instruction(root, session["id"], "MONITORING CYCLE DUE: poll now.", "cto-monitoring-lease")
                held = self._read_until(master, b"GOT_CONTROL", 2.0)
                self.assertNotIn(b"GOT_CONTROL", held, "the controller message was typed over the owner's unsent input")
                os.write(master, b" writing this\n")
                delivered = held + self._read_until(master, b"GOT_CONTROL", 8.0)
                self.assertIn(b"GOT_CONTROL", delivered)
                self.assertLess(delivered.index(b"writing this"), delivered.index(b"GOT_CONTROL"))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
