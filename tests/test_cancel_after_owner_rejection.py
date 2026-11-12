# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner, 2026-11-12: a released task he answered "Not accepted" could not be cancelled;
Cancel answered "this task is waiting for your test" while the page said it was in repair."""
import unittest

from harness import board
from tests import test_board as _board_tests


class CancelAfterOwnerRejectionTests(unittest.TestCase):
    setUp = _board_tests.BoardTests.setUp
    tearDown = _board_tests.BoardTests.tearDown
    delivery = _board_tests.BoardTests.delivery

    def released(self, task: str, decision: str = "") -> None:
        self.delivery(task)
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})[task] = {"task": task, "status": "VISUAL_TEST_REQUIRED", "cto_id": "cto-0001", "recorded_at": board.now()}
            if decision:
                state.setdefault("release_decisions", {})[task] = {"task": task, "decision": decision, "reason": "it does not work", "recorded_at": board.now()}

    def test_a_release_waiting_for_the_owners_test_still_cannot_be_cancelled(self):
        self.released("TASK-1")
        with self.assertRaisesRegex(ValueError, "waiting for your test"):
            board.cancel_task(self.root, "TASK-1")

    def test_a_release_the_owner_rejected_is_in_repair_and_can_be_cancelled(self):
        self.released("TASK-1", "not_accepted")
        board.cancel_task(self.root, "TASK-1")
        self.assertIn("TASK-1", board.snapshot(self.root).get("cancelled_tasks", {}))


if __name__ == "__main__":
    unittest.main()
