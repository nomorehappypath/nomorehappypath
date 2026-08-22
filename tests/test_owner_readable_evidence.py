# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control


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

    def test_new_submission_requires_owner_readable_column(self):
        legacy_header = (
            "| ID | Scenario | Simulation command | Expected system response | "
            "Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
        )
        path = self.ledger("A technical legacy description", header=legacy_header)
        valid, problems = contract.scenario_submission_exists(path)
        self.assertFalse(valid)
        self.assertIn("What was tested", " ".join(problems))

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

    def test_commands_paths_hashes_identifiers_and_controls_are_rejected(self):
        invalid = (
            "pytest tests/test_board.py -k ledger",
            "/private/tmp/tests/test_board.py",
            "a" * 40,
            "OWNER_RELEASE_REPAIR_REQUIRED",
            "Regression suite passed and reviewer challenge executed with CAS hash validation",
            "Readable words with a hidden\x07 control character inside.",
        )
        for description in invalid:
            with self.subTest(description=description):
                valid, problems = contract.scenario_submission_exists(self.ledger(description))
                self.assertFalse(valid)
                self.assertIn("What was tested", " ".join(problems))

    def test_atomic_request_qa_enforces_wording_at_live_intake_and_verdict(self):
        (self.root / "test_smoke.py").write_text(
            "import unittest\nclass Smoke(unittest.TestCase):\n"
            "    def test_safe(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        session = control.create(self.root, "codex_delivery")
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Deliver one safe behavior.")
        board.begin_task(self.root, delivery["id"], "ATOMIC-WORDING")
        board.record_requirement_confirmation(
            self.root, delivery["id"], "Confirm one safe behavior and its evidence.",
        )
        contract.create_contract(self.root, "ATOMIC-WORDING", "Deliver safely.", ["safe behavior"])
        board.define_delivery_plan(self.root, delivery["id"], "atomic", "One cohesive behavior")
        legacy = self.root / "legacy.md"
        legacy.write_text(
            "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-LEGACY-001 | old text | `python3 -m unittest test_smoke` | It stays safe | PASS: safe | PASS |\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "What was tested"):
            board.request_qa(self.root, delivery["id"], str(legacy), "check live intake")

        submitted = self.ledger(
            "The atomic behavior remains safe when its focused check is executed."
        )
        request = board.request_qa(
            self.root, delivery["id"], str(submitted), "check live intake",
        )
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.claim_qa(self.root, reviewer["id"], request["id"])
        submitted.write_text(
            submitted.read_text(encoding="utf-8").replace(
                "The atomic behavior remains safe when its focused check is executed.",
                "Regression suite passed and reviewer challenge executed with CAS hash validation",
            ),
            encoding="utf-8",
        )
        evidence = self.root / "evidence.txt"
        evidence.write_text("command: python3 -m unittest test_smoke\nresult: PASS\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "testing jargon"):
            board.qa_result(
                self.root, reviewer["id"], request["id"], "passed",
                "atomic behavior passed", str(evidence),
            )

    def test_legacy_validation_ignores_new_wording_rules_even_when_column_exists(self):
        path = self.ledger("Regression suite passed")
        valid, problems = contract.scenario_ledger_exists(path)
        self.assertTrue(valid, problems)
        valid, problems = contract.scenario_submission_exists(path)
        self.assertFalse(valid)
        self.assertIn("What was tested", " ".join(problems))

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
