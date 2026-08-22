# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P1 execute-once sims (item 5; owner bar: no verdict without certified execution).

Run:  PYTHONPATH=. python3 -m unittest tests.test_execute_once -v
"""
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, control


class ExecuteOnceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_passes(self): self.assertTrue(True)\n")

    def _claimed_review(self):
        header = ("| ID | What was tested | Simulation command | Expected system response | "
                  "Observed system response | QA result |\n|---|---|---|---|---|---|\n")
        contract.create_contract(self.root, "TASK-1", "Ship task", ["delivery"])
        proof = self.root / "proof.txt"
        proof.write_text("delivery proven\n")
        contract.add_evidence(self.root, "TASK-1", "delivery", [proof])
        ledger = self.root / "ledger.md"
        ledger.write_text(header + "| S-001 | Delivery behavior remains correct under the focused smoke check. | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        challenge = self.root / "challenge.md"
        challenge.write_text(header + "| S-101 | The independent check confirms the behavior through a distinct test scope. | `python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS |\n")
        evidence = self.root / "evidence"
        evidence.mkdir()
        review_evidence = evidence / "review.txt"
        review_evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                             vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Ship task")
        board.begin_task(self.root, dev["id"], "TASK-1")
        board.record_requirement_confirmation(
            self.root, dev["id"], "Final agreed requirements: ship the task with executable evidence.")
        board.define_delivery_plan(self.root, dev["id"], "atomic", "One cohesive task")
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        request = board.request_review(
            self.root, dev["id"], str(ledger), "review independently",
            phase="final_acceptance", test_command="python3 -m unittest test_smoke")
        board.claim_qa(self.root, qa["id"], request["id"], str(challenge))
        return qa, request, challenge, review_evidence

    # ---- S-P1-001: one board execution, certified, stamped, evented ----
    def test_execute_challenge_certifies_once(self):
        qa, request, challenge, _ = self._claimed_review()
        outcome = board.execute_challenge(self.root, qa["id"], request["id"])
        self.assertEqual(outcome["scenarios"], 1)
        self.assertTrue(Path(outcome["evidence"]).is_file())
        state = board.snapshot(self.root)
        stamped = state["qa_requests"][request["id"]].get("challenge_execution")
        self.assertTrue(stamped and stamped["id"] == outcome["execution_id"])
        self.assertEqual(
            stamped["candidate_identity"]["fields"]["tree"],
            request["reviewed_tree_hash"] or request["reviewed_worktree_digest"],
        )
        self.assertEqual(
            stamped["candidate_identity"]["fields"]["contract_revision"],
            request["contract_revision"]["sha256"],
        )
        self.assertTrue(stamped["environment_sha256"])
        self.assertEqual(len(stamped["scenario_identities"]), 1)
        self.assertTrue((board.board_dir(self.root) / "execution-store.jsonl").is_file())
        self.assertTrue(any(e.get("kind") == "challenge_executed"
                            for e in state.get("events", [])))

    # ---- S-P1-002: the verdict step does NOT re-execute a certified ledger ----
    def test_verdict_consumes_certified_execution_without_rerun(self):
        qa, request, challenge, review_evidence = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        with patch.object(board, "_execute_scenario_simulations",
                          side_effect=AssertionError("re-execution is forbidden")) as run:
            recorded = board.qa_result(self.root, qa["id"], request["id"],
                                       "passed", "verdict on certified execution",
                                       str(review_evidence))
        run.assert_not_called()
        self.assertEqual(recorded["status"], "passed")

    # ---- S-P1-003: a ledger changed after certification REFUSES the verdict ----
    def test_changed_ledger_after_certification_refuses_verdict(self):
        qa, request, challenge, review_evidence = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        challenge.write_text(challenge.read_text().replace("S-101", "S-999"))
        with self.assertRaisesRegex(ValueError, "execute-challenge again"):
            board.qa_result(self.root, qa["id"], request["id"],
                            "passed", "verdict on stale execution", str(review_evidence))

    # ---- S-P1-004: PASS without pre-execution is refused, never run at verdict ----
    def test_pass_without_execute_challenge_is_refused_without_execution(self):
        qa, request, challenge, review_evidence = self._claimed_review()
        with patch.object(board, "_execute_scenario_simulations",
                          side_effect=AssertionError("verdict execution is forbidden")) as run:
            with self.assertRaisesRegex(ValueError, "requires execute-challenge"):
                board.qa_result(self.root, qa["id"], request["id"],
                                "passed", "uncertified verdict",
                                str(review_evidence))
        run.assert_not_called()

    # ---- S-P1-005: only the claiming reviewer may execute the challenge ----
    def test_only_the_claiming_reviewer_executes(self):
        qa, request, challenge, _ = self._claimed_review()
        other = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "claimed this request"):
            board.execute_challenge(self.root, other["id"], request["id"])

    # ---- S-P1-006: concurrent callers serialize before command execution ----
    def test_concurrent_execute_challenge_runs_commands_once(self):
        qa, request, _, _ = self._claimed_review()
        original = board._execute_scenario_simulations
        calls = 0
        calls_lock = threading.Lock()

        def counted(*args, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return original(*args, **kwargs)

        with patch.object(board, "_execute_scenario_simulations", side_effect=counted):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(
                    lambda _: board.execute_challenge(self.root, qa["id"], request["id"]),
                    range(2),
                ))
        self.assertEqual(calls, 1)
        self.assertEqual(
            sorted(item.get("status", "certified") for item in outcomes),
            ["already_certified", "certified"],
        )

    def test_failed_challenge_can_be_explicitly_corrected_without_hiding_attempt(self):
        qa, request, challenge, _ = self._claimed_review()
        with patch.object(
            board, "_execute_scenario_simulations",
            side_effect=RuntimeError("sanitized execution environment rejected the command"),
        ):
            with self.assertRaisesRegex(RuntimeError, "sanitized execution environment"):
                board.execute_challenge(self.root, qa["id"], request["id"])

        corrected = self.root / "challenge-corrected.md"
        corrected.write_text(challenge.read_text().replace("S-101", "S-102"))
        with self.assertRaisesRegex(ValueError, "explicit reason"):
            board.attach_challenge_ledger(
                self.root, qa["id"], request["id"], str(corrected),
            )
        attached = board.attach_challenge_ledger(
            self.root, qa["id"], request["id"], str(corrected),
            "Correct command for the sanitized board execution environment.",
        )
        self.assertEqual(attached["status"], "claimed")
        self.assertEqual(len(attached["challenge_ledger_revisions"]), 1)
        self.assertEqual(
            attached["challenge_ledger_revisions"][0]["failed_attempt"]["status"],
            "failed",
        )
        with self.assertRaisesRegex(ValueError, "retrying.*requires"):
            board.execute_challenge(self.root, qa["id"], request["id"])
        outcome = board.execute_challenge(
            self.root, qa["id"], request["id"],
            "Ledger corrected after the recorded environment failure.",
        )
        self.assertEqual(outcome["scenarios"], 1)
        state = board.snapshot(self.root)
        self.assertTrue(any(
            event.get("kind") == "qa_challenge_ledger_corrected"
            for event in state.get("events", [])
        ))

    def test_successfully_certified_challenge_cannot_be_replaced(self):
        qa, request, challenge, _ = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        corrected = self.root / "challenge-after-pass.md"
        corrected.write_text(challenge.read_text().replace("S-101", "S-103"))
        with self.assertRaisesRegex(ValueError, "reserved this request"):
            board.attach_challenge_ledger(
                self.root, qa["id"], request["id"], str(corrected),
                "Attempt to replace already certified success.",
            )


if __name__ == "__main__":
    unittest.main()
