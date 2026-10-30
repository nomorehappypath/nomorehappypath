# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The 2026-08-21 overnight wedge can no longer happen.

A scaffold project whose folder was not yet a Git repository began its task
unbound; the only repair was an operation no agent could call; the watchdog
spammed recoveries for four hours instead of surfacing the diagnosis.

Run: PYTHONPATH=. python3 -m unittest tests.test_scaffold_wedge -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from harness import board, board_surface, control, project_manager
from harness.project_context import context_from_roots


def _old(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class ScaffoldFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.code_root = base / "product"
        self.code_root.mkdir()
        self.data_root = base / "data"
        self.workspace_root = base / "workspaces"
        self.context = context_from_roots(self.code_root, self.data_root, self.workspace_root)

    def begin_task(self, task="SCAFFOLD-TASK"):
        session = control.create(self.context, "codex_delivery")
        agent = board.register(
            self.context, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.context, session["id"], "Build the product.")
        return agent, board.begin_task(self.context, agent["id"], task)


class RepositoryInitializationTests(ScaffoldFixture):
    def test_begin_task_initializes_the_empty_scaffold_and_binds_the_broker(self):
        agent, event = self.begin_task()
        state = board.snapshot(self.context)
        self.assertEqual(
            Path(state["task_repositories"]["SCAFFOLD-TASK"]).resolve(),
            self.code_root.resolve(),
        )
        workspace = Path(state["task_workspaces"]["SCAFFOLD-TASK"])
        self.assertTrue(workspace.is_dir())
        self.assertTrue(
            str(workspace.resolve()).startswith(str(self.workspace_root.resolve())),
            f"worktree {workspace} must live inside the registered workspace root",
        )
        self.assertIn("SCAFFOLD-TASK", state.get("task_branches", {}))
        self.assertTrue((self.code_root / ".git").exists())

    def test_existing_repository_is_used_not_reinitialized(self):
        from harness import git_process
        git_process.run(["git", "init", "-q", "-b", "main"], cwd=self.code_root, capture_output=True)
        (self.code_root / "seed.txt").write_text("seed")
        git_process.run(["git", "add", "seed.txt"], cwd=self.code_root, capture_output=True)
        git_process.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
            cwd=self.code_root, capture_output=True, text=True,
        )
        self.begin_task()
        state = board.snapshot(self.context)
        workspace = Path(state["task_workspaces"]["SCAFFOLD-TASK"])
        self.assertTrue((workspace / "seed.txt").is_file(), "existing history must carry into the worktree")

    def test_bind_repository_without_a_path_binds_the_project_folder(self):
        session = control.create(self.context, "codex_delivery")
        agent = board.register(
            self.context, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.context, session["id"], "Build it.")
        # Simulate the legacy stranded state: task begun with no repository.
        with board.locked_state(self.context) as state:
            state["agents"][agent["id"]]["task"] = "LEGACY-TASK"
            state["agents"][agent["id"]]["status"] = "task_defined"
            state.setdefault("task_owner_directions", {})["LEGACY-TASK"] = "Build it."
        event = board.bind_task_repository(self.context, agent["id"], "")
        state = board.snapshot(self.context)
        self.assertEqual(
            Path(state["task_repositories"]["LEGACY-TASK"]).resolve(),
            self.code_root.resolve(),
        )
        self.assertTrue(
            str(Path(state["task_workspaces"]["LEGACY-TASK"]).resolve()).startswith(
                str(self.workspace_root.resolve()),
            ),
        )

    def test_surface_lets_delivery_bind_but_never_choose_the_path(self):
        self.assertIn("bind-repository", board_surface.DELIVERY_OPERATIONS)
        self.assertIn("--repo", board_surface.PROTECTED_ARGUMENTS)


class RefusalHoldTests(ScaffoldFixture):
    def gateway(self):
        authority = board_surface.SessionTokenAuthority(self.context)
        return board_surface.CommandGateway(self.context, authority)

    def test_repeated_identical_refusals_log_one_defect_and_hold_nothing(self):
        # owner's order 2026-10-28: the third identical refusal is a logged defect for the CTO, never a hold on the task
        agent, _ = self.begin_task()
        state = board.snapshot(self.context)
        session_id = state["agents"][agent["id"]]["session_id"]
        gateway = self.gateway()
        identity = SimpleNamespace(session_id=session_id)
        for _ in range(board_surface.CommandGateway.REFUSAL_HOLD_THRESHOLD + 2):
            gateway._track_refusal(identity, "start-subtask", "board-derived worktree is outside workspace_root")
        after = board.snapshot(self.context)
        self.assertEqual(after.get("control_plane_holds", {}), {})
        defects = after.get("friction_defects", {})
        self.assertEqual(list(defects), ["SCAFFOLD-TASK:start-subtask"])
        defect = defects["SCAFFOLD-TASK:start-subtask"]
        self.assertEqual(defect["status"], "open")
        self.assertIn("outside workspace_root", defect["message"])
