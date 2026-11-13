# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests for the project switch and the Gemini/CTO exclusions —
SKILL_LIBRARY_SPEC.md "Who gets it" and "Switching and updating" (S8, S18).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, control
from tests.test_requirements_contract import context, delivery_with_direction


class ProjectSwitchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def test_the_switch_defaults_on(self):
        self.assertTrue(board.project_skill_library_enabled(board.snapshot(self.root)))

    def test_turning_it_off_removes_the_catalogue_from_the_next_begin_task(self):
        board.set_project_skill_library(self.root, False)
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "OWNER DIRECTION — OFF-TASK: build the thing.")
        result = board.begin_task(self.root, agent["id"], "OFF-TASK")
        self.assertEqual(result["skill_library"], [])
        self.assertEqual(result["skill_library_unavailable"], "off")

    def test_a_gemini_builder_gets_no_catalogue_either(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="Google", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "OWNER DIRECTION — GEM-TASK: build the thing.")
        result = board.begin_task(self.root, agent["id"], "GEM-TASK")
        self.assertEqual(result["skill_library"], [])
        self.assertEqual(result["skill_library_unavailable"], "gemini")

    def test_a_claude_or_codex_builder_still_gets_the_full_catalogue(self):
        delivery_with_direction(self.root, "ON-TASK")
        state = board.snapshot(self.root)
        self.assertFalse(bool((state.get("task_skills_unavailable") or {}).get("ON-TASK")))

    def test_a_gemini_builders_own_proposal_is_frozen_as_unavailable_at_filing_time(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="Google", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "OWNER DIRECTION — GEM-PROPOSAL: build the thing.")
        board.begin_task(self.root, agent["id"], "GEM-PROPOSAL")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        proposal = board.snapshot(self.root)["requirement_proposals"]["GEM-PROPOSAL"]
        self.assertEqual(proposal["skills_unavailable"], "gemini")
        self.assertEqual(proposal["skills_proposed"], [])

    def test_the_rendered_card_omits_the_whole_block_when_a_proposal_is_marked_unavailable(self):
        # Reviewer finding: skillsBlockHtml used to render "0 of 12 chosen" plus the full
        # fold for an unavailable proposal, which still invited a tick, instead of nothing.
        self.assertIn("if(proposal.skills_unavailable)return'';", board_viewer.PAGE)
        self.assertIn("function skillsBlockHtml(proposal,name){", board_viewer.PAGE)


class GoAheadFreezeOverrideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def _decide(self, task, skills):
        proposal = board.snapshot(self.root)["requirement_proposals"][task]
        return board.record_requirements_decision(
            self.root, task, "go_ahead", version=proposal["version"],
            token=(proposal.get("contract_check") or {}).get("token", ""), skills=skills,
        )

    def test_the_switch_off_before_filing_overrides_any_ticks(self):
        agent = delivery_with_direction(self.root, "OFF-GOAHEAD")
        board.set_project_skill_library(self.root, False)
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        self._decide("OFF-GOAHEAD", ["backend"])
        state = board.snapshot(self.root)
        self.assertEqual(board.skills_agreed_for_task(state, "OFF-GOAHEAD"), [])
        self.assertEqual(state["task_skills_unavailable"]["OFF-GOAHEAD"], "off")

    def test_switching_off_after_filing_never_wipes_the_current_proposals_own_ticks(self):
        # Reviewer finding: the switch is project-wide and forward-looking ("takes effect for
        # the next proposal"); it must never retroactively erase ticks a proposal already
        # offered, filed while the switch was still on, just because the owner later flips it
        # (for future tasks) before clicking Go ahead on this one.
        agent = delivery_with_direction(self.root, "STILL-ON-GOAHEAD")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        board.set_project_skill_library(self.root, False)
        self._decide("STILL-ON-GOAHEAD", ["backend"])
        state = board.snapshot(self.root)
        self.assertEqual([item["slug"] for item in board.skills_agreed_for_task(state, "STILL-ON-GOAHEAD")], ["backend"])
        self.assertNotIn("STILL-ON-GOAHEAD", state.get("task_skills_unavailable") or {})

    def test_a_gemini_builders_ticks_are_also_overridden(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="Google", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "OWNER DIRECTION — GEM-GOAHEAD: build the thing.")
        board.begin_task(self.root, agent["id"], "GEM-GOAHEAD")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        self._decide("GEM-GOAHEAD", ["backend"])
        state = board.snapshot(self.root)
        self.assertEqual(board.skills_agreed_for_task(state, "GEM-GOAHEAD"), [])
        self.assertEqual(state["task_skills_unavailable"]["GEM-GOAHEAD"], "gemini")


class BriefingSkipsGeminiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def _decide(self, task, skills):
        proposal = board.snapshot(self.root)["requirement_proposals"][task]
        return board.record_requirements_decision(
            self.root, task, "go_ahead", version=proposal["version"],
            token=(proposal.get("contract_check") or {}).get("token", ""), skills=skills,
        )

    def test_a_gemini_reviewer_gets_no_skills_block_even_when_the_task_has_one(self):
        agent = delivery_with_direction(self.root, "SWITCH-ROUTE")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        self._decide("SWITCH-ROUTE", ["backend"])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Google", session_id=reviewer_session["id"])
        with board.locked_state(self.root) as state:
            state.setdefault("qa_requests", {})["review-switch-route"] = {
                "id": "review-switch-route", "task": "SWITCH-ROUTE", "status": "open", "stage": board.INDEPENDENT_REVIEW,
                "developer_id": agent["id"], "requested_at": board.now(), "cycle": 1, "claimed_by": None,
                "phase": "final_acceptance", "subtask": "", "chunk": "final", "review_wait_started_at": board.now(),
                "delivery_state": "passed",
            }
        board.route_open_reviews(self.root, retry_seconds=0)
        items = control.take_instructions(self.root, reviewer_session["id"])
        routed_text = "\n".join(item.get("text", "") for item in items)
        self.assertNotIn("SKILLS FOR THIS TASK", routed_text)
        del reviewer

    def test_a_gemini_replacement_gets_no_skills_block_either(self):
        agent = delivery_with_direction(self.root, "SWITCH-RESUME")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: build a small service.",
                                           skills=[{"name": "backend", "reason": "service code"}])
        self._decide("SWITCH-RESUME", ["backend"])
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["active"] = False
        replacement_session = control.create(self.root, "codex_delivery")
        replacement = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="Google", session_id=replacement_session["id"])
        board.resume_task(self.root, replacement["id"], agent["id"], "SWITCH-RESUME")
        items = control.take_instructions(self.root, replacement_session["id"])
        routed_text = "\n".join(item.get("text", "") for item in items)
        self.assertNotIn("SKILLS FOR THIS TASK", routed_text)


if __name__ == "__main__":
    unittest.main()
