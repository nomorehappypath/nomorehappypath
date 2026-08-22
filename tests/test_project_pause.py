# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Non-destructive project pause simulations for Task B phase 2."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, control, project_manager
from harness import project_registry as registry


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_manifest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _digest(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class ProjectPauseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()

    def _agent(self, session_id: str = "") -> dict:
        return board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session_id,
        )

    def test_drain_accepts_inflight_write_then_refuses_and_audits_late_write(self):
        agent = self._agent()
        pause = board.begin_project_pause(self.root, drain_seconds=0)

        during_drain = board.status(self.root, agent["id"], "finishing one in-flight write")
        self.assertEqual(during_drain["kind"], "status_update")
        board.finish_project_pause(self.root)

        with self.assertRaisesRegex(board.ProjectPausedError, "project is paused"):
            board.status(self.root, agent["id"], "late write must be rejected")

        state = board.snapshot(self.root)
        self.assertEqual(state["project_pause"]["pause_id"], pause["pause_id"])
        self.assertEqual(state["project_pause"]["status"], "paused")
        refusal = state["events"][-1]
        self.assertEqual(refusal["kind"], "project_paused_write_refused")
        self.assertIn("no requested board state changed", refusal["message"])
        self.assertEqual(state["agents"][agent["id"]]["status"], "paused")
        json.loads((self.root / ".harness" / "board" / "state.json").read_text())

    def test_pause_preserves_artifacts_review_owner_and_settled_result_idempotently(self):
        agent = self._agent()
        reviewer = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id="reviewer-session",
        )
        artifact_paths = [
            self.root / ".harness" / "tasks" / "T.json",
            self.root / ".harness" / "evidence" / "T" / "proof.txt",
            self.root / ".harness" / "reviews" / "challenge.md",
            self.base / "worktrees" / "T" / "branch-pointer.txt",
        ]
        for index, path in enumerate(artifact_paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"immutable-{index}\n", encoding="utf-8")
        before = {str(path): _digest(path) for path in artifact_paths}

        claimed = {
            "id": "review-claimed", "task": "T", "phase": "subtask_acceptance",
            "subtask": "pause", "chunk": "subtask-final", "cycle": 1,
            "status": "claimed", "result": None, "developer_id": agent["id"],
            "claimed_by": reviewer["id"], "reserved_by": reviewer["id"],
            "requested_at": board.now(), "review_wait_started_at": board.now(),
            "challenge_ledger": str(artifact_paths[2]), "route_state": "review_executing",
        }
        settled = {
            **claimed, "id": "review-passed", "status": "passed", "result": "PASS",
            "completed_at": board.now(), "route_state": "complete",
        }
        with board.locked_state(self.root) as state:
            state["qa_requests"][claimed["id"]] = dict(claimed)
            state["qa_requests"][settled["id"]] = dict(settled)

        pause = board.begin_project_pause(self.root, drain_seconds=0)
        first = board.finish_project_pause(self.root)
        second_begin = board.begin_project_pause(self.root, drain_seconds=30)
        second_finish = board.finish_project_pause(self.root)
        state = board.snapshot(self.root)

        self.assertEqual({str(path): _digest(path) for path in artifact_paths}, before)
        suspended = state["qa_requests"][claimed["id"]]
        self.assertEqual(suspended["status"], "suspended")
        self.assertEqual(suspended["paused_from_status"], "claimed")
        self.assertEqual(suspended["claimed_by"], reviewer["id"])
        self.assertEqual(suspended["challenge_ledger"], str(artifact_paths[2]))
        self.assertEqual(state["qa_requests"][settled["id"]], settled)
        self.assertIn(reviewer["id"], state["agents"])
        self.assertEqual(state["agents"][reviewer["id"]]["status"], "paused")
        self.assertEqual(first["pause_id"], pause["pause_id"])
        self.assertEqual(second_begin["pause_id"], pause["pause_id"])
        self.assertEqual(second_finish["reviews"], first["reviews"])

    def test_real_terminal_is_bounded_and_marked_paused_not_offline(self):
        session = control.create(self.root, "codex_delivery")
        process = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        control.attach(self.root, session["id"], process.pid)

        [paused] = control.pause_sessions(self.root, [session["id"]], timeout=0.05)
        process.wait(timeout=3)

        self.assertEqual(paused["status"], "paused")
        self.assertIsNotNone(paused["pause_requested_at"])
        self.assertIsNone(paused["stop_requested_at"])
        self.assertIn("pause", paused["reason"])
        current = next(value for value in control.snapshot(self.root)["sessions"]
                       if value["id"] == session["id"])
        self.assertEqual(current["status"], "paused")

    def test_terminal_exit_race_cannot_abort_durable_pause(self):
        session = control.create(self.root, "codex_delivery")
        control.attach(self.root, session["id"], 424242)

        with patch("harness.control.os.kill", side_effect=[None, None, ProcessLookupError]):
            paused = control.pause(self.root, session["id"])

        self.assertEqual(paused["status"], "paused")
        self.assertIn("exited while", paused["reason"])

    def test_manager_retry_finishes_interrupted_pause_without_resetting_pointer(self):
        home = self.base / "home"
        code = self.base / "code"
        code.mkdir()
        entry = registry.register(home, "alpha", code)
        context = registry.context_for_entry(entry)
        session = control.create(context, "codex_delivery")
        agent = board.register(
            context, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.status(context, agent["id"], "resume at the exact saved gate")
        interrupted = board.begin_project_pause(context, drain_seconds=0)

        manager = project_manager.ProjectManager(home, board_port=0)
        result = manager.pause_project(entry["id"], drain_seconds=0, stop_timeout=0)
        retried = manager.pause_project(entry["id"], drain_seconds=0, stop_timeout=0)

        self.assertEqual(result["pause"]["pause_id"], interrupted["pause_id"])
        self.assertEqual(retried["pause"]["pause_id"], interrupted["pause_id"])
        saved = result["pause"]["agents"][agent["id"]]
        self.assertEqual(saved["next_action"], "resume at the exact saved gate")
        [managed] = [value for value in control.snapshot(context)["sessions"]
                     if value["id"] == session["id"]]
        self.assertEqual(managed["status"], "paused")
        instructions = control.take_instructions(context, session["id"])
        self.assertEqual(len(instructions), 1)
        self.assertIn("project-pause", instructions[0]["text"])

    def test_adopted_repository_remains_byte_identical(self):
        home = self.base / "home"
        code = self.base / "owner-repository"
        code.mkdir()
        (code / "owner.txt").write_text("owner bytes\n", encoding="utf-8")
        (code / "nested").mkdir()
        (code / "nested" / "data.bin").write_bytes(b"\x00\x01\x02")
        entry = registry.register(home, "adopted", code, kind="adopted")
        before = _tree_manifest(code)

        manager = project_manager.ProjectManager(home, board_port=0)
        manager.pause_project(entry["id"], drain_seconds=0, stop_timeout=0)

        self.assertEqual(_tree_manifest(code), before)
        self.assertFalse((code / ".harness").exists())

    def test_opening_paused_board_resumes_it_for_use(self):
        home = self.base / "paused-open-home"
        code = self.base / "paused-open-code"
        code.mkdir()
        entry = registry.register(home, "paused-open", code)
        context = registry.context_for_entry(entry)
        pause = board.begin_project_pause(context, drain_seconds=0)
        board.finish_project_pause(context)

        class Worker:
            pid = 99123

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        manager = project_manager.ProjectManager(home, board_port=0)
        with patch("harness.project_manager.subprocess.Popen", return_value=Worker()):
            opened = manager.open_project(entry["id"])

        # Open means "give me a usable project": the paused board completes
        # its reconciliation instead of landing the owner in a read-only view,
        # and no terminal is spawned by this.
        self.assertEqual(opened["resume"]["pause_id"], pause["pause_id"])
        self.assertEqual(board.pause_state(context)["status"], "active")
        self.assertEqual(opened.get("sessions", []), [])


if __name__ == "__main__":
    unittest.main()
