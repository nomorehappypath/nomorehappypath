# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A review whose stored Delivery evidence is gone is withdrawn by the board, and Delivery is told the one repair (2026-11-04).

Before: the Reviewer's reserve was refused ("ledger is missing"), the route was re-sent ten times, and the CTO asked the owner to
restart the Reviewer. Now the board frees the scope and tells Delivery to file request-review again.

Run:  PYTHONPATH=. python3 -m unittest tests.test_review_evidence_lost -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

from harness import board, control
from tests import test_board as _tb


class ReviewEvidenceLostTests(unittest.TestCase):
    setUp = _tb.BoardTests.setUp
    tearDown = _tb.BoardTests.tearDown
    ledger = _tb.BoardTests.ledger
    qa_command = _tb.BoardTests.qa_command
    declare_chunks = _tb.BoardTests.declare_chunks
    delivery = _tb.BoardTests.delivery

    def test_lost_evidence_withdraws_the_request_tells_delivery_and_frees_the_scope(self):
        dev = self.delivery("TASK-LOST")
        self.declare_chunks(dev["id"], [("core", "evidence loss check")])
        qa = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        ledger = self.ledger("lost-delivery.md")
        request = board.request_review(self.root, dev["id"], ledger, "review", chunk="core", test_command=self.qa_command())
        stored = Path(self.root) / str(request["ledger"]) if not Path(str(request["ledger"])).is_absolute() else Path(str(request["ledger"]))
        self.assertTrue(stored.is_file())
        stored.unlink()      # what happened on 2026-11-04: the board's own copy vanished after a garbled answer

        with self.assertRaisesRegex(ValueError, "withdrew request .* told Delivery to file request-review again") as refused:
            board.reserve_qa(self.root, qa["id"], request["id"])
        self.assertIn("ledger is missing", str(refused.exception))
        state = board.snapshot(self.root)
        withdrawn = state["qa_requests"][request["id"]]
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertIn("is gone", withdrawn["withdrawn_reason"])
        self.assertEqual([e for e in state["events"] if e["kind"] == "review_withdrawn_evidence_lost"][-1]["request_id"], request["id"])
        session_id = state["agents"][dev["id"]]["session_id"]
        told = [item["text"] for item in control.take_instructions(self.root, session_id) if item.get("source") == "review-evidence-lost"]
        self.assertEqual(len(told), 1)
        self.assertIn("REVIEW EVIDENCE LOST", told[0])
        self.assertIn(request["id"], told[0])
        self.assertIn("request-review again", told[0])
        self.assertIn("--changes", told[0])

        # the scope is free: Delivery files again with its ledger and the Reviewer can reserve the new request
        again = board.request_review(self.root, dev["id"], self.ledger("lost-delivery-again.md"), "review again", chunk="core",
                                     changes="same work; evidence attached again", test_command=self.qa_command())
        self.assertNotEqual(again["id"], request["id"])
        self.assertEqual(board.reserve_qa(self.root, qa["id"], again["id"])["reserved_by"], qa["id"])

    def test_an_incomplete_ledger_is_refused_as_before_and_nothing_is_withdrawn(self):
        dev = self.delivery("TASK-KEPT")
        self.declare_chunks(dev["id"], [("core", "no withdrawal")])
        qa = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        request = board.request_review(self.root, dev["id"], self.ledger("kept-delivery.md"), "review", chunk="core", test_command=self.qa_command())
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["delivery_simulations"]["scenario_ids"] = []   # a different problem: nothing is missing
        try:
            board.reserve_qa(self.root, qa["id"], request["id"])
        except ValueError as refused:
            self.assertNotIn("withdrew", str(refused))
        self.assertNotEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "withdrawn")


class ApplicationScopeTests(unittest.TestCase):
    """Round 1 (Codex R1-1): an application subtask stayed "in review" after the withdrawal, so nothing could be filed again."""
    from tests.test_adaptive_planning import AdaptivePlanningSimulationTests as _App
    setUp = _App.setUp
    tearDown = _App.tearDown
    delivery = _App.delivery
    ledger = _App.ledger
    evidence = _App.evidence
    request = _App.request

    def test_lost_evidence_on_a_subtask_review_frees_the_subtask_for_a_new_review(self):
        agent = self.delivery("APP-LOST")
        board.define_delivery_plan(self.root, agent["id"], "application", "two capabilities")
        board.declare_subtasks(self.root, agent["id"], [
            {"id": "auth", "title": "Authentication", "acceptance_proof": "Users can authenticate", "dependencies": []},
        ])
        request = self.request(agent, "auth", "subtask_acceptance", subtask="auth")
        stored = Path(self.root) / str(request["ledger"]) if not Path(str(request["ledger"])).is_absolute() else Path(str(request["ledger"]))
        self.assertTrue(stored.is_file())
        stored.unlink()
        with self.assertRaisesRegex(ValueError, "withdrew"):
            board.reserve_qa(self.root, self.reviewer["id"], request["id"])
        item = board.snapshot(self.root)["delivery_plans"]["APP-LOST"]["subtasks"]["auth"]
        self.assertEqual(item.get("pipeline_status"), "in_progress")
        self.assertNotIn("active_review_request", item)
        again = self.request(agent, "auth-again", "subtask_acceptance", subtask="auth", changes="same work; evidence attached again")
        self.assertNotEqual(again["id"], request["id"])
        self.assertEqual(board.reserve_qa(self.root, self.reviewer["id"], again["id"])["reserved_by"], self.reviewer["id"])

if __name__ == "__main__":
    unittest.main()
