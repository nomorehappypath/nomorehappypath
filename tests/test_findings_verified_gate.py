# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A review of a report must say it checked every finding against the code itself (2026-10-28, Acremuse: two of eighteen were checked).

Run:  PYTHONPATH=. python3 -m unittest tests.test_findings_verified_gate -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from harness import standing_checks

BASE = "Security check: read the report's claims against the code.\nNot examined: nothing.\n"


class VerifiedFindingsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def problem(self, summary):
        return standing_checks.verdict_problem(summary, True, False, True, True)

    def test_a_report_pass_without_the_line_is_refused_and_says_what_to_do(self):
        message = self.problem(BASE)
        self.assertIn("Findings verified independently", message)
        self.assertIn("never proof", message)

    def test_every_finding_checked_passes(self):
        self.assertEqual(self.problem(BASE + "Findings verified independently: 18 of 18"), "")

    def test_fewer_than_all_needs_a_reason(self):
        self.assertIn("a reason", self.problem(BASE + "Findings verified independently: 2 of 18"))
        self.assertEqual(self.problem(BASE + "Findings verified independently: 16 of 18; two need the database, which the runner lacks"), "")

    def test_more_checked_than_the_report_makes_is_refused(self):
        self.assertIn("Findings verified independently", self.problem(BASE + "Findings verified independently: 5 of 3"))

    def test_a_clean_analysis_with_no_findings_can_pass(self):
        self.assertEqual(self.problem(BASE + "Findings verified independently: 0 of 0"), "")

    def test_a_spec_review_is_never_asked_for_findings(self):
        self.assertEqual(standing_checks.verdict_problem(BASE, True, False, True, False), "")

    def test_a_build_review_is_not_asked(self):
        self.assertEqual(standing_checks.verdict_problem("Security check: checked the diff.", True, False, False), "")


if __name__ == "__main__":
    unittest.main()


class AnalysisOnlyAddsTests(unittest.TestCase):
    """An analysis may add a report and new test files; changing or deleting an existing file is refused (owner's order 2026-10-28)."""

    def setUp(self):
        import subprocess
        import tempfile
        from pathlib import Path
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        run = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
        (self.repo / "app.py").write_text("print(1)\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_old.py").write_text("def test(): pass\n")
        run("add", "."); run("commit", "-qm", "seed")
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()
        self.state = {"task_baselines": {"T": {"head": self.base}}, "requirement_proposals": {"T": {"no_program": False}}}

    def touched(self):
        from harness import board
        return board._analysis_touched_existing_files(self.state, "T", self.repo, ["FINDINGS.md"])

    def test_added_report_and_new_tests_are_fine(self):
        (self.repo / "FINDINGS.md").write_text("report\n")
        (self.repo / "tests" / "test_new.py").write_text("def test(): pass\n")
        self.assertEqual(self.touched(), [])

    def test_a_changed_product_file_is_listed(self):
        (self.repo / "app.py").write_text("print(2)\n")
        self.assertEqual(self.touched(), ["app.py"])

    def test_a_changed_existing_test_is_listed_even_though_it_is_a_test(self):
        (self.repo / "tests" / "test_old.py").write_text("def test(): assert True\n")
        self.assertEqual(self.touched(), ["tests/test_old.py"])

    def test_a_deleted_file_is_listed(self):
        (self.repo / "app.py").unlink()
        self.assertEqual(self.touched(), ["app.py"])

    def test_the_refusal_names_the_rule_in_plain_words(self):
        message = standing_checks.report_only_files_problem(["app.py"])
        self.assertIn("never fixes them", message)
        self.assertIn("correct the REPORT", message)

    def test_the_directive_tells_both_roles(self):
        from pathlib import Path
        text = " ".join((Path(__file__).resolve().parents[1] / "directives" / "AGENT.md").read_text(encoding="utf-8").split())
        self.assertIn("Added after independent review", text)
        self.assertIn("never ask Delivery to fix product code", text)
