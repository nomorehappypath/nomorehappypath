# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A run that finished and failed is a result; a run that did not finish is not (task 2026-11-08_accept-after-repair, Codex r3-2 / r4-1).

The owner may waive a review whose checks the board ran to a FAIL; never one whose checks never reached a result. The board used to
tell the two apart by words in the message (an `assertIn` saying "not found" read as "never ran"), then by labelling every error a
completed failure (a real timeout read as "ran and failed"). Now the execution layer says which it was: a timed-out command raises
ExecutionIncomplete; the simulation runner raises ScenarioSimulationsFailed (completed) only when every command finished."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, certified_execution, execution_identity

LEDGER = ("| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
          "|---|---|---|---|---|---|\n"
          "| S-001 | The heading is shown on the page the owner sees. | `python3 -m unittest test_heading` | The heading is shown | PASS: heading observed | PASS |\n"
          "| S-002 | The page answers within the time the owner waits. | `python3 -m unittest test_speed` | The page answers in time | PASS: answered in time | PASS |\n")


class RealCommandsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {})

    def run_command(self, command: str, timeout_seconds: int = 1):
        return certified_execution.run(self.root, self.root, command, candidate=self.candidate, environment_sha256="e" * 64,
                                       environment=os.environ, lockfile_digests={}, role="reviewer", gate="final", timeout_seconds=timeout_seconds)

    def test_a_real_timed_out_command_is_incomplete_not_a_failure(self):
        # it has already said something (a FAIL line), so it is not "did not run" either: it started, spoke, and never finished
        command = "python3 -c \"import sys, time; print('test_slow ... FAIL'); sys.stdout.flush(); time.sleep(4)\""
        with self.assertRaises(certified_execution.ExecutionIncomplete) as caught:
            self.run_command(command, timeout_seconds=1)
        self.assertIn("timed out after 1 seconds", str(caught.exception))
        self.assertFalse(caught.exception.completed)

    def test_a_real_failing_unittest_is_a_completed_failure(self):
        (self.root / "test_heading.py").write_text(
            "import unittest\n\nclass Heading(unittest.TestCase):\n    def test_heading(self):\n        self.assertIn('Welcome', '1', 'Required heading not found')\n")
        with self.assertRaises(certified_execution.ChecksFailed) as caught:
            self.run_command("python3 -m unittest test_heading", timeout_seconds=60)
        self.assertTrue(caught.exception.completed)
        self.assertEqual(caught.exception.test_count, 1)
        self.assertIn("not found", str(caught.exception), "the ordinary assertIn words are in the message and change nothing")

    def test_a_real_empty_test_discovery_is_not_a_result(self):
        # Codex r5-1: "Ran 0 tests / OK" is not a failed run of the work; it tested nothing
        (self.root / "empty").mkdir()
        with self.assertRaises(ValueError) as caught:
            self.run_command("python3 -m unittest discover -s empty -p 'check_*.py'", timeout_seconds=60)
        self.assertNotIsInstance(caught.exception, certified_execution.ChecksFailed)
        self.assertFalse(getattr(caught.exception, "completed", False))
        self.assertIn("zero executed tests", str(caught.exception))


class RunnerOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "docs").mkdir()
        self.ledger = self.root / "docs" / "ledger.md"
        self.ledger.write_text(LEDGER)

    def test_one_unfinished_command_makes_the_whole_execution_unfinished(self):
        def fake(command, root, **_):
            if "test_speed" in command:
                raise certified_execution.ExecutionIncomplete("internal-QA test command timed out after 1 seconds; partial output was not certified", "partial")
            raise certified_execution.ChecksFailed("AssertionError: 'Welcome' not found in '1' / Ran 1 test / FAILED (failures=1)", test_count=1, exit_code=1)
        with mock.patch.object(board, "_execute_internal_qa", side_effect=fake):
            with self.assertRaises(ValueError) as caught:
                board._execute_scenario_simulations(self.root, self.ledger)
        self.assertNotIsInstance(caught.exception, board.ScenarioSimulationsFailed)
        self.assertFalse(getattr(caught.exception, "completed", False), "an unfinished run is never a completed failure")
        self.assertIn("did not reach a result", str(caught.exception))
        self.assertIn("also failed", str(caught.exception))

    def test_a_scenario_that_found_zero_tests_makes_the_execution_no_result(self):
        def fake(command, root, **_):
            if "test_speed" in command:
                raise ValueError("internal-QA test command reported zero executed tests")
            raise certified_execution.ChecksFailed("Ran 1 test / FAILED (failures=1)", test_count=1, exit_code=1)
        with mock.patch.object(board, "_execute_internal_qa", side_effect=fake):
            with self.assertRaises(ValueError) as caught:
                board._execute_scenario_simulations(self.root, self.ledger)
        self.assertNotIsInstance(caught.exception, board.ScenarioSimulationsFailed)
        self.assertFalse(getattr(caught.exception, "completed", False))
        self.assertIn("zero executed tests", str(caught.exception))

    def test_every_command_finished_and_some_failed_is_a_completed_failure(self):
        def fake(command, root, **_):
            if "test_heading" in command:
                raise certified_execution.ChecksFailed("AssertionError: 'Welcome' not found in '1' / Ran 1 test / FAILED (failures=1)", test_count=1, exit_code=1)
            return "Ran 1 test in 0.001s\nOK"
        with mock.patch.object(board, "_execute_internal_qa", side_effect=fake):
            with self.assertRaises(board.ScenarioSimulationsFailed) as caught:
                board._execute_scenario_simulations(self.root, self.ledger)
        self.assertTrue(caught.exception.completed)
        self.assertEqual([r["outcome"] for r in caught.exception.results], ["failed", "passed"])
        self.assertEqual(caught.exception.failures[0].split(":")[0], "S-001")

    def test_the_waiver_rule_reads_the_recorded_fact(self):
        finished = {"status": "failed", "challenge_execution_attempts": [{"status": "failed", "completed": True, "error_type": "ScenarioSimulationsFailed",
                                                                            "reason": "Scenario Ledger simulations failed: S-001: 'Welcome' not found"}]}
        unfinished = {"status": "failed", "challenge_execution_attempts": [{"status": "failed", "completed": False, "error_type": "ValueError",
                                                                              "reason": "Scenario Ledger simulations did not finish: S-002: timed out | also failed: S-001: FAILED (failures=1)"}]}
        self.assertTrue(board._reviewer_checks_ran(finished))
        self.assertFalse(board._reviewer_checks_ran(unfinished), "a timeout stays untested whatever words sit beside it")


if __name__ == "__main__":
    unittest.main()
