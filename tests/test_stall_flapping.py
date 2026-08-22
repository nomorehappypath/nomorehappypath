# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The recovery state machine must not flap.

A genuinely dead agent used to oscillate forever: automatic wake-up requested ->
(grace) -> marked stalled/automatic_failed -> the very next watchdog cycle
re-requested a wake-up -> recovering -> (grace) -> stalled -> ... Every swing
emitted an `agent_automatic_recovery_routed` event and re-queued a terminal
nudge, and the agent's liveness visibly bounced stalled<->recovering. The
existing tests missed it because `mark_stalled` only RETURNS `agent_stalled`
events, so the re-request churn (a different event kind) was invisible.

Two fixes, tested here:
  * a failed automatic recovery holds the agent visibly STALLED and retries at most
    once per AUTO_RECOVERY_RETRY_SECONDS — never every cycle (no flapping);
  * a real board heartbeat via a progress post (not only via poll) clears the
    automatic-recovery bookkeeping, so the agent's NEXT stall is handled fresh
    instead of being skipped with a stale request timestamp.

Run:  PYTHONPATH=. python3 -m unittest tests.test_stall_flapping -v
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, contract, control


class StallFlapping(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _delivery(self, task: str):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION,
                               vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"OWNER DIRECTION — {task}")
        board.begin_task(self.root, agent["id"], task)
        contract.create_contract(self.root, task, f"OWNER DIRECTION — {task}", ["delivery"])
        return agent

    def _routed_count(self) -> int:
        return sum(event["kind"] == "agent_automatic_recovery_routed"
                   for event in board.snapshot(self.root)["events"])

    def _liveness(self, agent_id: str) -> str:
        return board.snapshot(self.root)["agents"][agent_id]["liveness"]

    def _backdate_heartbeats(self, agent_id: str, seconds: int = 301):
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][agent_id].update({
                "spawned_at": old, "last_poll_at": old, "last_progress_at": old,
            })

    def _drive_to_automatic_failed(self, agent_id: str):
        self._backdate_heartbeats(agent_id)
        board.mark_stalled(self.root)  # -> automatic_requested + wake-up
        with board.locked_state(self.root) as state:
            state["agents"][agent_id]["automatic_recovery_requested_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=board.AUTO_RECOVERY_GRACE_SECONDS + 1)
            ).isoformat()
        failed = board.mark_stalled(self.root)  # -> automatic_failed
        self.assertEqual(len(failed), 1)
        self.assertEqual(self._liveness(agent_id), "stalled")

    # ---- S-STALL-001 (load-bearing: no flapping after a failed recovery) ----
    def test_failed_recovery_does_not_flap(self):
        dev = self._delivery("TASK-FLAP")
        self._drive_to_automatic_failed(dev["id"])
        control.take_instructions(self.root, dev["session_id"])  # drain the grace-period wake-up
        routed_before = self._routed_count()

        # Many watchdog cycles inside the retry backoff window.
        for _ in range(8):
            self.assertEqual(board.mark_stalled(self.root), [],
                             "a new agent_stalled fired while already stalled")

        self.assertEqual(self._routed_count(), routed_before,
                         "the agent was re-woken during the backoff window — this is the flapping")
        self.assertEqual(self._liveness(dev["id"]), "stalled",
                         "liveness oscillated away from stalled — this is the flapping")
        self.assertEqual(control.take_instructions(self.root, dev["session_id"]), [],
                         "repeated wake-up nudges were queued — this is the flapping")

    def test_live_agent_is_not_declared_stalled_after_only_one_minute(self):
        dev = self._delivery("TASK-NORMAL-TURN")
        self._backdate_heartbeats(dev["id"])
        board.mark_stalled(self.root)
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]]["automatic_recovery_requested_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=61)
            ).isoformat()

        self.assertEqual(board.mark_stalled(self.root), [])
        current = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(current["liveness"], "recovering")
        self.assertEqual(current["recovery_state"], "automatic_requested")
        self.assertGreater(board.AUTO_RECOVERY_GRACE_SECONDS, 60)

        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]]["automatic_recovery_requested_at"] = (
                datetime.now(timezone.utc)
                - timedelta(seconds=board.AUTO_RECOVERY_GRACE_SECONDS + 1)
            ).isoformat()
        self.assertEqual(len(board.mark_stalled(self.root)), 1)
        self.assertEqual(
            board.snapshot(self.root)["agents"][dev["id"]]["liveness"],
            "stalled",
        )

    # ---- S-STALL-002 (retry once, only after the backoff window) ----
    def test_failed_recovery_retries_once_after_backoff(self):
        dev = self._delivery("TASK-RETRY")
        self._drive_to_automatic_failed(dev["id"])
        routed_before = self._routed_count()

        board.mark_stalled(self.root)  # inside backoff -> no retry
        self.assertEqual(self._routed_count(), routed_before)
        self.assertEqual(self._liveness(dev["id"]), "stalled")

        # Age the last attempt past the retry backoff.
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]]["automatic_recovery_requested_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=board.AUTO_RECOVERY_RETRY_SECONDS + 1)
            ).isoformat()
        board.mark_stalled(self.root)  # backoff elapsed -> exactly one retry
        self.assertEqual(self._routed_count(), routed_before + 1, "backoff did not permit a single retry")
        after = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(after["liveness"], "recovering")
        self.assertEqual(after["recovery_state"], "automatic_requested")

    # ---- S-STALL-003 (defect-2: progress recovery clears the automatic bookkeeping) ----
    def test_status_post_clears_automatic_recovery_state(self):
        dev = self._delivery("TASK-STATUS-RECOVER")
        self._drive_to_automatic_failed(dev["id"])

        board.status(self.root, dev["id"], "resuming the saved work after the wake-up")
        after = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(after["liveness"], "healthy")
        self.assertEqual(after["recovery_state"], "resumed",
                         "recovery_state stayed stuck after a progress recovery")
        self.assertNotIn("automatic_recovery_requested_at", after)

        # A brand-new stall must now be handled fresh (a proper new wake-up), not
        # silently skipped because of a stale request timestamp.
        routed_before = self._routed_count()
        self._backdate_heartbeats(dev["id"])
        board.mark_stalled(self.root)
        self.assertEqual(self._routed_count(), routed_before + 1)
        self.assertEqual(self._liveness(dev["id"]), "recovering")

    # ---- S-STALL-005 (a reporting agent is not shown as stalled: the task_brief kind) ----
    def test_posting_a_brief_clears_a_false_stall(self):
        dev = self._delivery("TASK-BRIEF-ALIVE")
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]].update({
                "liveness": "stalled", "recovery_state": "automatic_failed",
                "automatic_recovery_requested_at": datetime.now(timezone.utc).isoformat(),
            })
        # A delivery agent posting its plain-language brief IS a liveness proof.
        board.task_brief(self.root, dev["id"], "Continuing the delivery.", "Wired the change; running checks.")
        after = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(after["liveness"], "healthy",
                         "an agent posting its brief was still shown as stalled")
        self.assertEqual(after["recovery_state"], "resumed")

    # ---- S-STALL-004 (guard: owner reset_requested is NOT cleared by the automatic fix) ----
    def test_progress_does_not_clear_owner_reset_requested(self):
        dev = self._delivery("TASK-OWNER-RESET")
        with board.locked_state(self.root) as state:
            state["agents"][dev["id"]].update({"liveness": "recovering", "recovery_state": "reset_requested"})
        board.status(self.root, dev["id"], "posting progress during an owner reset")
        after = board.snapshot(self.root)["agents"][dev["id"]]
        self.assertEqual(after["liveness"], "healthy")
        self.assertEqual(after["recovery_state"], "reset_requested",
                         "an owner-initiated reset must persist until the agent polls")

    def test_active_review_authoring_lease_prevents_false_stall_wake(self):
        session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(
            self.root, "qa", "TASK-REVIEW", vendor="Anthropic",
            session_id=session["id"],
        )
        old = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][reviewer["id"]].update({
                "spawned_at": old, "last_poll_at": old, "last_progress_at": old,
            })
            state["qa_requests"]["review"] = {
                "id": "review", "task": "TASK-REVIEW", "status": "reserved",
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "cycle": 2, "developer_id": "delivery",
                "requested_at": old,
                "reserved_by": reviewer["id"], "reserved_at": fresh,
                "routed_to": reviewer["id"],
                "claimed_by": None, "review_wait_started_at": old,
                "authoring_last_activity_at": fresh,
            }
        board.mark_stalled(self.root)
        state = board.snapshot(self.root)
        self.assertEqual(state["agents"][reviewer["id"]]["liveness"], "healthy")
        self.assertEqual(control.take_instructions(self.root, session["id"]), [])

    def test_reviewer_poll_starts_bounded_authoring_liveness_lease(self):
        session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(
            self.root, "qa", "TASK-REVIEW", vendor="Anthropic",
            session_id=session["id"],
        )
        old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=board.REVIEW_RESERVATION_SECONDS + 1)
        ).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        with board.locked_state(self.root) as state:
            state["agents"][reviewer["id"]].update({
                "spawned_at": old, "last_poll_at": fresh, "last_progress_at": old,
            })
            state["qa_requests"]["review"] = {
                "id": "review", "task": "TASK-REVIEW", "status": "reserved",
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "cycle": 2, "developer_id": "delivery", "requested_at": old,
                "reserved_by": reviewer["id"], "routed_to": reviewer["id"],
                "reserved_at": old, "authoring_last_activity_at": old,
                "claimed_by": None, "review_wait_started_at": old,
            }
        board.mark_stalled(self.root)
        self.assertEqual(
            board.snapshot(self.root)["agents"][reviewer["id"]]["liveness"],
            "healthy",
        )
        self.assertEqual(control.take_instructions(self.root, session["id"]), [])

        with board.locked_state(self.root) as state:
            state["agents"][reviewer["id"]]["last_poll_at"] = old
        board.mark_stalled(self.root)
        current = board.snapshot(self.root)["agents"][reviewer["id"]]
        self.assertEqual(current["liveness"], "recovering")
        self.assertEqual(len(control.take_instructions(self.root, session["id"])), 1)

    def test_status_prose_cannot_keep_abandoned_authoring_reserved(self):
        session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(
            self.root, "qa", "TASK-REVIEW", vendor="Anthropic",
            session_id=session["id"],
        )
        old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=board.REVIEW_RESERVATION_SECONDS + 1)
        ).isoformat()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review"] = {
                "id": "review", "task": "TASK-REVIEW", "status": "reserved",
                "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance",
                "cycle": 2, "developer_id": "delivery",
                "requested_at": old,
                "reserved_by": reviewer["id"], "reserved_at": old,
                "routed_to": reviewer["id"],
                "claimed_by": None, "review_wait_started_at": old,
                "authoring_last_activity_at": old,
            }
        board.status(self.root, reviewer["id"], "Still working on the review.")
        released = board.release_expired_review_reservations(self.root)
        self.assertEqual(len(released), 1)
        self.assertEqual(
            board.snapshot(self.root)["qa_requests"]["review"]["status"], "open",
        )


if __name__ == "__main__":
    unittest.main()
