# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Write specs: the independent reviewer always reviews the text (owner 2026-10-28).

A task the owner chose as "Write specs" cannot be finished by anyone without a passed final review of its text: not with no review
requested, not while the review is only claimed, not after a FAIL, and not when the text changed after the PASS.

Run:  PYTHONPATH=. python3 -m unittest tests.test_spec_always_reviewed -v
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract
from tests import test_administrative_close, test_board

TASK = "TASK-SPEC"
PASSED = "TASK DONE: YES - final passed"


class SpecAlwaysReviewedTests(unittest.TestCase):
    evidence = test_board.BoardTests.evidence
    ledger = test_board.BoardTests.ledger
    qa_command = test_board.BoardTests.qa_command
    atomic_plan = test_board.BoardTests.atomic_plan
    delivery = test_board.BoardTests.delivery

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
        (self.root / ".gitignore").write_text(".harness/\ndocs/\nevidence/\nproof/\n")
        for command in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "h@example.invalid"],
                        ["git", "config", "user.name", "Harness"], ["git", "add", ".gitignore", "test_smoke.py"],
                        ["git", "commit", "-qm", "baseline"]):
            subprocess.run(command, cwd=self.root, check=True)
        self.dev = self.delivery(TASK)
        self.atomic_plan(self.dev["id"])
        with board.locked_state(self.root) as state:        # the owner chose "Write specs" for this task
            state.setdefault("task_kinds", {})[TASK] = "spec"
            state.setdefault("task_document_only", {})[TASK] = True
        self.assertEqual(board.task_kind(board.snapshot(self.root), TASK), "spec")
        (self.root / "proof").mkdir()
        self.spec = self.root / "proof" / "spec.md"
        self.spec.write_text("# Spec\nThe page shows five days.\n")
        contract.add_evidence(self.root, TASK, "delivery", [self.spec])

    def request(self):
        return board.request_review(
            self.root, self.dev["id"], self.ledger("ledger.md"), "final", phase="final_acceptance",
            test_command=self.qa_command())

    def claimed(self):
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = self.request()
        board.claim_qa(self.root, reviewer["id"], request["id"], self.ledger("final-challenge.md"))
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        return reviewer, request

    def test_a_spec_cannot_finish_with_no_review_at_all(self):
        with self.assertRaisesRegex(ValueError, "passed final independent review"):
            board.complete(self.root, self.dev["id"], "done without a review")

    def test_a_spec_cannot_finish_while_the_review_is_only_requested_or_claimed(self):
        self.request()
        with self.assertRaisesRegex(ValueError, "passed final independent review"):
            board.complete(self.root, self.dev["id"], "done while the review is open")

    def test_a_spec_cannot_finish_after_a_failed_review(self):
        reviewer, request = self.claimed()
        board.qa_result(self.root, reviewer["id"], request["id"], "failed", "The spec says nothing about errors", self.evidence("failed.txt"))
        with self.assertRaisesRegex(ValueError, "passed final independent review"):
            board.complete(self.root, self.dev["id"], "done after a failed review")

    def test_the_text_changed_after_the_pass_blocks_completion(self):
        reviewer, request = self.claimed()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", PASSED, self.evidence("final.txt"))
        self.spec.write_text("# Spec\nThe page shows ten days, edited after the review.\n")
        with self.assertRaisesRegex(ValueError, r"delivery cannot complete: .*spec\.md - the file changed"):
            board.complete(self.root, self.dev["id"], "done on a stale review")

    def close_as_cto(self):
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        return board.close_released_administratively(self.root, cto["id"], TASK, "0" * 40, "shipped by hand and checked")

    def passed(self):
        reviewer, request = self.claimed()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", PASSED, self.evidence("final.txt"))

    def test_the_cto_cannot_close_a_spec_whose_attached_file_was_edited_after_the_pass(self):
        self.passed()
        self.spec.write_text("# Spec\nEdited after the review; Git HEAD did not change.\n")
        with self.assertRaisesRegex(ValueError, r"spec\.md - the file changed"):
            self.close_as_cto()
        self.assertFalse(board._task_finished(board.snapshot(self.root), TASK))

    def test_the_cto_cannot_close_a_spec_whose_attached_file_was_deleted_after_the_pass(self):
        self.passed()
        self.spec.unlink()
        with self.assertRaisesRegex(ValueError, r"spec\.md"):
            self.close_as_cto()

    def test_the_cto_cannot_close_a_spec_whose_attached_file_was_renamed_after_the_pass(self):
        self.passed()
        self.spec.rename(self.spec.with_name("spec-renamed.md"))
        with self.assertRaisesRegex(ValueError, r"spec\.md"):
            self.close_as_cto()

    def test_an_untouched_attached_file_is_not_what_stops_the_cto(self):
        self.passed()
        with self.assertRaises(ValueError) as caught:
            self.close_as_cto()      # refused later, for the missing Git release; never for the document
        self.assertNotIn("the file changed", str(caught.exception))
        self.assertNotIn("attached", str(caught.exception))

    def test_a_passed_review_of_the_current_text_lets_it_finish(self):
        reviewer, request = self.claimed()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", PASSED, self.evidence("final.txt"))
        board.complete(self.root, self.dev["id"], "reviewed and done")
        self.assertEqual(board.snapshot(self.root)["agents"][self.dev["id"]]["status"], "done")


class SpecAdministrativeCloseTests(unittest.TestCase):
    """The hand-shipped close is not a way round the review: the CTO cannot close a spec the reviewer has not passed."""
    setUp = test_administrative_close._fixture["setUp"]
    tearDown = test_administrative_close._fixture["tearDown"]
    git = test_administrative_close._fixture["git"]
    shipped_by_hand = test_administrative_close.AdministrativeCloseTests.shipped_by_hand
    cto = test_administrative_close.AdministrativeCloseTests.cto

    def choose_spec(self):
        with board.locked_state(self.root) as state:
            state.setdefault("task_kinds", {})["GIT-MODEL"] = "spec"
            state.setdefault("task_document_only", {})["GIT-MODEL"] = True

    def test_the_cto_cannot_close_a_spec_the_reviewer_has_not_passed(self):
        _, shipped = self.shipped_by_hand()
        self.choose_spec()
        with self.assertRaisesRegex(ValueError, "spec and the reviewer has not passed its current text"):
            board.close_released_administratively(self.root, self.cto()["id"], "GIT-MODEL", shipped, test_administrative_close.REASON)
        self.assertFalse(board._task_finished(board.snapshot(self.root), "GIT-MODEL"))

    def record_final(self, cycle, status, reviewed):
        with board.locked_state(self.root) as state:
            state.setdefault("qa_requests", {})[f"final-{cycle}"] = {
                "id": f"final-{cycle}", "task": "GIT-MODEL", "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "cycle": cycle, "status": status, "reviewed_commit": reviewed, "requested_at": f"2026-10-28T10:0{cycle}:00Z",
                "developer_id": "", "claimed_by": "", "review_wait_started_at": "",
                "structure_revision": int(((state.get("delivery_plans") or {}).get("GIT-MODEL") or {}).get("structure_revision", 0)),
            }

    def head(self):
        return self.git("rev-parse", board.snapshot(self.root)["task_branches"]["GIT-MODEL"]["branch"]).strip()

    def test_the_cto_can_close_a_spec_whose_current_text_the_newest_review_passed(self):
        task_head, shipped = self.shipped_by_hand()
        self.choose_spec()
        self.record_final(1, "passed", self.head())
        result = board.close_released_administratively(self.root, self.cto()["id"], "GIT-MODEL", shipped, test_administrative_close.REASON)
        self.assertEqual(result["by_role"], "cto")

    def test_the_cto_cannot_close_a_spec_whose_text_changed_after_the_pass(self):
        task_head, shipped = self.shipped_by_hand()
        self.choose_spec()
        (Path(self.begun["task_workspace"]) / "spec.md").write_text("changed text\n", encoding="utf-8")
        changed = board.broker_stage_commit(self.root, self.delivery["id"], ["spec.md"], "change the spec text")["commit"]
        self.record_final(1, "passed", task_head)    # the reviewer passed the EARLIER text only
        self.git("merge", "--no-edit", changed)
        merged = self.git("rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(ValueError, "has not passed its current text"):
            board.close_released_administratively(self.root, self.cto()["id"], "GIT-MODEL", merged, test_administrative_close.REASON)
        self.assertFalse(board._task_finished(board.snapshot(self.root), "GIT-MODEL"))

    def test_the_cto_cannot_close_a_spec_after_a_newer_final_review_failed(self):
        task_head, shipped = self.shipped_by_hand()
        self.choose_spec()
        self.record_final(1, "passed", self.head())
        self.record_final(2, "failed", self.head())
        with self.assertRaisesRegex(ValueError, "has not passed its current text"):
            board.close_released_administratively(self.root, self.cto()["id"], "GIT-MODEL", shipped, test_administrative_close.REASON)
        self.assertFalse(board._task_finished(board.snapshot(self.root), "GIT-MODEL"))

    def test_the_owner_can_still_close_a_spec_he_shipped_himself(self):
        _, shipped = self.shipped_by_hand()
        self.choose_spec()
        result = board.close_released_administratively(self.root, "", "GIT-MODEL", shipped, test_administrative_close.REASON)
        self.assertEqual(result["by_role"], "owner")


if __name__ == "__main__":
    unittest.main()
