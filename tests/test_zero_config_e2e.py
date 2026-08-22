# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Real zero-config Delivery → Reviewer → CTO release flow."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, cto, control


class ZeroConfigThreeCliE2E(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    def test_chunks_reviewer_poll_result_and_final_cto_gate_need_no_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"; remote = Path(tmp) / "origin.git"; root.mkdir()
            self.git(root, "init", "-q", "-b", "main")
            self.git(root, "config", "user.email", "harness@example.invalid"); self.git(root, "config", "user.name", "Harness")
            (root / ".gitignore").write_text(".harness/\n")
            docs = root / "docs"; docs.mkdir()
            ledger = docs / "scenario.md"; ledger.write_text("| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n| S-001 | A normal zero-configuration delivery completes without requiring an owner profile or technical setup. | normal | `python3 -m unittest test_smoke` | Normal flow succeeds | PASS: smoke simulation ran and normal behavior was observed | PASS |\n| S-002 | A failed zero-configuration delivery is detected and remains safely blocked for repair. | failure | `python3 -m unittest test_smoke` | Failure is detected and handled | PASS: smoke simulation ran and safe handling was observed | PASS |\n")
            challenge = docs / "final-challenge.md"; challenge.write_text("| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n| S-101 | An independent challenge detects a delivery fault and keeps the zero-configuration release safe. | reviewer challenge | `python3 -m unittest test_smoke -k passes` | Reviewer fault is detected and handled | PASS: reviewer smoke simulation ran and handling was observed | PASS |\n")
            (root / "app.txt").write_text("healthy\n")
            (root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
            self.git(root, "add", "."); self.git(root, "commit", "-qm", "candidate")
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            self.git(root, "remote", "add", "origin", str(remote)); self.git(root, "push", "-qu", "origin", "main")

            contract.create_contract(root, "TASK-ZERO", "Deliver without owner configuration", ["delivery"])
            evidence = root / ".harness" / "evidence" / "runtime.txt"; evidence.parent.mkdir(parents=True); evidence.write_text("command: test -f app.txt\nresult: PASS\n")
            contract.add_evidence(root, "TASK-ZERO", "delivery", [evidence])
            session = control.create(root, "codex_delivery")
            delivery = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Deliver without owner configuration")
            board.begin_task(root, delivery["id"], "TASK-ZERO")
            board.record_requirement_confirmation(root, delivery["id"], "Final agreed requirements: deliver the zero-configuration release flow and verify it end to end.")
            reviewer = board.register(root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
            board.define_delivery_plan(root, delivery["id"], "chunked", "Core and UX are bounded reviewable outcomes")
            board.declare_chunks(root, delivery["id"], [("core", "focused core behavior"), ("ux", "focused visible behavior")])
            for chunk in ("core", "ux"):
                request = board.request_review(root, delivery["id"], str(ledger.relative_to(root)), f"review {chunk}", chunk=chunk, test_command="python3 -m unittest test_smoke")
                board.claim_qa(root, reviewer["id"], request["id"], str(challenge.relative_to(root)))
                board.execute_challenge(root, reviewer["id"], request["id"])
                board.qa_result(root, reviewer["id"], request["id"], "passed", f"{chunk} independently passed", str(evidence))
                self.assertTrue(any(event["kind"] == "qa_result" for event in board.poll(root, delivery["id"])["events"]))
            final = board.request_review(root, delivery["id"], str(ledger.relative_to(root)), "full final acceptance", phase="final_acceptance", test_command="python3 -m unittest test_smoke")
            board.claim_qa(root, reviewer["id"], final["id"], str(challenge.relative_to(root)))
            board.execute_challenge(root, reviewer["id"], final["id"])
            board.qa_result(root, reviewer["id"], final["id"], "passed", "full objective independently passed", str(evidence))
            board.complete(root, delivery["id"], "all chunks and final acceptance complete")

            result = cto.release_check(root, "TASK-ZERO", ledger, root, execute_health=True, health_command="test -f app.txt")
            self.assertTrue(result["ready_for_owner_test"], result)
            self.assertTrue(result["delivery_chunks_complete"])
            self.assertTrue(result["final_acceptance_review_present"])
            self.assertIn("Live Harness Board", (root / ".harness" / "board" / "BOARD.md").read_text())
            self.assertTrue((root / ".harness" / "board" / "events.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
