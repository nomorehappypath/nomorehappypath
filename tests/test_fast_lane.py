# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Small change lane (task 2026-10-26_fast-lane).

A small task skips the Reviewer's question round, the coverage check and the red proof; the independent final review and the board's own
checks stay. The Reviewer may send the task back to the full lane once; the owner is never asked and nothing is held.

Run:  PYTHONPATH=. python3 -m unittest tests.test_fast_lane -v
"""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from harness import board, contract_check, control, review_brief
from tests.test_contract_first_checks import TASK, TEXT, _Fixture, submission, write_json

PLAIN_TEXT = "In plain words:\nThe button label says Save instead of Submit.\n\n" + TEXT


class FastLaneTests(_Fixture):
    def confirmed(self, small=True, reviewer_free=True):
        agent = self.delivery()
        session, reviewer = self.reviewer() if reviewer_free else (None, None)
        board.record_requirement_proposal(self.root, agent["id"], TEXT, small_change=small)
        return agent, session, reviewer

    def accept(self, agent, **decision):
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"], **decision)
        with mock.patch.object(board, "run_red_proof") as red:
            board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        return red

    def confirmation(self):
        return board.snapshot(self.root)["requirement_confirmations"][TASK]

    def brief(self):
        request = {"task": TASK, "id": "review-1", "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "status": "open", "cycle": 1}
        return review_brief.build(board.project_context(self.root), board.snapshot(self.root), request)

    def test_small_change_skips_questions_coverage_and_red_proof_but_keeps_the_independent_review(self):
        agent, session, _reviewer = self.confirmed(small=True)
        wakes = [item for item in control.take_instructions(self.root, session["id"]) if item.get("source") == "contract-check"]
        self.assertEqual(wakes, [], "no question round: the Reviewer is not asked to look first")
        check = self.proposal()["contract_check"]
        self.assertEqual((check["status"], check["reason"]), ("not_checked", "Small change: tests are not written in advance"))
        self.assertFalse(check.get("questions"))
        red = self.accept(agent)
        red.assert_not_called()                                    # no checks stored, so no red proof and no coverage
        block = self.confirmation()["contract_check"]
        self.assertEqual((block["checks"], block["lane"], self.confirmation()["small_change"]), ([], "small", True))
        self.assertNotIn("red_proof", block)
        self.assertFalse(board.snapshot(self.root).get("agent_questions"), "the owner is never asked")
        self.assertFalse(board.snapshot(self.root)["qa_requests"], "no review is requested or held by the lane")
        lane = self.brief()["lane"]                                # the final Reviewer is told, and may send it back once
        self.assertEqual((lane["name"], lane["may_send_back_to_full_lane_once"]), ("small change", True))

    def test_owner_checkbox_makes_it_small_while_the_reviewer_is_still_reading_and_the_wake_is_taken_back(self):
        agent, session, _reviewer = self.confirmed(small=False)
        self.assertEqual(self.proposal()["contract_check"]["status"], "reading")
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"], small_change=True)
        proposal = self.proposal()
        self.assertEqual((proposal["status"], proposal["small_change_by"], proposal["contract_check"]["reason"]), ("accepted", "owner", contract_check.SMALL_CHANGE_REASON))
        with mock.patch.object(board, "run_red_proof") as red:
            board.record_requirement_confirmation(self.root, agent["id"], proposal["text"])
        red.assert_not_called()
        self.assertTrue(self.confirmation()["small_change"])

    def test_owner_cannot_tick_it_after_the_reviewer_already_wrote_tests(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.submit(reviewer, submission())
        with self.assertRaisesRegex(ValueError, "already written tests"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"], small_change=True)
        self.assertEqual(self.proposal()["status"], "awaiting_owner")

    def test_full_lane_is_the_default_and_unchanged(self):
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.assertEqual(self.proposal()["contract_check"]["status"], "reading")
        self.assertEqual(len([i for i in control.take_instructions(self.root, session["id"]) if i.get("source") == "contract-check"]), 1)
        self.submit(reviewer, submission())
        red = self.accept(agent)
        red.assert_called_once()                                   # the red proof still runs on the stored checks
        self.assertNotIn("small_change", self.confirmation())
        self.assertEqual(self.confirmation()["contract_check"]["checks"][0]["n"], "1.1")
        self.assertNotIn("lane", self.brief())
        with self.assertRaisesRegex(ValueError, "not a Small change"):
            board.record_send_to_full_lane(self.root, reviewer["id"], TASK, "this changes behaviour, not just text")

    def test_reviewer_sends_it_back_once_the_contract_check_step_runs_and_a_second_send_back_is_refused(self):
        agent, session, reviewer = self.confirmed(small=True)
        self.accept(agent)
        control.take_instructions(self.root, session["id"])
        with self.assertRaisesRegex(ValueError, "8 to 300"):
            board.record_send_to_full_lane(self.root, reviewer["id"], TASK, "no")
        result = board.record_send_to_full_lane(self.root, reviewer["id"], TASK, "this changes behaviour, not just text")
        self.assertEqual(result["lane"], "full")
        [wake] = [item for item in control.take_instructions(self.root, session["id"]) if item.get("source") == "contract-check"]
        self.assertIn("FULL LANE", wake["text"])
        self.assertIn("this changes behaviour", wake["text"])
        self.assertEqual(self.proposal()["contract_check"]["status"], "reading")
        self.assertFalse(self.confirmation()["small_change"])
        with self.assertRaisesRegex(ValueError, "already sent back"):
            board.record_send_to_full_lane(self.root, reviewer["id"], TASK, "I would like to send it back again")
        # the step runs: questions are refused (the owner is never asked), checks are stored and the red proof runs
        with self.assertRaisesRegex(ValueError, "without questions"):
            self.submit(reviewer, submission(questions=[{"text": "Which greeting?", "options": ["hello", "hi"], "recommended": 0, "affects": [1]}]))
        with mock.patch.object(board, "run_red_proof") as red:
            outcome = self.submit(reviewer, submission())
        self.assertEqual(outcome["questions"], 0)
        red.assert_called_once_with(self.root, TASK)
        block = self.confirmation()["contract_check"]
        self.assertEqual((block["checks"][0]["n"], block["lane"], block["red_proof"]), ("1.1", "full", {"status": "pending"}))
        self.assertIn("this changes behaviour", block["lane_sent_back"]["reason"])
        self.assertEqual(self.brief()["lane"]["name"], "full (sent back from small change: this changes behaviour, not just text)")
        self.assertFalse(board.snapshot(self.root).get("agent_questions"))
        self.assertFalse(board.snapshot(self.root)["qa_requests"], "nothing was held")
        with self.assertRaisesRegex(ValueError, "early look at these requirements is over"):
            self.submit(reviewer, submission())                       # one submission only

    def test_only_the_other_company_reviewer_may_send_it_back_and_delivery_may_not(self):
        agent, _session, _reviewer = self.confirmed(small=True)
        self.accept(agent)
        _s2, same_vendor = self.reviewer(vendor=agent["vendor"])
        with self.assertRaisesRegex(ValueError, "other AI company|only the Reviewer"):
            board.record_send_to_full_lane(self.root, same_vendor["id"], TASK, "this changes behaviour, not just text")
        with self.assertRaisesRegex(ValueError, "only the Reviewer"):
            board.record_send_to_full_lane(self.root, agent["id"], TASK, "this changes behaviour, not just text")

    def test_delivery_proposes_it_with_one_flag_on_the_command(self):
        agent = self.delivery()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = board.main(["--root", str(self.root), "propose-requirements", "--agent", agent["id"], "--text", PLAIN_TEXT, "--small-change"])
        self.assertEqual(code, 0)
        self.assertTrue(self.proposal()["small_change"])
        self.assertEqual(self.proposal()["small_change_by"], "delivery")


if __name__ == "__main__":
    unittest.main()
