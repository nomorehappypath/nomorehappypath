# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The third identical refusal is a logged defect, the agent is told to stop retrying, the CTO is woken, and nothing is held (owner's order 2026-10-28).

Run:  PYTHONPATH=. python3 -m unittest tests.test_repeated_refusal_defect -v
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from harness import board, board_surface
from tests import test_refusal_hold_right_task as base

REFUSAL = "ledger has no concrete scenario rows"


class RepeatedRefusalDefectTests(unittest.TestCase):
    setUp = base._fixture["setUp"]
    begin_task = base._fixture["begin_task"]
    gateway = base.RefusalHoldTaskTests.gateway

    def developer(self, task="DEFECT-TASK"):
        from harness import control
        cto_session = control.create(self.context, "claude_cto")
        board.register(self.context, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
        agent, _ = self.begin_task(task)
        return SimpleNamespace(session_id=board.snapshot(self.context)["agents"][agent["id"]]["session_id"])

    def test_the_first_two_refusals_say_nothing_extra_and_the_third_tells_the_agent_to_move_on(self):
        identity, gateway = self.developer(), self.gateway()
        args = ["request-review", "--task", "DEFECT-TASK"]
        self.assertEqual(gateway._track_refusal(identity, "request-review", REFUSAL, args), "")
        self.assertEqual(gateway._track_refusal(identity, "request-review", REFUSAL, args), "")
        third = gateway._track_refusal(identity, "request-review", REFUSAL, args)
        self.assertIn("harness defect", third)
        self.assertIn("Do not retry", third)
        self.assertIn("CTO", third)
        self.assertIn("harness defect", gateway._track_refusal(identity, "request-review", REFUSAL, args))   # still told on the fourth

    def test_the_defect_is_recorded_once_with_no_hold_and_the_cto_is_asked_to_fix_it(self):
        identity, gateway = self.developer(), self.gateway()
        for _ in range(5):
            gateway._track_refusal(identity, "request-review", REFUSAL, ["request-review", "--task", "DEFECT-TASK"])
        state = board.snapshot(self.context)
        self.assertEqual(state.get("control_plane_holds") or {}, {})
        self.assertEqual(list(state["friction_defects"]), ["DEFECT-TASK:request-review"])
        self.assertEqual(state["friction_defects"]["DEFECT-TASK:request-review"]["message"], REFUSAL)
        events = [e for e in state["events"] if e.get("kind") == "repeated_refusal_defect"]
        self.assertEqual(len(events), 1)
        self.assertIn("task not held", events[0]["message"])
        self.assertIn("fix it locally", events[0]["message"])
        pending = [item for item in (state.get("cto_pending") or {}).values() if item.get("reason") == "stuck"]
        self.assertTrue(pending, "the CTO must be asked to fix it")

    def test_a_refusal_that_names_its_own_fix_is_never_a_defect(self):
        identity, gateway = self.developer(), self.gateway()
        for _ in range(5):
            self.assertEqual(gateway._track_refusal(identity, "qa-result", "the summary is empty. To fix it: write one line", ["qa-result", "--task", "DEFECT-TASK"]), "")
        self.assertFalse(board.snapshot(self.context).get("friction_defects"))

    def test_each_task_escalates_once_after_its_own_third_refusal(self):
        # Codex round 1, R1-1: the counter and the "already reported" memory were shared by every task, so a later task refused for
        # the same reason was told it had been reported while the board never recorded anything for it.
        first, second = self.developer("REVIEW-FIRST"), None
        agent, _ = self.begin_task("REVIEW-SECOND")
        second = SimpleNamespace(session_id=board.snapshot(self.context)["agents"][agent["id"]]["session_id"])
        gateway = self.gateway()
        for identity, task in ((first, "REVIEW-FIRST"), (second, "REVIEW-SECOND")):
            args = ["request-review", "--task", task]
            self.assertEqual(gateway._track_refusal(identity, "request-review", REFUSAL, args), "", f"{task}: no warning on refusal 1")
            self.assertEqual(gateway._track_refusal(identity, "request-review", REFUSAL, args), "", f"{task}: no warning on refusal 2")
            self.assertIn("harness defect", gateway._track_refusal(identity, "request-review", REFUSAL, args), f"{task}: told on refusal 3")
        state = board.snapshot(self.context)
        self.assertEqual(sorted(state["friction_defects"]), ["REVIEW-FIRST:request-review", "REVIEW-SECOND:request-review"])
        events = [e for e in state["events"] if e.get("kind") == "repeated_refusal_defect"]
        self.assertEqual(len(events), 2)
        self.assertEqual(state.get("control_plane_holds") or {}, {})
        stuck = [item for item in (state.get("cto_pending") or {}).values() if item.get("reason") == "stuck"]
        self.assertTrue(stuck, "the CTO is asked to fix it")
        self.assertTrue(any("REVIEW-SECOND" in json.dumps(item) for item in stuck), "the second task has its own CTO item")

    def test_the_threshold_is_three(self):
        self.assertEqual(board_surface.CommandGateway.REFUSAL_HOLD_THRESHOLD, 3)


if __name__ == "__main__":
    unittest.main()
