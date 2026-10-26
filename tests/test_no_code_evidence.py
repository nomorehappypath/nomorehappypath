# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A task that changes no product code still shows proof before its review can pass.

Owner 2026-10-15 (Studio, jls-studio-honesty-fix stage 1): a deliverable-only
task passed its final review while the evidence it relied on was not intact.
For such a task (the candidate touches no tracked file outside the harness's
own storage) every deliverable's current evidence file must exist, be
non-empty and still match the hash recorded when it was attached, at the
review request, at the PASS and at completion. An owner waiver still satisfies
a deliverable.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control
from tests import test_board

TASK = "TASK-NO-CODE"


class NoProductCodeEvidenceTests(unittest.TestCase):
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
        (self.root / "proof").mkdir()
        self.report = self.root / "proof" / "report.md"
        self.report.write_text("# Audit\nclaims checked: all supported\n")

    def attach(self, name="delivery", path=None, **kw):
        contract.add_evidence(self.root, TASK, name, [path or self.report], **kw)

    def request(self):
        return board.request_review(
            self.root, self.dev["id"], self.ledger("ledger.md"), "final", phase="final_acceptance",
            test_command=self.qa_command())

    # --- the gate at the review request ------------------------------------------------
    def test_missing_evidence_file_is_refused_with_a_plain_message(self):
        self.attach()
        self.report.unlink()
        with self.assertRaisesRegex(ValueError, r"changes no product code.*report\.md - the file is missing.*contract-evidence"):
            self.request()

    def test_emptied_evidence_file_is_refused(self):
        self.attach()
        self.report.write_text("")
        with self.assertRaisesRegex(ValueError, r"report\.md - the file is empty"):
            self.request()

    def test_changed_evidence_file_is_refused(self):
        self.attach()
        self.report.write_text("# Audit\nclaims checked: FAILED\n")
        with self.assertRaisesRegex(ValueError, r"report\.md - the file changed after it was attached"):
            self.request()

    def test_intact_evidence_is_accepted_and_pinned_on_the_request(self):
        self.attach()
        request = self.request()
        stored = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertTrue(stored["no_product_code_task"])
        self.assertEqual(stored["no_product_code_evidence_pins"], [{
            "deliverable": "delivery", "path": str(self.report),
            "sha256": hashlib.sha256(self.report.read_bytes()).hexdigest()}])

    def test_a_deliverable_with_no_proof_at_all_is_refused(self):
        # contract_complete would already refuse this; the new gate names what to attach
        with self.assertRaises(ValueError):
            self.request()

    def test_owner_waiver_satisfies_the_deliverable(self):
        contract.waive_deliverable(self.root, TASK, "delivery", {"id": "w1", "owner_words": "not required", "part": ""})
        request = self.request()
        self.assertTrue(board.snapshot(self.root)["qa_requests"][request["id"]]["no_product_code_task"])

    def test_newer_file_for_another_document_does_not_hide_a_missing_earlier_one(self):
        plan = self.root / "proof" / "plan.md"
        plan.write_text("# Plan\nnext steps\n")
        self.attach(for_owner=True, document="Report")
        self.attach(path=plan, for_owner=True, document="Plan")
        self.report.unlink()
        with self.assertRaisesRegex(ValueError, r"report\.md - the file is missing"):
            self.request()

    def test_a_newer_version_of_the_same_document_replaces_the_older_one(self):
        self.attach(for_owner=True, document="Report")
        newer = self.root / "proof" / "report-v2.md"
        newer.write_text("# Audit v2\nfixed\n")
        self.attach(path=newer, for_owner=True, document="Report")
        self.report.unlink()                      # the superseded version is no longer an obligation
        request = self.request()
        self.assertTrue(board.snapshot(self.root)["qa_requests"][request["id"]]["no_product_code_task"])

    def test_legacy_entry_without_a_hash_is_ignored_and_a_reattachment_recovers(self):
        path = contract._task_path(self.root, TASK)
        value = contract._load(path)
        value["deliverables"][0]["evidence"] = ["legacy/old-proof.txt"]
        value["deliverables"][0]["verified"] = True
        contract._save(path, value)
        with self.assertRaisesRegex(ValueError, r'deliverable "delivery" has no attached proof file'):
            self.request()
        self.attach()
        request = self.request()
        self.assertTrue(board.snapshot(self.root)["qa_requests"][request["id"]]["no_product_code_task"])

    # --- the proof must stay intact until the PASS and until completion ---------------------
    def claimed_final(self):
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = self.request()
        board.claim_qa(self.root, reviewer["id"], request["id"], self.ledger("final-challenge.md"))
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        return reviewer, request

    def test_evidence_removed_after_the_request_blocks_the_pass(self):
        self.attach()
        reviewer, request = self.claimed_final()
        self.report.write_text("# Audit\nclaims checked: FAILED\n")
        with self.assertRaisesRegex(ValueError, r"cannot record PASS: This task changes no product code.*report\.md - the file changed"):
            board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "claimed")

    def test_evidence_changed_after_the_pass_blocks_completion(self):
        self.attach()
        reviewer, request = self.claimed_final()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        self.report.unlink()
        with self.assertRaisesRegex(ValueError, r"delivery cannot complete: .*changes no product code.*report\.md - the file is missing"):
            board.complete(self.root, self.dev["id"], "done")

    def test_intact_evidence_passes_and_completes(self):
        self.attach()
        reviewer, request = self.claimed_final()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        board.complete(self.root, self.dev["id"], "done")
        self.assertEqual(board.snapshot(self.root)["agents"][self.dev["id"]]["status"], "done")

    def test_attaching_a_corrected_file_through_the_board_replaces_the_stale_one(self):
        self.attach()
        self.report.unlink()
        corrected = self.root / "proof" / "report-corrected.md"
        corrected.write_text("# Audit\nre-run: supported\n")
        board.attach_contract_evidence(self.root, self.dev["id"], "delivery", str(corrected))
        request = self.request()
        self.assertEqual([pin["path"] for pin in board.snapshot(self.root)["qa_requests"][request["id"]]["no_product_code_evidence_pins"]],
                         [str(corrected)])

    def test_evidence_attached_after_the_request_is_not_what_the_reviewer_judged(self):
        self.attach()
        reviewer, request = self.claimed_final()
        other = self.root / "proof" / "late.md"
        other.write_text("# Late\nattached after the request\n")
        self.attach(path=other)
        with self.assertRaisesRegex(ValueError, r"late\.md was attached or selected after the review was requested"):
            board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))

    def test_restoring_the_exact_reviewed_bytes_keeps_the_pass(self):
        self.attach()
        reviewer, request = self.claimed_final()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        original = self.report.read_bytes()
        self.report.unlink()
        self.report.write_bytes(original)
        board.complete(self.root, self.dev["id"], "done")

    def test_a_pass_whose_proof_changed_does_not_block_a_new_request_for_the_same_commit(self):
        self.attach()
        reviewer, request = self.claimed_final()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        self.report.write_text("# Audit\nre-run after the fix: supported\n")
        with self.assertRaises(ValueError):
            board.complete(self.root, self.dev["id"], "done")
        self.attach()                                  # the corrected proof, attached again
        again = board.request_review(
            self.root, self.dev["id"], self.ledger("ledger2.md"), "final again", phase="final_acceptance",
            test_command=self.qa_command(), changes="proof re-attached after correction")
        self.assertEqual(again["cycle"], 2)

    def test_the_reviewer_is_told_to_read_the_proof_and_a_skipped_check_is_not_presented_as_verified(self):
        self.attach()
        request = self.request()
        brief = board.snapshot(self.root)["qa_requests"][request["id"]]["review_brief"]["risk_and_scope"]["no_product_code_evidence"]
        self.assertTrue(brief["applies"])
        self.assertIn("FAIL the review", brief["instruction"])
        self.assertEqual(len(brief["pinned_files"]), 1)

    def test_a_saved_pass_reuse_check_names_missing_proof(self):
        self.attach()
        reviewer, request = self.claimed_final()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        self.report.unlink()
        state = board.snapshot(self.root)
        checks = board._evidence_reuse_checks(self.root, state, state["qa_requests"][request["id"]])
        proof = [check for check in checks if "proof files" in str(check)]
        self.assertTrue(proof)
        self.assertIn("report.md - the file is missing", str(proof))

    def test_a_stalled_read_releases_the_caller_with_a_plain_message(self):
        import time
        from unittest import mock
        with mock.patch.object(board, "EVIDENCE_READ_SECONDS", 0.2), \
                mock.patch.object(board, "_evidence_file_problem_unbounded", side_effect=lambda *a: time.sleep(2) or ""):
            started = time.monotonic()
            reason = board._evidence_file_problem(str(self.report), "x")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("could not be read within", reason)

    def test_named_independent_proofs_are_each_required(self):
        plan = self.root / "proof" / "plan.md"
        plan.write_text("# Plan\nnext steps\n")
        self.attach(document="Audit report")                          # --document without --for-owner
        self.attach(path=plan, document="Plan")
        self.report.unlink()
        with self.assertRaisesRegex(ValueError, r"report\.md - the file is missing"):
            self.request()

    def test_the_resume_path_rechecks_the_proof_even_with_a_cached_resume_id(self):
        self.attach()
        reviewer, request = self.claimed_final()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        first = board.reconcile_evidence_reuse(self.root, "resume-1")
        self.assertTrue(any(item["status"] == "reused" and item["request_id"] == request["id"] for item in first["reused"]), first)
        self.report.unlink()
        second = board.reconcile_evidence_reuse(self.root, "resume-1")      # the same resume id must not hide it
        self.assertTrue(any(item["request_id"] == request["id"] for item in second["invalidated"]), second)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "failed")

    def test_a_proof_selection_that_changes_while_completion_is_checked_is_not_accepted(self):
        from unittest import mock
        self.attach()
        reviewer, request = self.claimed_final()
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "TASK DONE: YES - final passed", self.evidence("final.txt"))
        real = board._obligation_identity
        calls = []
        def shifting(root, task):
            calls.append(1)
            return real(root, task) if len(calls) == 1 else ("a different selection",)
        with mock.patch.object(board, "_obligation_identity", side_effect=shifting):
            with self.assertRaisesRegex(ValueError, "changed while completion was being checked; run complete again"):
                board.complete(self.root, self.dev["id"], "done")

    def test_a_reader_over_the_total_limit_names_the_limit_and_a_busy_reader_pool_says_so(self):
        from unittest import mock
        with mock.patch.object(board, "EVIDENCE_TOTAL_SECONDS", -1.0):
            self.attach()
            problems, _ = board._no_product_code_evidence(self.root, TASK)
        self.assertTrue(any("limit for verifying the proof files was reached" in item for item in problems), problems)
        slots = board._evidence_reader_slots
        held = [slots.acquire(blocking=False) for _ in range(board.EVIDENCE_MAX_STALLED_READERS)]
        try:
            self.assertIn("still waiting", board._evidence_file_problem(str(self.report), "x"))
        finally:
            for ok in held:
                if ok:
                    slots.release()

    # --- a task that changes code is unaffected -----------------------------------------
    def test_a_code_changing_task_is_not_gated(self):
        self.attach()
        self.report.unlink()                       # broken proof would stop a no-code task
        workspace = Path(board.snapshot(self.root)["task_workspaces"][TASK])
        (workspace / "product.py").write_text("VALUE = 1\n")
        for command in (["git", "add", "product.py"], ["git", "commit", "-qm", "product code"]):
            subprocess.run(command, cwd=workspace, check=True)
        request = self.request()
        self.assertFalse(board.snapshot(self.root)["qa_requests"][request["id"]]["no_product_code_task"])

    def test_unknown_baseline_is_recorded_as_skipped_not_guessed(self):
        verdict, reason = board._task_changes_no_product_code(self.root, {"available": True, "base_commit": "abc", "files": []}, "")
        self.assertIsNone(verdict)
        self.assertIn("baseline", reason)
        verdict, _ = board._task_changes_no_product_code(self.root, {"available": False}, "abc")
        self.assertIsNone(verdict)

    def test_only_harness_storage_changes_still_count_as_no_product_code(self):
        artifact = {"available": True, "base_commit": "new", "files": [".harness/board/state.json"]}
        self.assertTrue(board._task_changes_no_product_code(self.root, artifact, "old")[0])
        artifact["files"].append("docs/report.md")
        self.assertFalse(board._task_changes_no_product_code(self.root, artifact, "old")[0])

    def test_unreadable_and_non_regular_files_are_named(self):
        directory = self.root / "proof" / "dir"
        directory.mkdir()
        self.assertIn("not a regular file", board._evidence_file_problem(str(directory), "x"))
        self.assertEqual(board._evidence_file_problem(str(self.report), hashlib.sha256(self.report.read_bytes()).hexdigest()), "")


if __name__ == "__main__":
    unittest.main()
