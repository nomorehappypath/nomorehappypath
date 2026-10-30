# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Run-4 defects of the Acremuse audit (task 2026-10-27_audit-run-defects, round c)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, standing_checks, story
from tests import test_git_model_board

# exactly what the run-4 board recorded for the report (results/nmhp4 task record)
RUN4_NAME = "FINDINGS.md: a list of problems, each stating what is wrong, where it is in the code, and why it is a problem"
RUN4_ENTRY = {
    "deliverable": RUN4_NAME, "document": RUN4_NAME,
    "path": "/Users/x/home/projects/c3f9/data/evidence/uploads/c0cf/1793149511713863000-evidence-7f5ac3a32a75.txt",
    "sha256": "7f5a", "marked_at": "", "attached_at": "", "latest": True,
}


class ReportOnlyGateAllowsTheNamedReportTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def allowed(self, documents=None, deliverables=None):
        return board._report_only_allowed([RUN4_ENTRY] if documents is None else documents, Path("/work/project"), deliverables)

    def extra(self, files, **kw):
        return standing_checks.report_only_extra_files(files, self.allowed(**kw))

    def test_the_report_named_by_the_deliverable_is_allowed_into_the_project(self) -> None:
        self.assertEqual(self.extra(["FINDINGS.md"]), [])

    def test_anything_else_is_still_refused(self) -> None:
        self.assertEqual(self.extra(["FINDINGS.md", "check_findings.py", "notes/FINDINGS.md.bak", "other/REPORT.md"]),
                         ["check_findings.py", "notes/FINDINGS.md.bak", "other/REPORT.md"])
        self.assertIn("temporary evidence folder", standing_checks.report_only_files_problem(self.extra(["check_findings.py"])))

    def test_the_report_is_allowed_even_before_its_owner_copy_is_attached(self) -> None:
        self.assertEqual(self.extra(["FINDINGS.md"], documents=[], deliverables=[RUN4_NAME]), [])

    def test_a_deliverable_text_that_names_no_file_allows_nothing_extra(self) -> None:
        for text in ("a list of problems", "/etc/passwd: x", "../x.md: y", "e.g. something", ""):
            self.assertEqual(standing_checks.report_file_name(text), "", text)
        self.assertEqual(self.extra(["a.md"], documents=[], deliverables=["a list of problems"]), ["a.md"])


class AcceptSaysWhenNothingWasDeliveredTests(unittest.TestCase):
    # borrow the real-board fixture without inheriting (and re-running) its tests
    setUp = test_git_model_board.GitModelBoardIntegrationTests.setUp
    tearDown = test_git_model_board.GitModelBoardIntegrationTests.tearDown
    git = test_git_model_board.GitModelBoardIntegrationTests.git
    certified_candidate = test_git_model_board.GitModelBoardIntegrationTests.certified_candidate

    def test_accept_of_an_unchanged_project_says_nothing_was_delivered(self) -> None:
        with mock.patch.object(board, "route_reintegration") as reintegrate:
            self.nothing_changed_candidate()
            board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        state = board.snapshot(self.root)
        event = [e for e in state["events"] if e["kind"] == "git_acceptance_completed"][-1]
        self.assertFalse(event["advanced"])
        self.assertNotIn("advanced", event["message"])
        self.assertIn("nothing new was delivered", event["message"])
        self.assertIn(self.base_commit[:12], event["message"])
        reintegrate.assert_not_called()

    def test_accept_of_a_real_change_still_says_main_advanced(self) -> None:
        self.certified_candidate()
        board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        event = [e for e in board.snapshot(self.root)["events"] if e["kind"] == "git_acceptance_completed"][-1]
        self.assertTrue(event["advanced"])
        self.assertIn("advanced local main", event["message"])

    def nothing_changed_candidate(self) -> None:
        tree = self.git("rev-parse", "HEAD^{tree}").strip()
        with board.locked_state(self.root) as state:
            request = {
                "id": "review-GIT-MODEL-final-01", "task": "GIT-MODEL", "cycle": 1, "stage": board.INDEPENDENT_REVIEW,
                "phase": "final_acceptance", "subtask": "", "chunk": "final", "status": "passed", "requested_at": board.now(),
                "developer_id": self.delivery["id"], "claimed_by": "qa-fixture", "review_wait_started_at": board.now(),
                "structure_revision": state["delivery_plans"]["GIT-MODEL"]["structure_revision"],
                "reviewed_commit": self.base_commit, "reviewed_tree_hash": tree, "reviewed_files": [],
            }
            state["qa_requests"][request["id"]] = request
            broker = board._broker_for_state(self.root, state, "GIT-MODEL")
            mirror = broker.create_review_ref(request, review_number=1, board_mutation=lambda record: request.update({
                "mirror_ref": record["ref"], "mirror_commit": record["commit"], "mirror_tree_hash": record["tree"],
                "mirror_transaction_id": record["transaction_id"]}))
            request["mirror_ref"] = mirror["ref"]
            state.setdefault("releases", {})["GIT-MODEL"] = {
                "task": "GIT-MODEL", "status": "VISUAL_TEST_REQUIRED", "head_commit": self.base_commit, "git_broker_governed": True,
                "acceptance_base_commit": self.base_commit, "acceptance_manifest": [], "cto_id": "cto-fixture", "recorded_at": board.now()}


class IdleCardsReadTheMeasuredCostTests(unittest.TestCase):
    COST = {"available": True, "overhead": {"roles": {"cto": {"usd": 0.99, "tokens": 5_000_000}}},
            "tasks": {"old": {"last_at": "2026-10-27T01:00:00+00:00", "roles": {"delivery": {"usd": 1.0, "tokens": 100_000}}},
                      "audit": {"last_at": "2026-10-28T01:00:00+00:00", "roles": {
                          "delivery": {"usd": 7.30, "tokens": 30_000_000}, "reviewer": {"usd": 2.94, "tokens": 10_000_000}}}}}

    def test_with_no_task_on_the_page_the_idle_builder_and_reviewer_show_their_latest_task_figure(self) -> None:
        builder = story._role_cost(self.COST, "", "builder")
        reviewer = story._role_cost(self.COST, "", "reviewer")
        self.assertIn("$7.30", builder)
        self.assertIn("$2.94", reviewer)
        self.assertTrue(builder.endswith("(last task)"))
        self.assertNotIn("not counted", builder + reviewer)

    def test_a_task_on_the_page_still_shows_its_own_figure_and_an_unmeasured_role_still_says_so(self) -> None:
        self.assertEqual(story._role_cost(self.COST, "old", "builder"), "$1.00 · 100K tokens")
        self.assertEqual(story._role_cost({**self.COST, "tasks": {}}, "", "builder"), "Cost: not counted yet")


if __name__ == "__main__":
    unittest.main()
