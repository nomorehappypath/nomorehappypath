# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item D: a good commit with other files still changed is not a hold.

Studio logged "RecoveryHoldError: post-commit task worktree is not clean" 14
times: the commit was already made, the board rolled back, and Delivery was
told to run a CTO-only recover-git. The commit now stands and the board says in
one line which files are still changed and not in it.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, git_broker
from tests import test_git_model_board

_fixture = vars(test_git_model_board.GitModelBoardIntegrationTests)  # helpers only


class PostCommitLeftoverTests(unittest.TestCase):
    setUp = _fixture["setUp"]
    tearDown = _fixture["tearDown"]
    git = _fixture["git"]

    def workspace(self) -> Path:
        return Path(self.begun["task_workspace"])

    def test_other_changed_files_leave_the_commit_standing_with_one_line(self):
        workspace = self.workspace()
        (workspace / "product.txt").write_text("changed\n", encoding="utf-8")
        (workspace / "scratch.txt").write_text("not for this commit\n", encoding="utf-8")
        (workspace / "docs").mkdir()
        (workspace / "docs" / "next.md").write_text("next commit\n", encoding="utf-8")
        committed = board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "the product change")
        self.assertEqual(committed["manifest"], ["product.txt"])
        self.assertEqual(committed["left_uncommitted"], ["docs/next.md", "scratch.txt"])
        self.assertEqual(
            committed["note"],
            "Committed 1 file. Still changed and not in this commit: docs/next.md, scratch.txt — commit them or remove them.",
        )
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=workspace).strip(), committed["commit"])
        state = board.snapshot(self.root)
        event = [item for item in state["events"] if item["kind"] == "broker_commit_created"][-1]
        self.assertEqual(event["commit"], committed["commit"])
        self.assertEqual(event["message"], committed["note"])
        self.assertFalse(state["agents"][self.delivery["id"]].get("broker_refusal"), "no refusal recorded")
        self.assertEqual(git_broker.GitBroker(
            git_broker.context_for_repository(board.project_context(self.root), self.root), state_loader=lambda: state,
        ).transaction_records(), [], "no hold or open transaction")
        # The leftovers commit normally next time.
        again = board.broker_stage_commit(self.root, self.delivery["id"], ["docs/next.md", "scratch.txt"], "the rest")
        self.assertEqual(again["left_uncommitted"], [])
        self.assertNotIn("note", again)

    def test_a_committed_path_rewritten_during_the_commit_is_still_a_hold(self):
        workspace = self.workspace()
        (workspace / "product.txt").write_text("changed\n", encoding="utf-8")
        original = git_broker.GitBroker._run_git

        def rewriting(broker, arguments, **options):
            result = original(broker, arguments, **options)
            if "commit" in arguments and "--no-gpg-sign" in arguments:
                (workspace / "product.txt").write_text("rewritten behind the commit\n", encoding="utf-8")
            return result

        with patch.object(git_broker.GitBroker, "_run_git", rewriting), \
                self.assertRaisesRegex(git_broker.RecoveryHoldError, "changed committed paths: product.txt"):
            board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "the product change")

    def test_a_clean_commit_is_unchanged(self):
        (self.workspace() / "product.txt").write_text("changed\n", encoding="utf-8")
        committed = board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "the product change")
        self.assertEqual(committed["left_uncommitted"], [])
        self.assertNotIn("note", committed)
        event = [item for item in board.snapshot(self.root)["events"] if item["kind"] == "broker_commit_created"][-1]
        self.assertEqual(event["message"], "trusted Git broker committed the explicit reviewed manifest")

    def test_porcelain_paths_cover_renames_and_spaces(self):
        output = "R  new name.txt\0old name.txt\0?? docs/a b.md\0 M src/x.py\0"
        self.assertEqual(git_broker._porcelain_paths(output), ["docs/a b.md", "new name.txt", "old name.txt", "src/x.py"])


if __name__ == "__main__":
    unittest.main()
