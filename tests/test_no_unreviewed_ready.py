# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-10-24 (owner, priority 1): a build whose final review never passed was put in front of the owner. Studio timeline:
three staged reviews cancelled (19:27-19:30), Delivery wrote "Feature complete and tested ... final review is stuck",
the CTO routed once and then posted "still waiting" for 90 minutes while the Reviewer, with no routed request, was
never climbed by the stall ladder. The board now checks it: no release without a passed final review of that head,
a ready-claim flag, and a clock on a blocked review."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, control

HEAD = "a" * 40


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=control.create(self.root, "claude_cto")["id"])
        self.delivery = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI",
                                       session_id=control.create(self.root, "codex_delivery")["id"])
        self.reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=control.create(self.root, "claude_reviewer")["id"])
        with board.locked_state(self.root) as state:
            state["agents"][self.delivery["id"]].update({"task": "DECK", "active": True})
        configure = getattr(board, "configure_owner_questions", lambda _section: None)
        configure(None)
        self.addCleanup(configure, None)

    def review(self, status: str, commit: str = HEAD, phase: str = "final_acceptance", rid: str = "r-1", cycle: int = 1):
        with board.locked_state(self.root) as state:
            state["qa_requests"][rid] = {
                "id": rid, "task": "DECK", "developer_id": self.delivery["id"], "claimed_by": "", "subtask": "", "chunk": "final", "review_wait_started_at": "", "phase": phase, "status": status, "reviewed_commit": commit, "cycle": cycle,
                "stage": board.INDEPENDENT_REVIEW, "requested_at": board.now(),
            }

    def event(self, kind: str, agent_id: str | None = None, task: str = "DECK", **fields):
        with board.locked_state(self.root) as state:
            agent = state["agents"].get(agent_id) if agent_id else None
            board._event(state, kind, agent, {"task": task, "message": kind, **fields})

    def kinds(self):
        return [event["kind"] for event in board.snapshot(self.root)["events"]]

    def age_blocks(self, minutes: int):
        old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with board.locked_state(self.root) as state:
            for block in state["review_blocks"].values():
                block.update({"since": old, "actioned_at": ""})

    def release(self, task: str = "DECK", head: str = HEAD):
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        checks["head_commit"] = head
        return board.record_release_ready(self.root, self.cto["id"], task, checks)


class ReleaseGuardTests(_Fixture):
    def test_no_release_without_a_passed_final_review_of_that_head(self):
        self.review("failed")
        with self.assertRaisesRegex(ValueError, "no final independent review has passed"):
            self.release()
        self.review("passed", commit="b" * 40, rid="r-2", cycle=2)
        with self.assertRaisesRegex(ValueError, "no final independent review has passed"):
            self.release()   # a PASS on another version is not a PASS of this one
        self.assertNotIn("DECK", board.snapshot(self.root).get("releases", {}))

    def test_a_pass_on_the_head_allows_the_release(self):
        self.review("passed")
        self.assertEqual(self.release()["status"], "VISUAL_TEST_REQUIRED")

    def test_a_newer_failed_review_of_the_same_commit_withdraws_an_older_pass(self):
        self.review("passed", rid="r-1", cycle=1)
        self.review("failed", rid="r-2", cycle=2)   # a check was corrected and the review failed again, with no code change
        with self.assertRaisesRegex(ValueError, "no final independent review has passed"):
            self.release()

    def test_a_git_task_needs_the_reviewed_commit_to_be_named(self):
        self.review("passed", commit="")
        with board.locked_state(self.root) as state:
            state.setdefault("task_repositories", {})["DECK"] = str(self.root)
        with self.assertRaisesRegex(ValueError, "no final independent review has passed"):
            self.release()   # a Git task whose PASS names no commit cannot vouch for this head

    def test_production_refuses_a_release_with_no_plan_and_no_review(self):
        with self.assertRaisesRegex(ValueError, "no final independent review has passed"):
            self.release("NO-PIPELINE")

    def test_only_the_explicit_fixture_switch_exempts_a_bare_task(self):
        from unittest import mock
        with mock.patch.object(board, "RELEASE_FIXTURE_EXEMPT", True):
            self.assertEqual(self.release("NO-PIPELINE")["status"], "VISUAL_TEST_REQUIRED")
        self.assertFalse(board.RELEASE_FIXTURE_EXEMPT, "production default: no exemption")

    def test_the_preview_is_refused_once_the_pass_is_invalidated(self):
        self.review("passed")
        self.release()
        with board.locked_state(self.root) as state:
            state["qa_requests"]["r-1"]["integrity_invalidated"] = True
        with self.assertRaisesRegex(ValueError, "no valid final independent review"):
            board.record_release_preview(self.root, "DECK", {"status": "ready", "head_commit": HEAD})


class ReadyClaimTests(_Fixture):
    def test_delivery_claiming_ready_without_a_final_pass_is_flagged_and_the_cto_must_route(self):
        self.review("failed")
        board.status(self.root, self.delivery["id"], "Feature complete and tested (912 tests); waiting on the CTO to fix the review check", "working")
        self.assertIn("ready_claim_without_review", self.kinds())
        with self.assertRaisesRegex(ValueError, "route the next action"):
            board.status(self.root, self.cto["id"], "reviewing", "waiting")

    def test_the_task_brief_is_checked_too_and_a_real_pass_silences_it(self):
        self.review("failed")
        board.task_brief(self.root, self.delivery["id"], "Build the deck", "Ready for you to test now.")
        self.assertEqual(self.kinds().count("ready_claim_without_review"), 1)
        self.review("passed", rid="r-2", cycle=2)
        before = self.kinds().count("ready_claim_without_review")
        board.task_brief(self.root, self.delivery["id"], "Build the deck", "Feature complete, ready for you to test.")
        self.assertEqual(self.kinds().count("ready_claim_without_review"), before)

    def test_a_plain_complete_status_and_an_invitation_are_flagged(self):
        self.review("failed")
        board.status(self.root, self.delivery["id"], "Implementation is complete. The owner can use the build now.", "working")
        self.assertEqual(self.kinds().count("ready_claim_without_review"), 1)

    def test_an_unrelated_negation_does_not_hide_an_invitation_in_status_or_brief(self):
        self.review("failed")
        board.status(self.root, self.delivery["id"], "The build is ready for you to test and does not require any setup.", "working")
        self.assertEqual(self.kinds().count("ready_claim_without_review"), 1)
        board.task_brief(self.root, self.delivery["id"], "Build the deck", "Ready for you to test; nothing else is needed.")
        self.assertEqual(self.kinds().count("ready_claim_without_review"), 2)

    def test_a_negated_or_pending_ready_is_a_status_not_an_invitation(self):
        self.review("failed")
        for text in ("Not ready for testing; fixing the failing review first.", "Complete once the review passes, not before.",
                     "Implementation done, awaiting the independent review."):
            board.status(self.root, self.delivery["id"], text, "working")
        self.assertNotIn("ready_claim_without_review", self.kinds())

    def test_ordinary_progress_text_is_not_flagged(self):
        self.review("failed")
        board.status(self.root, self.delivery["id"], "Fixing the radio layout; 914 tests green", "working")
        self.assertNotIn("ready_claim_without_review", self.kinds())


class ReviewBlockClockTests(_Fixture):
    def cancelled(self, times: int = 3):
        for _ in range(times):
            self.event("staged_review_cancelled", self.delivery["id"], request_id="r-1")

    def test_the_reviewer_counts_as_having_work_while_a_review_is_blocked(self):
        with board.locked_state(self.root) as state:
            self.assertFalse(board._agent_has_actionable_work(state, state["agents"][self.reviewer["id"]]))
        self.cancelled()
        with board.locked_state(self.root) as state:
            self.assertEqual(state["review_blocks"]["DECK"]["count"], 3)
            self.assertTrue(board._agent_has_actionable_work(state, state["agents"][self.reviewer["id"]]),
                            "the stall ladder can now climb on a silent Reviewer")

    def test_the_studio_timeline_repeated_still_waiting_is_refused_until_the_cto_acts(self):
        self.cancelled()
        # the CTO routed it once (a finding for the task), which used to end the obligation for good
        board.record_finding(self.root, "DECK", "Correct the stale check c1", "the Reviewer corrects it", True)
        self.assertEqual(board.status(self.root, self.cto["id"], "routed qa", "working")["kind"], "status_update")
        self.assertEqual(board.status(self.root, self.cto["id"], "Reviewer hasn't corrected the stale check yet", "waiting")["kind"], "status_update",
                         "inside the grace a waiting status is fine")
        self.age_blocks(20)
        with self.assertRaisesRegex(ValueError, "blocked since"):
            board.status(self.root, self.cto["id"], "Reviewer hasn't corrected the stale check yet", "waiting")
        self.event("agent_restart_requested", self.reviewer["id"], task="REVIEW_QUEUE")   # the Reviewer is relaunched
        self.assertEqual(board.status(self.root, self.cto["id"], "Reviewer relaunched", "waiting")["kind"], "status_update")

    def test_a_waiting_note_with_the_default_state_is_still_waiting(self):
        self.cancelled()
        board.record_finding(self.root, "DECK", "Correct the stale check c1", "the Reviewer corrects it", True)
        self.age_blocks(20)
        with self.assertRaisesRegex(ValueError, "blocked since"):
            board.status(self.root, self.cto["id"], "Still waiting for the Reviewer to correct the check", "working")
        self.assertEqual(board.status(self.root, self.cto["id"], "Restarted the Reviewer from Mission Control; checking its next poll", "working")["kind"], "status_update")

    def test_an_archived_reviewer_still_has_the_blocked_work(self):
        from harness import self_heal
        self.cancelled()
        with board.locked_state(self.root) as state:
            self.assertTrue(self_heal._work_remains(state, "qa", "REVIEW_QUEUE", self.reviewer["id"]))
        self.event("qa_result", self.reviewer["id"], result="passed")
        with board.locked_state(self.root) as state:
            self.assertFalse(self_heal._work_remains(state, "qa", "REVIEW_QUEUE", self.reviewer["id"]))

    def test_the_real_studio_timeline_with_the_cto_always_saying_working(self):
        """Studio, 2026-10-24: the CTO's status lines were all state "working" and kept saying the Reviewer had not corrected the
        stale check; the Reviewer, with no routed request, was never climbed. Replayed with the real wording."""
        self.cancelled()                                    # 19:27, 19:28, 19:30 staged reviews cancelled
        working = lambda text: board.status(self.root, self.cto["id"], text, "working")
        # 19:28 (inside the grace): fine
        self.assertEqual(working("fit-and-spec-reintegrate: still awaiting Accept. briefs-build: final review-01 blocked - QA evidence reported zero tests executed, staged reviewer run cancelled. Just happened, watching for recurrence next cycle.")["kind"], "status_update")
        self.age_blocks(8)                                  # 19:34 onwards: past the grace, no board action was taken
        for text in (
            'briefs-build: root cause found for c1 "Ran 0 tests" - stale execution, not Delivery\'s code (14/14 pass locally). Only the Reviewer can fix via contract-check-correct. Routing qa-0033 to verify + correct the check.',
            "fit-and-spec-reintegrate: still awaiting Accept. briefs-build: Reviewer hasn't corrected the stale check yet, Engineering correctly standing by, not retrying. No other blockers.",
            "fit-and-spec-reintegrate: still awaiting Accept. briefs-build: UI bug fixed and verified (913 tests green, 683046b). One new screen-script timing failure, watching. Stale-check correction still pending.",
            "fit-and-spec-reintegrate: still awaiting Accept. briefs-build: UI fix confirmed (913 tests), test server briefly dropped mid-owner-test then restarted - flagging to owner. Stale check still pending Reviewer.",
        ):
            with self.subTest(text=text[:50]), self.assertRaisesRegex(ValueError, "blocked since"):
                working(text)
        self.assertEqual(self.kinds().count("review_blocked_too_long"), 1, "ONE flag per block, not one per refused note")
        # the CTO acts (the Reviewer is restarted): the note that says so is accepted and the clock restarts
        self.event("agent_restart_requested", self.reviewer["id"], task="REVIEW_QUEUE")
        self.assertEqual(working("briefs-build: Reviewer qa-0033 relaunched from Mission Control; will check its next poll")["kind"], "status_update")

    def test_an_interrupted_execution_is_not_a_block(self):
        with board.locked_state(self.root) as state:
            board._event(state, "staged_review_cancelled", state["agents"][self.delivery["id"]],
                         {"task": "DECK", "request_id": "r-1", "message": "Delivery evidence did not run (the owner's machine was busy); no test failed, ask for the review again"})
            self.assertNotIn("DECK", state.get("review_blocks", {}))

    def test_a_verdict_for_another_scope_does_not_close_the_block(self):
        self.review("failed", phase="final_acceptance", rid="r-1")
        self.review("passed", phase="chunk", rid="r-chunk", cycle=1)
        self.cancelled(1)
        with board.locked_state(self.root) as state:
            state["review_blocks"]["DECK"].update({"phase": "final_acceptance", "chunk": "final"})
        self.event("qa_result", self.reviewer["id"], request_id="r-chunk", result="passed")
        with board.locked_state(self.root) as state:
            self.assertIn("DECK", state["review_blocks"])
        self.event("qa_result", self.reviewer["id"], request_id="r-1", result="passed")
        with board.locked_state(self.root) as state:
            self.assertNotIn("DECK", state["review_blocks"])

    def test_a_paused_project_holds_the_clock(self):
        self.cancelled(1)
        self.age_blocks(30)
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "paused"}
            self.assertEqual([f for f in board._unrouted_failures(state) if f["kind"] == "review_blocked"], [])

    def test_the_whole_arc_cancel_wait_recover_correct_pass_release(self):
        self.review("failed")
        self.cancelled()                                    # three staged reviews cancelled
        with self.assertRaisesRegex(ValueError, "no final independent review"):
            self.release()                                  # the owner is not invited
        board.record_finding(self.root, "DECK", "Correct the stale check", "the Reviewer corrects it", True)   # the CTO's one route
        self.age_blocks(20)
        with self.assertRaisesRegex(ValueError, "blocked since"):
            board.status(self.root, self.cto["id"], "Reviewer hasn't corrected it yet", "waiting")
        self.event("agent_restart_requested", self.reviewer["id"], task="REVIEW_QUEUE")
        self.event("contract_check_corrected", self.reviewer["id"])        # the Reviewer corrected the check
        with board.locked_state(self.root) as state:
            self.assertNotIn("DECK", state.get("review_blocks", {}))
        with self.assertRaisesRegex(ValueError, "no final independent review"):
            self.release()                                  # still no PASS: correction alone is not a pass
        self.review("passed", rid="r-2", cycle=2)           # the retried final review passes on the head
        with board.locked_state(self.root) as state:
            state["deferred_findings"] = {}                 # the CTO's one routed finding is settled
        self.assertEqual(self.release()["status"], "VISUAL_TEST_REQUIRED")

    def test_a_verdict_or_completion_closes_the_block(self):
        self.cancelled(1)
        self.age_blocks(20)
        self.event("qa_result", self.reviewer["id"], result="passed")
        with board.locked_state(self.root) as state:
            self.assertNotIn("DECK", state.get("review_blocks", {}))
            self.assertFalse(board._agent_has_actionable_work(state, state["agents"][self.reviewer["id"]]))
        self.assertEqual(board.status(self.root, self.cto["id"], "standing by", "waiting")["kind"], "status_update")


if __name__ == "__main__":
    unittest.main()
