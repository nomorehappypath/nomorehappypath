# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Self-development freeze guard — begin_task must refuse to run a governed
task inside the harness's own repository, while leaving every other workflow
(including a self-hosted board that targets a DIFFERENT repo) untouched.

These are adversarial simulations, not happy-path checks: the load-bearing
case is test_external_owner_repo_is_allowed, which proves the guard keys on the
execution workspace, not on where the board bookkeeping happens to live.

Run:  PYTHONPATH=. python3 -m unittest tests.test_self_development_guard -v
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, control
from harness.project_context import ProjectContext


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
    return path


def _ready_delivery(root: Path, direction_text: str) -> str:
    """Register a managed Delivery Agent that has passed the owner-direction gate."""
    session = control.create(root, "codex_delivery")
    agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION,
                           vendor="OpenAI", session_id=session["id"])
    board.record_owner_direction(root, session["id"], direction_text)
    return agent["id"]


class SelfDevelopmentGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # ---- S-GUARD-001 ----
    def test_self_target_is_refused(self):
        harness_repo = _init_repo(self.base / "harness_repo")
        with patch.object(board, "_harness_source_root", return_value=harness_repo):
            agent = _ready_delivery(harness_repo, "verify the crash brake fix")
            with self.assertRaises(ValueError) as ctx:
                board.begin_task(harness_repo, agent, "SELF-TASK")
            self.assertIn("harness's own repository", str(ctx.exception))
            # No task was recorded: the agent is still standing by.
            self.assertEqual(
                board.snapshot(harness_repo)["agents"][agent]["task"],
                board.AWAITING_OWNER_DIRECTION,
            )

    # ---- S-GUARD-002 (load-bearing) ----
    def test_external_owner_repo_is_allowed(self):
        harness_repo = _init_repo(self.base / "harness_repo")
        external = _init_repo(self.base / "ktrader_like")
        with patch.object(board, "_harness_source_root", return_value=harness_repo):
            # Board bookkeeping lives in the harness repo; the task targets a
            # different, owner-named repository. This must be allowed.
            agent = _ready_delivery(harness_repo, f"Work in {external} on the daemon")
            board.begin_task(harness_repo, agent, "EXTERNAL-TASK")
            self.assertEqual(
                board.snapshot(harness_repo)["agents"][agent]["task"], "EXTERNAL-TASK"
            )

    # ---- S-GUARD-003 ----
    def test_separate_project_board_is_allowed(self):
        harness_repo = _init_repo(self.base / "harness_repo")
        project = _init_repo(self.base / "project_board")
        with patch.object(board, "_harness_source_root", return_value=harness_repo):
            agent = _ready_delivery(project, "verify the project feature")
            board.begin_task(project, agent, "PROJECT-TASK")
            self.assertEqual(
                board.snapshot(project)["agents"][agent]["task"], "PROJECT-TASK"
            )

    # ---- S-GUARD-003A: explicit Projects-layer context ----
    def test_separate_project_context_without_named_path_is_allowed(self):
        harness_repo = _init_repo(self.base / "harness_repo")
        project = _init_repo(self.base / "project_code")
        context = ProjectContext(
            project,
            self.base / "private_data",
            self.base / "workspaces",
        )
        with patch.object(board, "_harness_source_root", return_value=harness_repo):
            agent = _ready_delivery(context, "verify the project feature")
            event = board.begin_task(context, agent, "CONTEXT-PROJECT-TASK")
            workspace = Path(event["task_workspace"])
            self.assertTrue(workspace.is_relative_to(context.workspace_root))
            self.assertTrue(workspace.is_dir())
            self.assertEqual(
                board.snapshot(context)["agents"][agent]["task"],
                "CONTEXT-PROJECT-TASK",
            )

    # ---- S-GUARD-004 ----
    def test_refusal_creates_no_workspace(self):
        harness_repo = _init_repo(self.base / "harness_repo")
        with patch.object(board, "_harness_source_root", return_value=harness_repo):
            agent = _ready_delivery(harness_repo, "verify the fix")
            with self.assertRaises(ValueError):
                board.begin_task(harness_repo, agent, "NO-SIDE-EFFECT")
            workspaces = harness_repo.parent / ".harness-task-workspaces"
            created = list(workspaces.glob("*NO-SIDE-EFFECT*")) if workspaces.exists() else []
            self.assertEqual(created, [], "guard must refuse before creating a worktree")

    # ---- S-GUARD-005 ----
    def test_override_env_permits_self_target(self):
        harness_repo = _init_repo(self.base / "harness_repo")
        with patch.object(board, "_harness_source_root", return_value=harness_repo):
            agent = _ready_delivery(harness_repo, "verify the fix")
            with patch.dict(os.environ, {"HARNESS_ALLOW_SELF_DEVELOPMENT": "1"}):
                board.begin_task(harness_repo, agent, "OVERRIDDEN")
            self.assertEqual(
                board.snapshot(harness_repo)["agents"][agent]["task"], "OVERRIDDEN"
            )

    # ---- S-GUARD-006 ----
    def test_installed_non_git_harness_is_inert(self):
        non_git = self.base / "installed_copy"
        non_git.mkdir()
        project = _init_repo(self.base / "some_project")
        with patch.object(board, "_harness_source_root", return_value=non_git):
            # A pinned, non-Git install has no repository identity, so nothing
            # can be "the harness's own repo" — the guard disables itself.
            self.assertFalse(board._is_harness_self_target(project))
            self.assertFalse(board._is_harness_self_target(non_git))

    # ---- S-GUARD-007 ----
    def test_identity_matches_same_repo_not_different_repo(self):
        repo_a = _init_repo(self.base / "a")
        repo_b = _init_repo(self.base / "b")
        with patch.object(board, "_harness_source_root", return_value=repo_a):
            self.assertTrue(board._is_harness_self_target(repo_a))
            self.assertFalse(board._is_harness_self_target(repo_b))
            self.assertFalse(board._is_harness_self_target(None))


if __name__ == "__main__":
    unittest.main()
