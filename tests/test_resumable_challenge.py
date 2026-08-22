# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Interrupted certified challenges resume; they are not restarted.

Run: PYTHONPATH=. python3 -m unittest tests.test_resumable_challenge -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, control


def _old_time(seconds: int = 600) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class InterruptionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        session = control.create(self.root, "claude_reviewer")
        self.reviewer = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
            session_id=session["id"],
        )

    def seed_claimed_request(self, *, reviewer_active: bool = True):
        with board.locked_state(self.root) as state:
            reviewer = state["agents"][self.reviewer["id"]]
            reviewer["active"] = reviewer_active
            # The execution lease expired long ago: the terminal died mid-run.
            reviewer["review_execution"] = {
                "active": True, "request_id": "review-01",
                "command": "challenge execution",
                "started_at": _old_time(700), "last_heartbeat_at": _old_time(650),
                "finished_at": None, "result": None,
            }
            state.setdefault("qa_requests", {})["review-01"] = {
                "id": "review-01", "task": "TASK-R", "phase": "final_acceptance",
                "status": "claimed", "claimed_by": self.reviewer["id"],
                "claimed_at": _old_time(700), "requested_at": _old_time(750),
                "cycle": 1, "stage": "independent_review", "subtask": "",
                "chunk": "final", "developer_id": "engineering-0001-x",
                "review_wait_started_at": _old_time(700),
                "challenge_ledger": "reviews/challenge.md",
                "challenge_ledger_sha256": "a" * 64,
                "reviewer_initial_intents": [{"id": "s1"}],
            }

    def request(self) -> dict:
        return board.snapshot(self.root)["qa_requests"]["review-01"]


class RecoveryKeepsTheClaimTests(InterruptionFixture):
    def test_dead_terminal_with_durable_reviewer_keeps_claim_ledger_and_intents(self):
        # No live managed session exists at all — exactly the pause/restart
        # window that used to abandon the ledger and void every certified
        # scenario through a forced re-authoring.
        self.seed_claimed_request(reviewer_active=True)
        recovered = board.recover_interrupted_executions(self.root)
        self.assertEqual(len(recovered), 1)
        request = self.request()
        self.assertEqual(request["status"], "claimed")
        self.assertEqual(request["claimed_by"], self.reviewer["id"])
        self.assertEqual(request["challenge_ledger"], "reviews/challenge.md")
        self.assertEqual(request["challenge_ledger_sha256"], "a" * 64)
        self.assertEqual(request["route_state"], "challenge_interrupted_retry_required")
        attempt = request["challenge_execution_attempts"][-1]
        self.assertEqual(attempt["status"], "interrupted")
        self.assertEqual(attempt["cause"], "system_interruption")
        self.assertIn("completed certified scenarios are retained", recovered[0]["message"])

    def test_retired_reviewer_still_releases_the_request_for_reassignment(self):
        self.seed_claimed_request(reviewer_active=False)
        board.recover_interrupted_executions(self.root)
        request = self.request()
        self.assertEqual(request["status"], "open")
        self.assertIsNone(request["claimed_by"])
        self.assertIsNone(request["challenge_ledger"])
        self.assertTrue(request.get("abandoned_challenge_ledgers"))

    def test_recovery_is_recorded_once_not_looped(self):
        self.seed_claimed_request()
        board.recover_interrupted_executions(self.root)
        board.recover_interrupted_executions(self.root)
        request = self.request()
        interrupted = [
            attempt for attempt in request["challenge_execution_attempts"]
            if attempt["status"] == "interrupted"
        ]
        self.assertEqual(len(interrupted), 1)


class RetryReasonGateTests(unittest.TestCase):
    def test_system_interruption_resumes_without_a_written_reason(self):
        attempts = [{
            "status": "interrupted", "recorded_at": "2026-08-20T10:00:00+00:00",
            "reason": "Reviewer execution heartbeat expired before certification completed.",
        }]
        reason = board._challenge_retry_reason(attempts, "")
        self.assertIn("resumed after recorded system interruption", reason)
        self.assertIn("2026-08-20T10:00:00+00:00", reason)

    def test_genuine_failure_still_requires_the_reviewer_reason(self):
        attempts = [{"status": "failed", "recorded_at": "2026-08-20T10:00:00+00:00"}]
        with self.assertRaisesRegex(ValueError, "non-empty repair reason"):
            board._challenge_retry_reason(attempts, "")
        self.assertEqual(board._challenge_retry_reason(attempts, "fixed the fixture"), "fixed the fixture")

    def test_first_attempt_and_passed_history_need_no_reason(self):
        self.assertEqual(board._challenge_retry_reason([], ""), "")
        self.assertEqual(board._challenge_retry_reason([{"status": "passed"}], ""), "")

    def test_reviewer_supplied_reason_always_wins(self):
        attempts = [{"status": "interrupted", "recorded_at": "t"}]
        self.assertEqual(board._challenge_retry_reason(attempts, "my own words"), "my own words")


if __name__ == "__main__":
    unittest.main()
