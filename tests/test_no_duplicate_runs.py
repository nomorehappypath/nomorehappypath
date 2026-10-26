# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The review process does not run the same thing twice on the same code (task 2026-10-26_no-duplicate-runs).

Owner, 2026-10-26: "can you check if nomorehappypath has such wasteful process?". These tests run the real board and count the real runner
calls: a stored check already seen on the unbuilt project is not probed again at request-review, and the Reviewer's execute-challenge does
not re-run a stored contract check the board already passed on that exact commit.

Run:  PYTHONPATH=. python3 -m unittest tests.test_no_duplicate_runs -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from harness import board, red_proof
from tests import test_contract_first_checks as first
from tests import test_repair_test_integrity as repair

TASK = repair.TASK


def counting():
    """Patch the board's runner so every call's gate is recorded while the real runner still runs."""
    gates: list[str] = []
    real = board._execute_scenario_simulations

    def spy(*args, **kwargs):
        gates.append(str((kwargs.get("certification") or {}).get("gate") or ""))
        return real(*args, **kwargs)

    return gates, mock.patch.object(board, "_execute_scenario_simulations", side_effect=spy)


class RedProofNotRepeatedTests(repair._Project):
    def reviewed(self):
        self.confirm([repair.sharp()])
        self.build({"greeting.txt": "hello\n"})
        gates, patch = counting()
        with patch:
            self.request()
        return [gate for gate in gates if gate.startswith("red-proof")]

    def test_a_check_already_labelled_at_confirmation_is_not_probed_again(self):
        self.assertEqual(self.reviewed(), [])
        self.assertEqual(self.block()["compare"], {"1": red_proof.FAILS_BEFORE})

    def test_a_changed_base_commit_is_probed_again(self):
        self.confirm([repair.sharp()])
        with board.locked_state(self.root) as state:      # the stored result belongs to a different base commit
            state["requirement_confirmations"][TASK]["contract_check"]["red_proof"]["checks"]["1.1"]["base_commit"] = "1" * 40
        self.build({"greeting.txt": "hello\n"})
        gates, patch = counting()
        with patch:
            self.request()
        self.assertEqual([gate for gate in gates if gate.startswith("red-proof")], ["red-proof:1.1"])

    def test_changed_check_bytes_are_probed_again(self):
        self.confirm([repair.sharp()])
        with board.locked_state(self.root) as state:
            state["requirement_confirmations"][TASK]["contract_check"]["red_proof"]["checks"]["1.1"]["sha256"] = "0" * 64
        self.build({"greeting.txt": "hello\n"})
        gates, patch = counting()
        with patch:
            self.request()
        self.assertEqual([gate for gate in gates if gate.startswith("red-proof")], ["red-proof:1.1"])

    def test_could_not_compare_is_never_reused(self):
        self.confirm([repair.sharp()])
        with board.locked_state(self.root) as state:
            state["requirement_confirmations"][TASK]["contract_check"]["red_proof"]["checks"]["1.1"]["label"] = red_proof.COULD_NOT_COMPARE
        self.build({"greeting.txt": "hello\n"})
        gates, patch = counting()
        with patch:
            self.request()
        self.assertEqual([gate for gate in gates if gate.startswith("red-proof")], ["red-proof:1.1"])


class ReviewerDoesNotRepeatTheBoardTests(unittest.TestCase):
    setUp = first.FinalReviewFlowTests.setUp
    confirm_with_checks = first.FinalReviewFlowTests.confirm_with_checks
    request = first.FinalReviewFlowTests.request

    def challenge(self, mutate=None):
        self.confirm_with_checks()
        request = self.request()
        if mutate:
            with board.locked_state(self.root) as state:
                mutate(state["qa_requests"][request["id"]])
        board.claim_qa(self.root, self.qa["id"], request["id"], str(self.root / "challenge.md"))
        gates, patch = counting()
        with patch:
            board.execute_challenge(self.root, self.qa["id"], request["id"])
        return request, gates, board.snapshot(self.root)["qa_requests"][request["id"]]

    def test_a_check_the_board_already_passed_on_this_commit_is_not_run_again_but_the_reviewers_own_row_is(self):
        request, gates, after = self.challenge()
        self.assertEqual([gate for gate in gates if gate.startswith("contract-check")], [])
        self.assertTrue(any(gate and not gate.startswith("contract-check") for gate in gates), "the Reviewer's own ledger command still ran")
        delivery, reviewer = after["contract_check_runs"]
        self.assertEqual((delivery["role"], reviewer["role"], reviewer["reused"]), ("delivery", "reviewer", True))
        self.assertEqual(reviewer["note"], "already run by the board on this exact commit")
        self.assertEqual(after["challenge_execution"]["contract_check_sha256"], request["contract_check_sha256"])
        evidence = self.base / "evidence.txt"
        evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
        board.qa_result(self.root, self.qa["id"], request["id"], "passed", "TASK DONE: YES - all good: the checks and my own scenario hold", str(evidence))
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "passed")

    def test_a_failed_or_other_commit_run_is_never_reused(self):
        for key, value in (("status", "failed"), ("commit", "0" * 40), ("passed_checks", {})):
            with self.subTest(key):
                self.setUp()
                _, gates, _ = self.challenge(lambda req, key=key, value=value: req["contract_check_runs"][0].__setitem__(key, value))
                self.assertEqual([gate for gate in gates if gate.startswith("contract-check")], ["contract-check:1.1"])

    def test_a_tampered_check_is_refused_not_reused(self):
        self.confirm_with_checks()
        request = self.request()
        block = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        path = block["checks"][0]["file"]
        with open(path, "ab") as handle:
            handle.write(b" ")
        board.claim_qa(self.root, self.qa["id"], request["id"], str(self.root / "challenge.md"))
        with self.assertRaisesRegex(ValueError, "changed or removed"):
            board.execute_challenge(self.root, self.qa["id"], request["id"])

    def test_a_corrected_check_runs_again(self):
        self.confirm_with_checks()
        request = self.request()
        fix = first.write_json(self.base, "fix.json", {"checks": [{"coverage": "checked", "ledger": first.ledger(), "files": {"test_hello.py": first.TEST_FILE + "# fixed\n"}}]})
        board.record_contract_check_correction(self.root, self.qa["id"], TASK, 1, "the check was wrong in one detail", fix)
        board.claim_qa(self.root, self.qa["id"], request["id"], str(self.root / "challenge.md"))
        gates, patch = counting()
        with patch:
            board.execute_challenge(self.root, self.qa["id"], request["id"])
        self.assertEqual([gate for gate in gates if gate.startswith("contract-check")], ["contract-check:1.1"])


if __name__ == "__main__":
    unittest.main()
