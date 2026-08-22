# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import hashlib
import json
import tempfile
import unittest
import subprocess
from pathlib import Path

from harness import board, contract, control
from harness import cto, execution_identity
from tests.requirements_support import agreed_requirements


def ledger_text(*rows, command="python3 -m unittest test_smoke"):
    header = "| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|---|\n"
    return header + "".join(
        f"| {scenario_id} | The release gate detects the {scenario} condition and preserves the safe expected outcome. | {scenario} | `{command}` | The harness detects and handles the simulated condition | PASS: targeted simulation ran and expected handling was observed | {result} |\n"
        for scenario_id, scenario, result in rows
    )


class CtoLedgerTests(unittest.TestCase):
    def test_release_health_reuses_only_exact_certified_delivery_full_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.md"
            ledger.write_text(ledger_text(("S-001", "release health", "PASS")))
            ledger_sha256 = hashlib.sha256(ledger.read_bytes()).hexdigest()
            request = {
                "id": "review-final-01", "task": "TASK-HEALTH", "cycle": 1,
                "phase": "final_acceptance", "subtask": "", "chunk": "",
                "structure_revision": 0, "test_scope": "full",
                "delivery_state": "passed", "status": "passed",
                "reviewed_commit": "a" * 40, "reviewed_tree_hash": "b" * 40,
                "ledger_sha256": ledger_sha256,
                "contract_revision": {"sha256": "c" * 64},
                "environment_identity": {"sha256": "d" * 64},
                "unit_test_command": "python3 -m unittest discover -s tests",
                "certified_artifacts": {
                    "delivery_ledger": {
                        "path": str(ledger), "sha256": ledger_sha256,
                    },
                },
                "command_executions": [],
            }
            candidate = execution_identity.candidate_evidence_identity(
                request["reviewed_commit"], request["reviewed_tree_hash"],
                request["contract_revision"]["sha256"],
                {
                    "delivery_ledger": ledger_sha256,
                    "review_scope": board._review_scope_identity(request)["sha256"],
                },
            )
            run = execution_identity.command_run_identity(
                candidate, request["unit_test_command"].split(), ".",
                request["environment_identity"]["sha256"], {},
                role="delivery", gate="final_acceptance::",
            )
            audit = {
                "problems": [], "timed_out": False,
                "forbidden_owner_browser_descendants": {},
                "new_keychain_or_permission_prompts": {},
                "default_handlers_unchanged": True,
            }
            audit_payload = (json.dumps(audit, sort_keys=True) + "\n").encode()
            audit_sha256 = hashlib.sha256(audit_payload).hexdigest()
            audit_path = board.board_dir(root) / "execution-audits" / f"{audit_sha256}.json"
            audit_path.parent.mkdir(parents=True)
            audit_path.write_bytes(audit_payload)
            output = "Ran 17 tests in 0.100s\n\nOK\n"
            certified = execution_identity.certify(
                root, run, exit_code=0,
                output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                duration_seconds=0.1, output=output,
                metadata={
                    "process_audit": {
                        "path": str(audit_path), "sha256": audit_sha256,
                    },
                },
            )["entry"]
            request["command_executions"] = [{
                "kind": "unit_test", "exit_code": 0,
                "command": request["unit_test_command"],
                "execution_identity": run["sha256"],
                "execution_record_id": certified["record_id"],
            }]

            verified = cto._certified_delivery_health(root, request)
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["source"], "certified_delivery_full_suite")
            self.assertEqual(
                verified["measurement"]["cache_decision"],
                "exact_success_reused_for_release_health",
            )
            self.assertTrue(cto._matching_certified_delivery_health(
                root, request, True, request["unit_test_command"],
            )["verified"])
            self.assertTrue(cto._matching_certified_delivery_health(
                root, request, True, "",
            )["verified"])
            self.assertFalse(cto._matching_certified_delivery_health(
                root, request, True,
                request["unit_test_command"] + " -k changed-scope",
            )["verified"])
            self.assertFalse(cto._matching_certified_delivery_health(
                root, request, False, request["unit_test_command"],
            )["verified"])

            request["reviewed_commit"] = "e" * 40
            self.assertFalse(cto._certified_delivery_health(root, request)["verified"])
            request["reviewed_commit"] = "a" * 40
            audit_path.write_bytes(audit_payload + b"tampered\n")
            self.assertFalse(cto._certified_delivery_health(root, request)["verified"])

    def test_release_gate_rejects_forged_or_changed_tree_repin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Harness"], cwd=root, check=True)
            (root / "app.txt").write_text("first\n")
            subprocess.run(["git", "add", "app.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "first"], cwd=root, check=True)
            first = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            (root / "app.txt").write_text("second\n")
            subprocess.run(["git", "add", "app.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "second"], cwd=root, check=True)
            second = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            forged = {
                "reviewed_commit": second,
                "reviewed_files": ["app.txt"],
                "review_repins": [{
                    "from_commit": first, "to_commit": second,
                    "tree_hash": subprocess.check_output(["git", "rev-parse", f"{first}^{{tree}}"], cwd=root, text=True).strip(),
                    "verified_by": "cto-forged", "board_verified": True,
                }],
            }
            artifact = cto._task_artifact_gate(root, "TASK", root, forged, False, "")
            self.assertFalse(artifact["review_repin_verified"])
            self.assertFalse(artifact["task_artifact_release_verified"])

    def test_scope_terms_normalize_common_inflections_without_fuzzy_matching(self):
        groups = (
            ("agent", "agents"),
            ("stop", "stops", "stopped", "stopping"),
            ("refresh", "refreshes", "refreshed", "refreshing"),
            ("show", "shows", "showed", "showing"),
            ("create", "created", "creating"),
        )
        for forms in groups:
            with self.subTest(forms=forms):
                stems = [cto._scope_terms(form) for form in forms]
                self.assertTrue(all(value == stems[0] for value in stems[1:]), stems)

    def test_claim_scope_audit_accepts_faithful_inflected_translation(self):
        owner = "Make the dashboard stop resetting my scroll position every time it refreshes, and show me why an agent is stalled."
        translated = "The dashboard stops resetting scroll position when it refreshes, and the viewer shows why agents are stalled."
        self.assertEqual(cto._scope_terms(owner) - cto._scope_terms(translated), set())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-INFLECTED-SCOPE"
            session = control.create(root, "codex_delivery")
            dev = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], owner)
            board.begin_task(root, dev["id"], task)
            agreed_requirements(root, dev["id"], "Final agreed requirements: " + translated)
            contract.create_contract(root, task, owner, [translated])
            ledger = root / "ledger.md"
            ledger.write_text(ledger_text(("S-001", "translated scope", "PASS")))
            with board.locked_state(root) as state:
                state["qa_requests"]["final-1"] = {
                    "id": "final-1", "task": task, "status": "passed",
                    "phase": "final_acceptance", "cycle": 1, "stage": "independent_review",
                    "subtask": "", "chunk": "", "developer_id": "dev", "claimed_by": "", "review_wait_started_at": "",
                    "structure_revision": 0, "requested_at": board.now(),
                }
            checks = cto.release_check(root, task, ledger, root)
            self.assertTrue(checks["claim_scope_audit_passed"], checks["claim_scope_missing_terms"])
            self.assertEqual(checks["claim_scope_missing_terms"], [])

    def test_claim_scope_audit_still_reports_omitted_capability(self):
        owner = "Make the dashboard stop resetting my scroll position every time it refreshes, and show me why an agent is stalled."
        incomplete = "The dashboard stops resetting scroll position when it refreshes."
        missing = cto._scope_terms(owner) - cto._scope_terms(incomplete)
        self.assertTrue({"agent", "stall"}.intersection(missing), missing)

    def test_claim_scope_audit_allows_terse_confirmation_to_remain_incomplete(self):
        owner = "Show why an agent stalled and preserve scroll position."
        self.assertTrue(cto._scope_terms(owner) - cto._scope_terms("approved as discussed"))

    def test_claim_scope_audit_reports_missing_capability_in_pm_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-SCOPE"
            session = control.create(root, "codex_delivery")
            dev = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            owner = "Build a resilient widget with restart recovery and an audit trail"
            board.record_owner_direction(root, session["id"], owner)
            board.begin_task(root, dev["id"], task)
            agreed_requirements(root, dev["id"], "Final requirements: resilient widget restart recovery")
            contract.create_contract(root, task, "Resilient widget restart recovery", ["restart recovery"])
            ledger = root / "ledger.md"; ledger.write_text(ledger_text(("S-001", "scope", "PASS")))
            translated = cto.release_check(root, task, ledger, root)
            self.assertFalse(translated["claim_scope_audit_passed"])
            self.assertIn("audit", translated["claim_scope_missing_terms"])
    def test_ledger_requires_all_concrete_scenarios_to_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.md"
            path.write_text(ledger_text(("S-001", "normal", "PASS"), ("S-002", "failure", "OPEN")))
            ok, problems = cto.ledger_complete(path)
            self.assertFalse(ok)
            self.assertEqual(len(problems), 1)
            path.write_text(ledger_text(("S-001", "normal", "PASS"), ("S-002", "failure", "PASS")))
            ok, problems = cto.ledger_complete(path)
            self.assertTrue(ok)
            self.assertEqual(problems, [])
            path.write_text(ledger_text(("S-001", "normal", "N/A:")))
            ok, problems = cto.ledger_complete(path)
            self.assertFalse(ok)

    def test_release_gate_requires_scenario_simulation_evidence_and_every_developer_to_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract.create_contract(root, "TASK-1", "Ship task", ["delivery"])
            contract_evidence = root / "contract-evidence.txt"
            contract_evidence.write_text("delivery proven\n")
            contract.add_evidence(root, "TASK-1", "delivery", [contract_evidence])
            ledger = root / "ledger.md"
            ledger.write_text(ledger_text(("S-001", "normal", "PASS")))
            challenge = root / "review-ledger.md"
            challenge.write_text(ledger_text(("S-101", "reviewer challenge", "PASS"), command="python3 -m unittest test_smoke -k passes"))
            (root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
            evidence = root / "evidence"
            evidence.mkdir()
            development_evidence = evidence / "dev-qa.txt"
            development_evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
            review_evidence = evidence / "review.txt"
            review_evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
            session = control.create(root, "codex_delivery")
            dev = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Ship task")
            board.begin_task(root, dev["id"], "TASK-1")
            agreed_requirements(root, dev["id"], "Final agreed requirements: ship the task with executable release evidence.")
            board.define_delivery_plan(root, dev["id"], "atomic", "One cohesive release-gate task")
            qa = board.register(root, "qa", "QA-QUEUE", vendor="Anthropic")
            review = board.request_review(
                root, dev["id"], str(ledger), "review independently",
                phase="final_acceptance", test_command="python3 -m unittest test_smoke",
            )
            board.claim_qa(root, qa["id"], review["id"], str(challenge))
            board.execute_challenge(root, qa["id"], review["id"])
            passed_review = board.qa_result(root, qa["id"], review["id"], "passed", "review pass", str(review_evidence))
            # Not a Git repo, so the complete gate cannot be green overall; the
            # development sub-gate should still prove the CTO does not accept
            # unfinished engineering work.
            before = cto.release_check(root, "TASK-1", ledger, root)
            self.assertFalse(before["development_agents_complete"])
            self.assertTrue(before["requirements_confirmation_recorded"])
            self.assertTrue(before["requirements_confirmation_scope_match"])
            self.assertTrue(before["delivery_scenario_simulations_executed"])
            self.assertTrue(before["reviewer_scenario_simulations_executed"])
            reviewer_evidence = Path(passed_review["reviewer_simulations"]["evidence"])
            original = reviewer_evidence.read_bytes()
            reviewer_evidence.write_bytes(original + b"tampered\n")
            tampered = cto.release_check(root, "TASK-1", ledger, root)
            self.assertTrue(tampered["reviewer_scenario_simulations_executed"])
            self.assertFalse(tampered["ready_for_owner_test"])
            reviewer_evidence.write_bytes(original)
            certified = Path(passed_review["certified_artifacts"]["reviewer_simulations_evidence"]["path"])
            certified.write_bytes(certified.read_bytes() + b"tampered certified\n")
            invalidated = cto.release_check(root, "TASK-1", ledger, root)
            self.assertFalse(invalidated["reviewer_scenario_simulations_executed"])
            board.complete(root, dev["id"], "merged candidate ready")
            after = cto.release_check(root, "TASK-1", ledger, root)
            self.assertTrue(after["development_agents_complete"])
            with board.locked_state(root) as state:
                state["requirement_confirmations"]["TASK-1"]["owner_direction"] = "A different owner objective"
            mismatched = cto.release_check(root, "TASK-1", ledger, root)
            self.assertFalse(mismatched["requirements_confirmation_scope_match"])
            self.assertFalse(mismatched["ready_for_owner_test"])


if __name__ == "__main__":
    unittest.main()
