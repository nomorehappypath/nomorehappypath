# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A terminal that shows its provider's usage-limit message is waiting, not stuck (2026-10-16_quota-handoff).

Before this change the self-heal ladder restarted such a terminal, relaunched it and finally replaced it, losing the conversation
Claude Code would have resumed by itself. These tests use the same stand-in processes as tests/test_self_restart.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from harness import attention, board, control, self_heal
from tests import test_self_restart as base


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _limit_incident(resets_in_hours: float = 3.0) -> dict:
    seen = datetime.now().astimezone()
    return {"seen_at": seen.isoformat(timespec="seconds"), "resets_at": (seen + timedelta(hours=resets_in_hours)).isoformat(timespec="seconds"),
            "kind": "announced", "when_text": "9:43pm", "provider": "claude"}


class HeldTerminalSelfHealTests(base._Fixture):
    def hold(self, session_id: str, provider: str = "codex") -> None:
        control.record_attention(self.root, session_id, attention.limit_reason(provider), limit=_limit_incident())

    def release(self, session_id: str) -> None:
        control.clear_attention(self.root, session_id)

    def test_a_frozen_terminal_that_shows_a_usage_limit_is_not_restarted_but_the_same_terminal_without_it_is(self):
        session, _, _ = self.delivery_with_task()
        self.freeze(session["id"])
        self.hold(session["id"])
        self.assertEqual(self_heal.plan(self.root), [], "a held terminal is waiting; nothing restarts it")
        self.release(session["id"])
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["limit_cleared_at"] = _ago(self_heal.attention.LIMIT_RESUME_GRACE_SECONDS + 30)
        self.assertEqual([a["action"] for a in self_heal.plan(self.root)], ["restart"], "once the line is gone and the grace has passed it is an ordinary frozen terminal")

    def test_the_grace_after_the_line_clears_covers_the_frozen_check(self):
        session, _, _ = self.delivery_with_task()
        self.freeze(session["id"])
        self.hold(session["id"])
        self.release(session["id"])          # limit_cleared_at = now
        self.assertEqual(self_heal.plan(self.root), [], "old queued-message ages must not restart a terminal that has only just resumed")
        later = datetime.now(timezone.utc) + timedelta(seconds=attention.LIMIT_RESUME_GRACE_SECONDS + 5)
        self.assertEqual([a["action"] for a in self_heal.plan(self.root, later)], ["restart"])

    def test_the_stall_ladder_treats_a_held_or_just_resumed_agent_as_waiting(self):
        session, agent, _ = self.delivery_with_task()
        self.hold(session["id"])
        state, current = board.snapshot(self.root), datetime.now(timezone.utc)
        held = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertTrue(self_heal._stall_blocked(state, state["agents"][agent["id"]], held, current))
        self.release(session["id"])
        resumed = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertTrue(self_heal._stall_blocked(state, state["agents"][agent["id"]], resumed, current), "within the grace")
        past_grace = current + timedelta(seconds=attention.LIMIT_RESUME_GRACE_SECONDS + 5)
        self.assertFalse(self_heal._stall_blocked(state, state["agents"][agent["id"]], resumed, past_grace))

    def test_no_second_reviewer_is_started_against_the_same_exhausted_allowance(self):
        session = control.create(self.root, "claude_reviewer")
        process = self.stand_in()
        control.attach(self.root, session["id"], process.pid)
        control.note_cli_exec(self.root, session["id"])
        agent = board.register(self.root, "qa", "REVIEW", vendor="Anthropic", session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": _ago(6 * 60), "request_id": "review-1"}
        self.hold(session["id"], "claude")
        self.assertEqual([a["action"] for a in self_heal.plan(self.root)], [], "the held Reviewer counts as present")
        self.release(session["id"])
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["limit_cleared_at"] = _ago(self_heal.attention.LIMIT_RESUME_GRACE_SECONDS + 30)
        self.assertEqual([a["action"] for a in self_heal.plan(self.root)], ["start_reviewer"])

    def test_an_unrelated_crashed_delivery_is_still_recovered_while_a_reviewer_is_held(self):
        reviewer = control.create(self.root, "claude_reviewer")
        process = self.stand_in()
        control.attach(self.root, reviewer["id"], process.pid)
        control.note_cli_exec(self.root, reviewer["id"])
        board.register(self.root, "qa", "REVIEW", vendor="Anthropic", session_id=reviewer["id"])
        self.hold(reviewer["id"], "claude")
        delivery, _, _ = self.delivery_with_task()
        self.freeze(delivery["id"])
        self.assertEqual([(a["action"], a["session_id"]) for a in self_heal.plan(self.root)], [("restart", delivery["id"])])

    def test_a_plan_made_a_tick_ago_is_dropped_at_execution_when_the_line_has_appeared_since(self):
        session, agent, _ = self.delivery_with_task()
        self.freeze(session["id"])
        action = self_heal.plan(self.root)[0]
        self.assertEqual(action["action"], "restart")
        self.hold(session["id"])                       # the line appears between planning and acting
        result = self_heal.execute(self.root, action, self.launch, self.create)
        self.assertEqual(result["action"], "restart")
        self.assertIn("usage-limit message", result["skipped"])
        current = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertEqual(current["status"], "running")      # not stopped
        self.assertEqual(self.launched, [])

    def test_a_reviewer_start_planned_before_the_hold_is_dropped_at_execution(self):
        session = control.create(self.root, "claude_reviewer")
        process = self.stand_in()
        control.attach(self.root, session["id"], process.pid)
        control.note_cli_exec(self.root, session["id"])
        board.register(self.root, "qa", "REVIEW", vendor="Anthropic", session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["reviewer_needed"] = {"requested_at": _ago(6 * 60), "request_id": "review-1"}
        action = {"action": "start_reviewer", "kind": "claude_reviewer", "self_heal_key": "role:reviewer", "reason": "a review has waited"}
        self.hold(session["id"], "claude")
        result = self_heal.execute(self.root, action, self.launch, self.create)
        self.assertIn("would hit the same limit", result["skipped"])
        self.assertEqual(self.created, [])

    def test_a_paused_project_takes_no_recovery_action_at_execution(self):
        session, _, _ = self.delivery_with_task()
        self.freeze(session["id"])
        action = self_heal.plan(self.root)[0]
        with board.locked_state(self.root, allow_paused=True) as state:
            state["project_pause"] = {"status": "paused", "pause_id": "p1"}
        result = self_heal.execute(self.root, action, self.launch, self.create)
        self.assertIn("not active", result["skipped"])

    def test_a_held_terminal_that_dies_is_an_ordinary_exit_and_is_brought_back(self):
        session, agent, process = self.delivery_with_task()
        self.hold(session["id"])
        process.kill()
        process.wait(timeout=5)
        with control.locked_state(self.root) as state:
            pass
        ended = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertEqual(ended["status"], "exited")
        self.assertIsNone(ended["attention_reason"])
        self.assertNotIn("limit_seen_at", ended)
        self.assertTrue(ended["last_limit_seen_at"])
        self.assertEqual([a["action"] for a in self_heal.plan(self.root)], ["relaunch_ended"])


if __name__ == "__main__":
    unittest.main()
