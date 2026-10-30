# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A review that could not run a check for want of the project's environment never starts a stronger model (owner's order 2026-10-28).

Run:  PYTHONPATH=. python3 -m unittest tests.test_review_environment_gap -v
"""
from __future__ import annotations

import unittest

from harness import review_rounds
from tests import test_reviewer_retry_model as retry

ENV_GAP = "TASK DONE: NOT TESTED — the race proof could not run: the reviewer's isolated runner lacks sqlmodel and has no test database URL"


class ClassifierTests(unittest.TestCase):
    def test_environment_words_make_an_environment_gap(self):
        for text in (ENV_GAP, "TASK DONE: NOT TESTED — No module named 'psycopg' in the sandbox",
                     "TASK DONE: NOT TESTED — cannot connect to the test database"):
            with self.subTest(text=text[:40]):
                self.assertTrue(review_rounds.is_environment_gap(text))

    def test_other_gaps_and_real_failures_are_not(self):
        self.assertFalse(review_rounds.is_environment_gap("TASK DONE: NOT TESTED — the report page cannot be opened here"))
        self.assertFalse(review_rounds.is_environment_gap("TASK DONE: NO — the report is wrong; the sandbox is fine"))


class NoStrongerModelTests(retry.Retry):
    def test_three_environment_gaps_start_no_fresh_reviewer_and_ask_the_cto_instead(self):
        qa = None
        for _ in range(3):
            request = self.request(changes="no code change; test it again")
            qa, _last = self.verdict(request, "failed", ENV_GAP)
        entry = self.entry()
        self.assertEqual(entry["status"], "needs_environment")
        self.assertNotEqual(entry.get("who"), "reviewer", "no stronger Reviewer is requested")
        state = self.state()
        kinds = [e.get("kind") for e in state["events"]]
        self.assertIn("review_needs_environment", kinds)
        self.assertNotIn("review_recovery_requested", kinds)
        self.assertEqual(review_rounds.refusal_for(state["review_escalations"], {"task": entry["task"], **entry["scope"], "stage": review_rounds.INDEPENDENT}), "",
                         "Delivery may ask for the review again once the environment is ready")

    def test_a_later_judgment_after_the_environment_is_ready_counts_normally(self):
        for _ in range(3):
            self.verdict(self.request(changes="no code change; test it again"), "failed", ENV_GAP)
        self.verdict(self.request(changes="environment prepared; test it again"), "failed", "TASK DONE: NO — the report is wrong in section 2")
        self.assertNotEqual(self.entry()["status"], "needs_environment")

    def test_a_real_defect_after_a_fresh_reviewers_environment_gap_goes_to_delivery_not_to_another_reviewer(self):
        # Codex round 1 (R1-1): three testing gaps -> one fresh Reviewer -> environment gap -> environment prepared -> a genuine defect
        self.gaps()
        fresh = self.start_fresh()
        self.use = fresh
        self.verdict(self.request(changes="no code change; the fresh Reviewer tests it"), "failed", ENV_GAP)
        self.assertEqual(self.entry()["status"], "needs_environment")
        self.verdict(self.request(changes="environment prepared; test it again"), "failed", "TASK DONE: NO — the report calculation is wrong", [retry.frozen.finding("B1")])
        entry = self.entry()
        self.assertEqual((entry["status"], entry["phase"], entry.get("who")), ("recovery", "requested", None), "ordinary Delivery recovery")
        self.assertEqual(entry["reviewer_attempts"], 1, "the one fresh Reviewer stays counted")
        self.assertIn("B1", entry["brief"])
        recoveries = [a for a in review_rounds_plan(self) if a["action"].startswith("review_recovery")]
        self.assertTrue(recoveries, "the planner acts on the Delivery recovery")
        self.assertEqual([a for a in recoveries if a.get("who") == "reviewer"], [], "the planner never starts another Reviewer")


def review_rounds_plan(case):
    from harness import self_heal
    return self_heal.plan(case.root)


if __name__ == "__main__":
    unittest.main()
