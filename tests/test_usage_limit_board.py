# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The board treats a terminal that shows its provider's usage-limit message as waiting, not stalled or signed out (2026-10-16_quota-handoff).

Before: such a terminal went quiet, was marked stalled and walked the restart ladder, or (for Gemini's quota line) was labelled
"needs sign-in". Now it has its own state, says what is observed once per incident, and is never marked stalled inside the grace
after the line clears.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from harness import attention, board, control
from tests import test_self_restart as base


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _incident(resets_in_hours: float = 3.0, kind: str = "announced") -> dict:
    seen = datetime.now().astimezone()
    return {"seen_at": seen.isoformat(timespec="seconds"), "resets_at": (seen + timedelta(hours=resets_in_hours)).isoformat(timespec="seconds"),
            "kind": kind, "when_text": "9:43pm", "provider": "codex"}


class UsageLimitBoardTests(base._Fixture):
    def silent_delivery(self):
        session, agent, process = self.delivery_with_task()
        with board.locked_state(self.root) as state:      # silent for hours: the ordinary stale rule would mark it stalled
            state["agents"][agent["id"]]["last_poll_at"] = _ago(6 * 3600)
            state["agents"][agent["id"]]["last_progress_at"] = _ago(6 * 3600)
            state["agents"][agent["id"]]["spawned_at"] = _ago(7 * 3600)
        return session, agent

    def agent(self, agent_id: str) -> dict:
        return board.snapshot(self.root)["agents"][agent_id]

    def events(self, kind: str) -> list:
        return [event for event in board.snapshot(self.root).get("events", []) if event.get("kind") == kind]

    def test_a_silent_agent_whose_terminal_shows_the_limit_is_waiting_not_stalled_and_says_so_once(self):
        session, agent = self.silent_delivery()
        board.mark_stalled(self.root)
        self.assertTrue(self.agent(agent["id"]).get("automatic_recovery_requested_at"), "control: without the line the same silence starts the ordinary recovery")
        control.record_attention(self.root, session["id"], attention.limit_reason("codex"), limit=_incident())
        for _ in range(3):
            board.mark_stalled(self.root)
        held = self.agent(agent["id"])
        self.assertEqual(held["liveness"], board.USAGE_LIMIT_LIVENESS)
        self.assertNotIn("automatic_recovery_requested_at", held, "no wake-up is routed to a held terminal")
        self.assertIn("The Delivery agent (Codex) shows Codex's usage-limit message; it announced", held["liveness_note"])
        self.assertNotIn("sign", held["liveness_note"].lower())
        self.assertEqual(len(self.events("agent_usage_limit")), 1, "one board line per incident, however often the watchdog looks")
        self.assertEqual(self.events("agent_needs_sign_in"), [])

    def test_a_nudge_that_did_not_end_the_message_changes_the_line_once(self):
        session, agent = self.silent_delivery()
        control.record_attention(self.root, session["id"], attention.limit_reason("codex"), limit=_incident(resets_in_hours=-1))
        board.mark_stalled(self.root)
        control.record_limit_nudge(self.root, session["id"], sent=True)
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["limit_nudged_at"] = _ago(attention.LIMIT_NUDGE_AFTER_SECONDS + 10)
        for _ in range(2):
            board.mark_stalled(self.root)
        self.assertIn("but still shows Codex's usage-limit message", self.agent(agent["id"])["liveness_note"])
        self.assertEqual(len(self.events("agent_usage_limit_nudged")), 1)

    def test_when_the_line_goes_the_agent_recovers_and_is_not_marked_stalled_inside_the_grace(self):
        session, agent = self.silent_delivery()
        control.record_attention(self.root, session["id"], attention.limit_reason("codex"), limit=_incident())
        board.mark_stalled(self.root)
        control.clear_attention(self.root, session["id"])
        board.mark_stalled(self.root)
        recovering = self.agent(agent["id"])
        self.assertEqual(recovering["liveness"], "recovering")
        self.assertEqual(len(self.events("agent_usage_limit_cleared")), 1)
        board.mark_stalled(self.root)
        self.assertNotIn("automatic_recovery_requested_at", self.agent(agent["id"]), "inside the grace the old silence starts no recovery")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["limit_cleared_at"] = _ago(attention.LIMIT_RESUME_GRACE_SECONDS + 30)
        board.mark_stalled(self.root)
        self.assertTrue(self.agent(agent["id"]).get("automatic_recovery_requested_at"), "after the grace the ordinary rule applies again")

    def test_gemini_quota_still_uses_the_signed_out_path_unchanged(self):
        session, agent = self.silent_delivery()
        control.record_attention(self.root, session["id"], attention.gemini_quota_reason("165"))
        board.mark_stalled(self.root)
        self.assertEqual(self.agent(agent["id"])["liveness"], board.SIGNED_OUT_LIVENESS)

    def test_a_held_terminal_is_not_an_owner_alarm_and_the_snapshot_carries_its_plain_notice(self):
        session, _ = self.silent_delivery()
        control.record_attention(self.root, session["id"], attention.limit_reason("codex"), limit=_incident())
        self.assertEqual(control.waiting_sessions(self.root), [], "nothing for the owner to answer: no red banner")
        notices = control.limit_notices(self.root)
        self.assertEqual([item["id"] for item in notices], [session["id"]])
        self.assertIn("If this is not a real limit, type a message into its terminal", notices[0]["notice"])
        snapshot_session = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertEqual(snapshot_session["limit_notice"], notices[0]["notice"])
        control.record_attention(self.root, session["id"], attention.PERMISSION_REASON)      # a real prompt replaces the line
        self.assertEqual(len(control.waiting_sessions(self.root)), 1)
        self.assertNotIn("limit_notice", next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"]))

    def test_incident_fields_reset_for_a_new_incident_and_are_kept_for_a_repeat(self):
        session, _ = self.silent_delivery()
        first = _incident()
        control.record_attention(self.root, session["id"], attention.limit_reason("codex"), limit=first)
        control.record_limit_nudge(self.root, session["id"], sent=True)
        control.record_attention(self.root, session["id"], attention.limit_reason("codex"), limit=first)      # a redraw of the same one
        record = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertTrue(record["limit_nudged_at"])
        second = dict(first, seen_at=(datetime.fromisoformat(first["seen_at"]) + timedelta(hours=5)).isoformat(timespec="seconds"))
        control.record_attention(self.root, session["id"], attention.limit_reason("codex"), limit=second)     # a new incident re-arms
        record = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertNotIn("limit_nudged_at", record)


if __name__ == "__main__":
    unittest.main()
