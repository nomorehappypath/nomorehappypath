# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests for the Go ahead skill-list freeze: the owner's exact ticks become
task_skills_agreed, every later builder/reviewer message names them with
full paths (or "none"), and an unknown slug is dropped —
SKILL_LIBRARY_SPEC.md requirement 8.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, control
from tests.test_requirements_contract import context, delivery_with_direction


class GoAheadSkillsFreezeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        self.agent = delivery_with_direction(self.root, "GOAHEAD-SKILLS")
        board.record_requirement_proposal(
            self.root, self.agent["id"], "Final agreed requirements: build a small service.",
            skills=[{"name": "backend", "reason": "service code"}],
        )

    def _decide(self, skills):
        proposal = board.snapshot(self.root)["requirement_proposals"]["GOAHEAD-SKILLS"]
        return board.record_requirements_decision(
            self.root, "GOAHEAD-SKILLS", "go_ahead", version=proposal["version"],
            token=(proposal.get("contract_check") or {}).get("token", ""), skills=skills,
        )

    def test_the_owners_ticks_are_frozen_as_skills_agreed(self):
        self._decide(["backend", "security"])
        agreed = board.skills_agreed_for_task(board.snapshot(self.root), "GOAHEAD-SKILLS")
        self.assertEqual(sorted(item["slug"] for item in agreed), ["backend", "security"])
        self.assertTrue(all(Path(item["path"]).is_file() for item in agreed))

    def test_an_unknown_slug_is_dropped_not_refused(self):
        self._decide(["backend", "not-a-real-skill"])
        agreed = board.skills_agreed_for_task(board.snapshot(self.root), "GOAHEAD-SKILLS")
        self.assertEqual([item["slug"] for item in agreed], ["backend"])

    def test_no_ticks_records_none_agreed(self):
        self._decide([])
        self.assertEqual(board.skills_agreed_for_task(board.snapshot(self.root), "GOAHEAD-SKILLS"), [])

    def test_the_routed_instruction_names_the_agreed_skills_with_full_paths(self):
        event = self._decide(["backend"])
        agent = board.snapshot(self.root)["agents"][self.agent["id"]]
        session_id = agent["session_id"]
        items = control.take_instructions(self.root, session_id)
        routed_text = "\n".join(item.get("text", "") for item in items)
        self.assertIn("SKILLS FOR THIS TASK (agreed by the owner)", routed_text)
        self.assertIn("Server (back end):", routed_text)
        self.assertIn("backend/SKILL.md", routed_text)
        del event

    def test_the_routed_instruction_says_none_when_nothing_is_ticked(self):
        self._decide([])
        agent = board.snapshot(self.root)["agents"][self.agent["id"]]
        items = control.take_instructions(self.root, agent["session_id"])
        routed_text = "\n".join(item.get("text", "") for item in items)
        self.assertIn("SKILLS FOR THIS TASK: none.", routed_text)

    def test_the_reviewer_routing_message_also_names_the_agreed_skills(self):
        # Second reviewer finding: the owner's go-ahead message to the builder named the
        # agreed skills, but the Reviewer's own later routing message (route_open_reviews)
        # did not. It must, reading the frozen list fresh rather than carrying a stale copy.
        self._decide(["backend"])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        with board.locked_state(self.root) as state:
            state.setdefault("qa_requests", {})["review-goahead-skills"] = {
                "id": "review-goahead-skills", "task": "GOAHEAD-SKILLS", "status": "open", "stage": board.INDEPENDENT_REVIEW,
                "developer_id": self.agent["id"], "requested_at": board.now(), "cycle": 1, "claimed_by": None,
                "phase": "final_acceptance", "subtask": "", "chunk": "final", "review_wait_started_at": board.now(),
                "delivery_state": "passed",
            }
        board.route_open_reviews(self.root, retry_seconds=0)
        items = control.take_instructions(self.root, reviewer_session["id"])
        routed_text = "\n".join(item.get("text", "") for item in items)
        self.assertIn("SKILLS FOR THIS TASK (agreed by the owner)", routed_text)
        self.assertIn("backend/SKILL.md", routed_text)
        del reviewer

    def test_a_replacement_builder_also_receives_the_agreed_skills(self):
        # Third reviewer finding: the Go ahead message and the reviewer routing message
        # both name the agreed skills, but a replacement agent resuming the task after the
        # original one died received only "Resume <task>", with no memory of the agreement.
        self._decide(["backend"])
        with board.locked_state(self.root) as state:
            state["agents"][self.agent["id"]]["active"] = False
        replacement_session = control.create(self.root, "codex_delivery")
        replacement = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=replacement_session["id"])
        board.resume_task(self.root, replacement["id"], self.agent["id"], "GOAHEAD-SKILLS")
        items = control.take_instructions(self.root, replacement_session["id"])
        routed_text = "\n".join(item.get("text", "") for item in items)
        self.assertIn("SKILLS FOR THIS TASK (agreed by the owner)", routed_text)
        self.assertIn("backend/SKILL.md", routed_text)


if __name__ == "__main__":
    unittest.main()
