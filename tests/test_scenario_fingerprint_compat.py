# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import tempfile
import unittest
from pathlib import Path

from harness import contract


LEGACY_HEADER = (
    "| ID | Scenario | Simulation command | Expected system response | "
    "Observed system response | QA result |\n"
    "|---|---|---|---|---|---|\n"
)
OWNER_READABLE_HEADER = (
    "| ID | What was tested | Scenario | Simulation command | "
    "Expected system response | Observed system response | QA result |\n"
    "|---|---|---|---|---|---|---|\n"
)
OWNER_READABLE_WITH_NOTES_HEADER = (
    "| ID | What was tested | Simulation command | Expected system response | "
    "Observed system response | QA result | Notes |\n"
    "|---|---|---|---|---|---|---|\n"
)


class ScenarioFingerprintCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _fingerprints(self, text):
        path = self.root / "ledger.md"
        path.write_text(text)
        return contract.scenario_fingerprints(path)

    def test_owner_readable_schema_fingerprints_executable_command(self):
        fingerprints = self._fingerprints(
            OWNER_READABLE_HEADER
            + "| S-1 | The saved project reopens without losing work. | shared prose | "
              "`python3 -m unittest tests.test_reopen` | Work is restored. | PASS: restored | PASS |\n"
        )
        self.assertEqual(fingerprints, {"python3 -m unittest tests.test_reopen"})

    def test_distinct_new_commands_remain_independent_when_prose_matches(self):
        delivery = self._fingerprints(
            OWNER_READABLE_HEADER
            + "| S-1 | The release proceeds only after its checks pass. | shared prose | "
              "`python3 -m unittest tests.test_delivery` | Delivery succeeds. | PASS: complete | PASS |\n"
        )
        challenge = self._fingerprints(
            OWNER_READABLE_HEADER
            + "| S-2 | A separate check challenges the release boundary. | shared prose | "
              "`python3 -m unittest tests.test_challenge` | Challenge succeeds. | PASS: complete | PASS |\n"
        )
        self.assertEqual(challenge - delivery, {"python3 -m unittest tests.test_challenge"})

    def test_rewording_does_not_make_an_identical_command_independent(self):
        delivery = self._fingerprints(
            OWNER_READABLE_HEADER
            + "| S-1 | Delivery checks the saved project. | first narrative | "
              "`python3 -m unittest tests.test_shared` | It succeeds. | PASS: complete | PASS |\n"
        )
        challenge = self._fingerprints(
            OWNER_READABLE_HEADER
            + "| S-2 | The reviewer checks restoration. | different narrative | "
              "`python3 -m unittest tests.test_shared` | It succeeds. | PASS: complete | PASS |\n"
        )
        self.assertFalse(challenge - delivery)

    def test_named_command_column_remains_authoritative_with_trailing_fields(self):
        delivery = self._fingerprints(
            OWNER_READABLE_WITH_NOTES_HEADER
            + "| S-1 | Delivery checks the saved project. | "
              "`python3 -m unittest tests.test_shared` | It succeeds. | "
              "PASS: complete | PASS | delivery note |\n"
        )
        challenge = self._fingerprints(
            OWNER_READABLE_WITH_NOTES_HEADER
            + "| S-2 | The reviewer uses different prose for the same behavior. | "
              "`python3 -m unittest tests.test_shared` | A different expectation. | "
              "PASS: complete | PASS | reviewer note |\n"
        )
        self.assertEqual(delivery, {"python3 -m unittest tests.test_shared"})
        self.assertEqual(challenge, delivery)
        self.assertFalse(challenge - delivery)

    def test_headerless_historical_rows_keep_the_positional_fallback(self):
        fingerprints = self._fingerprints(
            "| S-1 | historical prose | `python3 -m unittest tests.test_headerless` | "
            "It succeeds. | PASS: complete | PASS |\n"
        )
        self.assertEqual(fingerprints, {"python3 -m unittest tests.test_headerless"})

    def test_legacy_six_column_schema_keeps_command_identity(self):
        fingerprints = self._fingerprints(
            LEGACY_HEADER
            + "| S-1 | legacy prose | `python3 -m unittest tests.test_legacy` | "
              "It succeeds. | PASS: complete | PASS |\n"
        )
        self.assertEqual(fingerprints, {"python3 -m unittest tests.test_legacy"})


if __name__ == "__main__":
    unittest.main()
