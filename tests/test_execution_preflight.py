# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Adversarial dependency preflight coverage for expensive governed gates."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, execution_preflight


class ExecutionPreflightTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run"},
            "devDependencies": {"vitest": "1.0.0"},
        }))
        (self.web / "package-lock.json").write_text("{}\n")

    def test_missing_late_node_dependency_blocks_before_any_scenario_runs(self):
        ledger = self.root / "ledger.md"
        ledger.write_text(
            "| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | Python gate | `python3 -m unittest tests.test_smoke` | Python tests execute. | Python tests executed. | PASS |\n"
            "| S-002 | Web gate | `npm test --prefix web` | Web tests execute. | Web tests executed. | PASS |\n"
        )
        with patch("harness.board.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "Node dependencies are not installed"):
                board._execute_scenario_simulations(self.root, ledger)
        execute.assert_not_called()

    def test_install_declared_before_test_satisfies_preflight_without_installing(self):
        result = execution_preflight.validate_commands(
            self.root, ["npm ci --prefix web", "npm test --prefix web"],
        )
        self.assertEqual(result, {"checked_commands": 2, "status": "passed"})
        self.assertFalse((self.web / "node_modules").exists(), "preflight must never mutate dependencies")

    def test_test_before_install_is_rejected_even_when_install_appears_later(self):
        with self.assertRaisesRegex(ValueError, "run the lockfile install before"):
            execution_preflight.validate_commands(
                self.root, ["npm test --prefix web", "npm ci --prefix web"],
            )

    def test_prefix_escape_and_missing_executable_are_aggregated(self):
        with self.assertRaises(ValueError) as failure:
            execution_preflight.validate_commands(
                self.root,
                ["npm test --prefix ../outside", "definitely-missing-test-runner --version"],
            )
        message = str(failure.exception)
        self.assertIn("must stay inside", message)
        self.assertIn("required test executable is unavailable", message)

    def test_corrupt_package_manifest_fails_before_execution(self):
        (self.web / "package.json").write_text("{not-json")
        with self.assertRaisesRegex(ValueError, "cannot read Node dependency manifest"):
            execution_preflight.validate_commands(self.root, ["npm test --prefix web"])


if __name__ == "__main__":
    unittest.main()
