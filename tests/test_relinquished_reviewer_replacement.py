# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A Reviewer that gave a review back must not keep the Reviewer limit full (task 2026-10-27_stuck-review-no-ui).

Acremuse audit: both Reviewers gave the final review back and stayed open; the two-terminal limit then stopped any replacement.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone

from harness import board, control, self_heal
from tests import test_self_restart as base


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class RelinquishedReviewerReplacementTests(base._Fixture):
    def reviewer(self):
        session = control.create(self.root, "claude_reviewer")
        process = self.stand_in()
        control.attach(self.root, session["id"], process.pid)
        control.note_cli_exec(self.root, session["id"])
        agent = board.register(self.root, "qa", "REVIEW", vendor="Anthropic", session_id=session["id"])
        return session, agent, process

    def test_idle_reviewers_that_gave_the_review_back_are_closed_then_a_replacement_starts(self):
        first, first_agent, first_process = self.reviewer()
        second, second_agent, second_process = self.reviewer()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-1"] = {
                "id": "review-1", "task": "T", "status": "open", "requested_at": _ago(900), "cycle": 1, "developer_id": "", "claimed_by": "", "review_wait_started_at": "",
                "relinquished_by": [first_agent["id"], second_agent["id"]],
            }
            state["reviewer_needed"] = {"requested_at": _ago(600), "request_id": "review-1"}
        with self.assertRaises(ValueError):
            control.create(self.root, "claude_reviewer")   # the limit really is full
        planned = self_heal.plan(self.root)
        self.assertEqual(sorted(a["action"] for a in planned), ["retire_relinquished_reviewer"] * 2,
                         "free the slots first; a start now would only fail at the limit")
        self_heal.run_once(self.root, self.launch, create=self.create)
        for process, agent in ((first_process, first_agent), (second_process, second_agent)):
            process.wait(timeout=5)
            board.offline(self.root, agent["id"], "visible CLI terminal ended", transport_ended=True)
        self.assertEqual([a["action"] for a in self_heal.plan(self.root)], ["start_reviewer"])
        self_heal.run_once(self.root, self.launch, create=self.create)
        self.assertEqual(self.created, ["claude_reviewer"])

    def test_a_reviewer_that_did_not_give_the_review_back_is_left_alone(self):
        _, agent, _ = self.reviewer()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-1"] = {"id": "review-1", "task": "T", "status": "open", "requested_at": _ago(900), "cycle": 1, "developer_id": "", "claimed_by": "", "review_wait_started_at": "", "relinquished_by": []}
            state["reviewer_needed"] = {"requested_at": _ago(600), "request_id": "review-1"}
        self.assertNotIn("retire_relinquished_reviewer", [a["action"] for a in self_heal.plan(self.root)])


class NoScreenTaskRuleTests(base.unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_the_reviewer_is_told_a_task_with_no_screen_needs_no_screen_capture_and_is_never_handed_back_for_it(self):
        chapter = (self.ROOT / "directives/AGENT.md").read_text(encoding="utf-8")
        card = (self.ROOT / "directives/cards/REVIEWER.md").read_text(encoding="utf-8")
        self.assertIn("A task with no screen needs no screen capture", chapter)
        self.assertIn("never a reason to hand the final review back", chapter)
        self.assertIn("A task with no screen (a report, a document, a data file) needs no screen-check", card)
