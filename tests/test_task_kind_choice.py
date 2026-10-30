# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's per-task choice: Development, Code analysis or Spec (2026-10-28). One choice per task; each runs its own lane.

Run:  PYTHONPATH=. python3 -m unittest tests.test_task_kind_choice -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from harness import board, control, review_brief
from tests.test_contract_first_checks import TASK, TEXT, _Fixture


def agent_with_kind(root, kind=None, document_only=False, task=TASK):
    session = control.create(root, "codex_delivery")
    agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
    extra = {} if kind is None else {"task_kind": kind}
    board.record_owner_message(root, agent["id"], f"OWNER DIRECTION - {task}: do the thing.", "direction", [], document_only=document_only, **extra)
    board.begin_task(root, agent["id"], task)
    agent["session_id"] = session["id"]
    return agent


class KindReachesTheTaskTests(_Fixture):
    def kind(self):
        return board.task_kind(board.snapshot(self.root), TASK)

    def reaches(self, kind):
        agent = agent_with_kind(self.root, kind)
        self.assertEqual(self.kind(), kind)
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.assertEqual(board.snapshot(self.root)["requirement_proposals"][TASK]["task_kind"], kind)

    def test_development_reaches_the_task_and_the_proposal(self):
        self.reaches("development")

    def test_analysis_reaches_the_task_and_the_proposal(self):
        self.reaches("analysis")

    def test_spec_reaches_the_task_and_the_proposal(self):
        self.reaches("spec")

    def test_nothing_chosen_is_development_and_the_old_flag_means_spec(self):
        agent_with_kind(self.root, None, task="NOTHING-CHOSEN")
        self.assertEqual(board.task_kind(board.snapshot(self.root), "NOTHING-CHOSEN"), "development")
        agent_with_kind(self.root, None, document_only=True, task="OLD-FLAG")
        self.assertEqual(board.task_kind(board.snapshot(self.root), "OLD-FLAG"), "spec")

    def test_an_unknown_value_is_development(self):
        self.assertEqual(board.normalize_task_kind("banana"), "development")
        self.assertEqual(board.normalize_task_kind("ANALYSIS"), "analysis")

    def test_only_spec_may_use_no_program(self):
        agent = agent_with_kind(self.root, "analysis")
        with self.assertRaises(ValueError):
            board.record_requirement_proposal(self.root, agent["id"], TEXT, no_program=True)


class LanesFollowTheChoiceTests(unittest.TestCase):
    """The lane comes from the owner's choice, not from the words of the contract."""
    REPORT = "FINDINGS.md: a list of the problems found"
    TESTS = "Automated tests that prove each finding"

    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def files_problem(self, kind, names, files):
        state = {"task_kinds": {"T": kind}}
        with mock.patch.object(board, "_contract_deliverable_names", return_value=names), \
                mock.patch.object(board, "_owner_documents_now", return_value=[]), \
                mock.patch.object(board, "_analysis_touched_existing_files", return_value=[]):
            return board._extra_project_files_problem(Path("/x"), state, "T", files, Path("/x"))

    def test_development_is_never_limited_even_when_it_builds_a_report_feature(self):
        self.assertEqual(self.files_problem("development", [self.REPORT, self.TESTS], ["FINDINGS.md", "src/app.py", "tests/test_x.py"]), "")

    def test_analysis_is_limited_to_the_report_and_new_tests_whatever_the_contract_says(self):
        message = self.files_problem("analysis", ["A working dashboard"], ["FINDINGS.md", "tests/test_x.py", "src/app.py"])
        self.assertIn("src/app.py", message)
        self.assertNotIn("test_x", message)

    def test_spec_allows_only_the_document(self):
        self.assertIn("tests/test_x.py", self.files_problem("spec", [self.REPORT], ["FINDINGS.md", "tests/test_x.py"]))

    def test_the_proof_gate_applies_to_analysis_only(self):
        with mock.patch.object(board, "_contract_deliverable_names", return_value=["anything"]):
            run = lambda kind: board._analysis_proof_problem(Path("/x"), {"task_kinds": {"T": kind}}, "T", ["FINDINGS.md"], "Security: ok ok ok.\nCode quality: fine fine.")
            self.assertTrue(run("analysis"))
            self.assertEqual(run("development"), "")
            self.assertEqual(run("spec"), "")


class BriefTests(unittest.TestCase):
    def test_the_reviewer_brief_names_the_kind_and_its_rule(self):
        self.assertIn("EVERY finding", review_brief._process_rule("analysis"))
        self.assertIn("never ask Delivery to fix code", review_brief._process_rule("analysis"))
        self.assertIn("reading it against the confirmed requirements", review_brief._process_rule("spec"))
        self.assertIn("Development, the full process", review_brief._process_rule("development"))


if __name__ == "__main__":
    unittest.main()


class WireTests(unittest.TestCase):
    def test_the_viewer_accepts_the_kind_in_form_data_and_json_and_the_page_sends_it(self):
        source = (Path(__file__).resolve().parents[1] / "harness" / "board_viewer.py").read_text(encoding="utf-8")
        self.assertIn('"document_only", "task_kind",', source)
        self.assertIn('task_kind=str(fields.get("task_kind", ""))', source)
        self.assertIn('task_kind=str(data.get("task_kind", ""))', source)
        self.assertIn("form.append('task_kind',selectedTaskKind())", source)
        self.assertIn("task_kind:pending.taskKind", source)


class VerdictGateByKindTests(unittest.TestCase):
    """Codex round 1: a Spec PASS must not need a findings count, and a clean analysis may say 0 of 0."""
    BASE = "Security check: read it against the requirements.\nNot examined: nothing.\n"
    ANALYSIS_BASE = BASE

    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def problem(self, kind, summary):
        state = {"task_kinds": {"T": kind}}
        with mock.patch.object(board.code_quality, "notes", return_value=[{"note": "checked"}]):
            return board._standing_verdict_problem(Path("/x"), state, {}, "T", summary, True)

    def test_a_spec_pass_needs_no_findings_count(self):
        self.assertEqual(self.problem("spec", self.BASE), "")

    def test_an_analysis_pass_needs_the_count_and_zero_of_zero_is_fine(self):
        self.assertIn("Findings verified independently", self.problem("analysis", self.ANALYSIS_BASE))
        self.assertEqual(self.problem("analysis", self.ANALYSIS_BASE + "Findings verified independently: 0 of 0"), "")
        self.assertEqual(self.problem("analysis", self.ANALYSIS_BASE + "Findings verified independently: 18 of 18"), "")

    def test_a_development_pass_is_asked_for_neither(self):
        self.assertEqual(self.problem("development", "Security check: checked the diff."), "")
