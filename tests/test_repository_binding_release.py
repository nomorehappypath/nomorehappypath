# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Repository-reservation release simulations (finding-16023959aaadaae0).

Defect: a released, owner-accepted task kept its repository binding forever, so
bind_task_repository refused every later task on the same repository ("already
attached to another active task") and the CTO had to release bindings by raw
board surgery. These scenarios prove the fix: a concluded task (accepted
release or cancelled) no longer reserves its repository, an unfinished or
rejected-pending-repair task still does, and nothing is deleted — history and
evidence projections keep resolving every binding.

Run:  PYTHONPATH=. python3 -m unittest tests.test_repository_binding_release -v
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control


class RepositoryBindingReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def delivery(self, task: str):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                               vendor="OpenAI", session_id=session["id"])
        objective = f"OWNER DIRECTION — {task}"
        board.record_owner_direction(self.root, session["id"], objective)
        board.begin_task(self.root, agent["id"], task)
        board.record_requirement_confirmation(
            self.root, agent["id"],
            f"Final agreed requirements for {task}: preserve the requested delivery and verify it end to end.")
        contract.create_contract(self.root, task, objective, ["delivery"])
        return agent

    def repo(self, name: str) -> Path:
        repository = self.root / name
        repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=repository, check=True)
        (repository / "product.txt").write_text("baseline\n")
        subprocess.run(["git", "add", "product.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repository, check=True)
        return repository

    def accept_release(self, task: str) -> None:
        with board.locked_state(self.root) as state:
            state["releases"][task] = {
                "task": task, "status": "VISUAL_TEST_REQUIRED",
                "head_commit": "abc123", "cto_id": "cto-test", "recorded_at": board.now(),
            }
        board.record_release_decision(self.root, task, "accepted")

    # ---- S-RBIND-001: accepted release frees the repository for the next task ----
    def test_accepted_task_no_longer_reserves_its_repository(self):
        repository = self.repo("shared-product")
        first = self.delivery("TASK-DONE")
        board.bind_task_repository(self.root, first["id"], str(repository))
        self.accept_release("TASK-DONE")
        second = self.delivery("TASK-NEXT")
        event = board.bind_task_repository(self.root, second["id"], str(repository))
        self.assertEqual(event["kind"], "task_repository_bound")
        state = board.snapshot(self.root)
        # Nothing was deleted: the accepted task's binding is still recorded.
        self.assertEqual(Path(state["task_repositories"]["TASK-DONE"]), repository.resolve())
        self.assertEqual(Path(state["task_repositories"]["TASK-NEXT"]), repository.resolve())
        self.assertNotEqual(
            Path(state["task_workspaces"]["TASK-DONE"]),
            Path(state["task_workspaces"]["TASK-NEXT"]),
        )

    # ---- S-RBIND-002: an unfinished task still reserves its repository ----
    def test_active_task_still_reserves_its_repository(self):
        repository = self.repo("busy-product")
        first = self.delivery("TASK-BUSY")
        board.bind_task_repository(self.root, first["id"], str(repository))
        second = self.delivery("TASK-WAITING")
        with self.assertRaisesRegex(ValueError, "already attached"):
            board.bind_task_repository(self.root, second["id"], str(repository))

    # ---- S-RBIND-003: a rejected release pending repair still reserves ----
    def test_rejected_release_keeps_the_reservation_through_repair(self):
        repository = self.repo("repair-product")
        first = self.delivery("TASK-REPAIR")
        board.bind_task_repository(self.root, first["id"], str(repository))
        with board.locked_state(self.root) as state:
            state["releases"]["TASK-REPAIR"] = {
                "task": "TASK-REPAIR", "status": "VISUAL_TEST_REQUIRED",
                "head_commit": "abc123", "cto_id": "cto-test", "recorded_at": board.now(),
            }
        board.record_release_decision(self.root, "TASK-REPAIR", "not_accepted", "visual defect")
        second = self.delivery("TASK-BLOCKED")
        with self.assertRaisesRegex(ValueError, "already attached"):
            board.bind_task_repository(self.root, second["id"], str(repository))

    # ---- S-RBIND-004: a cancelled task no longer reserves ----
    def test_cancelled_task_no_longer_reserves_its_repository(self):
        repository = self.repo("cancelled-product")
        first = self.delivery("TASK-CANCELLED")
        board.bind_task_repository(self.root, first["id"], str(repository))
        with board.locked_state(self.root) as state:
            state.setdefault("cancelled_tasks", {})["TASK-CANCELLED"] = {
                "task": "TASK-CANCELLED", "cancelled_at": board.now(), "reason": "owner cancelled"}
        second = self.delivery("TASK-AFTER-CANCEL")
        event = board.bind_task_repository(self.root, second["id"], str(repository))
        self.assertEqual(event["kind"], "task_repository_bound")

    # ---- S-RBIND-005: the exact board scenario — two accepted tasks, no hand release ----
    def test_two_accepted_tasks_do_not_require_manual_release(self):
        repository = self.repo("real-sequence-product")
        for task in ("TASK-PHASE-A", "TASK-PHASE-B"):
            agent = self.delivery(task)
            board.bind_task_repository(self.root, agent["id"], str(repository))
            self.accept_release(task)
            # The finished task's terminal is gone, exactly as on the real board.
            control.stop(self.root, agent["session_id"])
        follow_up = self.delivery("TASK-FOLLOW-UP")
        event = board.bind_task_repository(self.root, follow_up["id"], str(repository))
        self.assertEqual(event["kind"], "task_repository_bound")
        state = board.snapshot(self.root)
        self.assertEqual(len([t for t, p in state["task_repositories"].items()
                              if Path(p) == repository.resolve()]), 3,
                         "all three bindings remain recorded for history")

    # ---- S-RBIND-006: attach_task_workspace honors the same conclusion rule ----
    def test_attach_honors_concluded_tasks_too(self):
        # The board root itself is the git repository, as in the shipped attach
        # flow: begin_task creates each task's isolated retained workspace.
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Harness"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text(".harness/\n")
        (self.root / "tracked.txt").write_text("baseline\n")
        subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)
        self.delivery("TASK-ATTACH-DONE")
        retained = Path(board.snapshot(self.root)["task_workspaces"]["TASK-ATTACH-DONE"])
        self.accept_release("TASK-ATTACH-DONE")
        second = self.delivery("TASK-ATTACH-NEXT")
        with board.locked_state(self.root) as state:
            state["task_workspaces"].pop("TASK-ATTACH-NEXT", None)
        event = board.attach_task_workspace(self.root, second["id"], str(retained))
        self.assertEqual(event["kind"], "task_workspace_attached")


if __name__ == "__main__":
    unittest.main()
