# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A review assignment must not outlive the reservation it announced (2026-10-16_quota-handoff).

A Reviewer held by a usage limit cannot poll the board, so its reservation can expire while it waits and another reviewer (or the same one
later) takes the request. The wake that announced the old assignment must then never be typed. Three guards: the wake is withdrawn at
expiry, every submission re-checks it, and the board itself refuses a verdict, ledger or intent from anyone but the current owner while
still allowing a fresh reservation of an open request.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, control, interactive_supervisor


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class AssignmentCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.first_session = control.create(self.root, "claude_reviewer")
        self.first = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=self.first_session["id"])
        self.second_session = control.create(self.root, "claude_reviewer")
        self.second = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=self.second_session["id"])
        self.dev_session = control.create(self.root, "codex_delivery")
        self.dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=self.dev_session["id"])
        self.wake = control.enqueue_instruction(self.root, self.first_session["id"], "REVIEW REQUEST ROUTED: R1 for task T", source="review-assignment")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["R1"] = {
                "id": "R1", "task": "TASK-1", "status": "reserved", "developer_id": self.dev["id"], "requested_at": _ago(4000),
                "phase": "final", "subtask": "", "chunk": "", "cycle": 1, "ledger": "ledger.md", "claimed_by": None, "review_wait_started_at": _ago(4000),
                "reserved_by": self.first["id"], "reserved_at": _ago(3600), "routed_to": self.first["id"],
                "routed_session_id": self.first_session["id"], "route_instruction_id": self.wake["id"],
            }

    def request(self) -> dict:
        return board.snapshot(self.root)["qa_requests"]["R1"]

    def owner(self):
        current = self.request()
        return current.get("reserved_by") or current.get("claimed_by")

    def test_the_wake_is_current_while_the_reviewer_owns_the_request(self):
        self.assertTrue(board.review_assignment_is_current(self.root, self.first_session["id"], self.wake["id"]))

    def test_expiry_withdraws_the_queued_wake_so_it_is_never_typed_and_the_request_is_open_again(self):
        released = board.release_expired_review_reservations(self.root, timeout_seconds=60)
        self.assertEqual(len(released), 1)
        self.assertEqual(self.request()["status"], "open")
        self.assertIsNone(self.request().get("routed_to"))
        self.assertEqual(control.instruction_receipt(self.root, self.wake["id"])["status"], "withdrawn")
        self.assertEqual(control.take_instructions(self.root, self.first_session["id"]), [], "nothing left to type after the wait")
        withdrawn = [event for event in board.snapshot(self.root)["events"] if event["kind"] == "review_wake_withdrawn"]
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual(withdrawn[0]["action"], "withdrawn")

    def test_a_wake_already_taken_before_the_expiry_is_dropped_at_the_moment_it_would_be_typed(self):
        taken = control.take_instructions(self.root, self.first_session["id"])
        self.assertEqual([item["id"] for item in taken], [self.wake["id"]])
        board.release_expired_review_reservations(self.root, timeout_seconds=60)     # cannot withdraw what is taken: it is invalidated instead
        self.assertEqual(control.instruction_receipt(self.root, self.wake["id"])["status"], "discarded")
        notes: list = []
        transcript = type("T", (), {"note": lambda _s, text: notes.append(text)})()
        self.assertFalse(interactive_supervisor._assignment_still_current(self.root, self.first_session["id"], taken[0], transcript))
        self.assertTrue(any("withdrawn or reassigned" in note for note in notes))

    def test_a_taken_wake_stays_dropped_after_the_request_is_actually_routed_to_another_reviewer(self):
        """The reviewer's finding on 2193913: once the request was re-routed no request named the old wake, so it became deliverable again."""
        from harness import attention
        taken = control.take_instructions(self.root, self.first_session["id"])
        seen = datetime.now().astimezone()
        control.record_attention(self.root, self.first_session["id"], attention.limit_reason("claude"), limit={
            "seen_at": seen.isoformat(timespec="seconds"), "resets_at": (seen + timedelta(hours=3)).isoformat(timespec="seconds"),
            "kind": "announced", "when_text": "x", "provider": "claude"})            # reviewer A is held, so only B is eligible
        board.release_expired_review_reservations(self.root, timeout_seconds=60)
        routed = board.route_open_reviews(self.root)
        self.assertEqual(self.request()["routed_to"], self.second["id"], str(routed))
        new_wake = self.request()["route_instruction_id"]
        self.assertNotEqual(new_wake, self.wake["id"])
        notes: list = []
        transcript = type("T", (), {"note": lambda _s, text: notes.append(text)})()
        # the old terminal still holds the wake it took before the wait: it must never be typed
        self.assertFalse(interactive_supervisor._assignment_still_current(self.root, self.first_session["id"], taken[0], transcript))
        self.assertEqual(control.instruction_receipt(self.root, self.wake["id"])["status"], "discarded")
        # the new reviewer's own wake is valid, and an unknown wake carried over by a relaunch is still delivered
        mine = control.take_instructions(self.root, self.second_session["id"])
        self.assertIn(new_wake, [item["id"] for item in mine])
        assignment = next(item for item in mine if item["id"] == new_wake)
        self.assertTrue(interactive_supervisor._assignment_still_current(self.root, self.second_session["id"], assignment, transcript))
        carried = control.enqueue_instruction(self.root, self.second_session["id"], "REVIEW REQUEST ROUTED: R9", source="review-assignment")
        self.assertTrue(interactive_supervisor._assignment_still_current(self.root, self.second_session["id"], carried, transcript))

    def test_the_delivery_succeeded_message_for_an_expired_reservation_is_closed_too_whether_queued_or_taken(self):
        queued = control.enqueue_instruction(self.root, self.first_session["id"], board._delivery_succeeded_instruction("R1", "TASK-1"),
                                             source="review-delivery-succeeded")
        taken_one = control.enqueue_instruction(self.root, self.first_session["id"], board._delivery_succeeded_instruction("R1", "TASK-1"),
                                                source="review-delivery-succeeded")
        other_request = control.enqueue_instruction(self.root, self.first_session["id"], board._delivery_succeeded_instruction("R2", "TASK-2"),
                                                    source="review-delivery-succeeded")
        control.take_instructions(self.root, self.first_session["id"])           # all three are taken by the (soon held) supervisor
        control.return_instructions(self.root, self.first_session["id"], [queued])  # one goes back to the queue, as a hold does
        board.release_expired_review_reservations(self.root, timeout_seconds=60)
        self.assertEqual(control.instruction_receipt(self.root, queued["id"])["status"], "discarded")
        self.assertEqual(control.instruction_receipt(self.root, taken_one["id"])["status"], "discarded")
        self.assertEqual(control.instruction_receipt(self.root, other_request["id"])["status"], "taken", "a message about another request is untouched")
        transcript = type("T", (), {"note": lambda _s, text: None})()
        self.assertFalse(interactive_supervisor._assignment_still_current(self.root, self.first_session["id"], taken_one, transcript))
        self.assertTrue(interactive_supervisor._assignment_still_current(self.root, self.first_session["id"], other_request, transcript))

    def test_after_another_reviewer_took_the_request_the_old_reviewers_wake_is_stale_for_it_only(self):
        board.release_expired_review_reservations(self.root, timeout_seconds=60)
        board.reserve_qa(self.root, self.second["id"], "R1")
        self.assertFalse(board.review_assignment_is_current(self.root, self.first_session["id"], self.wake["id"]))
        self.assertEqual(self.owner(), self.second["id"])

    def test_the_same_reviewer_can_reacquire_an_open_request_after_expiry(self):
        board.release_expired_review_reservations(self.root, timeout_seconds=60)
        board.reserve_qa(self.root, self.first["id"], "R1")
        self.assertEqual(self.owner(), self.first["id"])

    def test_a_wake_no_request_names_is_never_dropped_because_nothing_proves_it_stale(self):
        carried = control.enqueue_instruction(self.root, self.first_session["id"], "REVIEW REQUEST ROUTED: R9", source="review-assignment")
        with control.locked_state(self.root) as state:
            state["instruction_receipts"][carried["id"]]["queued_at"] = _ago(3600)
        self.assertTrue(board.review_assignment_is_current(self.root, self.first_session["id"], carried["id"]))

    def test_only_review_assignments_are_checked_and_unknown_ids_never_block_delivery(self):
        notes: list = []
        transcript = type("T", (), {"note": lambda _s, text: notes.append(text)})()
        self.assertTrue(interactive_supervisor._assignment_still_current(self.root, self.first_session["id"], {"id": "x", "source": "owner-message"}, transcript))
        self.assertTrue(interactive_supervisor._assignment_still_current(self.root, self.first_session["id"], {"id": "missing", "source": "review-assignment"}, transcript))
        self.assertEqual(notes, [])

    def test_the_board_refuses_a_stale_verdict_ledger_or_intent_from_a_reviewer_that_no_longer_owns_the_request(self):
        board.release_expired_review_reservations(self.root, timeout_seconds=60)
        board.reserve_qa(self.root, self.second["id"], "R1")
        evidence = board.board_dir(self.root) / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "stale.txt").write_text("python3 -m unittest\nRan 3 tests\nOK\n")
        with self.assertRaisesRegex(ValueError, "only the QA agent that claimed this request may submit its result"):
            board.qa_result(self.root, self.first["id"], "R1", "passed", "stale verdict", str(evidence / "stale.txt"))
        with self.assertRaisesRegex(ValueError, "only the reviewer that reserved this request may attach its Challenge Ledger"):
            board.attach_challenge_ledger(self.root, self.first["id"], "R1", str(evidence / "stale.txt"))
        with self.assertRaisesRegex(ValueError, "only the reviewer holding this reservation may record intents"):
            board.record_review_intents(self.root, self.first["id"], "R1", ["Check that the login page loads for a signed-in user"])


if __name__ == "__main__":
    unittest.main()
