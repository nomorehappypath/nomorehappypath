# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""P8 board-context compaction sims (owner bar: no happy path).

These sims prove the CTO remains a global long-lived monitor, the reviewer is
not routinely stopped or respawned, dashboard refresh never launches a
terminal, and bounded polls carry exact authoritative task context without
foreign-event flood or cursor stalls. Review independence remains enforced by
vendor separation and independently authored Challenge Ledgers.

Run:  PYTHONPATH=. python3 -m unittest tests.test_context_rotation -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, control


class ContextRotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _reviewer(self, task_reviews: list[tuple[str, str]]):
        """A live reviewer session whose agent completed the given reviews."""
        session = control.create(self.root, "claude_reviewer")
        agent = board.register(self.root, "qa", "REVIEW_QUEUE",
                               vendor="Anthropic", session_id=session["id"])
        with board.locked_state(self.root) as state:
            for rid, (task, status) in enumerate(task_reviews):
                state["qa_requests"][f"r{rid}"] = {
                    "id": f"r{rid}", "task": task, "status": status,
                    "phase": "chunk", "stage": "independent_review", "cycle": 1,
                    "subtask": "", "chunk": "", "developer_id": "dev",
                    "claimed_by": agent["id"], "review_wait_started_at": "",
                    "structure_revision": 0, "requested_at": board.now(),
                    "claimed_at": board.now(), "completed_at": board.now(),
                }
        return session, agent

    def _accept(self, task: str):
        with board.locked_state(self.root) as state:
            state["releases"][task] = {"task": task, "status": "VISUAL_TEST_REQUIRED",
                                       "head_commit": "abc", "cto_id": "cto",
                                       "recorded_at": board.now()}
        board.record_release_decision(self.root, task, "accepted")

    # ---- S-P8-001: accepted tasks never terminate the standing reviewer ----
    def test_reviewer_is_preserved_after_its_tasks_conclude(self):
        session, agent = self._reviewer([("TASK-X", "passed"), ("TASK-X", "failed")])
        self._accept("TASK-X")
        retired = board_viewer.rotate_reviewer_sessions(self.root)
        self.assertEqual(retired, [])
        statuses = {s["id"]: s["status"] for s in control.snapshot(self.root)["sessions"]}
        self.assertIn(statuses[session["id"]], control.ACTIVE_STATUSES)
        events = board.snapshot(self.root).get("events", [])
        self.assertFalse(any(e.get("kind") == "reviewer_rotated" for e in events))

    # ---- S-P8-002: NEVER retire mid-claim (settled-review immutability class) ----
    def test_reviewer_with_open_claim_is_untouchable(self):
        session, agent = self._reviewer([("TASK-X", "passed")])
        self._accept("TASK-X")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["live"] = {
                "id": "live", "task": "TASK-Y", "status": "claimed",
                "phase": "chunk", "stage": "independent_review", "cycle": 1,
                "subtask": "", "chunk": "", "developer_id": "dev",
                "claimed_by": agent["id"], "review_wait_started_at": "",
                "structure_revision": 0, "requested_at": board.now(),
                "claimed_at": board.now(),
            }
        retired = board_viewer.rotate_reviewer_sessions(self.root)
        self.assertEqual(retired, [], "a held claim forbids rotation absolutely")

    # ---- S-P8-003: an unconcluded reviewed task also forbids rotation ----
    def test_reviewer_waits_for_task_conclusion(self):
        session, agent = self._reviewer([("TASK-OPEN", "passed")])
        retired = board_viewer.rotate_reviewer_sessions(self.root)
        self.assertEqual(retired, [], "the task might still need repair cycles")

    def test_reviewer_affinity_is_stable_within_task_and_exclusive_across_tasks(self):
        old_session, old = self._reviewer([("TASK-X", "failed")])
        fresh_session = control.create(self.root, "claude_reviewer")
        fresh = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
            session_id=fresh_session["id"],
        )
        with board.locked_state(self.root) as state:
            state["agents"][old["id"]]["spawned_at"] = "2026-08-18T00:00:00+00:00"
            state["agents"][fresh["id"]]["spawned_at"] = "2026-08-18T00:01:00+00:00"
            state["qa_requests"]["repair-x"] = {
                "id": "repair-x", "task": "TASK-X", "status": "open",
                "phase": "chunk", "stage": "independent_review", "cycle": 2,
                "subtask": "", "chunk": "", "developer_id": "missing-dev",
                "claimed_by": "", "review_wait_started_at": "",
                "structure_revision": 0, "requested_at": board.now(),
            }
        board.route_open_reviews(self.root)
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"]["repair-x"]["routed_to"], old["id"])

        with board.locked_state(self.root) as state:
            state["qa_requests"]["repair-x"]["status"] = "passed"
            state["qa_requests"]["repair-x"]["completed_at"] = board.now()
            state["qa_requests"]["new-y"] = {
                "id": "new-y", "task": "TASK-Y", "status": "open",
                "phase": "chunk", "stage": "independent_review", "cycle": 1,
                "subtask": "", "chunk": "", "developer_id": "missing-dev",
                "claimed_by": "", "review_wait_started_at": "",
                "structure_revision": 0, "requested_at": board.now(),
            }
        board.route_open_reviews(self.root)
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"]["new-y"]["routed_to"], fresh["id"])
        self.assertNotEqual(
            state["qa_requests"]["new-y"]["routed_to"], old["id"],
            "a reviewer retained for repair continuity must never accumulate a second task",
        )

    # ---- S-P8-004 (REWRITTEN after the 2026-08-16 spawn-loop incident):
    # a refresh path NEVER launches terminals; it records the need once ----
    def test_open_review_signals_need_without_spawning(self):
        with board.locked_state(self.root) as state:
            state["qa_requests"]["orphan"] = {
                "id": "orphan", "task": "TASK-N", "status": "open",
                "phase": "chunk", "stage": "independent_review", "cycle": 1,
                "subtask": "", "chunk": "", "developer_id": "dev",
                "claimed_by": "", "review_wait_started_at": "",
                "structure_revision": 0, "requested_at": board.now(),
            }
        with patch("harness.board_viewer.launch_terminal") as launch:
            outcome = board_viewer.ensure_reviewer_available(self.root)
        self.assertEqual(outcome["status"], "reviewer_needed")
        launch.assert_not_called()
        events = board.snapshot(self.root).get("events", [])
        self.assertEqual(sum(1 for e in events if e.get("kind") == "reviewer_needed"), 1)
        # THE INCIDENT REPLAY: 50 dashboard refreshes in a row must launch,
        # route, stop, dispatch, or execute nothing and must not write state.
        state_path = board.board_dir(self.root) / "state.json"
        before = state_path.read_bytes()
        with patch("harness.board_viewer.launch_terminal") as launch2, \
                patch("harness.board_viewer.release_coordinator.coordinate") as coordinate, \
                patch("harness.board_viewer.dispatch_approved_findings") as dispatch, \
                patch("harness.board_viewer.rotate_cto_session") as rotate_cto, \
                patch("harness.board_viewer.rotate_reviewer_sessions") as rotate_reviewers, \
                patch("harness.board_viewer.retire_orphan_sessions") as retire:
            for _ in range(50):
                board_viewer.dashboard_payload(self.root)
        launch2.assert_not_called()
        coordinate.assert_not_called()
        dispatch.assert_not_called()
        rotate_cto.assert_not_called()
        rotate_reviewers.assert_not_called()
        retire.assert_not_called()
        self.assertEqual(state_path.read_bytes(), before)
        events = board.snapshot(self.root).get("events", [])
        self.assertEqual(sum(1 for e in events if e.get("kind") == "reviewer_needed"), 1,
                         "the need is recorded once, never once per refresh")

    # ---- S-P8-007: compaction cannot stop or relaunch the CTO ----
    def test_context_compaction_never_uses_the_spawn_budget(self):
        with board.locked_state(self.root) as state:
            for index in range(board_viewer._SPAWN_BUDGET_MAX):
                board._event(state, "cto_rotated", None, {
                    "task": "GLOBAL_MONITOR", "message": f"rotation {index}"})
        self.assertTrue(board_viewer._spawn_budget_exhausted(self.root))
        session = control.create(self.root, "claude_cto")
        agent = board.register(self.root, "cto", "GLOBAL_MONITOR",
                               vendor="Anthropic", session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["poll_counter"] = board_viewer.CTO_ROTATION_POLLS + 1
        with patch("harness.board_viewer.launch_terminal") as launch:
            outcome = board_viewer.rotate_cto_session(self.root)
        self.assertEqual(outcome["status"], "preserved")
        launch.assert_not_called()
        managed = {item["id"]: item for item in control.snapshot(self.root)["sessions"]}
        self.assertIn(managed[session["id"]]["status"], control.ACTIVE_STATUSES,
                      "budget refusal must not stop the only live CTO")

    # ---- S-P8-005: the CTO remains one global monitor, quiet or busy ----
    def test_cto_is_preserved_across_tasks_and_quiet_periods(self):
        session = control.create(self.root, "claude_cto")
        agent = board.register(self.root, "cto", "GLOBAL_MONITOR",
                               vendor="Anthropic", session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["poll_counter"] = board_viewer.CTO_ROTATION_POLLS + 1
        with patch("harness.board_viewer.launch_terminal") as launch, \
                patch("harness.board_viewer.control.stop") as stop:
            quiet = board_viewer.rotate_cto_session(self.root)
            board.record_finding(self.root, "TASK-F", "Pending triage item",
                                 "The global CTO must retain this task", False)
            busy = board_viewer.rotate_cto_session(self.root)
        self.assertEqual(quiet["status"], "preserved")
        self.assertEqual(busy["status"], "preserved")
        launch.assert_not_called()
        stop.assert_not_called()
        statuses = {s["id"]: s["status"] for s in control.snapshot(self.root)["sessions"]}
        self.assertIn(statuses[session["id"]], control.ACTIVE_STATUSES)

    # ---- S-P8-006: scoped polls — no foreign flood, no heartbeats, no cursor stall ----
    def test_poll_scoping_drops_foreign_flood_without_stalling_cursor(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                               vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "Deliver the widget end to end")
        board.begin_task(self.root, agent["id"], "TASK-MINE")
        with board.locked_state(self.root) as state:
            for kind, task in (("status_update", "TASK-OTHER"),
                               ("qa_result", "TASK-OTHER"),
                               ("board_polled", "TASK-OTHER"),
                               ("task_cancelled", "TASK-OTHER"),
                               ("status_update", "TASK-MINE")):
                state["events"].append({"kind": kind, "task": task, "at": board.now(),
                                        "agent_id": "x", "role": "system",
                                        "message": "y" * 500,
                                        "sequence": state["next_event"]})
                state["next_event"] += 1
        result = board.poll(self.root, agent["id"])
        kinds = [(e.get("kind"), e.get("task")) for e in result["events"]]
        self.assertNotIn(("status_update", "TASK-OTHER"), kinds, "foreign prose must not flood")
        self.assertNotIn(("qa_result", "TASK-OTHER"), kinds, "foreign verdicts must not flood")
        self.assertNotIn(("board_polled", "TASK-OTHER"), kinds, "heartbeats never delivered")
        self.assertIn(("task_cancelled", "TASK-OTHER"), kinds, "conclusion signals pass")
        self.assertIn(("status_update", "TASK-MINE"), kinds, "own-task events always pass")
        self.assertEqual(result["context_bundle"]["authority"], "board")
        self.assertEqual([item["task"] for item in result["context_bundle"]["tasks"]], ["TASK-MINE"])
        self.assertEqual(
            result["context_bundle"]["tasks"][0]["owner_direction"],
            "Deliver the widget end to end",
        )
        # The cursor advanced over EVERYTHING scanned: a second poll re-delivers nothing.
        second = board.poll(self.root, agent["id"])
        self.assertEqual([e for e in second["events"] if e.get("kind") != "board_polled"], [],
                         "filtered noise must never be re-scanned")

    def test_cto_poll_compacts_all_active_tasks_without_rotating(self):
        cto_session = control.create(self.root, "claude_cto")
        cto = board.register(
            self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic",
            session_id=cto_session["id"],
        )
        for task in ("TASK-A", "TASK-B"):
            with board.locked_state(self.root) as state:
                state["task_owner_directions"][task] = f"Full direction for {task}"
                state["delivery_plans"][task] = {
                    "task": task, "mode": "atomic", "rationale": "cohesive",
                    "subtasks": {}, "structure_revision": 1,
                }
                state["agents"][f"dev-{task}"] = {
                    **cto,
                    "id": f"dev-{task}", "role": "engineering", "task": task,
                    "active": True, "session_id": "", "status": "implementing",
                    "status_note": "active task",
                }
        result = board.poll(self.root, cto["id"])
        self.assertEqual(result["context_bundle"]["scope"], "global")
        self.assertEqual(
            [item["task"] for item in result["context_bundle"]["tasks"]],
            ["TASK-A", "TASK-B"],
        )
        self.assertEqual(
            [item["owner_direction"] for item in result["context_bundle"]["tasks"]],
            ["Full direction for TASK-A", "Full direction for TASK-B"],
        )


if __name__ == "__main__":
    unittest.main()
