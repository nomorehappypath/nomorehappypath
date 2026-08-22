# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control, cto, lifecycle


class LifecycleInstrumentationTests(unittest.TestCase):
    def test_internal_qa_records_command_boundaries_and_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "test_probe.py").write_text(
                "import unittest\n"
                "class Probe(unittest.TestCase):\n"
                "    def test_true(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            measurement = {}
            output = board._execute_internal_qa(
                "python3 -m unittest test_probe", root, measurement=measurement,
            )

        self.assertIn("Ran 1 test", output)
        self.assertEqual(measurement["command"], "python3 -m unittest test_probe")
        self.assertEqual(len(measurement["command_fingerprint"]), 64)
        self.assertEqual(measurement["exit_code"], 0)
        self.assertEqual(measurement["cache_decision"], "executed_no_cache_store")
        self.assertGreaterEqual(measurement["duration_seconds"], 0)
        self.assertLessEqual(measurement["started_at"], measurement["finished_at"])

    def test_scenario_execution_records_each_command_and_deduplication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "test_probe.py").write_text(
                "import unittest\n"
                "class Probe(unittest.TestCase):\n"
                "    def test_true(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            ledger = root / "ledger.md"
            ledger.write_text(
                "| ID | Description | Simulation command | Expected system response | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|\n"
                "| S-one | first | `python3 -m unittest test_probe` | One test executes successfully. | One test executed successfully. | PASS |\n"
                "| S-two | second | `python3 -m unittest test_probe` | The shared command remains successful. | The shared command remained successful. | PASS |\n",
                encoding="utf-8",
            )

            results = board._execute_scenario_simulations(root, ledger)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["cache_decision"], "executed_no_cache_store")
        self.assertGreaterEqual(results[0]["duration_seconds"], 0)
        self.assertEqual(results[1]["cache_decision"], "same_request_deduplicated")
        self.assertEqual(results[1]["deduplicated_from"], "S-one")
        self.assertEqual(results[1]["duration_seconds"], 0.0)

    def test_task_summary_reconciles_exclusive_phases_to_wall_clock(self):
        state = {
            "events": [
                {"kind": "task_begun", "task": "TASK", "at": "2026-08-16T10:00:00+00:00"},
                {"kind": "visual_test_required", "task": "TASK", "at": "2026-08-16T10:00:10+00:00"},
            ],
            "qa_requests": {
                "review-TASK-scope-01": {
                    "id": "review-TASK-scope-01",
                    "task": "TASK",
                    "phase": "subtask_acceptance",
                    "subtask": "scope",
                    "lifecycle": {
                        "implementation": lifecycle.phase(
                            "2026-08-16T10:00:00+00:00", "2026-08-16T10:00:02+00:00"
                        ),
                        "unit_execution": lifecycle.phase(
                            "2026-08-16T10:00:02+00:00", "2026-08-16T10:00:03+00:00"
                        ),
                        "scenario_execution": lifecycle.phase(
                            "2026-08-16T10:00:03+00:00", "2026-08-16T10:00:04+00:00"
                        ),
                        "review_queue": lifecycle.phase(
                            "2026-08-16T10:00:04+00:00", "2026-08-16T10:00:05+00:00"
                        ),
                        "challenge_authoring": lifecycle.phase(
                            "2026-08-16T10:00:05+00:00", "2026-08-16T10:00:06+00:00"
                        ),
                        "formal_review": lifecycle.phase(
                            "2026-08-16T10:00:06+00:00", "2026-08-16T10:00:08+00:00"
                        ),
                        "verdict": lifecycle.phase(
                            "2026-08-16T10:00:08+00:00", "2026-08-16T10:00:09+00:00"
                        ),
                    },
                    "command_executions": [{"command": "python3 -m unittest", "duration_seconds": 1.0}],
                }
            },
            "archive": [],
            "releases": {"TASK": {"recorded_at": "2026-08-16T10:00:10+00:00"}},
        }

        summary = lifecycle.task_summary(state, "TASK")

        self.assertEqual(summary["wall_clock_seconds"], 10.0)
        self.assertEqual(summary["reconciliation"]["difference_seconds"], 0.0)
        self.assertEqual(summary["phase_totals_seconds"]["unattributed"], 1.0)
        self.assertEqual(summary["command_execution_count"], 1)
        self.assertTrue(summary["reconciliation"]["within_tolerance"])

    def test_phase_rejects_reversed_or_invalid_boundaries_without_guessing(self):
        self.assertEqual(lifecycle.phase("bad", "also-bad"), {})
        self.assertEqual(
            lifecycle.phase("2026-08-16T10:00:02+00:00", "2026-08-16T10:00:01+00:00"),
            {},
        )

    def test_review_request_persists_all_review_phase_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "test_probe.py").write_text(
                "import unittest\n"
                "class Probe(unittest.TestCase):\n"
                "    def test_true(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            delivery_ledger = root / "delivery.md"
            challenge_ledger = root / "challenge.md"
            delivery_ledger.write_text(
                "| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|---|\n"
                "| S-delivery | Delivery execution records complete timing boundaries while the requested behavior succeeds. | delivery | `python3 -m unittest test_probe` | Delivery probe succeeds. | Delivery probe succeeded. | PASS |\n",
                encoding="utf-8",
            )
            challenge_ledger.write_text(
                "| ID | What was tested | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
                "|---|---|---|---|---|---|---|\n"
                "| S-review | Independent execution records its own complete timing boundaries while the challenge succeeds. | review | `python3 -m unittest test_probe -k true` | Reviewer probe succeeds independently. | Reviewer probe succeeded independently. | PASS |\n",
                encoding="utf-8",
            )
            evidence = root / "evidence.txt"
            evidence.write_text("command: python3 -m unittest test_probe -k true\nresult: PASS\n", encoding="utf-8")
            session = control.create(root, "codex_delivery")
            developer = board.register(
                root, "development", board.AWAITING_OWNER_DIRECTION,
                vendor="OpenAI", session_id=session["id"],
            )
            board.record_owner_direction(root, session["id"], "Instrument the lifecycle")
            board.begin_task(root, developer["id"], "TASK")
            board.record_requirement_confirmation(root, developer["id"], "Instrument and verify each phase.")
            contract.create_contract(root, "TASK", "Instrument the lifecycle", ["telemetry"])
            board.define_delivery_plan(root, developer["id"], "application", "Telemetry is a product capability.")
            board.declare_subtasks(root, developer["id"], [{
                "id": "telemetry", "title": "Telemetry", "description": "Telemetry",
                "acceptance_proof": "Executable phase assertions", "dependencies": [],
            }])
            board.start_subtask(root, developer["id"], "telemetry")
            request = board.request_review(
                root, developer["id"], str(delivery_ledger), "Review telemetry",
                phase="subtask_acceptance", subtask="telemetry",
                test_command="python3 -m unittest test_probe",
            )
            self.assertIn("duration_seconds", request["lifecycle"]["unit_execution"])
            self.assertIn("duration_seconds", request["lifecycle"]["scenario_execution"])
            self.assertEqual({item["kind"] for item in request["command_executions"]}, {"unit_test", "delivery_scenario"})
            reviewer = board.register(root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
            reserved = board.reserve_qa(root, reviewer["id"], request["id"])
            self.assertIn("duration_seconds", reserved["lifecycle"]["review_queue"])
            claimed = board.attach_challenge_ledger(root, reviewer["id"], request["id"], str(challenge_ledger))
            self.assertIn("duration_seconds", claimed["lifecycle"]["challenge_authoring"])
            board.execute_challenge(root, reviewer["id"], request["id"])
            result = board.qa_result(
                root, reviewer["id"], request["id"], "passed", "Telemetry passed", str(evidence),
            )

        self.assertIn("duration_seconds", result["lifecycle"]["formal_review"])
        self.assertIn("duration_seconds", result["lifecycle"]["verdict"])
        self.assertIn("reviewer_scenario", {item["kind"] for item in result["command_executions"]})

    def test_release_artifact_and_health_commands_are_timed_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
            (root / "test_probe.py").write_text(
                "import unittest\n"
                "class Probe(unittest.TestCase):\n"
                "    def test_true(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "test_probe.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

            result = cto._task_artifact_gate(
                root, "TASK", root,
                {"reviewed_commit": commit, "reviewed_files": ["test_probe.py"]},
                True, "python3 -m unittest test_probe", check_remote=False,
            )

        self.assertTrue(result["artifact_health_verified"])
        self.assertIn("duration_seconds", result["lifecycle"]["artifact_materialization"])
        self.assertIn("duration_seconds", result["lifecycle"]["health"])
        self.assertEqual(result["command_executions"][0]["cache_decision"], "executed_no_cache_store")


if __name__ == "__main__":
    unittest.main()
