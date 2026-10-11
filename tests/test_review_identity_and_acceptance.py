# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A pause and resume must not throw away a passed review, and Accept must not claim work reached main when it did not."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board


RELEASE = {"status": "VISUAL_TEST_REQUIRED", "head_commit": "abc", "cto_id": "cto-1", "recorded_at": "2026-10-09T00:00:00+00:00"}


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *arguments],
                   check=True, capture_output=True, text=True)


class ReviewIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        git(self.root, "init", "-q")
        (self.root / "notes.txt").write_text("notes\n")
        git(self.root, "add", "notes.txt")
        git(self.root, "commit", "-q", "-m", "delivery")

    def test_the_apps_own_files_under_harness_do_not_change_the_reviewed_identity(self):
        before = board._git_review_artifact(self.root)
        control = self.root / ".harness" / "control"
        control.mkdir(parents=True)
        # What macOS writes on every launch, and what resume writes again.
        (control / "agent-sandbox-d0ce4a5916ab8b14.sb").write_text("(version 1)\n")
        (control / "read-guard-0123456789abcdef.sb").write_text("(version 1)\n")
        after = board._git_review_artifact(self.root)
        self.assertTrue(before["immutable_clean"] and after["immutable_clean"])
        self.assertEqual(after["working_tree_digest"], before["working_tree_digest"])
        self.assertEqual(after["working_tree_files"], [])

    def test_a_real_change_to_the_project_still_changes_it(self):
        before = board._git_review_artifact(self.root)
        (self.root / "extra.txt").write_text("not reviewed\n")
        after = board._git_review_artifact(self.root)
        self.assertNotEqual(after["working_tree_digest"], before["working_tree_digest"])
        self.assertFalse(after["immutable_clean"])
        (self.root / "extra.txt").unlink()
        (self.root / "notes.txt").write_text("changed\n")
        modified = board._git_review_artifact(self.root)
        self.assertNotEqual(modified["working_tree_digest"], before["working_tree_digest"])

    def test_a_file_named_like_harness_but_in_the_project_is_still_counted(self):
        before = board._git_review_artifact(self.root)
        (self.root / ".harnessrc").write_text("x\n")
        (self.root / "docs").mkdir()
        (self.root / "docs" / ".harness").write_text("x\n")
        after = board._git_review_artifact(self.root)
        self.assertNotEqual(after["working_tree_digest"], before["working_tree_digest"])


class AcceptWithoutAPassingReviewTests(unittest.TestCase):
    def test_accept_is_refused_for_a_governed_task_whose_passed_review_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with board.locked_state(root) as state:
                state.setdefault("releases", {})["T"] = dict(RELEASE)
                state.setdefault("task_repositories", {})["T"] = {"repository": "x"}
                state.setdefault("qa_requests", {})["review-T-final-01"] = {
                    "id": "review-T-final-01", "task": "T", "phase": "final_acceptance", "status": "failed",
                    "integrity_invalidated": True, "requested_at": "2026-10-09T00:00:00+00:00",
                    "subtask": "", "chunk": "", "cycle": 1, "developer_id": "d", "claimed_by": None,
                    "review_wait_started_at": "2026-10-09T00:00:00+00:00",
                }
            with self.assertRaisesRegex(ValueError, "independent review was reset"):
                board.record_release_decision(root, "T", "accepted")
            self.assertNotIn("T", board.snapshot(root).get("release_decisions", {}))

    def test_not_accepted_and_ungoverned_tasks_are_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with board.locked_state(root) as state:
                state.setdefault("releases", {})["T"] = dict(RELEASE)
                state.setdefault("task_repositories", {})["T"] = {"repository": "x"}
                state["releases"]["U"] = dict(RELEASE)
            declined = board.record_release_decision(root, "T", "not_accepted", "wrong colour")
            self.assertEqual(declined["decision"], "not_accepted")
            accepted = board.record_release_decision(root, "U", "accepted")
            self.assertEqual(accepted["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
