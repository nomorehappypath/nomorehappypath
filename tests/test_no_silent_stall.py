# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""No silent stall: the harness recovers a silent agent by itself (owner order 2026-10-15).

Studio, task jls-studio-honesty-fix: Delivery sent no board heartbeat for 8.5 minutes, one wake-up went
unanswered, the open control-plane hold held the next wake-up back for 20 minutes, and only the CTO
noticed and did Delivery's job. These tests drive the REAL board and control state with stand-in
processes for terminals, and a fake launcher that records what would be opened. Time is simulated by
passing `current` to the self-heal pass; heartbeats are written relative to the real clock.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import board, board_viewer, claude_inbox, control, gemini_runtime, self_heal
from tests.test_self_restart import _Fixture, _ago

ROOT = Path(__file__).resolve().parents[1]
HOLD = {"status": "open", "source": "repeated_refusal:status", "reason": "The same operation was refused 5 times: status note is required and must be 240 characters or fewer"}


def flat(path: str) -> str:
    return re.sub(r"\s+", " ", (ROOT / path).read_text(encoding="utf-8"))


class _Stall(_Fixture):
    def setUp(self):
        super().setUp()
        self.base = datetime.now(timezone.utc)
        self.window, self.step = board.stall_settings({})

    def create(self, kind: str, continues: str = "") -> dict:
        self.created.append(kind)
        return control.create(self.root, kind, continues=continues)

    def at(self, seconds: float) -> datetime:
        return self.base + timedelta(seconds=seconds)

    def events(self, kind: str) -> list:
        return [e for e in board.snapshot(self.root)["events"] if e["kind"] == kind]

    def cards(self) -> list:
        return [c for c in board.snapshot(self.root).get("owner_actions", {}).values() if c.get("status") == "open"]

    def hold(self, task: str) -> None:
        with board.locked_state(self.root) as state:
            state.setdefault("control_plane_holds", {})[task] = {**HOLD, "task": task, "recorded_at": _ago(3000)}

    def silent(self, agent_id: str, seconds: float) -> None:
        stamp = _ago(seconds)
        with board.locked_state(self.root) as state:
            agent = state["agents"][agent_id]
            agent.update({"last_poll_at": stamp, "last_progress_at": None if agent["role"] == "cto" else stamp, "spawned_at": stamp,
                          "last_status_at": stamp})
            agent.pop("session_reattached_at", None)

    def reviewer(self, task: str = "BRIEF-TASK") -> tuple[dict, dict, object]:
        session = control.create(self.root, "claude_reviewer")
        process = self.stand_in()
        control.attach(self.root, session["id"], process.pid)
        control.note_cli_exec(self.root, session["id"])
        agent = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-1"] = {
                "id": "review-1", "task": task, "status": "open", "routed_to": agent["id"], "developer_id": "",
                "requested_at": _ago(900), "phase": "final_acceptance", "cycle": 1, "claimed_by": "", "review_wait_started_at": "", "challenge_ledger": {"scenarios": ["kept evidence"]},
            }
        return session, board.snapshot(self.root)["agents"][agent["id"]], process

    def cto(self) -> tuple[dict, dict, object]:
        session = control.create(self.root, "claude_cto")
        process = self.stand_in()
        control.attach(self.root, session["id"], process.pid)
        control.note_cli_exec(self.root, session["id"])
        agent = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        return session, board.snapshot(self.root)["agents"][agent["id"]], process

    def end_terminals(self, agent_ids: list[str]) -> None:
        """What the supervisor does when a stopped terminal's process ends."""
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=0.01)
                except subprocess.TimeoutExpired:
                    pass
        time.sleep(0.2)
        for agent_id in agent_ids:
            agent = board.snapshot(self.root)["agents"].get(agent_id)
            if agent is None:
                continue   # a Reviewer or CTO that went offline has already left the hot board
            session = next(s for s in control.snapshot(self.root)["sessions"] if s["id"] == agent["session_id"])
            if agent.get("active") and session["status"] not in control.ACTIVE_STATUSES:
                board.offline(self.root, agent_id, "visible CLI terminal ended", transport_ended=True)


class LiveCaseRegressionTests(_Stall):
    """The Studio case: alive but busy terminal, one wake unanswered, an open control-plane hold."""

    def live_case(self):
        session, agent, process = self.delivery_with_task("jls-studio-honesty-fix")
        self.hold("jls-studio-honesty-fix")
        self.silent(agent["id"], 360)   # last heartbeat 6 minutes ago
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({
                "liveness": "stalled", "recovery_state": "automatic_failed",
                "automatic_recovery_requested_at": _ago(140), "consecutive_stalls": 1})
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["last_output_at"] = _ago(5)   # it is still printing: not frozen
        return session, agent, process

    def test_a_silent_busy_delivery_under_a_control_plane_hold_is_restarted_without_a_human(self):
        session, agent, process = self.live_case()
        # base behaviour: mark_stalled holds the next wake-up back for 20 minutes under the hold, self_heal sees a live terminal
        board.mark_stalled(self.root)
        self.assertEqual(self_heal.plan(self.root, self.at(10)) and [], [] , "sanity")
        first = self_heal.run_once(self.root, self.launch, self.at(10), create=self.create)
        self.assertEqual([r["action"] for r in first], ["stall_wake"])
        steps = self.events("agent_stall_step")
        self.assertEqual([e["step"] for e in steps], ["wake"])
        self.assertIn("has not checked the board", steps[0]["message"])
        second = self_heal.run_once(self.root, self.launch, self.at(10 + self.step + 5), create=self.create)
        self.assertEqual([(r["action"], r.get("phase")) for r in second], [("stall_relaunch", "stopping")])
        process.wait(timeout=5)
        self.assertTrue(board.snapshot(self.root)["agents"][agent["id"]]["restart_requested_at"])
        self.assertEqual(self.cards(), [], "a ladder that is still progressing raises no card")

    def test_the_hold_does_not_stop_the_first_rung(self):
        session, agent, _ = self.live_case()
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].pop("automatic_recovery_requested_at")   # nothing woke it yet
            state["agents"][agent["id"]].pop("recovery_state")
        results = self_heal.run_once(self.root, self.launch, self.at(1), create=self.create)
        self.assertEqual([r["action"] for r in results], ["stall_wake"])
        queued = control.queued_instructions(self.root)["inbox"][session["id"]]
        self.assertTrue(any(item["source"] == "stall-ladder" and "run your board poll command now" in item["text"] for item in queued))


class LadderTests(_Stall):
    def drive_to_replacement(self):
        session, agent, process = self.delivery_with_task("LADDER-TASK")
        self.silent(agent["id"], self.window + 30)
        self.assertEqual([r["action"] for r in self_heal.run_once(self.root, self.launch, self.at(0), create=self.create)], ["stall_wake"])
        results = self_heal.run_once(self.root, self.launch, self.at(self.step + 1), create=self.create)
        self.assertEqual([(r["action"], r.get("phase")) for r in results], [("stall_relaunch", "stopping")])
        process.wait(timeout=5)
        self.end_terminals([agent["id"]])
        back = self_heal.run_once(self.root, self.launch, self.at(self.step + 2), create=self.create)
        self.assertEqual(back, [{"action": "relaunch", "session_id": session["id"], "phase": "launched"}])
        return session, agent

    def test_step_two_relaunches_the_same_session_and_the_same_agent_comes_back(self):
        session, agent = self.drive_to_replacement()
        self.assertEqual([item["id"] for item in self.launched], [session["id"]], "the SAME session is resumed")
        again = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        self.assertEqual((again["id"], again["task"]), (agent["id"], "LADDER-TASK"))
        self.assertEqual([e["step"] for e in self.events("agent_stall_step")], ["wake", "relaunch"])
        # no hurry: just relaunched; a heartbeat now clears the ladder and nothing else happens
        board.poll(self.root, agent["id"])
        self.assertEqual(self_heal.run_once(self.root, self.launch, self.at(self.step + 10), create=self.create)[0]["action"], "stall_reset")
        self.assertNotIn("delivery:LADDER-TASK", board.snapshot(self.root).get("stall_ladder", {}))
        self.assertEqual(self.cards(), [])

    def test_step_three_replaces_the_terminal_continues_the_conversation_and_carries_the_task(self):
        session, agent = self.drive_to_replacement()
        board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].update({"cli_session_id": "cli-conversation-1", "cli_session_provider": "codex", "cli_launches": 2})
        t = 2 * self.step + 10
        results = self_heal.run_once(self.root, self.launch, self.at(t), create=self.create)
        self.assertEqual([(r["action"], r.get("phase")) for r in results], [("stall_replace", "stopping")])
        old = board.snapshot(self.root)["agents"][agent["id"]]
        self.assertFalse(old["active"])
        self.assertTrue(old["stopped_by_harness_at"] and not old.get("stopped_by_owner_at"), "a harness retirement, not an owner stop")
        for process in self.processes:
            process.wait(timeout=5)
        self.end_terminals([agent["id"]])
        launched_before = len(self.launched)
        opened = self_heal.run_once(self.root, self.launch, self.at(t + 1), create=self.create)
        self.assertEqual(opened[0]["action"], "stall_replace_launch")
        new_session = self.launched[launched_before]
        self.assertNotEqual(new_session["id"], session["id"])
        self.assertEqual(new_session["continues_session"], session["id"])
        self.assertEqual(new_session["cli_session_id"], "cli-conversation-1", "the saved conversation is handed on")
        fresh = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=new_session["id"])
        carried = board.snapshot(self.root)["agents"][fresh["id"]]
        self.assertEqual(carried["task"], "LADDER-TASK", "bound to the stalled agent's own task")
        self.assertEqual([e["step"] for e in self.events("agent_stall_step")], ["wake", "relaunch", "replace", "replace"])
        self.assertEqual(self.cards(), [])

    def retire(self, session_id: str, agent_id: str, **marker) -> None:
        with control.locked_state(self.root) as state:
            state["sessions"][session_id].update({"status": "stopped", "ended_at": _ago(30)})
        with board.locked_state(self.root) as state:
            state["agents"][agent_id].update({"active": False, "status": "offline", **marker})

    def test_a_replacement_bound_to_its_source_is_not_confused_by_an_unrelated_stopped_task(self):
        older, older_agent, _ = self.delivery_with_task("OLDER-OWNER-STOPPED-TASK")
        session, agent, _ = self.delivery_with_task("LADDER-TASK")
        self.retire(older["id"], older_agent["id"], stopped_by_owner_at="2026-01-01T00:00:00+00:00")
        self.retire(session["id"], agent["id"], stopped_by_harness_at=_ago(20))
        unbound = control.create(self.root, "codex_delivery")
        with board.locked_state(self.root) as state:
            self.assertEqual(board._stopped_task_to_resume(state, "x"), (older_agent["id"], "OLDER-OWNER-STOPPED-TASK"), "unbound pickup guesses the earliest")
        board.bind_replacement(self.root, unbound["id"], agent["id"], "LADDER-TASK")
        fresh = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=unbound["id"])
        self.assertEqual(board.snapshot(self.root)["agents"][fresh["id"]]["task"], "LADDER-TASK", "bound to its own source, not the older owner-stopped task")

    def test_a_failed_handoff_is_said_out_loud(self):
        session, agent, _ = self.delivery_with_task("HANDOFF-TASK")   # the source is still active: two owners would result
        fresh_session = control.create(self.root, "codex_delivery")
        board.bind_replacement(self.root, fresh_session["id"], agent["id"], "HANDOFF-TASK")
        fresh = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=fresh_session["id"])
        failed = self.events("replacement_handoff_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("could not take over HANDOFF-TASK", failed[0]["message"])
        self.assertEqual(board.snapshot(self.root)["agents"][fresh["id"]]["task"], board.AWAITING_OWNER_DIRECTION)

    def test_exactly_one_owner_card_when_the_ladder_is_exhausted_and_none_before(self):
        session, agent = self.drive_to_replacement()
        board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        t = 2 * self.step + 10
        self_heal.run_once(self.root, self.launch, self.at(t), create=self.create)
        for process in self.processes:
            process.wait(timeout=5)
        self.end_terminals([agent["id"]])
        self_heal.run_once(self.root, self.launch, self.at(t + 1), create=self.create)   # replacement opened, never answers
        self.assertEqual(self.cards(), [], "no card while the ladder is still progressing")
        for extra in range(5):
            self_heal.run_once(self.root, self.launch, self.at(t + 2 + (extra + 1) * (self.step + 5)), create=self.create)
        cards = self.cards()
        self.assertEqual(len(cards), 1, "exactly one plain owner card")
        text = cards[0]["why"]
        for needle in ("Delivery Agent", "silent", "woke it", "restarted its terminal", "replaced its terminal", "open its terminal"):
            self.assertIn(needle, text)
        self.assertEqual(len(self.events("self_heal_gave_up")), 1)
        self.assertTrue(board.snapshot(self.root)["agents"][agent["id"]].get("stall_exhausted"))
        # the viewer no longer calls an exhausted agent's busy output "owner action none"
        self.assertIn("&&!agent.stall_exhausted", board_viewer.PAGE)

    def test_the_card_clears_itself_when_the_agent_answers_and_the_ladder_resets(self):
        session, agent, _ = self.delivery_with_task("CLEARS-TASK")
        self.silent(agent["id"], self.window + 30)
        self_heal.run_once(self.root, self.launch, self.at(0), create=self.create)
        key = "engineering:CLEARS-TASK"
        with board.locked_state(self.root) as state:
            state["stall_ladder"][key].update({"step": 3, "steps": ["woke it", "restarted its terminal", "replaced its terminal"], "step_at": _ago(self.step + 5)})
        results = self_heal.run_once(self.root, self.launch, self.at(1), create=self.create)
        self.assertEqual([r["action"] for r in results], ["stall_card"])
        self.assertEqual(len(self.cards()), 1)
        board.poll(self.root, agent["id"])
        self.assertEqual(self.cards(), [], "the card clears on the agent's own poll")
        self_heal.run_once(self.root, self.launch, self.at(2), create=self.create)
        self.assertNotIn(key, board.snapshot(self.root).get("stall_ladder", {}))
        self.assertFalse(board.snapshot(self.root)["agents"][agent["id"]].get("stall_exhausted"))

    def test_after_the_card_no_other_path_restarts_or_announces(self):
        session, agent, _ = self.delivery_with_task("EXHAUSTED-TASK")
        with board.locked_state(self.root) as state:
            state.setdefault("stall_ladder", {})["engineering:EXHAUSTED-TASK"] = {"card": True, "step": 3, "since": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "step_at": _ago(10)}
        self.freeze(session["id"])
        self.assertEqual(self_heal.plan(self.root, self.at(5)), [], "the frozen-terminal path stands down once the card is raised")

    def test_a_failing_relaunch_is_capped_and_never_loops(self):
        session, agent, process = self.delivery_with_task("CAPPED-TASK")
        self.silent(agent["id"], self.window + 30)
        with board.locked_state(self.root) as state:
            state.setdefault("self_heal", {}).setdefault("attempts", {})["relaunch:engineering:CAPPED-TASK"] = [_ago(60)] * self_heal.MAX_ACTIONS_PER_HOUR
        self_heal.run_once(self.root, self.launch, self.at(0), create=self.create)
        results = self_heal.run_once(self.root, self.launch, self.at(self.step + 1), create=self.create)
        self.assertEqual(results[0].get("skipped"), "capped")
        self.assertIsNone(board.snapshot(self.root)["agents"][agent["id"]].get("restart_requested_at"), "no fourth restart this hour")


class GuardTests(_Stall):
    def stalled_delivery(self):
        session, agent, process = self.delivery_with_task("GUARD-TASK")
        self.silent(agent["id"], self.window + 60)
        return session, agent, process

    def plan(self, seconds: float = 0):
        return [a for a in self_heal.plan(self.root, self.at(seconds)) if str(a["action"]).startswith("stall_")]

    def test_a_stalled_agent_is_laddered_but_each_guard_stands_the_ladder_down(self):
        session, agent, process = self.stalled_delivery()
        self.assertEqual([a["action"] for a in self.plan()], ["stall_wake"])
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "paused"}
        self.assertEqual(self.plan(), [], "a paused project")
        with board.locked_state(self.root, allow_paused=True) as state:
            state["project_pause"] = {"status": "active"}
        control.record_attention(self.root, session["id"], "Please run /login · API Error: 401")
        self.assertEqual(self.plan(), [], "signed out: the owner's")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["attention_reason"] = None
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["status"] = "awaiting_owner"
        self.assertEqual(self.plan(), [], "waiting for the owner")
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["status"] = "working"
        self.assertEqual([a["action"] for a in self.plan()], ["stall_wake"])
        control.stop(self.root, session["id"])
        self.assertEqual(self.plan(), [], "the owner stopped the terminal")

    def test_an_open_decision_card_for_the_agent_and_a_standing_by_delivery_are_not_stalls(self):
        session, agent, _ = self.stalled_delivery()
        with board.locked_state(self.root) as state:
            state["owner_actions"] = {"a1": {"id": "a1", "status": "open", "kind": "decision", "for_agent": agent["id"], "title": "x"}}
        self.assertEqual(self.plan(), [])
        with board.locked_state(self.root) as state:
            state["owner_actions"] = {}
            state["agents"][agent["id"]]["task"] = board.AWAITING_OWNER_DIRECTION
        self.assertEqual(self.plan(), [])

    def test_a_reviewer_lease_exempts_only_while_it_is_current_and_bounded(self):
        session, agent, _ = self.reviewer()
        self.silent(agent["id"], self.window + 60)
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["review_execution"] = {"active": True, "request_id": "review-1", "command": "pytest", "started_at": _ago(120), "last_heartbeat_at": _ago(2)}
        self.assertEqual(self.plan(), [], "a live execution lease is respected")
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["review_execution"]["started_at"] = _ago(self_heal.STALL_LEASE_MAX_SECONDS + 60)
        self.assertEqual([a["action"] for a in self.plan()], ["stall_wake"], "a check running past the bound is a stall, not an exemption")

    def test_a_session_that_never_started_belongs_to_the_launch_failure_card(self):
        session = control.create(self.root, "codex_delivery")
        control.fail_launch(self.root, session["id"], "the harness could not reopen the terminal: no Terminal")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["launch_failure"] = {"reason": "the CLI never started", "at": _ago(5)}
        actions = self_heal.plan(self.root, self.at(0))
        self.assertEqual([a["action"] for a in actions], ["announce_launch_failure"])

    def test_the_ctos_heartbeat_is_its_poll_alone(self):
        _, delivery, _ = self.delivery_with_task("CTO-WATCHED")
        session, cto, _ = self.cto()
        window = self.window + board.CTO_MONITOR_INTERVAL_SECONDS
        with board.locked_state(self.root) as state:   # status posts are recent, the poll is not
            state["agents"][cto["id"]].update({"last_poll_at": _ago(window + 30), "last_progress_at": _ago(5), "spawned_at": _ago(window + 60)})
        self.assertEqual([(a["action"], a["agent_id"]) for a in self.plan() if a["agent_id"] == cto["id"]], [("stall_wake", cto["id"])])
        with board.locked_state(self.root) as state:
            state["agents"][cto["id"]]["last_poll_at"] = _ago(self.window + 60)   # a normal five-minute rhythm is healthy
        self.assertEqual([a for a in self.plan() if a["agent_id"] == cto["id"]], [])

    def test_a_plan_made_before_a_poll_does_not_act_on_it(self):
        session, agent, _ = self.stalled_delivery()
        self_heal.run_once(self.root, self.launch, self.at(0), create=self.create)
        planned = [a for a in self_heal.plan(self.root, self.at(self.step + 1)) if a["action"] == "stall_relaunch"]
        self.assertEqual(len(planned), 1)
        board.poll(self.root, agent["id"])   # the agent answers between plan and execute
        self.assertIn("skipped", self_heal.execute(self.root, planned[0], self.launch, self.create))
        self.assertIsNone(board.snapshot(self.root)["agents"][agent["id"]].get("restart_requested_at"))

    def test_resume_after_a_pause_is_given_a_fresh_grace(self):
        session, agent, _ = self.stalled_delivery()
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "active", "resumed_at": self.at(-5).isoformat()}
        self.assertEqual(self.plan(), [], "silence during a pause is not charged to the agent")

    def test_an_unconfirmed_exit_never_hands_the_conversation_to_a_replacement(self):
        session, agent, process = self.stalled_delivery()
        self_heal.run_once(self.root, self.launch, self.at(0), create=self.create)
        with board.locked_state(self.root) as state:
            state["stall_ladder"]["engineering:GUARD-TASK"].update({"step": 3, "step_at": _ago(1)})
            state["agents"][agent["id"]].update({"active": False, "replacement_requested_at": _ago(1), "stopped_by_harness_at": _ago(1)})
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].update({"status": "stopped", "ended_at": _ago(1), "exit_unconfirmed": True})
        self.assertEqual([a for a in self.plan(5) if a["action"] == "stall_replace_launch"], [])
        self_heal.run_once(self.root, self.launch, self.at(self.step + 20), create=self.create)
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("could not be confirmed closed", cards[0]["why"])
        self.assertEqual(self.created, [])


class ReviewerAndSettingsTests(_Stall):
    def test_replacing_a_reviewer_keeps_the_review_request_and_its_evidence(self):
        session, agent, process = self.reviewer()
        self.silent(agent["id"], self.window + 60)
        before = board.snapshot(self.root)["qa_requests"]["review-1"]
        board.request_agent_replacement(self.root, agent["id"], "stalled")
        after = board.snapshot(self.root)["qa_requests"]["review-1"]
        self.assertEqual(after["challenge_ledger"], before["challenge_ledger"])
        self.assertEqual(after["status"], "open")
        self.assertFalse((board.snapshot(self.root)["agents"].get(agent["id"]) or {}).get("active"))

    def test_settings_are_validated_and_never_zero(self):
        self.assertEqual(board.stall_settings({}), (board.STALL_WINDOW_DEFAULT_SECONDS, board.STALL_STEP_DEFAULT_SECONDS))
        self.assertEqual(board.stall_settings({"HARNESS_STALL_WINDOW_SECONDS": "600", "HARNESS_STALL_STEP_SECONDS": "90"}), (600, 90))
        for bad in ("0", "-5", "abc", "", "5", "1.5"):
            self.assertEqual(board.stall_settings({"HARNESS_STALL_WINDOW_SECONDS": bad, "HARNESS_STALL_STEP_SECONDS": bad}),
                             (board.STALL_WINDOW_DEFAULT_SECONDS, board.STALL_STEP_DEFAULT_SECONDS), bad)
        with mock.patch.dict(os.environ, {"HARNESS_STALL_WINDOW_SECONDS": "900"}):
            self.assertEqual(board.stall_settings()[0], 900)

    def test_the_watchdog_still_recovers_when_the_control_plane_tick_fails(self):
        from harness import project_worker
        calls = []
        watchdog = project_worker.ProjectWatchdog(self.root, interval_seconds=15, stale_after=240)
        watchdog.self_heal = lambda: calls.append("healed") or []
        with mock.patch("harness.control_plane.tick", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                watchdog.tick()
        self.assertEqual(calls, ["healed"])


class NoSilentStallInvariantTests(_Stall):
    """For EVERY role, with and without a control-plane hold: a stall ends in recorded steps and exactly one owner card."""

    def run_role(self, role: str, hold: bool):
        _, delivery, _ = self.delivery_with_task("INVARIANT-TASK")
        if role == "engineering":
            agent = delivery
        elif role == "qa":
            _, agent, _ = self.reviewer("INVARIANT-TASK")
            with board.locked_state(self.root) as state:
                state["qa_requests"]["review-1"]["routed_to"] = agent["id"]
        else:
            _, agent, _ = self.cto()
        if hold:
            self.hold("INVARIANT-TASK")
        silence = self.window + board.CTO_MONITOR_INTERVAL_SECONDS + 60 if role == "cto" else self.window + 60
        self.silent(agent["id"], silence)
        if role != "engineering":   # keep Delivery itself healthy so only this role is the stalled one
            self.silent(delivery["id"], 5)
        ids = [agent["id"]]
        seen_steps: list[str] = []
        for tick in range(0, 12):
            when = tick * (self.step // 2 + 3)
            self_heal.run_once(self.root, self.launch, self.at(when), create=self.create)
            self.end_terminals(ids)
            seen_steps = [e["step"] for e in self.events("agent_stall_step") if e["agent_id"] == agent["id"]]
            if role != "engineering":
                self.silent(delivery["id"], 5)
        return agent, seen_steps

    def check(self, role: str, hold: bool):
        agent, steps = self.run_role(role, hold)
        self.assertIn("wake", steps, f"{role} hold={hold}: the first rung is always recorded")
        cards = [c for c in self.cards() if c.get("for_agent") == agent["id"]]
        self.assertEqual(len(cards), 1, f"{role} hold={hold}: a stall must end in exactly one owner card, never silence (steps {steps})")
        self.assertEqual(len([e for e in self.events("self_heal_gave_up") if e["agent_id"] == agent["id"]]), 1)

    def test_delivery_no_hold(self):
        self.check("engineering", False)

    def test_delivery_with_hold(self):
        self.check("engineering", True)

    def test_reviewer_no_hold(self):
        self.check("qa", False)

    def test_reviewer_with_hold(self):
        self.check("qa", True)

    def test_cto_no_hold(self):
        self.check("cto", False)

    def test_cto_with_hold(self):
        self.check("cto", True)


class CtoRuleTests(unittest.TestCase):
    def test_the_cto_never_does_a_stalled_roles_deliverable_and_the_harness_owns_the_card(self):
        cto = flat("directives/CTO.md")
        mirror = flat("harness/directives/CTO_COMPLETION_DIRECTIVE.md")
        self.assertIn("the harness ladder (wake, restart, replace) recovers a silent role", cto)
        self.assertIn("never do the stalled Delivery or Reviewer deliverable yourself", cto)
        self.assertIn("Only the harness raises the one owner card when the ladder is exhausted", cto)
        self.assertIn("the harness ladder (wake, restart, replace) recovers a silent role", mirror)
        self.assertIn("never does the stalled Delivery or Reviewer deliverable itself", mirror)
        self.assertIn("only the harness raises the one owner card when the ladder is exhausted", mirror)
        size = len((ROOT / "directives/CTO.md").read_bytes()) + len(claude_inbox.AUTHORITY_NOTE.encode("utf-8")) + 2
        self.assertLessEqual(size, gemini_runtime.RULES_LIMIT_BYTES)


if __name__ == "__main__":
    unittest.main()
