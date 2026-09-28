# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #9 (2026-09-27): owner-action cards clear themselves.

A card left Mission Control only when the CTO ran owner-action-done, which it
never did, so the studio project showed 14 cards 19-28 hours old, every one
already done. Now the event that makes a card obsolete clears it: accept,
release or cancel for a task; stop, restart or a new session for an agent;
Go ahead or Modify for a requirements decision; a live Reviewer for "start a
Reviewer". A card nothing can clear expires after OWNER_ACTION_TTL, the same
request is never pinned twice, and each card says what will clear it.
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

from harness import board, board_viewer, control
from tests import test_board as _board_tests
from tests import test_reviewer_release as _release_tests


class _Fixture(unittest.TestCase):
    setUp = _board_tests.BoardTests.setUp
    tearDown = _board_tests.BoardTests.tearDown
    ledger = _board_tests.BoardTests.ledger
    qa_command = _board_tests.BoardTests.qa_command
    declare_chunks = _board_tests.BoardTests.declare_chunks
    delivery = _board_tests.BoardTests.delivery
    accept = _release_tests.AcceptanceTests.accept

    def cto(self):
        session = control.create(self.root, "claude_cto")
        self._cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        return self._cto

    def pin(self, title: str, **kwargs) -> dict:
        cto = getattr(self, "_cto", None) or self.cto()
        return board.record_owner_action(self.root, cto["id"], title, **kwargs)

    def card(self, action_id: str) -> dict:
        return board.snapshot(self.root)["owner_actions"][action_id]

    def open_cards(self) -> list[dict]:
        return [card for card in board.snapshot(self.root)["owner_actions"].values() if card["status"] == "open"]

    def assertCleared(self, action_id: str, outcome: str):
        card = self.card(action_id)
        self.assertEqual((card["status"], card["outcome"]), ("done", outcome), json.dumps(card, indent=2))
        cleared = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "owner_action_cleared" and e["action_id"] == action_id]
        self.assertEqual(len(cleared), 1, "one plain event records the clearing")
        self.assertEqual(cleared[0]["outcome"], outcome)

    def assertOpen(self, action_id: str):
        self.assertEqual(self.card(action_id)["status"], "open", json.dumps(self.card(action_id), indent=2))


class TaskCardTests(_Fixture):
    """Criterion 1: accept, release or cancel clears the task's cards."""

    def test_accepting_the_task_clears_its_card(self):
        self.delivery("SHIP")
        card = self.pin("SHIP is release-ready. Open Mission Control and click Accept.", task="SHIP")
        other = self.pin("OTHER is release-ready. Open Mission Control and click Accept.", task="OTHER")
        self.assertEqual(self.card(card["id"])["kind"], "task")
        self.accept("SHIP")
        self.assertCleared(card["id"], "Resolved: task accepted")
        self.assertOpen(other["id"])

    def test_releasing_the_task_clears_its_card(self):
        self.delivery("SHIP")
        cto = self.cto()
        card = self.pin("Delivery on SHIP is waiting for your answer in Mission Control.", task="SHIP", kind="task")
        board.record_release_ready(self.root, cto["id"], "SHIP", {key: True for key in board.RELEASE_REQUIRED_CHECKS})
        self.assertCleared(card["id"], "Resolved: task released")

    def test_cancelling_the_task_clears_its_card(self):
        dev = self.delivery("SHIP")
        card = self.pin("Answer the Delivery question on SHIP.", task="SHIP", kind="task")
        board.cancel_session_work(self.root, dev["session_id"])
        self.assertCleared(card["id"], "Resolved: task cancelled")

    def test_a_task_named_only_in_the_words_is_still_tied_to_it(self):
        self.delivery("brand-upgrade")
        card = self.pin("brand-upgrade is release-ready. Review it and click Accept.")
        self.assertEqual((self.card(card["id"])["kind"], self.card(card["id"])["task"]), ("task", "brand-upgrade"))
        self.accept("brand-upgrade")
        self.assertCleared(card["id"], "Resolved: task accepted")

    def test_a_rejected_release_leaves_the_card(self):
        self.delivery("SHIP")
        card = self.pin("Test SHIP and tell us what is wrong.", task="SHIP", kind="task")
        with board.locked_state(self.root) as state:
            board._event(state, "owner_release_decision_recorded", None, {"task": "SHIP", "decision": "rejected"})
        self.assertOpen(card["id"])


class AgentCardTests(_Fixture):
    """Criterion 2: stop, restart or a replaced session clears the agent's cards."""

    def waiting_delivery(self):
        session = control.create(self.root, "codex_delivery")
        return board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])

    def test_the_owner_stopping_the_agent_clears_its_card(self):
        dev = self.waiting_delivery()
        card = self.pin("Delivery is frozen. Stop it and start a fresh one.", for_agent=dev["id"])
        self.assertEqual(self.card(card["id"])["kind"], "agent")
        board.cancel_session_work(self.root, dev["session_id"])
        self.assertCleared(card["id"], "Resolved: agent stopped")

    def test_the_terminal_ending_clears_its_card(self):
        dev = self.waiting_delivery()
        card = self.pin("Delivery is stuck on text in its input.", for_agent=dev["id"])
        board.offline(self.root, dev["id"], "managed CLI session ended", transport_ended=True)
        self.assertCleared(card["id"], "Resolved: agent stopped")

    def test_a_restart_clears_its_card_and_a_card_about_another_agent_stays(self):
        dev = self.waiting_delivery()
        other = self.waiting_delivery()
        card = self.pin("Delivery terminal is frozen. Restart it.", for_agent=dev["id"])
        stays = self.pin("Delivery terminal is not responding. Restart it.", for_agent=other["id"])
        board.request_agent_restart(self.root, dev["id"], "frozen terminal")
        self.assertCleared(card["id"], "Resolved: agent restarted")
        self.assertOpen(stays["id"])

    def test_an_agent_named_in_the_words_is_the_target(self):
        dev = self.waiting_delivery()
        short = dev["id"].rsplit("-", 1)[0]
        card = self.pin(f"Delivery {short} went idle; restart it from Mission Control.")
        self.assertEqual((self.card(card["id"])["kind"], self.card(card["id"])["for_agent"]), ("agent", dev["id"]))
        board.request_agent_restart(self.root, dev["id"], "idle")
        self.assertCleared(card["id"], "Resolved: agent restarted")

    def test_a_status_note_about_an_unnamed_frozen_terminal_clears_when_a_worker_restarts(self):
        cto = self.cto()
        dev = self.waiting_delivery()
        board.status(self.root, cto["id"], "OWNER ACTION: Delivery Codex terminal is frozen. Reply yes to restart it.", "waiting")
        [card] = self.open_cards()
        self.assertEqual(card["kind"], "agent")
        board.status(self.root, cto["id"], "still watching", "working")  # CTO activity alone clears nothing
        self.assertOpen(card["id"])
        board.request_agent_restart(self.root, dev["id"], "frozen")
        self.assertCleared(card["id"], "Resolved: agent restarted")

    def test_a_replaced_session_and_a_sign_in_clear_the_agent_card(self):
        dev = self.waiting_delivery()
        replaced = self.pin("Delivery's terminal is gone. Restart it.", for_agent=dev["id"])
        with board.locked_state(self.root) as state:
            board._event(state, "project_resume_session_replaced", state["agents"][dev["id"]], {"task": dev["task"]})
        self.assertCleared(replaced["id"], "Resolved: agent session replaced")
        signed = self.pin("Delivery needs /login. Sign it in.", for_agent=dev["id"])
        with board.locked_state(self.root) as state:
            board._event(state, "agent_signed_in", state["agents"][dev["id"]], {"task": dev["task"]})
        self.assertCleared(signed["id"], "Resolved: agent signed in again")

    def test_an_unknown_agent_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown agent"):
            self.pin("Restart it.", for_agent="development-9999-abcdef")


class DecisionCardTests(_Fixture):
    """Criterion 3: Go ahead or Modify clears the requirements-decision card."""

    def proposal(self, task: str):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"OWNER DIRECTION — {task}")
        board.begin_task(self.root, agent["id"], task)
        board.record_requirement_proposal(self.root, agent["id"], f"Requirements for {task}: do exactly this.")
        return agent

    def test_go_ahead_clears_the_decision_card(self):
        self.proposal("PLAN")
        card = self.pin("Content-planning requirements await your decision. Open the task and choose.")
        self.assertEqual(self.card(card["id"])["kind"], "decision")
        board.record_requirements_decision(self.root, "PLAN", "go_ahead")
        self.assertCleared(card["id"], "Resolved: you chose Go ahead")

    def test_modify_clears_the_decision_card(self):
        self.proposal("PLAN")
        card = self.pin("Requirements for PLAN await your decision.", task="PLAN")
        self.assertEqual(self.card(card["id"])["kind"], "decision")
        board.record_requirements_decision(self.root, "PLAN", "modify", "Add the before/after runs.")
        self.assertCleared(card["id"], "Resolved: you chose Modify")

    def test_a_decision_on_another_task_leaves_the_card(self):
        self.proposal("PLAN")
        self.proposal("OTHER")
        card = self.pin("Requirements for OTHER await your decision.", task="OTHER", kind="decision")
        board.record_requirements_decision(self.root, "PLAN", "go_ahead")
        self.assertOpen(card["id"])


class ReviewerCardTests(_Fixture):
    """Criterion 4: a live Reviewer clears "start a Reviewer"."""

    def test_a_reviewer_registering_clears_the_card(self):
        cto = self.cto()
        board.status(self.root, cto["id"], "OWNER ACTION: Baseline part is ready for review but no reviewer is running. In Mission Control, start a Reviewer agent.", "waiting")
        [card] = self.open_cards()
        self.assertEqual(card["kind"], "reviewer")
        self.waiting_delivery_does_not_clear(card["id"])
        session = control.create(self.root, "claude_reviewer")
        board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        self.assertCleared(card["id"], "Resolved: a Reviewer is running")

    def waiting_delivery_does_not_clear(self, action_id: str):
        session = control.create(self.root, "codex_delivery")
        board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        self.assertOpen(action_id)


class ExpiryTests(_Fixture):
    """Criterion 5: a card nothing can clear expires after OWNER_ACTION_TTL."""

    def age(self, action_id: str, hours: float):
        with board.locked_state(self.root) as state:
            state["owner_actions"][action_id]["recorded_at"] = (
                datetime.now(timezone.utc) - timedelta(hours=hours)
            ).isoformat()

    def test_the_default_ttl_is_a_day(self):
        self.assertEqual(board.OWNER_ACTION_TTL, timedelta(hours=24))

    def test_an_old_unmatched_card_expires_on_the_watchdog_tick(self):
        card = self.pin("Run the film GPU proof", command="bash /tmp/release_film.sh")
        fresh = self.pin("Open the Desktop folder", command="open ~/Desktop")
        self.assertEqual(self.card(card["id"])["kind"], "other")
        self.age(card["id"], 25)
        board.mark_stalled(self.root)
        self.assertCleared(card["id"], board.OWNER_ACTION_EXPIRED)
        self.assertEqual(board.OWNER_ACTION_EXPIRED, "Expired: no longer current")
        self.assertOpen(fresh["id"])

    def test_a_card_waiting_for_its_event_does_not_expire(self):
        self.delivery("SHIP")
        card = self.pin("SHIP is release-ready. Click Accept.", task="SHIP")
        self.age(card["id"], 72)
        board.mark_stalled(self.root)
        self.assertOpen(card["id"])

    def test_an_expired_card_blocks_nothing_and_is_never_shown(self):
        card = self.pin("Run the film GPU proof", command="bash /tmp/release_film.sh")
        self.age(card["id"], 25)
        # Before the next sweep the page already leaves it out.
        compact = board_viewer._compact_dashboard_state(board.snapshot(self.root))
        self.assertNotIn(card["id"], compact["owner_actions"])
        # The sweep is one bookkeeping write inside the watchdog pass; the pass
        # still completes and the card's age never refuses any board write.
        board.mark_stalled(self.root)
        dev = self.delivery("NEXT")
        self.assertTrue(board.snapshot(self.root)["agents"][dev["id"]]["active"])


class DedupeTests(_Fixture):
    """Criterion 6: the same request about the same task/agent is one card."""

    def test_the_same_request_about_the_same_task_is_one_card(self):
        self.delivery("SHIP")
        first = self.pin("SHIP is release-ready. Click Accept.", task="SHIP")
        again = self.pin("SHIP release-ready - please click Accept now.", task="SHIP")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.open_cards()), 1)

    def test_the_same_note_about_one_agent_is_one_card(self):
        cto = self.cto()
        dev = self.waiting_delivery()
        short = dev["id"].rsplit("-", 1)[0]
        board.status(self.root, cto["id"], f"OWNER ACTION: Delivery {short} went idle; restart it.", "waiting")
        board.status(self.root, cto["id"], f"OWNER ACTION: Delivery {short} is frozen. Please restart it now.", "waiting")
        self.assertEqual(len(self.open_cards()), 1)

    def test_the_same_targetless_note_repeated_word_for_word_is_one_card(self):
        cto = self.cto()
        note = "OWNER ACTION: Baseline part is ready for review but no reviewer is running. In Mission Control, start a Reviewer agent."
        board.status(self.root, cto["id"], note, "waiting")
        board.status(self.root, cto["id"], note, "waiting")
        self.assertEqual(len(self.open_cards()), 1)

    def test_different_requests_that_name_no_task_or_agent_are_never_merged(self):
        """Review round 1 (BLOCKING): two unnamed-terminal requests both read as
        kind "agent" with no target, and the second silently returned the first
        card. Every kind is checked, so no empty target counts as the same one."""
        cto = self.cto()
        pairs = {
            "agent": ("Delivery Codex terminal is frozen. Reply yes to restart it.",
                      "The QA terminal is stuck on text in its input. Click into it and press Enter."),
            "reviewer": ("Baseline part is ready for review but no reviewer is running. Start a Reviewer agent.",
                         "Old reviewer is still pinned to the finished task. Stop it and start a new Reviewer."),
            "decision": ("Content-planning requirements await your decision.",
                         "Film requirements v2 await your decision: Go ahead or Modify."),
            "other": ("Run the film GPU proof", "Open the Desktop folder"),
        }
        for kind, (first, second) in pairs.items():
            with self.subTest(kind=kind):
                a = board.record_owner_action(self.root, cto["id"], first)
                b = board.record_owner_action(self.root, cto["id"], second)
                self.assertEqual((self.card(a["id"])["kind"], self.card(b["id"])["kind"]), (kind, kind))
                self.assertEqual((self.card(a["id"])["task"], self.card(a["id"])["for_agent"]), ("", ""))
                self.assertNotEqual(a["id"], b["id"], f"two different {kind} requests became one card")
                self.assertOpen(a["id"])
                self.assertOpen(b["id"])

    def test_different_agents_or_unrelated_requests_stay_separate(self):
        a = self.pin("Delivery is frozen. Restart it.", for_agent=self.waiting_delivery()["id"])
        b = self.pin("Delivery is frozen. Restart it.", for_agent=self.waiting_delivery()["id"])
        c = self.pin("Run the film GPU proof", command="bash /tmp/a.sh")
        d = self.pin("Open the Desktop folder", command="open ~/Desktop")
        self.assertEqual(len({a["id"], b["id"], c["id"], d["id"]}), 4)

    waiting_delivery = AgentCardTests.waiting_delivery


class FooterTests(_Fixture):
    """Criterion 7: the card says what clears it (the rendered proof is in
    test_owner_action_cards_rendered)."""

    def test_each_kind_says_what_clears_it(self):
        self.assertEqual(board.owner_action_clears_when({"kind": "task"}), "Clears when the task is accepted")
        self.assertEqual(board.owner_action_clears_when({"kind": "agent"}), "Clears when the agent restarts")
        self.assertEqual(board.owner_action_clears_when({"kind": "decision"}), "Clears when you choose Go ahead or Modify")
        self.assertEqual(board.owner_action_clears_when({"kind": "reviewer"}), "Clears when a Reviewer starts")
        recorded = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        other = {"kind": "other", "recorded_at": recorded.isoformat()}
        self.assertEqual(board.owner_action_clears_when(other, recorded), "Expires in 24 h")
        self.assertEqual(board.owner_action_clears_when(other, recorded + timedelta(hours=20, minutes=30)), "Expires in 4 h")
        self.assertEqual(board.owner_action_clears_when(other, recorded + timedelta(hours=23, minutes=59)), "Expires in 1 h")

    def test_the_page_no_longer_promises_the_cto_will_clear_it(self):
        self.assertNotIn("clears when the CTO records the outcome", board_viewer.PAGE.lower())

    def test_a_card_recorded_before_this_change_is_classified_on_the_page(self):
        cto = self.cto()
        with board.locked_state(self.root) as state:
            state.setdefault("owner_actions", {})["action-legacy"] = {
                "id": "action-legacy", "title": "Old reviewer is still pinned. Stop it and start a new Reviewer.",
                "command": "", "why": "", "task": "", "agent_id": cto["id"], "status": "open",
                "recorded_at": board.now(), "outcome": "", "done_at": "",
            }
        compact = board_viewer._compact_dashboard_state(board.snapshot(self.root))
        self.assertEqual(compact["owner_actions"]["action-legacy"]["clears_when"], "Clears when a Reviewer starts")


class CommandLineTests(_Fixture):
    """The CTO's owner-action command: before this change `--command` replaced
    the sub-command name, so a card with a command could not be pinned at all."""

    def run_cli(self, *argv: str) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            code = board.main(["--root", str(self.root), *argv])
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def test_a_card_with_a_command_and_a_target_is_pinned_from_the_command_line(self):
        cto = self.cto()
        dev = AgentCardTests.waiting_delivery(self)
        card = self.run_cli(
            "owner-action", "--agent", cto["id"], "--title", "Restart the Delivery terminal",
            "--command", "open -a Terminal", "--for-agent", dev["id"],
        )
        self.assertEqual((card["command"], card["kind"], card["for_agent"]), ("open -a Terminal", "agent", dev["id"]))
        done = self.run_cli("owner-action-done", "--agent", cto["id"], "--id", card["id"], "--outcome", "restarted")
        self.assertEqual(done["status"], "done")


if __name__ == "__main__":
    unittest.main()
