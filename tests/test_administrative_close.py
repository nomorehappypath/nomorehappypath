# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch 2 item A2: close a task the owner shipped by hand, through a governed operation.

2026-10-01: the owner merged headless-browser-maya-mac into main himself
(0c48126 contains the task head d88fa78). Owner Accept needs an unchanged main
and a final review, so the task could never leave the open list.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import board, board_surface, control, project_manager
from tests import test_git_model_board

_fixture = vars(test_git_model_board.GitModelBoardIntegrationTests)  # helpers only
REASON = "Owner shipped it by hand: main has the task head plus the Help commit, pushed; Maya server on 2026.10.01.1519."


class AdministrativeCloseTests(unittest.TestCase):
    setUp = _fixture["setUp"]
    tearDown = _fixture["tearDown"]
    git = _fixture["git"]

    def shipped_by_hand(self):
        """Task work committed on its branch, then merged into main outside the board, plus a Help commit."""
        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("shipped\n", encoding="utf-8")
        committed = board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "task work")
        self.git("merge", "--ff-only", committed["commit"])
        (self.root / "help.md").write_text("help\n", encoding="utf-8")
        self.git("add", "help.md")
        self.git("commit", "-m", "Help commit")
        return committed["commit"], self.git("rev-parse", "HEAD").strip()

    def cto(self):
        session = control.create(self.root, "claude_cto")
        return board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])

    def open_count(self):
        directory = Path(tempfile.mkdtemp(dir=self.root.parent))
        (directory / "data" / "board").mkdir(parents=True)
        (directory / "data" / "board" / "state.json").write_text(json.dumps(board.snapshot(self.root)), encoding="utf-8")
        row = project_manager.derive_status({"data_root": str(directory / "data"), "code_root": str(directory), "workspace_root": str(directory / "ws")})
        return row["task_counts"]

    def test_cto_closes_a_task_shipped_by_hand_and_it_leaves_the_open_list(self):
        task_head, shipped = self.shipped_by_hand()
        with self.assertRaisesRegex(ValueError, "owner responses are available only for a released task"):
            board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        before = self.open_count()
        cto = self.cto()["id"]
        closed = board.close_released_administratively(self.root, cto, "GIT-MODEL", shipped[:7], REASON)
        self.assertEqual((closed["released_commit"], closed["task_head"], closed["by_role"]), (shipped, task_head, "cto"))
        state = board.snapshot(self.root)
        self.assertEqual(state["releases"]["GIT-MODEL"]["status"], board.RELEASE_ACCEPTED)
        self.assertEqual(state["releases"]["GIT-MODEL"]["head_commit"], shipped)
        self.assertEqual(state["release_decisions"]["GIT-MODEL"]["decision"], "accepted")
        self.assertTrue(state["release_decisions"]["GIT-MODEL"]["administrative"])
        self.assertEqual(state["git_acceptances"]["GIT-MODEL"]["commit"], shipped)
        self.assertTrue(board._task_finished(state, "GIT-MODEL"))
        event = state["events"][-1]
        self.assertEqual(event["kind"], "task_closed_administratively")
        self.assertIn(f"The CTO recorded GIT-MODEL as released and accepted at {shipped[:12]}", event["message"])
        after = self.open_count()
        self.assertEqual(before["open"], 1)
        self.assertEqual(after["open"], 0, "the task left the open list")
        with self.assertRaisesRegex(ValueError, "already released or accepted"):
            board.close_released_administratively(self.root, cto, "GIT-MODEL", shipped, REASON)

    def test_the_owner_can_close_it_at_the_local_cli(self):
        _, shipped = self.shipped_by_hand()
        result = subprocess.run(
            [sys.executable, str(Path(board.__file__)), "--root", str(self.root), "close-released",
             "--task", "GIT-MODEL", "--commit", shipped, "--reason", REASON],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(board.snapshot(self.root)["releases"]["GIT-MODEL"]["administrative_close"]["by_role"], "owner")

    def test_unproven_closes_are_refused_and_change_nothing(self):
        task_head, shipped = self.shipped_by_hand()
        cto = self.cto()["id"]

        def refused(pattern, *, agent=cto, task="GIT-MODEL", commit=shipped, reason=REASON):
            before = json.dumps(board.snapshot(self.root).get("releases", {}), sort_keys=True)
            with self.assertRaisesRegex(ValueError, pattern):
                board.close_released_administratively(self.root, agent, task, commit, reason)
            self.assertEqual(json.dumps(board.snapshot(self.root).get("releases", {}), sort_keys=True), before)

        refused("plain-language reason", reason="")
        refused("commit id", commit="not-a-sha")
        refused("does not exist", commit="0" * 40)
        # A commit off main: a side branch carrying the task head.
        self.git("checkout", "-q", "-b", "side", task_head)
        (self.root / "side.txt").write_text("side\n", encoding="utf-8")
        self.git("add", "side.txt"); self.git("commit", "-m", "side")
        side = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "main")
        refused("is not on main", commit=side)
        # A main commit that predates the task's work.
        refused("does not contain the task's own work", commit=self.base_commit)
        refused("only the CTO or the owner", agent=self.delivery["id"])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        refused("only the CTO or the owner", agent=reviewer["id"])
        refused("no governed repository", task="NO-SUCH-TASK")
        with board.locked_state(self.root) as state:
            state["task_repositories"]["GIT-MODEL"] = str(self.root / "moved-away")
        refused("not reachable")

    def test_the_command_surface_allows_only_the_cto(self):
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["close-released"], frozenset({"cto"}))
        self.assertIn("close-released", board_surface.AGENT_ARGUMENT_OPERATIONS, "an agent session always acts as itself")


if __name__ == "__main__":
    unittest.main()
