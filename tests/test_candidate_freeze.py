# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Candidate freeze: a passed final acceptance locks the task branch.

Run: PYTHONPATH=. python3 -m unittest tests.test_candidate_freeze -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import board, contract, control


class FreezeFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        session = control.create(self.root, "codex_delivery")
        self.developer = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Build the feature.")
        board.begin_task(self.root, self.developer["id"], "TASK-F")
        contract.create_contract(self.root, "TASK-F", "Build the feature.", ["It works"])
        cto_session = control.create(self.root, "claude_cto")
        self.cto = board.register(
            self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic",
            session_id=cto_session["id"],
        )

    def seed_final(self, status: str, *, invalidated: bool = False,
                   completed_at: str = "2026-08-20T10:00:00+00:00",
                   request_id: str = "review-final-01"):
        with board.locked_state(self.root) as state:
            state.setdefault("task_repositories", {})["TASK-F"] = "/tmp/repo"
            state.setdefault("qa_requests", {})[request_id] = {
                "id": request_id, "task": "TASK-F", "phase": "final_acceptance",
                "status": status, "completed_at": completed_at,
                "requested_at": completed_at, "cycle": 1, "stage": "independent_review",
                "subtask": "", "chunk": "final", "developer_id": self.developer["id"],
                "claimed_by": None, "review_wait_started_at": completed_at,
                **({"integrity_invalidated": True} if invalidated else {}),
            }

    def freeze_reason(self) -> str:
        return board._candidate_freeze_reason(board.snapshot(self.root), "TASK-F")

    def attempt_commit(self):
        return board.broker_stage_commit(
            self.root, self.developer["id"], ["harness/app.py"], "another change",
        )


class FreezeRuleTests(FreezeFixture):
    def test_no_final_acceptance_means_no_freeze(self):
        self.seed_final("open")
        self.assertEqual(self.freeze_reason(), "")

    def test_passed_final_acceptance_freezes_the_candidate(self):
        self.seed_final("passed")
        self.assertIn("frozen", self.freeze_reason())
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.attempt_commit()

    def test_commit_refusal_names_the_release_valves(self):
        self.seed_final("passed")
        with self.assertRaises(ValueError) as raised:
            self.attempt_commit()
        message = str(raised.exception)
        self.assertIn("failed product review", message)
        self.assertIn("owner rejection", message)
        self.assertIn("reopen-candidate-scope", message)

    def test_genuinely_failed_review_lifts_the_freeze(self):
        self.seed_final("passed", completed_at="2026-08-20T10:00:00+00:00")
        self.seed_final(
            "failed", completed_at="2026-08-20T11:00:00+00:00",
            request_id="review-final-02",
        )
        self.assertEqual(self.freeze_reason(), "")

    def test_invalidated_pass_keeps_the_freeze(self):
        self.seed_final(
            "failed", invalidated=True,
            completed_at="2026-08-20T11:00:00+00:00",
        )
        reason = self.freeze_reason()
        self.assertIn("invalidated", reason)
        self.assertIn("Re-request the review", reason)
        with self.assertRaisesRegex(ValueError, "invalidated"):
            self.attempt_commit()

    def test_owner_rejection_repair_lifts_the_freeze(self):
        self.seed_final("passed")
        with board.locked_state(self.root) as state:
            state.setdefault("release_repairs", {})["TASK-F"] = {
                "status": "DELIVERY_REPAIR_IN_PROGRESS",
            }
        self.assertEqual(self.freeze_reason(), "")

    def test_environment_failure_never_appears_as_an_exception(self):
        # A control-plane hold (the release coordinator's failure record) does
        # not lift the freeze: environment problems are not product defects.
        self.seed_final("passed")
        with board.locked_state(self.root) as state:
            state.setdefault("control_plane_holds", {})["TASK-F"] = {
                "step": "release_coordinator:checks", "reason": "runtime probe failed",
            }
        self.assertIn("frozen", self.freeze_reason())


class ReopenScopeTests(FreezeFixture):
    def test_cto_reopen_lifts_the_freeze_once_and_is_event_logged(self):
        self.seed_final("passed", completed_at="2026-08-20T10:00:00+00:00")
        record = board.reopen_candidate_scope(
            self.root, self.cto["id"], "TASK-F",
            "The deployed probe exposed a real product defect in the settings save path.",
        )
        self.assertEqual(record["task"], "TASK-F")
        self.assertEqual(self.freeze_reason(), "")
        events = [
            event for event in board.snapshot(self.root).get("events", [])
            if event.get("kind") == "candidate_scope_reopened"
        ]
        self.assertEqual(len(events), 1)
        # A newer PASS supersedes the reopen: the freeze re-engages. The
        # timestamp must postdate the reopen record, which is stamped now.
        self.seed_final(
            "passed", completed_at="2099-01-01T00:00:00+00:00",
            request_id="review-final-03",
        )
        self.assertIn("frozen", self.freeze_reason())

    def test_reopen_requires_cto_role_a_real_reason_and_an_actual_freeze(self):
        self.seed_final("passed")
        with self.assertRaisesRegex(ValueError, "only the CTO"):
            board.reopen_candidate_scope(
                self.root, self.developer["id"], "TASK-F",
                "delivery must not lift its own freeze ever",
            )
        with self.assertRaisesRegex(ValueError, "at least 20 characters"):
            board.reopen_candidate_scope(self.root, self.cto["id"], "TASK-F", "because")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-final-01"]["status"] = "open"
        with self.assertRaisesRegex(ValueError, "not frozen"):
            board.reopen_candidate_scope(
                self.root, self.cto["id"], "TASK-F",
                "there is nothing frozen here to reopen at all",
            )


if __name__ == "__main__":
    unittest.main()
