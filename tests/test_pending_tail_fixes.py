# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Pending-tail fixes: issue rows 1, 2, 5, 6, 9 (fold-completion program).

Run:  PYTHONPATH=. python3 -m unittest tests.test_pending_tail_fixes -v
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, control


class PendingTailFixTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _delivery_session(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                               vendor="OpenAI", session_id=session["id"])
        return session, agent

    def _git_repo(self, name):
        repository = self.root / name
        repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "h@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "H"], cwd=repository, check=True)
        (repository / "p.txt").write_text("x\n")
        subprocess.run(["git", "add", "p.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repository, check=True)
        return repository

    # ---- row 1: a bare CLI control command is never an owner direction ----
    def test_slash_command_is_refused_as_direction(self):
        session, _ = self._delivery_session()
        for command in ("/model", "/help", "/clear-history"):
            with self.assertRaisesRegex(ValueError, "CLI control command"):
                board.record_owner_direction(self.root, session["id"], command)
        # A real direction that BEGINS with a filesystem path is unaffected.
        event = board.record_owner_direction(
            self.root, session["id"],
            "/Users/owner/projects/widget/docs/TASK.md is the directive file; read it fully.")
        self.assertTrue(event)

    # ---- row 2: bind-repository re-applies the self-development freeze ----
    def test_bind_repository_reapplies_self_freeze(self):
        session, agent = self._delivery_session()
        board.record_owner_direction(self.root, session["id"], "Deliver the widget improvements end to end")
        board.begin_task(self.root, agent["id"], "TASK-REBIND")
        harness_repo = self._git_repo("harness-repo")
        with patch.object(board, "_harness_source_root", return_value=harness_repo), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_ALLOW_SELF_DEVELOPMENT", None)
            with self.assertRaisesRegex(ValueError, "harness's own"):
                board.bind_task_repository(self.root, agent["id"], str(harness_repo))
        # A foreign repository binds exactly as before.
        foreign = self._git_repo("foreign-product")
        event = board.bind_task_repository(self.root, agent["id"], str(foreign))
        self.assertEqual(event["kind"], "task_repository_bound")

    # ---- row 9: a dead terminal is never healthy standby ----
    def test_dead_session_agent_is_offline_not_standby(self):
        session, agent = self._delivery_session()
        control.stop(self.root, session["id"])
        board.mark_stalled(self.root, stale_seconds=1)
        state = board.snapshot(self.root)
        row = state["agents"][agent["id"]]
        self.assertEqual(row["liveness"], "offline")
        self.assertIn("no longer running", row["liveness_note"])
        self.assertTrue(any(e.get("kind") == "agent_offline" for e in state.get("events", [])))

    def test_live_session_standby_is_unchanged(self):
        session, agent = self._delivery_session()
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["liveness"] = "stalled"
        board.mark_stalled(self.root, stale_seconds=10_000)
        row = board.snapshot(self.root)["agents"][agent["id"]]
        self.assertEqual(row["liveness"], "healthy")
        self.assertEqual(row["recovery_state"], "standby")

    # ---- row 6: env-prefixed commands are executable evidence ----
    def test_env_prefixed_commands_are_accepted(self):
        self.assertTrue(contract.EXECUTABLE_TEST_COMMAND.match(
            "HARNESS_BACKUP_DIR=/tmp/x GIT_TRACE=0 python3 -m unittest tests.test_board"))
        self.assertTrue(contract.EXECUTABLE_TEST_COMMAND.match("python3 -m unittest tests.test_board"))
        self.assertFalse(contract.EXECUTABLE_TEST_COMMAND.match("rm -rf /"))
        self.assertFalse(contract.EXECUTABLE_TEST_COMMAND.match("X=1; python3 -m unittest t"))

    # ---- row 5 (native here): evidence executes behind the child_process
    # boundary, which strips ambient GIT_* — proven by an in-suite probe ----
    def test_internal_qa_strips_git_env(self):
        (self.root / "test_env_probe.py").write_text(
            "import os, unittest\n\nclass P(unittest.TestCase):\n"
            "    def test_git_dir_stripped(self):\n"
            "        self.assertNotIn('GIT_DIR', os.environ)\n")
        with patch.dict(os.environ, {"GIT_DIR": "/tmp/poison.git"}):
            output = board._execute_internal_qa("python3 -m unittest test_env_probe", self.root)
        self.assertIn("OK", output)


if __name__ == "__main__":
    unittest.main()


class GitEnvironmentBoundaryTests(unittest.TestCase):
    """Issue row 8 (reverse fold): ambient GIT_* can no longer redirect git."""

    def _repo(self, base: Path, name: str, content: str) -> Path:
        repo = base / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "h@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "H"], cwd=repo, check=True)
        (repo / "f.txt").write_text(content)
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", content.strip()], cwd=repo, check=True)
        return repo

    def test_poisoned_git_dir_cannot_redirect_board_git_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = self._repo(base, "real", "real\n")
            poison = self._repo(base, "poison", "poison\n")
            real_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=real, text=True).strip()
            poison_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=poison, text=True).strip()
            self.assertNotEqual(real_head, poison_head)
            with patch.dict(os.environ, {"GIT_DIR": str(poison / ".git")}):
                commit, _tree = board._git_commit_and_tree(real, "HEAD")
                self.assertEqual(commit, real_head,
                                 "a poisoned GIT_DIR must not redirect the board's git identity")
