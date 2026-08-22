# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Crash-safe, board-authoritative project resume simulations."""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, control, project_manager, project_memory
from harness import project_registry as registry


def _tree_manifest(root: Path) -> dict[str, str]:
    # The ONE sanctioned automatic write into an adopted repository is the
    # owner-directed Claude permissions file; everything else stays identical.
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and str(path.relative_to(root)) != ".claude/settings.local.json"
    }


class _Worker:
    pid = 88123

    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


class ProjectResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.code = self.base / "code"
        self.code.mkdir()
        self.entry = registry.register(
            self.home, "resume-project", self.code,
            description="A project used to prove exact pause and resume recovery.",
        )
        self.root = registry.context_for_entry(self.entry)

    def _manager(self, launched: list[str]) -> project_manager.ProjectManager:
        return project_manager.ProjectManager(
            self.home, board_port=0,
            terminal_launcher=lambda _root, session: launched.append(session["id"]),
        )

    def _resume(self, manager: project_manager.ProjectManager) -> dict:
        with patch("harness.project_manager.subprocess.Popen", return_value=_Worker()):
            return manager.resume_project(self.entry["id"])

    def test_dead_sessions_stage_for_owner_relaunch_and_claim_resumes_with_same_owner(self):
        delivery_session = control.create(self.root, "codex_delivery")
        review_session = control.create(self.root, "claude_reviewer")
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=delivery_session["id"],
        )
        reviewer = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
            session_id=review_session["id"],
        )
        board.status(self.root, delivery["id"], "continue from acceptance gate 4")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-resume"] = {
                "id": "review-resume", "task": "T", "phase": "subtask_acceptance",
                "subtask": "resume", "chunk": "subtask-final", "cycle": 1,
                "status": "claimed", "result": None, "developer_id": delivery["id"],
                "claimed_by": reviewer["id"], "reserved_by": reviewer["id"],
                "requested_at": board.now(), "review_wait_started_at": board.now(),
                "challenge_ledger": "/durable/reviewer-ledger.md", "route_state": "review_executing",
            }

        manager = self._manager([])
        manager.pause_project(self.entry["id"], drain_seconds=0, stop_timeout=0)
        launched: list[str] = []
        manager.terminal_launcher = lambda _root, session: launched.append(session["id"])
        first = self._resume(manager)
        second = self._resume(manager)

        staged = {
            item["id"] for item in control.snapshot(self.root)["sessions"]
            if item.get("status") == "launching" and not item.get("resume_launch_requested_at")
        }
        self.assertEqual(staged, set([delivery_session["id"], review_session["id"]]))
        self.assertEqual(launched, [], "resume must never spawn terminals; the owner's button does")
        self.assertEqual(second["resume"]["resume_id"], first["resume"]["resume_id"])
        self.assertTrue(second["sessions"])
        self.assertTrue(all(
            item["action"] in {"awaiting_attachment", "re_adopted", "relaunch"}
            for item in second["sessions"]
        ), "double resume may observe sessions but cannot relaunch them")
        state = board.snapshot(self.root)
        self.assertEqual(state["project_pause"]["status"], "active")
        self.assertTrue(state["agents"][delivery["id"]]["active"])
        self.assertEqual(state["agents"][delivery["id"]]["status_note"], "continue from acceptance gate 4")
        claim = state["qa_requests"]["review-resume"]
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["claimed_by"], reviewer["id"])
        self.assertEqual(claim["challenge_ledger"], "/durable/reviewer-ledger.md")


    def test_failed_latest_review_restores_inactive_delivery_owner(self):
        session = control.create(self.root, "codex_delivery")
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Resume the failed review repair.")
        board.begin_task(self.root, delivery["id"], "RESUME-FAILED")
        with board.locked_state(self.root) as state:
            state["agents"][delivery["id"]].update({
                "active": False, "status": "independent_review_failed",
            })
            state["qa_requests"]["review-resume-failed"] = {
                "id": "review-resume-failed", "task": "RESUME-FAILED",
                "status": "failed", "result": "failed", "cycle": 3,
                "phase": "final_acceptance", "subtask": "", "chunk": "final",
                "structure_revision": 0, "developer_id": delivery["id"],
                "integrity_invalidated": True, "requested_at": board.now(),
                "claimed_by": None, "review_wait_started_at": board.now(),
            }
        board.begin_project_pause(self.root, drain_seconds=0)
        paused = board.finish_project_pause(self.root)
        self.assertNotIn(delivery["id"], paused["agents"])

        launched: list[str] = []
        result = self._resume(self._manager(launched))

        staged = {
            item["id"] for item in control.snapshot(self.root)["sessions"]
            if item.get("status") == "launching" and not item.get("resume_launch_requested_at")
        }
        self.assertEqual(staged, set([session["id"]]))
        self.assertEqual(launched, [], "resume must never spawn terminals; the owner's button does")
        self.assertEqual(result["resume"]["restored_agents"], 1)
        restored = board.snapshot(self.root)["agents"][delivery["id"]]
        self.assertTrue(restored["active"])
        self.assertEqual(restored["task"], "RESUME-FAILED")
        checkpoint = result["resume"]["checkpoints"]["delivery_ownership_reconciled"]
        self.assertEqual(checkpoint["details"]["staged"][0]["agent_id"], delivery["id"])

    def test_missing_control_record_is_recreated_under_same_delivery_identity(self):
        session = control.create(self.root, "codex_delivery")
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Recover the missing terminal transport.")
        board.begin_task(self.root, delivery["id"], "RESUME-MISSING-TRANSPORT")
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        with control.locked_state(self.root) as state:
            state["sessions"].pop(session["id"])

        launched: list[str] = []
        self._resume(self._manager(launched))

        staged = {
            item["id"] for item in control.snapshot(self.root)["sessions"]
            if item.get("status") == "launching" and not item.get("resume_launch_requested_at")
        }
        self.assertEqual(staged, set([session["id"]]))
        self.assertEqual(launched, [], "resume must never spawn terminals; the owner's button does")
        sessions = {item["id"]: item for item in control.snapshot(self.root)["sessions"]}
        self.assertIn(session["id"], sessions)
        agents = board.snapshot(self.root)["agents"]
        self.assertEqual(len(agents), 1)
        self.assertTrue(agents[delivery["id"]]["active"])

    def test_development_complete_without_delivery_action_does_not_revive_delivery(self):
        session = control.create(self.root, "codex_delivery")
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Keep release-only work with the CTO.")
        board.begin_task(self.root, delivery["id"], "RELEASE-ONLY")
        with board.locked_state(self.root) as state:
            state["agents"][delivery["id"]].update({"active": False, "status": "done"})
            state.setdefault("release_lifecycle", {})["RELEASE-ONLY"] = {
                "development_completed_at": board.now(), "phases": {},
            }
            # Prove resume does not depend on this lifecycle event remaining in
            # the bounded hot window after a long task.
            state["events"] = []
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)

        launched: list[str] = []
        result = self._resume(self._manager(launched))

        self.assertEqual(launched, [])
        self.assertEqual(result["resume"]["restored_agents"], 0)
        self.assertFalse(board.snapshot(self.root)["agents"][delivery["id"]]["active"])

    def test_active_reviewer_work_does_not_revive_completed_delivery(self):
        delivery_session = control.create(self.root, "codex_delivery")
        reviewer_session = control.create(self.root, "claude_reviewer")
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=delivery_session["id"],
        )
        reviewer = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
            session_id=reviewer_session["id"],
        )
        board.record_owner_direction(self.root, delivery_session["id"], "Review without wasting a Delivery terminal.")
        board.begin_task(self.root, delivery["id"], "REVIEWER-ONLY")
        with board.locked_state(self.root) as state:
            state["agents"][delivery["id"]].update({"active": False, "status": "done"})
            state.setdefault("release_lifecycle", {})["REVIEWER-ONLY"] = {
                "development_completed_at": board.now(), "phases": {},
            }
            state["qa_requests"]["reviewer-only-final"] = {
                "id": "reviewer-only-final", "task": "REVIEWER-ONLY",
                "status": "claimed", "result": None, "cycle": 1,
                "phase": "final_acceptance", "subtask": "", "chunk": "final",
                "structure_revision": 0, "developer_id": delivery["id"],
                "claimed_by": reviewer["id"], "reserved_by": reviewer["id"],
                "requested_at": board.now(), "review_wait_started_at": board.now(),
                "challenge_ledger": "/durable/reviewer-ledger.md",
            }
        board.begin_project_pause(self.root, drain_seconds=0)
        paused = board.finish_project_pause(self.root)
        self.assertNotIn(delivery["id"], paused["agents"])
        self.assertIn(reviewer["id"], paused["agents"])

        launched: list[str] = []
        result = self._resume(self._manager(launched))

        # The relaunch OFFER is what must exclude the completed delivery:
        # only the reviewer's session is prepared for the owner's relaunch
        # button; the retired delivery terminal stays retired.
        offers = {
            item["id"]: item.get("action") for item in result["sessions"]
        }
        self.assertEqual(offers.get(reviewer_session["id"]), "relaunch")
        self.assertNotEqual(offers.get(delivery_session["id"]), "relaunch")
        # The consent boundary is durable: only the offered session may be
        # launched by the owner's button; the retired terminal is refused.
        control.mark_resume_launch_requested(self.root, reviewer_session["id"])
        with self.assertRaisesRegex(ValueError, "not offered for relaunch|not staged for launch"):
            control.mark_resume_launch_requested(self.root, delivery_session["id"])
        self.assertEqual(launched, [], "resume must never spawn terminals; the owner's button does")
        self.assertEqual(result["resume"]["restored_agents"], 1)
        self.assertFalse(board.snapshot(self.root)["agents"][delivery["id"]]["active"])

    def test_surviving_process_is_readopted_without_launching_duplicate(self):
        session = control.create(self.root, "codex_delivery")
        process = subprocess.Popen(["sleep", "30"])
        def stop_process():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        self.addCleanup(stop_process)
        control.attach(self.root, session["id"], process.pid)
        agent = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.status(self.root, agent["id"], "continue with the live terminal")
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)

        launched: list[str] = []
        result = self._resume(self._manager(launched))

        self.assertEqual(launched, [])
        self.assertEqual(result["sessions"][0]["action"], "re_adopted")
        resumed = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["pid"], process.pid)

    def test_resume_recovers_before_and_after_terminal_launch_checkpoint(self):
        # Resume never launches; it stages. "after" simulated a legacy
        # already-requested launch, "expired" a stale request that restages.
        for checkpoint, expected_staged in (("before", 1), ("after", 0), ("expired", 1)):
            with self.subTest(checkpoint=checkpoint):
                case = self.base / f"{checkpoint}-launch"
                case.mkdir()
                entry = registry.register(self.home, f"case-{checkpoint}", case)
                root = registry.context_for_entry(entry)
                session = control.create(root, "codex_delivery")
                board.register(
                    root, "engineering", board.AWAITING_OWNER_DIRECTION,
                    vendor="OpenAI", session_id=session["id"],
                )
                board.begin_project_pause(root, drain_seconds=0)
                board.finish_project_pause(root)
                transaction = board.begin_project_resume(root)
                [prepared] = control.prepare_resume_sessions(root, [session["id"]])
                self.assertEqual(prepared["action"], "relaunch")
                if checkpoint in {"after", "expired"}:
                    control.mark_resume_launch_requested(root, session["id"])
                if checkpoint == "expired":
                    with control.locked_state(root) as state:
                        saved = state["sessions"][session["id"]]
                        saved["launch_deadline"] = "2000-01-01T00:00:00+00:00"

                launched: list[str] = []
                manager = project_manager.ProjectManager(
                    self.home, board_port=0,
                    terminal_launcher=lambda _root, resumed: launched.append(resumed["id"]),
                )
                with patch("harness.project_manager.subprocess.Popen", return_value=_Worker()):
                    result = manager.resume_project(entry["id"])

                self.assertEqual(result["resume"]["resume_id"], transaction["resume_id"])
                self.assertEqual(launched, [], "resume must never spawn terminals")
                staged = [
                    item for item in control.snapshot(root)["sessions"]
                    if item.get("status") == "launching" and not item.get("resume_launch_requested_at")
                ]
                self.assertEqual(len(staged), expected_staged)
                self.assertEqual(board.pause_state(root)["status"], "active")
                manager.close_project(entry["id"])

    def test_divergent_memory_warns_and_is_rebuilt_from_board(self):
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        transaction = board.begin_project_resume(self.root)
        damaged = project_memory.load_index(self.root)
        damaged["board_sequence"] = 999999
        damaged["answers"]["current_status"] = "Memory falsely says everything is done."
        project_memory._write_index(self.root, damaged)

        result = self._resume(self._manager([]))

        self.assertEqual(result["resume"]["resume_id"], transaction["resume_id"])
        self.assertEqual(result["memory"]["authority"], "board")
        self.assertEqual(result["memory"]["status"], "rebuilt")
        self.assertIn("did not match board sequence", result["memory"]["warning"])
        repaired = project_memory.load_index(self.root)
        self.assertNotEqual(repaired["answers"]["current_status"], "Memory falsely says everything is done.")
        checkpoint = board.pause_state(self.root)["last_resume"]["checkpoints"]["board_authority"]
        self.assertEqual(checkpoint["details"]["status"], "rebuilt")

    def test_resume_keeps_write_gate_closed_until_atomic_finish(self):
        agent = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI",
        )
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        transaction = board.begin_project_resume(self.root)

        with self.assertRaises(board.ProjectPausedError):
            board.status(self.root, agent["id"], "must remain blocked during reconciliation")
        board.finish_project_resume(self.root, transaction["resume_id"])
        event = board.status(self.root, agent["id"], "write gate reopened at the saved stage")
        self.assertEqual(event["kind"], "status_update")

    def test_relaunched_runner_reattaches_preserved_agent_instead_of_registering_duplicate(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        board.begin_project_resume(self.root)

        reattached = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )

        self.assertEqual(reattached["id"], agent["id"])
        self.assertEqual(len(board.snapshot(self.root)["agents"]), 1)

    def test_reattached_cto_gets_heartbeat_grace_without_a_fabricated_poll(self):
        cto_session = control.create(self.root, "claude_cto")
        delivery_session = control.create(self.root, "codex_delivery")
        cto_agent = board.register(
            self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic",
            session_id=cto_session["id"],
        )
        delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=delivery_session["id"],
        )
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][delivery["id"]]["task"] = "ACTIVE-TASK"
            state["agents"][cto_agent["id"]].update({
                "last_poll_at": old,
                "recovery_state": "automatic_requested",
                "automatic_recovery_requested_at": old,
            })
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        transaction = board.begin_project_resume(self.root)
        reattached = board.register(
            self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic",
            session_id=cto_session["id"],
        )
        board.finish_project_resume(self.root, transaction["resume_id"])

        self.assertEqual(reattached["poll_counter"], 0)
        self.assertTrue(reattached["session_reattached_at"])
        self.assertEqual(board.mark_stalled(self.root, stale_seconds=60), [])
        current = board.snapshot(self.root)["agents"][cto_agent["id"]]
        self.assertEqual(current["poll_counter"], 0)
        self.assertNotEqual(current["liveness"], "stalled")

    def test_late_exact_session_reattachment_can_recover_an_offline_agent(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        transaction = board.begin_project_resume(self.root)
        board.finish_project_resume(self.root, transaction["resume_id"])
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({
                "active": True, "status": "offline", "liveness": "offline",
                "status_note": "first resume launch exited before attachment",
            })

        reattached = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        self.assertEqual(reattached["id"], agent["id"])
        self.assertEqual(reattached["liveness"], "recovering")
        self.assertNotEqual(reattached["status"], "offline")
        polled = board.poll(self.root, agent["id"])
        self.assertEqual(polled["agent_id"], agent["id"])
        current = board.snapshot(self.root)["agents"][agent["id"]]
        self.assertEqual(current["liveness"], "healthy")
        self.assertTrue(current.get("session_reattached_at"))
        self.assertEqual(len(board.snapshot(self.root)["agents"]), 1)

    def test_only_the_preserved_session_can_register_while_resume_gate_is_closed(self):
        saved_session = control.create(self.root, "codex_delivery")
        saved = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=saved_session["id"],
        )
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        board.begin_project_resume(self.root)

        intruder_session = control.create(self.root, "codex_delivery")
        with self.assertRaises(board.ProjectPausedError):
            board.register(
                self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
                vendor="OpenAI", session_id=intruder_session["id"],
            )
        reattached = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=saved_session["id"],
        )

        self.assertEqual(reattached["id"], saved["id"])
        self.assertEqual(len(board.snapshot(self.root)["agents"]), 1)

    def test_four_owner_answers_are_current_from_memory_alone_after_resume(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["task"] = "T-resume"
            state["task_owner_directions"]["T-resume"] = "Resume exactly where work stopped."
        board.status(self.root, agent["id"], "continue the resume implementation")
        manager = self._manager([])
        manager.pause_project(self.entry["id"], drain_seconds=0, stop_timeout=0)
        self._resume(manager)

        memory_only = project_memory.resume_context(self.root)
        answers = memory_only["answers"]
        self.assertEqual(memory_only["authority"], "memory")
        self.assertIn("exact pause and resume recovery", answers["project_about"])
        self.assertIn("T-resume", answers["current_status"])
        self.assertIn("T-resume", answers["last_task_result"])
        self.assertIn("T-resume", answers["remaining_work"])

    def test_adopted_repository_remains_byte_identical_through_resume(self):
        home = self.base / "adopted-home"
        code = self.base / "owner-repository"
        data = self.base / "adopted-data"
        workspace = self.base / "adopted-workspace"
        code.mkdir()
        (code / "owner.txt").write_text("owner bytes\n", encoding="utf-8")
        (code / "nested").mkdir()
        (code / "nested" / "data.bin").write_bytes(b"\x00\x01\x02")
        entry = registry.register(
            home, "adopted-resume", code, kind="adopted",
            data_root=data, workspace_root=workspace,
        )
        root = registry.context_for_entry(entry)
        before = _tree_manifest(code)

        manager = project_manager.ProjectManager(home, board_port=0, terminal_launcher=lambda *_: None)
        manager.pause_project(entry["id"], drain_seconds=0, stop_timeout=0)
        with patch("harness.project_manager.subprocess.Popen", return_value=_Worker()):
            manager.resume_project(entry["id"])

        self.assertEqual(_tree_manifest(code), before)
        self.assertFalse((code / ".harness").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
