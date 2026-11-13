# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A pending subtask whose branch was refreshed earlier must still start (owner 2026-11-12, "must not leave start-subtask refusing it forever").

Seen on the clone project's build-skill-library: a pending branch was moved to a newer task head while the board's record still named
the old base (the move was made, the record rolled back), so every later start-subtask answered "pending subtask branch moved
outside its recorded base"; two stages had to be thrown away and rebuilt."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from harness import board
from tests import test_subtask_pipelining as fixture


class StaleSubtaskBase(unittest.TestCase):
    setUp = fixture.SubtaskPipeliningTests.setUp          # the module, not the class, is imported so its own tests are not run twice
    _git = fixture.SubtaskPipeliningTests._git
    declare = fixture.SubtaskPipeliningTests.declare

    def git_in(self, directory: Path, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=directory, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def move_task_head(self, name: str) -> str:
        task_workspace = Path(board.snapshot(self.root)["task_workspaces"]["PIPELINE"])
        (task_workspace / name).write_text(f"{name}\n", encoding="utf-8")
        self.git_in(task_workspace, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "add", name)
        self.git_in(task_workspace, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", name)
        return self.git_in(task_workspace, "rev-parse", "HEAD")

    def beta(self) -> tuple[Path, str]:
        state = board.snapshot(self.root)
        return Path(state["subtask_workspaces"]["PIPELINE"]["beta"]), state["subtask_branches"]["PIPELINE"]["beta"]["base_commit"]

    def test_a_branch_refreshed_earlier_and_left_behind_by_a_newer_head_still_starts(self):
        self.declare()
        beta_workspace, recorded_base = self.beta()
        refreshed = self.move_task_head("note-one.txt")
        self.git_in(beta_workspace, "reset", "-q", "--hard", refreshed)         # the refresh happened; the board's record did not follow
        self.assertEqual(board.snapshot(self.root)["subtask_branches"]["PIPELINE"]["beta"]["base_commit"], recorded_base)
        newest = self.move_task_head("note-two.txt")                             # and the task head moved on again
        board.start_subtask(self.root, self.delivery["id"], "beta")               # was: "pending subtask branch moved outside its recorded base"
        state = board.snapshot(self.root)
        self.assertEqual(state["subtask_branches"]["PIPELINE"]["beta"]["base_commit"], newest)
        self.assertEqual(self.git_in(beta_workspace, "rev-parse", "HEAD"), newest)

    def test_a_branch_with_a_commit_of_its_own_is_still_refused(self):
        self.declare()
        beta_workspace, _ = self.beta()
        (beta_workspace / "own.txt").write_text("work of its own\n", encoding="utf-8")
        flags = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
        self.git_in(beta_workspace, *flags, "add", "own.txt")
        self.git_in(beta_workspace, *flags, "commit", "-q", "-m", "own work")
        own = self.git_in(beta_workspace, "rev-parse", "HEAD")
        self.move_task_head("note-one.txt")
        with self.assertRaisesRegex(ValueError, "moved outside its recorded base"):
            board.start_subtask(self.root, self.delivery["id"], "beta")
        self.assertEqual(self.git_in(beta_workspace, "rev-parse", "HEAD"), own, "its own commit is untouched")
