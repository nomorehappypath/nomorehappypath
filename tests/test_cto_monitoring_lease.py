# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Adversarial checks for the global CTO's five-minute monitoring lease."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, control


class CtoMonitoringLeaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cto_session = control.create(self.root, "claude_cto")
        self.cto = board.register(
            self.root, "cto", "GLOBAL_MONITOR",
            vendor="Anthropic", session_id=self.cto_session["id"],
        )

    def _activate_task(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Implement the active task.")
        board.begin_task(self.root, agent["id"], "TASK-ACTIVE")
        return agent

    def _backdate_cto_poll(self, seconds):
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][self.cto["id"]].update({
                "spawned_at": old,
                "last_poll_at": old,
                "last_progress_at": fresh,
                "last_status_at": fresh,
                "liveness": "healthy",
                "recovery_state": "resumed",
            })
        with control.locked_state(self.root) as state:
            state["sessions"][self.cto_session["id"]]["last_output_at"] = fresh

    def _monitoring_instructions(self):
        return [
            item for item in control.take_instructions(self.root, self.cto_session["id"])
            if item["source"] == "cto-monitoring-lease"
        ]

    def test_active_task_wakes_cto_only_when_poll_deadline_is_due(self):
        self._activate_task()
        self._backdate_cto_poll(board.CTO_MONITOR_ROUTE_SECONDS - 2)
        board.mark_stalled(self.root)
        self.assertEqual(self._monitoring_instructions(), [])

        self._backdate_cto_poll(board.CTO_MONITOR_INTERVAL_SECONDS + 1)
        with patch("harness.board_viewer.launch_terminal") as launch:
            board.mark_stalled(self.root)
            board.mark_stalled(self.root)
        queued = self._monitoring_instructions()
        self.assertEqual(len(queued), 1, "repeated watchdog ticks must not duplicate the wake")
        launch.assert_not_called()

        state = board.snapshot(self.root)
        cto = state["agents"][self.cto["id"]]
        self.assertEqual(cto["recovery_state"], "automatic_requested")
        self.assertEqual(cto["automatic_recovery_instruction_id"], queued[0]["id"])
        self.assertGreater(cto["last_progress_at"], cto["last_poll_at"])
        self.assertTrue(any(
            event["kind"] == "instruction_route_queued"
            and event.get("instruction_id") == queued[0]["id"]
            for event in state["events"]
        ))

    def test_real_poll_resets_lease_and_a_second_missed_cycle_wakes_again(self):
        self._activate_task()
        self._backdate_cto_poll(board.CTO_MONITOR_INTERVAL_SECONDS + 1)
        board.mark_stalled(self.root)
        self.assertEqual(len(self._monitoring_instructions()), 1)

        board.poll(self.root, self.cto["id"])
        board.mark_stalled(self.root)
        self.assertEqual(self._monitoring_instructions(), [])
        self._backdate_cto_poll(board.CTO_MONITOR_INTERVAL_SECONDS + 1)
        board.mark_stalled(self.root)
        self.assertEqual(len(self._monitoring_instructions()), 1)

    def test_idle_board_never_wakes_or_spawns_cto(self):
        self._backdate_cto_poll(board.CTO_MONITOR_INTERVAL_SECONDS * 3)
        with patch("harness.board_viewer.launch_terminal") as launch:
            for _ in range(4):
                board.mark_stalled(self.root)
        self.assertEqual(self._monitoring_instructions(), [])
        launch.assert_not_called()
        self.assertEqual(
            board.snapshot(self.root)["agents"][self.cto["id"]]["liveness"],
            "healthy",
        )

    def test_open_review_without_active_delivery_is_still_a_task_in_play(self):
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review"] = {
                "id": "review", "task": "TASK-REVIEW", "status": "open",
                "phase": "final_acceptance", "developer_id": "inactive-delivery",
                "stage": board.INDEPENDENT_REVIEW, "subtask": "", "chunk": "",
                "cycle": 1, "claimed_by": None, "review_wait_started_at": board.now(),
                "requested_at": board.now(),
            }
        self._backdate_cto_poll(board.CTO_MONITOR_INTERVAL_SECONDS + 1)
        board.mark_stalled(self.root)
        self.assertEqual(len(self._monitoring_instructions()), 1)


if __name__ == "__main__":
    unittest.main()
