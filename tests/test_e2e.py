# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Executable whole-harness release-flow verification in a disposable Git repo."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, cto, control


class EndToEndHarnessTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    def test_clean_main_requires_and_accepts_the_full_release_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            remote = Path(tmp) / "remote.git"
            root.mkdir()
            self.git(root, "init", "-q", "-b", "main")
            self.git(root, "config", "user.email", "harness@example.invalid")
            self.git(root, "config", "user.name", "Harness E2E")
            (root / ".gitignore").write_text(".harness/\n")
            docs = root / "docs"
            docs.mkdir()
            ledger = docs / "TASK-E2E-scenarios.md"
            ledger.write_text("| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n| S-001 | A valid release completes every governed step and leaves the accepted application available. | successful flow | `python3 -m unittest test_smoke` | Release flow succeeds | PASS: smoke simulation ran and release behavior was observed | PASS |\n| S-002 | Invalid release input is refused without changing the accepted application or its evidence. | invalid input | `python3 -m unittest test_smoke` | Invalid input is rejected without side effects | PASS: smoke simulation ran and rejection behavior was observed | PASS |\n")
            challenge = docs / "TASK-E2E-challenge.md"
            challenge.write_text("| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n| S-101 | An independent failure check keeps an unsafe release blocked without losing the prior accepted state. | independent failure challenge | `python3 -m unittest test_smoke -k passes` | Failure is detected and release remains safe | PASS: reviewer smoke simulation ran and safe handling was observed | PASS |\n")
            (root / "app.txt").write_text("release candidate\n")
            (root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
            profile_path = root / "profile.json"
            profile = {
                "project_name": "e2e", "default_branch": "main",
                "test_command": "python3 -m unittest", "build_command": "true",
                "health_command": "test -f app.txt", "deployment_channels": ["local"],
            }
            profile_path.write_text(json.dumps(profile))
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "release candidate")
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            self.git(root, "remote", "add", "origin", str(remote))
            self.git(root, "push", "-qu", "origin", "main")

            contract.create_contract(root, "TASK-E2E", "Prove a clean end-to-end harness release", ["release flow"])
            execution_evidence = root / ".harness" / "evidence" / "execution.txt"
            execution_evidence.parent.mkdir(parents=True)
            execution_evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
            contract.add_evidence(root, "TASK-E2E", "release flow", [execution_evidence])

            session = control.create(root, "codex_delivery")
            developer = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Prove a clean end-to-end harness release")
            board.begin_task(root, developer["id"], "TASK-E2E")
            board.record_requirement_confirmation(root, developer["id"], "Final agreed requirements: prove the clean release flow end to end.")
            board.define_delivery_plan(root, developer["id"], "atomic", "One cohesive end-to-end release proof")
            review = board.request_review(
                root, developer["id"], str(ledger.relative_to(root)),
                "challenge assumptions independently", phase="final_acceptance",
                test_command="python3 -m unittest test_smoke",
            )
            reviewer = board.register(root, "qa", "QA-QUEUE", vendor="Anthropic")
            board.claim_qa(root, reviewer["id"], review["id"], str(challenge.relative_to(root)))
            board.execute_challenge(root, reviewer["id"], review["id"])
            board.qa_result(root, reviewer["id"], review["id"], "passed", "challenge scenarios passed", str(execution_evidence))
            board.complete(root, developer["id"], "all implementation deliverables complete")

            result = cto.release_check(root, "TASK-E2E", ledger, root, profile, execute_health=True)
            self.assertTrue(result["ready_for_owner_test"], result)
            self.assertTrue(all(result[key] for key in (
                "development_qa_passed", "independent_review_passed", "scenario_ledger_complete",
                "reviewer_challenge_ledger_complete", "completion_contract_complete",
                "development_agents_complete", "candidate_branch", "git_clean",
                "main_unchanged_before_accept", "mirror_candidate_verified",
                "candidate_health_verified",
            )))
            self.assertFalse(result["main_pushed"])


if __name__ == "__main__":
    unittest.main()
