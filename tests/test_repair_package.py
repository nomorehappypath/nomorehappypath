# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, control, repair_package
from tests.requirements_support import agreed_requirements


class RepairPackageTests(unittest.TestCase):
    def request(self):
        return {
            "id": "review-1", "phase": "final_acceptance", "subtask": "", "chunk": "final",
            "reviewed_commit": "c" * 40, "reviewed_tree_hash": "t" * 40,
            "reviewed_files": ["ui.py", "security.py"],
        }

    def test_strictest_member_forces_full_scope(self):
        package = repair_package.build("TASK", self.request(), "Several failures", [
            {"id": "UX", "category": "cosmetic", "summary": "Spacing breaks on a phone.", "affected_paths": ["ui.py"], "surface": "phone UI", "regression_check": "Verify the phone layout."},
            {"id": "AUTH", "category": "security", "summary": "Authorization permits a cross-task read.", "affected_paths": ["security.py"], "surface": "authorization", "regression_check": "Reject the forged task identity."},
        ])
        self.assertEqual(package["required_test_scope"], "full")

    def test_every_member_must_be_resolved(self):
        package = repair_package.build("TASK", self.request(), "Several failures", [
            {"id": "A", "category": "behavior", "summary": "First behavior is incorrect.", "affected_paths": ["ui.py"], "surface": "UI", "regression_check": "Verify the first behavior."},
            {"id": "B", "category": "compatibility", "summary": "Second behavior is incompatible.", "affected_paths": ["security.py"], "surface": "API", "regression_check": "Verify the second behavior."},
        ])
        with self.assertRaisesRegex(ValueError, "not repaired yet"):
            repair_package.resolve(package, [
                {"id": "A", "resolution": "Corrected the first behavior.", "regression_check": "The focused check now passes."},
            ], "delivery-1")
        resolved = repair_package.resolve(package, [
            {"id": "A", "resolution": "Corrected the first behavior.", "regression_check": "The focused check now passes."},
            {"id": "B", "resolution": "Restored compatible behavior.", "regression_check": "The compatibility check now passes."},
        ], "delivery-1")
        self.assertEqual(resolved["status"], "ready_for_review")

    def test_split_reapplies_depth_and_cannot_drop_or_duplicate_members(self):
        package = repair_package.build("TASK", self.request(), "Several failures", [
            {"id": "UX", "category": "cosmetic", "summary": "Spacing breaks on a phone.", "affected_paths": ["ui.py"], "surface": "phone UI", "regression_check": "Verify the phone layout."},
            {"id": "AUTH", "category": "security", "summary": "Authorization permits a cross-task read.", "affected_paths": ["security.py"], "surface": "authorization", "regression_check": "Reject the forged task identity."},
        ])
        with self.assertRaisesRegex(ValueError, "exactly once"):
            repair_package.split(package, [["UX"], ["UX"]], "qa-1", "Different boundaries")
        children = repair_package.split(
            package, [["UX"], ["AUTH"]], "qa-1", "Different ownership and security boundaries",
        )
        self.assertEqual(children[0]["required_test_scope"], "affected")
        self.assertEqual(children[1]["required_test_scope"], "full")

    def test_unclassified_legacy_failure_fails_safe_to_full_scope(self):
        package = repair_package.build("TASK", self.request(), "An older reviewer reported a blocker.")
        self.assertEqual(package["required_test_scope"], "full")
        self.assertFalse(package["requires_explicit_resolution"])


class RepairPackageBoardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\nclass Smoke(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Repair the grouped findings", allow_split=True)
        board.begin_task(self.root, self.delivery["id"], "REPAIR-TASK")
        agreed_requirements(
            self.root, self.delivery["id"], "Repair every grouped finding and preserve review depth.",
        )
        contract.create_contract(
            self.root, "REPAIR-TASK", "Repair the grouped findings", ["delivery"],
        )
        board.define_delivery_plan(self.root, self.delivery["id"], "chunked", "One repair scope")
        board.declare_chunks(self.root, self.delivery["id"], [("core", "repair scope")])
        self.reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        self.ledger = self.root / "ledger.md"
        self.ledger.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            "| S-001 | The grouped repair closes every recorded failure. | `python3 -m unittest test_smoke` | Every repair holds. | PASS: every repair held. | PASS |\n",
            encoding="utf-8",
        )
        source = {
            "id": "failed-review", "phase": "chunk", "subtask": "", "chunk": "core",
            "reviewed_files": ["security.py", "ui.py"],
        }
        self.package = repair_package.build("REPAIR-TASK", source, "Two failures", [
            {"id": "AUTH", "category": "security", "summary": "Cross-task authorization is not rejected.", "affected_paths": ["security.py"], "surface": "authorization", "regression_check": "Reject a forged task identity."},
            {"id": "UI", "category": "cosmetic", "summary": "The phone layout clips its final row.", "affected_paths": ["ui.py"], "surface": "phone UI", "regression_check": "Render the phone-sized status view."},
        ])
        self.package["source_reviewer_id"] = self.reviewer["id"]
        repair_package.refresh_digest(self.package)
        with board.locked_state(self.root) as state:
            state["repair_packages"][self.package["id"]] = self.package

    def test_explicit_members_close_before_execution_and_strictest_depth_is_binding(self):
        with patch.object(board, "_execute_internal_qa", side_effect=AssertionError("must not execute")) as execute:
            with self.assertRaisesRegex(ValueError, "every explicit.*member"):
                board.request_review(
                    self.root, self.delivery["id"], str(self.ledger), "review repairs",
                    chunk="core", changes="repaired both failures",
                    test_command="python3 -m unittest test_smoke",
                    repair_package_id=self.package["id"],
                )
        execute.assert_not_called()
        board.resolve_repair_package(self.root, self.delivery["id"], self.package["id"], [
            {"id": "AUTH", "resolution": "Bound reads to the authenticated task.", "regression_check": "The forged task request is rejected."},
            {"id": "UI", "resolution": "Made the phone list scroll within its viewport.", "regression_check": "The phone-sized render keeps all controls visible."},
        ])

    def resolve_and_request(self):
        board.resolve_repair_package(self.root, self.delivery["id"], self.package["id"], [
            {"id": "AUTH", "resolution": "Bound reads to the authenticated task.", "regression_check": "The forged task request is rejected."},
            {"id": "UI", "resolution": "Made the phone list scroll within its viewport.", "regression_check": "The phone-sized render keeps all controls visible."},
        ])
        return board.request_review(
            self.root, self.delivery["id"], str(self.ledger), "review repairs", chunk="core", changes="repaired both failures",
            test_command="python3 -m unittest test_smoke", repair_package_id=self.package["id"],
        )

    def request_again(self):
        return board.request_review(
            self.root, self.delivery["id"], str(self.ledger), "review repairs again", chunk="core", changes="same repairs",
            test_command="python3 -m unittest test_smoke", repair_package_id=self.package["id"],
        )

    def test_a_withdrawn_review_frees_its_repair_for_the_replacement_review(self):
        # Owner 2026-11-27 (test567): the review was withdrawn, the repair stayed "under review", every request-review was refused.
        first = self.resolve_and_request()
        self.assertEqual(board.snapshot(self.root)["repair_packages"][self.package["id"]]["status"], "under_review")
        with board.locked_state(self.root) as state:
            board._withdraw_open_request(state, state["qa_requests"][first["id"]], "its checks could not run in the clean review copy")
        self.assertEqual(board.snapshot(self.root)["repair_packages"][self.package["id"]]["status"], "ready_for_review")
        second = self.request_again()
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(board.snapshot(self.root)["repair_packages"][self.package["id"]]["review_request_id"], second["id"])

    def test_a_board_already_stuck_that_way_is_freed_by_the_next_request(self):
        first = self.resolve_and_request()
        with board.locked_state(self.root) as state:  # the state a board saved before the fix holds
            state["qa_requests"][first["id"]].update({"status": "withdrawn", "withdrawn_reason": "old board"})
        second = self.request_again()
        self.assertEqual(board.snapshot(self.root)["repair_packages"][self.package["id"]]["review_request_id"], second["id"])

    def test_a_new_request_replaces_a_review_still_open(self):
        # Owner 2026-11-27 rule 1: the builder's request is never refused because an older review of the same scope is open
        first = self.resolve_and_request()
        second = self.request_again()
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][first["id"]]["status"], "withdrawn")
        self.assertEqual(state["repair_packages"][self.package["id"]]["review_request_id"], second["id"])

    def test_only_source_reviewer_can_split_and_each_child_recomputes_depth(self):
        other = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "source Reviewer"):
            board.split_repair_package(
                self.root, other["id"], self.package["id"], [["AUTH"], ["UI"]],
                "Separate security and visual ownership",
            )
        children = board.split_repair_package(
            self.root, self.reviewer["id"], self.package["id"], [["AUTH"], ["UI"]],
            "Separate security and visual ownership",
        )
        self.assertEqual([child["required_test_scope"] for child in children], ["full", "affected"])


if __name__ == "__main__":
    unittest.main()
