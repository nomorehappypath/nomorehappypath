# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Failed review attempts retain immutable owner-readable checklist evidence."""

import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, contract, control


HEADER = (
    "| ID | What was tested | Simulation command | Expected system response | "
    "Observed system response | QA result |\n"
    "|---|---|---|---|---|---|\n"
)


class FailedReviewEvidenceDurabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\n"
            "class Smoke(unittest.TestCase):\n"
            "    def test_delivery(self): self.assertTrue(True)\n"
            "    def test_reviewer(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.delivery = self.root / "delivery.md"
        self.delivery.write_text(
            HEADER
            + "| S-DUR-01 | Delivery confirms saved work remains available after a repair. "
            "| `python3 -m unittest test_smoke.Smoke.test_delivery` | Saved work remains. "
            "| PASS: saved work remained. | PASS |\n",
            encoding="utf-8",
        )
        self.challenge = self.root / "challenge.md"
        self.challenge.write_text(
            HEADER
            + "| S-DUR-11 | The independent reviewer reopens the failed attempt and finds its original check. "
            "| `python3 -m unittest test_smoke.Smoke.test_reviewer` | The original check remains readable. "
            "| PASS: the original check remained readable. | PASS |\n",
            encoding="utf-8",
        )
        contract.create_contract(self.root, "TASK-DUR", "Keep failed evidence", ["history"])
        proof = self.root / "proof.txt"
        proof.write_text("history proof\n", encoding="utf-8")
        contract.add_evidence(self.root, "TASK-DUR", "history", [proof])
        session = control.create(self.root, "codex_delivery")
        self.dev = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Keep failed evidence")
        board.begin_task(self.root, self.dev["id"], "TASK-DUR")
        board.record_requirement_confirmation(
            self.root, self.dev["id"],
            "Keep every failed attempt's exact owner-readable evidence.",
        )
        board.define_delivery_plan(self.root, self.dev["id"], "atomic", "One evidence lifecycle")
        self.qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")

    def claimed_request(self):
        request = board.request_review(
            self.root,
            self.dev["id"],
            str(self.delivery),
            "Review failed evidence durability",
            phase="final_acceptance",
            test_command="python3 -m unittest test_smoke.Smoke.test_delivery",
        )
        board.claim_qa(self.root, self.qa["id"], request["id"], str(self.challenge))
        return request

    def fail(self, request):
        evidence = self.root / "review-result.txt"
        evidence.write_text(
            "command: python3 -m unittest tests.test_failed_review_evidence_durability\n"
            "result: FAILED - the independent semantic verdict needs repair\n",
            encoding="utf-8",
        )
        return board.qa_result(
            self.root, self.qa["id"], request["id"], "failed",
            "The independent review found a repair is required.", str(evidence),
        )

    def test_failed_executed_attempt_survives_reused_source_paths_in_history(self):
        request = self.claimed_request()
        board.execute_challenge(self.root, self.qa["id"], request["id"])
        failed = self.fail(request)

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            board.snapshot(self.root)["qa_requests"][request["id"]]["result"],
            "failed",
        )
        artifacts = failed["certified_artifacts"]
        self.assertEqual(
            set(artifacts),
            {
                "delivery_ledger", "challenge_ledger", "result_evidence",
                "delivery_evidence", "delivery_simulations_evidence",
                "reviewer_simulations_evidence",
            },
        )
        for artifact in artifacts.values():
            self.assertTrue(Path(artifact["path"]).is_file())

        self.delivery.write_text(HEADER + "| S-NEW-01 | Replacement wording hides the old attempt. | `python3 -m unittest test_smoke` | New. | PASS: new. | PASS |\n", encoding="utf-8")
        self.challenge.write_text(HEADER + "| S-NEW-11 | Replacement reviewer wording hides the old attempt. | `python3 -m unittest test_smoke -k reviewer` | New. | PASS: new. | PASS |\n", encoding="utf-8")
        Path(failed["evidence"]).unlink()
        Path(failed["delivery_evidence"]).unlink()

        with board.locked_state(self.root) as state:
            state["agents"][self.dev["id"]].update({"active": False, "status": "done"})
            state.setdefault("releases", {})["TASK-DUR"] = {
                "task": "TASK-DUR", "status": "VISUAL_TEST_REQUIRED",
                "cto_id": "cto", "recorded_at": board.now(),
            }
            state.setdefault("release_decisions", {})["TASK-DUR"] = {
                "task": "TASK-DUR", "decision": "accepted", "reason": "",
                "attachments": [], "recorded_at": board.now(),
            }

        history = board_viewer.history_payload(self.root)
        item = next(value for value in history["task_history"] if value["task"] == "TASK-DUR")
        attempt = item["test_ledgers"][0]
        self.assertIn("saved work remains available", attempt["delivery"]["scenarios"][0]["what_was_tested"])
        self.assertIn("reopens the failed attempt", attempt["reviewer"]["scenarios"][0]["what_was_tested"])
        self.assertEqual(attempt["delivery"]["scenarios"][0]["label"], "Passed")
        self.assertEqual(attempt["reviewer"]["scenarios"][0]["label"], "Passed")
        self.assertIn("found a problem", attempt["attempt_status"])

    def test_failed_unexecuted_attempt_is_preserved_as_not_tested(self):
        request = self.claimed_request()
        failed = self.fail(request)
        view = board_viewer._request_ledger_view(
            self.root, board.snapshot(self.root), "TASK-DUR", failed,
        )
        self.assertEqual(view["reviewer"]["state"], "available")
        self.assertEqual(view["reviewer"]["scenarios"][0]["label"], "Not tested yet")
        self.assertIn("challenge_ledger", failed["certified_artifacts"])

    def test_changed_challenge_bytes_cannot_be_attached_to_failed_verdict(self):
        request = self.claimed_request()
        self.challenge.write_text(
            self.challenge.read_text(encoding="utf-8").replace("S-DUR-11", "S-DUR-99"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "changed before the failed verdict"):
            self.fail(request)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["status"], "claimed")


if __name__ == "__main__":
    unittest.main()
