# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-09-28: a review PASS recorded after Delivery completed must not undo the completion.

Live order on studio: Delivery ran `complete`, then a re-certified subtask PASS set it back
to "independent_review_passed"; Delivery had ended, so the owner never got Accept.
"""
import unittest

from harness import cto

TASK = "T"


def state(after_completion):
    agent = {"id": "dev", "role": "engineering", "task": TASK, "active": False, "status": "independent_review_passed"}
    events = [{"sequence": 1, "kind": "development_complete", "task": TASK, "agent_id": "dev"}] + [
        {"sequence": 2 + index, "task": TASK, **event} for index, event in enumerate(after_completion)
    ]
    return {"agents": {"dev": agent}, "events": events}, agent


class DeliveryCompletionTests(unittest.TestCase):
    def test_a_pass_after_completion_leaves_delivery_complete(self):
        board_state, agent = state([{"kind": "qa_result", "result": "passed"}])
        self.assertTrue(cto._delivery_completed(board_state, TASK, agent))

    def test_anything_that_reopens_the_work_does_not(self):
        for event in ({"kind": "qa_result", "result": "failed"}, {"kind": "independent_review_requested"}, {"kind": "task_resumed"}):
            with self.subTest(event=event):
                board_state, agent = state([event])
                self.assertFalse(cto._delivery_completed(board_state, TASK, agent))

    def test_no_recorded_completion_is_not_complete(self):
        board_state, agent = state([])
        board_state["events"] = []
        self.assertFalse(cto._delivery_completed(board_state, TASK, agent))


if __name__ == "__main__":
    unittest.main()
