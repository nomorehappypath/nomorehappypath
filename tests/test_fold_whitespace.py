# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Defect #10 (2026-09-26): the reviewed bytes fold as reviewed.

The broker fold used ``git apply --whitespace=error``, so a certified subtask
whose ADDED lines carried Markdown's two-space hard line break, or blank lines
at end of file, was refused at the last step with ``adds whitespace errors``
after passing every semantic check. The fold already verifies the folded tree
byte-for-byte against the certified manifest, so the lint could only refuse
bytes the reviewer accepted.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from harness import board
from tests.test_subtask_pipelining import SubtaskPipeliningTests

# The shape of roles/strategic-narrative.md at studio candidate 71927cb2: a
# heading line ending in two spaces (a hard break), then trailing blank lines.
HARD_BREAK_ROLE = (
    "# Strategic Narrative Architect\n"
    "\n"
    "**Who you are:**  \n"
    "You shape the story a brand tells about where its market is going.  \n"
    "\n"
    "**What you deliver:**\n"
    "- a narrative platform\n"
    "\n"
    "\n"
)


class FoldWhitespaceTests(SubtaskPipeliningTests):
    def commit_bytes(self, subtask: str, path: str, content: str) -> dict:
        destination = self.workspace(subtask) / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content.encode("utf-8"))
        return board.broker_stage_commit(
            self.root, self.delivery["id"], [path], f"implement {subtask}", subtask=subtask,
        )

    def test_a_reviewed_markdown_hard_break_and_eof_blank_lines_fold_byte_exact(self):
        self.declare()
        reviewer = self.reviewer()
        started = board.start_subtask(self.root, self.delivery["id"], "alpha")
        candidate = self.commit_bytes("alpha", "alpha/strategic-narrative.md", HARD_BREAK_ROLE)
        # Git's own lint agrees these added lines are "whitespace errors": that
        # is the condition the fold used to refuse.
        check = subprocess.run(
            ["git", "-C", str(self.workspace("alpha")), "diff", "--check", started["base_commit"], candidate["commit"]],
            capture_output=True, text=True,
        )
        self.assertNotEqual(check.returncode, 0, "the fixture must carry trailing-whitespace lines")
        self.assertIn("trailing whitespace", check.stdout)

        passed = self.pass_request(reviewer, self.request("alpha"), "alpha")

        task_workspace = Path(board.snapshot(self.root)["task_workspaces"]["PIPELINE"])
        folded = (task_workspace / "alpha" / "strategic-narrative.md").read_bytes()
        self.assertEqual(folded, HARD_BREAK_ROLE.encode("utf-8"), "the folded bytes must equal the certified bytes")
        self.assertTrue(passed["integrated_commit"])
        self.assertEqual(self._git("-C", str(task_workspace), "status", "--porcelain"), "")
        parents = self._git("-C", str(task_workspace), "show", "-s", "--format=%P", passed["integrated_commit"]).split()
        self.assertEqual(parents, [started["base_commit"], candidate["commit"]])


if __name__ == "__main__":
    unittest.main()
