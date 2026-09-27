# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Backlog #6 (2026-09-27): a Reviewer stayed bound to a finished task, so the
next task's review waited for "a live eligible reviewer" while that healthy
Reviewer idled, and the owner had to start a new Reviewer for every task.

Now a Reviewer is returned to the shared review queue when its task is
released or accepted (one plain line), an open review is routed to it, a
Reviewer left bound to a finished task by older state is still eligible, a
Reviewer busy on another task is untouched, and the cross-vendor rule holds.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from harness import board, control
from tests import test_board as _board_tests


class _Fixture(unittest.TestCase):
    setUp = _board_tests.BoardTests.setUp
    tearDown = _board_tests.BoardTests.tearDown
    ledger = _board_tests.BoardTests.ledger
    qa_command = _board_tests.BoardTests.qa_command
    declare_chunks = _board_tests.BoardTests.declare_chunks
    delivery = _board_tests.BoardTests.delivery

    def reviewer(self, bound_to: str, vendor: str = "Anthropic"):
        session = control.create(self.root, "claude_reviewer")
        agent = board.register(self.root, "qa", "REVIEW_QUEUE", vendor=vendor, session_id=session["id"])
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"task": bound_to, "status": "waiting"})
        control.take_instructions(self.root, session["id"])
        return session, agent

    def cto(self):
        session = control.create(self.root, "claude_cto")
        return board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])

    def open_review(self, task: str):
        dev = self.delivery(task)
        self.declare_chunks(dev["id"], [("one", "one reviewable outcome")])
        return board.request_review(
            self.root, dev["id"], self.ledger(f"{task.lower()}-review.md"), f"review {task}",
            chunk="one", test_command=self.qa_command(),
        )

    def agent(self, agent_id: str) -> dict:
        return board.snapshot(self.root)["agents"][agent_id]

    def returned_events(self) -> list[str]:
        return [e["message"] for e in board.snapshot(self.root)["events"] if e["kind"] == "reviewer_returned_to_queue"]


class ReleaseTests(_Fixture):
    def test_a_released_task_returns_its_reviewer_and_the_waiting_review_is_routed_to_it(self):
        self.delivery("FINISHED")
        session, reviewer = self.reviewer("FINISHED")
        cto = self.cto()
        waiting = self.open_review("NEXT")
        self.assertNotEqual(board.snapshot(self.root)["qa_requests"][waiting["id"]].get("routed_to"), reviewer["id"],
                            "while FINISHED is still in progress its Reviewer is not offered other work")
        board.record_release_ready(self.root, cto["id"], "FINISHED", {key: True for key in board.RELEASE_REQUIRED_CHECKS})
        self.assertEqual(self.returned_events(), ["Reviewer returned to the review queue after FINISHED was released."])
        # returned to the queue, then immediately given the waiting review
        self.assertEqual(self.agent(reviewer["id"])["task"], "NEXT")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][waiting["id"]]["routed_to"], reviewer["id"])
        self.assertEqual(len(control.take_instructions(self.root, session["id"])), 1, "the Reviewer is woken for the review")


class AcceptanceTests(_Fixture):
    def accept(self, task: str) -> None:
        """Owner Accept through the real accept_owner_release; only the broker's merge is stood in."""
        with board.locked_state(self.root) as state:
            state.setdefault("qa_requests", {})["final-" + task] = {
                "id": "final-" + task, "task": task, "stage": board.INDEPENDENT_REVIEW,
                "phase": "final_acceptance", "status": "passed", "mirror_ref": "refs/mirror/x",
                "reviewed_commit": "c0ffee", "reviewed_tree_hash": "tree", "cycle": 1,
                "requested_at": board.now(), "developer_id": "", "chunk": "final", "subtask": "",
                "claimed_by": None, "review_wait_started_at": board.now(),
            }
            state.setdefault("releases", {})[task] = {
                "task": task, "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto", "recorded_at": board.now(),
                "head_commit": "c0ffee", "acceptance_base_commit": "base", "acceptance_manifest": [],
            }
            state.setdefault("release_decisions", {})[task] = {"task": task, "decision": "accepted", "recorded_at": board.now()}
            state.setdefault("task_repositories", {})[task] = str(self.root)

        class _Broker:
            def accept_merge(self, task, candidate, *, board_mutation):
                record = {"commit": "c0ffee", "tree": "tree", "mirror_ref": "refs/mirror/x"}
                board_mutation(record)
                return record

        with patch.object(board, "_broker_for_state", return_value=_Broker()):
            board.accept_owner_release(self.root, task)

    def test_an_accepted_task_returns_its_reviewer_and_routes_the_waiting_review(self):
        self.delivery("ACCEPTED")
        session, reviewer = self.reviewer("ACCEPTED")
        waiting = self.open_review("NEXT")
        with board.locked_state(self.root) as state:
            state["releases"] = {}  # not yet released: the Reviewer is still pinned
        self.accept("ACCEPTED")
        self.assertIn("Reviewer returned to the review queue after ACCEPTED was accepted.", self.returned_events())
        self.assertEqual(self.agent(reviewer["id"])["task"], "NEXT", "returned, then given the waiting review")
        self.assertEqual(board.snapshot(self.root)["qa_requests"][waiting["id"]]["routed_to"], reviewer["id"])

    def test_a_reviewer_busy_on_another_task_is_untouched(self):
        self.delivery("ACCEPTED")
        other = self.open_review("OTHER")
        session, busy = self.reviewer("OTHER")
        with board.locked_state(self.root) as state:
            state["qa_requests"][other["id"]].update({"status": "claimed", "claimed_by": busy["id"]})
        before = dict(self.agent(busy["id"]))
        self.accept("ACCEPTED")
        after = self.agent(busy["id"])
        self.assertEqual((after["task"], after["status"]), (before["task"], before["status"]))
        self.assertEqual(self.returned_events(), [])


class EligibilityTests(_Fixture):
    def test_a_reviewer_left_bound_to_a_finished_task_is_still_eligible(self):
        session, reviewer = self.reviewer("OLD-FINISHED")
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})["OLD-FINISHED"] = {
                "task": "OLD-FINISHED", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto", "recorded_at": board.now(),
            }
        waiting = self.open_review("NEXT")
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertEqual(board.snapshot(self.root)["qa_requests"][waiting["id"]]["routed_to"], reviewer["id"])

    def test_the_cross_vendor_rule_still_holds_for_a_returned_reviewer(self):
        session, same_vendor = self.reviewer("OLD-FINISHED", vendor="OpenAI")
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})["OLD-FINISHED"] = {
                "task": "OLD-FINISHED", "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto", "recorded_at": board.now(),
            }
        waiting = self.open_review("NEXT")  # Delivery vendor is OpenAI
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertNotEqual(board.snapshot(self.root)["qa_requests"][waiting["id"]].get("routed_to"), same_vendor["id"])


if __name__ == "__main__":
    unittest.main()
