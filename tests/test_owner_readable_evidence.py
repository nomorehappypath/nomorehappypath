# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control
from tests.requirements_support import agreed_requirements


HEADER = (
    "| ID | What was tested | Simulation command | Expected system response | "
    "Observed system response | QA result |\n"
    "|---|---|---|---|---|---|\n"
)


class OwnerReadableEvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def ledger(self, description: str, *, header: str = HEADER) -> Path:
        path = self.root / "ledger.md"
        path.write_text(
            header
            + "| S-OWNER-001 | "
            + description
            + " | `python3 -m unittest tests.test_owner_readable_evidence` | "
              "The submitted behavior remains true | PASS: the behavior was observed | PASS |\n",
            encoding="utf-8",
        )
        return path

    def test_legacy_validation_remains_compatible(self):
        legacy_header = (
            "| ID | Scenario | Simulation command | Expected system response | "
            "Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
        )
        path = self.ledger("A legacy description remains readable", header=legacy_header)
        valid, problems = contract.scenario_ledger_exists(path)
        self.assertTrue(valid, problems)

    def test_meaningful_plain_language_is_accepted(self):
        path = self.ledger(
            "Opening a saved project returns to the same work without losing progress."
        )
        valid, problems, rows = contract.scenario_submission_simulations(path)
        self.assertTrue(valid, problems)
        self.assertEqual(
            rows[0]["what_was_tested"],
            "Opening a saved project returns to the same work without losing progress.",
        )

    def test_execution_evidence_carries_the_certified_wording(self):
        description = "A failed check remains visible and is never shown as passed."
        evidence = board._store_internal_qa_evidence(
            self.root,
            "python3 -m unittest tests.test_owner_readable_evidence",
            "Ran 1 test\nOK",
            [{
                "id": "S-OWNER-001",
                "what_was_tested": description,
                "command": "python3 -m unittest tests.test_owner_readable_evidence",
                "expected_response": "The failure is visible",
                "outcome": "passed",
                "output": "Ran 1 test\nOK",
            }],
        )
        text = Path(evidence).read_text(encoding="utf-8")
        self.assertIn(f"what was tested: {description}", text)


if __name__ == "__main__":
    unittest.main()
