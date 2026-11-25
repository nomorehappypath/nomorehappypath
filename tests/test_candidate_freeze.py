# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
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
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-final-01"]["status"] = "open"
        with self.assertRaisesRegex(ValueError, "not frozen"):
            board.reopen_candidate_scope(
                self.root, self.cto["id"], "TASK-F",
                "there is nothing frozen here to reopen at all",
            )


class ReopenReachesTheBuilderTests(FreezeFixture):
    """2026-11-17: the CTO reopened a passed task; the builder that replaced the stopped one saw only "final review passed" and waited."""

    REASON = "Spec 3.1 Accept refusal was left out, and the Linux tests must run for real on the final candidate."

    def reopen(self):
        self.seed_final("passed", completed_at="2026-08-20T10:00:00+00:00")
        board.reopen_candidate_scope(self.root, self.cto["id"], "TASK-F", self.REASON)

    def replacement(self):
        with board.locked_state(self.root) as state:
            state["agents"][self.developer["id"]]["active"] = False
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.resume_task(self.root, agent["id"], self.developer["id"], "TASK-F")
        return agent

    def test_the_running_builder_and_a_later_replacement_both_read_the_work_owed(self):
        self.assertNotIn("reopened_scope", board.poll(self.root, self.developer["id"]))
        self.reopen()
        owed = board.poll(self.root, self.developer["id"])["reopened_scope"]
        self.assertEqual(owed["reason"], self.REASON)
        self.assertIn("request the final review again", owed["what_to_do"])
        replacement = self.replacement()  # registered after the reopen: its cursor starts past the reopen event
        result = board.poll(self.root, replacement["id"])
        self.assertNotIn("candidate_scope_reopened", [event.get("kind") for event in result["events"]])
        self.assertEqual(result["reopened_scope"]["reason"], self.REASON)
        # the task summary no longer tells it to wait for the CTO's release step
        next_action = result["context_bundle"]["tasks"][0]["next_action"]
        self.assertIn("Delivery does the work the CTO reopened", next_action)
        self.assertNotIn("release verification", next_action)

    def test_a_final_review_asked_for_after_the_reopen_settles_it(self):
        self.reopen()
        self.seed_final("open", completed_at="2099-01-01T00:00:00+00:00", request_id="review-final-02")
        self.assertNotIn("reopened_scope", board.poll(self.root, self.developer["id"]))

    def test_a_chunk_review_after_the_reopen_does_not_settle_it(self):
        self.reopen()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-chunk-02"] = {
                "id": "review-chunk-02", "task": "TASK-F", "phase": "chunk", "status": "passed", "chunk": "c2", "cycle": 1,
                "stage": "independent_review", "subtask": "", "developer_id": self.developer["id"], "claimed_by": None, "review_wait_started_at": "2099-01-01T00:00:00+00:00",
                "requested_at": "2099-01-01T00:00:00+00:00", "completed_at": "2099-01-01T00:10:00+00:00"}
        replacement = self.replacement()
        self.assertEqual(board.poll(self.root, replacement["id"])["reopened_scope"]["reason"], self.REASON)

    def test_a_final_review_trimmed_by_cleanup_still_settles_it(self):
        # cleanup moves finished requests out of qa_requests into qa_request_index; the old reason must not come back
        self.reopen()
        with board.locked_state(self.root) as state:
            state.setdefault("qa_request_index", {})["review-final-02"] = {
                "id": "review-final-02", "task": "TASK-F", "phase": "final_acceptance", "status": "passed",
                "requested_at": "2099-01-01T00:00:00+00:00", "completed_at": "2099-01-01T00:10:00+00:00"}
        replacement = self.replacement()
        self.assertNotIn("reopened_scope", board.poll(self.root, replacement["id"]))

    def test_a_second_reopen_after_a_new_pass_carries_its_own_reason(self):
        self.reopen()
        with board.locked_state(self.root) as state:  # the first reopen, then a later final review that passed
            state["candidate_scope_reopens"]["TASK-F"]["recorded_at"] = "2026-08-20T10:30:00+00:00"
        self.seed_final("passed", completed_at="2026-08-21T10:00:00+00:00", request_id="review-final-02")
        self.assertNotIn("reopened_scope", board.poll(self.root, self.developer["id"]))
        second = "The deployed probe found the saved-data check still accepts a stale base commit."
        board.reopen_candidate_scope(self.root, self.cto["id"], "TASK-F", second)
        self.assertEqual(board.poll(self.root, self.developer["id"])["reopened_scope"]["reason"], second)

    def test_an_engineering_builder_reads_it_too(self):
        self.reopen()
        with board.locked_state(self.root) as state:
            state["agents"][self.developer["id"]]["active"] = False
        session = control.create(self.root, "codex_delivery")
        engineer = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.resume_task(self.root, engineer["id"], self.developer["id"], "TASK-F")
        self.assertEqual(board.poll(self.root, engineer["id"])["reopened_scope"]["reason"], self.REASON)

    def test_nobody_else_and_no_settled_task_is_told(self):
        self.reopen()
        self.assertNotIn("reopened_scope", board.poll(self.root, self.cto["id"]))
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=control.create(self.root, "claude_reviewer")["id"])
        self.assertNotIn("reopened_scope", board.poll(self.root, reviewer["id"]))
        with board.locked_state(self.root) as state:
            state["candidate_scope_reopens"]["OTHER-TASK"] = state["candidate_scope_reopens"].pop("TASK-F")
        self.assertNotIn("reopened_scope", board.poll(self.root, self.developer["id"]))
        with board.locked_state(self.root) as state:
            state["candidate_scope_reopens"]["TASK-F"] = state["candidate_scope_reopens"].pop("OTHER-TASK")
            state.setdefault("git_acceptances", {})["TASK-F"] = {"task": "TASK-F"}
        self.assertNotIn("reopened_scope", board.poll(self.root, self.developer["id"]))
        with board.locked_state(self.root) as state:
            state["git_acceptances"].pop("TASK-F")
        self.assertIn("reopened_scope", board.poll(self.root, self.developer["id"]))
        board.cancel_task(self.root, "TASK-F")
        with board.locked_state(self.root) as state:
            self.assertIsNone(board._reopened_scope_owed(state, "TASK-F"))


if __name__ == "__main__":
    unittest.main()
