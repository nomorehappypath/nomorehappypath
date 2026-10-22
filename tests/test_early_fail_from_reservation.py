# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A Reviewer that holds a reservation and has found a real failure can record an early FAIL (windows-walk batch review, round 5).

Run:  PYTHONPATH=. python3 -m unittest tests.test_early_fail_from_reservation -v
"""
from pathlib import Path
from unittest.mock import patch

from harness import board
from tests import test_execute_once as base


class EarlyFailTests(base.ExecuteOnceTests):
    def reserved_review(self):
        # created and reserved through the real board operations (request_review, reserve_qa): the request keeps challenge_ledger=None
        with patch.object(board, "claim_qa", side_effect=lambda root, agent, request_id, ledger: board.reserve_qa(root, agent, request_id)):
            qa, request, _challenge, _evidence = self._claimed_review()
        held = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual((held["status"], held["reserved_by"], held["challenge_ledger"]), ("reserved", qa["id"], None))
        failing = self.root / "evidence" / "frozen-candidate-fail.txt"
        failing.write_text("command: python3 -m unittest test_smoke\nresult: FAIL\nRan 1 tests\nFAILED (failures=1)\n")
        return qa, request, failing

    def test_a_reserved_reviewer_can_record_a_real_failure_without_a_ledger(self):
        qa, request, failing = self.reserved_review()
        board.qa_result(self.root, qa["id"], request["id"], "failed", "TASK DONE: NO - the frozen candidate's own tests fail (exit 2)", str(failing))
        state = board.snapshot(self.root)
        recorded = state["qa_requests"][request["id"]]
        self.assertEqual(recorded["status"], "failed")
        self.assertTrue(recorded["early_fail_from_reservation"])
        self.assertTrue(any(e.get("kind") == "qa_early_fail" for e in state["events"]), "auditable")
        self.assertTrue(recorded.get("created_repair_package_id"), "Delivery gets the repair package as for any FAIL")

    def test_the_early_path_is_for_failures_by_the_reserving_reviewer_only(self):
        qa, request, failing = self.reserved_review()
        with self.assertRaisesRegex(ValueError, "only the QA agent that claimed"):
            board.qa_result(self.root, qa["id"], request["id"], "passed", "TASK DONE: YES", str(failing))
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "reserved", "a PASS from a reservation changes nothing")
        other = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "only the QA agent that claimed"):
            board.qa_result(self.root, other["id"], request["id"], "failed", "TASK DONE: NO - someone else's request", str(failing))
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "reserved")
        with self.assertRaisesRegex(ValueError, "evidence"):
            board.qa_result(self.root, qa["id"], request["id"], "failed", "TASK DONE: NO - no proof", str(self.root / "missing.txt"))
        passing = self.root / "evidence" / "passing.txt"
        passing.write_text("command: python3 -m unittest test_smoke\nresult: PASS\nRan 1 tests\nOK\n")
        with self.assertRaisesRegex(ValueError, "shows the failure itself"):
            board.qa_result(self.root, qa["id"], request["id"], "failed", "TASK DONE: NO - invented", str(passing))
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "reserved", "a refused FAIL leaves the reservation as it was")

    def test_a_nonzero_exit_is_a_failure_and_a_reservation_during_delivery_evidence_is_not_eligible(self):
        qa, request, _failing = self.reserved_review()
        exit_log = self.root / "evidence" / "exit.txt"
        exit_log.write_text("command: make test\nExit code: 2\n")
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["delivery_state"] = "executing"
        with self.assertRaisesRegex(ValueError, "only the QA agent that claimed"):
            board.qa_result(self.root, qa["id"], request["id"], "failed", "TASK DONE: NO - too early", str(exit_log))
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]]["delivery_state"] = "passed"
        board.qa_result(self.root, qa["id"], request["id"], "failed", "TASK DONE: NO - the frozen candidate's make test exits 2", str(exit_log))
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "failed")

    def test_the_failure_is_recorded_even_when_the_workspace_moved_on(self):
        qa, request, failing = self.reserved_review()
        (self.root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(False)\n")   # Delivery changed the workspace
        board.qa_result(self.root, qa["id"], request["id"], "failed", "TASK DONE: NO - fails on the frozen candidate", str(failing))
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "failed")


# the inherited ExecuteOnceTests are not repeated here
for _name in [name for name in dir(base.ExecuteOnceTests) if name.startswith("test_")]:
    if _name not in EarlyFailTests.__dict__:
        setattr(EarlyFailTests, _name, None)


if __name__ == "__main__":
    import unittest
    unittest.main()
