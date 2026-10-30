# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Delivery says a locked check does not match reality; the Reviewer answers once (task 2026-10-26_check-deviation).

Run:  PYTHONPATH=. python3 -m unittest tests.test_check_deviation -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from harness import board, contract_check, control
from tests.test_contract_first_checks import TASK, TEXT, TEST_FILE, _Fixture, ledger, submission, write_json

WHY = "Check 1 assumes the answer has a greeting field, but the real answer only has a text field (curl shows {\"text\": \"hello\"})."


class CheckDeviationTests(_Fixture):
    def confirmed(self):
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.submit(reviewer, submission())
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        control.take_instructions(self.root, session["id"])      # the earlier wakes are read
        return agent, session, reviewer

    def block(self):
        return board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]

    def report(self, agent, why=WHY):
        return board.record_check_deviation(self.root, agent["id"], TASK, 1, "1.1", why)

    def fix(self, name="fix.json", extra=""):
        return write_json(self.base, name, {"checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE + extra}}]})

    def keep(self, name="keep.json"):
        return write_json(self.base, name, {"keep": True, "checks": []})

    def test_deviation_is_recorded_and_the_reviewer_is_woken_with_a_plain_message(self):
        agent, session, reviewer = self.confirmed()
        result = self.report(agent)
        self.assertEqual((result["status"], result["reviewer_woken"]), ("open", True))
        wake = [item for item in control.take_instructions(self.root, session["id"]) if item.get("source") == "contract-check"]
        self.assertEqual(len(wake), 1)
        self.assertIn("CHECK DEVIATION", wake[0]["text"])
        self.assertIn(WHY, wake[0]["text"])
        self.assertIn("Never ask the owner", wake[0]["text"])
        [item] = self.block()["deviations"]
        self.assertEqual((item["status"], item["check"], item["reviewer_id"]), ("open", "1.1", reviewer["id"]))
        self.assertFalse(board.snapshot(self.root).get("agent_questions"), "the owner is never asked")
        self.assertFalse([r for r in board.snapshot(self.root)["qa_requests"].values()], "no review request, nothing held")
        with self.assertRaisesRegex(ValueError, "already has a deviation"):
            self.report(agent)

    def test_reviewer_replaces_the_check_it_is_locked_again_and_red_proof_and_coverage_are_recomputed(self):
        agent, _session, reviewer = self.confirmed()
        self.report(agent)
        before = self.block()
        with mock.patch.object(board, "run_red_proof") as red:
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "the answer has a text field, not greeting", self.fix(extra="# real field\n"))
        red.assert_called_once_with(self.root, TASK, [1], "correction")
        after = self.block()
        self.assertNotEqual(before["checks"][0]["sha256"], after["checks"][0]["sha256"])
        self.assertNotEqual(before["checks_sha256"], after["checks_sha256"])
        self.assertEqual(after["corrections"][0]["replaced"][0]["sha256"], before["checks"][0]["sha256"])
        self.assertEqual(after["deviations"][0]["status"], "replaced")
        self.assertIn("text field", after["deviations"][0]["answer_reason"])
        self.assertTrue(after["labels"], "the coverage labels were recomputed")
        with self.assertRaisesRegex(ValueError, "no open builder deviation"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "keeping it after all", self.keep())

    def test_reviewer_rejects_the_check_stays_exactly_as_locked_and_a_second_answer_is_refused(self):
        agent, _session, reviewer = self.confirmed()
        with self.assertRaisesRegex(ValueError, "no open builder deviation"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "nothing to answer yet", self.keep("early.json"))
        self.report(agent)
        before = self.block()
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "the check is right because the spec names the greeting field", self.keep())
        after = self.block()
        self.assertEqual((after["checks"], after["checks_sha256"], after.get("corrections") or []), (before["checks"], before["checks_sha256"], []))
        self.assertEqual(after["deviations"][0]["status"], "kept")
        self.assertIn("the spec names", after["deviations"][0]["answer_reason"])
        with self.assertRaisesRegex(ValueError, "no open builder deviation"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "I changed my mind again", self.keep("again.json"))
        with self.assertRaisesRegex(ValueError, "already has a deviation"):
            self.report(agent)       # the same locked bytes are not argued twice

    def test_delivery_can_never_change_a_check_by_this_path_and_can_only_name_its_own_checks(self):
        agent, _session, _reviewer = self.confirmed()
        stored = self.block()["checks"]
        self.report(agent)
        self.assertEqual(self.block()["checks"], stored)
        with self.assertRaisesRegex(ValueError, "only the Reviewer"):
            board.record_contract_check_correction(self.root, agent["id"], TASK, 1, "I want it my way", self.keep())
        with self.assertRaisesRegex(ValueError, "only the Reviewer"):
            board.record_contract_check_correction(self.root, agent["id"], TASK, 1, "I want it my way", self.fix("mine.json"))
        with self.assertRaisesRegex(ValueError, "not a stored check"):
            board.record_check_deviation(self.root, agent["id"], TASK, 1, "9.9", WHY)
        with self.assertRaisesRegex(ValueError, "20 to"):
            board.record_check_deviation(self.root, agent["id"], TASK, 1, "1.1", "wrong")
        with self.assertRaisesRegex(ValueError, "only Delivery"):
            board.record_check_deviation(self.root, agent["id"], "OTHER-TASK", 1, "1.1", WHY)

    def test_not_allowed_once_the_final_review_is_requested_and_the_reviewer_is_not_needed_to_continue(self):
        agent, _session, _reviewer = self.confirmed()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-x"] = {"id": "review-x", "task": TASK, "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "status": "open", "cycle": 1, "requested_at": board.now(),
                                              "subtask": "", "chunk": "final", "developer_id": "", "claimed_by": "", "review_wait_started_at": ""}
        with self.assertRaisesRegex(ValueError, "final review has been requested"):
            self.report(agent)
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-x"]["status"] = "failed"        # a failed review sends Delivery back to building
        self.assertEqual(self.report(agent)["status"], "open")

    def test_with_no_reviewer_free_it_stays_open_for_the_final_reviewer_and_holds_nothing(self):
        agent, _session, reviewer = self.confirmed()
        with board.locked_state(self.root) as state:
            state["agents"][reviewer["id"]].update({"active": False})
        self.assertFalse(self.report(agent)["reviewer_woken"])
        self.assertEqual(self.block()["deviations"][0]["status"], "open")
        self.assertEqual(self.block()["checks"][0]["n"], "1.1")
        from harness import review_brief
        state = board.snapshot(self.root)
        request = {"task": TASK, "id": "review-1", "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "status": "open", "cycle": 1}
        shown = review_brief.build(board.project_context(self.root), state, request)["check_deviations"]
        self.assertEqual(shown[0]["check"], "1.1")
        self.assertIn("has not answered yet", shown[0]["reviewer"])
        self.assertEqual(shown[0]["builder_said"], WHY)

if __name__ == "__main__":
    unittest.main()
