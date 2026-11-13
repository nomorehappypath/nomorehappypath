# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's skill ticks survive the card redrawing and are what Go ahead records (final review B1, 2026-11-13).

Codex found it on the real page: the owner ticked frontend instead of backend, the early review finished, the card was rewritten from the
builder's proposal and Go ahead (and the automatic Go ahead of "wait for the review") froze backend. The ticks and the fold now carry a
data-keep key made of the task and the proposal version, and the counter is recomputed from the restored ticks."""
from __future__ import annotations

import json
import unittest

from harness import board, board_viewer, control
from tests.mission_control_support import RenderedCase
from tests.test_requirements_contract import context, delivery_with_direction


class SkillTicksSurviveTheEarlyReview(RenderedCase):
    def walk(self, wait: bool, questions: bool = False):
        root = context(self.base)
        task = "TICKS-SURVIVE"
        developer = delivery_with_direction(root, task)
        session = control.create(root, "claude_reviewer")
        reviewer = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        board.record_requirement_proposal(root, developer["id"], "Final agreed requirements: a small web page shows the project name.",
                                          skills=[{"name": "backend", "reason": "The proposed page is served locally."}])
        submission = self.base / "early-review.json"
        submission.write_text(json.dumps({"criteria": [{
            "text": "The page shows the project name.", "plain": "You see the name of your project.",
            "why_not_checked": "This test covers the card interaction before construction."}],
            **({"questions": [{"text": "Which project name should the page show?", "options": ["Skill review web", "Review page"],
                               "recommended": 0, "affects": [1]}]} if questions else {})}))
        parent = board_viewer.make_handler(root, project_name="Skill ticks")

        class Handler(parent):
            def do_POST(self):
                if self.path == "/__complete_early_review":
                    self.send_json(200, board.record_contract_check_submission(root, reviewer["id"], task, 1, str(submission)))
                    return
                super().do_POST()

        body = r"""
await until(() => document.querySelector('[data-skills-block]'));
const ticks = () => [...document.querySelectorAll('[data-skill-tick]:checked')].map(n => n.dataset.skillTick);
window.confirm = () => true;
document.querySelector('[data-skill-tick="backend"]').click();
document.querySelector('.skill-fold').open = true;
document.querySelector('[data-skill-tick="frontend"]').click();
document.activeElement.blur();
out.ownerChoice = ticks();
if (__WAIT__) { document.querySelector('[data-req-wait]').click(); await refresh(); }
await fetch('/__complete_early_review', {method: 'POST'});
await refresh();
if (!__WAIT__) await until(() => document.querySelector(__QUESTIONS__ ? '[data-contract-questions]' : '[data-contract-check="ready"]'));
out.afterReview = ticks();
out.foldOpen = document.querySelector('.skill-fold')?.open;
out.counter = document.querySelector('.skills-count')?.textContent;
if (!__WAIT__) document.querySelector('[data-req-go]').click();
await wait(1000);
"""
        result = self.run_probe(self.serve(Handler), body.replace("__WAIT__", "true" if wait else "false").replace("__QUESTIONS__", "true" if questions else "false"))
        agreed = board.snapshot(root).get("task_skills_agreed", {}).get(task, [])
        return result, [item["slug"] for item in agreed]

    def test_the_owners_ticks_survive_the_early_review_and_go_ahead_freezes_them(self):
        result, agreed = self.walk(wait=False)
        self.assertEqual(result["ownerChoice"], ["frontend"])
        self.assertEqual(result["afterReview"], ["frontend"], "finishing the early review keeps the owner's ticks")
        self.assertTrue(result["foldOpen"], "and the fold he opened")
        self.assertIn("1 of 12 chosen", result["counter"])
        self.assertEqual(agreed, ["frontend"], "Go ahead freezes what he ticked, not the builder's proposal")

    def test_wait_for_the_review_then_start_uses_the_ticks_as_they_stand(self):
        result, agreed = self.walk(wait=True)
        self.assertEqual(result["afterReview"], ["frontend"])
        self.assertEqual(agreed, ["frontend"], "the automatic Go ahead of Wait uses the owner's ticks")

    def test_go_ahead_with_the_recommended_answers_keeps_the_owners_ticks(self):
        # review round 2, R2-1: while the early review asked a question the card had no skills block at all, so Go ahead on that card
        # saved no skills whatever the owner had ticked
        result, agreed = self.walk(wait=False, questions=True)
        self.assertEqual(result["ownerChoice"], ["frontend"])
        self.assertEqual(result["afterReview"], ["frontend"], "the question card still shows the owner's ticks")
        self.assertEqual(agreed, ["frontend"], "and Go ahead on it freezes them")


if __name__ == "__main__":
    unittest.main()
