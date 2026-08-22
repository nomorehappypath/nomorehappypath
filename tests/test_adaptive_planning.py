# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, control, cto
from tests.requirements_support import agreed_requirements


class AdaptivePlanningSimulationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_behavior(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")

    def tearDown(self):
        self.tmp.cleanup()

    def delivery(self, task: str):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        objective = f"OWNER DIRECTION — {task}"
        board.record_owner_direction(self.root, session["id"], objective)
        board.begin_task(self.root, agent["id"], task)
        contract.create_contract(self.root, task, objective, ["delivery"])
        agreed_requirements(self.root, agent["id"], f"Final agreed requirements for {task}: deliver the requested objective and verify all relevant scenarios.")
        return agent

    def ledger(self, name: str, reviewer: bool = False) -> str:
        path = self.root / "docs" / f"{name}.md"
        path.parent.mkdir(exist_ok=True)
        prefix = "reviewer adversarial challenge" if reviewer else "delivery acceptance behavior"
        scenario_id = "S-R101" if reviewer else "S-D001"
        command = "python3 -m unittest test_smoke -k behavior" if reviewer else "python3 -m unittest test_smoke"
        path.write_text(
            "| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|---|\n"
            f"| {scenario_id} | The selected {name} scope completes its required behavior without bypassing the planned acceptance gate. | {prefix} for {name} | `{command}` | The selected scope behaves correctly | PASS: executable simulation completed and the selected scope behaved correctly | PASS |\n",
            encoding="utf-8",
        )
        return str(path.relative_to(self.root))

    def evidence(self, name: str) -> str:
        path = self.root / "evidence" / f"{name}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("command: python3 -m unittest test_smoke\nresult: PASS\n", encoding="utf-8")
        return str(path.relative_to(self.root))

    def request(self, agent, scope: str, phase: str, *, subtask: str = "", chunk: str = "", changes: str = ""):
        if subtask:
            state = board.snapshot(self.root)
            task = state["agents"][agent["id"]]["task"]
            item = state["delivery_plans"][task]["subtasks"][subtask]
            if item.get("pipeline_status", "pending") == "pending":
                board.start_subtask(self.root, agent["id"], subtask)
        return board.request_review(
            self.root, agent["id"], self.ledger(f"delivery-{scope}"),
            f"review {scope}", phase=phase, subtask=subtask, chunk=chunk,
            changes=changes, test_command="python3 -m unittest test_smoke",
        )

    def pass_request(self, request, scope: str):
        board.claim_qa(self.root, self.reviewer["id"], request["id"], self.ledger(f"challenge-{scope}", reviewer=True))
        board.execute_challenge(self.root, self.reviewer["id"], request["id"])
        return board.qa_result(
            self.root, self.reviewer["id"], request["id"], "passed",
            f"{scope} independently accepted", self.evidence(scope),
        )

    def test_atomic_task_uses_unit_tests_then_one_final_review_without_chunks(self):
        agent = self.delivery("ATOMIC")
        board.define_delivery_plan(self.root, agent["id"], "atomic", "One cohesive user-visible correction")
        with self.assertRaisesRegex(ValueError, "root delivery chunks are valid only"):
            board.declare_chunks(self.root, agent["id"], [("fake", "unnecessary process chunk")])
        request = self.request(agent, "atomic-final", "final_acceptance")
        self.assertEqual(request["delivery_mode"], "atomic")
        self.assertEqual(request["unit_test_command"], "python3 -m unittest test_smoke")
        self.pass_request(request, "atomic-final")
        state = board.snapshot(self.root)
        self.assertEqual(state["delivery_plans"]["ATOMIC"]["subtasks"], {})
        self.assertNotIn("ATOMIC", state["task_chunks"])
        self.assertEqual(len([value for value in state["qa_requests"].values() if value["task"] == "ATOMIC"]), 1)

    def test_review_is_rejected_until_product_management_defines_task_type(self):
        agent = self.delivery("UNCLASSIFIED")
        with self.assertRaisesRegex(ValueError, "must define a chunked delivery plan"):
            board.declare_chunks(self.root, agent["id"], [("guessed", "must not infer a structure")])
        with self.assertRaisesRegex(ValueError, "must define a delivery plan"):
            board.request_qa(self.root, agent["id"], self.ledger("legacy-shortcut"), "must not bypass planning")
        with self.assertRaisesRegex(ValueError, "must define an atomic, chunked, or application"):
            self.request(agent, "unclassified", "final_acceptance")
        self.assertNotIn("UNCLASSIFIED", board.snapshot(self.root)["delivery_plans"])
        self.assertFalse(board.snapshot(self.root)["qa_requests"])

    def test_final_requirements_confirmation_is_required_immutable_and_archived(self):
        agent = self.delivery("REQUIREMENTS-CONFIRMATION")
        state = board.snapshot(self.root)
        confirmation = state["requirement_confirmations"]["REQUIREMENTS-CONFIRMATION"]
        self.assertIn("Final agreed requirements", confirmation["text"])
        self.assertEqual(confirmation["version"], 1)
        self.assertEqual(confirmation["owner_direction"], "OWNER DIRECTION — REQUIREMENTS-CONFIRMATION")
        with self.assertRaisesRegex(ValueError, "already confirmed"):
            agreed_requirements(self.root, agent["id"], "A silent scope rewrite")
        self.assertTrue(any(event["kind"] == "requirements_confirmed" for event in state["events"]))

    def test_delivery_plan_is_blocked_until_owner_go_ahead_confirmation(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        direction = "OWNER DIRECTION — WAIT-FOR-GO-AHEAD"
        board.record_owner_direction(self.root, session["id"], direction)
        board.begin_task(self.root, agent["id"], "WAIT-FOR-GO-AHEAD")
        contract.create_contract(self.root, "WAIT-FOR-GO-AHEAD", direction, ["delivery"])
        with self.assertRaisesRegex(ValueError, "final requirements confirmation"):
            board.define_delivery_plan(self.root, agent["id"], "atomic", "Must not start before agreement")
        self.assertNotIn("WAIT-FOR-GO-AHEAD", board.snapshot(self.root)["delivery_plans"])
        agreed_requirements(self.root, agent["id"], "Final agreed requirements: proceed with the exact owner direction and verify it end to end.")
        plan = board.define_delivery_plan(self.root, agent["id"], "atomic", "One cohesive agreed objective")
        self.assertEqual(plan["mode"], "atomic")

    def test_confirmation_cannot_fabricate_a_missing_owner_direction(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"task": "NO-OWNER-DIRECTION", "status": "task_defined"})
        with self.assertRaisesRegex(ValueError, "preserved original owner direction"):
            agreed_requirements(self.root, agent["id"], "Final agreed requirements without an owner request")

    def test_real_board_cli_records_application_plan_and_subtasks(self):
        agent = self.delivery("CLI-APP")
        base = [sys.executable, str(Path(board.__file__)), "--root", str(self.root)]
        planned = subprocess.run(
            base + ["define-plan", "--agent", agent["id"], "--mode", "application", "--rationale", "Full product request"],
            capture_output=True, text=True,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        declared = subprocess.run(
            base + [
                "declare-subtasks", "--agent", agent["id"],
                "--subtask", "auth|Authentication|Users authenticate|",
                "--subtask", "workspace|Workspace|Users manage work|auth",
            ], capture_output=True, text=True,
        )
        self.assertEqual(declared.returncode, 0, declared.stderr)
        started = subprocess.run(
            base + ["start-subtask", "--agent", agent["id"], "--subtask", "auth"],
            capture_output=True, text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        plan = board.snapshot(self.root)["delivery_plans"]["CLI-APP"]
        self.assertEqual(plan["mode"], "application")
        self.assertEqual(plan["subtasks"]["workspace"]["dependencies"], ["auth"])
        self.assertEqual(plan["subtasks"]["auth"]["pipeline_status"], "in_progress")
        self.assertEqual(plan["subtasks"]["auth"]["owned_paths"], ["*"])

    def test_real_board_cli_runs_named_unit_test_gate_before_atomic_acceptance(self):
        agent = self.delivery("CLI-ATOMIC")
        board.define_delivery_plan(self.root, agent["id"], "atomic", "One cohesive CLI task")
        command = [
            sys.executable, str(Path(board.__file__)), "--root", str(self.root),
            "request-review", "--agent", agent["id"], "--ledger", self.ledger("cli-atomic"),
            "--summary", "CLI atomic acceptance", "--phase", "final_acceptance",
            "--unit-test-command", "python3 -m unittest test_smoke",
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        request = next(iter(board.snapshot(self.root)["qa_requests"].values()))
        self.assertEqual(request["unit_test_command"], "python3 -m unittest test_smoke")
        self.assertTrue(Path(request["unit_test_evidence"]).is_file())

    def test_unit_test_failure_stops_acceptance_request_before_queueing(self):
        agent = self.delivery("UNIT-FAIL")
        board.define_delivery_plan(self.root, agent["id"], "atomic", "Small cohesive task")
        (self.root / "test_failure.py").write_text(
            "import unittest\n\nclass Failure(unittest.TestCase):\n    def test_failure(self): self.fail('simulated unit defect')\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "internal-QA test command failed"):
            board.request_review(
                self.root, agent["id"], self.ledger("unit-failure"), "must not queue",
                phase="final_acceptance", test_command="python3 -m unittest test_failure",
            )
        self.assertFalse(board.snapshot(self.root)["qa_requests"])

    def test_chunked_task_blocks_final_until_each_chunk_is_independently_passed(self):
        agent = self.delivery("CHUNKED")
        board.define_delivery_plan(self.root, agent["id"], "chunked", "Two bounded risk areas need separate review")
        board.declare_chunks(self.root, agent["id"], [("api", "API behavior"), ("ui", "user-visible behavior")])
        with self.assertRaisesRegex(ValueError, "every chunk to pass"):
            self.request(agent, "premature", "final_acceptance")
        for chunk in ("api", "ui"):
            self.pass_request(self.request(agent, chunk, "chunk", chunk=chunk), chunk)
        final = self.request(agent, "chunked-final", "final_acceptance")
        self.pass_request(final, "chunked-final")
        self.assertTrue(all(value["status"] == "passed" for value in board.snapshot(self.root)["task_chunks"]["CHUNKED"].values()))

    def test_application_enforces_dependencies_optional_chunks_and_integrated_final(self):
        agent = self.delivery("APPLICATION")
        board.define_delivery_plan(self.root, agent["id"], "application", "A full application has several independently acceptable capabilities")
        board.declare_subtasks(self.root, agent["id"], [
            {"id": "auth", "title": "Authentication", "acceptance_proof": "Users can authenticate", "dependencies": []},
            {"id": "workspace", "title": "Workspace", "acceptance_proof": "Users can manage work", "dependencies": ["auth"]},
            {"id": "reporting", "title": "Reporting", "acceptance_proof": "Users can read reports", "dependencies": ["workspace"]},
        ])
        board.declare_subtask_chunks(self.root, agent["id"], "workspace", [("api", "workspace API"), ("ui", "workspace UI")])
        with self.assertRaisesRegex(ValueError, "unmet dependencies"):
            self.request(agent, "workspace-early", "subtask_acceptance", subtask="workspace")
        self.pass_request(self.request(agent, "auth", "subtask_acceptance", subtask="auth"), "auth")
        with self.assertRaisesRegex(ValueError, "every declared subtask chunk"):
            self.request(agent, "workspace-before-chunks", "subtask_acceptance", subtask="workspace")
        for chunk in ("api", "ui"):
            scope = f"workspace-{chunk}"
            self.pass_request(self.request(agent, scope, "chunk", subtask="workspace", chunk=chunk), scope)
        self.pass_request(self.request(agent, "workspace", "subtask_acceptance", subtask="workspace"), "workspace")
        with self.assertRaisesRegex(ValueError, "every subtask acceptance"):
            self.request(agent, "application-early", "final_acceptance")
        self.pass_request(self.request(agent, "reporting", "subtask_acceptance", subtask="reporting"), "reporting")
        self.pass_request(self.request(agent, "application-final", "final_acceptance"), "application-final")
        state = board.snapshot(self.root)
        self.assertTrue(all(value["status"] == "passed" for value in state["delivery_plans"]["APPLICATION"]["subtasks"].values()))

    def test_application_rejects_missing_unknown_and_cyclic_subtask_graphs(self):
        agent = self.delivery("BAD-GRAPH")
        board.define_delivery_plan(self.root, agent["id"], "application", "Multiple capabilities require a dependency graph")
        with self.assertRaisesRegex(ValueError, "declared product subtasks"):
            self.request(agent, "empty-app", "final_acceptance")
        with self.assertRaisesRegex(ValueError, "unknown dependencies"):
            board.declare_subtasks(self.root, agent["id"], [{"id": "ui", "title": "UI", "acceptance_proof": "UI works", "dependencies": ["missing"]}])
        with self.assertRaisesRegex(ValueError, "must not contain a cycle"):
            board.declare_subtasks(self.root, agent["id"], [
                {"id": "one", "title": "One", "acceptance_proof": "One works", "dependencies": ["two"]},
                {"id": "two", "title": "Two", "acceptance_proof": "Two works", "dependencies": ["one"]},
            ])
        self.assertEqual(board.snapshot(self.root)["delivery_plans"]["BAD-GRAPH"]["subtasks"], {})

    def test_unisolated_subtasks_serialize_and_duplicate_scope_cannot_queue(self):
        agent = self.delivery("PARALLEL-APP")
        board.define_delivery_plan(self.root, agent["id"], "application", "Independent capabilities can be reviewed concurrently")
        board.declare_subtasks(self.root, agent["id"], [
            {"id": "alpha", "title": "Alpha", "acceptance_proof": "Alpha works", "dependencies": [], "owned_paths": ["alpha"]},
            {"id": "beta", "title": "Beta", "acceptance_proof": "Beta works", "dependencies": [], "owned_paths": ["beta"]},
        ])
        alpha = self.request(agent, "alpha", "subtask_acceptance", subtask="alpha")
        with self.assertRaisesRegex(ValueError, "distinct broker-created workspaces"):
            self.request(agent, "beta", "subtask_acceptance", subtask="beta")
        with self.assertRaisesRegex(ValueError, "active implementation scope"):
            self.request(agent, "alpha-duplicate", "subtask_acceptance", subtask="alpha")
        self.pass_request(alpha, "alpha")
        beta = self.request(agent, "beta", "subtask_acceptance", subtask="beta")
        self.pass_request(beta, "beta")
        subtasks = board.snapshot(self.root)["delivery_plans"]["PARALLEL-APP"]["subtasks"]
        self.assertEqual({name for name, value in subtasks.items() if value["status"] == "passed"}, {"alpha", "beta"})

    def test_reviewer_failure_requires_repair_cycle_before_atomic_acceptance(self):
        agent = self.delivery("REPAIR")
        board.define_delivery_plan(self.root, agent["id"], "atomic", "One cohesive repair")
        first = self.request(agent, "repair-first", "final_acceptance")
        board.claim_qa(self.root, self.reviewer["id"], first["id"], self.ledger("repair-challenge", reviewer=True))
        board.qa_result(self.root, self.reviewer["id"], first["id"], "failed", "Reviewer found an edge-case defect", self.evidence("repair-failed"))
        second = self.request(agent, "repair-second", "final_acceptance", changes="fixed the reviewer edge case")
        self.assertEqual(second["cycle"], 2)
        self.pass_request(second, "repair-second")

    def test_new_required_subtask_invalidates_older_application_final_acceptance(self):
        agent = self.delivery("EXPANDING-APP")
        board.define_delivery_plan(self.root, agent["id"], "application", "The product begins with one known capability")
        board.declare_subtasks(self.root, agent["id"], [{"id": "core", "title": "Core", "acceptance_proof": "Core works", "dependencies": []}])
        self.pass_request(self.request(agent, "core", "subtask_acceptance", subtask="core"), "core")
        original_final = self.request(agent, "initial-final", "final_acceptance")
        self.pass_request(original_final, "initial-final")
        board.declare_subtasks(self.root, agent["id"], [{"id": "required", "title": "Required follow-up capability", "acceptance_proof": "Required capability works", "dependencies": ["core"]}], "The first implementation exposed a required end-to-end boundary.")
        proof = self.root / "contract-proof.txt"
        proof.write_text("delivery proven\n", encoding="utf-8")
        contract.add_evidence(self.root, "EXPANDING-APP", "delivery", [proof])
        with self.assertRaisesRegex(ValueError, "passed final independent review"):
            board.complete(self.root, agent["id"], "must not accept stale final review")
        self.assertNotEqual(
            original_final["structure_revision"],
            board.snapshot(self.root)["delivery_plans"]["EXPANDING-APP"]["structure_revision"],
        )
        self.pass_request(self.request(agent, "required", "subtask_acceptance", subtask="required"), "required")
        replacement_final = self.request(
            agent, "expanded-final", "final_acceptance",
            changes="added and independently accepted the newly required capability",
        )
        self.assertEqual(replacement_final["cycle"], 2)
        self.pass_request(replacement_final, "expanded-final")
        board.complete(self.root, agent["id"], "current application structure independently accepted")
        self.assertEqual(board.snapshot(self.root)["agents"][agent["id"]]["status"], "done")

    def test_scope_expansion_requires_and_records_owner_readable_reason(self):
        agent = self.delivery("EXPLAIN-GROWTH")
        board.define_delivery_plan(self.root, agent["id"], "application", "Start with the known capability")
        board.declare_subtasks(self.root, agent["id"], [
            {"id": "core", "title": "Core", "acceptance_proof": "Core works", "dependencies": []},
        ])
        addition = [
            {"id": "safety", "title": "Safety boundary", "acceptance_proof": "Unsafe input is rejected", "dependencies": ["core"]},
        ]
        with self.assertRaisesRegex(ValueError, "scope expansion reason"):
            board.declare_subtasks(self.root, agent["id"], addition)
        board.declare_subtasks(
            self.root, agent["id"], addition,
            "Adversarial testing exposed an input boundary required by the agreed objective.",
        )
        plan = board.snapshot(self.root)["delivery_plans"]["EXPLAIN-GROWTH"]
        self.assertEqual(len(plan["structure_changes"]), 1)
        self.assertEqual(plan["structure_changes"][0]["added"], ["safety"])
        self.assertIn("Adversarial testing", plan["structure_changes"][0]["reason"])
        self.assertTrue(plan["structure_changes"][0]["at"])

    def test_cto_release_gate_uses_whole_application_subtask_completion(self):
        agent = self.delivery("CTO-APP")
        board.define_delivery_plan(self.root, agent["id"], "application", "Two product capabilities must both be accepted")
        board.declare_subtasks(self.root, agent["id"], [
            {"id": "one", "title": "One", "acceptance_proof": "One works", "dependencies": []},
            {"id": "two", "title": "Two", "acceptance_proof": "Two works", "dependencies": []},
        ])
        ledger = self.ledger("cto-final")
        challenge = self.ledger("cto-challenge", reviewer=True)
        evidence = self.evidence("cto")
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["status"] = "done"
            state["agents"][agent["id"]]["active"] = False
            state["delivery_plans"]["CTO-APP"]["subtasks"]["one"]["status"] = "passed"
            common = {
                "task": "CTO-APP", "stage": board.INDEPENDENT_REVIEW, "status": "passed",
                "cycle": 1, "evidence": evidence, "delivery_evidence": evidence,
                "unit_test_command": "python3 -m unittest test_smoke", "unit_test_evidence": evidence,
                "ledger": ledger, "challenge_ledger": challenge,
                "developer_id": agent["id"], "claimed_by": self.reviewer["id"],
                "requested_at": board.now(), "review_wait_started_at": board.now(),
            }
            state["qa_requests"]["one-final"] = {"id": "one-final", "phase": "subtask_acceptance", "subtask": "one", "chunk": "subtask-final", **common}
            state["qa_requests"]["two-final"] = {"id": "two-final", "phase": "subtask_acceptance", "subtask": "two", "chunk": "subtask-final", **common}
            state["qa_requests"]["app-final"] = {"id": "app-final", "phase": "final_acceptance", "subtask": "", "chunk": "final", **common}
        artifact = {
            "branch": "main", "task_artifact_clean": True,
            "artifact_commit_pushed": True, "artifact_commit_exact": True,
            "artifact_health_verified": True, "task_artifact_release_verified": True,
            "artifact_health_output": "PASS", "head_commit": "abc",
        }
        with patch.object(cto, "_task_artifact_gate", return_value=artifact), patch.object(cto, "ledger_complete", return_value=(True, [])), patch.object(board, "simulation_evidence_complete", return_value=(True, [])), patch.object(contract, "contract_complete", return_value=(True, [], {"objective": "OWNER DIRECTION — CTO-APP"})):
            blocked = cto.release_check(self.root, "CTO-APP", self.root / ledger, self.root)
            self.assertFalse(blocked["product_subtasks_complete"])
            self.assertFalse(blocked["delivery_chunks_complete"])
            with board.locked_state(self.root) as state:
                state["delivery_plans"]["CTO-APP"]["subtasks"]["two"]["status"] = "passed"
            ready_structure = cto.release_check(self.root, "CTO-APP", self.root / ledger, self.root)
            self.assertTrue(ready_structure["product_subtasks_complete"])
            self.assertTrue(ready_structure["delivery_chunks_complete"])


if __name__ == "__main__":
    unittest.main()
