# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import board, certified_execution, contract, control, execution_identity
from tests.requirements_support import agreed_requirements


class DeliveryExecutionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.candidate = execution_identity.candidate_evidence_identity(
            "c" * 40, "t" * 40, "contract-1", {"ledger": "l" * 64},
        )

    def execute(self, command: str, *, retry_reason: str = ""):
        return certified_execution.run(
            self.root, self.root, command, candidate=self.candidate,
            environment_sha256="e" * 64, environment=self.environment,
            lockfile_digests={}, role="delivery", gate="final_acceptance",
            retry_reason=retry_reason,
        )

    def test_same_delivery_and_scenario_command_launches_once(self):
        (self.root / "test_smoke.py").write_text(
            "import unittest\nclass Smoke(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n"
        )
        task = "DELIVERY-ONCE"
        contract.create_contract(self.root, task, "Deliver once", ["delivery"])
        proof = self.root / "proof.txt"; proof.write_text("proof\n")
        contract.add_evidence(self.root, task, "delivery", [proof])
        command = "python3 -m unittest test_smoke"
        ledger = self.root / "ledger.md"
        ledger.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            f"| S-001 | The complete smoke behavior runs successfully. | `{command}` | One test passes. | One test passed. | PASS |\n"
        )
        session = control.create(self.root, "codex_delivery")
        delivery = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Deliver once")
        board.begin_task(self.root, delivery["id"], task)
        agreed_requirements(self.root, delivery["id"], "Deliver once with exact evidence.")
        board.define_delivery_plan(self.root, delivery["id"], "atomic", "One cohesive task")
        original = certified_execution.subprocess.Popen
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            if kwargs.get("shell"):
                calls += 1
            return original(*args, **kwargs)

        with patch.object(certified_execution.subprocess, "Popen", side_effect=counted):
            request = board.request_review(
                self.root, delivery["id"], str(ledger), "Review once",
                phase="final_acceptance", test_command=command,
            )
        self.assertEqual(calls, 1)
        executions = request.get("command_executions", [])
        if executions:
            self.assertIn("exact_success_reused", [item.get("cache_decision") for item in executions])

    def test_failed_retry_needs_reason_then_success_is_reused(self):
        marker = self.root / "repaired"
        command = (
            "python3 -c \"import pathlib,sys; "
            f"ok=pathlib.Path(r'{marker}').exists(); "
            "print('Ran 1 test in 0.001s'); print('OK' if ok else 'FAILED'); sys.exit(0 if ok else 1)\""
        )
        original = certified_execution.subprocess.Popen
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            if kwargs.get("shell"):
                calls += 1
            return original(*args, **kwargs)

        with patch.object(certified_execution.subprocess, "Popen", side_effect=counted):
            with self.assertRaisesRegex(ValueError, "failed with exit code"):
                self.execute(command)
            with self.assertRaisesRegex(ValueError, "repair reason"):
                self.execute(command)
            marker.write_text("fixed\n")
            passed = self.execute(command, retry_reason="The failing condition was repaired.")
            reused = self.execute(command)
        self.assertEqual(calls, 2)
        self.assertEqual(passed["measurement"]["cache_decision"], "executed_and_certified")
        self.assertEqual(reused["measurement"]["cache_decision"], "exact_success_reused")
        records = [__import__("json").loads(line) for line in (board.board_dir(self.root) / execution_identity.STORE_NAME).read_text().splitlines()]
        self.assertEqual(records[-1]["retry_of"], records[0]["record_id"])

    def test_concurrent_exact_success_runs_one_process(self):
        command = "python3 -c \"import time; time.sleep(.15); print('Ran 1 test in 0.001s'); print('OK')\""
        original = certified_execution.subprocess.Popen
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            if kwargs.get("shell"):
                calls += 1
            return original(*args, **kwargs)

        with patch.object(certified_execution.subprocess, "Popen", side_effect=counted):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.execute(command), range(4)))
        self.assertEqual(calls, 1)
        self.assertEqual(
            sorted(item["measurement"]["cache_decision"] for item in results),
            ["exact_success_reused"] * 3 + ["executed_and_certified"],
        )


if __name__ == "__main__":
    unittest.main()
