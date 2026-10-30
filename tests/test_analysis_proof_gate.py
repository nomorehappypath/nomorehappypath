# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""An analysis on the full process hands in proof tests and an examined list (task 2026-10-28_analysis-proof-tests).

Run:  PYTHONPATH=. python3 -m unittest tests.test_analysis_proof_gate -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from harness import board, standing_checks

FLOWS = ("Flows: money: every caller of the AI gateway, entry billing.py:10 to the retry in worker.py:200; identity: every route against frozen, killed and "
         "trial accounts, entry routes.py:5 to the license check at gates.py:40; data lifecycle: upload, confirm, retry and crash paths, entry upload.py:20 to jobs.py:90")
GOOD = ("Security: read every route for access rules.\nCode quality: report only.\nProof: tests/test_issue_1.py shows issue 1; issue 2 is code evidence in the report, section 2 (jobs.py:90).\n"
        "Examined: server routes, workers, billing, website.\n" + "\nHelpers: 6 helper agents; raw outputs in evidence/helpers/\nHelper findings dropped: none")
GOOD_NO_TESTS = GOOD.replace("tests/test_issue_1.py shows issue 1; issue 2 is code evidence in the report", "every issue is code evidence in the report")
REPORT = "FINDINGS.md: a list of the problems found"
TESTS = "Automated tests that prove each finding"


class HandInTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_complete_hand_in_passes(self):
        self.assertEqual(standing_checks.analysis_hand_in_problem(GOOD, ["FINDINGS.md", "tests/test_issue_1.py"]), "")

    def test_a_proof_line_that_names_tests_needs_the_test_file(self):
        self.assertIn("test file your Proof line names", standing_checks.analysis_hand_in_problem(GOOD, ["FINDINGS.md"]))

    def test_a_proof_line_of_code_evidence_needs_no_test_file(self):
        self.assertEqual(standing_checks.analysis_hand_in_problem(GOOD_NO_TESTS, ["FINDINGS.md"]), "")

    def test_the_old_proof_tests_label_is_still_read(self):
        old = GOOD.replace("Proof:", "Proof tests:")
        self.assertEqual(standing_checks.analysis_hand_in_problem(old, ["FINDINGS.md", "tests/test_issue_1.py"]), "")

    def test_missing_lines_are_named(self):
        message = standing_checks.analysis_hand_in_problem("Security: fine here.", ["tests/test_a.py"])
        for label in ("Proof:", "Examined:"):
            self.assertIn(label, message)

    def test_switched_off_gate_is_silent(self):
        with mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "off"}):
            self.assertEqual(standing_checks.analysis_hand_in_problem("", []), "")


class LaneTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def problem(self, no_program, names, files, summary=""):
        state = {"requirement_proposals": {"T": {"no_program": no_program}}}
        with mock.patch.object(board, "_contract_deliverable_names", return_value=names):
            return board._analysis_proof_problem(Path("/x"), state, "T", files, summary)

    def test_unticked_analysis_needs_proof(self):
        self.assertTrue(self.problem(False, [REPORT, TESTS], ["FINDINGS.md"], "Security: ok ok ok.\nCode quality: fine fine."))

    def test_unticked_analysis_with_proof_passes(self):
        self.assertEqual(self.problem(False, [REPORT, TESTS], ["FINDINGS.md", "tests/test_issue_1.py"], GOOD), "")

    def test_document_only_is_not_asked(self):
        self.assertEqual(self.problem(True, [REPORT], ["FINDINGS.md"]), "")

    def test_a_build_naming_its_source_file_is_not_asked(self):
        self.assertEqual(self.problem(False, ["app.py: a working web application", "Automated tests for the application"], ["app.py", "test_smoke.py"], "Security: checked it.\nCode quality: tidy code."), "")
        self.assertFalse(standing_checks.analysis_deliverables_only(["server.js: the API", "Automated tests"]))

    def test_a_build_is_not_asked(self):
        self.assertEqual(self.problem(False, ["README.md: how to run it", "A working app"], ["src/app.py"]), "")


class DirectiveTests(unittest.TestCase):
    def test_builder_and_reviewer_are_told(self):
        root = Path(__file__).resolve().parents[1]
        agent = " ".join((root / "directives/AGENT.md").read_text(encoding="utf-8").split())
        self.assertIn("`Proof:`", agent)
        self.assertIn("`Examined:`", agent)
        self.assertIn("nobody read", agent)
        self.assertIn("refactoring or optimization review", agent)
        self.assertIn("what you suggest doing", agent)
        self.assertIn("proof", (root / "directives/cards/REVIEWER.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
